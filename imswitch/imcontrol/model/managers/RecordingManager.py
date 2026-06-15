import enum
import json
import os
import time
import threading
import queue
from io import BytesIO
from typing import Any, Dict, List, Optional, Type, Union

import h5py
import zarr
import numpy as np
import tifffile as tiff

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Worker
from imswitch.imcommon.model import initLogger
import abc
import logging

from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager

logger = logging.getLogger(__name__)

# Recording loop constants
FRAME_POLL_INTERVAL = 0.0001  # seconds; prevents UI freezing during acquisition
DEFAULT_STALL_TIMEOUT = 10.0  # seconds; watchdog triggers if no frames arrive within this period
_RECORDING_CHUNK_CONSUMER = 'RecordingManager'  # readChunk consumer key (see DetectorManager.readChunk)

# Off-thread writer constants
WRITER_QUEUE_MAXSIZE = 64  # Bounded queue size for backpressure (blocks acquisition when full)
WRITE_BATCH_FRAMES = 32  # Number of frames to accumulate per detector before flushing to disk


class AsTemporaryFile(object):
    """ A temporary file that when exiting the context manager is renamed to its original name. """
    def __init__(self, filepath, tmp_extension='.tmp'):
        if os.path.exists(filepath):
            raise FileExistsError(f'File {filepath} already exists.')
        self.path = filepath
        self.tmp_path = filepath + tmp_extension

    def __enter__(self):
        return self.tmp_path

    def __exit__(self, *args, **kwargs):
        if args and args[0] is not None:
            return False
        os.rename(self.tmp_path, self.path)


class Storer(abc.ABC):
    """Base class for storing detector data to disk.
    
    All storers use the (T, Y, X) axis convention:
    - T: time/frame dimension
    - Y: vertical image dimension (rows)
    - X: horizontal image dimension (columns)
    
    This matches numpy array indexing and the _record streaming format.
    No axis reversal or transposition is applied.
    """
    def __init__(self, filepath, detectorManager):
        self.filepath = filepath
        self.detectorManager: DetectorsManager = detectorManager

    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Store snapshot images with metadata.
        
        Args:
            images: Dict mapping detector name to image array (T, Y, X) or (Y, X)
            attrs: Dict mapping detector name to flat metadata dict with ':'-separated keys
        """
        raise NotImplementedError

    # Streaming lifecycle for RecordingWorker
    def openStream(self, fileDests: Dict[str, Union[str, 'BytesIO']], detectorNames: List[str],
                   shapes: Dict[str, tuple], attrs: Dict[str, Dict[str, str]], *,
                   singleMultiDetectorFile: bool, singleLapseFile: bool, saveMode) -> None:
        """Initialize streaming recording session.
        
        Called once before frame acquisition starts. Opens files and prepares for streaming writes.
        
        Args:
            fileDests: Dict mapping detector name to file path (str) or BytesIO for RAM mode
            detectorNames: List of detector names to record
            shapes: Dict mapping detector name to detector shape (Y, X)
            attrs: Dict mapping detector name to flat metadata dict
            singleMultiDetectorFile: Whether all detectors share one file
            singleLapseFile: Whether lapse scans share one file with multiple datasets
            saveMode: SaveMode enum value (Disk, RAM, DiskAndRAM)
        """
        raise NotImplementedError

    def writeFrames(self, detectorName: str, frames: np.ndarray) -> None:
        """Write frame chunk to stream.
        
        Lazily creates dataset on first call (deriving dtype from frames).
        Subsequent calls append to the dataset.
        
        Args:
            detectorName: Name of detector
            frames: Frame chunk array, shape (N, Y, X) or (Y, X)
        """
        raise NotImplementedError

    def finalizeStream(self, currentFrames: Dict[str, int], filePaths: Dict[str, str],
                       recordingManager, saveMode) -> None:
        """Finalize streaming session and emit signals.
        
        Called once after acquisition ends. Closes files, removes empty datasets,
        and emits sigMemoryRecordingAvailable for RAM/DiskAndRAM modes.
        
        Args:
            currentFrames: Dict mapping detector name to total frames written
            filePaths: Dict mapping detector name to file path (for signal emission)
            recordingManager: RecordingManager instance (for signal emission)
            saveMode: SaveMode enum value
        """
        raise NotImplementedError
    
    @staticmethod
    def _group_metadata_by_category(attrs: Dict[str, str]) -> Dict[str, Dict[str, str]]:
        """Group flat metadata by category prefix.
        
        Input attrs have keys like 'detector:exposure', 'lasers:laser1:power', 'uncategorized'.
        Output groups by first component: {'detector': {'exposure': ...}, 'lasers': {'laser1:power': ...}}.
        Keys without ':' go to the '' (empty string) category.
        
        Args:
            attrs: Flat dict with ':'-separated keys
            
        Returns:
            Dict mapping category to sub-dict of keys within that category
        """
        grouped = {}
        if not isinstance(attrs, dict):
            # Defensive: callers occasionally pass a scalar (e.g. a single
            # description string) where a flat per-detector attribute dict is
            # expected. Treat that as "no metadata" rather than blowing up
            # inside .items(), since the structured detector group is still
            # valid without metadata.
            return grouped
        for key, value in attrs.items():
            if ':' in key:
                category, _, rest = key.partition(':')
            else:
                category, rest = '', key
            
            if category not in grouped:
                grouped[category] = {}
            grouped[category][rest or key] = value
        
        return grouped


class ZarrStorer(Storer):
    """Storer for Zarr format with the same structured layout as HDF5."""

    @staticmethod
    def _make_store(path: str) -> Any:
        if hasattr(zarr.storage, 'DirectoryStore'):
            return zarr.storage.DirectoryStore(path)
        return zarr.storage.LocalStore(path)

    @staticmethod
    def _close_store(store) -> None:
        close = getattr(store, 'close', None)
        if close is not None:
            close()

    @staticmethod
    def _create_array(root: Any, name: str, *, data: Any = None,
                      shape: tuple | None = None, chunks: tuple | None = None,
                      dtype: Any = None) -> Any:
        if hasattr(root, 'create_dataset'):
            kwargs = {'chunks': chunks}
            if data is not None:
                kwargs['data'] = data
            if shape is not None:
                kwargs['shape'] = shape
            if dtype is not None:
                kwargs['dtype'] = dtype
            return root.create_dataset(name, **kwargs)

        kwargs = {'chunks': chunks}
        if data is not None:
            kwargs['data'] = data
        else:
            kwargs['shape'] = shape
            kwargs['dtype'] = dtype
        return root.create_array(name, **kwargs)

    @staticmethod
    def _require_group(parent: Any, name: str) -> Any:
        if name in parent:
            return parent[name]
        return parent.create_group(name)

    @staticmethod
    def _zarr_attr_value(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, bytes):
            return value.decode(errors='replace')
        if isinstance(value, (list, tuple)):
            return [ZarrStorer._zarr_attr_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): ZarrStorer._zarr_attr_value(val)
                    for key, val in value.items()}
        return value

    def _set_attrs(self, target: Any, attrs: Dict[str, Any], context: str) -> None:
        for key, value in attrs.items():
            try:
                target.attrs[key] = self._zarr_attr_value(value)
            except Exception as e:
                logger.debug(f'Could not save Zarr metadata {context}/{key}={value}: {e}')

    def _createDetectorGroup(self, root: Any, detectorName: str, dtype: Any,
                             attrs: Dict[str, Any], *, data: Any = None,
                             groupPath: str | None = None, writing: bool = False) -> Any:
        """Create structured Zarr detector group with ``data`` and ``metadata``."""
        parent = root
        if groupPath:
            parent = self._require_group(root, groupPath)

        det_group = self._require_group(parent, detectorName)
        if 'data' in det_group:
            raise ValueError(f'Zarr data array already exists for detector {detectorName}')

        if data is not None:
            data = np.asarray(data)
            if data.ndim == 2:
                data = data[np.newaxis, ...]
            spatialShape = data.shape[-2:]
            chunks = (1, *spatialShape)
            dataset = self._create_array(
                det_group,
                'data',
                data=data,
                chunks=chunks,
                dtype=dtype,
            )
        else:
            spatialShape = None
            dataset = None

        if dataset is None:
            raise ValueError('Zarr detector group creation requires data or streaming frames')

        dataset.attrs['detector_name'] = detectorName
        dataset.attrs['element_size_um'] = self._zarr_attr_value(
            self.detectorManager[detectorName].pixelSizeUm
        )
        dataset.attrs['axes'] = ['T', 'Y', 'X']
        dataset.attrs['writing'] = writing

        grouped = self._group_metadata_by_category(attrs)
        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:
                    cat_group = meta_group.create_group(category)
                    self._set_attrs(cat_group, cat_attrs, category)
                else:
                    self._set_attrs(meta_group, cat_attrs, 'metadata')

        return dataset

    def _createStreamingDetectorGroup(self, root: Any, detectorName: str,
                                      dtype: np.dtype, spatialShape: tuple, attrs: Dict[str, Any],
                                      groupPath: str | None = None) -> Any:
        parent = root
        if groupPath:
            parent = self._require_group(root, groupPath)

        det_group = self._require_group(parent, detectorName)
        if 'data' in det_group:
            raise ValueError(f'Zarr data array already exists for detector {detectorName}')

        # Use multi-frame chunks for better compression ratio and fewer I/O ops
        chunk_frames = min(WRITE_BATCH_FRAMES, 32)  # Match batch size for efficiency
        dataset = self._create_array(
            det_group,
            'data',
            shape=(0, *spatialShape),
            dtype=dtype,
            chunks=(chunk_frames, *spatialShape),
        )
        dataset.attrs['detector_name'] = detectorName
        dataset.attrs['element_size_um'] = self._zarr_attr_value(
            self.detectorManager[detectorName].pixelSizeUm
        )
        dataset.attrs['axes'] = ['T', 'Y', 'X']
        dataset.attrs['writing'] = True

        grouped = self._group_metadata_by_category(attrs)
        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:
                    cat_group = meta_group.create_group(category)
                    self._set_attrs(cat_group, cat_attrs, category)
                else:
                    self._set_attrs(meta_group, cat_attrs, 'metadata')

        return dataset

    def snap(self, images: Dict[str, np.ndarray],
             attrs: Dict[str, Dict[str, Any]] = None) -> None:
        attrs = attrs or {}
        with AsTemporaryFile(f'{self.filepath}.zarr') as path:
            store = self._make_store(path)
            root = zarr.group(store=store, overwrite=True)
            root.attrs['timestamp'] = time.time()
            root.attrs['rec_mode'] = 'snap'

            for channel, image in images.items():
                channel_attrs = attrs.get(channel, {})
                self._createDetectorGroup(
                    root,
                    channel,
                    np.asarray(image).dtype,
                    channel_attrs,
                    data=image,
                )
            self._close_store(store)
            logger.info(f"Saved image to zarr store {path} with structured layout")
    
    def openStream(self, fileDests: Dict[str, Union[str, BytesIO]],
                   detectorNames: List[str], shapes: Dict[str, tuple],
                   attrs: Dict[str, Dict[str, Any]], *,
                   singleMultiDetectorFile: bool, singleLapseFile: bool,
                   saveMode: 'SaveMode') -> None:
        """Initialize Zarr streaming session.

        The first pass supports disk-backed stores. RAM-backed Zarr recording
        needs a separate MemoryStore policy because a Zarr directory cannot be
        represented by the existing BytesIO memory-recording signal contract.
        """
        if saveMode == SaveMode.RAM:
            raise NotImplementedError('Zarr RAM streaming is not supported yet')

        self._stores = {}
        self._roots = {}
        self._datasets = {}
        self._attrs = attrs
        self._fileDests = fileDests
        self._currentFrames = {}
        self._groupPaths = {}
        self._dtypeWarned = set()  # Track detectors for which dtype mismatch was warned
        
        for detectorName in detectorNames:
            dest = fileDests[detectorName]
            if dest not in self._stores:
                store = self._make_store(dest)
                self._stores[dest] = store
                root = zarr.group(store=store, overwrite=not singleLapseFile)
                root.attrs['timestamp'] = time.time()
                root.attrs['rec_mode'] = 'recording'
                self._roots[dest] = root

            if singleLapseFile:
                scanNum = 0
                scanGroup = f'scan{scanNum}'
                root = self._roots[dest]
                while scanGroup in root:
                    scanNum += 1
                    scanGroup = f'scan{scanNum}'
                self._groupPaths[detectorName] = scanGroup
            else:
                self._groupPaths[detectorName] = None

            self._currentFrames[detectorName] = 0
    
    def writeFrames(self, detectorName: str, frames: np.ndarray) -> None:
        """Write frames to Zarr dataset, lazily creating it from declared dtype."""
        if len(frames) == 0:
            return

        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[np.newaxis, ...]

        if detectorName not in self._datasets:
            # Read the authoritative dtype from the detector (the contract's single source of truth)
            declared = self.detectorManager[detectorName].dtype
            spatialShape = frames.shape[-2:]
            
            root = self._roots.get(self._fileDests[detectorName])
            if root is None:
                raise RuntimeError(f'No Zarr root available for detector {detectorName}')
            # Create dataset with the DECLARED dtype (the contract), not frames.dtype
            self._datasets[detectorName] = self._createStreamingDetectorGroup(
                root,
                detectorName,
                declared,
                spatialShape,
                self._attrs[detectorName],
                groupPath=self._groupPaths[detectorName],
            )

        # Warn-once on dtype mismatch (loud alert, not silent)
        dataset = self._datasets[detectorName]
        if frames.dtype != dataset.dtype and detectorName not in self._dtypeWarned:
            logger.warning(
                f"ZarrStorer dtype mismatch for '{detectorName}': "
                f"declared={dataset.dtype}, actual frame={frames.dtype}. "
                f"Recording with declared dtype (frames will be cast). "
                f"This warning is shown once per detector per recording."
            )
            self._dtypeWarned.add(detectorName)

        # Append frames to dataset (cast happens on assignment if needed)
        it = self._currentFrames[detectorName]
        newSize = it + len(frames)
        dataset.resize((newSize, *dataset.shape[-2:]))
        dataset[it:newSize, :, :] = frames
        
        self._currentFrames[detectorName] += len(frames)
    
    def finalizeStream(self, currentFrames: Dict[str, int], filePaths: Dict[str, str],
                       recordingManager, saveMode: 'SaveMode') -> None:
        """Close Zarr stores and emit memory-recording signals when applicable."""
        for detectorName, dataset in self._datasets.items():
            dataset.attrs['writing'] = False
            if currentFrames[detectorName] < 1:
                dataset.resize((0, *dataset.shape[-2:]))

        if saveMode == SaveMode.DiskAndRAM:
            for detectorName in self._datasets:
                filePath = filePaths[detectorName]
                name = os.path.basename(filePath)
                recordingManager.sigMemoryRecordingAvailable.emit(
                    name, self._roots[self._fileDests[detectorName]], filePath, True
                )

        for store in self._stores.values():
            self._close_store(store)


class HDF5Storer(Storer):
    """Storer for HDF5 format with structured layout.
    
    Snapshot layout:
        <file>.h5
          @imswitch_version (future)
          @timestamp
          @rec_mode = 'snap'
          <detectorName>/
            data              # (T, Y, X) or (Y, X), dtype from frame, gzip compressed
              @detector_name
              @element_size_um
            metadata/
              <category>/     # e.g., 'detector', 'lasers', 'scan'
                @key = value  # attrs within category

    Compression: gzip (lossless, universally supported by Fiji/h5view) by default.
    """

    def __init__(self, filepath, detectorManager, compression='gzip'):
        """Initialize HDF5 storer.

        Args:
            filepath: Base path for output file (without extension)
            detectorManager: DetectorsManager instance
            compression: Compression filter ('gzip', 'lzf', None, or h5py compression spec).
                         Use 'gzip' (default) for Fiji/h5view compatibility; 'lzf' is faster
                         but requires the LZF plugin in external readers.
        """
        super().__init__(filepath, detectorManager)
        self.compression = compression

    def _createDetectorGroup(self, h5file, detectorName, dtype, attrs, *,
                             maxshape=None, data=None, groupPath=None):
        """Create structured HDF5 detector group with data and metadata.

        Creates the unified structured layout used by both snapshots and recordings:
            <groupPath>/<detectorName>/data           - dataset with compression
            <groupPath>/<detectorName>/metadata/      - grouped attributes

        Args:
            h5file: Open h5py.File
            detectorName: Name of detector (used as group name)
            dtype: Numpy dtype for data dataset
            attrs: Flat dict of metadata attributes (with ':'-separated keys)
            maxshape: If provided, creates extendable dataset (for streaming).
                      Should be (None, Y, X) for time-extendable recordings.
            data: If provided (and maxshape is None), creates fixed dataset from data.
            groupPath: Optional parent path (e.g., 'scan0' for lapse files).
                       Detector group created at <groupPath>/<detectorName>.

        Returns:
            dataset: The created data dataset (for further writes)
        """
        # Create group hierarchy
        if groupPath:
            if groupPath not in h5file:
                parent = h5file.create_group(groupPath)
            else:
                parent = h5file[groupPath]
            det_group = parent.create_group(detectorName)
        else:
            det_group = h5file.create_group(detectorName)

        # Create data dataset
        if maxshape is not None:
            # Extendable dataset for streaming (start with 0 frames)
            shape = maxshape[-2:]  # (Y, X)
            # Use multi-frame chunks for better compression ratio and fewer I/O ops
            chunk_frames = min(WRITE_BATCH_FRAMES, 32)  # Match batch size for efficiency
            dataset = det_group.create_dataset(
                'data',
                shape=(0, *shape),
                maxshape=maxshape,
                dtype=dtype,
                compression=self.compression,
                shuffle=True if self.compression else False,
                chunks=(chunk_frames, *shape)  # Multi-frame chunks for batched writes
            )
        else:
            # Fixed dataset from data (snapshot)
            if data is None:
                raise ValueError("Must provide either maxshape or data")
            # Ensure 3D: (T, Y, X)
            if data.ndim == 2:
                data = data[np.newaxis, ...]
            chunks = (1, *data.shape[-2:]) if data.ndim >= 3 else True
            dataset = det_group.create_dataset(
                'data',
                data=data,
                dtype=dtype,
                compression=self.compression,
                shuffle=True if self.compression else False,
                chunks=chunks
            )

        # Dataset-level metadata
        dataset.attrs['detector_name'] = detectorName
        dataset.attrs['element_size_um'] = self.detectorManager[detectorName].pixelSizeUm

        # Group attrs by category and create metadata subgroups
        grouped = self._group_metadata_by_category(attrs)

        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:  # Non-empty category -> subgroup
                    cat_group = meta_group.create_group(category)
                    for key, value in cat_attrs.items():
                        try:
                            cat_group.attrs[key] = value
                        except Exception as e:
                            logger.debug(f'Could not save metadata {category}/{key}={value}: {e}')
                else:  # Empty category -> flat in metadata group
                    for key, value in cat_attrs.items():
                        try:
                            meta_group.attrs[key] = value
                        except Exception as e:
                            logger.debug(f'Could not save metadata {key}={value}: {e}')

        return dataset

    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Save snapshot with structured HDF5 layout.

        Creates one file per detector with structured groups and lossless compression.
        """
        attrs = attrs or {}

        for channel, image in images.items():
            with AsTemporaryFile(f'{self.filepath}_{channel}.h5') as path:
                with h5py.File(path, 'w') as file:
                    # File-level metadata
                    file.attrs['timestamp'] = time.time()
                    file.attrs['rec_mode'] = 'snap'

                    # Create structured detector group using shared helper
                    channel_attrs = attrs.get(channel, {})
                    self._createDetectorGroup(
                        file, channel, image.dtype, channel_attrs,
                        data=image
                    )

                logger.info(f"Saved snapshot to {path} with structured HDF5 layout")
    
    def openStream(self, fileDests, detectorNames, shapes, attrs, *,
                   singleMultiDetectorFile, singleLapseFile, saveMode):
        """Initialize HDF5 streaming session."""
        self._files = {}
        self._datasets = {}
        self._fileDests = fileDests
        self._shapes = shapes
        self._attrs = attrs
        self._singleMultiDetectorFile = singleMultiDetectorFile
        self._singleLapseFile = singleLapseFile
        self._groupPaths = {}  # Track group paths for lapse files
        self._saveMode = saveMode
        self._dtypeWarned = set()  # Track detectors for which dtype mismatch was warned

        # Temporarily disable compression for RAM mode (BytesIO) due to h5py instability
        self._streamCompression = None if saveMode == SaveMode.RAM else self.compression

        # Open HDF5 files
        for detectorName in detectorNames:
            if singleMultiDetectorFile and len(self._files) > 0:
                # Reuse first file for all detectors
                self._files[detectorName] = list(self._files.values())[0]
            else:
                # Open new file (append mode for lapse files, write mode otherwise)
                mode = 'a' if singleLapseFile else 'w-'
                # Let h5py automatically handle BytesIO (RAM mode) vs file paths (Disk mode)
                self._files[detectorName] = h5py.File(fileDests[detectorName], mode)

            # Determine group path for lapse files (structured: scan{N}/detector)
            if singleLapseFile:
                scanNum = 0
                scanGroup = f'scan{scanNum}'
                file = self._files[detectorName]
                while scanGroup in file:
                    scanNum += 1
                    scanGroup = f'scan{scanNum}'
                self._groupPaths[detectorName] = scanGroup
            else:
                self._groupPaths[detectorName] = None

            # Dataset creation is LAZY - deferred until first writeFrames call

    def writeFrames(self, detectorName, frames):
        """Write frames to HDF5 dataset, lazily creating structured group on first call."""
        if len(frames) == 0:
            return

        # Lazy dataset creation using structured layout
        if detectorName not in self._datasets:
            # Read the authoritative dtype from the detector (the contract's single source of truth)
            declared = self.detectorManager[detectorName].dtype
            
            # Derive spatial dims from the frame arrays themselves. The detector
            # .shape attribute is (X, Y) while frame arrays follow numpy's
            # (n, Y, X) convention, so using _shapes here would mis-broadcast
            # for non-square detectors.
            spatialShape = frames.shape[-2:]

            file = self._files[detectorName]
            groupPath = self._groupPaths[detectorName]

            # Temporarily override compression for streaming
            original_compression = self.compression
            self.compression = self._streamCompression
            try:
                # Create dataset with the DECLARED dtype (the contract), not frames.dtype
                dataset = self._createDetectorGroup(
                    file, detectorName, declared, self._attrs[detectorName],
                    maxshape=(None, *spatialShape),  # (None, Y, X)
                    groupPath=groupPath
                )
            finally:
                self.compression = original_compression
            
            dataset.attrs['writing'] = True
            self._datasets[detectorName] = dataset

        # Warn-once on dtype mismatch (loud alert, not silent)
        dataset = self._datasets[detectorName]
        if frames.dtype != dataset.dtype and detectorName not in self._dtypeWarned:
            logger.warning(
                f"HDF5Storer dtype mismatch for '{detectorName}': "
                f"declared={dataset.dtype}, actual frame={frames.dtype}. "
                f"Recording with declared dtype (frames will be cast). "
                f"This warning is shown once per detector per recording."
            )
            self._dtypeWarned.add(detectorName)

        # Append frames to structured dataset (cast happens on assignment if needed)
        currentSize = dataset.shape[0]
        newSize = currentSize + len(frames)
        dataset.resize(newSize, axis=0)
        dataset[currentSize:newSize, :, :] = frames
    
    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        """Close HDF5 files and emit signals."""
        # Track unique files to avoid duplicate close (singleMultiDetectorFile mode)
        processed_files = set()
        
        for detectorName, file in self._files.items():
            # Remove empty datasets (if no frames captured)
            dataset = self._datasets.get(detectorName)
            if dataset is not None and currentFrames[detectorName] < 1:
                dataset.resize(0, axis=0)
            
            # Mark dataset as complete
            if dataset is not None:
                dataset.attrs['writing'] = False
            
            # Emit signal for each detector (even in singleMultiDetectorFile mode)
            if saveMode == SaveMode.RAM or saveMode == SaveMode.DiskAndRAM:
                filePath = filePaths[detectorName]
                name = os.path.basename(filePath)
                if saveMode == SaveMode.RAM:
                    recordingManager.sigMemoryRecordingAvailable.emit(
                        name, self._fileDests[detectorName], filePath, False
                    )
                else:  # DiskAndRAM
                    recordingManager.sigMemoryRecordingAvailable.emit(
                        name, file, filePath, True
                    )
            
            # Only close/flush each unique file once (handles singleMultiDetectorFile mode)
            file_id = id(file)
            if file_id in processed_files:
                continue
            processed_files.add(file_id)
            
            if saveMode == SaveMode.RAM:
                file.close()
            elif saveMode == SaveMode.DiskAndRAM:
                file.flush()
            elif saveMode == SaveMode.Disk:
                file.close()


class TiffStorer(Storer):
    """Storer for TIFF format with ImageJ-compatible metadata."""
    
    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Save snapshot as TIFF with ImageJ metadata.
        
        Uses tifffile's ImageJ mode for multi-frame compatibility.
        Metadata is embedded as ImageJ metadata and custom TIFF tags.
        """
        attrs = attrs or {}
        
        for channel, image in images.items():
            with AsTemporaryFile(f'{self.filepath}_{channel}.tiff') as path:
                # Prepare metadata for ImageJ
                channel_attrs = attrs.get(channel, {})
                pixel_size_um = self.detectorManager[channel].pixelSizeUm[0]  # Assume square pixels
                
                # Build ImageJ-compatible metadata dict
                metadata = {
                    'axes': 'TYX' if image.ndim == 3 else 'YX',
                    'unit': 'um',
                    'spacing': pixel_size_um,
                }
                
                # Add custom attrs as ImageJ metadata (will appear in ImageJ info window)
                info_lines = [f'detector_name={channel}']
                for key, value in channel_attrs.items():
                    # Flatten ':'-separated keys for ImageJ display
                    info_lines.append(f'{key}={value}')
                metadata['Info'] = '\n'.join(info_lines)
                
                # Write TIFF with ImageJ metadata
                tiff.imwrite(
                    path,
                    image,
                    imagej=True,
                    resolution=(1.0/pixel_size_um, 1.0/pixel_size_um),  # pixels per micron
                    metadata=metadata
                )
                logger.info(f"Saved snapshot to {path} with ImageJ metadata")
    
    def openStream(self, fileDests, detectorNames, shapes, attrs, *,
                   singleMultiDetectorFile, singleLapseFile, saveMode):
        """Initialize TIFF streaming session."""
        self._filenames = {}
        self._basePaths = {}
        self._partNumbers = {}
        self._dtypeWarned = set()  # Track detectors for which dtype mismatch was warned

        # Determine output file paths
        for detectorName in detectorNames:
            # TIFF files are created per-detector (no singleMultiDetectorFile support)
            basePath = fileDests[detectorName]
            self._basePaths[detectorName] = basePath
            self._filenames[detectorName] = basePath
            self._partNumbers[detectorName] = 1

    def writeFrames(self, detectorName, frames):
        """Write frames to TIFF file (append mode) with automatic >4GB rollover."""
        if len(frames) == 0:
            return

        # Read the authoritative dtype from the detector (the contract's single source of truth)
        declared = self.detectorManager[detectorName].dtype
        
        # Warn-once on dtype mismatch (loud alert, not silent)
        if frames.dtype != declared and detectorName not in self._dtypeWarned:
            logger.warning(
                f"TiffStorer dtype mismatch for '{detectorName}': "
                f"declared={declared}, actual frame={frames.dtype}. "
                f"Recording with declared dtype (frames will be cast). "
                f"This warning is shown once per detector per recording."
            )
            self._dtypeWarned.add(detectorName)
        
        # Cast to declared dtype if needed (now logged, not silent)
        if frames.dtype != declared:
            frames = frames.astype(declared)

        filePath = self._filenames[detectorName]
        try:
            tiff.imwrite(filePath, frames, append=True)
        except ValueError as e:
            # TIFF file exceeded 4GB limit - rollover to next part
            logger.warning(f"TIFF file exceeded 4GB limit: {filePath}. Rolling over to next part.")

            # Generate next part filename
            basePath = self._basePaths[detectorName]
            self._partNumbers[detectorName] += 1
            partNum = self._partNumbers[detectorName]

            # Insert _part{N} before extension
            if basePath.endswith('.tiff'):
                newPath = basePath[:-5] + f'_part{partNum}.tiff'
            elif basePath.endswith('.tif'):
                newPath = basePath[:-4] + f'_part{partNum}.tif'
            else:
                newPath = basePath + f'_part{partNum}.tiff'

            self._filenames[detectorName] = newPath
            logger.info(f"Continuing recording to: {newPath}")

            # Write frames to new file (create mode, not append)
            try:
                tiff.imwrite(newPath, frames, append=False)
            except Exception as write_error:
                logger.error(f"Failed to write to rollover file {newPath}: {write_error}")
    
    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        """TIFF files are automatically closed by tifffile. Nothing to finalize."""
        # TIFF writes are direct to disk - no RAM mode or special cleanup needed
        pass


class SaveMode(enum.Enum):
    Disk = 1
    RAM = 2
    DiskAndRAM = 3
    Numpy = 4


class SaveFormat(enum.Enum):
    HDF5 = 1
    TIFF = 2
    ZARR = 3


DEFAULT_STORER_MAP: Dict[str, Type[Storer]] = {
    SaveFormat.ZARR: ZarrStorer,
    SaveFormat.HDF5: HDF5Storer,
    SaveFormat.TIFF: TiffStorer
}


class RecordingManager(SignalInterface):
    """ RecordingManager handles single frame captures as well as continuous
    recordings of detector data. """
    sigRecordingStarted = Signal()
    sigRecordingEnded = Signal()
    sigRecordingStalled = Signal(str)  # (detectorName) - emitted when watchdog detects zero-progress stall
    sigRecordingFrameNumUpdated = Signal(int)  # (frameNumber)
    sigRecordingTimeUpdated = Signal(int)  # (recTime)
    sigMemorySnapAvailable = Signal(
        str, np.ndarray, object, bool
    )  # (name, image, filePath, savedToDisk)
    sigMemoryRecordingAvailable = Signal(
        str, object, object, bool
    )  # (name, file, filePath, savedToDisk)

    def __init__(self, detectorsManager, storerMap: Optional[Dict[str, Type[Storer]]] = None):
        super().__init__()
        self.__logger = initLogger(self)
        self.__storerMap = storerMap or DEFAULT_STORER_MAP
        self._memRecordings = {}  # { filePath: bytesIO }
        self.__detectorsManager = detectorsManager
        self.__record = False
        self.__recordingWorker = RecordingWorker(self)
        self.__thread = Thread()
        self.__recordingWorker.moveToThread(self.__thread)
        self.__thread.started.connect(self.__recordingWorker.run)

    def __del__(self):
        self.endRecording(emitSignal=False, wait=True)
        if hasattr(super(), '__del__'):
            super().__del__()

    @property
    def record(self):
        """ Whether a recording is currently being recorded. """
        return self.__record

    @property
    def detectorsManager(self):
        return self.__detectorsManager

    def startRecording(self, detectorNames, recMode, savename, saveMode, attrs,
                       saveFormat=SaveFormat.HDF5, singleMultiDetectorFile=False, singleLapseFile=False,
                       recFrames=None, recTime=None, numCamTTL=None, stallTimeout=None):
        """ Starts a recording with the specified detectors, recording mode,
        file name prefix and attributes to save to the recording per detector.
        In SpecFrames mode, recFrames (the number of frames) must be specified,
        and in SpecTime mode, recTime (the recording time in seconds) must be
        specified.
        
        Args:
            stallTimeout: Maximum seconds without frame progress before aborting
                         (None uses DEFAULT_STALL_TIMEOUT). Watchdog only applies
                         to streaming recording, not snap().
        """

        self.__logger.info('Starting recording')
        self.__record = True
        self.__recordingWorker.detectorNames = detectorNames
        self.__recordingWorker.recMode = recMode
        self.__recordingWorker.savename = savename
        self.__recordingWorker.saveMode = saveMode
        self.__recordingWorker.saveFormat = saveFormat
        self.__recordingWorker.attrs = attrs
        self.__recordingWorker.recFrames = recFrames
        self.__recordingWorker.numCamTTL = numCamTTL
        self.__recordingWorker.recTime = recTime
        self.__recordingWorker.singleMultiDetectorFile = singleMultiDetectorFile
        self.__recordingWorker.singleLapseFile = singleLapseFile
        self.__recordingWorker.stallTimeout = stallTimeout if stallTimeout is not None else DEFAULT_STALL_TIMEOUT
        self.__detectorsManager.execOnAll(lambda c: c.flushBuffers(),
                                          condition=lambda c: c.forAcquisition)
        self.__thread.start()

    def endRecording(self, emitSignal=True, wait=True):
        """ Ends the current recording. Unless emitSignal is false, the
        sigRecordingEnded signal will be emitted. Unless wait is False, this
        method will wait until the recording is complete before returning. """

        self.__detectorsManager.execOnAll(lambda c: c.flushBuffers(),
                                          condition=lambda c: c.forAcquisition)

        if self.__record:
            self.__logger.info('Stopping recording')
        self.__record = False
        self.__thread.quit()
        if emitSignal:
            self.sigRecordingEnded.emit()
        if wait:
            self.__thread.wait()

    def snap(self, detectorNames, savename, saveMode, saveFormat, attrs):
        """ Saves an image with the specified detectors to a file
        with the specified name prefix, save mode, file format and attributes
        to save to the capture per detector. """
        acqHandle = self.__detectorsManager.startAcquisition()

        try:
            images = {}

            # Acquire data
            for detectorName in detectorNames:
                images[detectorName] = self.__detectorsManager[detectorName].getLatestFrame(is_save=True)
                image = images[detectorName]

            if saveFormat:
                storer = self.__storerMap[saveFormat]

                if saveMode == SaveMode.Disk or saveMode == SaveMode.DiskAndRAM:
                    # Save images to disk
                    store = storer(savename, self.__detectorsManager)
                    store.snap(images, attrs)

                if saveMode == SaveMode.RAM or saveMode == SaveMode.DiskAndRAM:
                    for channel, image in images.items():
                        name = os.path.basename(f'{savename}_{channel}')
                        self.sigMemorySnapAvailable.emit(name, image, savename, saveMode == SaveMode.DiskAndRAM)

        finally:
            self.__detectorsManager.stopAcquisition(acqHandle)
            if saveMode == SaveMode.Numpy:
                return images

    def snapImagePrev(self, detectorName, savename, saveFormat, image, attrs):
        """Save a previously captured image using the appropriate Storer.
        
        Routes through Storer.snap() for unified snapshot saving logic.
        This ensures consistent file format, metadata structure, and axis ordering
        between snap() and snapImagePrev().
        
        Args:
            detectorName: Name of the detector
            savename: Base filename (without extension)
            saveFormat: SaveFormat enum value
            image: Image array to save (T, Y, X) or (Y, X)
            attrs: Dict mapping detector name to flat metadata dict
        """
        storer = self.__storerMap[saveFormat]
        store = storer(savename, self.__detectorsManager)

        # Wrap single detector in dict for storer interface
        images = {detectorName: image}
        store.snap(images, attrs)

    def getSaveFilePath(self, path, allowOverwriteDisk=False, allowOverwriteMem=False):
        newPath = path
        numExisting = 0

        def existsFunc(pathToCheck):
            if not allowOverwriteDisk and os.path.exists(pathToCheck):
                return True
            if not allowOverwriteMem and pathToCheck in self._memRecordings:
                return True
            return False

        while existsFunc(newPath):
            numExisting += 1
            pathWithoutExt, pathExt = os.path.splitext(path)
            newPath = f'{pathWithoutExt}_{numExisting}{pathExt}'
        return newPath


class WriterThread(threading.Thread):
    """Dedicated writer thread that owns all storer I/O operations.
    
    Runs compression and disk writes off the acquisition thread, fed by a bounded
    queue from the acquisition loop. Batches frames per detector for efficiency.
    """
    def __init__(self, storer, fileDests, detectorNames, shapes, attrs,
                 singleMultiDetectorFile, singleLapseFile, saveMode, filePaths, recordingManager):
        super().__init__(daemon=False, name='RecordingWriterThread')
        self._storer = storer
        self._fileDests = fileDests
        self._detectorNames = detectorNames
        self._shapes = shapes
        self._attrs = attrs
        self._singleMultiDetectorFile = singleMultiDetectorFile
        self._singleLapseFile = singleLapseFile
        self._saveMode = saveMode
        self._filePaths = filePaths
        self._recordingManager = recordingManager
        
        # Bounded queue for backpressure (put() blocks when full)
        self._queue = queue.Queue(maxsize=WRITER_QUEUE_MAXSIZE)
        
        # Per-detector batching buffers
        self._batches = {detectorName: [] for detectorName in detectorNames}
        self._batch_frame_counts = {detectorName: 0 for detectorName in detectorNames}
        
        # Track total frames written per detector
        self._currentFrames = {detectorName: 0 for detectorName in detectorNames}
        
        # Handshake for openStream completion (success or exception)
        self._opened_event = threading.Event()
        self._open_exception = None
        # Exception raised in the write loop (surfaced to the producer so it
        # never blocks forever on a dead writer; see enqueue_frames).
        self._write_exception = None

    def run(self):
        """Writer thread main loop."""
        try:
            # Open streaming session (h5py/zarr file handles must be touched by one thread only)
            self._storer.openStream(
                fileDests=self._fileDests,
                detectorNames=self._detectorNames,
                shapes=self._shapes,
                attrs=self._attrs,
                singleMultiDetectorFile=self._singleMultiDetectorFile,
                singleLapseFile=self._singleLapseFile,
                saveMode=self._saveMode
            )
        except Exception as e:
            # Store exception to re-raise on acquisition thread
            self._open_exception = e
            self._opened_event.set()
            return
        
        # Signal that openStream succeeded
        self._opened_event.set()
        
        # Main write loop: process frames until sentinel
        try:
            while True:
                item = self._queue.get()
                
                if item is None:
                    # Sentinel: flush remaining batches and finalize
                    self._flush_all_batches()
                    self._storer.finalizeStream(
                        self._currentFrames, self._filePaths, self._recordingManager, self._saveMode
                    )
                    break
                
                # Real frame item: (detectorName, frames)
                detectorName, frames = item
                
                # Append to batch
                self._batches[detectorName].append(frames)
                self._batch_frame_counts[detectorName] += len(frames)
                
                # Flush batch if threshold reached
                if self._batch_frame_counts[detectorName] >= WRITE_BATCH_FRAMES:
                    self._flush_batch(detectorName)
        
        except Exception as e:
            # Record the failure and exit. Do NOT re-raise into the thread void:
            # the producer (enqueue_frames) and finish() detect the dead writer
            # via is_alive() and surface this exception, so the acquisition
            # thread never blocks forever on a full queue.
            logger.exception(f"WriterThread failed during write loop: {e}")
            self._write_exception = e
    
    def _flush_batch(self, detectorName):
        """Flush accumulated frames for a detector to disk."""
        if not self._batches[detectorName]:
            return
        
        # Concatenate all accumulated frames
        batch = np.concatenate(self._batches[detectorName], axis=0)
        
        # Write batch (compression + I/O happens here, off acquisition thread)
        self._storer.writeFrames(detectorName, batch)
        
        # Update counters
        self._currentFrames[detectorName] += len(batch)
        
        # Clear batch
        self._batches[detectorName] = []
        self._batch_frame_counts[detectorName] = 0
    
    def _flush_all_batches(self):
        """Flush all remaining per-detector batches."""
        for detectorName in self._detectorNames:
            self._flush_batch(detectorName)
    
    def wait_for_open(self):
        """Wait for openStream handshake and re-raise any exception.
        
        Returns:
            None if openStream succeeded
            
        Raises:
            Any exception that occurred during openStream
        """
        self._opened_event.wait()
        if self._open_exception is not None:
            raise self._open_exception
    
    def enqueue_frames(self, detectorName, frames):
        """Enqueue frames for the writer thread.

        Blocks while the queue is full (intended backpressure), but NEVER
        forever: if the writer thread has died, raises so the failure surfaces
        on the acquisition thread instead of deadlocking the producer.
        """
        while True:
            try:
                self._queue.put((detectorName, frames), timeout=0.1)
                return
            except queue.Full:
                if not self.is_alive():
                    raise RuntimeError(
                        'RecordingWriterThread died before frames could be '
                        'enqueued; recording aborted'
                    ) from self._write_exception
                # Writer still alive and draining - keep applying backpressure.
                continue
    
    def finish(self):
        """Signal end of recording and wait for writer thread to complete.
        
        Uses a timeout to avoid deadlock if queue is full. The writer thread
        will drain the queue, making space for the sentinel.
        """
        # Enqueue sentinel (None means "no more frames")
        # Use timeout to avoid deadlock - writer thread drains queue
        while True:
            try:
                self._queue.put(None, timeout=0.1)
                break
            except queue.Full:
                # Queue still full, writer is draining - retry
                if not self.is_alive():
                    # Writer thread died unexpectedly, don't wait forever
                    logger.error("WriterThread died before sentinel could be enqueued")
                    return
                continue
        
        # Wait for writer thread to finish (finalizeStream must complete)
        self.join(timeout=30.0)
        if self.is_alive():
            logger.error("WriterThread did not finish within timeout")


class RecordingWorker(Worker):
    def __init__(self, recordingManager):
        super().__init__()
        self.__logger = initLogger(self)
        self.__recordingManager = recordingManager
        self.__logger = initLogger(self)

    def run(self):
        acqHandle = self.__recordingManager.detectorsManager.startAcquisition()
        try:
            self._record()

        finally:
            self.__recordingManager.detectorsManager.stopAcquisition(acqHandle)
    
    def _getFileDests(self):
        """Prepare file destinations and paths for streaming."""
        singleMultiDetectorFile = self.singleMultiDetectorFile
        singleLapseFile = self.recMode == RecMode.ScanLapse and self.singleLapseFile
        
        fileDests = {}
        filePaths = {}
        
        if self.saveFormat == SaveFormat.TIFF:
            extension = 'tiff'
        elif self.saveFormat == SaveFormat.HDF5:
            extension = 'hdf5'
        elif self.saveFormat == SaveFormat.ZARR:
            extension = 'zarr'
        else:
            raise ValueError(f'Unsupported save format: {self.saveFormat}')
        
        # Determine file paths
        for detectorName in self.detectorNames:
            if singleMultiDetectorFile and self.saveFormat != SaveFormat.TIFF:
                baseFilePath = f'{self.savename}.{extension}'
            else:
                baseFilePath = f'{self.savename}_{detectorName}.{extension}'
            
            filePaths[detectorName] = self.__recordingManager.getSaveFilePath(
                baseFilePath,
                allowOverwriteDisk=singleLapseFile and self.saveMode != SaveMode.RAM,
                allowOverwriteMem=singleLapseFile and self.saveMode == SaveMode.RAM
            )
        
        # Determine file destinations (path or BytesIO)
        for detectorName in self.detectorNames:
            if self.saveMode == SaveMode.RAM:
                memRecordings = self.__recordingManager._memRecordings
                if (filePaths[detectorName] not in memRecordings or
                        memRecordings[filePaths[detectorName]].closed):
                    memRecordings[filePaths[detectorName]] = BytesIO()
                fileDests[detectorName] = memRecordings[filePaths[detectorName]]
            else:
                fileDests[detectorName] = filePaths[detectorName]
        
        return fileDests, filePaths

    def _record(self):
        """Unified streaming recording loop delegating all I/O to Storer.
        
        Collapses recMode branches into a single loop with stop-condition predicates.
        """
        # Validate inputs
        if len(self.detectorNames) < 1:
            raise ValueError('No detectors to record specified')
        
        # Prepare file destinations
        fileDests, filePaths = self._getFileDests()
        
        # Get detector shapes
        shapes = {detectorName: self.__recordingManager.detectorsManager[detectorName].shape
                  for detectorName in self.detectorNames}
        
        # Initialize storer for streaming
        storerClass = self.__recordingManager._RecordingManager__storerMap[self.saveFormat]
        storer = storerClass(self.savename, self.__recordingManager.detectorsManager)
        
        # Frame counters and stall watchdog timestamps
        currentFrame = {detectorName: 0 for detectorName in self.detectorNames}
        lastFrameTime = {detectorName: time.time() for detectorName in self.detectorNames}
        
        # Prepare shapes for storer
        for detectorName in shapes:
            shape = shapes[detectorName]
            if len(shape) > 2:
                shapes[detectorName] = shape[-2:]
        
        # Start writer thread and wait for openStream handshake
        writerThread = WriterThread(
            storer=storer,
            fileDests=fileDests,
            detectorNames=self.detectorNames,
            shapes=shapes,
            attrs=self.attrs,
            singleMultiDetectorFile=self.singleMultiDetectorFile,
            singleLapseFile=self.recMode == RecMode.ScanLapse and self.singleLapseFile,
            saveMode=self.saveMode,
            filePaths=filePaths,
            recordingManager=self.__recordingManager
        )
        writerThread.start()
        
        # Wait for openStream to complete (blocks until success or raises on error)
        writerThread.wait_for_open()
        
        # Determine stop condition based on recMode
        if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
            recFrames = self.recFrames
            if recFrames is None:
                raise ValueError('recFrames must be specified in SpecFrames, ScanOnce or ScanLapse mode')
            
            # Calculate total number of frames for each detector (recFrames * number of TTL)
            numCamTTL = self.numCamTTL if self.numCamTTL is not None else {}
            nFramesPerDetector = {
                detectorName: recFrames * numCamTTL.get(detectorName, 1)
                for detectorName in self.detectorNames
            }
            
            def should_stop():
                return not any(currentFrame[det] < nFramesPerDetector[det]
                               for det in self.detectorNames)
        
        elif self.recMode == RecMode.SpecTime:
            recTime = self.recTime
            if recTime is None:
                raise ValueError('recTime must be specified in SpecTime mode')
            
            startTime = time.time()
            
            def should_stop():
                return (time.time() - startTime) >= recTime
        
        elif self.recMode == RecMode.UntilStop:
            def should_stop():
                return False  # Only external record flag stops this mode
        
        else:
            raise ValueError('Unsupported recording mode specified')
        
        # Unified acquisition loop. Start each detector's chunk-consumer
        # queue fresh so no stale frames from a previous recording leak in
        # (buffers were flushed in startRecording).
        for detectorName in self.detectorNames:
            self.__recordingManager.detectorsManager[detectorName].releaseChunkConsumer(
                _RECORDING_CHUNK_CONSUMER
            )
        self.__recordingManager.sigRecordingStarted.emit()
        shouldStopNext = False
        try:
            while self.__recordingManager.record and not shouldStopNext:
                for detectorName in self.detectorNames:
                    # Skip detector if it has reached its frame target
                    if (self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse] and
                            currentFrame[detectorName] >= nFramesPerDetector[detectorName]):
                        continue
                    
                    # Get new frames
                    newFrames = self._getNewFrames(detectorName)
                    n = len(newFrames)
                    if n > 0:
                        # Clip frames if needed for SpecFrames mode
                        if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                            remaining = nFramesPerDetector[detectorName] - currentFrame[detectorName]
                            if n > remaining:
                                newFrames = newFrames[:remaining]
                                n = remaining
                        
                        # Enqueue frames for writer thread (blocks if queue full, providing backpressure)
                        writerThread.enqueue_frames(detectorName, newFrames)
                        currentFrame[detectorName] += n
                        lastFrameTime[detectorName] = time.time()  # Update watchdog timestamp
                
                # Emit progress signals based on recMode
                if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                    # Report the lowest frame number (for multi-detector)
                    self.__recordingManager.sigRecordingFrameNumUpdated.emit(
                        min(list(currentFrame.values()))
                    )
                elif self.recMode == RecMode.SpecTime:
                    currentRecTime = time.time() - startTime
                    self.__recordingManager.sigRecordingTimeUpdated.emit(
                        np.around(currentRecTime, decimals=2)
                    )
                
                # Check for stalled detectors (only for modes with frame targets)
                if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                    now = time.time()
                    for detectorName in self.detectorNames:
                        # Skip detectors that have already reached their target
                        if currentFrame[detectorName] >= nFramesPerDetector[detectorName]:
                            continue
                        
                        elapsed = now - lastFrameTime[detectorName]
                        if elapsed > self.stallTimeout:
                            # Stall detected - log diagnostics and abort
                            self.__logger.error(
                                f"Detector '{detectorName}' stalled: no frames received for {elapsed:.1f}s "
                                f"(timeout: {self.stallTimeout}s). Current: {currentFrame[detectorName]} frames, "
                                f"expected: {nFramesPerDetector[detectorName]} frames. "
                                f"Check camera triggering and numCamTTL configuration."
                            )
                            self.__recordingManager.sigRecordingStalled.emit(detectorName)
                            shouldStopNext = True
                            break
                
                # Check stop condition
                if should_stop():
                    shouldStopNext = True
                
                time.sleep(FRAME_POLL_INTERVAL)  # Yield to event loop to prevent UI freezing
            
            # Reset progress signals
            if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                self.__recordingManager.sigRecordingFrameNumUpdated.emit(0)
            elif self.recMode == RecMode.SpecTime:
                self.__recordingManager.sigRecordingTimeUpdated.emit(0)
        
        finally:
            # Stop retaining frames for this consumer now that the recording
            # is over (other readChunk consumers may keep draining).
            for detectorName in self.detectorNames:
                self.__recordingManager.detectorsManager[detectorName].releaseChunkConsumer(
                    _RECORDING_CHUNK_CONSUMER
                )
            
            # Signal writer thread to finish and wait for it (ensures finalizeStream completes)
            # Note: writerThread.finish() enqueues None sentinel and joins, so the queue
            # must not be full. Since we've stopped reading frames, backpressure is no longer
            # an issue - the sentinel will be deliverable once the writer drains the queue.
            writerThread.finish()
            
            # End recording
            emitSignal = True
            if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                emitSignal = False
            self.__recordingManager.endRecording(emitSignal=emitSignal, wait=False)

    def _getNewFrames(self, detectorName):
        # readChunk (not getChunk): the destructive getChunk would steal
        # frames from concurrent consumers such as BeadRec — see
        # DetectorManager.readChunk.
        newFrames = self.__recordingManager.detectorsManager[detectorName].readChunk(
            _RECORDING_CHUNK_CONSUMER
        )
        # Preserve native dtype: no silent promotion to float64.
        # readChunk returns List[ndarray] already in detector's native dtype.
        if len(newFrames) == 0:
            # Empty chunk: return a correctly typed empty array (not float64).
            detector_dtype = self.__recordingManager.detectorsManager[detectorName].dtype
            return np.empty((0,), dtype=detector_dtype)
        # Non-empty: stack preserves native dtype, no copy needed.
        return np.stack(newFrames)


class RecMode(enum.Enum):
    SpecFrames = 1
    SpecTime = 2
    ScanOnce = 3
    ScanLapse = 4
    UntilStop = 5


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
