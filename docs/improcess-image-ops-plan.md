# ImProcess image-operations plan

Close the gap between ImProcess and the everyday ImageJ/Fiji pixel-plumbing
layer, and reorganize the toolbars so display actions, data operations, tools
and plugins each have an honest home. Analysis-side features (colocalization,
FRC, SMLM, drift, segmentation) are already stronger than base ImageJ — this
plan targets the thin base layer underneath them.

Decisions taken with Lenny (2026-07-15):

- Stack/Combine is fully rank-general: N same-shaped inputs of any ndim gain
  one new leading axis (two 2D → 3D, two 3D → 4D, ...). Identical shape is a
  hard requirement — no pad/crop-to-common (maybe later).
- Toolbar reorg ships without a layout-migration shim. Persisted layout state
  only covers the pyqtgraph dock area, not toolbars; stale dock entries fail
  safely and get overwritten on the next save.
- The one-star ImageJ gaps (reslice/orthogonal views, FFT/bandpass, projection
  toolbar modes) are explicitly OUT of scope — not relevant right now.

## Phase 1 — Stack/Combine processor + dialog (DONE when: merge two loaded
images/stacks from the UI into a new stack)

The existing `channel-merge` processor already stacks same-shaped results, but
it is hard-coded to a new leading `C` axis, has no dialog (no axis choice, no
input order, no output name), and its toolbar button silently disables when
inputs are incompatible instead of saying why.

1. New processor package `imswitch/improcess/processors/combine/`:
   - id `stack-combine`, name "Stack/Combine", category "Dimensions and
     channels", kinds `("image",)`.
   - `stack_results(results, axis_label, name)` — `np.stack` along a new
     leading axis labelled Z/T/C (or custom). Rank-general. New axis scale 1.0;
     labels/scales/`scale_unit` propagate from the first input like
     channel-merge. When the label is `C`, attach the "Channels" view mode so
     it renders like a channel merge.
   - `concatenate_results(results, join_axis, name)` — `np.concatenate` along
     an existing shared axis (append stacks: grow a T-series, add Z planes).
   - `combine_compatibility(results, mode, ...)` — returns (ok, reason) so the
     UI can SHOW why a combine is impossible instead of a dead button.
   - **Metadata must match, not just shape** (review finding): identical shape
     alone would let same-shaped but physically different arrays silently
     become one calibrated stack. Compatibility requires ALL inputs to agree
     on axis labels, axis scales (including the concatenate join axis) and
     `scale_unit`; a mismatch surfaces as the dialog's reason message (the
     user-facing warning), never as silent first-input metadata propagation.
   - **Duplicate axis labels are blocked** (review finding): stacking
     `[Z, Y, X]` inputs under a new `Z` would yield `[Z, Z, Y, X]`, and
     label-driven helpers (`resolve_axis`) take the first match. The
     compatibility check rejects a new-axis label already present in the
     inputs; the dialog defaults to the first of Z/T/C/S not in use
     (`default_stack_axis_label`).
   - **Compatibility is metadata-only** (review finding): it must use
     `shape_for_result` / `axis_labels_for_result` / `axis_scales_for_result`
     and never materialize lazy pixel data — proven by a shape-only-array
     test, same protection the subset/split enablement tests use.
   - `apply(result, params)` takes `params["results"]` (multi-input, same
     convention as channel-merge); raises ValueError with the reason.
2. `view/StackCombineDialog.py` (same `get_params` classmethod pattern as
   `StackSubsetDialog`): input list with order up/down, mode (stack new axis /
   concatenate along axis), axis-label choice, output name, and a live
   compatibility message; OK disabled with the reason shown when incompatible.
   - **Multi-layer results are expanded** (review finding): a result whose
     canonical data groups several components (`display_layers`) is listed
     layer by layer via the existing `processor_input_choices()` accessor —
     the whole result checked by default plus one checkable entry per
     non-context component (`DisplayLayerProcessingResult`), so individual
     layers of heterogeneous results can be combined without hiding them
     behind the container result.
3. Toolbar action "Stack/Combine..." in ImageToolbarController: takes the
   multi-selection from the reconstruction list
   (`_selectedProcessingResults`), opens the dialog, publishes via
   `_publishResults`. Enabled when >= 2 image results are selected.
4. Keep `channel-merge` untouched as the one-click C-merge.
5. Tests: pure-function geometry/label/scale tests (2×2D→3D, 2×3D→4D,
   concatenate, mismatch reasons), registry contract (automatic), dialog
   param extraction.

## Phase 2 — Toolbar reorg

Split along the result-unification invariant: display-only actions never
publish a result; operations always do.

- **Image** (display): auto contrast, brightness/contrast, reset contrast,
  LUT, channels, reset view.
- **Image operations** (publish results): duplicate, crop/substack, max
  projection, split stack, split channels, merge channels, stack/combine,
  make composite, make RGB — plus the Phase 3 ops as they land.
- **Tools** (renamed from "Analysis tools"): load-tool combo with the built-in
  panels (Graph, Profile, ROI manager, ROI stats, processor panels).
- **Plugins** (new): drop-in plugin panels get their own combo, plus the
  plugin-store / reload / open-folder actions currently buried in the
  Analysis menu submenu.

Menus mirror the same split. New toolbar objectNames; no migration shim (see
decisions above).

**Origin-aware enumeration** (review finding): the split needs more than an
update to `runtime_analysis_tool_choices` — today `available_processor_specs()`
merges `_AVAILABLE_PROCESSOR_CLASSES` and `_USER_PROCESSOR_CLASSES` into one
list, and the plugin-reload path refreshes that same merged list. Phase 2 adds
origin-separated enumerations (built-in-only vs user-plugin specs) in
`processors/__init__.py`, feeds the Tools combo from the built-in set and the
Plugins combo from the user set, and updates BOTH population paths together:
startup wiring and the `sigReloadPluginsRequested` refresh, so a hot-reloaded
plugin appears in the Plugins combo and never leaks into Tools.

## Phase 3 — Standard image operations (ImageJ parity, the ★★★/★★ set)

Each is a normal registry `Processor` (single-input unless noted), so it gets
the generic panel, result publishing, and the contract tests for free. Order
within the phase:

1. **Image calculator** (multi-input, Process ▸ Image Calculator): add,
   subtract, multiply, divide, min, max, average, difference of two selected
   results. Reuses Phase 1's multi-select plumbing and compatibility check;
   32-bit float output by default to avoid clipping. NOTE (review finding):
   the generic ResultProcessorWidget feeds a processor ONE selected input +
   params, so being a registry Processor does not give multi-input UI for
   free — the calculator ships its own dialog + toolbar action exactly like
   Stack/Combine does (or waits for a generic multi-input panel extension,
   which is not planned here).
2. **Math** (Process ▸ Math): add/subtract/multiply constant, invert, log,
   exp, gamma, square root — one processor with an operation combo.
3. **Filters** (Process ▸ Filters): gaussian blur, median, mean, unsharp mask
   — one `filter` processor with a method combo (scipy.ndimage; 2D applied
   slice-wise over leading axes, sigma/radius parameter).
4. **Transform** (Image ▸ Transform): rotate 90° CW/CCW, flip horizontal /
   vertical (axis-label aware: operates on the X/Y axes wherever they sit).
5. **Scale/resize + type conversion** (Image ▸ Scale, Image ▸ Type):
   downsample/upsample X/Y by factor with interpolation choice; convert
   dtype (8/16/32-bit) with optional rescale of the data range.
6. **Background subtraction** (Process ▸ Subtract Background): rolling-ball /
   top-hat background removal (skimage.restoration.rolling_ball), radius
   parameter, slice-wise over leading axes.
7. **Binary/label morphology** (Process ▸ Binary): fill holes, erode, dilate,
   open, close, watershed split — operates on `labels`-kind results
   (segmentation output), so `kinds = ("labels",)`.

Out of scope (decided): reslice/orthogonal views, FFT/bandpass, projection
mode dropdown in the toolbar, pad/crop-to-common-shape combining.
