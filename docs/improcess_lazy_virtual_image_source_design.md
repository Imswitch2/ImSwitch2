# ImProcess Lazy / Virtual Image Source Design

Status: phase 1 implemented; first low-risk phase 2 consumer implemented

## Problem

`DataObj` is format-aware. Historically, `DataObj.data` materialized the selected
dataset immediately:

- HDF5: `np.array(dataset[:])`
- TIFF / OME-TIFF: `np.array(series.asarray())`
- Zarr / OME-Zarr: `np.array(array)`

That is safe for existing consumers, but it makes large TIFF, OME-TIFF, HDF5,
Zarr, and OME-Zarr inputs expensive before a reconstructor or processor has a
chance to select a subset, compute projections, or stream chunks.

The implemented phase 1 introduces a lazy/virtual image source layer without
breaking the existing contract that `DataObj.data` returns a NumPy array.

## Compatibility rules

These rules should be treated as hard constraints for the first implementation:

1. `DataObj.data` remains the legacy materialization boundary and returns a
   NumPy array.
2. Existing reconstructors and processors that call `data_obj.data` continue to
   work unchanged.
3. `resolve_image(...).array` keeps returning the native resolved backend object
   for now, because live sources already depend on that behavior.
4. New lazy-aware code must opt in through a new API, not by changing the type of
   `DataObj.data`.
5. File/store lifetime must be explicit. A lazy result must not depend on a
   `DataObj` whose `checkAndUnloadData()` has already closed the backing file.

## Proposed API

Add a new module:

```text
imswitch/improcess/model/virtual_image.py
```

Core protocol:

```python
from typing import Any, Protocol
import numpy as np


class VirtualImageArray(Protocol):
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
```

Metadata container:

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VirtualImageSource:
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
```

`VirtualImageSource` is deliberately close to the existing `ResolvedImage`
dataclass. The resolver can stay native, while `DataObj` builds a virtual source
from the resolved image.

## DataObj integration

Add new opt-in properties/methods:

```python
class DataObj:
    @property
    def data_handle(self) -> VirtualImageArray:
        """Lazy array-like handle for new code."""

    @property
    def data_source(self) -> VirtualImageSource:
        """Lazy array plus metadata and ownership."""

    @property
    def sourceLoaded(self) -> bool:
        """True when the file/store is open and the image is resolved."""

    @property
    def dataMaterialized(self) -> bool:
        """True when the legacy NumPy cache exists."""

    def checkAndOpenData(self) -> None:
        """Open the file/store and resolve the lazy source only."""
```

Keep existing API behavior:

```python
    @property
    def data(self):
        if self._data is None:
            self.checkAndOpenData()
            self._data = self.data_handle.asarray()
        return self._data

    @property
    def dataLoaded(self):
        # Preserve legacy meaning: the NumPy cache exists.
        return self._data is not None

    def checkAndLoadData(self):
        # Preserve legacy behavior: open and materialize.
        if not self.dataLoaded:
            self.checkAndOpenData()
            _ = self.data
```

`numFrames`, `axis_labels`, `axis_scales`, `scale_unit`, and `source_info` should
use `data_source.array.shape` / source metadata when available, so metadata and
shape checks do not force materialization.

## Backend wrappers

### HDF5

Wrap `h5py.Dataset`.

Behavior:

- `shape`, `dtype`, `ndim`, `chunks` delegate to the dataset.
- `__getitem__` returns `dataset[key]`.
- `asarray()` returns `dataset[()]`.
- `close()` is a no-op for the dataset wrapper itself; `DataObj` owns the file
  in phase 1.

Notes:

- Structured detector groups still resolve to the nested `data` dataset.
- Metadata flattening remains in `image_sources.py`.

### Zarr / OME-Zarr

Wrap `zarr.Array`.

Behavior:

- `shape`, `dtype`, `ndim`, `chunks` delegate to the array.
- `__getitem__` returns `array[key]`.
- `asarray()` returns `np.asarray(array[:])` or `array[...]`, depending on the
  Zarr version.
- `close()` is normally a no-op; the store is owned by the opened group.

Notes:

- OME-NGFF multiscale selection stays in `image_sources.py`.
- Phase 1 should continue selecting the first/full-resolution level, matching
  current behavior.

### TIFF / OME-TIFF

Prefer `tifffile`'s Zarr bridge:

```python
store = series.aszarr()
array = zarr.open(store, mode="r")
```

Wrap that Zarr array and keep the `ZarrTiffStore` alive.

Behavior:

- `shape`, `dtype`, `ndim`, `chunks` come from the Zarr view.
- `__getitem__` reads only the requested chunk/page region through the store.
- `asarray()` materializes the selected series.
- `close()` closes the Zarr TIFF store when supported.

Fallback:

- If `series.aszarr()` fails for an unusual TIFF, use an eager fallback wrapper.
- The fallback exposes shape/dtype metadata but marks
  `supports_lazy_indexing=False`; `__getitem__` may materialize internally.
- This avoids breaking compatibility while still giving good behavior for
  standard TIFF and OME-TIFF.

## Optional Dask bridge

The core layer should not require Dask, but it can expose an optional bridge:

```python
def to_dask(self, chunks="native"):
    import dask.array as da
    return da.from_array(self, chunks=self.chunks or chunks)
```

Use this later for lazy viewer-facing `ProcessingResult.data`, because napari is
much better at lazy display with Dask arrays than with arbitrary HDF5/TIFF
wrappers. This should be opt-in and tested separately from `DataObj.data`.

## Lifetime model

Phase 1:

- `DataObj` owns the open file/store.
- `data_handle` is only valid while the `DataObj` is loaded/open.
- `checkAndUnloadData()` closes the file/store, clears the lazy source, and
  clears the materialized NumPy cache. Axis metadata is retained for current
  compatibility with existing `DataObj` tests and viewer paths.
- Existing reconstructors still materialize through `DataObj.data`, so no lazy
  handle escapes into a `ProcessingResult`.

Phase 2:

- If a `ProcessingResult` stores a lazy array, it must own a closeable source.
- Add one of these before returning lazy view-only results:
  - `ProcessingResult.close()` with controller-owned lifetime management; or
  - `DataObj.detach_data_source()` to transfer ownership to the result.
- Do not pass a `DataObj`-owned lazy array into a result and then call
  `DataObj.checkAndUnloadData()`.

## Migration plan

### Phase 1: add the layer with no behavior change

Implemented:

1. Added `virtual_image.py` wrappers.
2. Added `DataObj.data_source`, `DataObj.data_handle`, `sourceLoaded`,
   `dataMaterialized`, and `checkAndOpenData()`.
3. Kept `DataObj.data` and `checkAndLoadData()` materializing as the legacy
   compatibility path.
4. Changed `numFrames` to use the source shape when available.
5. Added tests proving `data_handle` can inspect shape/dtype/slices without
   populating `_data`.

This phase is low risk and should not require reconstructor changes.

### Phase 2: opt in low-risk consumers

Convert consumers that can naturally work slice-by-slice:

- metadata inspection and dataset picker paths;
- view-only previews for Zarr and TIFF via Dask-backed display;
- projection/segmentation widgets when operating on a selected 2D plane;
- `getMeanData()` as a chunked mean for large arrays. **Implemented.**

Leave MoNaLISA and SNOUTY reconstructors materializing until their kernels have
explicit chunk/stream contracts.

### Phase 3: lazy `ProcessingResult` support

Allow `ProcessingResult.data` to be:

- NumPy arrays for computed results;
- Dask arrays for lazy display;
- virtual arrays only behind `DisplayLayerSpec` or explicit result classes that
  own their source lifetime.

Update viewer code to avoid unconditional `np.asarray(...)` on display layers
when lazy data is already viewer-compatible.

### Phase 4: replace batch fallbacks where useful

For very large raw recordings, use existing live/streaming contracts even for
closed files:

- HDF5/Zarr/TIFF source reads chunks from `VirtualImageArray`.
- `StreamingReconstructor` sessions consume those chunks.
- Plain reconstructors still get a materialized NumPy array through the legacy
  batch path.

## Test matrix

Add focused tests for each backend:

- HDF5 plain dataset: lazy shape/dtype/slice, then materialized equality.
- HDF5 structured detector group: metadata flattening and lazy data access.
- Zarr plain array: lazy shape/dtype/slice, then materialized equality.
- OME-Zarr root multiscale: metadata, full-resolution selection, lazy slice.
- OME-TIFF single series: axes/scales metadata and lazy slice through
  `aszarr()`.
- OME-TIFF multi-series: named series selection and materialization equality.
- Fallback TIFF path: still materializes correctly if lazy bridge is unavailable.
- `checkAndUnloadData()` invalidates the lazy source and closes resources.
- Existing `DataObj.data` tests remain unchanged.

## Acceptance criteria for phase 1

- All current `improcess` tests that do not hit unrelated known failures still
  pass.
- Existing `DataObj.data` callers observe a NumPy array with unchanged values.
- New `data_handle` tests prove HDF5/Zarr/TIFF shape and slices are accessible
  without filling `_data`.
- `DataObj.numFrames` no longer materializes the full array once the source is
  open.
- `checkAndUnloadData()` releases file handles and clears both lazy and eager
  caches.

## Risks

- TIFF lazy slicing quality depends on `tifffile.aszarr()` support for the file.
  Keep a safe eager fallback.
- HDF5 file handles cannot be closed while lazy arrays are still in use.
  Lazy results therefore need explicit ownership before phase 3.
- Some processors use `np.asarray(result.data)` immediately. That is fine for
  compatibility, but it means lazy results only pay off after targeted processor
  updates.
- Napari lazy display should use Dask arrays rather than arbitrary custom
  wrappers.
