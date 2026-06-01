import os
from typing import Any

import h5py
import numpy as np
import tifffile as tiff
import zarr
import time

# zarr v2 placed the Group class under _ZarrGroup; v3 lifted it
# to the top level. Use a single name (`_ZarrGroup`) for isinstance checks
# regardless of which version is installed.
_ZarrGroup = getattr(zarr, "Group", None) or getattr(getattr(zarr, "hierarchy", None), "Group", None)
_ZarrArray = getattr(zarr, "Array", None) or getattr(getattr(zarr, "core", None), "Array", None)
from imswitch.imcommon.model import initLogger


def _is_zarr_group(obj: Any) -> bool:
    return _ZarrGroup is not None and isinstance(obj, _ZarrGroup)


def _is_zarr_array(obj: Any) -> bool:
    return _ZarrArray is not None and isinstance(obj, _ZarrArray)


class DataObj:
    def __init__(self, name, datasetName, *, path=None, file=None):
        self.__logger = initLogger(self, instanceName=f'{name}/{datasetName}')

        self.name = name
        self.dataPath = path or (name if isinstance(name, str) and os.path.exists(name) else None)
        self.darkFrame = None
        self._meanData = None
        self._file = file
        self._data = None
        self._datasetName = datasetName
        self._attrs = None
        self.__logger = initLogger(self, tryInheritParent=False)

    @property
    def data(self):
        if self._data is not None:
            return self._data

        if isinstance(self._file, h5py.File):
            self._data = np.array(DataObj._resolve_dataset(self._file, self._datasetName)[:])
        elif isinstance(self._file, tiff.TiffFile):
            self._data = self._file.asarray()
        elif _is_zarr_group(self._file):
            self._data = np.array(DataObj._resolve_dataset(self._file, self._datasetName))
        return self._data

    @property
    def attrs(self):
        if self._attrs is not None:
            return self._attrs

        if isinstance(self._file, h5py.File):
            attrs = dict(self._file.attrs)
            attrs.update(DataObj._read_dataset_attrs(self._file, self.datasetName))
            self._attrs = attrs
        if _is_zarr_group(self._file):
            attrs = dict(self._file.attrs)
            attrs.update(DataObj._read_dataset_attrs(self._file, self.datasetName))
            self._attrs = attrs
        return self._attrs

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
        self._meanData = None

    def getMeanData(self):
        if self._meanData is None:
            self._meanData = np.array(np.mean(self.data, 0), dtype=np.float32)

        return self._meanData

    @staticmethod
    def getDatasetNames(path):
        file, _ = DataObj._open(path, allowMultipleDatasets=True)
        try:
            if isinstance(file, h5py.File) or _is_zarr_group(file):
                return DataObj._dataset_names(file)
            elif isinstance(file, tiff.TiffFile):
                return ['default']
            else:
                raise ValueError(f'Unsupported file type "{type(file).__name__}"')
        finally:
            if isinstance(file, h5py.File):
                file.close()

    @staticmethod
    def _open(path, datasetName=None, allowMultipleDatasets=False):
        ext = os.path.splitext(path)[1]
        if ext in ['.hdf5', '.hdf', '.h5']:
            file = h5py.File(path, 'r')
            datasetNames = DataObj._dataset_names(file)
            if len(datasetNames) < 1:
                raise RuntimeError('File does not contain any datasets')
            elif len(datasetNames) > 1 and datasetName is None and not allowMultipleDatasets:
                raise RuntimeError('File contains multiple datasets')

            if datasetName is None and not allowMultipleDatasets:
                datasetName = datasetNames[0]
            elif datasetName not in datasetNames and len(datasetNames) == 1 and not allowMultipleDatasets:
                datasetName = datasetNames[0]

            return file, datasetName
        elif ext in ['.tiff', '.tif']:
            return tiff.TiffFile(path), None
        elif ext in ['.zarr']:
            file = zarr.open(path, mode='r')
            datasetNames = DataObj._dataset_names(file)
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
            raise ValueError(f'Unsupported file extension "{ext}"')

    @staticmethod
    def _is_structured_detector_group(node):
        if isinstance(node, h5py.Group):
            return 'data' in node and isinstance(node['data'], h5py.Dataset)
        if _is_zarr_group(node):
            return 'data' in node and _is_zarr_array(node['data'])
        return False

    @staticmethod
    def _is_array_node(node):
        return isinstance(node, h5py.Dataset) or _is_zarr_array(node)

    @staticmethod
    def _dataset_names(file):
        names = []
        for name in file.keys():
            node = file[name]
            if DataObj._is_array_node(node) or DataObj._is_structured_detector_group(node):
                names.append(name)
        return names

    @staticmethod
    def _resolve_dataset(file, datasetName):
        if datasetName is None:
            raise ValueError('datasetName is required')

        node = file[datasetName]
        if DataObj._is_structured_detector_group(node):
            return node['data']
        if DataObj._is_array_node(node):
            return node
        raise ValueError(f'Dataset "{datasetName}" is not an array or structured detector group')

    @staticmethod
    def _read_dataset_attrs(file, datasetName):
        node = file[datasetName]
        if DataObj._is_structured_detector_group(node):
            attrs = dict(node['data'].attrs)
            metadata = node.get('metadata')
            if metadata is not None:
                attrs.update(DataObj._flatten_metadata_attrs(metadata))
            return attrs
        if DataObj._is_array_node(node):
            return dict(node.attrs)
        return {}

    @staticmethod
    def _flatten_metadata_attrs(metadata_group, prefix=None):
        prefix = [] if prefix is None else prefix
        attrs = {}

        for key, value in dict(metadata_group.attrs).items():
            attrs[':'.join([*prefix, key])] = value

        for name in metadata_group.keys():
            child = metadata_group[name]
            if isinstance(child, h5py.Group) or _is_zarr_group(child):
                attrs.update(DataObj._flatten_metadata_attrs(child, [*prefix, name]))

        return attrs

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
        print(lastModif)
        print(time.time())
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
