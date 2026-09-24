"""Lazy array wrappers for ImProcess input images."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import h5py
import numpy as np
import tifffile as tiff
import zarr

from imswitch.improcess.model.image_sources import ResolvedImage, is_zarr_array


class VirtualImageArray(Protocol):
    """Array-like lazy input contract used by DataObj."""

    shape: tuple[int, ...]
    dtype: np.dtype
    ndim: int
    chunks: tuple[int, ...] | None
    backend: str
    supports_lazy_indexing: bool

    def __getitem__(self, key: Any) -> np.ndarray: ...
    def asarray(self) -> np.ndarray: ...
    def __array__(self, dtype=None) -> np.ndarray: ...
    def close(self) -> None: ...


@dataclass
class VirtualImageSource:
    """Resolved lazy array plus source metadata."""

    name: str
    array: VirtualImageArray
    attrs: dict[str, Any] = field(default_factory=dict)
    array_path: str | None = None
    source_format: str | None = None
    axis_labels: list[str] | None = None
    axis_scales: list[float] | None = None
    scale_unit: str | None = None

    def close(self) -> None:
        self.array.close()


class _VirtualArrayBase:
    backend = "unknown"
    supports_lazy_indexing = True

    @property
    def ndim(self) -> int:
        return len(self.shape)

    def __array__(self, dtype=None) -> np.ndarray:
        array = self.asarray()
        if dtype is not None:
            return np.asarray(array, dtype=dtype)
        return array

    def to_dask(self, chunks: str | tuple[int, ...] = "native"):
        """Return a Dask array view when dask is installed."""
        import dask.array as da

        dask_chunks = self.chunks if chunks == "native" else chunks
        if dask_chunks is None:
            dask_chunks = self.shape
        return da.from_array(self, chunks=dask_chunks)

    def close(self) -> None:
        pass


class Hdf5VirtualArray(_VirtualArrayBase):
    backend = "hdf5"

    def __init__(self, dataset: h5py.Dataset):
        self._dataset = dataset

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self._dataset.shape)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self._dataset.dtype)

    @property
    def chunks(self) -> tuple[int, ...] | None:
        chunks = self._dataset.chunks
        return tuple(chunks) if chunks is not None else None

    def __getitem__(self, key: Any) -> np.ndarray:
        return np.asarray(self._dataset[key])

    def asarray(self) -> np.ndarray:
        return np.asarray(self._dataset[()])


class ZarrVirtualArray(_VirtualArrayBase):
    backend = "zarr"

    def __init__(self, array: Any):
        self._array = array

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self._array.shape)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self._array.dtype)

    @property
    def chunks(self) -> tuple[int, ...] | None:
        chunks = getattr(self._array, "chunks", None)
        return tuple(chunks) if chunks is not None else None

    def __getitem__(self, key: Any) -> np.ndarray:
        return np.asarray(self._array[key])

    def asarray(self) -> np.ndarray:
        return np.asarray(self._array[...])


class TiffVirtualArray(_VirtualArrayBase):
    backend = "tiff"

    def __init__(self, series: tiff.TiffPageSeries):
        self._series = series
        self._store = None
        self._array = None
        try:
            self._store = series.aszarr()
            self._array = zarr.open(self._store, mode="r")
            self.supports_lazy_indexing = True
        except Exception:
            self.supports_lazy_indexing = False

    @property
    def shape(self) -> tuple[int, ...]:
        if self._array is not None:
            return tuple(self._array.shape)
        return tuple(self._series.shape)

    @property
    def dtype(self) -> np.dtype:
        if self._array is not None:
            return np.dtype(self._array.dtype)
        return np.dtype(self._series.dtype)

    @property
    def chunks(self) -> tuple[int, ...] | None:
        if self._array is None:
            return None
        chunks = getattr(self._array, "chunks", None)
        return tuple(chunks) if chunks is not None else None

    def __getitem__(self, key: Any) -> np.ndarray:
        if self._array is not None:
            return np.asarray(self._array[key])
        # No lazy path: the whole series is decoded to serve any key. What is
        # returned must not be a view into that decode, or one displayed plane
        # keeps the entire series alive -- and a loop over planes holds the
        # previous series while decoding the next.
        return _detached(self.asarray(), key)

    def asarray(self) -> np.ndarray:
        if self._array is not None:
            return np.asarray(self._array[...])
        return np.asarray(self._series.asarray())

    def close(self) -> None:
        closer = getattr(self._store, "close", None)
        if callable(closer):
            closer()


def _detached(full: np.ndarray, key: Any) -> np.ndarray:
    """``full[key]``, copied when it is a view of part of ``full``."""
    selected = np.asarray(full[key])
    if selected.base is not None and selected.nbytes < full.nbytes:
        selected = selected.copy()
    return selected


class EagerVirtualArray(_VirtualArrayBase):
    """Compatibility fallback for array-like inputs without lazy slicing."""

    supports_lazy_indexing = False

    def __init__(
        self,
        array_like: Any,
        *,
        backend: str = "eager",
        shape: tuple[int, ...] | None = None,
        dtype: np.dtype | None = None,
    ):
        self.backend = backend
        self._array_like = array_like
        self._shape = tuple(shape) if shape is not None else tuple(np.shape(array_like))
        self._dtype = np.dtype(dtype if dtype is not None else np.asarray(array_like).dtype)

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    @property
    def chunks(self) -> tuple[int, ...] | None:
        return None

    def __getitem__(self, key: Any) -> np.ndarray:
        return np.asarray(self.asarray()[key])

    def asarray(self) -> np.ndarray:
        return np.asarray(self._array_like)


def virtual_source_from_resolved_image(image: ResolvedImage) -> VirtualImageSource:
    """Wrap a resolved image's native array handle in the lazy DataObj contract."""
    return VirtualImageSource(
        name=image.name,
        array=virtual_array_from_native(image.array),
        attrs=dict(image.attrs),
        array_path=image.array_path,
        source_format=image.source_format,
        axis_labels=image.axis_labels,
        axis_scales=image.axis_scales,
        scale_unit=image.scale_unit,
    )


def virtual_array_from_native(array: Any) -> VirtualImageArray:
    if isinstance(array, h5py.Dataset):
        return Hdf5VirtualArray(array)
    if is_zarr_array(array):
        return ZarrVirtualArray(array)
    if isinstance(array, tiff.TiffPageSeries):
        return TiffVirtualArray(array)
    return EagerVirtualArray(array, backend=type(array).__name__)


__all__ = [
    "EagerVirtualArray",
    "Hdf5VirtualArray",
    "TiffVirtualArray",
    "VirtualImageArray",
    "VirtualImageSource",
    "ZarrVirtualArray",
    "virtual_array_from_native",
    "virtual_source_from_resolved_image",
]


# Copyright (C) 2020-2026 ImSwitch developers
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
