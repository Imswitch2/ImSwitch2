"""Deferred sliced view over an array-like object.

Shared by any model/processor code that needs to hand out a lazy subset of
a (possibly lazy/virtual) source array without materializing it. Lives in
``model`` rather than a specific processor package so both processors and
other model code (e.g. ``ArrayProcessingResult.duplicate``) can depend on it
without a processors -> model -> processors import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np


class LazySubsetArray:
    """Deferred sliced view over an array-like object."""

    supports_lazy_indexing = True

    def __init__(
        self,
        source,
        base_key: tuple[slice, ...],
        *,
        source_shape: Sequence[int],
    ):
        self._source = source
        self._base_key = base_key
        self._source_shape = tuple(int(size) for size in source_shape)
        self._shape = tuple(
            _slice_length(selection, size)
            for selection, size in zip(base_key, self._source_shape, strict=True)
        )
        self.backend = f"{getattr(source, 'backend', type(source).__name__)}-subset"

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def ndim(self) -> int:
        return len(self._shape)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(getattr(self._source, "dtype", np.asarray(self.asarray()).dtype))

    @property
    def chunks(self) -> tuple[int, ...] | None:
        return None

    def __getitem__(self, key: Any) -> np.ndarray:
        composed = self._compose_key(key)
        if composed is None:
            return np.asarray(self.asarray()[key])
        return np.asarray(self._source[composed])

    def asarray(self) -> np.ndarray:
        return np.asarray(self._source[self._base_key])

    def __array__(self, dtype=None) -> np.ndarray:
        array = self.asarray()
        if dtype is not None:
            return np.asarray(array, dtype=dtype)
        return array

    def close(self) -> None:
        pass

    def _compose_key(self, key: Any) -> tuple[Any, ...] | None:
        expanded = _expand_key(key, self.ndim)
        if expanded is None:
            return None
        composed = []
        for axis_key, base_slice, source_size, subset_size in zip(
            expanded,
            self._base_key,
            self._source_shape,
            self._shape,
            strict=True,
        ):
            composed_axis = _compose_axis_key(
                axis_key,
                base_slice,
                source_size=source_size,
                subset_size=subset_size,
            )
            if composed_axis is None:
                return None
            composed.append(composed_axis)
        return tuple(composed)


def is_dask_array(data: Any) -> bool:
    return type(data).__module__.split(".", 1)[0] == "dask"


def plane_chunks(shape: Sequence[int]) -> tuple[int, ...]:
    """One chunk per 2-D plane: what a viewer reads when it shows one."""
    shape = tuple(int(size) for size in shape)
    if len(shape) <= 2:
        return shape
    return (1,) * (len(shape) - 2) + shape[-2:]


def display_array(data: Any):
    """``data`` in a form the viewer can transpose.

    An ndarray or a dask array is returned as it is. A lazy source that owns
    its files and says so (``display_lazily``) gets a dask view chunked by
    plane, so napari reads the plane on screen and nothing else. Any other
    lazy view is read now: it borrows a handle that closes when another file
    is loaded, and a read deferred to the next slider move would fail then.
    Something that transposes itself is left to do so.
    """
    if isinstance(data, np.ndarray) or is_dask_array(data):
        return data
    to_dask = getattr(data, "to_dask", None)
    shape = getattr(data, "shape", None)
    if getattr(data, "display_lazily", False) and callable(to_dask) and shape is not None:
        try:
            return to_dask(chunks=plane_chunks(shape))
        except ImportError:
            pass
    if hasattr(data, "transpose"):
        return data
    return np.asarray(data)


def identity_lazy_view(source, *, source_shape: Sequence[int]) -> LazySubsetArray:
    """Wrap ``source`` in a full-range LazySubsetArray (no cropping)."""
    shape = tuple(int(size) for size in source_shape)
    base_key = tuple(slice(None) for _ in shape)
    return LazySubsetArray(source, base_key, source_shape=shape)


def _slice_length(selection: slice, size: int) -> int:
    start, stop, step = selection.indices(size)
    if step > 0:
        return max(0, (stop - start + step - 1) // step)
    return max(0, (start - stop - step - 1) // (-step))


def _expand_key(key: Any, ndim: int) -> tuple[Any, ...] | None:
    if not isinstance(key, tuple):
        key = (key,)
    if any(part is None for part in key):
        return None
    if Ellipsis in key:
        ellipsis_index = key.index(Ellipsis)
        before = key[:ellipsis_index]
        after = key[ellipsis_index + 1:]
        fill = (slice(None),) * (ndim - len(before) - len(after))
        key = before + fill + after
    if len(key) > ndim:
        return None
    return key + (slice(None),) * (ndim - len(key))


def _compose_axis_key(
    axis_key: Any,
    base_slice: slice,
    *,
    source_size: int,
    subset_size: int,
):
    base_start, _base_stop, base_step = base_slice.indices(source_size)
    if isinstance(axis_key, int):
        index = axis_key + subset_size if axis_key < 0 else axis_key
        if index < 0 or index >= subset_size:
            raise IndexError("Lazy subset index out of range")
        return base_start + index * base_step
    if not isinstance(axis_key, slice):
        return None
    start, stop, step = axis_key.indices(subset_size)
    if step <= 0:
        return None
    return slice(
        base_start + start * base_step,
        base_start + stop * base_step,
        base_step * step,
    )


__all__ = [
    "LazySubsetArray",
    "display_array",
    "identity_lazy_view",
    "is_dask_array",
    "plane_chunks",
]
