# improcess OME/Bio-Formats Read-Compatibility Audit

Status: **audit / findings only** (no code changed). Companion to the write-side
work in `docs/recording_ome_standardization_plan.md`.

Goal asked: improcess should read **not only ImSwitch's own** recordings but **any
Bio-Formats / OME file** (OME-TIFF, OME-Zarr from `bioformats2raw`/OMERO/Fiji, …).

## TL;DR

The improcess read path is hard-wired to **ImSwitch's own historical layout** (a single
3D `(frames, Y, X)` array, named `data` for structured groups, with pixel size carried as
the Fiji `element_size_um` attribute). It does **not** parse OME metadata at all — there is
**zero** awareness of OME-XML, NGFF `multiscales`, TIFF series, channels, or
`coordinateTransformations` anywhere in the reader (verified by grep). Consequences:

- A standard **multi-resolution OME-Zarr** (the `bioformats2raw` norm) **fails to load**
  (`RuntimeError: File contains multiple datasets`).
- A standard **OME-TIFF with >3 dims (e.g. TCYX)** loads its raw pixels but its axes,
  channels and pixel sizes are **silently ignored** → downstream (which assumes
  `(frames, Y, X)`) misinterprets it.
- Our *own* new OME-NGFF output is readable **only because Phase 3 deliberately kept the
  array named `data`**. The moment that array is named `0` (the OME convention), the
  current readers break — so generalizing the reader is needed regardless.

## Read paths in improcess

| Path | Entry | Reads |
|---|---|---|
| Batch / open-a-file | `DataObj` (`model/DataObj.py`), via `FileIOController` | HDF5, TIFF, Zarr |
| Live / watch-folder | `make_live_source` → `ZarrLiveSource`/`Hdf5LiveSource` (+ `*LapseSource`) (`live/sources.py`, `live/source_factory.py`) | Zarr, HDF5 (TIFF explicitly **unsupported** for live) |
| Live-completion detection | `LiveModeController`, `MemoryLiveController` | zarr/h5py groups, key off `data` |

## Findings

### F1 — OME-Zarr from external tools: largely unreadable
`DataObj._is_structured_detector_group` / `_resolve_dataset` (DataObj.py:161-191) and
`ZarrLiveSource._open_array` (sources.py:328-352) hard-code the array name **`data`** and
**never read `multiscales`**. Standard OME-Zarr stores the image as resolution arrays
`0`, `1`, `2`, … under an `ome.multiscales` group attribute — there is no `data` array.

Empirically (this audit):
- Single-level OME-Zarr (`0` only): `getDatasetNames → ['0']`, loads `0` **by accident**
  (treated as a bare array), but axes/scale from `multiscales` are dropped.
- **Multi-resolution OME-Zarr (`0`,`1`): `getDatasetNames → ['0','1']` →
  `RuntimeError: File contains multiple datasets`.** The pyramid levels are mistaken for
  independent datasets; there is no "pick full-res level 0" logic.
- `bioformats2raw.layout=3` collections (series as subgroups, optional `OME/` group):
  the series subgroups are neither arrays nor `data`-groups → `_dataset_names` returns
  nothing → "File does not contain any datasets".

### F2 — OME-TIFF: pixels load, metadata ignored
`DataObj.data` for TIFF is `self._file.asarray()` (DataObj.py:48) — returns series[0]'s
raw array. The OME-XML is never parsed:
- **Axes not read.** A TCYX/TZYX/CZYX file is returned as a raw N-D array; `numFrames`
  uses `shape[0]` (DataObj.py:78) and downstream assumes `(frames, Y, X)`. Empirically a
  `(2,3,64,64)` TCYX OME-TIFF loads as 4D with no axis info (tifffile *does* expose
  `series[0].axes == 'TCYX'`, but improcess never asks).
- **Pixel size not read.** `DataObj.attrs` only handles h5py/zarr, not `TiffFile`
  (DataObj.py:54-66) → TIFF files carry no scale/units into processing.
- **Multi-series ignored.** `asarray()` flattens to one array; `getDatasetNames` returns
  the literal `['default']` (DataObj.py:117) — no series/scene selection.
- **Pyramidal OME-TIFF** (SubIFDs) not handled (full-res assumed implicitly).

### F3 — Axis model is fixed to "leading frame axis + YX"
Nothing normalizes axis order. The pipeline assumes a 3D `(T|Z, Y, X)` stack with the
frame axis first. Channels (`C`) are never separated, and arbitrary OME orders (e.g.
`XYCZT`-stored data) are not transposed to a canonical order. Notably the **result** model
(`model/result.py`) *does* support `axis_labels` + `scale` + `transpose` — but the
**input** `DataObj` never derives them from file metadata, so that capability is unused on
load.

### F4 — Pixel scale source mismatch
improcess (where it uses scale at all, e.g. FRC) takes pixel size as a manual parameter;
on load it would only find scale in the Fiji `element_size_um` array attr (ImSwitch/Fiji
HDF5). It does **not** read NGFF `coordinateTransformations.scale` or OME-TIFF
`PhysicalSizeX/Y/Z` — so externally-authored OME files contribute no calibration.

### F5 — Our own forward-compatibility
Phase 3 kept the NGFF array named `data` specifically so these readers keep working. That
is a deliberate stopgap; a conventional `0`-named OME-Zarr (ours or external) is not
readable. Fixing F1 also removes that constraint.

## Compatibility matrix

| Source | Batch (`DataObj`) | Live source |
|---|---|---|
| ImSwitch HDF5/Zarr/TIFF (current + new OME) | ✅ (data named `data`) | ✅ zarr/hdf5 |
| External OME-Zarr, single level | ⚠️ loads pixels, drops axes/scale | ❌ (`data` not found) |
| External OME-Zarr, multi-resolution | ❌ "multiple datasets" | ❌ |
| External OME-Zarr collection (layout=3 / HCS) | ❌ no datasets | ❌ |
| OME-TIFF ≤3D simple | ⚠️ pixels only, no axes/scale | n/a (TIFF unsupported) |
| OME-TIFF ≥4D / multi-series / pyramidal | ❌ misread as `(frames,Y,X)` | n/a |
| Non-OME Bio-Formats (ND2/CZI/LIF/…) | ❌ unsupported extensions | ❌ |

## Recommendations (prioritized)

1. **Generalize the Zarr image-array resolver (highest value).** When a group carries
   `ome.multiscales`, resolve the image array from `multiscales[0].datasets[0].path`
   (full-res level), reading both **0.4** (`.zattrs`, zarr v2) and **0.5**
   (`zarr.json attributes.ome`, zarr v3); fall back to legacy `data`. Read axes + scale
   from the `axes` / `coordinateTransformations`. Stop treating pyramid levels as separate
   datasets. This fixes F1 + F5 and unblocks `bioformats2raw`/OMERO/napari output.
2. **Read OME-TIFF via tifffile's OME support.** Use `TiffFile.series` (+ `series.axes`)
   and `ome_metadata` to (a) select a series, (b) expose axes, (c) read `PhysicalSize*`.
   Handle pyramidal series (`series.levels`). Fixes F2.
3. **Add an axis-normalization layer on load.** Parse the source axes (OME/NGFF) and
   populate the existing `result.py` `axis_labels` + `scale` + `transpose` so the rest of
   improcess sees a canonical order and real calibration. Separate `C` properly. Fixes
   F3/F4.
4. **Consolidate the array-resolution + axis/scale logic** into one helper shared by
   `DataObj`, the live sources, and `LiveModeController` (today the `data`-name assumption
   is duplicated in ~6 places) — so "understands OME" is implemented once.
5. **Decide the scope of "all Bio-Formats".**
   - *OME-native (recommended first):* OME-TIFF + OME-Zarr are covered by libs already in
     the stack (`tifffile`, `zarr`, and the declared `ome-zarr` dep). This satisfies the
     OME-standard goal with no heavy new deps.
   - *Broader proprietary formats (ND2/CZI/LIF/…):* requires a real reader — either
     **`bioio`** (the maintained successor to `aicsimageio`, pure-Python plugin readers,
     returns xarray with dims+coords) as an optional backend, or a **Bio-Formats bridge**
     (`bioformats`/`scyjava` via a JVM) which is heavyweight and JRE-dependent. Recommend
     `bioio` behind a feature flag if/when non-OME formats are needed; do **not** put a JVM
     on the default path.

## Suggested validation (when implementing)

Round-trip read tests against: (a) `bioformats2raw` OME-Zarr (single + pyramidal + a
2-series collection), (b) an OME-TIFF written by Fiji and by `bioformats2raw`, with TCZYX
axes and non-unit `PhysicalSize`; assert improcess loads full-res, correct axis labels,
and pixel scale. Keep the existing ImSwitch-format tests green (back-compat).
