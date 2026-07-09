# ImProcess Analysis Widgets Plan

**Status:** In progress - Phases 1-5 implemented  
**Date:** 2026-06-09  
**Scope:** `imswitch/improcess/`, config-editor ImProcess section templates,
and ImProcess-only setup JSONs.

This plan covers the next set of ImProcess-specific analysis improvements.
These are not `availableWidgets` entries for ImControl. They should be exposed
through the ImProcess `processing` block, e.g. `projectionPanel`,
`roiManagerPanel`, `segmentationPanel`, and processor IDs where a reusable
processor abstraction makes sense.

The guiding principle is to build shared analysis infrastructure first. ROI
management is the key dependency: segmentation, PSF/bead resolution, and
colocalization become cleaner if they can all read/write the same ROI model.

## Phase 1 - Generic Projections

Goal: add a generic projection panel and processor for active ImProcess
image/result layers.

Implementation status: done. The pure projection kernel, registered
`projection` processor, `ProjectionResult`, optional `ProjectionWidget`,
config-editor field, and setup-preset updates are in place.

Features:

- Select the active image/result layer.
- Choose axis: `T`, `Z`, `C`, or any available dimension.
- Projection modes: `max`, `mean`, `sum`, `median`, `std`.
- Output a new `ProcessingResult` with correct axis labels.
- Optional graph payload for projection summaries.

Config:

```json
"processing": {
  "projectionPanel": true
}
```

Listing `"projection"` under `processors` still preloads the processor without
opening the panel, but startup-enabled projection panels now auto-register their
backing processor when needed.

Likely files:

- `imswitch/improcess/analysis/projections.py`
- `imswitch/improcess/processors/projection/`
- `imswitch/improcess/view/ProjectionWidget.py`
- `utility_scripts/builtin_templates/sections/processing.json`
- focused tests for numerical projections and shape/metadata behavior

## Phase 2 - ROI Manager

Goal: add an ImageJ-like ROI manager for ImProcess.

Implementation status: done for the first rectangular-ROI slice. The
`ROIManagerModel`, `ROIRecord`, `ROIStatsRecord`, optional `ROIManagerWidget`,
`roiManagerPanel` config flag, config-editor field, and setup-preset updates
are in place. Ellipse/polygon ROIs remain later work.

Features:

- Store multiple ROIs per active result/image.
- Initial ROI type: rectangle. Ellipse/polygon can follow once napari shape
  handling is stable enough.
- Add ROI from the current napari shape.
- Rename, delete, duplicate, show/hide ROIs.
- Export ROI table as CSV/JSON.
- Persist ROI definitions in result metadata where possible.

Statistics:

- area
- finite pixel count
- mean
- median
- standard deviation
- min
- max
- sum
- optional per-channel/per-timepoint stats later

Config:

```json
"processing": {
  "roiManagerPanel": true
}
```

Likely files:

- `imswitch/improcess/analysis/roi_manager.py`
- `imswitch/improcess/view/ROIManagerWidget.py`
- tests for ROI serialization and multi-ROI statistics

Implementation note: keep the existing simple `ROIStatsWidget` for now. The ROI
Manager can supersede it once stable, or the simple panel can remain as a
single-ROI quick view.

## Phase 3 - Segmentation Widget

Goal: create ROIs from segmentation and feed them into the ROI Manager.

Implementation status: done for the first threshold/connected-component slice.
The pure segmentation kernel, registered `segmentation` processor,
`SegmentationResult`, optional `SegmentationWidget`, `segmentationPanel`
config flag, config-editor field, setup-preset updates, and ROI Manager bridge
are in place. The widget creates a label layer, exports segmentation region
tables as CSV/JSON, and can add exact segmented component masks as ROI Manager
entries. Segmentation HDF5 saves include labels, masks and region-table
datasets.

Features:

- Run segmentation on the active image/result layer.
- Initial algorithms:
  - manual threshold
  - Otsu threshold
  - connected components
  - minimum area filter
  - optional smoothing
- Convert labeled regions into exact mask ROI Manager entries.
- Display label mask as an optional result/layer.
- Export segmentation labels and region table.

Config:

```json
"processing": {
  "segmentationPanel": true
}
```

Likely files:

- `imswitch/improcess/analysis/segmentation.py`
- `imswitch/improcess/processors/segmentation/`
- `imswitch/improcess/view/SegmentationWidget.py`
- tests with synthetic blobs

Remaining refinements:

- Polygon/outline visualization for ROI Manager entries, if needed for manual
  editing of segmented objects.
- Additional algorithms such as watershed or spot detection where useful.

## Phase 4 - PSF / Bead Resolution

Goal: add an ImProcess panel and processor for bead/PSF resolution measurements.

Implementation status: done for the first 2D Gaussian fitting slice. The pure
PSF fitting kernel, registered `psf-resolution` processor,
`PSFResolutionResult`, optional `PSFResolutionWidget`, `psfResolutionPanel`
config flag, config-editor field, setup-preset updates, graph payload, HDF5/CSV
save path, and synthetic Gaussian tests are in place. The widget can fit the
full active image or batch over ROI Manager entries, including segmentation
mask ROIs.

Features:

- Use full image or ROIs from ROI Manager.
- Fit 2D Gaussian per bead/ROI.
- Report FWHM X/Y, sigma X/Y, amplitude, background, fit error.
- Batch over multiple ROIs.
- Export result table.

Config:

```json
"processing": {
  "psfResolutionPanel": true,
  "processors": ["psf-resolution"]
}
```

Likely files:

- `imswitch/improcess/analysis/psf_resolution.py`
- `imswitch/improcess/processors/psf_resolution/`
- `imswitch/improcess/view/PSFResolutionWidget.py`
- tests with synthetic Gaussian beads

Dependency: best after ROI Manager and Segmentation, because it can consume
manually drawn or segmented bead ROIs.

Remaining refinements:

- Automatic bead candidate detection.
- Optional radial profile plot per fit.

## Phase 5 - Colocalization

Goal: analyze channel overlap within the full image or within ROIs.

Implementation status: done for the first two-plane metric slice. The pure
colocalization kernel, registered `colocalization` processor,
`ColocalizationResult`, optional `ColocalizationWidget`,
`colocalizationPanel` config flag, config-editor field, setup-preset updates,
graph scatter payload, HDF5/CSV save path, and synthetic correlated/ROI tests
are in place. The widget compares two planes from an active stack axis and can
batch over ROI Manager entries.

Features:

- Select two channels/planes from an active stack axis.
- Optional ROI selection from ROI Manager.
- Metrics:
  - Pearson correlation
  - Manders M1/M2
  - overlap coefficient
  - intensity scatter plot
- Optional thresholding per channel.
- Export per-ROI colocalization table.

Config:

```json
"processing": {
  "colocalizationPanel": true,
  "processors": ["colocalization"]
}
```

Likely files:

- `imswitch/improcess/analysis/colocalization.py`
- `imswitch/improcess/processors/colocalization/`
- `imswitch/improcess/view/ColocalizationWidget.py`
- tests with synthetic correlated/uncorrelated channels

Dependency: best after ROI Manager, because per-ROI colocalization is much more
useful than only whole-image metrics.

Remaining refinements:

- Direct two-layer selection in addition to stack-axis comparison.
- Optional threshold helpers such as Costes-style automated thresholds.

## Recommended Order

1. Generic Projection panel/processor.
2. ROI Manager.
3. Segmentation widget feeding ROI Manager.
4. PSF / Bead Resolution using ROI Manager.
5. Colocalization using ROI Manager.

## Config Editor Updates

Each phase should update the ImProcess section template:

- `utility_scripts/builtin_templates/sections/processing.json`
- fallback dynamic options in `utility_scripts/imswitch_config_editor.py`
- setup presets under `imswitch/_data/user_defaults/imcontrol_setups/`

The config editor should expose these under `Config Settings` -> `System` ->
`ImProcess`, not under `Available Widgets`, because they belong to ImProcess
post-processing.
