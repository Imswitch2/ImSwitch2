import enum
import json
import os
import time
from io import BytesIO
from typing import Dict, Optional, Type

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

    def stream(self, data = None, **kwargs):
        """Store data in streaming fashion (used by RecordingWorker._record)."""
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
    """ A storer that stores the images in a zarr file store """
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
                    
                    # Create detector group
                    det_group = file.create_group(channel)
                    
                    # Ensure 3D: (T, Y, X)
                    if image.ndim == 2:
                        image = image[np.newaxis, ...]  # Add time dimension
                    
                    # Create dataset with compression (per-frame chunks for compatibility with streaming)
                    chunks = (1, *image.shape[-2:]) if image.ndim >= 3 else True
                    dataset = det_group.create_dataset(
                        'data',
                        data=image,
                        dtype=image.dtype,
                        compression=self.compression,
                        shuffle=True if self.compression else False,
                        chunks=chunks
                    )
                    
                    # Dataset-level metadata
                    dataset.attrs['detector_name'] = channel
                    dataset.attrs['element_size_um'] = self.detectorManager[channel].pixelSizeUm
                    
                    # Group attrs by category and create metadata subgroups
                    channel_attrs = attrs.get(channel, {})
                    grouped = self._group_metadata_by_category(channel_attrs)
                    
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
                
                logger.info(f"Saved snapshot to {path} with structured HDF5 layout")
        

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
                       recFrames=None, recTime=None, numCamTTL = None):
        """ Starts a recording with the specified detectors, recording mode,
        file name prefix and attributes to save to the recording per detector.
        In SpecFrames mode, recFrames (the number of frames) must be specified,
        and in SpecTime mode, recTime (the recording time in seconds) must be
        specified. """

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

    def _record(self):
        if self.saveFormat == SaveFormat.HDF5 or self.saveFormat == SaveFormat.ZARR:
            files, fileDests, filePaths = self._getFiles()

        shapes = {detectorName: self.__recordingManager.detectorsManager[detectorName].shape
                  for detectorName in self.detectorNames}

        currentFrame = {}
        datasets = {}
        filenames = {}
        datasetNames = {}  # Store dataset names for lazy HDF5 creation
        for detectorName in self.detectorNames:
            currentFrame[detectorName] = 0

            datasetName = detectorName
            if self.recMode == RecMode.ScanLapse and self.singleLapseFile:
                # Add scan number to dataset name
                scanNum = 0
                datasetNameWithScan = f'{datasetName}_scan{scanNum}'
                while datasetNameWithScan in files[detectorName]:
                    scanNum += 1
                    datasetNameWithScan = f'{datasetName}_scan{scanNum}'
                datasetName = datasetNameWithScan

            shape = shapes[detectorName]
            if len(shape) > 2:
                shape = shape[-2:]

            if self.saveFormat == SaveFormat.HDF5:
                # HDF5 dataset creation is now LAZY - deferred until first frame write
                # to derive dtype from actual frame data instead of hardcoded 'i2'.
                # Store dataset name and shape for later creation.
                datasetNames[detectorName] = datasetName

            elif self.saveFormat == SaveFormat.TIFF:
                fileExtension = str(self.saveFormat.name).lower()
                filenames[detectorName] = self.__recordingManager.getSaveFilePath(
                    f'{self.savename}_{detectorName}.{fileExtension}', False, False)

            elif self.saveFormat == SaveFormat.ZARR:
                datasets[detectorName] = files[detectorName].create_dataset(datasetName, shape=(1, *reversed(shape)),
                                                                            dtype='i2', chunks=(1, 512, 512)
                                                                            )

                datasets[detectorName].attrs['ImSwitchData'] = self.attrs[detectorName]
                datasets[detectorName].attrs['detector_name'] = detectorName
                # For ImageJ compatibility
                datasets[detectorName].attrs['element_size_um'] \
                    = self.__recordingManager.detectorsManager[detectorName].pixelSizeUm
                datasets[detectorName].attrs['writing'] = True

        def _ensureHDF5Dataset(detectorName, newFrames):
            """Lazily create HDF5 dataset on first frame write with dtype from actual frames."""
            if detectorName not in datasets:
                shape = shapes[detectorName]
                if len(shape) > 2:
                    shape = shape[-2:]
                datasetName = datasetNames[detectorName]
                
                # Create dataset with dtype from actual frame data
                datasets[detectorName] = files[detectorName].create_dataset(
                    datasetName, (0, *reversed(shape)),
                    maxshape=(None, *reversed(shape)),
                    dtype=newFrames.dtype
                )

                # Set attributes
                for key, value in self.attrs[detectorName].items():
                    try:
                        if isinstance(value, dict):
                            datasets[detectorName].attrs[key] = json.dumps(value)
                        else:
                            datasets[detectorName].attrs[key] = value
                    except Exception as e:
                        self.__logger.error(f"Error saving {key} {value} to Hdf5: {e}")
                datasets[detectorName].attrs['detector_name'] = detectorName
                datasets[detectorName].attrs['element_size_um'] \
                    = self.__recordingManager.detectorsManager[detectorName].pixelSizeUm
                datasets[detectorName].attrs['writing'] = True

        self.__recordingManager.sigRecordingStarted.emit()
        try:
            if len(self.detectorNames) < 1:
                raise ValueError('No detectors to record specified')
            
            if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                recFrames = self.recFrames
                if recFrames is None:
                    raise ValueError('recFrames must be specified in SpecFrames, ScanOnce or'
                                     ' ScanLapse mode')

                # calculate total number offrames for each detector (recFrames * number of TTL)
                numCamTTL = self.numCamTTL if self.numCamTTL is not None else {}
                nFramesPerDetector = {}
                for detectorName in self.detectorNames:
                    nFramesPerDetector[detectorName] = recFrames * numCamTTL.get(detectorName, 1) 
                maxFrames = max(nFramesPerDetector.values())
                # print(f"Aiming to capture {maxFrames} Frames", nFramesPerDetector, recFrames)

                while (self.__recordingManager.record and
                       any([currentFrame[detectorName] < maxFrames
                            for detectorName in self.detectorNames])):
                    for detectorName in self.detectorNames:
                        nFrames = nFramesPerDetector[detectorName]
                        if currentFrame[detectorName] >= nFrames:
                            continue  # Reached requested number of frames with this detector, skip

                        newFrames = self._getNewFrames(detectorName)
                        n = len(newFrames)
                        if n > 0:
                            it = currentFrame[detectorName]
                            if self.saveFormat == SaveFormat.TIFF:
                                try:
                                    filePath = filenames[detectorName]
                                    tiff.imwrite(filePath, newFrames, append=True)
                                except ValueError:
                                    self.__logger.error("TIFF File exceeded 4GB.")
                                    if self.saveFormat == SaveFormat.TIFF:
                                        filePath = self.__recordingManager.getSaveFilePath(
                                            f'{self.savename}_{detectorName}.{fileExtension}', False, False)
                                        continue
                            elif self.saveFormat == SaveFormat.HDF5:
                                _ensureHDF5Dataset(detectorName, newFrames)
                                dataset = datasets[detectorName]
                                if (it + n) <= nFrames:
                                    dataset.resize(n + it, axis=0)
                                    dataset[it:it + n, :, :] = newFrames
                                    currentFrame[detectorName] += n
                                else:
                                    dataset.resize(nFrames, axis=0)
                                    dataset[it:nFrames, :, :] = newFrames[0:nFrames - it]
                                    currentFrame[detectorName] = nFrames
                            elif self.saveFormat == SaveFormat.ZARR:
                                dataset = datasets[detectorName]
                                if it == 0:
                                    dataset[0, :, :] = newFrames[0, :, :]
                                    if n > 0:
                                        dataset.append(newFrames[1:n, :, :])
                                else:
                                    dataset.append(newFrames)
                                currentFrame[detectorName] += n

                            # Things get a bit weird if we have multiple detectors when we report
                            # the current frame number, since the detectors may not be synchronized.
                            # For now, we will report the lowest number.
                            self.__recordingManager.sigRecordingFrameNumUpdated.emit(
                                min(list(currentFrame.values()))
                            )
                    time.sleep(0.0001)  # Prevents freezing for some reason

                self.__recordingManager.sigRecordingFrameNumUpdated.emit(0)
            elif self.recMode == RecMode.SpecTime:
                recTime = self.recTime
                if recTime is None:
                    raise ValueError('recTime must be specified in SpecTime mode')

                start = time.time()
                currentRecTime = 0
                shouldStop = False
                while True:
                    for detectorName in self.detectorNames:
                        newFrames = self._getNewFrames(detectorName)
                        n = len(newFrames)
                        if n > 0:
                            if self.saveFormat == SaveFormat.TIFF:
                                try:
                                    filePath = filenames[detectorName]
                                    tiff.imwrite(filePath, newFrames, append=True)
                                except ValueError:
                                    self.__logger.error("TIFF File exceeded 4GB.")
                                    if self.saveFormat == SaveFormat.TIFF:
                                        filePath = self.__recordingManager.getSaveFilePath(
                                            f'{self.savename}_{detectorName}.{fileExtension}', False, False)
                                        continue
                            elif self.saveFormat == SaveFormat.HDF5:
                                _ensureHDF5Dataset(detectorName, newFrames)
                                it = currentFrame[detectorName]
                                dataset = datasets[detectorName]
                                dataset.resize(n + it, axis=0)
                                dataset[it:it + n, :, :] = newFrames
                            elif self.saveFormat == SaveFormat.ZARR:
                                it = currentFrame[detectorName]
                                dataset = datasets[detectorName]
                                dataset.resize(n + it, axis=0)
                                dataset[it:it + n, :, :] = newFrames
                            currentFrame[detectorName] += n
                            self.__recordingManager.sigRecordingTimeUpdated.emit(
                                np.around(currentRecTime, decimals=2)
                            )
                            currentRecTime = time.time() - start

                    if shouldStop:
                        break  # Enter loop one final time, then stop

                    if not self.__recordingManager.record or currentRecTime >= recTime:
                        shouldStop = True

                    time.sleep(0.0001)  # Prevents freezing for some reason

                self.__recordingManager.sigRecordingTimeUpdated.emit(0)
            elif self.recMode == RecMode.UntilStop:
                shouldStop = False
                while True:
                    for detectorName in self.detectorNames:
                        newFrames = self._getNewFrames(detectorName)
                        n = len(newFrames)
                        if n > 0:
                            if self.saveFormat == SaveFormat.TIFF:
                                try:
                                    filePath = filenames[detectorName]
                                    tiff.imwrite(filePath, newFrames, append=True)
                                except ValueError:
                                    self.__logger.error("TIFF File exceeded 4GB.")
                                    if self.saveFormat == SaveFormat.TIFF:
                                        filePath = self.__recordingManager.getSaveFilePath(
                                            f'{self.savename}_{detectorName}.{fileExtension}', False, False)
                                        continue

                            elif self.saveFormat == SaveFormat.HDF5:
                                _ensureHDF5Dataset(detectorName, newFrames)
                                it = currentFrame[detectorName]
                                dataset = datasets[detectorName]
                                dataset.resize(n + it, axis=0)
                                dataset[it:it + n, :, :] = newFrames

                            elif self.saveFormat == SaveFormat.ZARR:
                                it = currentFrame[detectorName]
                                dataset = datasets[detectorName]
                                if it == 0:
                                    dataset[0, :, :] = newFrames[0, :, :]
                                    if n > 0:
                                        dataset.append(newFrames[1:n, :, :])
                                else:
                                    dataset.append(newFrames)

                            currentFrame[detectorName] += n

                    if shouldStop:
                        break

                    if not self.__recordingManager.record:
                        shouldStop = True  # Enter loop one final time, then stop

                    time.sleep(0.0001)  # Prevents freezing for some reason
            else:
                raise ValueError('Unsupported recording mode specified')
        finally:

            if self.saveFormat == SaveFormat.HDF5 or self.saveFormat == SaveFormat.ZARR:
                for detectorName, file in files.items():
                    # Remove default frame if no frames have been captured
                    # (HDF5 lazy creation: dataset may not exist if no frames captured)
                    dataset = datasets.get(detectorName)
                    if dataset is not None and currentFrame[detectorName] < 1:
                        if self.saveFormat == SaveFormat.HDF5:
                            dataset.resize(0, axis=0)

                    # Handle memory recordings
                    if self.saveMode == SaveMode.RAM or self.saveMode == SaveMode.DiskAndRAM:
                        filePath = filePaths[detectorName]
                        name = os.path.basename(filePath)
                        if self.saveMode == SaveMode.RAM:
                            file.close()
                            self.__recordingManager.sigMemoryRecordingAvailable.emit(
                                name, fileDests[detectorName], filePath, False
                            )
                        else:
                            file.flush()
                            self.__recordingManager.sigMemoryRecordingAvailable.emit(
                                name, file, filePath, True
                            )
                    else:
                        if dataset is not None:
                            dataset.attrs['writing'] = False
                        if self.saveFormat == SaveFormat.HDF5:
                            file.close()
                        else:
                            self.store.close()
            emitSignal = True
            if self.recMode in [RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse]:
                emitSignal = False
            self.__recordingManager.endRecording(emitSignal=emitSignal, wait=False)

    def _getFiles(self):
        singleMultiDetectorFile = self.singleMultiDetectorFile
        singleLapseFile = self.recMode == RecMode.ScanLapse and self.singleLapseFile

        files = {}
        fileDests = {}
        filePaths = {}
        extension = 'hdf5' if self.saveFormat == SaveFormat.HDF5 else 'zarr'

        for detectorName in self.detectorNames:
            if singleMultiDetectorFile:
                baseFilePath = f'{self.savename}.{extension}'
            else:
                baseFilePath = f'{self.savename}_{detectorName}.{extension}'

            filePaths[detectorName] = self.__recordingManager.getSaveFilePath(
                baseFilePath,
                allowOverwriteDisk=singleLapseFile and self.saveMode != SaveMode.RAM,
                allowOverwriteMem=singleLapseFile and self.saveMode == SaveMode.RAM
            )

        for detectorName in self.detectorNames:
            if self.saveMode == SaveMode.RAM:
                memRecordings = self.__recordingManager._memRecordings
                if (filePaths[detectorName] not in memRecordings or
                        memRecordings[filePaths[detectorName]].closed):
                    memRecordings[filePaths[detectorName]] = BytesIO()
                fileDests[detectorName] = memRecordings[filePaths[detectorName]]
            else:
                fileDests[detectorName] = filePaths[detectorName]

            if singleMultiDetectorFile and len(files) > 0:
                files[detectorName] = list(files.values())[0]
            else:
                if self.saveFormat == SaveFormat.HDF5:
                    files[detectorName] = h5py.File(fileDests[detectorName],
                                                    'a' if singleLapseFile else 'w-')
                elif self.saveFormat == SaveFormat.ZARR:
                    self.store = zarr.storage.DirectoryStore(fileDests[detectorName])
                    files[detectorName] = zarr.group(store=self.store, overwrite=True)

        return files, fileDests, filePaths

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
