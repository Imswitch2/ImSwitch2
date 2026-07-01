import os

import h5py
import numpy as np
import tifffile as tiff
import zarr
import time

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.dataset_sources import resolve_dataset_source
from imswitch.improcess.model.image_sources import (
    axis_scales_from_element_size,
    default_axis_labels,
    dataset_names,
    flatten_metadata_attrs,
    is_array_node,
    is_structured_detector_group,
    is_zarr_array as _is_zarr_array,
    is_zarr_group as _is_zarr_group,
    resolve_image,
)


class DataObj:
    def __init__(self, name, datasetName, *, path=None, file=None):
        self.__logger = initLogger(self, instanceName=f'{name}/{datasetName}')

        self.name = name
        raw_path = path or (name if isinstance(name, str) and os.path.exists(name) else None)
        if raw_path is not None:
            try:
                self.dataPath = str(resolve_dataset_source(raw_path).path)
            except Exception:
                self.dataPath = raw_path
        else:
            self.dataPath = None
        self.darkFrame = None
        self._meanData = None
        self._file = file
        self._data = None
        self._datasetName = datasetName
        self._attrs = None
        self._resolvedImage = None
        self._axis_labels = None
        self._axis_scales = None
        self._scale_unit = None
        self._source_info = None
        self.__logger = initLogger(self, tryInheritParent=False)

    @property
    def data(self):
        if self._data is not None:
            return self._data

        if isinstance(self._file, h5py.File):
            self._data = np.array(self._resolveImage().array[:])
        elif isinstance(self._file, tiff.TiffFile):
            self._data = np.array(self._resolveImage().array.asarray())
        elif _is_zarr_group(self._file):
            self._data = np.array(self._resolveImage().array)
        return self._data

    @property
    def attrs(self):
        if self._attrs is not None:
            return self._attrs

        if isinstance(self._file, h5py.File):
            attrs = dict(self._file.attrs)
            attrs.update(self._resolveImage().attrs)
            self._attrs = attrs
        if _is_zarr_group(self._file):
            attrs = dict(self._file.attrs)
            attrs.update(self._resolveImage().attrs)
            self._attrs = attrs
        if isinstance(self._file, tiff.TiffFile):
            self._attrs = dict(self._resolveImage().attrs)
        return self._attrs

    @property
    def axis_labels(self):
        self._ensureMetadata()
        return self._axis_labels

    @property
    def axis_scales(self):
        self._ensureMetadata()
        return self._axis_scales

    @property
    def scale_unit(self):
        self._ensureMetadata()
        return self._scale_unit

    @property
    def source_info(self):
        self._ensureMetadata()
        return self._source_info

    @property
    def dataLoaded(self):
        return self.data is not None

    @property
    def datasetName(self):
        return self._datasetName

    @property
    def numFrames(self):
        return np.shape(self.data)[0] if self.data is not None else None

    def checkAndLoadData(self):
        if not self.dataLoaded:
            try:
                self._file, self._datasetName = DataObj._open(self.dataPath, self._datasetName)
                if self.data is not None:
                    self.__logger.debug('Data loaded')
            except Exception:
                pass

    def checkAndLoadDarkFrame(self):
        pass

    def checkAndUnloadData(self):
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                self.__logger.error('Error closing file')

        self._file = None
        self._data = None
        self._attrs = None
        self._resolvedImage = None
        self._meanData = None

    def getMeanData(self):
        if self._meanData is None:
            self._meanData = np.array(np.mean(self.data, 0), dtype=np.float32)

        return self._meanData

    def _resolveImage(self):
        if self._resolvedImage is None:
            self._resolvedImage = resolve_image(self._file, self._datasetName)
            self._applyResolvedImageMetadata(self._resolvedImage)
        return self._resolvedImage

    def _applyResolvedImageMetadata(self, image):
        ndim = len(image.array.shape)
        fallback_scales, fallback_unit = axis_scales_from_element_size(image.attrs, ndim)

        self._axis_labels = image.axis_labels or default_axis_labels(ndim)
        self._axis_scales = image.axis_scales or fallback_scales or [1.0] * ndim
        self._scale_unit = image.scale_unit or fallback_unit or "px"
        self._source_info = {
            "dataset_name": image.name,
            "dataset_path": image.array_path,
            "source_format": image.source_format,
        }

    def _ensureMetadata(self):
        if self._axis_labels is not None:
            return

        if (
            isinstance(self._file, h5py.File)
            or _is_zarr_group(self._file)
            or isinstance(self._file, tiff.TiffFile)
        ):
            self._resolveImage()
            return

        data = self._data
        if data is not None:
            ndim = np.ndim(data)
            self._axis_labels = default_axis_labels(ndim)
            self._axis_scales = [1.0] * ndim
            self._scale_unit = "px"
            self._source_info = {
                "dataset_name": self._datasetName,
                "dataset_path": None,
                "source_format": type(self._file).__name__ if self._file is not None else None,
            }

    @staticmethod
    def getDatasetNames(path):
        file, _ = DataObj._open(path, allowMultipleDatasets=True)
        try:
            if isinstance(file, h5py.File) or _is_zarr_group(file):
                return dataset_names(file)
            elif isinstance(file, tiff.TiffFile):
                return dataset_names(file)
            else:
                raise ValueError(f'Unsupported file type "{type(file).__name__}"')
        finally:
            if isinstance(file, h5py.File) or isinstance(file, tiff.TiffFile):
                file.close()

    @staticmethod
    def _open(path, datasetName=None, allowMultipleDatasets=False):
        source = resolve_dataset_source(path)
        path = str(source.path)
        if source.format_id == 'hdf5':
            file = h5py.File(path, 'r')
            datasetNames = dataset_names(file)
            if len(datasetNames) < 1:
                raise RuntimeError('File does not contain any datasets')
            elif len(datasetNames) > 1 and datasetName is None and not allowMultipleDatasets:
                raise RuntimeError('File contains multiple datasets')

            if datasetName is None and not allowMultipleDatasets:
                datasetName = datasetNames[0]
            elif datasetName not in datasetNames and len(datasetNames) == 1 and not allowMultipleDatasets:
                datasetName = datasetNames[0]

            return file, datasetName
        elif source.format_id == 'tiff':
            file = tiff.TiffFile(path)
            datasetNames = dataset_names(file)
            if len(datasetNames) < 1:
                raise RuntimeError('File does not contain any datasets')
            elif len(datasetNames) > 1 and datasetName is None and not allowMultipleDatasets:
                raise RuntimeError('File contains multiple datasets')

            if datasetName is None and not allowMultipleDatasets:
                datasetName = datasetNames[0]
            elif datasetName not in datasetNames and len(datasetNames) == 1 and not allowMultipleDatasets:
                datasetName = datasetNames[0]

            return file, datasetName
        elif source.format_id == 'zarr':
            file = zarr.open(path, mode='r')
            datasetNames = dataset_names(file)
            if len(datasetNames) < 1:
                raise RuntimeError('File does not contain any datasets')
            elif len(datasetNames) > 1 and datasetName is None and not allowMultipleDatasets:
                raise RuntimeError('File contains multiple datasets')

            if datasetName is None and not allowMultipleDatasets:
                datasetName = datasetNames[0]
            elif datasetName not in datasetNames and len(datasetNames) == 1 and not allowMultipleDatasets:
                datasetName = datasetNames[0]

            return file, datasetName
        else:
            raise ValueError(f'Unsupported file extension "{os.path.splitext(path)[1]}"')

    @staticmethod
    def _is_structured_detector_group(node):
        return is_structured_detector_group(node)

    @staticmethod
    def _is_array_node(node):
        return is_array_node(node)

    @staticmethod
    def _dataset_names(file):
        return dataset_names(file)

    @staticmethod
    def _resolve_dataset(file, datasetName):
        return resolve_image(file, datasetName).array

    @staticmethod
    def _read_dataset_attrs(file, datasetName):
        return resolve_image(file, datasetName).attrs

    @staticmethod
    def _flatten_metadata_attrs(metadata_group, prefix=None):
        return flatten_metadata_attrs(metadata_group, prefix)

    def describesSameAs(self, other):  # Don't use __eq__, that makes the class unhashable
        try:
            sameFile = self._file == other._file or self._file.filename == other._file.filename
        except AttributeError:
            sameFile = False

        return (self.name == other.name and
                self.dataPath == other.dataPath and
                sameFile and
                self.datasetName == other.datasetName)

    def checkLock(self):
        try:
            if self.attrs['writing']:
                raise OSError(f'Writing in progress')
        except Exception:
            pass
    
    def checkModifTime(self,minDiffTime):
        """ 
        This function checks last modif time of the file and throw error if too close to current time.
        arg:
            `minDiffTime`: float or int
                minimum difference time in sec for not throwing exception
        """

        lastModif = os.path.getmtime(self.dataPath)
        if lastModif + minDiffTime > time.time():
            raise OSError(f'Modif time less than {minDiffTime}sec ago')



# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
