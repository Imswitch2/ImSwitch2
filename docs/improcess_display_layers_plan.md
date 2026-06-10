# ImProcess Display Layers Plan

## Problem

Some ImProcess reconstructors produce arrays whose leading stack axis contains
semantically different images rather than repeated samples of the same signal.
Displaying these outputs as one Napari image layer causes shared contrast limits,
ROI statistics, profiles, and other active-layer tools to mix incompatible
quantities.

The immediate example is Widefield STaRSS, where the current result is a single
`CYX` stack containing:

- `r_smooth`
- `r_raw`
- `mask`
- `base_image`

The intensity scale of `base_image` is typically much larger than the
anisotropy maps, so Napari auto levels and downstream ROI statistics are not
meaningful for the stack as a whole.

MoNaLISA has a related issue: its `Base` axis can contain different semantic
components. In the common two-base path, base 0 is the reconstructed signal and
base 1 is the modeled background. These should be viewable independently even
though the canonical saved result should remain grouped.

## Design Direction

Add an optional display-layer contract to `ProcessingResult`.

The canonical `ProcessingResult.data` stays unchanged and remains the source
for saving, processor pipelines, and backward compatibility. Results that have
heterogeneous display components can additionally expose named Napari layer
specs.

The viewer should use display layers when present. Results without display
layers continue through the existing single-layer path.

## Processor Policy

There are two processor/tool categories:

- Interactive Napari tools, such as ROI statistics, profile, FRC, projection,
  segmentation, and colocalization. These already mostly operate on the active
  Napari image layer, so split display layers improve behavior immediately.
- Registered ImProcess processors under `imswitch/improcess/processors`. These
  operate on `ProcessingResult` objects and should remain result-based for
  reproducibility, batch use, and headless use.

Recommended rule:

- Interactive analysis widgets use the active Napari layer.
- Registered processors use a result/component selector. Do not silently make
  all registered processors consume the active Napari layer.

## Implementation Phases

### Phase 1: Shared Display Contract

- Add `DisplayLayerSpec` to `imswitch/improcess/model/result.py`.
- Add `ProcessingResult.display_layers()` returning an empty list by default.
- Each display layer spec should carry:
  - layer name
  - array data
  - axis labels
  - optional contrast limits
  - optional colormap
  - optional scale and scale unit
  - metadata, including result/component identity

### Phase 2: Widefield STaRSS

- Keep `WidefieldStarssResult.data` as the existing `CYX` stack for saving and
  compatibility.
- Add `display_layers()` returning separate 2D layers:
  - `r_smooth`
  - `r_raw`
  - `mask`
  - `base_image`
- Give each layer independent display levels.
- Store metadata such as:
  - `source_result`
  - `component`
  - `axis_labels`
  - `scale_unit`
- Add tests for display layer names, shapes, independent contrast levels, and
  unchanged save behavior.

### Phase 3: Viewer Integration

- Update `ReconstructionViewController` to use `result.display_layers()` when
  non-empty.
- Update `ReconstructionView` to manage multiple result-owned Napari image
  layers while keeping the existing single-layer behavior for all other
  results.
- Make sure Napari layer metadata includes `axis_labels` and `scale_unit`, so
  active-layer widgets do not need to infer axes from shape alone.
- Decide how to preserve display-level state for multi-layer results. Initial
  implementation may recompute per-layer levels on selection; later work can
  persist contrast limits per component.

### Phase 4: MoNaLISA

- Audit exact base semantics in `MonalisaProcessingResult` and
  `MonalisaReconstructor`.
- Add display layers for semantic bases while preserving canonical data:
  - `signal` for base 0
  - `background` for base 1 when background modeling creates a second base
  - generic `base_N` fallback names for additional bases
- Keep true dimensions (`Dataset`, `T`, `Z`, `Y`, `X`) in each layer.
- Preserve physical `Y`, `X`, and `Z` scales.
- Add tests that verify:
  - display layers preserve data values from the corresponding base
  - signal/background names are assigned for the two-base path
  - canonical save shape remains unchanged

### Phase 5: Processor Component Selection

- Add a component selector to the processor UI for result-based processors.
- Selector options should include:
  - whole current result where valid
  - active Napari layer where explicitly supported
  - named display components exposed by the current result
- Use explicit selection rather than implicit active-layer consumption for
  registered processors.

## Parallel-Agent Candidate

MoNaLISA display-layer naming and tests can be handled in parallel once the
shared `DisplayLayerSpec` contract exists.

File: `imswitch/improcess/reconstructors/monalisa/result.py`

Task summary

Add MoNaLISA display-layer specs that split the semantic `Base` axis into named
viewer layers while preserving the canonical result data and save behavior.

Todo list

- Inspect `MonalisaProcessingResult.data` shape and `axis_labels`.
- Implement `display_layers()` using `DisplayLayerSpec`.
- Name base 0 `signal`.
- Name base 1 `background` when present.
- Use `base_N` fallback names for additional bases.
- Preserve all non-Base axes in each layer.
- Preserve axis scales and scale unit for retained axes.
- Add focused tests in the MoNaLISA test suite.

Do NOTs

- Do not change `MonalisaProcessingResult.data`.
- Do not change TIFF save behavior.
- Do not modify the MoNaLISA signal extraction or coefficient-to-image logic.
- Do not make registered processors consume Napari active layers implicitly.
- Do not touch unrelated ImControl or BeadRec files.

Implementation:

1. Import `DisplayLayerSpec` from `imswitch.improcess.model.result`.
2. Add a helper that returns base component names.
3. In `display_layers()`, find the `Base` axis and create one spec per base.
4. Slice the base axis out of each layer's data.
5. Remove `Base` from the display layer axis labels and scales.
6. Use per-layer finite percentiles or min/max for contrast limits.
7. Add metadata with `source_result`, `component`, and `base_index`.

Sanity checks

- Run the MoNaLISA unit tests.
- Add a test that two-base data yields `signal` and `background` layers.
- Add a test that the layer data equals the matching base slice.
- Confirm `save()` still receives and writes the original 6D data.

Commit instructions

- Commit only MoNaLISA result/tests and any shared helper touched by this task.
- Use a concise message such as `Add MoNaLISA display layer specs`.
