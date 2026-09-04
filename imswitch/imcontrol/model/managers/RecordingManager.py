import enum
import json
import math
import os
import shutil
import time
import threading
import queue
from datetime import datetime, timezone
from io import BytesIO
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import h5py
import sip
import zarr
import numpy as np
import tifffile as tiff
from qtpy import QtCore

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Worker
from imswitch.imcommon.model import (
    ACQUISITION_LAYOUT_SCHEMA,
    AcquisitionLayout,
    AcquisitionLayoutError,
    JSON_ATTR_PREFIX,
    LayoutIssue,
    decode_acquisition_layout,
    encode_acquisition_layout,
    initLogger,
    validate_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    scan_position_count,
)
import abc
import logging

import imswitch
from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    ChunkKind, RawFrameUnavailableError,
)
from imswitch.imcontrol.model.managers import recording_metadata as _ome
from imswitch.imcommon.model.zarr_compat import (
    install_zarr_create_array_compat,
    write_zarr_json_sidecar,
)

logger = logging.getLogger(__name__)
install_zarr_create_array_compat()

# Recording loop constants
FRAME_POLL_INTERVAL = 0.0001  # seconds; prevents UI freezing during acquisition
DEFAULT_STALL_TIMEOUT = 10.0  # seconds; watchdog triggers if no frames arrive within this period
_RECORDING_CHUNK_CONSUMER = 'RecordingManager'  # readChunk consumer key (see DetectorManager.readChunk)
RECORDING_ARM_TIMEOUT = 5.0  # seconds; max wait for detectors to arm before starting a scan
RECORDING_THREAD_STOP_TIMEOUT_MS = 30000

# Off-thread writer constants
WRITER_QUEUE_MAXSIZE = 64  # Bounded queue size for backpressure (blocks acquisition when full)
WRITE_BATCH_FRAMES = 32  # Number of frames to accumulate per detector before flushing to disk
WRITER_OPEN_TIMEOUT_S = 30.0
#: How long the acquisition loop may sit blocked on a full writer queue before
#: it says so. Well under any detector's chunk-queue budget, so the stall is
#: reported before the overflow it causes rather than after it.
PRODUCER_STALL_WARN_S = 1.0
# SWMR requires HDF5 1.10+ object formats, but libver='latest' maps to
# ('v200', 'v200') with HDF5 2.x. Pin the writer to the oldest SWMR-capable
# format so Fiji/HDFView builds that do not understand HDF5 2.0 can still open
# completed recordings.
HDF5_STREAM_LIBVER = ('v110', 'v110')


@dataclass(frozen=True)
class StreamPayloadInfo:
    """Exact container location and shape of one finalising stream.

    This is deliberately reported while the backend is still open.  HDF5 and
    Zarr handles no longer expose their datasets after finalisation, while a
    manifest needs the exact detector group and the shape actually committed
    to disk rather than a prediction made from the acquisition settings.
    """

    group: Optional[str]
    stored_shape: Tuple[int, ...]
    #: Whether the container stores the writer's outer frame dimension.  A
    #: singleton outer dimension is storage detail and is removed from the
    #: logical payload descriptor; a multi-frame dimension remains logical.
    frame_axis_stored: bool


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
        # Per-detector OmeImageMeta, set by RecordingManager before snap()/openStream().
        # None for legacy/fallback paths; storers that understand it (TiffStorer)
        # use it to write standard OME metadata.
        self.omeMeta: Dict[str, Any] = {}

    @staticmethod
    def _layout_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in (attrs or {}).items()
            if isinstance(key, str) and key.startswith("AcquisitionLayout:")
        }

    @staticmethod
    def _ome_annotation_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
        recording_keys = {
            "recording:completion_outcome",
            "recording:planned_frames",
            "recording:actual_frames",
            "recording:planned_partitions",
            "recording:actual_partitions",
        }
        return {
            key: value
            for key, value in (attrs or {}).items()
            if (
                isinstance(key, str)
                and (key.startswith("AcquisitionLayout:") or key in recording_keys)
            )
        }

    def _set_ome_annotations(self, detectorName: str, attrs: Dict[str, Any]) -> None:
        meta = (getattr(self, "omeMeta", None) or {}).get(detectorName)
        if meta is not None:
            meta.annotations.update(self._ome_annotation_attrs(attrs))

    def _collapse_layout_frame_axis(self, detectorName: str) -> None:
        """Drop the leading ``frame`` axis from the layout this file embeds.

        A single-frame OME-TIFF stores the plane itself rather than a
        one-element stack, so the container has one axis fewer than the layout
        declares. The layout is then rejected outright -- an explicit layout
        that disagrees with the container is never fallen back from, by design,
        so a scan-driven detector's recording became unreadable rather than
        merely unannotated. The frame axis is the one the storer dropped, so
        the recorded layout drops it too and goes on describing what is
        actually there.
        """
        meta = (getattr(self, "omeMeta", None) or {}).get(detectorName)
        if meta is None:
            return
        encoded = meta.annotations.get("AcquisitionLayout:json")
        if not encoded:
            return
        try:
            layout = decode_acquisition_layout(encoded)
            if not layout.storage_axes or layout.storage_axes[0] != "frame":
                return
            collapsed = replace(layout, storage_axes=layout.storage_axes[1:])
            meta.annotations["AcquisitionLayout:json"] = (
                encode_acquisition_layout(collapsed)
            )
        except Exception as error:
            logger.warning(
                f"Could not adapt the acquisition layout of {detectorName!r} "
                f"to the collapsed single-frame TIFF axis; the file will carry "
                f"a layout that does not match its rank: {error}"
            )

    def _snapshot_attrs(
        self,
        detectorName: str,
        attrs: Dict[str, Any] | None,
        image: Any,
    ) -> Dict[str, Any]:
        completed = dict(attrs or {})
        array = np.asarray(image)
        actual_frames = 1 if array.ndim <= 2 else int(array.shape[0])
        completed.setdefault("recording:planned_frames", actual_frames)
        completed["recording:actual_frames"] = actual_frames
        completed.setdefault("recording:planned_partitions", 1)
        completed["recording:actual_partitions"] = 1
        completed["recording:completion_outcome"] = "complete"
        self._set_ome_annotations(detectorName, completed)
        return completed

    def noteDiscardedFrames(self, discarded: Dict[str, int]) -> None:
        """Frames the producer received beyond the plan and did not write.

        Recorded per detector as ``recording:discarded_frames`` at finalize,
        so a file whose camera ran free or was pulsed more often than the
        scan declared does not look like a clean scan to a reader.
        """
        self._discardedFrames = {
            str(k): int(v) for k, v in (discarded or {}).items() if int(v) > 0
        }

    def _finalize_recording_attrs(
        self,
        currentFrames: Dict[str, int],
    ) -> Dict[str, Dict[str, Any]]:
        """Add final counts/outcomes without changing writer-liveness markers."""
        finalized: Dict[str, Dict[str, Any]] = {}
        source = getattr(self, "_attrs", {}) or {}
        discarded = getattr(self, "_discardedFrames", {}) or {}
        detector_names = set(source) | set(currentFrames)
        for detectorName in detector_names:
            attrs = dict(source.get(detectorName, {}) or {})
            actual_frames = int(max(0, currentFrames.get(detectorName, 0)))
            attrs["recording:discarded_frames"] = int(discarded.get(detectorName, 0))
            planned_frames = attrs.get("recording:planned_frames")
            try:
                planned_frames = int(planned_frames) if planned_frames is not None else None
            except (TypeError, ValueError):
                planned_frames = None
            attrs["recording:actual_frames"] = actual_frames
            attrs.setdefault("recording:planned_partitions", 1)
            attrs["recording:actual_partitions"] = 1
            attrs["recording:completion_outcome"] = (
                "stopped_early"
                if planned_frames is not None and actual_frames < planned_frames
                else "complete"
            )
            finalized[detectorName] = attrs
            self._set_ome_annotations(detectorName, attrs)
        self._attrs = finalized
        return finalized

    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Store snapshot images with metadata.
        
        Args:
            images: Dict mapping detector name to image array (T, Y, X) or (Y, X)
            attrs: Dict mapping detector name to flat metadata dict with ':'-separated keys

        Returns:
            ``{detector: stored_shape}`` for what was actually written, or None
            from a storer that does not report it. A container may add axes of
            its own -- HDF5 gives every 2-D snapshot a leading frame axis -- so
            the stored shape is the writer's to state and no one else's to
            guess.
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

    def streamPayloadInfo(
        self, detectorName: str, currentFrames: Dict[str, int]
    ) -> Optional[StreamPayloadInfo]:
        """Describe a stream immediately before finalisation.

        Third-party storers predating payload manifests may return ``None``;
        the built-in storers override this with an exact locator.
        """
        return None

    def abortStream(self, filePaths: Dict[str, str],
                    fileDests: Dict[str, Union[str, 'BytesIO']], saveMode) -> None:
        """Abort a streaming session, discarding partial output.

        Called instead of finalizeStream when a recording is aborted. Closes any
        open handles and removes the partial output file(s)/store(s) so no
        truncated dataset is left behind. Best-effort: failures are logged, not
        raised, so an abort always completes.

        Args:
            filePaths: Dict mapping detector name to on-disk file path
            fileDests: Dict mapping detector name to file path or BytesIO
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
                      dtype: Any = None, dimension_names: tuple | None = None) -> Any:
        if hasattr(root, 'create_array'):
            # Zarr v3: flat v2-style chunk keys ("0.0.0") instead of the v3
            # default nested folders ("c/0/0/0"), so a recording creates far
            # fewer on-disk directories. On zarr v2 this method may be the
            # compatibility shim installed above, which ignores v3-only kwargs.
            kwargs = {'chunks': chunks,
                      'chunk_key_encoding': {'name': 'v2', 'configuration': {'separator': '.'}}}
            if dimension_names is not None:
                kwargs['dimension_names'] = tuple(dimension_names)
            if data is not None:
                kwargs['data'] = data
            else:
                kwargs['shape'] = shape
                kwargs['dtype'] = dtype
            try:
                return root.create_array(name, **kwargs)
            except TypeError:
                # Older zarr builds do not expose dimension_names yet. The NGFF
                # multiscales block remains authoritative in that case.
                kwargs.pop('dimension_names', None)
                array = root.create_array(name, **kwargs)
                write_zarr_json_sidecar(array, dimension_names)
                return array

        kwargs = {'chunks': chunks}
        if data is not None:
            kwargs['data'] = data
        if shape is not None:
            kwargs['shape'] = shape
        if dtype is not None:
            kwargs['dtype'] = dtype
        dataset = root.create_dataset(name, **kwargs)
        write_zarr_json_sidecar(dataset, dimension_names)
        return dataset

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

    def _dimension_names(self, detectorName: str, ndim: int) -> tuple[str, ...]:
        meta = (self.omeMeta or {}).get(detectorName)
        if meta is not None:
            return tuple(axis.name for axis in meta.padded_to(ndim).axes)
        fallback = ('t', 'y', 'x')
        return fallback[-int(ndim):]

    def _register_root_series(self, root: Any, path: str) -> None:
        """Add a lightweight NGFF discovery index for detector image groups."""
        try:
            ome = dict(root.attrs.get('ome', {}) or {})
            series = list(ome.get('series', []))
            if not any(isinstance(item, dict) and item.get('path') == path
                       for item in series):
                series.append({'path': path})
            ome['version'] = '0.5'
            ome['series'] = series
            root.attrs['ome'] = ome
        except Exception as e:
            logger.debug(f'Could not update root OME-Zarr series metadata: {e}')

    def _set_ngff_attrs(self, det_group: Any, detectorName: str, dataset: Any) -> None:
        """Write OME-NGFF 0.5 ``multiscales`` metadata onto the detector group.

        Spec-compliant standard layout: ``datasets[].path`` points at the existing
        ``data`` array (a relative path is allowed), so standard OME-Zarr readers
        resolve axes/pixel-size while ImSwitch's own readers (which still open
        ``data``) are unaffected. ImSwitch extras stay in ``metadata/`` and the
        legacy array attrs, never under ``ome``.
        """
        meta = (self.omeMeta or {}).get(detectorName)
        if meta is None:
            return
        try:
            det_group.attrs['ome'] = meta.ngff_ome_metadata(path='data', ndim=dataset.ndim)
        except Exception as e:
            logger.debug(f'Could not write OME-NGFF metadata for {detectorName}: {e}')

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
                dimension_names=self._dimension_names(detectorName, data.ndim),
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
        dataset.attrs['axes'] = [name.upper()
                                 for name in self._dimension_names(detectorName, dataset.ndim)]
        dataset.attrs['writing'] = writing

        for key, value in self._layout_attrs(attrs).items():
            dataset.attrs[key] = self._zarr_attr_value(value)

        grouped = self._group_metadata_by_category(
            {key: value for key, value in attrs.items() if key not in self._layout_attrs(attrs)}
        )
        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:
                    cat_group = meta_group.create_group(category)
                    self._set_attrs(cat_group, cat_attrs, category)
                else:
                    self._set_attrs(meta_group, cat_attrs, 'metadata')

        self._set_ngff_attrs(det_group, detectorName, dataset)
        imagePath = f'{groupPath}/{detectorName}' if groupPath else detectorName
        self._register_root_series(root, imagePath)
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
            dimension_names=self._dimension_names(
                detectorName, 1 + len(spatialShape)
            ),
        )
        dataset.attrs['detector_name'] = detectorName
        dataset.attrs['element_size_um'] = self._zarr_attr_value(
            self.detectorManager[detectorName].pixelSizeUm
        )
        dataset.attrs['axes'] = [name.upper()
                                 for name in self._dimension_names(detectorName, dataset.ndim)]
        dataset.attrs['writing'] = True
        # Committed-frames barrier for live readers: Zarr resizes the array
        # BEFORE writing frame data, so array.shape runs ahead of the data on
        # disk. This attr is updated AFTER each batch write, so a reader that
        # honours it never reads uninitialised chunks.
        dataset.attrs['recording:frames_committed'] = 0

        for key, value in self._layout_attrs(attrs).items():
            dataset.attrs[key] = self._zarr_attr_value(value)

        recording_attrs, other_attrs = self._split_recording_attrs(attrs)
        recording_attrs['detector_name'] = detectorName
        recording_attrs['dataset_path'] = (
            f'/{groupPath}/{detectorName}/data' if groupPath else f'/{detectorName}/data'
        )
        recording_attrs.setdefault('source_format', 'ZARR')
        for key, value in recording_attrs.items():
            try:
                dataset.attrs[f'recording:{key}'] = self._zarr_attr_value(value)
            except Exception as e:
                logger.debug(f'Could not save Zarr recording metadata {key}={value}: {e}')

        grouped = self._group_metadata_by_category(
            {
                key: value
                for key, value in other_attrs.items()
                if key not in self._layout_attrs(other_attrs)
            }
        )
        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:
                    cat_group = meta_group.create_group(category)
                    self._set_attrs(cat_group, cat_attrs, category)
                else:
                    self._set_attrs(meta_group, cat_attrs, 'metadata')

        self._set_ngff_attrs(det_group, detectorName, dataset)
        imagePath = f'{groupPath}/{detectorName}' if groupPath else detectorName
        self._register_root_series(root, imagePath)
        return dataset

    @staticmethod
    def _split_recording_attrs(attrs: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        recording_attrs = {}
        other_attrs = {}
        for key, value in attrs.items():
            if isinstance(key, str) and key.startswith('recording:'):
                recording_attrs[key.split(':', 1)[1]] = value
            else:
                other_attrs[key] = value
        return recording_attrs, other_attrs

    def snap(self, images: Dict[str, np.ndarray],
             attrs: Dict[str, Dict[str, Any]] = None):
        attrs = attrs or {}
        storedShapes = {}
        with AsTemporaryFile(f'{self.filepath}.zarr') as path:
            store = self._make_store(path)
            root = zarr.group(store=store, overwrite=True)
            root.attrs['timestamp'] = time.time()
            root.attrs['rec_mode'] = 'snap'

            for channel, image in images.items():
                channel_attrs = self._snapshot_attrs(channel, attrs.get(channel, {}), image)
                self._createDetectorGroup(
                    root,
                    channel,
                    np.asarray(image).dtype,
                    channel_attrs,
                    data=image,
                )
                array = np.asarray(image)
                storedShapes[channel] = (
                    (1,) + array.shape if array.ndim == 2 else array.shape
                )
            self._close_store(store)
            logger.info(f"Saved image to zarr store {path} with structured layout")
        return storedShapes
    
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
            # Everything after the leading frame axis belongs to one logical
            # detector frame. Scan-driven point detectors may include a
            # linestep plane axis: (N, S, Y, X).
            spatialShape = frames.shape[1:]
            
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
        dataset.resize((newSize, *dataset.shape[1:]))
        dataset[it:newSize, ...] = frames
        # Barrier AFTER the data: a reader seeing frames_committed=N is
        # guaranteed frames [0, N) are on disk, even though shape resized early.
        dataset.attrs['recording:frames_committed'] = newSize

        self._currentFrames[detectorName] += len(frames)
    
    def finalizeStream(self, currentFrames: Dict[str, int], filePaths: Dict[str, str],
                       recordingManager, saveMode: 'SaveMode') -> None:
        """Close Zarr stores and emit memory-recording signals when applicable."""
        finalized_attrs = self._finalize_recording_attrs(currentFrames)
        for detectorName, dataset in self._datasets.items():
            dataset.attrs['writing'] = False
            if currentFrames[detectorName] < 1:
                dataset.resize((0, *dataset.shape[1:]))
            dataset.attrs['recording:frames_committed'] = int(
                max(0, currentFrames.get(detectorName, 0))
            )
            for key, value in finalized_attrs.get(detectorName, {}).items():
                if isinstance(key, str) and key.startswith("recording:"):
                    dataset.attrs[key] = self._zarr_attr_value(value)

        if saveMode == SaveMode.DiskAndRAM:
            for detectorName in self._datasets:
                filePath = filePaths[detectorName]
                name = os.path.basename(filePath)
                recordingManager.sigMemoryRecordingAvailable.emit(
                    name, self._roots[self._fileDests[detectorName]], filePath, True
                )

        for store in self._stores.values():
            self._close_store(store)

    def streamPayloadInfo(self, detectorName, currentFrames):
        dataset = getattr(self, '_datasets', {}).get(detectorName)
        if dataset is None:
            return None
        parent = getattr(self, '_groupPaths', {}).get(detectorName)
        group = f'{parent}/{detectorName}' if parent else detectorName
        return StreamPayloadInfo(
            group=group,
            stored_shape=tuple(int(size) for size in dataset.shape),
            frame_axis_stored=True,
        )

    def abortStream(self, filePaths, fileDests, saveMode):
        """Close Zarr stores and remove the partial .zarr directories."""
        for store in getattr(self, '_stores', {}).values():
            try:
                self._close_store(store)
            except Exception as e:
                logger.warning(f'Zarr abort: failed to close store: {e}')
        for dest in set(getattr(self, '_fileDests', {}).values()):
            try:
                if isinstance(dest, str) and os.path.isdir(dest):
                    shutil.rmtree(dest, ignore_errors=True)
            except Exception as e:
                logger.warning(f'Zarr abort: failed to remove store {dest}: {e}')


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

    @staticmethod
    def _set_hdf5_attr(target, key, value, context) -> None:
        """Write an HDF5 attribute, JSON-encoding values h5py can't store natively.

        h5py attrs only accept native scalar/array dtypes; a dict or ragged list
        (e.g. per-device ScanTTL timing maps) raises "Object dtype ... has no
        native HDF5 equivalent" and used to be dropped silently. JSON-encoding
        keeps the value round-trippable through SharedAttributes.fromHDF5File
        instead of losing it.
        """
        try:
            if key in target.attrs:
                attr_id = target.attrs.get_id(key)
                if attr_id.dtype.kind == "S" and isinstance(value, str):
                    value = value.encode("utf-8")
                target.attrs.modify(key, value)
            else:
                target.attrs[key] = value
            return
        except Exception:
            pass
        try:
            target.attrs[key] = JSON_ATTR_PREFIX + json.dumps(value, default=str)
        except Exception as e:
            logger.debug(f'Could not save metadata {context}/{key}={value}: {e}')

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
            shape = maxshape[1:]
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
            # Ensure a leading logical-frame axis. This is exactly the kind of
            # change a reader cannot predict from the array it was handed, so
            # snap() reports the resulting shape back to the caller.
            if data.ndim == 2:
                data = data[np.newaxis, ...]
            chunks = (1, *data.shape[1:]) if data.ndim >= 3 else True
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
        dataset.attrs['element_size_um'] = self._elementSizeUm(detectorName, dataset.ndim)
        if data is not None:
            dataset.attrs["writing"] = False

        layout_attrs = self._layout_attrs(attrs)
        for key, value in layout_attrs.items():
            self._set_hdf5_attr(dataset, key, value, "acquisition-layout")

        recording_attrs, other_attrs = self._split_recording_attrs(attrs)
        if maxshape is not None or recording_attrs:
            if maxshape is not None:
                # These final fields must exist before SWMR is enabled. A live
                # reader may keep the file open during post-close finalization;
                # modifying existing attrs is safe, while creating new attrs
                # can block in HDF5. Blank outcome is normalized as absent.
                recording_attrs.setdefault("planned_partitions", 1)
                recording_attrs.setdefault("actual_frames", 0)
                recording_attrs.setdefault("actual_partitions", 0)
            recording_attrs['detector_name'] = detectorName
            recording_attrs['dataset_path'] = (
                f'/{groupPath}/{detectorName}/data' if groupPath else f'/{detectorName}/data'
            )
            recording_attrs.setdefault('source_format', 'HDF5')
            for key, value in recording_attrs.items():
                self._set_hdf5_attr(dataset, f'recording:{key}', value, 'recording')
            if maxshape is not None and "recording:completion_outcome" not in dataset.attrs:
                dataset.attrs.create(
                    "recording:completion_outcome",
                    np.bytes_(""),
                    dtype="S13",
                )

        # Group attrs by category and create metadata subgroups
        grouped = self._group_metadata_by_category(
            {key: value for key, value in other_attrs.items() if key not in layout_attrs}
        )

        if grouped:
            meta_group = det_group.create_group('metadata')
            for category, cat_attrs in grouped.items():
                if category:  # Non-empty category -> subgroup
                    cat_group = meta_group.create_group(category)
                    for key, value in cat_attrs.items():
                        self._set_hdf5_attr(cat_group, key, value, category)
                else:  # Empty category -> flat in metadata group
                    for key, value in cat_attrs.items():
                        self._set_hdf5_attr(meta_group, key, value, 'metadata')

        # Snapshot: shape is known now, so embed OME-XML. Streaming (maxshape)
        # defers to finalizeStream (frame count unknown here, and SWMR forbids
        # adding attrs once enabled).
        if data is not None:
            self._embed_ome_xml(det_group, detectorName, dataset.shape)

        return dataset

    def _elementSizeUm(self, detectorName, ndim) -> list:
        """Fiji ``element_size_um`` = spatial ``[z, y, x]`` pixel sizes.

        When OME metadata is available, spatial axes take their calibrated size
        from it — crucially the scan Z *step* rather than the detector's static
        Z pixel size, so a z-stack reads back with the right axial spacing. A
        recording without a Z axis (snap/timelapse) keeps the detector's Z pixel
        size in the leading slot; the time interval is a temporal quantity and
        does not belong in the spatial ``element_size_um``.
        """
        detector_px = list(self.detectorManager[detectorName].pixelSizeUm)
        while len(detector_px) < 3:
            detector_px.insert(0, 1.0)
        meta = (self.omeMeta or {}).get(detectorName)
        if meta is None:
            return detector_px
        try:
            padded = meta.padded_to(ndim)
            by_name = {axis.name: scale for axis, scale in zip(padded.axes, padded.scale)}
        except Exception:
            return detector_px
        return [
            float(by_name.get('z', detector_px[0])),
            float(by_name.get('y', detector_px[1])),
            float(by_name.get('x', detector_px[2])),
        ]

    def _embed_ome_xml(self, group, detectorName, shape) -> None:
        """Embed the shared OME model as OME-XML (``ome_xml`` group attr) for
        logical metadata parity with OME-TIFF/OME-NGFF. HDF5 has no OME container
        standard, so this is a best-effort payload alongside Fiji ``element_size_um``;
        no reader auto-detects it.

        Also records the axis order as an explicit ``axes`` attr on the ``data``
        dataset (mirroring the Zarr storer and ImageJ/TIFF ``axes`` metadata).
        This is what lets the reader label a timelapse's leading axis ``T``
        instead of defaulting a 3D stack's first axis to ``C``.
        """
        meta = (self.omeMeta or {}).get(detectorName)
        if meta is None:
            return
        try:
            group.attrs['ome_xml'] = _ome.build_ome_xml(meta.padded_to(len(shape)), shape)
        except Exception as e:
            logger.debug(f'Could not embed OME-XML for {detectorName}: {e}')
        try:
            data_node = group.get('data')
            if data_node is not None:
                data_node.attrs['axes'] = meta.padded_to(len(shape)).axes_string
        except Exception as e:
            logger.debug(f'Could not write HDF5 axes attr for {detectorName}: {e}')

    @staticmethod
    def _split_recording_attrs(attrs: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        recording_attrs = {}
        other_attrs = {}
        for key, value in attrs.items():
            if isinstance(key, str) and key.startswith('recording:'):
                recording_attrs[key.split(':', 1)[1]] = value
            else:
                other_attrs[key] = value
        return recording_attrs, other_attrs

    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Save snapshot with structured HDF5 layout.

        Creates one file per detector with structured groups and lossless compression.
        """
        attrs = attrs or {}

        storedShapes = {}
        for channel, image in images.items():
            with AsTemporaryFile(f'{self.filepath}_{channel}.h5') as path:
                with h5py.File(path, 'w') as file:
                    # File-level metadata
                    file.attrs['timestamp'] = time.time()
                    file.attrs['rec_mode'] = 'snap'

                    # Create structured detector group using shared helper
                    channel_attrs = self._snapshot_attrs(channel, attrs.get(channel, {}), image)
                    self._createDetectorGroup(
                        file, channel, image.dtype, channel_attrs,
                        data=image
                    )
                    # The same leading frame axis _createDetectorGroup adds to
                    # a 2-D image. Reported, not left for a reader to deduce.
                    stored = np.asarray(image)
                    storedShapes[channel] = (
                        (1,) + stored.shape if stored.ndim == 2
                        else stored.shape
                    )

                logger.info(f"Saved snapshot to {path} with structured HDF5 layout")
    
        return storedShapes

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
        self._swmr_enabled = set()  # Track files for which SWMR mode was enabled
        # Committed-frames barrier + completion marker for live SWMR readers.
        # SWMR forbids attribute writes after swmr_mode=True, but dataset
        # writes are allowed - so these live as 1-element datasets next to
        # 'data' instead of attrs (see writeFrames/finalizeStream).
        self._committedDatasets = {}
        self._completeDatasets = {}

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
                # Enable SWMR for disk-based streaming recordings
                # (not RAM mode, which uses BytesIO and doesn't support SWMR)
                if saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
                    self._files[detectorName] = h5py.File(
                        fileDests[detectorName], mode, libver=HDF5_STREAM_LIBVER
                    )
                else:
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
            # Preserve all per-frame axes, including APD/PMT linesteps.
            spatialShape = frames.shape[1:]

            file = self._files[detectorName]
            groupPath = self._groupPaths[detectorName]

            # Temporarily override compression for streaming
            original_compression = self.compression
            self.compression = self._streamCompression
            try:
                # Create dataset with the DECLARED dtype (the contract), not frames.dtype
                dataset = self._createDetectorGroup(
                    file, detectorName, declared, self._attrs[detectorName],
                    maxshape=(None, *spatialShape),
                    groupPath=groupPath
                )
            finally:
                self.compression = original_compression
            
            dataset.attrs['writing'] = True
            self._datasets[detectorName] = dataset

            # Live-reader barrier: frames_committed is updated AFTER each data
            # write+flush, so a SWMR reader that honours it never reads ahead
            # of the data actually on disk. stream_complete is set at finalize
            # WHILE the SWMR handle is still open - unlike the writing attr,
            # which is rewritten via a post-close r+ reopen that a live SWMR
            # reader can never see. Both must exist before SWMR is enabled
            # (no new objects afterwards).
            det_group = dataset.parent
            self._committedDatasets[detectorName] = det_group.create_dataset(
                'frames_committed', shape=(1,), dtype=np.int64
            )
            self._completeDatasets[detectorName] = det_group.create_dataset(
                'stream_complete', shape=(1,), dtype=np.uint8
            )

            # Enable SWMR mode after creating datasets and metadata
            # (SWMR forbids creating objects after swmr_mode=True)
            file_id = id(file)
            if self._saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM) and file_id not in self._swmr_enabled:
                file.flush()
                file.swmr_mode = True
                self._swmr_enabled.add(file_id)

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
        dataset[currentSize:newSize, ...] = frames

        # Flush after each write so SWMR readers see growth
        if self._saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
            file = self._files[detectorName]
            file.flush()

        # Barrier AFTER the data is flushed: a reader seeing
        # frames_committed=N is guaranteed frames [0, N) are visible.
        committed = self._committedDatasets.get(detectorName)
        if committed is not None:
            committed[0] = newSize
            if self._saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
                self._files[detectorName].flush()
    
    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        """Close HDF5 files and emit signals."""
        finalized_attrs = self._finalize_recording_attrs(currentFrames)
        pending_memory_signals = []
        pending_disk_memory_signals = []

        # Track dataset paths for updating writing attribute after SWMR close
        dataset_paths = {}
        for detectorName, dataset in self._datasets.items():
            if dataset is not None:
                dataset_paths[detectorName] = dataset.name

        # Finalize every detector dataset before closing any shared file handle.
        # In singleMultiDetectorFile mode multiple detector names point to the
        # same h5py.File; closing inside this loop invalidates later datasets.
        for detectorName, dataset in self._datasets.items():
            file = self._files[detectorName]
            # Remove empty datasets (if no frames captured)
            if dataset is not None and currentFrames[detectorName] < 1:
                dataset.resize(0, axis=0)

            # Final barrier + completion marker, written while the SWMR handle
            # is still open so a live SWMR reader (which never sees the
            # writing=False attr rewritten after close) can terminate.
            committed = self._committedDatasets.get(detectorName)
            if committed is not None:
                committed[0] = int(max(0, currentFrames.get(detectorName, 0)))
            complete = self._completeDatasets.get(detectorName)
            if complete is not None:
                complete[0] = 1
            if (committed is not None or complete is not None) and \
                    saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
                file.flush()

            # For RAM mode, we can directly modify attributes now (no SWMR).
            if saveMode == SaveMode.RAM and dataset is not None:
                dataset.attrs['writing'] = False
                for key, value in finalized_attrs.get(detectorName, {}).items():
                    if isinstance(key, str) and key.startswith("recording:"):
                        self._set_hdf5_attr(dataset, key, value, "recording")
                if currentFrames.get(detectorName, 0) >= 1:
                    self._embed_ome_xml(dataset.parent, detectorName, dataset.shape)
            
            # Emit signal for each detector (even in singleMultiDetectorFile mode).
            # RAM-backed BytesIO recordings are announced after their HDF5 file
            # handle is closed below, so consumers never see a half-finalized
            # in-memory file.
            if saveMode == SaveMode.RAM or saveMode == SaveMode.DiskAndRAM:
                filePath = filePaths[detectorName]
                name = os.path.basename(filePath)
                if saveMode == SaveMode.RAM:
                    pending_memory_signals.append(
                        (name, self._fileDests[detectorName], filePath, False)
                    )
                else:  # DiskAndRAM
                    pending_disk_memory_signals.append((name, filePath, True))

        # Only close/flush each unique file once (handles singleMultiDetectorFile mode).
        processed_files = set()
        for file in self._files.values():
            file_id = id(file)
            if file_id in processed_files:
                continue
            processed_files.add(file_id)

            if saveMode == SaveMode.RAM:
                file.close()
            elif saveMode == SaveMode.DiskAndRAM:
                file.flush()
                file.close()
            elif saveMode == SaveMode.Disk:
                file.close()

        # For disk-based SWMR recordings, reopen files to set writing=False
        # (cannot modify attributes while in SWMR mode)
        if saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
            paths_to_datasets = {}
            for detectorName, dataset_path in dataset_paths.items():
                filePath = filePaths[detectorName]
                paths_to_datasets.setdefault(filePath, []).append((detectorName, dataset_path))

            for filePath, entries in paths_to_datasets.items():
                # Reopen file in read/write mode (not SWMR) to update writing
                # attribute and embed OME-XML (both forbidden under SWMR).
                # This reopen can fail while a live reader holds a SWMR handle
                # on the file (e.g. improcess live reconstruction following
                # the recording) - HDF5 refuses attribute writes then. That
                # must not crash the recording teardown: live readers
                # terminate via the stream_complete marker written above, and
                # the completion gate accepts the marker as well.
                try:
                    with h5py.File(filePath, 'r+', libver=HDF5_STREAM_LIBVER) as f:
                        for detectorName, dataset_path in entries:
                            if dataset_path in f:
                                f[dataset_path].attrs['writing'] = False
                                for key, value in finalized_attrs.get(detectorName, {}).items():
                                    if isinstance(key, str) and key.startswith("recording:"):
                                        self._set_hdf5_attr(
                                            f[dataset_path], key, value, "recording"
                                        )
                                if currentFrames.get(detectorName, 0) >= 1:
                                    self._embed_ome_xml(
                                        f[dataset_path].parent, detectorName, f[dataset_path].shape)
                except OSError as e:
                    logger.warning(
                        f'HDF5 finalize: could not rewrite writing=False / embed '
                        f'OME-XML in {filePath} (a live reader may hold the file '
                        f'open): {e}. The stream_complete marker still marks the '
                        f'recording as finished.'
                    )

        if recordingManager is not None:
            for signalArgs in pending_memory_signals:
                recordingManager.sigMemoryRecordingAvailable.emit(*signalArgs)

            # DiskAndRAM should not keep the SWMR writer handle open after
            # finalization. Reopen read-only for in-app consumers; the saved file
            # is then a normal, completed HDF5 file for external viewers too.
            for name, filePath, savedToDisk in pending_disk_memory_signals:
                try:
                    readFile = h5py.File(filePath, 'r', libver=HDF5_STREAM_LIBVER)
                except OSError as e:
                    logger.warning(f'HDF5 finalize: could not reopen {filePath} for memory hand-off: {e}')
                    continue
                recordingManager.sigMemoryRecordingAvailable.emit(
                    name, readFile, filePath, savedToDisk
                )

    def abortStream(self, filePaths, fileDests, saveMode):
        """Close HDF5 files and remove the partial on-disk file(s)."""
        processed = set()
        for file in getattr(self, '_files', {}).values():
            if id(file) in processed:
                continue
            processed.add(id(file))
            try:
                file.close()
            except Exception as e:
                logger.warning(f'HDF5 abort: failed to close file: {e}')
        if saveMode in (SaveMode.Disk, SaveMode.DiskAndRAM):
            for path in set(filePaths.values()):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.warning(f'HDF5 abort: failed to remove {path}: {e}')

    def streamPayloadInfo(self, detectorName, currentFrames):
        dataset = getattr(self, '_datasets', {}).get(detectorName)
        if dataset is None:
            return None
        parent = getattr(self, '_groupPaths', {}).get(detectorName)
        group = f'{parent}/{detectorName}' if parent else detectorName
        return StreamPayloadInfo(
            group=group,
            stored_shape=tuple(int(size) for size in dataset.shape),
            frame_axis_stored=True,
        )


class TiffStorer(Storer):
    """Storer for OME-TIFF.

    Snap writes a native OME-TIFF in one shot. Streaming writes a plain BigTIFF
    (per-frame contiguous appends -- no 4 GB limit) and embeds the OME-XML at
    finalize, once the frame count is known: tifffile's OME mode needs the full
    dimensional shape up front, which a stream doesn't have. The shared
    :class:`OmeImageMeta` (``self.omeMeta``) provides the axes/pixel-size; a
    minimal detector-derived meta is used as a fallback.
    """

    def _meta_for(self, detectorName, image=None, n_frames=None):
        meta = (self.omeMeta or {}).get(detectorName)
        if meta is not None:
            return meta
        det = self.detectorManager[detectorName]
        if image is not None:
            n_frames = 1 if np.asarray(image).ndim == 2 else int(np.asarray(image).shape[0])
        mode = _ome.MODE_SNAP if (n_frames or 1) <= 1 else _ome.MODE_TIMELAPSE
        pix = list(det.pixelSizeUm)
        py = pix[1] if len(pix) > 1 else 1.0
        px = pix[2] if len(pix) > 2 else py
        return _ome.build_ome_image_meta(
            detectorName, mode, n_frames or 1,
            pixel_size_yx_um=(py, px), dtype=det.dtype)

    def snap(self, images: Dict[str, np.ndarray], attrs: Dict[str, Dict[str, str]] = None):
        """Save snapshot as a native OME-TIFF (one shot, shape known)."""
        attrs = attrs or {}
        storedShapes = {}
        for channel, image in images.items():
            image = np.asarray(image)
            self._snapshot_attrs(channel, attrs.get(channel, {}), image)
            # OME-TIFF stores the array as handed over.
            storedShapes[channel] = image.shape
            with AsTemporaryFile(f'{self.filepath}_{channel}.ome.tiff') as path:
                meta = self._meta_for(channel, image=image)
                tiff.imwrite(path, image, ome=True, bigtiff=True,
                             metadata=meta.tiff_metadata(image.shape))
                tiff.tiffcomment(path, _ome.build_ome_xml(meta, image.shape))
                logger.info(f"Saved OME-TIFF snapshot to {path}")
    
        return storedShapes

    def openStream(self, fileDests, detectorNames, shapes, attrs, *,
                   singleMultiDetectorFile, singleLapseFile, saveMode):
        """Open one plain BigTIFF per detector; OME-XML is embedded at finalize."""
        self._writers = {}
        self._paths = {}
        self._spatial = {}            # detectorName -> per-frame shape
        self._dtypeWarned = set()
        self._attrs = attrs
        for detectorName in detectorNames:
            path = fileDests[detectorName]
            self._paths[detectorName] = path
            # ome=False + plain BigTIFF: appended frame-by-frame with no size
            # cap. OME mode is deferred to finalize (it needs the full shape).
            self._writers[detectorName] = tiff.TiffWriter(path, ome=False, bigtiff=True)

    def writeFrames(self, detectorName, frames):
        """Append frames (2D contiguous) to the detector's BigTIFF."""
        if len(frames) == 0:
            return
        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[np.newaxis, ...]

        declared = self.detectorManager[detectorName].dtype
        if frames.dtype != declared and detectorName not in self._dtypeWarned:
            logger.warning(
                f"TiffStorer dtype mismatch for '{detectorName}': "
                f"declared={declared}, actual frame={frames.dtype}. "
                f"Recording with declared dtype (frames will be cast). "
                f"This warning is shown once per detector per recording."
            )
            self._dtypeWarned.add(detectorName)
        if frames.dtype != declared:
            frames = frames.astype(declared)

        self._spatial[detectorName] = tuple(
            int(s) for s in frames.shape[1:]
        )
        tw = self._writers[detectorName]
        # One contiguous (N,Y,X) series: write each 2D plane appended in place.
        for frame in frames:
            tw.write(frame, contiguous=True)

    def finalizeStream(self, currentFrames, filePaths, recordingManager, saveMode):
        """Close writers, then embed OME-XML now that the frame count is known."""
        self._finalize_recording_attrs(currentFrames)
        errors = []
        for detectorName, tw in getattr(self, '_writers', {}).items():
            try:
                tw.close()
            except Exception as e:
                errors.append((
                    f'failed to close writer for "{detectorName}"', e
                ))
                logger.error(
                    f'TIFF finalize: failed to close writer for '
                    f'{detectorName}: {e}',
                    exc_info=True,
                )
        for detectorName, path in getattr(self, '_paths', {}).items():
            try:
                n = int(currentFrames.get(detectorName, 0))
                if n <= 0 or detectorName not in self._spatial:
                    continue
                frame_shape = self._spatial[detectorName]
                meta = self._meta_for(detectorName, n_frames=n)
                if n == 1 and len(meta.axes) == len(frame_shape):
                    self._collapse_layout_frame_axis(detectorName)
                    stored_meta = meta
                    shape = frame_shape
                else:
                    shape = (n, *frame_shape)
                    stored_meta = meta.padded_to(len(shape))
                tiff.tiffcomment(path, _ome.build_ome_xml(stored_meta, shape))
            except Exception as e:
                errors.append((
                    f'failed to embed OME-XML for "{detectorName}" at {path}',
                    e,
                ))
                logger.error(
                    f'Failed to embed OME-XML in {path}: {e}',
                    exc_info=True,
                )

        if errors:
            summary = '; '.join(
                f'{context}: {type(error).__name__}: {error}'
                for context, error in errors
            )
            raise RuntimeError(
                f'TIFF stream finalization failed ({len(errors)} error(s)): '
                f'{summary}'
            ) from errors[0][1]

    def streamPayloadInfo(self, detectorName, currentFrames):
        frame_shape = getattr(self, '_spatial', {}).get(detectorName)
        n_frames = int(currentFrames.get(detectorName, 0))
        if frame_shape is None or n_frames < 1:
            return None
        stored_shape = (
            tuple(frame_shape) if n_frames == 1
            else (n_frames, *tuple(frame_shape))
        )
        return StreamPayloadInfo(
            group=None,
            stored_shape=stored_shape,
            frame_axis_stored=n_frames > 1,
        )

    def abortStream(self, filePaths, fileDests, saveMode):
        """Close any open writers and delete the partial file(s)."""
        for tw in getattr(self, '_writers', {}).values():
            try:
                tw.close()
            except Exception:
                pass
        candidates = set(filePaths.values())
        candidates.update(getattr(self, '_paths', {}).values())
        for path in candidates:
            try:
                if isinstance(path, str) and os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning(f'TIFF abort: failed to remove {path}: {e}')


class FailureKind(enum.Enum):
    """What kind of thing went wrong, so a caller can decide whether to go on.

    A caller that survives some failures and not others cannot work this out
    from a message string. Matching on text would be worse than not
    classifying at all: it would look like a policy while silently
    misclassifying anything reworded.

    Only ``WRITER`` is recoverable in the sense that the run producing the
    recording may sensibly continue without it — the measurement happened, the
    file did not. Everything else means the data itself is suspect.
    """

    #: Serialising or writing failed. Nothing is wrong with the acquisition.
    WRITER = 'writer'
    #: Frames were lost, late, or never arrived.
    ACQUISITION = 'acquisition'
    #: The scan producing the data failed, was refused, or timed out.
    SCAN = 'scan'
    #: A device faulted or was lost.
    HARDWARE = 'hardware'
    #: Not classified at the point it was raised. Treated as unrecoverable —
    #: the safe reading, since the alternative is continuing past something
    #: nobody understood.
    UNKNOWN = 'unknown'

    @property
    def recoverable(self) -> bool:
        """Whether a multi-point run may reasonably continue past this."""
        return self is FailureKind.WRITER


@dataclass(frozen=True)
class PayloadLocator:
    """Where one detector's recorded data actually ended up.

    A filename is not enough to find it again: grouped HDF5/Zarr needs the
    group, and separate files may have been de-duplicated against existing
    ones. This is filled in from what the writer reports **after
    finalisation**, never guessed at dispatch — ``snapImagePrev`` already had
    to learn that lesson, and returns its written paths for the same reason.
    """

    path: str
    detector: str
    group: Optional[str] = None
    axes: str = ''
    stored_axes: str = ''
    shape: Tuple[int, ...] = ()
    stored_shape: Tuple[int, ...] = ()
    generation: Optional[int] = None
    complete: bool = False

    def asdict(self) -> Dict:
        return {
            'path': self.path,
            'detector': self.detector,
            'group': self.group,
            'axes': self.axes,
            'stored_axes': self.stored_axes,
            'shape': list(self.shape),
            'stored_shape': list(self.stored_shape),
            'generation': self.generation,
            'complete': self.complete,
        }


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
    sigRecordingFailed = Signal(str)
    # Internal identity-carrying companions. The legacy signals above remain
    # stable for plugins and the CommunicationChannel, while controllers that
    # can observe successive sessions use these to reject queued callbacks
    # from an older recording.
    sigRecordingStartedDetailed = Signal(int)
    sigRecordingEndedDetailed = Signal(int)
    sigRecordingFailedDetailed = Signal(str, int)
    #: message, generation, FailureKind value. The typed form; the two
    #: above stay for callers that only want to know that it failed.
    sigRecordingFailedTyped = Signal(str, int, str)
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
        self.__abort = False
        self.__activeDetectorNames = ()
        self.__acqStartedEvent = threading.Event()
        self.__acqStartFailed = False
        self.__recordingSignalLock = threading.Lock()
        self.__recordingGeneration = 0
        # Where each session's data actually landed, keyed by generation.
        # Filled by the worker once paths are resolved (they are de-duplicated
        # against existing files, so they cannot be predicted), and marked
        # complete only at writer finalisation.
        self.__payloadLocators = {}
        self.__endSignalEmitted = False
        self.__failureSignalEmitted = False
        self.__lastRecordingError = None
        # Scan-driven detectors publish one assembled frame only after the
        # scan has finished. Their no-frame watchdog is therefore anchored to
        # this completion time, not to recording arm time.
        self.__scanExpectedCompletionGeneration = None
        self.__scanExpectedCompletionTime = None
        self.__scanCompletionGeneration = None
        self.__scanCompletionTime = None
        self.__recordingWorker = None
        self.__thread = None
        self.__prepareRecordingThread()

    def __del__(self):
        try:
            self.endRecording(emitSignal=False, wait=True)
        except Exception:
            # Destructors must not leak cleanup failures as an ignored
            # exception. Explicit endRecording()/abortRecording() calls still
            # surface the same failure to their caller.
            pass
        if hasattr(super(), '__del__'):
            super().__del__()

    def __prepareRecordingThread(self):
        if self.__thread is not None:
            self.__quitRecordingThread()
            self.__waitForRecordingThread()
        self.__recordingWorker = RecordingWorker(self)
        self.__thread = Thread()
        self.__recordingWorker.moveToThread(self.__thread)
        self.__thread.started.connect(self.__recordingWorker.run)

    def __quitRecordingThread(self):
        """Request QThread shutdown, tolerating a deleted retained wrapper."""
        if (
            isinstance(self.__thread, QtCore.QThread)
            and sip.isdeleted(self.__thread)
        ):
            return
        try:
            self.__thread.quit()
        except RuntimeError:
            if (
                isinstance(self.__thread, QtCore.QThread)
                and sip.isdeleted(self.__thread)
            ):
                return
            raise

    def __waitForRecordingThread(self):
        """Wait for the owned QThread without ever blocking indefinitely."""
        if isinstance(self.__thread, QtCore.QThread):
            # A completed ImSwitch Thread may already have been deleteLater'd
            # while its Python wrapper is still retained by this manager. The
            # framework's unbounded wait() treats that as stopped; mirror that
            # behavior before invoking Qt's bounded C++ method.
            if sip.isdeleted(self.__thread):
                return
            try:
                stopped = QtCore.QThread.wait(
                    self.__thread, RECORDING_THREAD_STOP_TIMEOUT_MS
                )
            except RuntimeError:
                if sip.isdeleted(self.__thread):
                    return
                raise
            if not stopped:
                raise TimeoutError(
                    'Recording worker did not stop within 30 seconds'
                )
            return
        # Lightweight test/fallback thread implementations are synchronous.
        self.__thread.wait()

    @property
    def record(self):
        """ Whether a recording is currently being recorded. """
        return self.__record

    @property
    def aborting(self):
        """ Whether the current recording is being aborted (partial output
        discarded rather than finalized). """
        return self.__abort

    @property
    def detectorsManager(self):
        return self.__detectorsManager

    def shutdownComplete(self) -> bool:
        """Whether producer and writer workers have both left their run loops."""
        if self.__record:
            return False
        thread = self.__thread
        if thread is not None:
            try:
                if (
                    isinstance(thread, QtCore.QThread)
                    and sip.isdeleted(thread)
                ):
                    threadRunning = False
                else:
                    threadRunning = bool(thread.isRunning())
            except RuntimeError:
                threadRunning = False
            except Exception:
                return False
            if threadRunning:
                return False

        worker = self.__recordingWorker
        writer = getattr(worker, '_writerThread', None)
        if writer is not None:
            try:
                if writer.is_alive():
                    return False
            except Exception:
                return False
            # Reap the retained identity only after termination is proven.
            # A timed-out finish/abort deliberately leaves it here so a new
            # recording cannot overwrite an orphan that may still own files.
            if getattr(worker, '_writerThread', None) is writer:
                worker._writerThread = None
        return True

    def __normalizeDetectorNames(self, detectorNames):
        """Materialize, de-duplicate and validate a detector selection."""
        if isinstance(detectorNames, str):
            detectorNames = (detectorNames,)
        try:
            normalized = tuple(dict.fromkeys(detectorNames))
        except TypeError as e:
            raise ValueError(
                'detectorNames must be an iterable of hashable detector names'
            ) from e
        if not normalized:
            raise ValueError('No detectors to record specified')

        # Resolve every name before mutating recording state. This makes an
        # invalid selection fail synchronously instead of leaving record=True
        # for a worker that can never acquire its detector.
        for detectorName in normalized:
            self.__detectorsManager[detectorName]
        return normalized

    @staticmethod
    def __layoutFrameCount(layout):
        if layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
            return None
        if layout.recorded_event_spans is not None:
            return sum(
                span.count * span.repeats
                for span in layout.recorded_event_spans
            )
        return math.prod(loop.count for loop in layout.event_loops)

    def __normalizeAcquisitionLayouts(
        self,
        acquisitionLayouts,
        detectorNames,
        recMode,
        recFrames,
        numCamTTL,
        attrs=None,
    ):
        """Canonicalize and cross-check layouts before opening any writer."""
        if acquisitionLayouts is None:
            return {}
        try:
            supplied = dict(acquisitionLayouts)
        except (TypeError, ValueError) as error:
            raise TypeError(
                'acquisitionLayouts must be a detector-to-layout mapping'
            ) from error

        unknown = set(supplied) - set(detectorNames)
        if unknown:
            raise ValueError(
                'Acquisition layouts were supplied for unselected detectors: '
                f'{sorted(unknown)}'
            )

        normalized = {}
        for detectorName, rawLayout in supplied.items():
            if isinstance(rawLayout, AcquisitionLayout):
                layout = rawLayout
            elif isinstance(rawLayout, (str, bytes, bytearray)):
                layout = decode_acquisition_layout(rawLayout)
            else:
                raise TypeError(
                    f'Acquisition layout for {detectorName!r} must be an '
                    'AcquisitionLayout or encoded JSON'
                )

            issues = list(validate_acquisition_layout(layout))
            if layout.detector != detectorName:
                issues.append(
                    LayoutIssue(
                        'error',
                        'DETECTOR_MISMATCH',
                        f'Layout detector {layout.detector!r} does not match '
                        f'mapping key {detectorName!r}',
                        'detector',
                    )
                )

            if recMode in (RecMode.ScanOnce, RecMode.ScanLapse):
                detector = self.__detectorsManager[detectorName]
                scanDriven = bool(getattr(detector, 'isScanDriven', False))
                expectedKind = (
                    PAYLOAD_ASSEMBLED_IMAGE
                    if scanDriven else PAYLOAD_DETECTOR_FRAME_STREAM
                )
                if layout.payload_kind != expectedKind:
                    issues.append(
                        LayoutIssue(
                            'error',
                            'DETECTOR_PAYLOAD_MISMATCH',
                            f'Detector {detectorName!r} requires '
                            f'{expectedKind!r}, got {layout.payload_kind!r}',
                            'payload_kind',
                        )
                    )

                producerPositions = scan_position_count(layout)
                if producerPositions is None:
                    # Loop kinds are an open vocabulary. A layout using a kind
                    # this version cannot classify must not be rejected before
                    # a writer opens merely because the unknown loop was
                    # assumed to advance the scan.
                    self.__logger.info(
                        f'Skipping the scan-position cross-check for '
                        f'{detectorName!r}: its layout uses a loop kind this '
                        f'version cannot classify as positional.'
                    )
                elif recFrames is not None and producerPositions != int(recFrames):
                    issues.append(
                        LayoutIssue(
                            'error',
                            'SCAN_POSITION_COUNT_MISMATCH',
                            f'Layout describes {producerPositions} scan '
                            f'positions but recFrames is {recFrames}',
                            'event_loops',
                        )
                    )

                plannedFrames = self.__layoutFrameCount(layout)
                if plannedFrames is not None:
                    if plannedFrames <= 0:
                        issues.append(
                            LayoutIssue(
                                'error',
                                'EMPTY_FRAME_SELECTION',
                                'A selected detector must record at least one frame',
                                'recorded_event_spans',
                            )
                        )
                    elif (
                        producerPositions is not None
                        and producerPositions > 0
                        and plannedFrames % producerPositions == 0
                    ):
                        layoutPulses = plannedFrames // producerPositions
                        declared = (numCamTTL or {}).get(detectorName)
                        if declared is None and not scanDriven:
                            # The producer and this gate both used to default
                            # an undeclared detector to one pulse per position,
                            # so the check below compared a default with
                            # itself and a free-running camera was recorded as
                            # a certain, complete scan.
                            issues.append(
                                LayoutIssue(
                                    'error',
                                    'DETECTOR_PULSES_UNDECLARED',
                                    f'The scan declares no TTL pulse per '
                                    f'position for detector {detectorName!r} '
                                    f'(getNumCamTTL has no entry), so its '
                                    f'frames cannot be tied to scan '
                                    f'positions. Gate it in the scan, '
                                    f'deselect it, or record it in a '
                                    f'non-scan mode.',
                                    'recorded_event_spans',
                                )
                            )
                        elif declared is not None and layoutPulses != int(declared):
                            issues.append(
                                LayoutIssue(
                                    'error',
                                    'DETECTOR_PULSE_COUNT_MISMATCH',
                                    f'Layout selects {layoutPulses} detector '
                                    f'frame(s) per scan position but numCamTTL '
                                    f'is {int(declared)}',
                                    'recorded_event_spans',
                                )
                            )
                    elif producerPositions is not None and producerPositions > 0:
                        # A per-expanded-line mask can select a frame count
                        # that is not a multiple of the position count; that
                        # is legitimate, but the pulse cross-check cannot run
                        # on it and used to say nothing at all.
                        self.__logger.warning(
                            f'Skipping the pulses-per-position cross-check for '
                            f'{detectorName!r}: the layout selects '
                            f'{plannedFrames} frame(s) over {producerPositions} '
                            f'scan position(s), which is not a whole number of '
                            f'pulses per position (numCamTTL says '
                            f'{(numCamTTL or {}).get(detectorName)!r}).'
                        )

            self.__reportLayoutDisagreements(
                detectorName, layout, (attrs or {}).get(detectorName)
            )

            errors = tuple(
                issue for issue in issues if issue.severity == 'error'
            )
            if errors:
                raise AcquisitionLayoutError(
                    f'Invalid acquisition layout for {detectorName!r}',
                    errors,
                )
            # Encoding here enforces the common inline-size budget before
            # __prepareRecordingThread can create a writer or destination.
            encode_acquisition_layout(layout)
            normalized[detectorName] = layout
        return normalized

    def __reportLayoutDisagreements(self, detectorName, layout, detectorAttrs):
        """Log where the layout and the legacy attributes describe differently.

        Both are still written, so this is the window in which a producer bug
        is cheap to find: if the layout disagrees with the attributes recorded
        beside it, one of them is wrong, and today a reader can pick either.
        Nothing is failed on a disagreement -- the legacy attributes are on
        their way out and are not worth blocking a measurement over.
        """
        if not isinstance(detectorAttrs, dict):
            return
        loops = {loop.kind: loop for loop in layout.event_loops}
        expected = {
            'ScanTTL:Nx': getattr(loops.get('scan_x'), 'count', None),
            'ScanTTL:Ny': getattr(loops.get('scan_y'), 'count', None),
            'ScanTTL:n_linesteps': getattr(loops.get('condition'), 'count', None),
        }
        for key, derived in expected.items():
            if derived is None or key not in detectorAttrs:
                continue
            try:
                recorded = int(detectorAttrs[key])
            except (TypeError, ValueError):
                continue
            if recorded != derived:
                self.__logger.warning(
                    f'Acquisition layout for {detectorName!r} implies '
                    f'{key}={derived}, but the recording writes {recorded}. '
                    f'One of the two descriptions of this scan is wrong.'
                )

    def startRecording(self, detectorNames, recMode, savename, saveMode, attrs,
                       saveFormat=SaveFormat.HDF5, singleMultiDetectorFile=False, singleLapseFile=False,
                       recFrames=None, recTime=None, numCamTTL=None, stallTimeout=None,
                       recLapseTotal=1, recLapseIndex=0, scanDims=None,
                       scanStepSizes=None, recLapseIntervalS=None,
                       recLapseScheduledTime=None, acquisitionLayouts=None):
        """ Starts a recording with the specified detectors, recording mode,
        file name prefix and attributes to save to the recording per detector.
        In SpecFrames mode, recFrames (the number of frames) must be specified,
        and in SpecTime mode, recTime (the recording time in seconds) must be
        specified.
        
        Args:
            stallTimeout: Maximum seconds without frame progress before aborting
                         (None uses DEFAULT_STALL_TIMEOUT). Watchdog only applies
                         to streaming recording, not snap().
            recLapseTotal: Total timepoints in the lapse (default 1 for non-lapse).
            recLapseIndex: 0-based index of this stack within the lapse (default 0).
            recLapseIntervalS: Requested camera-lapse interval in seconds.
            recLapseScheduledTime: ISO-8601 planned start for this timepoint.
            acquisitionLayouts: Optional synthetic or producer-authored
                ``{detector: AcquisitionLayout}`` mapping. Layouts are
                canonicalized and cross-checked before a writer is opened.
        """

        self.__logger.info('Starting recording')
        if self.__record:
            raise RuntimeError('Cannot start a new recording while one is active')
        if not self.shutdownComplete():
            raise RuntimeError(
                'Cannot start a new recording while the previous producer or '
                'writer is still shutting down'
            )

        detectorNames = self.__normalizeDetectorNames(detectorNames)
        acquisitionLayouts = self.__normalizeAcquisitionLayouts(
            acquisitionLayouts,
            detectorNames,
            recMode,
            recFrames,
            numCamTTL,
            attrs,
        )
        self.__prepareRecordingThread()
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
        self.__recordingWorker.recLapseTotal = recLapseTotal
        self.__recordingWorker.recLapseIndex = recLapseIndex
        self.__recordingWorker.scanDims = scanDims
        self.__recordingWorker.scanStepSizes = scanStepSizes
        self.__recordingWorker.recLapseIntervalS = recLapseIntervalS
        self.__recordingWorker.recLapseScheduledTime = recLapseScheduledTime
        self.__recordingWorker.acquisitionLayouts = acquisitionLayouts
        self.__recordingWorker.stallTimeout = stallTimeout if stallTimeout is not None else DEFAULT_STALL_TIMEOUT

        self.__activeDetectorNames = detectorNames
        self.__abort = False
        self.__acqStartedEvent.clear()
        self.__acqStartFailed = False
        with self.__recordingSignalLock:
            self.__recordingGeneration += 1
            recordingGeneration = self.__recordingGeneration
            self.__endSignalEmitted = False
            self.__failureSignalEmitted = False
            self.__lastRecordingError = None
            self.__scanExpectedCompletionGeneration = None
            self.__scanExpectedCompletionTime = None
            self.__scanCompletionGeneration = None
            self.__scanCompletionTime = None
        self.__recordingWorker.recordingGeneration = recordingGeneration
        self.__record = True
        try:
            self.__thread.start()
        except Exception as error:
            self.__record = False
            self.__activeDetectorNames = ()
            # The worker thread never started: nothing was acquired and
            # nothing was written, so this is not a writer problem.
            self._signalRecordingFailed(error, recordingGeneration,
                                        FailureKind.HARDWARE)
            raise
        return recordingGeneration

    def endRecording(self, emitSignal=True, wait=True):
        """ Ends the current recording. Unless emitSignal is false, the
        sigRecordingEnded signal will be emitted. Unless wait is False, this
        method will wait until the recording is complete before returning. """

        failure = None
        if self.__record:
            self.__logger.info('Stopping recording')
        # Stop the producer before touching its consumer state. The previous
        # terminal flush raced readChunk() and could discard frames that had
        # not reached the writer yet.
        self.__record = False
        self.__activeDetectorNames = ()

        if self.__thread is not None:
            try:
                self.__quitRecordingThread()
            except Exception as e:
                failure = e
        if wait and self.__thread is not None:
            try:
                self.__waitForRecordingThread()
            except Exception as e:
                failure = failure or e
        if emitSignal and failure is None:
            try:
                self._signalRecordingEnded()
            except Exception as e:
                failure = failure or e

        if failure is not None:
            raise failure

    def abortRecording(self, emitSignal=True, wait=True):
        """ Aborts the current recording, DISCARDING partial output on disk.

        Like endRecording, but the streaming writer deletes the partial
        file(s)/store(s) instead of finalizing them. The acquisition loop stops
        at its next iteration and the writer discards any queued frames.

        Note: this stops the recording sink only. For scan-driven detectors the
        scan hardware source is not stopped here - see the source-abort design
        in docs/recording_dataflow_plan.md. """

        failure = None
        if self.__record:
            self.__logger.info('Aborting recording')
        self.__abort = True
        self.__record = False
        self.__activeDetectorNames = ()

        if self.__recordingWorker is not None:
            try:
                self.__recordingWorker.requestWriterAbort()
            except Exception as e:
                failure = e
        if self.__thread is not None:
            try:
                self.__quitRecordingThread()
            except Exception as e:
                failure = failure or e
        if wait and self.__thread is not None:
            try:
                self.__waitForRecordingThread()
            except Exception as e:
                failure = failure or e
        if emitSignal and failure is None:
            try:
                self._signalRecordingEnded()
            except Exception as e:
                failure = failure or e

        if failure is not None:
            raise failure

    def _signalRecordingEnded(self, generation=None, *, emitLegacy=True):
        """Publish one identity-carrying successful terminal.

        ``sigRecordingEndedDetailed`` is the manager's writer-finalization
        boundary.  Scan-mode workers suppress the legacy UI signal because the
        controller also waits for scan completion, but they must still publish
        this detailed terminal after their writer has drained.
        """
        with self.__recordingSignalLock:
            if generation is None:
                generation = self.__recordingGeneration
            if generation != self.__recordingGeneration or generation <= 0:
                return False
            if self.__endSignalEmitted:
                return False
            self.__endSignalEmitted = True
        self.sigRecordingEndedDetailed.emit(int(generation))
        if emitLegacy:
            self.sigRecordingEnded.emit()
        return True

    def registerPayloadLocators(self, generation, locators) -> None:
        """Record where a session's files were actually written.

        Called by the worker once ``getSaveFilePath`` has resolved them.
        Nothing is complete at this point — the paths exist, the data is not
        yet finalised — which is why :meth:`payloadLocators` reports
        ``complete`` separately.
        """
        if generation is None:
            return
        with self.__recordingSignalLock:
            self.__payloadLocators[int(generation)] = dict(locators)

    def payloadLocators(self, generation) -> Dict[str, 'PayloadLocator']:
        """Where a session's data ended up, per detector.

        Authoritative only after the session has finalised: before that the
        entries exist but report ``complete=False``. A caller writing these
        into a manifest must wait for the terminal, not the dispatch — pointing
        a manifest at a file whose writer never drained is the same mistake as
        guessing its name.
        """
        with self.__recordingSignalLock:
            found = self.__payloadLocators.get(int(generation), {})
            complete = (int(generation) != self.__recordingGeneration
                        or self.__endSignalEmitted)
            failed = self.__failureSignalEmitted
        complete = bool(complete and not failed)
        return {
            name: PayloadLocator(
                path=locator.path, detector=locator.detector,
                group=locator.group, axes=locator.axes,
                stored_axes=locator.stored_axes, shape=locator.shape,
                stored_shape=locator.stored_shape,
                generation=int(generation), complete=complete,
            )
            for name, locator in found.items()
        }

    def reportRecordingFailure(self, error, generation=None, kind=None):
        """Publish an externally detected failure for the active session."""
        return self._signalRecordingFailed(error, generation, kind)

    def _signalRecordingStarted(self, generation=None):
        """Publish start with identity while preserving the legacy signal."""
        if generation is None:
            with self.__recordingSignalLock:
                generation = self.__recordingGeneration
        self.sigRecordingStartedDetailed.emit(int(generation))
        self.sigRecordingStarted.emit()

    def _signalAcquisitionStarted(self):
        """Called by the worker once detector acquisition has started (armed)."""
        self.__acqStartedEvent.set()

    def _signalAcquisitionFailed(self):
        """Unblock scan-start waiters when recording setup cannot be armed."""
        self.__acqStartFailed = True
        self.__acqStartedEvent.set()

    def _signalRecordingFailed(self, error, generation=None,
                               kind=None):
        """Publish one terminal failure without masquerading as completion.

        ``kind`` is a :class:`FailureKind`; omitting it means UNKNOWN, which
        is deliberately *not* recoverable. A caller that continues past
        failures must be told what it is continuing past.
        """
        message = str(error) or type(error).__name__
        kind = kind or getattr(error, 'failureKind', None) or FailureKind.UNKNOWN
        with self.__recordingSignalLock:
            if generation is None:
                generation = self.__recordingGeneration
            if generation != self.__recordingGeneration:
                return False
            if self.__failureSignalEmitted or self.__endSignalEmitted:
                return False
            self.__lastRecordingError = error
            self.__failureSignalEmitted = True
            # Failure replaces ordinary completion for this session. This also
            # makes re-entrant UI cleanup unable to emit a misleading "ended".
            self.__endSignalEmitted = True
        self._signalAcquisitionFailed()
        self.sigRecordingFailedDetailed.emit(message, int(generation))
        self.sigRecordingFailedTyped.emit(message, int(generation), kind.value)
        self.sigRecordingFailed.emit(message)
        return True

    @property
    def lastRecordingError(self):
        with self.__recordingSignalLock:
            return self.__lastRecordingError

    @property
    def recordingGeneration(self):
        """Identity of the most recently started recording session."""
        with self.__recordingSignalLock:
            return self.__recordingGeneration

    def markScanCompleted(self, generation=None):
        """Start the final-frame watchdog for one exact scan recording.

        APD/PMT/TimeTagger managers integrate the complete scan and cannot
        produce frame progress while it is still running. RecordingController
        calls this at the owner-scoped scan terminal so their normal stall
        timeout measures delayed final-frame publication rather than total
        scan duration.
        """
        with self.__recordingSignalLock:
            if generation is None:
                generation = self.__recordingGeneration
            if (
                generation != self.__recordingGeneration
                or generation <= 0
                or not self.__record
            ):
                return False
            if self.__scanCompletionGeneration == generation:
                return False
            self.__scanCompletionGeneration = generation
            self.__scanCompletionTime = time.time()
            return True

    def markScanStarted(self, scanInfoDict, generation=None):
        """Set an expected end time for a scan-driven recording watchdog.

        The generated sample count already includes line steps, flyback and
        higher scan axes. Adding the ordinary stall timeout after that expected
        duration preserves detection of a missing NI-DAQ trigger without
        mistaking a long, healthy scan for a detector stall.
        """
        try:
            duration = (
                float(scanInfoDict['scan_samples_total'])
                * float(scanInfoDict['scan_time_step'])
            )
        except (KeyError, TypeError, ValueError):
            return False
        if not np.isfinite(duration) or duration < 0:
            return False

        with self.__recordingSignalLock:
            if generation is None:
                generation = self.__recordingGeneration
            recMode = getattr(self.__recordingWorker, 'recMode', None)
            if (
                generation != self.__recordingGeneration
                or generation <= 0
                or not self.__record
                or recMode not in (RecMode.ScanOnce, RecMode.ScanLapse)
            ):
                return False
            self.__scanExpectedCompletionGeneration = generation
            self.__scanExpectedCompletionTime = time.time() + duration
            return True

    def scanCompletionTime(self, generation):
        """Return observed or expected scan completion time for ``generation``."""
        with self.__recordingSignalLock:
            if self.__scanCompletionGeneration == generation:
                return self.__scanCompletionTime
            if self.__scanExpectedCompletionGeneration == generation:
                return self.__scanExpectedCompletionTime
            return None

    def waitForAcquisitionStarted(self, timeout=None):
        """Block until the recording worker is ready for incoming frames, or
        until timeout.

        Returns True once acquisition detectors are armed and the recording
        stream/chunk consumer is ready. Scan-driven recordings use this to gate
        scan TTL output on actual recording readiness instead of a fixed sleep.
        Safe to call from the GUI thread: waits on a threading.Event set
        directly by the worker thread, so it does not depend on the Qt event
        loop.
        """
        signaled = self.__acqStartedEvent.wait(timeout)
        return signaled and not self.__acqStartFailed

    def snap(self, detectorNames, savename, saveMode, saveFormat, attrs):
        """ Saves an image with the specified detectors to a file
        with the specified name prefix, save mode, file format and attributes
        to save to the capture per detector. """
        # Iterators/generators must be materialized once: acquire(), the frame
        # loop and filename generation all consume this selection.
        detectorNames = tuple(detectorNames)
        # Lease exactly what is being snapped. This used to arm every
        # forAcquisition detector for a subset snap, spinning up hardware
        # nobody asked for.
        acqHandle = self.__detectorsManager.acquire(detectorNames,
                                                    LeasePurpose.SNAP)

        images = {}
        try:
            # Acquire data
            for detectorName in detectorNames:
                try:
                    images[detectorName] = (
                        self.__detectorsManager[detectorName]
                        .getLatestFrameShared(is_save=True)
                    )
                except RawFrameUnavailableError as error:
                    # A scan-driven detector mid-scan has nothing complete to
                    # offer. Saving the buffer it is still filling would look
                    # like data, so this detector is left out of the snapshot
                    # and said so, rather than the whole snap failing.
                    self.__logger.warning('%s', error)
                    continue
                image = images[detectorName]

            if saveFormat:
                storer = self.__storerMap[saveFormat]

                if saveMode == SaveMode.Disk or saveMode == SaveMode.DiskAndRAM:
                    savename = self.getSaveSnapName(savename, saveFormat, detectorNames)
                    # Save images to disk
                    store = storer(savename, self.__detectorsManager)
                    store.omeMeta = {
                        det: self.buildOmeMeta(
                            det, _ome.MODE_SNAP,
                            1 if np.asarray(img).ndim == 2 else int(np.asarray(img).shape[0]))
                        for det, img in images.items()
                    }
                    store.snap(images, attrs)

                if saveMode == SaveMode.RAM or saveMode == SaveMode.DiskAndRAM:
                    for channel, image in images.items():
                        name = os.path.basename(f'{savename}_{channel}')
                        self.sigMemorySnapAvailable.emit(name, image, savename, saveMode == SaveMode.DiskAndRAM)

        finally:
            self.__detectorsManager.release(acqHandle)
        if saveMode == SaveMode.Numpy:
            return images

    def snapImagePrev(self, detectorName, savename, saveFormat, image, attrs,
                      stagePositionUm=None, zStepUm=None):
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
            stagePositionUm: Optional ``(x, y, z)`` stage position in µm,
                written as OME ``Plane/@PositionX|Y|Z``. This is what lets a
                set of separately saved images (tiling, for one) be
                reassembled into a mosaic by any OME-aware reader.

        Returns:
            The list of paths actually written. Callers that index the saved
            files (a tiling manifest, say) must use these rather than guess:
            the storer appends the detector name and its own extension, and
            de-duplicates the basename against existing files.
        """
        return self.snapImagesPrev({detectorName: image}, savename, saveFormat,
                                   attrs, stagePositionUm=stagePositionUm,
                                   zStepUm=zStepUm)

    def snapImagesPrev(self, images, savename, saveFormat, attrs,
                       stagePositionUm=None, zStepUm=None):
        """Save several already-captured images that share one stage position.

        The multi-detector form of :meth:`snapImagePrev`. Every image was
        captured at the same place at the same time — a tiling run's tile,
        across whichever detectors the operator selected — so they share the
        stage position written into their OME metadata, which is what lets them
        be reassembled into one mosaic per detector afterwards.

        The images are passed in rather than re-read: the caller had to capture
        them under its own timing constraints (a stage that must not move, a
        scan that must have finished), and re-reading here would hand back
        whatever arrived since.

        Args:
            images: ``{detectorName: array}``, each ``(Y, X)`` or ``(..., Y, X)``.
            zStepUm: Optional Z spacing, applied to every image. A caller with
                differing spacings per detector must call once per detector.

        Returns:
            The paths actually written, in the order the detectors were given.
        """
        if not images:
            return []

        detectorNames = list(images)
        storer = self.__storerMap[saveFormat]
        savename = self.getSaveSnapName(savename, saveFormat, detectorNames)
        savePaths = self._snapSavePaths(savename, saveFormat, detectorNames)
        store = storer(savename, self.__detectorsManager)

        omeMeta = {}
        for detectorName, image in images.items():
            array = np.asarray(image)
            nf = 1 if array.ndim == 2 else int(array.shape[0])
            # A caller that knows its planes are a Z stack says so with
            # zStepUm. Without it the leading axis is labelled T, which would
            # mislabel every plane of a 3D tile and silently corrupt its
            # calibration.
            if zStepUm:
                omeMeta[detectorName] = self.buildOmeMeta(
                    detectorName, _ome.MODE_SCAN, nf, scanDims=(1, 1, nf),
                    scanStepSizes=(0, 0, zStepUm),
                    stagePositionUm=stagePositionUm)
            else:
                omeMeta[detectorName] = self.buildOmeMeta(
                    detectorName, _ome.MODE_SNAP, nf,
                    stagePositionUm=stagePositionUm)

        store.omeMeta = omeMeta
        storedShapes = store.snap(dict(images), attrs)

        # What the container actually wrote, which the caller cannot infer: an
        # HDF5 snapshot of a 2-D image gains a leading frame axis, a TIFF does
        # not. Reported per detector so a manifest can describe the file rather
        # than the array that went in.
        self.__lastSnapStoredShapes = {
            name: tuple(shape) for name, shape in (storedShapes or {}).items()
        }
        return savePaths

    def lastSnapStoredShapes(self) -> Dict[str, Tuple[int, ...]]:
        """Stored shapes from the most recent :meth:`snapImagesPrev`.

        Empty when the storer did not report them, in which case a reader must
        reconcile against the file itself rather than trust a guess.
        """
        return dict(getattr(self, '_RecordingManager__lastSnapStoredShapes', {}))

    @staticmethod
    def _parameter_seconds(param) -> Optional[float]:
        try:
            value = float(getattr(param, 'value'))
        except Exception:
            return None
        unit = str(getattr(param, 'valueUnits', '') or '').strip().lower()
        if unit in ('s', 'sec', 'second', 'seconds'):
            return value
        if unit in ('ms', 'millisecond', 'milliseconds'):
            return value / 1000.0
        if unit in ('us', 'µs', 'μs', 'microsecond', 'microseconds'):
            return value / 1_000_000.0
        return None

    def _detectorFrameIntervalSeconds(self, det) -> float:
        params = getattr(det, 'parameters', {}) or {}
        for key in ('Internal frame interval', 'Frame interval', 'Real exposure time',
                    'Set exposure time', 'Exposure', 'exposure'):
            if key in params:
                seconds = self._parameter_seconds(params[key])
                if seconds is not None and seconds > 0:
                    return seconds
        try:
            if hasattr(det, 'getExposureTime'):
                value = float(det.getExposureTime())
                if value > 0:
                    return value
        except Exception:
            pass
        return 1.0

    @staticmethod
    def _scanStep(scanStepSizes, axis) -> float:
        """One axis of ``getScanStepSizes()`` as a magnitude, 0 when unusable."""
        if scanStepSizes is None:
            return 0.0
        try:
            return abs(float(scanStepSizes[axis]))
        except (IndexError, KeyError, TypeError, ValueError):
            return 0.0

    def buildOmeMeta(self, detectorName, mode, nFrames, scanDims=None,
                     scanStepSizes=None, frameIntervalS=None, annotations=None,
                     stagePositionUm=None):
        """Build the shared :class:`OmeImageMeta` for a detector from recording
        context. ``mode`` is a normalized recording mode (see recording_metadata),
        ``scanDims`` is ``(Nx, Ny, Nz)`` from the scan controller (for z-stack
        axis labeling), and ``scanStepSizes`` is ``(x, y, z)``. Scan steps are
        authoritative spatial calibration for scan-driven detectors."""
        det = self.__detectorsManager[detectorName]
        pix = list(det.pixelSizeUm)
        py = pix[1] if len(pix) > 1 else 1.0
        px = pix[2] if len(pix) > 2 else py
        z_step = pix[0] if (pix and pix[0]) else 1.0
        # An inactive axis reports a 0 step (getScanStepSizes pads to 3), and 0
        # is not a pixel size -- it would emit PhysicalSize=0 and make every
        # downstream consumer that divides by the scale blow up. So a 0 step
        # always leaves the detector's own value in place.
        xStep, yStep, zStep = (self._scanStep(scanStepSizes, axis)
                               for axis in range(3))
        if getattr(det, 'isScanDriven', False):
            # Scan geometry is authoritative for point detectors: their pixel
            # size *is* the scan step. The detector's own cache is only written
            # once the scan is built, which is after this metadata is
            # snapshotted, so it still describes the previous scan (or the
            # startup default) at this point.
            px = xStep or px
            py = yStep or py
        z_step = zStep or z_step
        t_interval = (float(frameIntervalS) if frameIntervalS is not None
                      else self._detectorFrameIntervalSeconds(det))
        return _ome.build_ome_image_meta(
            detectorName, mode, nFrames,
            pixel_size_yx_um=(py, px), scan_dims=scanDims,
            z_step_um=z_step, t_interval_s=t_interval,
            dtype=det.dtype, annotations=annotations or {},
            stage_position_um=stagePositionUm)

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

    def getSaveSnapName(self, savename, saveFormat, detectorNames):
        """Return a snapshot basename whose final output path(s) do not exist."""
        detectorNames = tuple(detectorNames)
        newSavename = savename
        numExisting = 0

        def existingSnapPath(saveNameToCheck):
            for pathToCheck in self._snapSavePaths(saveNameToCheck, saveFormat, detectorNames):
                if os.path.exists(pathToCheck):
                    return True
            return False

        while existingSnapPath(newSavename):
            numExisting += 1
            newSavename = f'{savename}_{numExisting}'
        return newSavename

    @staticmethod
    def _snapSavePaths(savename, saveFormat, detectorNames):
        if saveFormat == SaveFormat.ZARR:
            return [f'{savename}.zarr']
        if saveFormat == SaveFormat.HDF5:
            return [f'{savename}_{detectorName}.h5' for detectorName in detectorNames]
        if saveFormat == SaveFormat.TIFF:
            return [f'{savename}_{detectorName}.ome.tiff' for detectorName in detectorNames]
        raise ValueError(f'Unsupported save format: {saveFormat}')


class WriterThread(threading.Thread):
    """Dedicated writer thread that owns all storer I/O operations.
    
    Runs compression and disk writes off the acquisition thread, fed by a bounded
    queue from the acquisition loop. Batches frames per detector for efficiency.
    """
    def __init__(self, storer, fileDests, detectorNames, shapes, attrs,
                 singleMultiDetectorFile, singleLapseFile, saveMode, filePaths,
                 recordingManager, *, recordingGeneration=None,
                 recordingMode=_ome.MODE_TIMELAPSE, scanDims=None,
                 scanDrivenDetectors=None):
        # Normal shutdown still joins with a deadline and reports a failure.
        # The daemon fail-safe prevents an uninterruptible storage-backend call
        # from keeping the whole application process alive forever.
        super().__init__(daemon=True, name='RecordingWriterThread')
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
        self._recordingGeneration = recordingGeneration
        self._recordingMode = recordingMode
        self._scanDims = scanDims
        self._scanDrivenDetectors = dict(scanDrivenDetectors or {})
        
        # Bounded queue for backpressure (put() blocks when full)
        self._queue = queue.Queue(maxsize=WRITER_QUEUE_MAXSIZE)
        
        # Per-detector batching buffers
        self._batches = {detectorName: [] for detectorName in detectorNames}
        self._batch_frame_counts = {detectorName: 0 for detectorName in detectorNames}
        
        # Track total frames written per detector
        self._currentFrames = {detectorName: 0 for detectorName in detectorNames}
        # Frames the producer discarded beyond the plan, reported at finalize.
        self._discardedFrames: Dict[str, int] = {}
        
        # Handshake for openStream completion (success or exception)
        self._opened_event = threading.Event()
        self._open_exception = None
        # Exception raised in the write loop (surfaced to the producer so it
        # never blocks forever on a dead writer; see enqueue_frames).
        self._write_exception = None
        # Set by abort(): queued frames are discarded and the partial output is
        # deleted (abortStream) instead of being flushed and finalized.
        self._abort_event = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_lock = threading.Lock()
        self._stream_cleanup_done = threading.Event()

    def _abort_partial_stream(self):
        """Best-effort partial-output cleanup, owned by the writer thread."""
        if self._stream_cleanup_done.is_set():
            return
        try:
            self._storer.abortStream(
                self._filePaths, self._fileDests, self._saveMode
            )
        except Exception as cleanupError:
            logger.error(
                f'Recording writer failed to clean up partial output: '
                f'{cleanupError}',
                exc_info=True,
            )
        finally:
            self._stream_cleanup_done.set()

    def _raise_if_failed(self):
        if self._open_exception is not None:
            raise self._open_exception
        if self._write_exception is not None:
            raise self._write_exception

    @staticmethod
    def _logical_axes(mode, n_frames, scan_dims, logical_rank, scan_driven):
        """Name the dimensions the acquisition produced, not writer wrappers."""
        if logical_rank < 2:
            raise ValueError(
                f'A recorded image needs at least Y/X, got rank {logical_rank}'
            )
        base = ''.join(
            axis.upper()
            for axis in _ome.axes_for_recording(mode, n_frames, scan_dims)
        )
        target_leading = logical_rank - 2
        leading = list(base[:-2])
        # A scan-driven detector returns one assembled raw frame. Extra axes in
        # that frame are channels first, then Z. For an actual sequence of
        # assembled frames, the writer's outer dimension is logical time.
        candidates = (
            (('T', 'C', 'Z') if scan_driven else ('T', 'Z', 'C'))
            if n_frames > 1
            else (('C', 'Z') if scan_driven else ('Z', 'C'))
        )
        for axis in candidates:
            if len(leading) >= target_leading:
                break
            if axis not in leading:
                leading.append(axis)
        if len(leading) != target_leading:
            raise ValueError(
                f'Cannot describe rank-{logical_rank} recording payload with '
                'the supported T/C/Z/Y/X axis model'
            )
        order = {'T': 0, 'C': 1, 'Z': 2}
        leading.sort(key=order.__getitem__)
        return ''.join(leading) + 'YX'

    def _final_payload_locators(self, payload_info):
        locators = {}
        for detectorName, info in payload_info.items():
            if info is None:
                # Preserve the resolved path for compatibility. There is no
                # exact group/shape to publish when a detector wrote no array.
                locators[detectorName] = PayloadLocator(
                    path=str(self._filePaths[detectorName]),
                    detector=detectorName,
                )
                continue
            n_frames = int(self._currentFrames.get(detectorName, 0))
            stored_shape = tuple(int(size) for size in info.stored_shape)
            remove_frame_wrapper = bool(
                info.frame_axis_stored and n_frames == 1
            )
            logical_shape = (
                stored_shape[1:] if remove_frame_wrapper else stored_shape
            )
            axes = self._logical_axes(
                self._recordingMode,
                n_frames,
                self._scanDims,
                len(logical_shape),
                self._scanDrivenDetectors.get(detectorName, False),
            )
            stored_axes = f'T{axes}' if remove_frame_wrapper else axes
            if len(set(stored_axes)) != len(stored_axes):
                raise ValueError(
                    f'Payload for {detectorName!r} needs a logical T axis '
                    'inside a singleton writer frame; the recording model '
                    'cannot name both dimensions without ambiguity'
                )
            locators[detectorName] = PayloadLocator(
                path=str(self._filePaths[detectorName]),
                detector=detectorName,
                group=info.group,
                axes=axes,
                stored_axes=stored_axes,
                shape=logical_shape,
                stored_shape=stored_shape,
            )
        return locators

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
            # openStream may have created a subset of its files/handles before
            # failing. Clean that partial session in the writer thread, which
            # is the only thread allowed to own those backend objects.
            # Store exception to re-raise on acquisition thread
            self._open_exception = e
            self._abort_partial_stream()
            self._opened_event.set()
            return
        
        # Signal that openStream succeeded
        self._opened_event.set()
        
        # Main write loop: process frames until sentinel
        try:
            while True:
                item = self._queue.get()
                
                if item is None:
                    # Sentinel. On abort, discard partial output; otherwise flush
                    # remaining batches and finalize normally.
                    if self._abort_event.is_set():
                        self._storer.abortStream(
                            self._filePaths, self._fileDests, self._saveMode
                        )
                        self._stream_cleanup_done.set()
                    else:
                        self._flush_all_batches()
                        payloadInfo = {
                            detectorName: self._storer.streamPayloadInfo(
                                detectorName, self._currentFrames
                            )
                            if hasattr(self._storer, 'streamPayloadInfo')
                            else None
                            for detectorName in self._detectorNames
                        }
                        finalLocators = self._final_payload_locators(payloadInfo)
                        # finish() can time out while a slow backend is
                        # flushing, after which the owner escalates to
                        # abort(). Re-check at both blocking boundaries so a
                        # late abort cannot leave a finalized file behind
                        # while the session reports failure/partial cleanup.
                        if self._abort_event.is_set():
                            self._storer.abortStream(
                                self._filePaths,
                                self._fileDests,
                                self._saveMode,
                            )
                        else:
                            noteDiscarded = getattr(self._storer, 'noteDiscardedFrames', None)
                            if callable(noteDiscarded):
                                noteDiscarded(dict(self._discardedFrames))
                            self._storer.finalizeStream(
                                self._currentFrames,
                                self._filePaths,
                                self._recordingManager,
                                self._saveMode,
                            )
                            if self._abort_event.is_set():
                                self._storer.abortStream(
                                    self._filePaths,
                                    self._fileDests,
                                    self._saveMode,
                                )
                            elif self._recordingManager is not None:
                                self._recordingManager.registerPayloadLocators(
                                    self._recordingGeneration,
                                    finalLocators,
                                )
                        self._stream_cleanup_done.set()
                    break

                # Discard frames once an abort has been requested.
                if self._abort_event.is_set():
                    continue

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
            # A write/finalize failure is not a successful recording. Remove
            # the partial stream in this writer thread so callers cannot
            # mistake a truncated output for a completed acquisition.
            self._abort_partial_stream()
    
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
    
    def wait_for_open(self, timeout=WRITER_OPEN_TIMEOUT_S):
        """Wait for openStream handshake and re-raise any exception.
        
        Returns:
            None if openStream succeeded
            
        Raises:
            Any exception that occurred during openStream
        """
        if not self._opened_event.wait(timeout):
            raise TimeoutError(
                f'Recording writer did not open within {timeout:g} seconds'
            )
        self._raise_if_failed()
    
    def enqueue_frames(self, detectorName, frames):
        """Enqueue frames for the writer thread.

        Blocks while the queue is full (intended backpressure), but NEVER
        forever: if the writer thread has died, raises so the failure surfaces
        on the acquisition thread instead of deadlocking the producer.

        A long block here is invisible in a log otherwise: the acquisition loop
        simply stops draining, and what gets reported is the *consequence* --
        a detector's chunk consumer overflowing, filled meanwhile by the
        live-view poller. Say that the producer stalled, and for how long, so
        the writer is a candidate rather than a deduction.
        """
        blockedSince = None
        stallReported = False
        while True:
            self._raise_if_failed()
            if not self.is_alive():
                raise RuntimeError(
                    'RecordingWriterThread is not running; frames cannot be '
                    'enqueued'
                )
            try:
                self._queue.put((detectorName, frames), timeout=0.1)
                # Cover the race where the writer failed while this put was
                # completing. finish() performs the same check for a failure
                # that occurs after the producer's final enqueue.
                self._raise_if_failed()
                if not self.is_alive():
                    raise RuntimeError(
                        'RecordingWriterThread stopped while frames were being '
                        'enqueued'
                    )
                if stallReported:
                    logger.warning(
                        f'Recording producer resumed for "{detectorName}" '
                        f'after {time.time() - blockedSince:.1f}s blocked on '
                        f'the writer queue.'
                    )
                return
            except queue.Full:
                self._raise_if_failed()
                if not self.is_alive():
                    raise RuntimeError(
                        'RecordingWriterThread died before frames could be '
                        'enqueued; recording aborted'
                    ) from self._write_exception
                # Writer still alive and draining - keep applying backpressure.
                if blockedSince is None:
                    blockedSince = time.time()
                elif (not stallReported
                        and time.time() - blockedSince >= PRODUCER_STALL_WARN_S):
                    stallReported = True
                    logger.warning(
                        f'Recording producer has been blocked for '
                        f'{PRODUCER_STALL_WARN_S:g}s enqueuing frames for '
                        f'"{detectorName}": the writer is not draining fast '
                        f'enough. Frames arriving meanwhile are held in the '
                        f'detector\'s chunk queue, which is bounded.'
                    )
                continue
    
    def noteDiscardedFrames(self, discarded: Dict[str, int]) -> None:
        """Record how many frames the producer dropped beyond the plan."""
        self._discardedFrames = {k: int(v) for k, v in (discarded or {}).items() if int(v) > 0}

    def finish(self):
        """Signal end of recording and wait for writer thread to complete.
        
        Uses a timeout to avoid deadlock if queue is full. The writer thread
        will drain the queue, making space for the sentinel.
        """
        self._request_stop(abort=False)

    def abort(self):
        """Abort: discard queued frames and partial output, then stop the writer.

        Unlike finish(), does NOT flush or finalize - the storer deletes the
        partial file(s)/store(s) via abortStream. Drains the queue aggressively
        so the sentinel is deliverable even under full-queue backpressure (the
        discarded frames are not needed).
        """
        self._request_stop(abort=True)

    def _request_stop(self, abort):
        if abort:
            self._abort_event.set()
        stopped_unexpectedly = False
        with self._stop_lock:
            if not self._stop_requested.is_set():
                self._stop_requested.set()
                if not self.is_alive():
                    stopped_unexpectedly = (
                        self._open_exception is None
                        and self._write_exception is None
                    )
                else:
                    while True:
                        try:
                            self._queue.put(None, timeout=0.1)
                            break
                        except queue.Full:
                            if not self.is_alive():
                                stopped_unexpectedly = (
                                    self._open_exception is None
                                    and self._write_exception is None
                                )
                                break
                            if abort:
                                # Discard a queued item to make room for the
                                # sentinel; abort mode deliberately does not
                                # preserve partial output.
                                try:
                                    self._queue.get_nowait()
                                except queue.Empty:
                                    pass
                            # finish() keeps waiting for the writer to drain so
                            # all queued frames are finalized.

        # join() itself rejects a thread that was never started. Surface that
        # as a lifecycle error rather than silently claiming success.
        if self.ident is None and not self.is_alive():
            self._raise_if_failed()
            raise RuntimeError('RecordingWriterThread was never started')

        self.join(timeout=30.0)
        if self.is_alive():
            raise TimeoutError('RecordingWriterThread did not finish within 30 seconds')
        self._raise_if_failed()
        if stopped_unexpectedly:
            raise RuntimeError(
                'RecordingWriterThread stopped before finalization completed'
            )


class RecordingWorker(Worker):
    def __init__(self, recordingManager):
        super().__init__()
        self.__logger = initLogger(self)
        self.__recordingManager = recordingManager
        self.__logger = initLogger(self)
        self._writerThread = None

    def requestWriterAbort(self):
        writerThread = self._writerThread
        if writerThread is not None:
            writerThread.abort()

    def _clearWriterReferenceIfStopped(self, writerThread):
        """Forget a writer only after its thread is proven terminated."""
        try:
            stopped = not writerThread.is_alive()
        except Exception:
            stopped = False
        if stopped and self._writerThread is writerThread:
            self._writerThread = None
        return stopped

    def run(self):
        detectorsManager = self.__recordingManager.detectorsManager
        # Lease exactly the detectors being recorded, not every
        # forAcquisition detector. Scan-driven participants are additionally
        # held by the scan's own SCAN lease, so a recording that names one
        # keeps it armed across the whole recording rather than only for the
        # scan iteration.
        detectorNames = list(getattr(self, 'detectorNames', None) or [])
        acqHandle = None
        try:
            # Keep acquisition inside the guarded lifecycle. Previously an
            # acquire failure escaped before readiness was signalled and before
            # RecordingManager.record was reset, wedging scan-start waiters and
            # the recording UI.
            if detectorNames:
                acqHandle = detectorsManager.acquire(
                    detectorNames, LeasePurpose.RECORDING
                )
            self._record()

        except Exception as e:
            self.__logger.error(f'Recording failed: {e}', exc_info=True)
            signalFailure = getattr(
                self.__recordingManager, '_signalRecordingFailed', None
            )
            if callable(signalFailure):
                signalFailure(
                    e, getattr(self, 'recordingGeneration', None)
                )

            # If setup failed before _record announced readiness, wake any scan
            # controller waiting to emit TTL and make that wait return False.
            if not self.__recordingManager.waitForAcquisitionStarted(timeout=0):
                self.__recordingManager._signalAcquisitionFailed()

            # _record owns normal writer finalization once it enters its main
            # loop. Failures before that loop (file setup, writer open, mode
            # validation) leave RecordingManager.record True and need explicit
            # abort/manager cleanup here.
            if self.__recordingManager.record:
                writerThread = self._writerThread
                if writerThread is not None:
                    try:
                        writerThread.abort()
                    except Exception as cleanupError:
                        self.__logger.error(
                            f'Failed to abort recording writer: {cleanupError}',
                            exc_info=True,
                        )
                    finally:
                        self._clearWriterReferenceIfStopped(writerThread)
                try:
                    # This callback is running on the recording worker's own
                    # QThread. Never wait for that same thread, and do not let
                    # a detector-flush or signal-slot failure bypass the lease
                    # release in the outer finally.
                    self.__recordingManager.endRecording(
                        emitSignal=False, wait=False
                    )
                except Exception as cleanupError:
                    self.__logger.error(
                        f'Failed to finish recording manager cleanup: '
                        f'{cleanupError}',
                        exc_info=True,
                    )

        finally:
            if acqHandle is not None:
                try:
                    detectorsManager.release(acqHandle)
                except Exception as e:
                    self.__logger.error(
                        f'Failed to release recording detector lease: {e}',
                        exc_info=True,
                    )
    
    def _getFileDests(self):
        """Prepare file destinations and paths for streaming."""
        singleMultiDetectorFile = self.singleMultiDetectorFile
        singleLapseFile = (
            self.recMode in (RecMode.ScanLapse, RecMode.CameraLapse)
            and self.singleLapseFile
        )
        
        fileDests = {}
        filePaths = {}
        
        if self.saveFormat == SaveFormat.TIFF:
            extension = 'ome.tiff'
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
        
        # Publish where this session's data is going. The paths are resolved
        # here and nowhere else -- getSaveFilePath de-duplicates against what
        # already exists, so a caller that guessed would name a file that is
        # not the one being written.
        try:
            self.__recordingManager.registerPayloadLocators(
                getattr(self, 'recordingGeneration', None),
                {
                    name: PayloadLocator(path=str(path), detector=name)
                    for name, path in filePaths.items()
                },
            )
        except Exception:
            # Locators are a convenience for callers that index the output;
            # failing to publish them must not fail the recording itself.
            pass

        return fileDests, filePaths
    
    def _augment_attrs_with_recording_metadata(
        self,
        attrs: Dict[str, Dict[str, Any]],
        expected_frames: Dict[str, int] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Augment attrs with acquisition and live-source recording metadata.
        
        Args:
            attrs: Dict mapping detector name to flat metadata dict.
            expected_frames: Optional total frame count per detector.
        
        Returns:
            New dict with augmented metadata for each detector.
        """
        augmented = {}
        acquisition_start = datetime.now(timezone.utc).isoformat()
        
        for detectorName, detector_attrs in attrs.items():
            # Copy existing attrs
            new_attrs = dict(detector_attrs) if detector_attrs else {}
            
            # Add software version
            new_attrs['acquisition:software_version'] = imswitch.__version__
            
            # Add acquisition start timestamp
            new_attrs['acquisition:start_time'] = acquisition_start
            
            # Add exposure time from detector if available
            try:
                detector = self.__recordingManager.detectorsManager[detectorName]
                # Different detectors may expose this differently; try common attributes
                if hasattr(detector, 'getExposureTime'):
                    exposure_ms = detector.getExposureTime()
                    new_attrs['acquisition:exposure_time_ms'] = str(exposure_ms)
                elif hasattr(detector, 'exposure'):
                    exposure_ms = detector.exposure
                    new_attrs['acquisition:exposure_time_ms'] = str(exposure_ms)
            except Exception as e:
                logger.debug(
                    "Could not get exposure time for detector %s: %s", 
                    detectorName, e
                )

            new_attrs['recording:detector_name'] = detectorName
            new_attrs['recording:source_format'] = self.saveFormat.name
            if expected_frames is not None and detectorName in expected_frames:
                frame_count = int(expected_frames[detectorName])
                new_attrs['recording:expected_frames'] = frame_count
                new_attrs["recording:planned_frames"] = frame_count
                # No generic multi-stack boundary exists yet. For single-stack
                # recording modes, the expected frame count is the stack size.
                new_attrs['recording:frames_per_stack'] = frame_count
            new_attrs.setdefault("recording:planned_partitions", 1)

            layout_value = getattr(self, "acquisitionLayouts", {}).get(detectorName)
            if layout_value is not None:
                if isinstance(layout_value, AcquisitionLayout):
                    encoded_layout = encode_acquisition_layout(layout_value)
                elif isinstance(layout_value, (str, bytes, bytearray)):
                    encoded_layout = encode_acquisition_layout(
                        decode_acquisition_layout(layout_value)
                    )
                else:
                    raise TypeError(
                        f"Acquisition layout for {detectorName!r} must be an "
                        "AcquisitionLayout or encoded JSON"
                    )
                new_attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
                new_attrs["AcquisitionLayout:json"] = encoded_layout
            
            # Add lapse metadata
            new_attrs['recording:num_timepoints'] = int(self.recLapseTotal or 1)
            new_attrs['recording:lapse_index'] = int(self.recLapseIndex or 0)
            new_attrs['recording:single_lapse_file'] = bool(self.singleLapseFile)
            recLapseIntervalS = getattr(
                self, 'recLapseIntervalS', None
            )
            if recLapseIntervalS is not None:
                new_attrs['recording:lapse_interval_s'] = float(
                    recLapseIntervalS
                )
            recLapseScheduledTime = getattr(
                self, 'recLapseScheduledTime', None
            )
            if recLapseScheduledTime:
                new_attrs['recording:planned_start_time'] = str(
                    recLapseScheduledTime
                )
            
            augmented[detectorName] = new_attrs
        
        return augmented

    def _isScanDrivenDetector(self, detectorName) -> bool:
        try:
            detector = self.__recordingManager.detectorsManager[detectorName]
        except Exception:
            return False
        return bool(getattr(detector, 'isScanDriven', False))

    def _expectedFramesFor(self, detectorName, recFrames, numCamTTL) -> int:
        """How many frames this detector produces for the recording session.

        The two detector families answer this completely differently, and
        treating them alike is what made scan recordings hang or truncate:

        - A **free-running/trigger-driven camera** emits one frame per scan
          position (per camera TTL pulse), so it yields
          ``recFrames * numCamTTL``.
        - A **scan-driven** detector (APD, PMT, TimeTagger) integrates the
          whole scan into a single assembled image and emits exactly ONE
          frame per scan, whatever the position count. Expecting one frame
          per position means waiting for frames that are never produced.

        In the lapse modes each session covers one scan, so the scan-driven
        answer stays 1 there too; the per-timepoint loop supplies the
        repetition.
        """
        layout = (self.__dict__.get('acquisitionLayouts') or {}).get(
            detectorName
        )
        if layout is not None:
            if layout.payload_kind == PAYLOAD_ASSEMBLED_IMAGE:
                return 1
            if layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM:
                if layout.recorded_event_spans is not None:
                    return sum(
                        span.count * span.repeats
                        for span in layout.recorded_event_spans
                    )
                return math.prod(loop.count for loop in layout.event_loops)
        if self.recMode in (RecMode.ScanOnce, RecMode.ScanLapse) and \
                self._isScanDrivenDetector(detectorName):
            return 1
        if self.recMode in (RecMode.ScanOnce, RecMode.ScanLapse):
            declared = numCamTTL.get(detectorName)
            if declared is None:
                raise ValueError(
                    f'The scan declares no TTL pulse per position for detector '
                    f'{detectorName!r}, so the number of frames to record '
                    f'cannot be derived. Gate it in the scan, deselect it, or '
                    f'record it in a non-scan mode.'
                )
            return recFrames * int(declared)
        return recFrames * numCamTTL.get(detectorName, 1)

    def _stallReferenceTimeFor(self, detectorName, lastFrameTime):
        """Return when no-frame timing may begin, or ``None`` while scanning."""
        if (
            self.recMode in (RecMode.ScanOnce, RecMode.ScanLapse)
            and self._isScanDrivenDetector(detectorName)
        ):
            completionTime = getattr(
                self.__recordingManager, 'scanCompletionTime', None
            )
            if not callable(completionTime):
                return None
            return completionTime(
                getattr(self, 'recordingGeneration', None)
            )
        return lastFrameTime

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
        # Frames delivered beyond the plan, per detector (see the clip below).
        discardedFrames: Dict[str, int] = {}
        discardWarned: set = set()
        self.discardedFrames = discardedFrames
        lastFrameTime = {detectorName: time.time() for detectorName in self.detectorNames}
        
        # Prepare shapes for storer
        for detectorName in shapes:
            shape = shapes[detectorName]
            if len(shape) > 2:
                shapes[detectorName] = shape[-2:]
        
        expected_frames = None
        if self.recMode in [
            RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse,
            RecMode.CameraLapse,
        ]:
            recFrames = self.recFrames
            if recFrames is None:
                raise ValueError(
                    'recFrames must be specified in SpecFrames, ScanOnce, '
                    'ScanLapse or CameraLapse mode'
                )

            numCamTTL = self.numCamTTL if self.numCamTTL is not None else {}
            expected_frames = {
                detectorName: self._expectedFramesFor(detectorName, recFrames,
                                                      numCamTTL)
                for detectorName in self.detectorNames
            }
            # A recording that stalls or truncates almost always comes down to
            # this number being wrong for one detector: a scan-driven detector
            # emits ONE assembled frame per scan while a camera emits one per
            # position, so log what is actually being waited for, per detector.
            self.__logger.debug(
                f'{self.recMode.name} recording expects: '
                + ', '.join(
                    f'{name} {count} frame(s)'
                    f'{" [scan-driven]" if self._isScanDrivenDetector(name) else ""}'
                    for name, count in expected_frames.items()
                )
            )

        # Augment attrs with recording metadata (exposure, version, timestamp)
        augmented_attrs = self._augment_attrs_with_recording_metadata(
            self.attrs,
            expected_frames,
        )

        # Shared OME metadata for the storer (axes from recording mode + scan
        # geometry). A streaming stack is always (N, Y, X), so build with a
        # >=2 frame count to force the leading axis; the actual count is filled
        # in at finalize from the real frames written.
        mode = _ome.normalize_mode(self.recMode.name)
        scanDims = getattr(self, 'scanDims', None)
        scanStepSizes = getattr(self, 'scanStepSizes', None)
        frameIntervalS = (
            getattr(self, 'recLapseIntervalS', None)
            if self.recMode == RecMode.CameraLapse else None
        )
        storer.omeMeta = {
            detectorName: self.__recordingManager.buildOmeMeta(
                detectorName, mode,
                max(2, int((expected_frames or {}).get(detectorName, 2))),
                scanDims=scanDims,
                scanStepSizes=scanStepSizes,
                frameIntervalS=frameIntervalS)
            for detectorName in self.detectorNames
        }

        # Start writer thread and wait for openStream handshake
        writerThread = WriterThread(
            storer=storer,
            fileDests=fileDests,
            detectorNames=self.detectorNames,
            shapes=shapes,
            attrs=augmented_attrs,
            singleMultiDetectorFile=self.singleMultiDetectorFile,
            singleLapseFile=(
                self.recMode in (RecMode.ScanLapse, RecMode.CameraLapse)
                and self.singleLapseFile
            ),
            saveMode=self.saveMode,
            filePaths=filePaths,
            recordingManager=self.__recordingManager,
            recordingGeneration=getattr(self, 'recordingGeneration', None),
            recordingMode=mode,
            scanDims=scanDims,
            scanDrivenDetectors={
                detectorName: self._isScanDrivenDetector(detectorName)
                for detectorName in self.detectorNames
            },
        )
        self._writerThread = writerThread
        writerThread.start()
        
        # Wait for openStream to complete (blocks until success or raises on error)
        writerThread.wait_for_open()
        
        # Determine stop condition based on recMode
        if self.recMode in [
            RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse,
            RecMode.CameraLapse,
        ]:
            nFramesPerDetector = expected_frames
            
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
        
        startedConsumers = []
        recordingSucceeded = False
        shouldStopNext = False
        try:
            # Establish an atomic per-consumer boundary after detector
            # ownership and writer readiness. Existing consumers retain
            # pre-boundary frames; this recording sees only frames captured
            # after it is fully armed. Registration is part of the guarded
            # transaction so a later detector failing cannot leak earlier
            # consumer queues.
            for detectorName in self.detectorNames:
                detector = (
                    self.__recordingManager.detectorsManager[detectorName]
                )
                startConsumer = getattr(
                    detector, 'startChunkConsumer', None
                )
                if callable(startConsumer):
                    # A recording wants the measurement, not the picture of
                    # it. For a point detector these differ: what reaches the
                    # screen is whatever its line-step view mode reduced the
                    # scan to, which is a display preference and was never
                    # meant to decide what got saved.
                    try:
                        startConsumer(_RECORDING_CHUNK_CONSUMER,
                                      kind=ChunkKind.RAW)
                    except TypeError:
                        # An out-of-tree detector predating the kind argument
                        # serves one representation; take it rather than fail.
                        startConsumer(_RECORDING_CHUNK_CONSUMER)
                else:
                    # Compatibility for out-of-tree detector implementations
                    # that have not yet adopted the atomic boundary API.
                    detector.releaseChunkConsumer(
                        _RECORDING_CHUNK_CONSUMER
                    )
                startedConsumers.append(detectorName)
            self.__recordingManager._signalAcquisitionStarted()
            self.__recordingManager._signalRecordingStarted(
                getattr(self, 'recordingGeneration', None)
            )

            while self.__recordingManager.record and not shouldStopNext:
                for detectorName in self.detectorNames:
                    # Skip detector if it has reached its frame target
                    if (self.recMode in [
                            RecMode.SpecFrames, RecMode.ScanOnce,
                            RecMode.ScanLapse, RecMode.CameraLapse,
                    ] and
                            currentFrame[detectorName] >= nFramesPerDetector[detectorName]):
                        continue
                    
                    # Get new frames
                    newFrames = self._getNewFrames(detectorName)
                    n = len(newFrames)
                    if n > 0:
                        # Clip frames if needed for SpecFrames mode
                        if self.recMode in [
                            RecMode.SpecFrames, RecMode.ScanOnce,
                            RecMode.ScanLapse, RecMode.CameraLapse,
                        ]:
                            remaining = nFramesPerDetector[detectorName] - currentFrame[detectorName]
                            if n > remaining:
                                # Frames beyond the plan are not written; say
                                # so, and record it, rather than finalising a
                                # file that looks exactly like a clean scan. A
                                # surplus means the detector produced frames
                                # the scan did not account for -- free-running,
                                # or pulsed more often than declared.
                                surplus = n - remaining
                                discardedFrames[detectorName] = (
                                    discardedFrames.get(detectorName, 0) + surplus
                                )
                                if detectorName not in discardWarned:
                                    discardWarned.add(detectorName)
                                    self.__logger.warning(
                                        f'Detector {detectorName!r} delivered '
                                        f'{surplus} frame(s) beyond the '
                                        f'{nFramesPerDetector[detectorName]} '
                                        f'planned for this recording; they are '
                                        f'not written. The recording is not the '
                                        f'clean scan its metadata describes -- '
                                        f'check numCamTTL and the camera '
                                        f'trigger mode.'
                                    )
                                newFrames = newFrames[:remaining]
                                n = remaining
                        
                        # Enqueue frames for writer thread (blocks if queue full, providing backpressure)
                        writerThread.enqueue_frames(detectorName, newFrames)
                        currentFrame[detectorName] += n
                        lastFrameTime[detectorName] = time.time()  # Update watchdog timestamp
                
                # Emit progress signals based on recMode
                if self.recMode in [
                    RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse,
                    RecMode.CameraLapse,
                ]:
                    # Report the lowest frame number (for multi-detector)
                    self.__recordingManager.sigRecordingFrameNumUpdated.emit(
                        min(list(currentFrame.values()))
                    )
                elif self.recMode == RecMode.SpecTime:
                    currentRecTime = time.time() - startTime
                    # sigRecordingTimeUpdated is declared Signal(int); PyQt5 does
                    # NOT coerce a numpy.float64 here, it reinterprets the bits
                    # into a garbage int. Emit a plain Python int.
                    self.__recordingManager.sigRecordingTimeUpdated.emit(
                        int(currentRecTime)
                    )
                
                # Check for stalled detectors (only for modes with frame targets)
                if self.recMode in [
                    RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse,
                    RecMode.CameraLapse,
                ]:
                    now = time.time()
                    for detectorName in self.detectorNames:
                        # Skip detectors that have already reached their target
                        if currentFrame[detectorName] >= nFramesPerDetector[detectorName]:
                            continue

                        referenceTime = self._stallReferenceTimeFor(
                            detectorName, lastFrameTime[detectorName]
                        )
                        if referenceTime is None:
                            # A scan-driven detector has no frame-level
                            # progress to report before the full scan ends.
                            continue

                        elapsed = now - referenceTime
                        if elapsed > self.stallTimeout:
                            if self._isScanDrivenDetector(detectorName):
                                message = (
                                    f"Detector '{detectorName}' stalled: no "
                                    f"assembled frame received for "
                                    f"{elapsed:.1f}s after scan completion "
                                    f"(timeout: {self.stallTimeout}s). "
                                    f"Current: {currentFrame[detectorName]} "
                                    f"frames, expected: "
                                    f"{nFramesPerDetector[detectorName]} "
                                    f"frames. Check the detector input, scan "
                                    f"trigger, and sample-clock configuration."
                                )
                            else:
                                message = (
                                    f"Detector '{detectorName}' stalled: no "
                                    f"frames received for {elapsed:.1f}s "
                                    f"(timeout: {self.stallTimeout}s). "
                                    f"Current: {currentFrame[detectorName]} "
                                    f"frames, expected: "
                                    f"{nFramesPerDetector[detectorName]} "
                                    f"frames. Check camera triggering and "
                                    f"numCamTTL configuration."
                                )
                            self.__logger.error(message)
                            self.__recordingManager.sigRecordingStalled.emit(detectorName)
                            # A truncated stream is a recording failure, not a
                            # successful short file. Raising routes through the
                            # abort/partial-output cleanup and distinct failure
                            # signal.
                            raise RuntimeError(message)
                
                # Check stop condition
                if should_stop():
                    shouldStopNext = True
                
                time.sleep(FRAME_POLL_INTERVAL)  # Yield to event loop to prevent UI freezing
            
            # Reset progress signals
            if self.recMode in [
                RecMode.SpecFrames, RecMode.ScanOnce, RecMode.ScanLapse,
                RecMode.CameraLapse,
            ]:
                self.__recordingManager.sigRecordingFrameNumUpdated.emit(0)
            elif self.recMode == RecMode.SpecTime:
                self.__recordingManager.sigRecordingTimeUpdated.emit(0)
            recordingSucceeded = True
        
        finally:
            # Stop retaining frames for this consumer now that the recording
            # is over (other readChunk consumers may keep draining).
            cleanupErrors = []
            for detectorName in startedConsumers:
                try:
                    self.__recordingManager.detectorsManager[
                        detectorName
                    ].releaseChunkConsumer(_RECORDING_CHUNK_CONSUMER)
                except Exception as error:
                    cleanupErrors.append(error)
                    self.__logger.error(
                        f'Failed to release recording frame consumer for '
                        f'"{detectorName}": {error}',
                        exc_info=True,
                    )
            
            # Tear down the writer thread. On abort, discard partial output;
            # otherwise drain the queue and finalize. Both enqueue a sentinel and
            # join, so finalize/abortStream completes before _record returns.
            aborting = (
                self.__recordingManager.aborting
                or not recordingSucceeded
                or bool(cleanupErrors)
            )
            try:
                if aborting:
                    writerThread.abort()
                else:
                    writerThread.noteDiscardedFrames(discardedFrames)
                    writerThread.finish()
            except Exception as error:
                cleanupErrors.append(error)
                self.__logger.error(
                    f'Failed to finalize recording writer: {error}',
                    exc_info=True,
                )
                if not aborting:
                    try:
                        writerThread.abort()
                    except Exception as abortError:
                        cleanupErrors.append(abortError)
                        self.__logger.error(
                            f'Failed to abort writer after finalization '
                            f'failure: {abortError}',
                            exc_info=True,
                        )
                aborting = True
            finally:
                self._clearWriterReferenceIfStopped(writerThread)

            # End recording. When aborting, abortRecording() already emitted
            # its requested terminal signal (or a failure terminal was
            # published), so suppress success here.
            if aborting:
                self.__recordingManager.endRecording(emitSignal=False, wait=False)
            else:
                # The detailed signal is the authoritative writer-drained
                # boundary for every mode. Scan-driven modes suppress only the
                # legacy UI signal; RecordingController waits for both this
                # terminal and sigScanDone before advancing/resetting.
                emitLegacy = self.recMode not in [
                    RecMode.ScanOnce, RecMode.ScanLapse
                ]
                self.__recordingManager.endRecording(
                    emitSignal=False, wait=False
                )
                self.__recordingManager._signalRecordingEnded(
                    self.recordingGeneration,
                    emitLegacy=emitLegacy,
                )
            if cleanupErrors:
                raise RuntimeError(
                    'Recording cleanup failed; partial output was aborted'
                ) from cleanupErrors[0]

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
    CameraLapse = 6


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
