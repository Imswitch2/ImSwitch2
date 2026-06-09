# ImProcess Graph Widget + WidefieldSTARSS Analysis Plan

**Status:** In progress - graph widget implemented; first WidefieldSTARSS
ImProcess reconstructor slice implemented for H/V TIFF pairs; FRC and ROI
statistics panels added as generic ImProcess analysis widgets
**Date:** 2026-05-29
**Scope:** `imswitch/improcess/`, with source analysis reference from
`/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/analysis`

This plan captures two related pieces of work:

1. Add a general-purpose graph/result widget to ImProcess. Implemented.
2. Port the WidefieldSTARSS anisotropy analysis as an ImProcess processing
   unit that uses that graph widget. First single-pair implementation is in
   place; batch/table/layer polish remains follow-up work.
3. Add generic quantitative ImProcess widgets that can reuse the same result
   and graph infrastructure. First implementations: FRC / single-image FRC and
   ROI statistics.

The graph widget should land first because it is broadly useful beyond
WidefieldSTARSS: drift correction, FLIM summaries, STED/confocal projections,
batch processing, line profiles, histograms, and any future processor that
returns tabular or one-dimensional results all need the same display surface.

---

## 1. Goals

### 1.1 ImProcess Graph Widget

Add a reusable ImProcess widget/controller pair for plotting derived data:

- histograms, e.g. region anisotropy distribution
- line plots, e.g. line profiles, z profiles, drift traces
- scatter plots, e.g. per-region metrics or QC plots
- grouped summary plots, e.g. batch replicate summaries
- optional table-linked selection later

The widget is intentionally generic. It should not know about MoNaLISA,
WidefieldSTARSS, FLIM, or any other modality. Processors and reconstructors
publish plot-ready data to it through a small typed interface.

### 1.2 WidefieldSTARSS Analysis Unit

Port the reusable numerical kernels from the WidefieldSTARSS analysis package
into an ImProcess processing unit that turns an H/V acquisition pair into:

- anisotropy maps: `r_raw`, `r_smooth`, standard-error maps, valid mask
- intensity maps: `ihh`, `ihv`, `ivh`, `ivv`
- segmentation artifacts: label mask and base image
- per-region table: pooled anisotropy, frame SE, spatial SD, fit results
- optional HDF5 export
- graph widget outputs: anisotropy histogram, per-region scatter/summary,
  batch summary plots later

---

## 2. Source Audit Summary

Audited files under `WidefieldStarss/src/WFS/analysis`:

| File | Portability | Notes |
|---|---|---|
| `containers.py` | High | Dataclasses for `PolarizationStats` and `AnisotropyMaps`; good starting point. |
| `polarization.py` | High | 2x2 mosaic splitting, Stokes stats, simple intensity stats. Pure numerical code. |
| `anisotropy.py` | High | x-based anisotropy, Gaussian fit, smoothed maps. Pure numerical code. |
| `regions.py` | High | Weighted pooling and per-region tables. Depends on pandas but no GUI. |
| `segmentation.py` | High | Otsu, PSF peak, line-PSF segmentation. Depends on scikit-image/scipy. |
| `io.py` | Medium | Useful TIFF pair loading and HDF5 schema, but should be adapted to ImProcess I/O. |
| `pipeline.py` | Medium | Good reference, but too much orchestration and long parameter list for direct port. |
| `visualization.py` | Low initially | Matplotlib output should be deferred; graph widget replaces much of this role. |
| `__init__.py` | Low | Public export map only. |

The important distinction in the source package is that it supports two
acquisition families:

- **Standard polarization mosaic mode:** H/V file pair, 2x2 analyzer mosaic,
  alternating signal/background frames by default.
- **Split-detection / line-PSF mode:** H/V file pair, upper/lower camera halves
  represent H/V detection channels, often block-framed with dark/off/signal
  sections.

That distinction should become explicit in the ImProcess parameter UI and
result metadata.

---

## 3. Phase 1 - General ImProcess Graph Widget

**Implementation status:** Done.  The graph panel, `PlotPayload` /
`PlotSeries` model contract and `GraphController` are in place.  Built-in
payload producers now include drift correction, WidefieldSTARSS and FRC.

### 3.1 Proposed Files

- `imswitch/improcess/view/GraphWidget.py`
- `imswitch/improcess/controller/GraphController.py`
- optional model helper:
  `imswitch/improcess/model/plotting.py`

### 3.2 Minimal Data Contract

Use a small dataclass-style contract so any processing unit can publish plots
without importing widget classes:

```python
@dataclass
class PlotSeries:
    name: str
    x: np.ndarray | None
    y: np.ndarray
    kind: Literal["line", "scatter", "histogram"]
    style: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlotPayload:
    title: str
    x_label: str = ""
    y_label: str = ""
    series: list[PlotSeries] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

`ProcessingResult` can optionally expose plots:

```python
def plot_payloads(self) -> list[PlotPayload]:
    return []
```

This keeps plotting opt-in and backward-compatible with existing MoNaLISA and
view-only results.

### 3.3 Widget Behavior

First version:

- embedded `pyqtgraph.PlotWidget`
- plot selector combo box when multiple `PlotPayload`s are available
- clear/redraw API
- export image action if cheap to add
- no modality-specific controls
- no pandas dependency in the widget itself

Likely public methods:

```python
class GraphWidget(QtWidgets.QWidget):
    sigExportClicked = QtCore.Signal()

    def setPlotPayloads(self, payloads: list[PlotPayload]) -> None: ...
    def clear(self) -> None: ...
```

The controller should listen for current result changes from the reconstruction
view/result controller and call `result.plot_payloads()` if present.

### 3.4 Why Graph First

The graph widget unblocks several workflows without requiring the full
WidefieldSTARSS port:

- drift-correction traces from `drift-correct`
- histograms of current image/result values
- line profiles from existing viewer tools
- batch summaries once batch processing exists
- WFS anisotropy histograms and per-region plots later

It also forces a clean display contract before WFS introduces pandas tables and
many derived arrays.

---

## 4. Phase 2 - WidefieldSTARSS Kernel Port

**Implementation status:** First slice done.  Pure kernels now live under
`imswitch/improcess/reconstructors/widefield_starss/analysis/` with synthetic
tests covering mosaic splitting, alternating frame splitting, anisotropy,
standard H/V analysis, split-detection analysis and result save behavior.

The implemented WFS reconstructor uses this local package shape:

```text
imswitch/improcess/reconstructors/widefield_starss/
|-- __init__.py
|-- reconstructor.py
|-- result.py
|-- params_widget.py
`-- analysis/
    |-- containers.py
    |-- polarization.py
    |-- anisotropy.py
    |-- regions.py
    `-- segmentation.py
```

The numerical kernels stay pure: file dialogs, folder scanning, matplotlib
plotting and threaded UI workers are kept out of the kernel package.

Implemented pure API:

```python
def analyze_widefield_starss_pair(
    stack_h: np.ndarray,
    stack_v: np.ndarray,
    params: WidefieldStarssParams | None = None,
) -> WidefieldStarssAnalysis:
    ...
```

`WidefieldStarssAnalysis` contains:

- `regions: pandas.DataFrame`
- `mask: np.ndarray`
- `base_image: np.ndarray`
- `stats_h: PolarizationStats | None`
- `stats_v: PolarizationStats | None`
- `anis_maps: AnisotropyMaps`
- optional split-detection intensity maps

---

## 5. Phase 3 - WidefieldSTARSS Reconstructor

**Implementation status:** First slice done.  `widefield-starss` is registered
as a built-in reconstructor.  It supports `_h.tif` / `_v.tif` auto-pairing,
manual counterpart selection, standard mosaic mode, split-detection mode,
presets, map-stack display, HDF5/TIFF save and graph payloads.  Standard mosaic
anisotropy now exposes an explicit mode switch: `stokes` remains the default
and uses all four analyzer pixels through `S0/S1`, while `direct_0_90` uses
only the raw 0°/90° analyzer pixels.  The WFS segmentation mode list also
includes `generic_otsu`, which reuses the generic ImProcess segmentation kernel
used by the `segmentation` processor.  A pure backend batch API now discovers
`*_h.tif[f]` / `*_v.tif[f]` pairs, runs all pairs with one shared parameter set
and writes consolidated per-region and per-sample tables.  The first
parameter-widget batch controls now run that backend in a Qt worker thread,
report pair-level progress, support cancellation between pairs and export
CSV/HDF5 tables.  Dedicated table, unmatched-file preview and multi-layer
viewer display remain Phase 4 work.

`WidefieldStarssReconstructor` is now registered as a modality-specific
ImProcess reconstructor.

### 5.1 Input Model

The source WFS analysis expects a paired acquisition:

- `*_h.tif`
- `*_v.tif`

ImProcess currently treats input as a single `DataObj`. This is the main
integration question. Candidate approaches:

1. **Reconstructor-specific paired-file parameter widget**
   The user selects H and V paths in `WidefieldStarssParamsWidget`.
   This is simplest for the first version.
2. **Paired DataObj abstraction**
   Add a generic `DataBundle` or paired-dataset input model to ImProcess.
   Cleaner long-term, but larger scope.
3. **Auto-pair by file naming**
   When the current file ends in `_h.tif` or `_v.tif`, auto-discover the
   counterpart. Useful as a convenience, not enough as the only path.

Implemented first version: option 1 plus auto-pair convenience.

### 5.2 Parameter Surface

The parameter widget exposes the core controls from the WFS analysis widget:

- H file, V file
- loading convention: `alternating` or `block`
- `start_frame`
- `sum_stacks`
- `n_dark`, `n_off` for block convention
- segmentation mode:
  - standard Otsu
  - none / full field
  - PSF peaks
  - line PSF
- Otsu params: `segmentation_sigma`, `min_size`, `hole_size`,
  `threshold_scale`
- PSF params: `psf_sigma`, `psf_min_distance`, `psf_threshold_rel`,
  `psf_radius`
- line-PSF params: `psf_sigma`, `threshold_scale`, `min_size`, optional
  `split_y`
- map params: `smooth_sigma`, optional `intensity_threshold`
- optional ROI

Implemented named presets:

- `Widefield cells`: alternating, `start_frame=20`, Otsu,
  `min_size=200`, `smooth_sigma=10`
- `Line PSF / split detection`: block, `split_detection=True`, `n_dark`,
  `n_off`, small `min_size`, `smooth_sigma=1`

### 5.3 Result Object

Initial implementation note: `WidefieldStarssResult` currently exposes a
4-channel map stack (`r_smooth`, `r_raw`, label mask, segmentation base image)
so the existing ImProcess viewer can browse all first-pass outputs without a
new multi-layer UI.  The richer table/layer UI remains Phase 4 work.

`WidefieldStarssResult(ProcessingResult)` currently:

- exposes a small map stack until generic layer support exists
- exposes `axis_labels=["C", "Y", "X"]`
- include all maps in `maps`
- includes the region table in `analysis.regions`
- implements `plot_payloads()` for the graph widget:
  - histogram of `anisotropy_direct`
  - scatter: `area_superpixels` vs `anisotropy_direct`
  - line/scatter: label vs anisotropy with error bars later
- implements `save(path, fmt)` for TIFF and HDF5

---

## 6. Phase 4 - Tables, Napari Layers, and Batch

After the single-pair reconstructor works:

- add a generic result table widget or reuse an existing Qt table surface
- push WFS maps into napari/ImProcess viewer layers:
  - base image
  - mask labels
  - `r_raw`
  - `r_smooth`
  - valid mask
- add batch folder mode:
  - discover `*_h` / `*_v` pairs
  - run single-pair analysis repeatedly
  - concatenate region tables with `source_base`, `local_label`,
    `global_label`
  - execute in a worker thread with pair-level progress and cancellation
  - graph widget displays batch histogram and per-source summaries

The first batch mode is implemented; remaining Phase 4 work is result browsing,
unmatched-file inspection, graph summaries and careful memory handling for very
large folders.

---

## 6.5 Generic Analysis Widgets Added During This Work

### FRC / Single-Image FRC

Implemented:

- pure numerical kernels in `imswitch/improcess/analysis/frc.py`
- registered `frc` processor under `imswitch/improcess/processors/frc/`
- `FRCResult` with graph payloads and HDF5/CSV save
- optional `FRCWidget`, enabled by `processing.frcPanel`
- two-image FRC across an image axis
- single-image FRC with checkerboard or odd/even splitting
- Hann/no windowing and fixed 1/7 threshold

Remaining follow-up:

- half-bit / three-sigma thresholds
- ROI-cropped FRC driven by rectangle selection
- random repeated split mode with confidence intervals
- processor-chain UI integration

### ROI Statistics

Implemented:

- pure ROI statistics helper in `imswitch/improcess/analysis/roi_stats.py`
- optional `ROIStatsWidget`, enabled by `processing.roiStatsPanel`
- full-image or rectangle-ROI area, finite pixel count, mean, median, standard
  deviation, min, max and sum

Remaining follow-up:

- CSV export from the panel
- per-timepoint / per-channel table mode
- table-linked ROI persistence

---

## 7. Testing Plan

Implemented tests:

- graph payload behavior for drift correction, WFS and FRC results
- WFS synthetic 2x2 mosaic splitting
- WFS known-intensity anisotropy formula
- WFS alternating frame splitting
- WFS standard H/V pair analysis with one finite region
- WFS split-detection pair analysis
- WFS temporary TIFF H/V auto-pairing
- WFS HDF5 result save
- FRC two-image and single-image numerical behavior
- ROI statistics full-image and clipped rectangle behavior

Remaining useful tests:

- Qt smoke tests for optional panels when the local Qt test environment is
  stable
- WFS Otsu/PSF segmentation edge cases on synthetic blobs/stripes
- FRC ROI-cropped analysis once ROI FRC is added

---

## 8. Open Questions

- Should ImProcess introduce a generic paired-input abstraction now, or keep
  paired file selection local to the first WFS plugin?
- Should pandas be allowed in `ProcessingResult` objects, or should region
  tables use a small internal table model and convert to pandas only for save?
- Should graph payloads support error bars in v1, or should error bars wait
  until WFS starts using them?
- Should WFS HDF5 output preserve the current source schema exactly, or write a
  new ImProcess-native schema with compatibility export?

---

## 9. Recommended Next Work

1. Add a general processor-chain UI so registered processors such as `frc` and
   `drift-correct` can be run interactively without bespoke panels.
2. Add WFS table/layer display: show region tables in Qt and expose `r_raw`,
   `r_smooth`, masks and base image as separate viewer layers.
3. Add WFS batch folder processing with progress, cancellation and source-aware
   region-table concatenation.
4. Extend FRC with ROI-cropped analysis, half-bit threshold and repeated
   single-image split confidence intervals.
5. Add projection and histogram/threshold panels as the next generic ImProcess
   widgets.
