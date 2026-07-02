# SMLM Localization Port — Detailed Plan

**Roadmap:** Milestone 15 (Single-Molecule Localization Microscopy).
**Status:** planning (2026-07-02).
**Scope of this document:** localization + in-house rendering. Drift
correction (COMET), grouping, and advanced filtering are named as future
phases but are explicitly out of the initial scope.

## 1. Goal

Take a blinking image stack and:

1. **localize** single emitters → a coordinate table with properties,
2. **represent** that table as a first-class ImProcess result,
3. **render** it in-house (histogram / Gaussian) for immediate 2D/3D viewing,
4. **hand off** the same table to the external
   [napari-storm](https://github.com/super-resolution/napari-storm) plugin for
   premium GPU point-cloud visualization.

Non-goals for now: drift correction, grouping/linking, astigmatism 3D
fitting, GPU/vectorized high-throughput localization. These are Phase 6+.

## 2. Why the fit is clean (grounded in the current code)

Findings from reading `imswitch/improcess/` and the two reference projects
(`napari-storm-dev`, `pyMINFLUX`):

- **Acquisition** — ImControl already has camera + laser(s) + optional filter
  flipper. No hardware work.
- **Localization compute** — the reconstructor registry
  (`reconstructors/registry.py`) + the `StreamingReconstructor` /
  `StreamingSession` contract (`reconstructors/base.py`) already support
  chunk-by-chunk processing. The Milestone 10 streaming stack
  (`LiveSource`, `frames_committed` barrier) means live localization during
  acquisition is nearly free.
- **The one true gap: result type.** `ProcessingResult`
  (`model/result.py`) is image/array-centric — `data` is an N-d ndarray with
  `axis_labels` / `view_modes` / `display_levels`, rendered via napari
  `add_image`. `ReconstructionView` **only calls `add_image`/`setImage`;
  there is no points/particle layer anywhere.** SMLM's native output is a
  **coordinate table**, so a new table-backed result is required.
- **In-house rendering reuses the image path.** pyMINFLUX
  (`pyminflux/render/_render.py`) renders coordinates → an *image* via
  pure-numpy `render_xy` / `render_xyz` (`"histogram"` binning or
  `"fixed_gaussian"` splatting). The rendered array is exactly what the
  existing image viewer displays, so **no napari points layer is needed** for
  the in-house renderer.

### Reference data model (napari-storm `LOCS_DTYPE`)

```
("frame_number",   "i4"),
("x_pos_pixels",   "f4"),
("y_pos_pixels",   "f4"),
("z_pos_pixels",   "f4"),
("sigma_x_pixels", "f4"),
("sigma_y_pixels", "f4"),
("sigma_z_pixels", "f4"),
("photon_count",   "f4"),
```

We adopt an equivalent canonical schema (finalized in Phase 1) and keep it the
single source of truth shared with the napari-storm handoff.

### Reference localizer (napari-storm `picasso_localiztion.py`)

- `detect_spots(frame, threshold, roi, sigma)` — Picasso-style net-gradient
  detection + non-max suppression + edge exclusion. Pure numpy/scipy.
- `fit_spot(frame, y0, x0, roi, method)` — `"gausslq"` (centroid + second
  moments) or `"mle"` (scipy `L-BFGS-B` Poisson MLE). Pure numpy/scipy.

Reference-quality, frame-by-frame, unvectorized. Correct starting point; a
throughput-oriented replacement is a future phase, behind the same contract.

### Reference renderer (pyMINFLUX `_render.py`)

- `render_xy(x, y, ..., render_type)` → 2D image (`"histogram"` or
  `"fixed_gaussian"` with an FWHM), returns image + grid.
- `render_xyz(...)` → 3D volume. Pure numpy.

## 3. Architecture

```
 image stack (DataObj / LiveSource)
        │
        ▼
 Localizer  (Reconstructor / StreamingSession)      ← Phase 2/5
   detect_spots → fit_spot  (pure-function core)
        │
        ▼
 LocalizationResult  (table-backed ProcessingResult) ← Phase 1
   recarray[frame,x,y,z,sx,sy,sz,photons] + pixel size + units
        │
        ├───────────────► Renderer (processor)        ← Phase 3
        │                   render_xy / render_xyz
        │                   → image ProcessingResult → existing napari viewer
        │
        ├───────────────► table processors            ← Phase 6+ (future)
        │                   filter / group / COMET drift
        │
        └───────────────► napari-storm export          ← Phase 4
                            recarray in napari-storm's reader format
```

Design rules:

- **The table is the source of truth.** Rendering and export are derived
  views; processors transform table → table.
- **Pure-function cores.** Detection, fitting, and rendering are numpy
  functions with no Qt/registry coupling, wrapped by thin plugin adapters —
  matching how `reconstructors/monalisa/` separates `gauss_processor.py` from
  the reconstructor.
- **The render boundary is the ImProcess/napari-storm split.** In-house =
  table→image (histogram/Gaussian) in the embedded viewer. Premium =
  export→napari-storm GPU particles. No runtime dependency on napari-storm.

## 4. Phases

### Phase 1 — `LocalizationResult` + canonical schema *(foundation)*

Deliverables:

- `model/localization_result.py`: `LocalizationResult(ProcessingResult)`
  wrapping a structured `np.recarray` (canonical schema above) plus
  `pixel_size_nm`, `z_step_nm` (opt), `dims` (`"2D"`/`"3D"`), `source_name`,
  and free-form metadata.
- A stable, documented column contract (constant module, one place) reused by
  the localizer, the renderer, processors, and the export.
- Minimal viewer behavior: since `ProcessingResult` requires a viewable
  `data`, `LocalizationResult` lazily exposes a low-res histogram render as
  its default `data` so it *has* an image the existing viewer can show, while
  keeping the recarray as the real payload (`.locs`).
- Round-trip helpers: `to_recarray()`, `from_recarray()`, and a
  pandas/`ResultsTableWidget`-compatible view (columns → the existing table).

Tests: schema validation, empty-table handling, 2D vs 3D, metadata
round-trip, table-widget projection. No hardware, no Qt viewer.

**Exit criteria:** a `LocalizationResult` can be constructed from a recarray,
surfaced in the results table, and carries the schema the later phases depend
on.

### Phase 2 — `Localizer` reconstructor (batch) *(first visible result)*

Deliverables:

- `reconstructors/smlm/detection.py` + `fitting.py`: the pure-function core,
  ported from napari-storm `picasso_localiztion.py` (net-gradient detect +
  centroid/MLE fit), typed and unit-tested against small synthetic frames
  with known emitter positions (assert recovered x/y within tolerance).
- `reconstructors/smlm/localizer.py`: `SmlmLocalizer(Reconstructor)` —
  `process(data_obj, params) -> LocalizationResult` iterating frames,
  concatenating per-frame fits into the canonical recarray.
- `reconstructors/smlm/params_widget.py`: threshold, ROI size, smoothing
  sigma, fit method (`gausslq`/`mle`), pixel size (nm). Follows the existing
  params-widget pattern.
- Registry registration + auto-select heuristic (a plain 2D+T stack with no
  MoNaLISA/SIM metadata can offer the localizer).

Tests: synthetic 3-emitter frame → 3 localizations at expected coordinates;
empty/blank frame → 0; parameter plumbing.

**Exit criteria:** load a blinking stack in ImProcess, run the `Localizer`,
get a populated results table of localizations. (Rendering still crude — the
Phase 1 default histogram.)

### Phase 3 — In-house renderer *(the "shows something" milestone)*

Deliverables:

- `analysis/smlm_render.py`: pure-numpy `render_xy` and `render_xyz`
  (`"histogram"` + `"fixed_gaussian"`), ported/adapted from pyMINFLUX
  `_render.py`. Inputs: recarray + output pixel size (nm/px) + FWHM. Outputs:
  image (2D) / volume (3D) + world-coordinate grid.
- `processors/smlm_render/`: a `Processor` taking a `LocalizationResult` and
  producing an image `ProcessingResult` (2D image or 3D z-stack) that the
  **existing** `ReconstructionView` renders — 3D via the dims slider, correct
  `axis_scales`/`scale_unit="nm"`.
- Params widget: render type, output pixel size (super-res, e.g. 5–10 nm/px),
  Gaussian FWHM, optional z-range/z-step.
- Reasonable performance for ~10⁵–10⁶ localizations (vectorize the Gaussian
  splat over the small kernel; chunk if needed). Note the pyMINFLUX reference
  loops per-spot — acceptable to start, flagged for a vectorization pass.

Tests: known localizations → histogram image has counts in the right bins;
Gaussian render is smooth and mass-preserving; 3D render shape matches
z-binning; nm scaling correct.

**Exit criteria:** localize → render → a super-resolved 2D image (and a 3D
volume) visible in the ImProcess napari viewer, entirely in-house.

### Phase 4 — napari-storm handoff *(premium renderer, decoupled)*

Deliverables:

- An exporter writing the `LocalizationResult` recarray in the format
  napari-storm's reader ingests (confirm against
  `napari-storm-dev` `_reader.py` / `FileToLocalizationDataInterface.py`;
  likely an HDF5/CSV with the `LOCS_DTYPE` columns). Wire it as a
  save action on the result / results table.
- Documentation: the exact column contract and units at the boundary, so the
  two projects stay compatible without a code dependency.
- Optional convenience: a "Open in napari-storm" action that writes a temp
  export and launches napari-storm if it is installed (soft, import-guarded —
  never a hard dependency).

Tests: exported file re-read matches the source recarray (columns, dtypes,
units); round-trip through napari-storm's reader if importable in CI,
otherwise a format-contract test.

**Exit criteria:** one action turns an ImProcess localization result into a
file napari-storm opens and renders as a GPU point cloud.

### Phase 5 — Streaming / live localization *(reuse Milestone 10)*

Deliverables:

- `SmlmLocalizer` implements `StreamingReconstructor`; an `SmlmLiveSession`
  (`StreamingSession`) localizes each incoming chunk and appends to the
  growing table, exposing an incrementally-updated histogram render as its
  live `result()`.
- Rides the existing `LiveSource` + `frames_committed` barrier so
  localizations accumulate live during a recording, and the in-house render
  updates as frames arrive.

Tests: fake streaming source feeds frames in batches → localization count
grows monotonically and matches the batch equivalent; completion via the
existing markers.

**Exit criteria:** watch a super-resolved image build live during a STORM
acquisition, using the same streaming infrastructure as live reconstruction.

### Phase 6+ — Table processors *(future scope, named only)*

Each a `Processor` operating `LocalizationResult → LocalizationResult`,
slotting into the existing processor chain:

- **Filtering** — by photons/sigma/frame/precision/ROI; the results-table
  plotting work already gives histograms to choose cutoffs from.
- **Grouping / linking** — merge localizations of the same emitter across
  consecutive frames (blinking), with per-group statistics.
- **COMET drift correction** — GPU-optional (import-guarded), CPU fallback;
  the marquee future capability.
- **3D fitting** — astigmatism / PSF-model z from `sigma_x`/`sigma_y`.
- **Throughput localization** — vectorized/GPU detect+fit behind the same
  pure-function contract, swappable for the reference implementation.

## 5. File map (proposed)

```
imswitch/improcess/
  model/
    localization_result.py        # Phase 1: LocalizationResult + schema
  reconstructors/smlm/
    __init__.py
    detection.py                  # Phase 2: net-gradient detect (pure fn)
    fitting.py                    # Phase 2: centroid/MLE fit (pure fn)
    localizer.py                  # Phase 2/5: Reconstructor + StreamingSession
    params_widget.py              # Phase 2
  analysis/
    smlm_render.py                # Phase 3: render_xy/render_xyz (pure fn)
  processors/
    smlm_render/                  # Phase 3: table -> image ProcessingResult
    smlm_export/                  # Phase 4: napari-storm export
    smlm_filter/  smlm_group/     # Phase 6+ (future)
```

## 6. Attribution / licensing

- Detection/fitting are **ports from napari-storm** (`super-resolution`) — the
  user's own project; carry provenance in the module docstrings.
- Rendering is **adapted from pyMINFLUX** (`bsse-scf/pyMINFLUX`, Apache-2.0) —
  check licence compatibility with ImSwitch's GPL before copying verbatim;
  a clean-room reimplementation of histogram/Gaussian rendering from the
  described algorithm is trivial if needed.

## 7. Open questions (for discussion before Phase 1)

1. **Canonical units** — store positions in pixels (napari-storm) or nm
   (pyMINFLUX)? Proposal: nm internally with pixel size in metadata; keep a
   pixel view for the napari-storm export.
2. **Default `data` for `LocalizationResult`** — lazy histogram vs. a null
   image with rendering strictly via the render processor. Proposal: lazy
   low-res histogram so the result is never blank in the viewer.
3. **2D-first or 2D+3D from the start** — the schema is 3D-ready; ship the
   localizer 2D-first (z=0) and add astigmatism z in Phase 6, or wire a z
   path earlier? Proposal: 2D-first, schema 3D-ready.
4. **Where the render lives** — a `Processor` (fits the chain, explicit
   result) vs. a view-side toggle on `LocalizationResult` (more "live"
   feeling). Proposal: processor for the explicit result, plus the lazy
   default histogram for immediate feedback.
