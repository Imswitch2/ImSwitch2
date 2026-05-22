import enum
import json
import os
import time
from io import BytesIO
from typing import Dict, List, Optional, Type, Union

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
    """A storer that stores the images in a zarr file store.
    
    Note: Streaming implementation preserves legacy behavior with hardcoded 'i2' dtype.
    This is isolated for future migration to dtype-aware zarr.
    """
    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, str] = None):
        with AsTemporaryFile(f'{self.filepath}.zarr') as path:
            store = zarr.storage.DirectoryStore(path)
            root = zarr.group(store=store)

            for channel, image in images.items():
                shape = self.detectorManager[channel].shape
                d = root.create_dataset(channel, data=image, shape=tuple(reversed(shape)),
                                        chunks=(512, 512), dtype='i2') #TODO: why not dynamic chunking?
                d.attrs["ImSwitchData"] = attrs[channel]
            logger.info(f"Saved image to zarr file {path}")
    
    def openStream(self, fileDests, detectorNames, shapes, attrs, *,
                   singleMultiDetectorFile, singleLapseFile, saveMode):
        """Initialize ZARR streaming session (legacy behavior preserved)."""
        self._store = zarr.storage.DirectoryStore(list(fileDests.values())[0])
        self._root = zarr.group(store=self._store, overwrite=True)
        self._datasets = {}
        self._attrs = attrs
        self._shapes = shapes
        self._currentFrames = {}
        
        # Create datasets up-front (legacy behavior)
        for detectorName in detectorNames:
            shape = shapes[detectorName]
            if len(shape) > 2:
                shape = shape[-2:]
            
            self._datasets[detectorName] = self._root.create_dataset(
                detectorName, shape=(1, *reversed(shape)),
                dtype='i2', chunks=(1, 512, 512)
            )
            self._datasets[detectorName].attrs['ImSwitchData'] = attrs[detectorName]
            self._datasets[detectorName].attrs['detector_name'] = detectorName
            self._datasets[detectorName].attrs['element_size_um'] = \
                self.detectorManager[detectorName].pixelSizeUm
            self._datasets[detectorName].attrs['writing'] = True
            self._currentFrames[detectorName] = 0
    
    def writeFrames(self, detectorName, frames):
        """Write frames to ZARR dataset (legacy append behavior)."""
        if len(frames) == 0:
            return
        
        dataset = self._datasets[detectorName]
        it = self._currentFrames[detectorName]
        
        # Legacy behavior: first frame uses index 0, subsequent frames append
        if it == 0:
            dataset[0, :, :] = frames[0, :, :]
            if len(frames) > 1:
                dataset.append(frames[1:, :, :])
        else:
            dataset.append(frames)
        
        self._currentFrames[detectorName] += len(frames)
    
    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        """Close ZARR store."""
        for detectorName, dataset in self._datasets.items():
            dataset.attrs['writing'] = False
        self._store.close()


class HDF5Storer(Storer):
    """Storer for HDF5 format with structured layout.
    
    Snapshot layout:
        <file>.h5
          @imswitch_version (future)
          @timestamp
          @rec_mode = 'snap'
          <detectorName>/
            data              # (T, Y, X) or (Y, X), dtype from frame, lzf compressed
              @detector_name
              @element_size_um
            metadata/
              <category>/     # e.g., 'detector', 'lasers', 'scan'
                @key = value  # attrs within category
    
    Compression: lzf (fast, lossless) + shuffle filter by default.
    """
    
    def __init__(self, filepath, detectorManager, compression='lzf'):
        """Initialize HDF5 storer.
        
        Args:
            filepath: Base path for output file (without extension)
            detectorManager: DetectorsManager instance
            compression: Compression filter ('lzf', 'gzip', None, or h5py compression spec)
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
            dataset = det_group.create_dataset(
                'data',
                shape=(0, *shape),
                maxshape=maxshape,
                dtype=dtype,
                compression=self.compression,
                shuffle=True if self.compression else False,
                chunks=(1, *shape)  # Per-frame chunks
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
                dataset = self._createDetectorGroup(
                    file, detectorName, frames.dtype, self._attrs[detectorName],
                    maxshape=(None, *spatialShape),  # (None, Y, X)
                    groupPath=groupPath
                )
            finally:
                self.compression = original_compression
            
            dataset.attrs['writing'] = True
            self._datasets[detectorName] = dataset

        # Append frames to structured dataset
        dataset = self._datasets[detectorName]
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
        if saveFormat == SaveFormat.ZARR:
            # ZARR kept as-is (deferred - will be unified in upstream merge)
            fileExtension = str(saveFormat.name).lower()
            path = self.getSaveFilePath(f'{savename}.{fileExtension}')
            store = zarr.storage.DirectoryStore(path)
            root = zarr.group(store=store)
            shape = self.__detectorsManager[detectorName].shape
            d = root.create_dataset(detectorName, data=image, shape=tuple(reversed(shape)), chunks=(512, 512),
                                    dtype='i2')
            d.attrs["ImSwitchData"] = attrs[detectorName]
            store.close()
        else:
            # Route through Storer for HDF5 and TIFF
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
        
        # Open streaming session
        storer.openStream(
            fileDests=fileDests,
            detectorNames=self.detectorNames,
            shapes=shapes,
            attrs=self.attrs,
            singleMultiDetectorFile=self.singleMultiDetectorFile,
            singleLapseFile=self.recMode == RecMode.ScanLapse and self.singleLapseFile,
            saveMode=self.saveMode
        )
        
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
        
        # Unified acquisition loop
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
                        
                        # Delegate write to storer
                        storer.writeFrames(detectorName, newFrames)
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
            # Finalize streaming (close files, emit signals)
            storer.finalizeStream(currentFrame, filePaths, self.__recordingManager, self.saveMode)
            
            # End recording
            emitSignal = True
            if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                emitSignal = False
            self.__recordingManager.endRecording(emitSignal=emitSignal, wait=False)

    def _getNewFrames(self, detectorName):
        newFrames = self.__recordingManager.detectorsManager[detectorName].getChunk()
        newFrames = np.array(newFrames)
        return newFrames


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
