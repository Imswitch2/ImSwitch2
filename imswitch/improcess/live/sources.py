"""Format-agnostic live source contracts and in-memory batch fallback."""

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import zarr

from imswitch.improcess.reconstructors.base import Chunk, StackInfo

_ZarrGroup = getattr(zarr, "Group", None) or getattr(
    getattr(zarr, "hierarchy", None),
    "Group",
    None,
)
_ZarrArray = getattr(zarr, "Array", None) or getattr(getattr(zarr, "core", None), "Array", None)


def _is_zarr_group(obj: Any) -> bool:
    return _ZarrGroup is not None and isinstance(obj, _ZarrGroup)


def _is_zarr_array(obj: Any) -> bool:
    return _ZarrArray is not None and isinstance(obj, _ZarrArray)


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        array = np.asarray(value).flatten()
        if array.size != 1:
            return None
        value = array[0]
    if isinstance(value, (bytes, np.bytes_)):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in {"", "null", "none", "nan", "n/a", "na"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number) or number <= 0:
        return None
    return max(1, int(number))


def _numeric_vector(value: Any, min_len: int) -> np.ndarray | None:
    try:
        vector = np.asarray(value, dtype=float).flatten()
    except (TypeError, ValueError):
        return None
    if vector.size < min_len or not np.all(np.isfinite(vector[:min_len])):
        return None
    return vector


def _meta_lookup(attrs: dict[str, Any], key: str) -> Any:
    """Read a metadata key from flat attrs or a nested ``ImswitchData`` block.

    ImSwitch2's structured storer flattens scan metadata to top-level keys
    (``ScanStage:axis_length`` …); legacy ImSwitch-1 Zarr nests the same keys
    under an ``ImswitchData`` attr. Prefer the flat key, then the nested one.
    """
    if key in attrs:
        return attrs[key]
    nested = attrs.get("ImswitchData")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return None


def _derive_scan_frames_per_stack(attrs: dict[str, Any]) -> int | None:
    """Return frames per MoNaLISA scan stack from recorder metadata.

    ``recording:frames_per_stack`` wins, followed by explicit ``ScanTTL:Nx`` /
    ``ScanTTL:Ny`` counts. Otherwise derive the X/Y scan counts from
    ImControl's scan-size convention, where ``axis_length`` is a physical
    length and the number of positions is ``ceil(length / step_size)``.
    Metadata is read from flat attrs or a nested ``ImswitchData`` block so both
    the ImSwitch2 structured layout and legacy ImSwitch-1 Zarr work.
    """
    explicit = _coerce_positive_int(_meta_lookup(attrs, "recording:frames_per_stack"))
    if explicit is not None:
        return explicit

    nx_ttl = _coerce_positive_int(_meta_lookup(attrs, "ScanTTL:Nx"))
    ny_ttl = _coerce_positive_int(_meta_lookup(attrs, "ScanTTL:Ny"))
    if nx_ttl is not None and ny_ttl is not None:
        return nx_ttl * ny_ttl

    lengths = _numeric_vector(_meta_lookup(attrs, "ScanStage:axis_length"), 2)
    step_sizes = _numeric_vector(_meta_lookup(attrs, "ScanStage:axis_step_size"), 2)
    if lengths is None or step_sizes is None:
        return None
    if step_sizes[0] == 0 or step_sizes[1] == 0:
        return None

    nx_s = max(1, int(np.ceil(abs(lengths[0]) / abs(step_sizes[0]))))
    ny_s = max(1, int(np.ceil(abs(lengths[1]) / abs(step_sizes[1]))))
    return nx_s * ny_s


class LiveSource(ABC):
    """Polls a growing source and yields new raw-frame chunks."""

    @abstractmethod
    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the source and return its stack metadata."""
        ...

    @abstractmethod
    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        ...

    @abstractmethod
    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        ...

    def close(self) -> None:
        """Release source resources."""
        return None


class InMemoryStackWrapper:
    """Minimal DataObj-compatible wrapper for buffered live stacks."""

    def __init__(
        self,
        name: str,
        dataset_name: str,
        data: np.ndarray,
        attrs: dict[str, Any] | None = None,
    ):
        self.name = name
        self._datasetName = dataset_name
        self._data = np.asarray(data)
        self._attrs = dict(attrs or {})
        self.dataPath = None
        self._meanData = None

    @property
    def datasetName(self) -> str:
        return self._datasetName

    @property
    def data(self) -> np.ndarray:
        return self._data

    @property
    def attrs(self) -> dict[str, Any]:
        return self._attrs

    @property
    def dataLoaded(self) -> bool:
        return True

    @property
    def numFrames(self) -> int | None:
        return self._data.shape[0] if self._data.ndim > 0 else None

    def checkAndLoadData(self) -> None:
        return None

    def checkAndUnloadData(self) -> None:
        return None

    def getMeanData(self) -> np.ndarray:
        if self._meanData is None:
            self._meanData = np.array(np.mean(self._data, 0), dtype=np.float32)
        return self._meanData


class ZarrLiveSource(LiveSource):
    """Polls a growing Zarr array for new frames."""

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name for structured layout. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use array.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._root = None
        self._array = None
        self._array_path: tuple[str, ...] = ()
        self._cursor = 0
        self._chunk_size = 1
        self._expected_frames: int | None = None
        self._writing = True

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the Zarr store and return stack metadata."""
        self._cursor = 0
        self._root = self._open_root(path_or_handle)

        if _is_zarr_array(self._root):
            self._array = self._root
            self._array_path = ()
            attrs = self._read_attrs()
            detector_name = (
                self._detector_name
                or attrs.get('recording:detector_name')
                or attrs.get('detector_name')
            )
        else:
            detector_name = self._detector_name or self._auto_detect_detector()
            self._array, attrs = self._open_array(detector_name)

        if self._array.ndim != 3:
            raise ValueError(f"Expected 3D array (T, Y, X), got shape {self._array.shape}")

        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._array.chunks is not None and len(self._array.chunks) > 0:
            self._chunk_size = max(1, int(self._array.chunks[0]))
        else:
            self._chunk_size = 1

        frame_shape = self._array.shape[-2:]
        self._refresh_state_from_attrs(attrs)

        # Legacy / metadata-less stores carry no recording:expected_frames. A
        # completed store (not writing) is one whole stack, so its length is the
        # expected frame count — without this, downstream startup would treat
        # frames_per_stack as unknown and grab only the first chunk.
        if self._expected_frames is None and not self._writing:
            self._expected_frames = int(self._array.shape[0])

        # frames_per_stack: an explicit recording attr wins; for a completed
        # single-stack store the exact array length is more reliable than the
        # scan-size guess (which is sensitive to the size-vs-endpoint length
        # convention); only fall back to the ScanStage-derived count for a
        # still-writing store with no explicit metadata.
        explicit_fps = _coerce_positive_int(_meta_lookup(attrs, "recording:frames_per_stack"))
        if explicit_fps is not None:
            frames_per_stack = explicit_fps
        elif not self._writing and self._expected_frames is not None:
            frames_per_stack = self._expected_frames
        else:
            frames_per_stack = _derive_scan_frames_per_stack(attrs)

        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._array.dtype,
            attrs=dict(attrs),
            expected_frames=self._expected_frames,
            frames_per_stack=frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or detector_name,
            dataset_path=attrs.get('recording:dataset_path') or self._default_dataset_path(),
            source_format=attrs.get('recording:source_format') or 'ZARR',
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        self._refresh_array()
        if self._array is None:
            return []

        self._refresh_state_from_attrs(self._read_attrs())
        readable_length = self._readable_length()
        if self._cursor >= readable_length:
            return []

        chunks = []
        while self._cursor < readable_length:
            start = self._cursor
            end = min(start + self._chunk_size, readable_length)

            data = self._array[start:end]
            chunks.append(Chunk(data=data, start=start, end=end))

            self._cursor = end

        return chunks

    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        self._refresh_array()
        if self._array is None:
            return True

        self._refresh_state_from_attrs(self._read_attrs())
        current_length = self._array.shape[0]

        if self._expected_frames is not None:
            if self._cursor >= self._expected_frames:
                return True

        if not self._writing and self._cursor >= current_length:
            return True

        return False

    def close(self) -> None:
        """Release source resources."""
        self._path = None
        self._root = None
        self._array = None
        self._array_path = ()

    def _auto_detect_detector(self) -> str:
        """Auto-detect detector name from root contents."""
        if self._root is None or _is_zarr_array(self._root):
            raise ValueError("Root not opened")

        for key in self._root.keys():
            item = self._root[key]
            if _is_zarr_group(item):
                if 'data' in item:
                    return key
            elif _is_zarr_array(item):
                return key

        raise ValueError("No detector array found in Zarr store")

    def _open_array(self, detector_name: str) -> tuple[Any, dict[str, Any]]:
        """Open array and collect attributes from structured or legacy layout."""
        if self._root is None or _is_zarr_array(self._root):
            raise ValueError("Root not opened")

        if detector_name in self._root:
            item = self._root[detector_name]

            if _is_zarr_group(item):
                if 'data' not in item:
                    raise ValueError(f"Detector group '{detector_name}' has no 'data' array")
                array = item['data']
                self._array_path = (detector_name, 'data')

            elif _is_zarr_array(item):
                array = item
                self._array_path = (detector_name,)

            else:
                raise ValueError(f"'{detector_name}' is neither a group nor an array")
        else:
            raise ValueError(f"Detector '{detector_name}' not found in Zarr store")

        self._array = array
        return array, self._read_attrs()

    def _flatten_metadata(self, group: Any, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]

        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if _is_zarr_group(subgroup):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _open_root(self, path_or_handle: Any) -> Any:
        if _is_zarr_group(path_or_handle) or _is_zarr_array(path_or_handle):
            self._path = None
            return path_or_handle

        path = Path(path_or_handle)
        self._path = str(path)
        return zarr.open(str(path), mode='r')

    def _refresh_array(self) -> None:
        if self._path is None or self._array is None:
            return

        self._root = zarr.open(self._path, mode='r')
        node = self._root
        for part in self._array_path:
            node = node[part]
        self._array = node

    def _read_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._array is None:
            return attrs

        if _is_zarr_group(self._root):
            attrs.update(dict(self._root.attrs))

            if len(self._array_path) == 2:
                detector_group = self._root[self._array_path[0]]
                attrs.update(dict(self._array.attrs))
                if 'metadata' in detector_group:
                    self._flatten_metadata(detector_group['metadata'], attrs, prefix='')
                return attrs

        attrs.update(dict(self._array.attrs))
        return attrs

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        expected_frames = self._coerce_int(attrs.get('recording:expected_frames'))
        if expected_frames is not None:
            self._expected_frames = expected_frames

        self._writing = self._coerce_bool(attrs.get('writing'), default=self._writing)

    def _readable_length(self) -> int:
        if self._array is None:
            return 0

        current_length = int(self._array.shape[0])
        if self._expected_frames is None:
            return current_length
        return min(current_length, self._expected_frames)

    def _default_dataset_path(self) -> str | None:
        if not self._array_path:
            return None
        return '/' + '/'.join(self._array_path)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


def _lapse_index_template(path: str):
    """Derive how to build sibling per-file-lapse paths from one file name.

    Matches the scan index in names like ``..._scan__00__CAM.zarr`` (legacy)
    or ``..._scan0.zarr`` (ImSwitch2). Returns ``(folder, prefix, width,
    suffix, index)`` so ``prefix + str(i).zfill(width) + suffix`` reconstructs
    timepoint ``i``'s file, or ``None`` if the name carries no scan index.
    """
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    match = re.search(r"(?i)scan[_]*(\d+)", name)
    if match is None:
        return None
    start, end = match.span(1)
    width = end - start
    return folder, name[:start], width, name[end:], int(match.group(1))


class ZarrMultiFileLapseSource(LiveSource):
    """Stream a per-file timelapse (one ``.zarr`` per timepoint) as one stream.

    Each ``<base>_scan<NN>_<...>.zarr`` store holds a single stack (one
    timepoint). Files are streamed in index order and frames are emitted with
    GLOBAL indices (``position * frames_per_stack + local``) so a streaming
    session's ``time_index = start // frames_per_stack`` routes each file into
    its own timepoint, accumulating one multi-timepoint result. Later timepoint
    files (live recording) are picked up by re-deriving the next index path.
    """

    def __init__(self, first_path, detector_name=None, chunk_size=None,
                 num_timepoints=None):
        self._first_path = str(first_path)
        self._detector_name = detector_name
        self._chunk_size = chunk_size
        self._num_timepoints = num_timepoints
        self._inner: ZarrLiveSource | None = None
        self._template = None
        self._first_index = 0
        self._position = 0  # 0-based timepoint position
        self._frames_per_stack = 1

    def open(self, path_or_handle: Any) -> StackInfo:
        first = str(path_or_handle) if path_or_handle is not None else self._first_path
        self._first_path = first
        self._template = _lapse_index_template(first)
        self._first_index = self._template[4] if self._template else 0
        self._position = 0

        self._inner = ZarrLiveSource(self._detector_name, self._chunk_size)
        info = self._inner.open(first)
        self._frames_per_stack = int(info.frames_per_stack or info.expected_frames or 1)

        num_tp = (
            self._num_timepoints
            or _coerce_positive_int(_meta_lookup(info.attrs, "recording:num_timepoints"))
            or _coerce_positive_int(_meta_lookup(info.attrs, "Rec:LapseTime"))
            or self._count_present_timepoints()
        )
        self._num_timepoints = max(1, int(num_tp or 1))
        expected_total = self._frames_per_stack * self._num_timepoints

        return StackInfo(
            frame_shape=info.frame_shape,
            dtype=info.dtype,
            attrs=dict(info.attrs),
            expected_frames=expected_total,
            frames_per_stack=self._frames_per_stack,
            detector_name=info.detector_name,
            dataset_path=info.dataset_path,
            source_format=info.source_format or "ZARR",
        )

    def poll(self) -> list[Chunk]:
        if self._inner is None:
            return []

        offset = self._position * self._frames_per_stack
        chunks = [Chunk(c.data, c.start + offset, c.end + offset) for c in self._inner.poll()]

        # Current timepoint fully read: advance to the next file if it exists.
        if self._inner.is_complete() and self._position + 1 < self._num_timepoints:
            next_path = self._build_path(self._position + 1)
            if next_path is not None and os.path.exists(next_path):
                self._inner.close()
                self._position += 1
                self._inner = ZarrLiveSource(self._detector_name, self._chunk_size)
                self._inner.open(next_path)
        return chunks

    def is_complete(self) -> bool:
        if self._inner is None:
            return True
        if not self._inner.is_complete():
            return False
        # On the last expected timepoint with its file fully read -> done.
        if self._position + 1 >= self._num_timepoints:
            return True
        # Otherwise wait for the next timepoint file (live). If it isn't going
        # to appear, the user stops live mode; we don't guess completion here.
        return False

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
        self._inner = None

    def _build_path(self, position: int) -> str | None:
        if self._template is None:
            return None
        folder, prefix, width, suffix, _ = self._template
        index = self._first_index + position
        return os.path.join(folder, f"{prefix}{index:0{width}d}{suffix}")

    def _count_present_timepoints(self) -> int:
        count = 1
        while True:
            path = self._build_path(count)
            if path is None or not os.path.exists(path):
                break
            count += 1
        return count


class ZarrLapseSource(LiveSource):
    """Polls a single-file scan{N} timelapse store as one continuous global frame stream."""

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name inside scan groups. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use array.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._root = None
        self._scan_groups: list[str] = []
        self._current_group_index = 0
        self._global_cursor = 0
        self._local_cursor = 0
        self._chunk_size = 1
        self._frames_per_stack: int | None = None
        self._num_timepoints: int | None = None
        self._current_array = None
        self._current_array_path: tuple[str, ...] = ()
        self._resolved_detector_name: str | None = None
        self._writing = True

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the Zarr store and return stack metadata."""
        self._global_cursor = 0
        self._local_cursor = 0
        self._current_group_index = 0
        
        self._root = self._open_root(path_or_handle)
        
        if _is_zarr_array(self._root):
            raise ValueError("ZarrLapseSource expects a group with scan{N} structure, not a raw array")
        
        # Enumerate scan{N} groups
        self._scan_groups = sorted(
            [key for key in self._root.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )
        
        if not self._scan_groups:
            raise ValueError("No scan{N} groups found in Zarr store")
        
        # Open scan0 to detect detector and read metadata
        scan0 = self._root[self._scan_groups[0]]
        self._resolved_detector_name = self._detector_name or self._auto_detect_detector(scan0)
        
        # Open the detector array in scan0
        self._current_array, attrs = self._open_detector_array(scan0, self._resolved_detector_name)
        self._current_array_path = (self._scan_groups[0], self._resolved_detector_name, 'data')
        
        if self._current_array.ndim != 3:
            raise ValueError(f"Expected 3D array (T, Y, X), got shape {self._current_array.shape}")
        
        # Determine chunk size
        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._current_array.chunks is not None and len(self._current_array.chunks) > 0:
            self._chunk_size = max(1, int(self._current_array.chunks[0]))
        else:
            self._chunk_size = 1
        
        # Extract frames_per_stack and num_timepoints
        self._frames_per_stack = self._coerce_int(attrs.get('recording:frames_per_stack'))
        if self._frames_per_stack is None:
            self._frames_per_stack = self._current_array.shape[0]
        
        self._num_timepoints = self._coerce_int(attrs.get('recording:num_timepoints'))
        self._refresh_state_from_attrs(attrs)
        
        # Calculate expected_frames
        expected_frames = None
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_frames = self._num_timepoints * self._frames_per_stack
        
        frame_shape = self._current_array.shape[-2:]
        
        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._current_array.dtype,
            attrs=dict(attrs),
            expected_frames=expected_frames,
            frames_per_stack=self._frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or self._resolved_detector_name,
            dataset_path=attrs.get('recording:dataset_path'),
            source_format=attrs.get('recording:source_format') or 'ZARR',
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks with GLOBAL indices across all scan groups."""
        if self._current_array is None:
            return []
        
        chunks = []
        
        while True:
            # Refresh current array and re-enumerate scan groups to detect new ones
            self._refresh_current_array()
            self._refresh_scan_groups()
            
            # Read current group's attributes
            current_group = self._root[self._scan_groups[self._current_group_index]]
            _, attrs = self._open_detector_array(current_group, self._resolved_detector_name)
            self._refresh_state_from_attrs(attrs)
            
            # Determine how many frames we can read from current group
            readable_length = min(self._current_array.shape[0], self._frames_per_stack or self._current_array.shape[0])
            
            if self._local_cursor >= readable_length:
                # Current group is fully read, try to advance to next
                if self._current_group_index + 1 < len(self._scan_groups):
                    # Check if next group exists
                    next_group_name = self._scan_groups[self._current_group_index + 1]
                    if next_group_name in self._root:
                        # Advance to next group
                        self._current_group_index += 1
                        self._local_cursor = 0
                        next_group = self._root[next_group_name]
                        self._current_array, _ = self._open_detector_array(next_group, self._resolved_detector_name)
                        self._current_array_path = (next_group_name, self._resolved_detector_name, 'data')
                        continue
                    else:
                        # Next group doesn't exist yet, wait
                        break
                else:
                    # No more groups expected
                    break
            
            # Read chunks from current group
            start_local = self._local_cursor
            end_local = min(start_local + self._chunk_size, readable_length)
            
            if start_local >= end_local:
                break
            
            # Calculate GLOBAL indices
            start_global = self._current_group_index * (self._frames_per_stack or 0) + start_local
            end_global = self._current_group_index * (self._frames_per_stack or 0) + end_local
            
            data = self._current_array[start_local:end_local]
            chunks.append(Chunk(data=data, start=start_global, end=end_global))
            
            self._local_cursor = end_local
            self._global_cursor = end_global
            
            # If we haven't filled a full chunk, stop polling (wait for more data)
            if end_local - start_local < self._chunk_size and end_local < readable_length:
                break
        
        return chunks

    def is_complete(self) -> bool:
        """Return whether all timepoints have been fully consumed."""
        if self._current_array is None:
            return True
        
        # If we know the total expected frames, check against global cursor
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_total = self._num_timepoints * self._frames_per_stack
            if self._global_cursor >= expected_total:
                return True
        
        # Check if we're on the last expected group
        if self._num_timepoints is not None:
            if self._current_group_index >= self._num_timepoints - 1:
                # We're on or past the last expected group
                self._refresh_current_array()
                current_group = self._root[self._scan_groups[self._current_group_index]]
                _, attrs = self._open_detector_array(current_group, self._resolved_detector_name)
                self._refresh_state_from_attrs(attrs)
                
                readable_length = min(self._current_array.shape[0], self._frames_per_stack or self._current_array.shape[0])
                
                if not self._writing and self._local_cursor >= readable_length:
                    return True
        
        return False

    def close(self) -> None:
        """Release source resources."""
        self._path = None
        self._root = None
        self._current_array = None
        self._current_array_path = ()
        self._scan_groups = []

    def _auto_detect_detector(self, scan_group: Any) -> str:
        """Auto-detect detector name from scan group contents."""
        for key in scan_group.keys():
            item = scan_group[key]
            if _is_zarr_group(item) and 'data' in item:
                return key
        
        raise ValueError("No detector group with 'data' array found in scan group")

    def _open_detector_array(self, scan_group: Any, detector_name: str) -> tuple[Any, dict[str, Any]]:
        """Open detector array and collect flattened attributes."""
        if detector_name not in scan_group:
            raise ValueError(f"Detector '{detector_name}' not found in scan group")
        
        det_group = scan_group[detector_name]
        if not _is_zarr_group(det_group):
            raise ValueError(f"'{detector_name}' is not a group")
        
        if 'data' not in det_group:
            raise ValueError(f"Detector group '{detector_name}' has no 'data' array")
        
        array = det_group['data']
        
        # Flatten attributes from root, detector group, and metadata
        attrs: dict[str, Any] = {}
        
        # Root attrs
        if _is_zarr_group(self._root):
            attrs.update(dict(self._root.attrs))
        
        # Array attrs
        attrs.update(dict(array.attrs))
        
        # Metadata group
        if 'metadata' in det_group:
            self._flatten_metadata(det_group['metadata'], attrs, prefix='')
        
        return array, attrs

    def _flatten_metadata(self, group: Any, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]
        
        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if _is_zarr_group(subgroup):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _open_root(self, path_or_handle: Any) -> Any:
        if _is_zarr_group(path_or_handle):
            self._path = None
            return path_or_handle
        
        path = Path(path_or_handle)
        self._path = str(path)
        return zarr.open(str(path), mode='r')

    def _refresh_current_array(self) -> None:
        """Re-open current array to see new frames."""
        if self._path is None or self._current_array is None:
            return
        
        self._root = zarr.open(self._path, mode='r')
        current_group = self._root[self._scan_groups[self._current_group_index]]
        det_group = current_group[self._resolved_detector_name]
        self._current_array = det_group['data']

    def _refresh_scan_groups(self) -> None:
        """Re-enumerate scan groups to detect newly added ones."""
        if self._path is None:
            return
        
        self._scan_groups = sorted(
            [key for key in self._root.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        self._writing = self._coerce_bool(attrs.get('writing'), default=self._writing)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


class Hdf5LiveSource(LiveSource):
    """Polls a growing HDF5 dataset for new frames (SWMR protocol)."""

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name for structured layout. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use dataset.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._file = None
        self._dataset = None
        self._dataset_path: str | None = None
        self._cursor = 0
        self._chunk_size = 1
        self._expected_frames: int | None = None
        self._writing = True
        self._owns_file = False

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the HDF5 file in SWMR mode and return stack metadata."""
        self._cursor = 0

        if isinstance(path_or_handle, h5py.File):
            self._file = path_or_handle
            self._path = None
            self._owns_file = False
        else:
            path = Path(path_or_handle)
            self._path = str(path)
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
            self._owns_file = True

        attrs = self._read_root_attrs()
        detector_name = self._detector_name or attrs.get('recording:detector_name')

        if detector_name is None:
            detector_name = self._auto_detect_detector()

        self._dataset, dataset_attrs = self._open_dataset(detector_name, attrs)

        if self._dataset.ndim != 3:
            raise ValueError(f"Expected 3D dataset (T, Y, X), got shape {self._dataset.shape}")

        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._dataset.chunks is not None and len(self._dataset.chunks) > 0:
            self._chunk_size = max(1, int(self._dataset.chunks[0]))
        else:
            self._chunk_size = 1

        frame_shape = self._dataset.shape[-2:]
        all_attrs = {**attrs, **dataset_attrs}
        self._refresh_state_from_attrs(all_attrs)
        frames_per_stack = _derive_scan_frames_per_stack(all_attrs)

        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._dataset.dtype,
            attrs=dict(all_attrs),
            expected_frames=self._expected_frames,
            frames_per_stack=frames_per_stack,
            detector_name=all_attrs.get('recording:detector_name') or detector_name,
            dataset_path=all_attrs.get('recording:dataset_path') or self._dataset_path,
            source_format=all_attrs.get('recording:source_format') or 'HDF5',
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks since the previous poll."""
        if self._dataset is None:
            return []

        self._dataset.refresh()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        readable_length = self._readable_length()

        if self._cursor >= readable_length:
            return []

        chunks = []
        while self._cursor < readable_length:
            start = self._cursor
            end = min(start + self._chunk_size, readable_length)

            data = self._dataset[start:end]
            chunks.append(Chunk(data=data, start=start, end=end))

            self._cursor = end

        return chunks

    def is_complete(self) -> bool:
        """Return whether the source has no more frames to yield."""
        if self._dataset is None:
            return True

        self._dataset.refresh()
        self._refresh_state_from_attrs(self._read_dataset_attrs())
        current_length = self._dataset.shape[0]

        if self._expected_frames is not None:
            if self._cursor >= self._expected_frames:
                return True

        if not self._writing and self._cursor >= current_length:
            return True

        return False

    def close(self) -> None:
        """Release source resources."""
        if self._owns_file and self._file is not None:
            self._file.close()
        self._path = None
        self._file = None
        self._dataset = None
        self._dataset_path = None

    def _auto_detect_detector(self) -> str:
        """Auto-detect detector name from file contents."""
        if self._file is None:
            raise ValueError("File not opened")

        for key in self._file.keys():
            item = self._file[key]
            if isinstance(item, h5py.Group):
                if 'data' in item:
                    return key
            elif isinstance(item, h5py.Dataset) and item.ndim == 3:
                return key

        raise ValueError("No detector dataset found in HDF5 file")

    def _open_dataset(self, detector_name: str, root_attrs: dict[str, Any]) -> tuple[h5py.Dataset, dict[str, Any]]:
        """Open dataset and collect attributes from structured or legacy layout."""
        if self._file is None:
            raise ValueError("File not opened")

        dataset_path = root_attrs.get('recording:dataset_path')
        if dataset_path:
            if dataset_path.startswith('/'):
                dataset_path = dataset_path[1:]
            if dataset_path in self._file:
                self._dataset_path = '/' + dataset_path
                dataset = self._file[dataset_path]
                return dataset, self._read_dataset_attrs_at(dataset.name)

        if detector_name in self._file:
            item = self._file[detector_name]

            if isinstance(item, h5py.Group):
                if 'data' not in item:
                    raise ValueError(f"Detector group '{detector_name}' has no 'data' dataset")
                dataset = item['data']
                self._dataset_path = f'/{detector_name}/data'

            elif isinstance(item, h5py.Dataset):
                dataset = item
                self._dataset_path = f'/{detector_name}'

            else:
                raise ValueError(f"'{detector_name}' is neither a group nor a dataset")
        else:
            raise ValueError(f"Detector '{detector_name}' not found in HDF5 file")

        return dataset, self._read_dataset_attrs_at(dataset.name)

    def _read_root_attrs(self) -> dict[str, Any]:
        """Read and flatten root-level attributes."""
        attrs: dict[str, Any] = {}
        if self._file is None:
            return attrs

        attrs.update(dict(self._file.attrs))
        return attrs

    def _read_dataset_attrs(self) -> dict[str, Any]:
        """Read and flatten dataset-level attributes."""
        if self._dataset is None:
            return {}
        return self._read_dataset_attrs_at(self._dataset.name)

    def _read_dataset_attrs_at(self, path: str) -> dict[str, Any]:
        """Read attributes at a specific path, flattening recording: prefixes and metadata groups."""
        attrs: dict[str, Any] = {}
        if self._file is None:
            return attrs

        dataset = self._file[path]
        for key, value in dataset.attrs.items():
            attrs[key] = value

        # Find parent group path and check for metadata group
        path_parts = path.strip('/').split('/')
        if len(path_parts) > 1:
            parent_path = '/' + '/'.join(path_parts[:-1])
            if parent_path in self._file:
                parent = self._file[parent_path]
                if isinstance(parent, h5py.Group) and 'metadata' in parent:
                    self._flatten_metadata(parent['metadata'], attrs, prefix='')

        return attrs

    def _flatten_metadata(self, group: h5py.Group, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]

        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if isinstance(subgroup, h5py.Group):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        expected_frames = self._coerce_int(attrs.get('recording:expected_frames'))
        if expected_frames is not None:
            self._expected_frames = expected_frames

        self._writing = self._coerce_bool(attrs.get('writing'), default=self._writing)

    def _readable_length(self) -> int:
        """Return the number of frames that can be read."""
        if self._dataset is None:
            return 0

        current_length = int(self._dataset.shape[0])
        if self._expected_frames is None:
            return current_length
        return min(current_length, self._expected_frames)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)


class Hdf5MultiFileLapseSource(LiveSource):
    """Stream a per-file HDF5 timelapse (one ``.h5`` per timepoint) as one stream.

    Each ``<base>_scan<NN>_<...>.h5`` file holds a single stack (one
    timepoint). Files are streamed in index order and frames are emitted with
    GLOBAL indices (``position * frames_per_stack + local``) so a streaming
    session's ``time_index = start // frames_per_stack`` routes each file into
    its own timepoint, accumulating one multi-timepoint result. Later timepoint
    files (live recording) are picked up by re-deriving the next index path.
    """

    def __init__(self, first_path, detector_name=None, chunk_size=None,
                 num_timepoints=None):
        self._first_path = str(first_path)
        self._detector_name = detector_name
        self._chunk_size = chunk_size
        self._num_timepoints = num_timepoints
        self._inner: Hdf5LiveSource | None = None
        self._template = None
        self._first_index = 0
        self._position = 0  # 0-based timepoint position
        self._frames_per_stack = 1

    def open(self, path_or_handle: Any) -> StackInfo:
        first = str(path_or_handle) if path_or_handle is not None else self._first_path
        self._first_path = first
        self._template = _lapse_index_template(first)
        self._first_index = self._template[4] if self._template else 0
        self._position = 0

        self._inner = Hdf5LiveSource(self._detector_name, self._chunk_size)
        info = self._inner.open(first)
        self._frames_per_stack = int(info.frames_per_stack or info.expected_frames or 1)

        num_tp = (
            self._num_timepoints
            or _coerce_positive_int(_meta_lookup(info.attrs, "recording:num_timepoints"))
            or _coerce_positive_int(_meta_lookup(info.attrs, "Rec:LapseTime"))
            or self._count_present_timepoints()
        )
        self._num_timepoints = max(1, int(num_tp or 1))
        expected_total = self._frames_per_stack * self._num_timepoints

        return StackInfo(
            frame_shape=info.frame_shape,
            dtype=info.dtype,
            attrs=dict(info.attrs),
            expected_frames=expected_total,
            frames_per_stack=self._frames_per_stack,
            detector_name=info.detector_name,
            dataset_path=info.dataset_path,
            source_format=info.source_format or "HDF5",
        )

    def poll(self) -> list[Chunk]:
        if self._inner is None:
            return []

        offset = self._position * self._frames_per_stack
        chunks = [Chunk(c.data, c.start + offset, c.end + offset) for c in self._inner.poll()]

        # Current timepoint fully read: advance to the next file if it exists.
        if self._inner.is_complete() and self._position + 1 < self._num_timepoints:
            next_path = self._build_path(self._position + 1)
            if next_path is not None and os.path.exists(next_path):
                self._inner.close()
                self._position += 1
                self._inner = Hdf5LiveSource(self._detector_name, self._chunk_size)
                self._inner.open(next_path)
        return chunks

    def is_complete(self) -> bool:
        if self._inner is None:
            return True
        if not self._inner.is_complete():
            return False
        # On the last expected timepoint with its file fully read -> done.
        if self._position + 1 >= self._num_timepoints:
            return True
        # Otherwise wait for the next timepoint file (live). If it isn't going
        # to appear, the user stops live mode; we don't guess completion here.
        return False

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
        self._inner = None

    def _build_path(self, position: int) -> str | None:
        if self._template is None:
            return None
        folder, prefix, width, suffix, _ = self._template
        index = self._first_index + position
        return os.path.join(folder, f"{prefix}{index:0{width}d}{suffix}")

    def _count_present_timepoints(self) -> int:
        count = 1
        while True:
            path = self._build_path(count)
            if path is None or not os.path.exists(path):
                break
            count += 1
        return count


class Hdf5LapseSource(LiveSource):
    """Polls a single-file scan{N} timelapse HDF5 as one continuous global frame stream."""

    def __init__(self, detector_name: str | None = None, chunk_size: int | None = None):
        """
        Args:
            detector_name: Detector name inside scan groups. If None, auto-detect.
            chunk_size: Override chunk size for polling. If None, use dataset.chunks[0].
        """
        self._detector_name = detector_name
        self._chunk_size_override = chunk_size
        self._path: str | None = None
        self._file = None
        self._scan_groups: list[str] = []
        self._current_group_index = 0
        self._global_cursor = 0
        self._local_cursor = 0
        self._chunk_size = 1
        self._frames_per_stack: int | None = None
        self._num_timepoints: int | None = None
        self._current_dataset = None
        self._current_dataset_path: str | None = None
        self._resolved_detector_name: str | None = None
        self._writing = True
        self._owns_file = False

    def open(self, path_or_handle: Any) -> StackInfo:
        """Open the HDF5 file in SWMR mode and return stack metadata."""
        self._global_cursor = 0
        self._local_cursor = 0
        self._current_group_index = 0
        
        if isinstance(path_or_handle, h5py.File):
            self._file = path_or_handle
            self._path = None
            self._owns_file = False
        else:
            path = Path(path_or_handle)
            self._path = str(path)
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
            self._owns_file = True
        
        # Enumerate scan{N} groups
        self._scan_groups = sorted(
            [key for key in self._file.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )
        
        if not self._scan_groups:
            raise ValueError("No scan{N} groups found in HDF5 file")
        
        # Open scan0 to detect detector and read metadata
        scan0 = self._file[self._scan_groups[0]]
        self._resolved_detector_name = self._detector_name or self._auto_detect_detector(scan0)
        
        # Open the detector dataset in scan0
        self._current_dataset, attrs = self._open_detector_dataset(scan0, self._resolved_detector_name)
        self._current_dataset_path = f'/{self._scan_groups[0]}/{self._resolved_detector_name}/data'
        
        if self._current_dataset.ndim != 3:
            raise ValueError(f"Expected 3D dataset (T, Y, X), got shape {self._current_dataset.shape}")
        
        # Determine chunk size
        if self._chunk_size_override is not None:
            self._chunk_size = max(1, int(self._chunk_size_override))
        elif self._current_dataset.chunks is not None and len(self._current_dataset.chunks) > 0:
            self._chunk_size = max(1, int(self._current_dataset.chunks[0]))
        else:
            self._chunk_size = 1
        
        # Extract frames_per_stack and num_timepoints
        self._frames_per_stack = self._coerce_int(attrs.get('recording:frames_per_stack'))
        if self._frames_per_stack is None:
            self._frames_per_stack = self._current_dataset.shape[0]
        
        self._num_timepoints = self._coerce_int(attrs.get('recording:num_timepoints'))
        self._refresh_state_from_attrs(attrs)
        
        # Calculate expected_frames
        expected_frames = None
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_frames = self._num_timepoints * self._frames_per_stack
        
        frame_shape = self._current_dataset.shape[-2:]
        
        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._current_dataset.dtype,
            attrs=dict(attrs),
            expected_frames=expected_frames,
            frames_per_stack=self._frames_per_stack,
            detector_name=attrs.get('recording:detector_name') or attrs.get('detector_name') or self._resolved_detector_name,
            dataset_path=attrs.get('recording:dataset_path'),
            source_format=attrs.get('recording:source_format') or 'HDF5',
        )

    def poll(self) -> list[Chunk]:
        """Return newly available chunks with GLOBAL indices across all scan groups."""
        if self._current_dataset is None:
            return []
        
        chunks = []
        
        while True:
            # Refresh current dataset and re-enumerate scan groups to detect new ones
            self._refresh_current_dataset()
            self._refresh_scan_groups()
            
            # Read current group's attributes
            current_group = self._file[self._scan_groups[self._current_group_index]]
            _, attrs = self._open_detector_dataset(current_group, self._resolved_detector_name)
            self._refresh_state_from_attrs(attrs)
            
            # Determine how many frames we can read from current group
            readable_length = min(self._current_dataset.shape[0], self._frames_per_stack or self._current_dataset.shape[0])
            
            if self._local_cursor >= readable_length:
                # Current group is fully read, try to advance to next
                if self._current_group_index + 1 < len(self._scan_groups):
                    # Check if next group exists
                    next_group_name = self._scan_groups[self._current_group_index + 1]
                    if next_group_name in self._file:
                        # Advance to next group
                        self._current_group_index += 1
                        self._local_cursor = 0
                        next_group = self._file[next_group_name]
                        self._current_dataset, _ = self._open_detector_dataset(next_group, self._resolved_detector_name)
                        self._current_dataset_path = f'/{next_group_name}/{self._resolved_detector_name}/data'
                        continue
                    else:
                        # Next group doesn't exist yet, wait
                        break
                else:
                    # No more groups expected
                    break
            
            # Read chunks from current group
            start_local = self._local_cursor
            end_local = min(start_local + self._chunk_size, readable_length)
            
            if start_local >= end_local:
                break
            
            # Calculate GLOBAL indices
            start_global = self._current_group_index * (self._frames_per_stack or 0) + start_local
            end_global = self._current_group_index * (self._frames_per_stack or 0) + end_local
            
            data = self._current_dataset[start_local:end_local]
            chunks.append(Chunk(data=data, start=start_global, end=end_global))
            
            self._local_cursor = end_local
            self._global_cursor = end_global
            
            # If we haven't filled a full chunk, stop polling (wait for more data)
            if end_local - start_local < self._chunk_size and end_local < readable_length:
                break
        
        return chunks

    def is_complete(self) -> bool:
        """Return whether all timepoints have been fully consumed."""
        if self._current_dataset is None:
            return True
        
        # If we know the total expected frames, check against global cursor
        if self._num_timepoints is not None and self._frames_per_stack is not None:
            expected_total = self._num_timepoints * self._frames_per_stack
            if self._global_cursor >= expected_total:
                return True
        
        # Check if we're on the last expected group
        if self._num_timepoints is not None:
            if self._current_group_index >= self._num_timepoints - 1:
                # We're on or past the last expected group
                self._refresh_current_dataset()
                current_group = self._file[self._scan_groups[self._current_group_index]]
                _, attrs = self._open_detector_dataset(current_group, self._resolved_detector_name)
                self._refresh_state_from_attrs(attrs)
                
                readable_length = min(self._current_dataset.shape[0], self._frames_per_stack or self._current_dataset.shape[0])
                
                if not self._writing and self._local_cursor >= readable_length:
                    return True
        
        return False

    def close(self) -> None:
        """Release source resources."""
        if self._owns_file and self._file is not None:
            self._file.close()
        self._path = None
        self._file = None
        self._current_dataset = None
        self._current_dataset_path = None
        self._scan_groups = []

    def _auto_detect_detector(self, scan_group: h5py.Group) -> str:
        """Auto-detect detector name from scan group contents."""
        for key in scan_group.keys():
            item = scan_group[key]
            if isinstance(item, h5py.Group) and 'data' in item:
                return key
        
        raise ValueError("No detector group with 'data' dataset found in scan group")

    def _open_detector_dataset(self, scan_group: h5py.Group, detector_name: str) -> tuple[h5py.Dataset, dict[str, Any]]:
        """Open detector dataset and collect flattened attributes."""
        if detector_name not in scan_group:
            raise ValueError(f"Detector '{detector_name}' not found in scan group")
        
        det_group = scan_group[detector_name]
        if not isinstance(det_group, h5py.Group):
            raise ValueError(f"'{detector_name}' is not a group")
        
        if 'data' not in det_group:
            raise ValueError(f"Detector group '{detector_name}' has no 'data' dataset")
        
        dataset = det_group['data']
        
        # Flatten attributes from root, detector group, and metadata
        attrs: dict[str, Any] = {}
        
        # Root attrs
        attrs.update(dict(self._file.attrs))
        
        # Dataset attrs
        attrs.update(dict(dataset.attrs))
        
        # Metadata group
        if 'metadata' in det_group:
            self._flatten_metadata(det_group['metadata'], attrs, prefix='')
        
        return dataset, attrs

    def _flatten_metadata(self, group: h5py.Group, attrs: dict[str, Any], prefix: str) -> None:
        """Recursively flatten metadata group into attrs dict with category prefixes."""
        for key in group.attrs.keys():
            flat_key = f"{prefix}{key}" if prefix else key
            attrs[flat_key] = group.attrs[key]
        
        for subgroup_name in group.keys():
            subgroup = group[subgroup_name]
            if isinstance(subgroup, h5py.Group):
                new_prefix = f"{subgroup_name}:" if not prefix else f"{prefix}{subgroup_name}:"
                self._flatten_metadata(subgroup, attrs, new_prefix)

    def _refresh_current_dataset(self) -> None:
        """Re-open current dataset to see new frames."""
        if self._path is None or self._current_dataset is None:
            return
        
        # Close and re-open file to refresh SWMR
        if self._owns_file and self._file is not None:
            self._file.close()
            self._file = h5py.File(self._path, 'r', libver='latest', swmr=True)
        
        current_group = self._file[self._scan_groups[self._current_group_index]]
        det_group = current_group[self._resolved_detector_name]
        self._current_dataset = det_group['data']
        self._current_dataset.refresh()

    def _refresh_scan_groups(self) -> None:
        """Re-enumerate scan groups to detect newly added ones."""
        if self._path is None:
            return
        
        self._scan_groups = sorted(
            [key for key in self._file.keys() if key.startswith('scan') and key[4:].isdigit()],
            key=lambda x: int(x[4:])
        )

    def _refresh_state_from_attrs(self, attrs: dict[str, Any]) -> None:
        """Update internal state from attributes."""
        self._writing = self._coerce_bool(attrs.get('writing'), default=self._writing)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)
