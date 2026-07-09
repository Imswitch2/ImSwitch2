# Recording File-Format OME Standardization Plan

Status: **proposal / not yet implemented**
Scope: `imswitch/imcontrol/model/managers/RecordingManager.py` (the three `Storer`
subclasses) + the improcess readers (`DataObj`, `ZarrLiveSource`).

## 1. Goal

Bring all three recording formats (TIFF, HDF5, Zarr) to recognized **standards
(OME where one exists)**, driven by a **single shared metadata model** so the same
recording carries the *same logical metadata* regardless of container. Today each
format invents its own layout and metadata convention, none of them OME, and the
TIFF streaming path writes no metadata at all.

Non-goal: reducing Zarr's on-disk file/folder count — that is the separate
chunk-key-flattening change (see §9). OME-NGFF does **not** change chunk fan-out.

## 2. Current state (audit)

| Format | Storer | Layout | Metadata convention | Standard? | Concurrent live-read |
|---|---|---|---|---|---|
| TIFF | `TiffStorer` (RecordingManager.py:863) | per-detector `.tiff`, `_partN` rollover >4 GB | **snap**: ImageJ (`imagej=True`, `axes`/`unit`/`spacing`/`Info`); **stream**: *none* (`imwrite(append=True)`) | ImageJ (snap only); raw (stream) | no (append file) |
| HDF5 | `HDF5Storer` (RecordingManager.py:501) | `/{det}/data` + `/{det}/metadata/{cat}` | `element_size_um` (Fiji), `detector_name`, `recording:*` | Fiji-HDF5 convention | yes (SWMR) |
| Zarr | `ZarrStorer` (RecordingManager.py:176) | `{det}/data` + `{det}/metadata/{cat}` | `element_size_um` + `axes:['T','Y','X']`, `recording:*` | custom (not OME-NGFF) | yes (growing array) |

Shared traits: all use a "structured detector group" (`{detector}/data` +
`metadata`) for HDF5/Zarr; pixel size via `element_size_um` (an ImageJ/Fiji idiom);
axes hard-coded to `['T','Y','X']`. Reader detection: `DataObj._is_structured_detector_group`
keys off the literal name `data` (RecordingManager-independent, DataObj.py:161).

Inconsistency already in-tree: `improcess/controller/WatcherFrameController.py`
*does* write OME-NGFF via `ome_zarr.writer.write_image(..., axes="zyx")`, and
`ome-zarr>=0.6` is a declared dependency (setup.cfg) — but the recording storer
ignores it.

## 3. Target standards

| Format | Target | Mechanism |
|---|---|---|
| TIFF | **OME-TIFF** | `tifffile.imwrite(..., ome=True, metadata={axes, PhysicalSizeX/Y/Z(+Unit), TimeIncrement, Channel})` → OME-XML in `ImageDescription`. Verified working on tifffile 2026.3.3 (`TiffFile.is_ome == True`). |
| Zarr | **OME-NGFF / OME-Zarr** | group `multiscales` metadata: `axes` (name/type/unit) + `datasets[{path:"0", coordinateTransformations:[{type:"scale", scale:[...]}]}]`; image array renamed `data` → `0`. |
| HDF5 | **No OME container standard exists** | OME blesses only OME-TIFF + OME-NGFF. Decision required — see §7. |

OME-NGFF version choice (decision, §7):
- **0.4** = Zarr v2, metadata in `.zattrs` at group root.
- **0.5** = Zarr v3, metadata under `zarr.json` `attributes.ome` (with `version` inside an `ome` block). We run zarr 3.2.1, so 0.5 is the native target; the attrs are plain JSON the storer can write directly (`group.attrs["ome"] = {...}`), no extra lib needed.

## 4. Unified metadata model (the "same formatting" piece)

Introduce one canonical, format-agnostic descriptor built **once per detector per
recording**, then serialized per format:

```
@dataclass
class OmeImageMeta:
    axes: list[dict]          # [{name,type,unit}], e.g. t/z/y/x; order = data axis order
    pixel_sizes: list[float]  # physical size per axis (same order as axes)
    pixel_units: list[str]    # per axis ("micrometer", "second", ...)
    channels: list[dict]      # [{name, color?, window?}]  (1 per detector here)
    dtype: np.dtype
    name: str                 # detector / image name
    acquisition_time: str     # ISO-8601 UTC
    annotations: dict         # the existing recording:*/acquisition/scan/ImSwitch extras
```

- Built in `RecordingManager` from `detector.pixelSizeUm`, the recording mode, and
  `sharedAttrs` (same inputs the storers read today).
- Serializers:
  - **OME-TIFF** → `metadata` dict for tifffile (`PhysicalSizeX=...`, `axes="TYX"`, etc.).
  - **OME-NGFF** → `ome.multiscales` block (axes + scale).
  - **HDF5** → `element_size_um` (Fiji interop) **and** the same model as embedded
    OME-XML (see §7), plus `annotations` as a `metadata/` subgroup (unchanged).
- `annotations` (ImSwitch-specific provenance) live under a **non-`ome`** key so they
  never break standard readers.

### Axis-semantics fix (important, currently wrong)

Today every recording is labeled `T`. That is incorrect for scan data: a scan-once
stack is **scan positions / Z-planes**, not a timelapse. The model must label axes
from the recording mode:
- Camera snap → `YX`.
- Camera timelapse / `SpecFrames` → `TYX` (real time axis).
- Scan-once (`ScanOnce`) → `ZYX` if the scan has a slow/Z axis, else the frames are a
  flattened `YX` grid → label by scan geometry (from `getDimsScan()` / scanInfoDict).
- Scan-lapse → per-scan datasets use the same axes as scan-once (`ZYX` or
  `TYX`/`YX`); the lapse/timepoint dimension is represented by recording
  metadata and, for single-file Zarr/HDF5 lapse recordings, by the scan-group
  hierarchy. Do not claim `TZYX` until the stored array is actually 4-D.

This is a metadata-correctness win that OME standardization forces us to get right.

## 5. Per-format implementation

### 5a. OME-TIFF (`TiffStorer`) — ✅ **DONE**
- **Snap**: native `.ome.tiff` files via
  `tifffile.imwrite(ome=True, bigtiff=True, metadata=meta.tiff_metadata())`.
- **Streaming**: persistent `TiffWriter(path, ome=False, bigtiff=True)` with per-frame
  2D `write(frame, contiguous=True)` appends (single `(N,Y,X)` series, no 4 GB cap);
  at `finalizeStream` the OME-XML is built from the real frame count and injected via
  `tifffile.tiffcomment` (ASCII-safe: `µ`→`&#181;`). `bigtiff=True` retired the `_partN`
  rollover. Live-read N/A (live source is Zarr-only).
- Integration: `RecordingManager.buildOmeMeta()` builds the shared `OmeImageMeta`;
  `storer.omeMeta` is set before `snap`/`openStream` (attribute, no signature churn so
  HDF5/Zarr are untouched). `scanDims` threaded from `RecordingController`
  (`getDimsScan()`) → `startRecording(scanDims=...)` → worker, so scan-once z-stacks
  label `ZYX`.
- Verified: `test_ome_tiff_storer.py` (snap YX, stream TYX w/ injected OME-XML, ZYX
  z-stack, abort cleanup) + full-pipeline round-trip (snap `is_ome` YX; 100-frame
  ScanOnce `is_ome` TYX, 100 pages, PhysicalSizeX present, data intact). 58 existing
  recording tests still green.

### 5b. OME-NGFF (`ZarrStorer`) — ✅ **DONE**
- `_set_ngff_attrs()` writes `det_group.attrs["ome"]` = NGFF **0.5** multiscales from
  `meta.ngff_ome_metadata(path="data", ndim=array.ndim)`, called from both
  `_createDetectorGroup` (snap) and `_createStreamingDetectorGroup` (stream).
  Zarr v3 arrays also get `dimension_names` matching the multiscales axes, and
  the root group gets a lightweight `ome.series` index for detector discovery.
- **Decision deviation (intentional, low-risk):** the image array keeps the name
  **`data`** and `datasets[].path` points at it (a relative path is spec-valid), instead
  of renaming to `0`. This avoids touching the ~6 reader sites that key off `data`
  (`ZarrLiveSource`, the lapse source, `LiveModeController`, `DataObj`) -- the recording
  is OME-NGFF *and* every existing ImSwitch reader works unchanged. The 2D-snap case
  (stored `(1,Y,X)`) gets a leading `t` axis via `ngff_ome_metadata(ndim=...)`. Renaming
  to the conventional `0` (with a reader resolver + `data` fallback) is an optional
  cosmetic follow-up.
- Streaming `resize()`+append into `data` preserved (live-reconstruction loop intact).
  ImSwitch extras stay in `metadata/` + legacy array attrs (`element_size_um`, `writing`,
  `recording:*`), never under `ome`.
- Verified: `test_ome_zarr_storer.py` (snap + stream multiscales, scale order, flat
  chunks) + full-pipeline round-trip (NGFF `version 0.5`, axes `[t,y,x]`, `path=data`,
  scale, `data` (100,64,64)) + `DataObj`/`ZarrLiveSource` still open it + 13 existing
  `test_zarr_live_source` pass.

### 5c. HDF5 (`HDF5Storer`) — ✅ **DONE**
- Structured `/{det}/data` + `metadata/{cat}` + SWMR and Fiji `element_size_um` all kept.
- `_embed_ome_xml()` writes the shared model as OME-XML into the detector group's
  `ome_xml` attribute (via `build_ome_xml(meta.padded_to(ndim), shape)`): at
  `_createDetectorGroup` for snap (shape known), and in `finalizeStream` for streaming.
  SWMR forbids adding attrs once enabled, so streaming reuses the existing post-SWMR
  reopen (`h5py.File(path,'r+')`, same block that flips `writing=False`); RAM mode embeds
  inline. Best-effort parity payload, not an auto-detected container (no OME-HDF5 std).
- Verified: `test_ome_hdf5_storer.py` (snap + stream `ome_xml` with `SizeT`/`PhysicalSize`,
  `element_size_um` kept, `/det/data` preserved) + full-pipeline round-trip (snap valid
  `<OME>`; 100-frame ScanOnce `SizeT=100`, `writing=False`).

## 6. Reader changes (back-compat, accept old + new)

- `DataObj._is_structured_detector_group` / `_resolve_dataset` (DataObj.py:161,186):
  recognize an image array named **`0`** (NGFF) **and** legacy `data`. Read pixel
  size/axes from `ome.multiscales` when present, else fall back to `element_size_um`/`axes`.
- `ZarrLiveSource._open_array` (improcess/live/sources.py:328): open `0` (NGFF) or
  legacy `data`; read scale/axes from `multiscales`.
- OME-TIFF reading: `tifffile` parses OME-XML natively; map to the same internal
  axes/scale path used for Zarr/HDF5.
- All readers MUST keep reading pre-migration files (no forced migration).

## 7. Decisions (settled)

1. **HDF5 target** → **Fiji `element_size_um` + embedded OME-XML**. Keep the structured
   layout and Fiji-readable pixel size, AND embed the shared OME model as OME-XML so the
   logical metadata matches TIFF/Zarr. (HDF5 remains a non-OME *container*; the OME-XML is
   a best-effort parity payload, not an auto-detected standard.)
2. **NGFF version** → **0.5 (Zarr v3)**. Metadata under `zarr.json` `attributes.ome`,
   native to our zarr 3.2.1 stack.
3. **Axis labeling** → **auto from recording mode + scan geometry** (the §4 matrix):
   snap→YX (or TYX for multi-plane snap), timelapse(SpecFrames/SpecTime)→TYX,
   scan-once→ZYX if the scan has a Z/slow axis else TYX/YX, scan-lapse stores
   one scan per dataset/group and therefore uses scan-once axes.
4. **TIFF >4 GB** → **BigTIFF single file** (`bigtiff=True`); retire the `_partN`
   rollover hack.

## 8. Phasing

1. **Metadata model** — ✅ **DONE**. `imswitch/imcontrol/model/managers/recording_metadata.py`:
   `OmeImageMeta` + `build_ome_image_meta` + `axes_for_recording` (the §4 matrix) +
   serializers `tiff_metadata()` / `ngff_ome_metadata()` (0.5) / `element_size_um()`.
   16 unit tests (`test_recording_metadata.py`) + verified OME-TIFF (`tifffile.is_ome`,
   `PhysicalSizeZ` in OME-XML) and OME-NGFF 0.5 round-trips with real libs. No on-disk
   change to recordings yet. (OME-XML serializer for HDF5 embedding: Phase 4.)
2. **OME-TIFF** — ✅ **DONE** (see §5a). Snap + streaming, `buildOmeMeta`/`omeMeta`
   integration, `scanDims` threading, `test_ome_tiff_storer.py`.
3. **OME-NGFF** — ✅ **DONE** (see §5b). `ome` multiscales (path=`data`) + flat chunk
   keys; readers unchanged (back-compat by construction); `test_ome_zarr_storer.py`.
4. **HDF5** — ✅ **DONE** (see §5c). Fiji `element_size_um` + embedded `ome_xml`;
   structured layout/SWMR unchanged; `test_ome_hdf5_storer.py`.
5. **Reader back-compat sweep** — ✅ **DONE** (2026-07-09).
   `test_ome_recording_roundtrip.py` chains each real storer (TIFF/HDF5/Zarr)
   through `DataObj`: snap YX, streaming timelapse TYX, scan ZYX, plus
   cross-format axis agreement and pre-OME (`data`-named / no-`axes`) fallback.
   The sweep caught two write-side calibration bugs, now fixed:
   - **HDF5 axis labels**: the reader defaulted a 3D stack's leading axis to
     `C`, so a timelapse read back as N *channels*. The storer now writes an
     explicit `axes` attr on `data` (mirroring Zarr / ImageJ) and the reader
     (`image_sources._axis_labels_from_hdf5_attrs`) consumes it; legacy files
     with no `axes` attr keep the historical fallback.
   - **HDF5 scan Z scale**: `element_size_um` came from the detector's static
     Z pixel (1.0 µm), not the scan Z *step*. `HDF5Storer._elementSizeUm` now
     takes spatial sizes from the OME meta when present, so a z-stack reads
     back with the correct axial spacing.
6. **Docs** — ✅ **DONE** (2026-07-09). This plan + the read-compat audit
   updated; the round-trip contract and the two fixes recorded above.

## 9. Companion change (chunk-key flattening) — ✅ **DONE**

Flattened Zarr chunk keys to v2-style `0.0.0` (instead of v3 nested `c/0/0/0`) via
`chunk_key_encoding={'name':'v2','configuration':{'separator':'.'}}` in
`ZarrStorer._create_array`. Landed with Phase 3; verified flat on disk + transparent
read-back. Eliminates the per-chunk folder explosion.

## 10. Verification

- Round-trip each format: write → read back via `DataObj` → assert shape/dtype/pixel
  size/axes preserved.
- Standards checks: `tifffile.TiffFile(p).is_ome`; open Zarr with `ome_zarr` /
  napari-ome-zarr and assert `multiscales` parsed; (HDF5) Fiji `element_size_um`
  present.
- Live loop: confirm `ZarrLiveSource` still tails a growing OME-NGFF `0` array during a
  simulated scan recording.
- Regression: existing `test_zarr_live_source`, `test_recording`,
  `test_scan_once_recording_sources`, `test_workflow_provenance` stay green.

## 11. Risks

- OME-TIFF streaming requires a persistent writer handle + correct final `SizeT/Z`;
  abort/rollover paths need care.
- Renaming Zarr `data` → `0` touches the reader contract; the back-compat branch must be
  airtight or old recordings/tests break.
- Mislabeled axes today (`T` for scans) mean current files are already "wrong" — the new
  reader should not assume `T`.
