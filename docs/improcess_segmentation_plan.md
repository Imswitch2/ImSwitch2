# ImProcess Segmentation Plan

## Problem

The current ImProcess segmentation path is intentionally simple: a 2D image is
thresholded with Otsu or a manual value, connected components are measured, and
the result can be exported as labels, tables, or ROIs. This is useful for clean
beads or high-contrast objects, but it is weak for common microscopy cases:

- uneven background
- touching cells or beads
- low contrast foreground
- bright debris or border artifacts
- workflows that need repeatable, headless processing

## Constraints

- Keep the current lightweight, reproducible processor path.
- Do not add heavy dependencies to core ImSwitch.
- Prefer existing dependencies first: `scipy` and `scikit-image`.
- Keep optional model-based segmentation as install-time extras/backends.
- Preserve current HDF5/TIFF result saving and ROI export behavior.

## Phase 1: Classical Microscopy Segmentation

Status: done.

Implement a stronger no-new-dependency baseline using `scipy` and
`scikit-image`.

Methods:

- `otsu`
- `manual`
- `triangle`
- `yen`
- `local`
- `watershed`

Pre/post-processing:

- Gaussian smoothing
- white top-hat background subtraction
- binary opening/closing
- hole filling
- border clearing
- min-area filtering
- watershed splitting from distance-transform markers

This phase should update both the registered `SegmentationProcessor` and the
interactive active-layer `SegmentationWidget`, since both call the same
analysis helper.

## Phase 2: Better 2D/Stack Selection

Status: done for registered `SegmentationProcessor` plane selection.

Improve the processor-side input controls so users can choose which plane or
component to segment instead of always taking the first non-spatial index.

Candidate controls:

- explicit T/Z/C index fields
- segment current display component from the result-component selector
- optional named-axis mapping, such as `Dataset=0, Base=1`
- optional batch-over-axis mode later

## Phase 3: Ilastik-Like Lightweight Classifier

Status: future work.

Prototype a pixel-classifier path without making ilastik a dependency.

Options:

- optional `scikit-learn` backend with random forest / extra trees
- feature stack from Gaussian, Laplacian, Sobel, Hessian-like filters
- user-provided sparse label masks from Napari labels
- probability map output followed by threshold/watershed

This should remain optional until there is a clear user workflow for training
and persisting classifiers.

## Phase 4: Optional Model Backends

Status: future work.

Add optional backends only when dependencies are installed:

- Cellpose for cells/nuclei/cytoplasm
- StarDist for star-convex nuclei-like objects
- micro-SAM/SAM-style tools for prompt-driven or precomputed-model workflows

Rules:

- no hard dependency in core
- clear unavailable-backend messages
- model weights and GPU/CPU expectations must be explicit
- backend outputs should still use `SegmentationResult`

## Phase 5: UX and Persistence

Once algorithms are stable:

- save/load segmentation presets
- expose algorithm metadata clearly in saved HDF5
- add preview/apply separation in the widget if needed
- add ROI-manager roundtrip tests for labels and masks
