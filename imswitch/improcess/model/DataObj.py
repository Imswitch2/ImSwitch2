import os

import h5py
import numpy as np
import tifffile as tiff
import zarr
import time

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.dataset_sources import resolve_dataset_source
from imswitch.improcess.model.acquisition_layout_resolver import (
    ResolvedAcquisitionLayout,
    adapt_tiling_manifest,
    persist_layout_override,
    resolve_acquisition_layout,
)
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
from imswitch.improcess.model.plane_navigation import (
    iter_planes,
    mean_plane,
    plane_count,
)
from imswitch.improcess.model.virtual_image import virtual_source_from_resolved_image


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
        self._dataSource = None
        self._datasetName = datasetName
        self._attrs = None
        self._resolvedImage = None
        self._axis_labels = None
        self._axis_scales = None
        self._scale_unit = None
        self._source_info = None
        self._acquisitionLayoutResolution = None
        self._acquisitionLayoutOverride = None
        self.sourceKind = "image"
        self.sourceMetadata = None
        self.sourceSummary = None
        self.sourceFingerprint = None
        self._metadataSourceReady = False
        self.__logger = initLogger(self, tryInheritParent=False)

    @classmethod
    def fromMetadataSource(
        cls, name, path, sourceKind, sourceMetadata, originalPath=None
    ):
        """Create a routing-ready source without opening an image container.

        ``originalPath`` is what the user actually selected, which is not
        always ``path``: opening any file inside a tiling run resolves to that
        run's manifest. Keeping it means a later choice of a reconstructor that
        wants the file itself can still be honoured, instead of the inferred
        source kind becoming permanent.
        """
        obj = cls(name, sourceKind)
        obj.dataPath = str(path)
        obj.sourceOriginalPath = str(
            path if originalPath is None else originalPath
        )
        obj.sourceKind = str(sourceKind)
        obj.sourceMetadata = sourceMetadata
        obj._attrs = {}
        obj._source_info = {
            "dataset_name": None,
            "dataset_path": str(path),
            "source_format": str(sourceKind),
        }
        obj._metadataSourceReady = True
        return obj

    @property
    def data(self):
        if self.sourceKind != "image":
            return None
        if self._data is not None:
            return self._data

        source = self.data_source
        if source is None:
            return None
        self._data = source.array.asarray()
        return self._data

    @property
    def data_source(self):
        if self.sourceKind != "image":
            return None
        if self._dataSource is None:
            self.checkAndOpenData()
        return self._dataSource

    @property
    def data_handle(self):
        source = self.data_source
        return source.array if source is not None else None

    @property
    def attrs(self):
        if self.sourceKind != "image":
            return self._attrs or {}
        if self._attrs is not None:
            return self._attrs

        if self._file is None and self.dataPath is not None:
            try:
                self.checkAndOpenData()
            except Exception:
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
    def acquisition_layout(self) -> ResolvedAcquisitionLayout:
        """Resolve acquisition semantics without materializing image pixels."""
        if self._acquisitionLayoutResolution is not None:
            return self._acquisitionLayoutResolution
        if self.sourceKind != "image":
            self._acquisitionLayoutResolution = adapt_tiling_manifest(
                self.sourceMetadata,
            )
            return self._acquisitionLayoutResolution

        if self._file is None and self.dataPath is not None:
            self.checkAndOpenData()
        image = self._resolveImage() if self._file is not None else None
        if image is not None:
            attrs = image.attrs
            shape = image.array.shape
            detector = (
                attrs.get("recording:detector_name")
                or attrs.get("detector_name")
                or self._datasetName
                or "unknown"
            )
            axis_labels = image.axis_labels
            dataset_path = image.array_path
            explicit_axes = any(
                key in attrs
                for key in ("ngff:axes", "tiff:axes", "axes", "_ARRAY_DIMENSIONS")
            )
        else:
            attrs = self._attrs or {}
            shape = np.shape(self._data)
            detector = self._datasetName or "unknown"
            axis_labels = self._axis_labels
            dataset_path = None
            explicit_axes = False

        # Remember what resolution was keyed on, so a persisted override can
        # be written against the same identity it will be read back with.
        self._acquisitionLayoutDetector = str(detector)
        self._acquisitionLayoutResolution = resolve_acquisition_layout(
            attrs,
            shape=shape,
            detector=str(detector),
            axis_labels=axis_labels,
            axis_metadata_explicit=explicit_axes,
            user_override=self._acquisitionLayoutOverride,
            source_path=self.dataPath,
            dataset_path=dataset_path,
            fingerprint=self.sourceFingerprint,
        )
        return self._acquisitionLayoutResolution

    @property
    def recording_lifecycle(self):
        if self.sourceKind != "image":
            return None
        if self._file is None and self.dataPath is not None:
            self.checkAndOpenData()
        return self._resolveImage().recording_lifecycle

    def setAcquisitionLayoutOverride(self, layout, *, persist: bool = False) -> None:
        """Apply a validated user choice, optionally in a fingerprinted sidecar."""
        self._acquisitionLayoutOverride = layout
        self._acquisitionLayoutResolution = None
        resolved = self.acquisition_layout
        if not persist:
            return
        if self.dataPath is None:
            raise ValueError("Cannot persist a layout override without a source path")
        # The identity the *resolver* will use when it reads the sidecar back,
        # not the one the layout happens to declare. They differ whenever the
        # container names its data something else -- a TIFF series is
        # ``Image0`` while the layout says ``Camera`` -- and the override was
        # then rejected as targeting a different detector the moment the file
        # was reopened, silently restoring the metadata it was written to
        # correct.
        persist_layout_override(
            self.dataPath,
            resolved.layout,
            detector=self._resolutionDetectorName(resolved),
            dataset_path=(self._source_info or {}).get("dataset_path"),
            fingerprint=self.sourceFingerprint,
        )

    def _resolutionDetectorName(self, resolved) -> str:
        """The detector name resolution is keyed on for this source."""
        name = self.__dict__.get("_acquisitionLayoutDetector")
        if name:
            return str(name)
        return str(getattr(resolved.layout, "detector", "") or "unknown")

    @property
    def dataLoaded(self):
        return self._data is not None

    @property
    def dataMaterialized(self):
        return self._data is not None

    @property
    def sourceLoaded(self):
        return self._dataSource is not None

    @property
    def sourceReady(self):
        """Whether this object is ready to be routed to source consumers."""
        if self.sourceKind != "image":
            return self._metadataSourceReady
        return self.dataLoaded or self.sourceLoaded

    @property
    def datasetName(self):
        return self._datasetName

    @property
    def numFrames(self):
        """Number of 2D planes available for display.

        This is ``shape[0]`` for the usual ``(frames, Y, X)`` stack, but it is
        the *product* of every non-``(Y, X)`` axis in general: a 4D dataset has
        to expose all of its planes on the one frame slider, and a plain 2D
        image has exactly one plane rather than one "frame" per pixel row.
        """
        if self.sourceKind != "image":
            return None
        if self._data is not None:
            shape = np.shape(self._data)
        else:
            source = self.data_source
            shape = source.array.shape if source is not None else ()
        if not shape:
            return None
        return plane_count(shape, self.axis_labels)

    def checkAndLoadData(self):
        if self.sourceKind != "image":
            return
        if not self.dataLoaded:
            try:
                self.checkAndOpenData()
                if self.data is not None:
                    self.__logger.debug('Data loaded')
            except Exception:
                pass

    def checkAndOpenData(self):
        if self.sourceKind != "image":
            return
        if self._dataSource is not None:
            return
        if self._file is None:
            if self.dataPath is None:
                return
            self._file, self._datasetName = DataObj._open(self.dataPath, self._datasetName)
        self._resolveImage()

    def checkAndLoadDarkFrame(self):
        pass

    def checkAndUnloadData(self):
        if self._dataSource is not None:
            try:
                self._dataSource.close()
            except Exception:
                self.__logger.error('Error closing data source')

        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                self.__logger.error('Error closing file')

        self._file = None
        self._data = None
        self._dataSource = None
        self._attrs = None
        self._resolvedImage = None
        self._acquisitionLayoutResolution = None
        self._meanData = None
        if self.sourceKind != "image":
            self._metadataSourceReady = False

    def getMeanData(self):
        """Mean 2D plane, averaged over every navigation axis.

        A lazy handle is accumulated one plane at a time so a dataset larger
        than memory can still be summarised.
        """
        if self.sourceKind != "image":
            return None
        if self._meanData is None:
            labels = self.axis_labels
            handle = self.data_handle
            if handle is not None and handle.ndim > 0 and not self.dataMaterialized:
                frame_count = plane_count(handle.shape, labels)
                if frame_count > 0:
                    accumulator = None
                    for frame in iter_planes(handle, labels):
                        if accumulator is None:
                            accumulator = np.zeros(frame.shape, dtype=np.float64)
                        accumulator += frame
                    self._meanData = np.asarray(
                        accumulator / frame_count,
                        dtype=np.float32,
                    )
                else:
                    self._meanData = mean_plane(self.data, labels)
            else:
                self._meanData = mean_plane(self.data, labels)

        return self._meanData

    def _resolveImage(self):
        if self._resolvedImage is None:
            self._resolvedImage = resolve_image(
                self._file,
                self._datasetName,
                validate_layout_metadata=False,
            )
            self._dataSource = virtual_source_from_resolved_image(self._resolvedImage)
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
        if self.sourceKind != "image":
            return
        if self._axis_labels is not None:
            return

        if self._dataSource is not None:
            return

        if self._file is None and self.dataPath is not None:
            try:
                self.checkAndOpenData()
                return
            except Exception:
                pass

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
