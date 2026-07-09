# improcess OME/Bio-Formats Read-Compatibility Audit

Status: **implementation in progress, core batch compatibility implemented**.
The source locator, shared image resolver, `DataObj` metadata bridge, view-only
metadata propagation, batch OME-NGFF Zarr reading, and batch OME-TIFF
series/metadata reading are implemented with focused regressions. Single-store
`ZarrLiveSource` now reuses the shared resolver where live semantics allow it.
Companion to the write-side work in `docs/recording_ome_standardization_plan.md`.

The audit findings below describe the baseline behavior observed before the
implementation started. The implementation phase list tracks what has been
fixed or started.

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
- The quick-load / drag-drop path has a more basic locator problem: selecting a chunk
  inside a `.zarr` store (e.g. `.../recording.zarr/APD/data/0.0.0`) sends that chunk file
  to `DataObj`, which sees suffix `.0` and rejects it. File-vs-folder handling is currently
  a legacy UI decision, not a shared source-resolution contract.

## Read paths in improcess

| Path | Entry | Reads |
|---|---|---|
| Batch / open-a-file | `DataObj` (`model/DataObj.py`), via `FileIOController` | HDF5, TIFF, Zarr |
| Live / watch-folder | `make_live_source` → `ZarrLiveSource`/`Hdf5LiveSource` (+ `*LapseSource`) (`live/sources.py`, `live/source_factory.py`) | Zarr, HDF5 (TIFF explicitly **unsupported** for live) |
| Live-completion detection | `LiveModeController`, `MemoryLiveController` | zarr/h5py groups, key off `data` |

## Findings

### F0 — File/folder source selection is not centralized
Quick-load currently decides whether to open a file dialog or folder dialog from the
legacy parameter-tree value `File extension` (`FileIOController.quickLoadData`). That
logic is format-string based (`zarr`→folder, `hdf5`→file) and is not derived from the
active reconstructor's declared inputs. Drag-drop is extension based too. Consequences:

- View-only, MoNaLISA, Snouty, and future reconstructors each inherit brittle loader
  behavior instead of a shared "what is a valid dataset source?" contract.
- Selecting or dropping a path *inside* a `.zarr` store can pass a chunk path like
  `.../data/0.0.0` to `DataObj`, which fails with `Unsupported file extension ".0"`.
- The later OME-Zarr work needs the same source normalization anyway: open the `.zarr`
  store root first, then resolve image arrays from OME metadata.

This should be fixed before deeper OME parsing: normalize incoming paths to a dataset
source root, then enumerate datasets/images.

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
| External OME-Zarr, single level | ✅ full-res pixels + axes/scale | ✅ for completed/growing 3D single-store arrays |
| External OME-Zarr, multi-resolution | ✅ full-res level selected, pyramid levels hidden | ✅ for completed/growing 3D single-store arrays |
| External OME-Zarr collection (layout=3 / HCS) | ⚠️ child image groups supported; broad HCS not fully validated | ❌ lapse/HCS live paths still strict |
| OME-TIFF ≤3D simple | ✅ pixels + series axes/physical size when available | n/a (TIFF unsupported) |
| OME-TIFF ≥4D / multi-series / pyramidal | ⚠️ multi-dimensional and multi-series supported; pyramidal level policy still minimal/full-res | n/a |
| Non-OME Bio-Formats (ND2/CZI/LIF/…) | ❌ unsupported extensions | ❌ |

## Recommendations (prioritized)

1. **Create one dataset-source resolver (highest leverage).** Move file-vs-folder,
   extension support, `.zarr` root detection, and dataset enumeration into a shared helper
   used by quick-load, drag-drop, multidata load, live source setup, and `DataObj`.
   Reconstructors should declare what they accept; the app should decide how to locate it.
   Initial source specs:
   - HDF5: suffixes `.h5`, `.hdf5`, `.hdf`, locator `file`.
   - TIFF/OME-TIFF: suffixes `.tif`, `.tiff`, `.ome.tif`, `.ome.tiff`, locator `file`.
   - Zarr/OME-Zarr: suffix `.zarr`, locator `directory/store`, with path normalization
     that climbs from any child path back to the nearest `.zarr` ancestor.
   This fixes F0 and gives the OME work a single stable entry point.
2. **Generalize the Zarr image-array resolver.** When a group carries OME-NGFF metadata,
   resolve the full-resolution image array from `multiscales[0].datasets[0].path`, reading
   both **0.4-style** root `multiscales` metadata and **0.5-style** `ome.multiscales`;
   fall back to legacy `data`. Read axes + scale from the `axes` /
   `coordinateTransformations`. Stop treating pyramid levels as independent datasets. This
   fixes F1 + F5 and unblocks `bioformats2raw`/OMERO/napari output.
3. **Read OME-TIFF via tifffile's OME support.** Use `TiffFile.series` (+ `series.axes`)
   and `ome_metadata` to (a) select a series, (b) expose axes, (c) read `PhysicalSize*`.
   Handle pyramidal series (`series.levels`). Fixes F2.
4. **Expose loaded axis/scale metadata to reconstructors.** Add `DataObj.axis_labels`,
   `DataObj.axis_scales`, and `DataObj.scale_unit` populated by the source resolver /
   reader metadata. Update View-only to use these instead of inventing labels from ndim.
   Processors already understand `axis_labels` and `axis_scales` through `ProcessingResult`;
   the input side is the missing bridge. Fixes F3/F4 for pass-through and makes the data
   contract explicit for other reconstructors.
5. **Share the resolver with live completion/source code.** `ZarrLiveSource`,
   `Hdf5LiveSource`, `LiveModeController`, and `MemoryLiveController` duplicate the
   `data`-array assumption. After batch reading is correct, move their array/metadata lookup
   to the same resolver where live semantics allow it. Keep live scope narrower than batch:
   a live source may require one growing full-resolution array.
6. **Decide the scope of "all Bio-Formats".**
   - *OME-native (recommended first):* OME-TIFF + OME-Zarr are covered by libs already in
     the stack (`tifffile`, `zarr`, and the declared `ome-zarr` dep). This satisfies the
     OME-standard goal with no heavy new deps.
   - *Broader proprietary formats (ND2/CZI/LIF/…):* requires a real reader — either
     **`bioio`** (the maintained successor to `aicsimageio`, pure-Python plugin readers,
     returns xarray with dims+coords) as an optional backend, or a **Bio-Formats bridge**
     (`bioformats`/`scyjava` via a JVM) which is heavyweight and JRE-dependent. Recommend
     `bioio` behind a feature flag if/when non-OME formats are needed; do **not** put a JVM
     on the default path.

## Proposed implementation phases

1. **Dataset-source resolver / locator.** ✅ Implemented.
   - Introduce a small module, e.g. `imswitch/improcess/model/dataset_sources.py`.
   - Define source specs from reconstructor `file_extensions` plus central suffix rules.
   - Normalize paths: if any path has a `.zarr` ancestor, use that ancestor as the store.
   - Replace quick-load's `zarr` folder special-case with resolver-driven dialog choice.
   - Use the resolver in drag-drop and multidata load before calling `DataObj`.
   - Regression: dropping/selecting `.../recording.zarr/APD/data/0.0.0` loads
     `.../recording.zarr`.
   - Test coverage added for resolver rules, quick-load zarr folder selection,
     `.zarr` child-path routing, and `DataObj` loading from a normalized zarr store.

2. **Shared Zarr/NGFF image resolver for batch.** ✅ Implemented for batch.
   - Return a `ResolvedImage` with array handle, logical image name, array path, axes,
     scale, attrs, and optional pyramid level metadata.
   - Support legacy ImSwitch `detector/data`, bare arrays, OME-NGFF 0.4 `multiscales`,
     OME-NGFF 0.5 `ome.multiscales`, and root `ome.series` / series subgroups.
   - `DataObj.getDatasetNames()` should list logical images, not pyramid levels.
   - Implemented in `imswitch/improcess/model/image_sources.py`. Batch `DataObj` zarr
     reads now collapse a root NGFF pyramid to one logical full-resolution image and list
     child NGFF image groups as logical datasets. Legacy ImSwitch zarr array and
     detector-group layouts remain covered.

3. **DataObj metadata bridge.** ✅ Implemented.
   - Add `axis_labels`, `axis_scales`, `scale_unit`, and possibly `source_info`.
   - Preserve old behavior as fallback (`["T","Y","X"]`/unit pixels where no metadata exists).
   - Update View-only to use `DataObj` metadata.
   - Implemented for HDF5/Zarr/TIFF. View-only now propagates source labels/scales into
     `ProcessingResult` when dimensions match.
   - HDF5 axis labels: our OME recordings now write an explicit `axes` attr on the
     `data` array, read by `_axis_labels_from_hdf5_attrs`. Without it (pre-OME files)
     the reader falls back to `default_axis_labels`, so a timelapse would otherwise
     read as `C,Y,X`; new recordings read `T,Y,X`. Verified end-to-end by the
     write→read sweep `test_ome_recording_roundtrip.py`.

4. **OME-TIFF batch reader.** ✅ Implemented for series/full-resolution reads.
   - Enumerate TIFF series as logical datasets instead of hard-coded `default`.
   - Load selected series/level through `tifffile.series`.
   - Extract axes and physical sizes from OME metadata / tifffile metadata.
   - Implemented for named OME-TIFF series, N-D axes from `series.axes`, and
     `PhysicalSizeX/Y/Z` calibration from OME metadata. Pyramidal SubIFD policy remains
     minimal: the selected tifffile series is read at its default/full-resolution level.

5. **Live/read-completion reuse.** 🚧 Partially implemented.
   - Replace the duplicated `data` lookup in `ZarrLiveSource`, `LiveModeController`, and
     memory paths with the shared resolver where possible.
   - Keep live validation strict: one writable 3D full-resolution array is acceptable first.
   - Implemented for single-store `ZarrLiveSource` array detection/opening. Lapse sources,
     HDF5 live source, and completion gates remain intentionally conservative and should
     be migrated only with dedicated live tests for each layout.

## Suggested validation (when implementing)

Round-trip read tests against: (a) `bioformats2raw` OME-Zarr (single + pyramidal + a
2-series collection), (b) an OME-TIFF written by Fiji and by `bioformats2raw`, with TCZYX
axes and non-unit `PhysicalSize`; assert improcess loads full-res, correct axis labels,
and pixel scale. Keep the existing ImSwitch-format tests green (back-compat).

Add focused source-locator tests:
- quick-load/dialog policy is derived from the active reconstructor's accepted formats.
- path normalization maps `.zarr` children/chunks back to the `.zarr` store root.
- unsupported child paths fail with a useful message rather than extension `.0`.
