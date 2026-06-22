"""Format-agnostic live source contracts and in-memory batch fallback."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

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

        return StackInfo(
            frame_shape=frame_shape,
            dtype=self._array.dtype,
            attrs=dict(attrs),
            expected_frames=self._expected_frames,
            frames_per_stack=self._coerce_int(attrs.get('recording:frames_per_stack')),
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
