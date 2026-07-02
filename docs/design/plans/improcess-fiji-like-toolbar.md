# ImProcess Fiji-like Image Toolbar

**Status:** Implementation in progress — toolbar shell, brightness/contrast,
duplicate, crop/substack, max-projection, split-stack and split-channel slices
landed; LUT/channel controls, channel-merge, make-composite, make-RGB and
panel shortcuts landed; semantic QtAwesome icon mapping landed
**Date:** 2026-07-01
**Scope:** Add a persistent image-operation toolbar to ImProcess with
Fiji/ImageJ-like stack, channel, LUT, and brightness/contrast workflows.
**Related code:** `ImProcessMainView`, `ReconstructionView`,
`ReconstructionViewController`, `ProcessingResult`, `Processor`, runtime
analysis tools, and the lazy `DataObj` / virtual image source layer.

---

## 1. Goal

ImProcess already has a plugin-backed processing model and a runtime analysis
toolbar. What is missing is the always-present "image workbench" surface users
expect from Fiji/ImageJ: quick access to contrast, LUT, channels, stack
operations, projections, duplication, cropping/substacks, and basic ROI-oriented
inspection.

The toolbar should be visible in normal processing sessions, independent of
which reconstructor is active. Actions are enabled or disabled from the current
viewer/result state, not from the selected data file.

The key UX rule is Fiji-like behavior with ImProcess contracts:

- display-only operations should update the active Napari layer and
  `ProcessingResult.display_levels` without changing pixel data;
- data-changing operations should produce a new `ProcessingResult` and append it
  to the reconstruction list through `sigResultProduced`;
- heavyweight or parameter-rich workflows should open their existing panel or a
  focused dialog instead of crowding the toolbar.

---

## 2. Fiji/ImageJ Reference Points

Fiji/ImageJ makes a useful role model because its frequently used image
commands are shallow and always nearby:

- `Image > Adjust > Brightness/Contrast...` is an interactive display-range
  control with min/max settings, auto/reset/set/apply style actions. ImageJ
  documents that standard brightness/contrast changes are display mappings for
  grayscale images until explicitly applied to pixels.
  Reference: [ImageJ brightness/contrast](https://imagej.net/learn/brightness-and-contrast),
  [ImageJ user guide - Image menu](https://imagej.net/ij/docs/guide/146-28.html).
- `Image > Color` exposes composite/channel workflows such as channel tools,
  split channels, merge channels, make composite, make RGB, and LUT selection.
  Reference: [ImageJ color image processing](https://imagej.net/imaging/color-image-processing).
- `Image > Stacks` exposes substack, stack-to-images, images-to-stack, and
  projection workflows. These map directly to ImProcess result operations.
  Reference: [ImageJ image menu](https://imagej.net/ij/docs/menus/image.html).

ImProcess should mirror the mental model, not the exact menu tree. Napari is the
viewer, so per-layer contrast, visibility, colormaps, and RGB/composite handling
should use Napari semantics where possible.

---

## 3. Toolbar Layout

Add a second toolbar in `ImProcessMainView`, separate from the existing runtime
analysis toolbar:

- **File/result:** quick load, save current, save all.
- **View/LUT:** auto contrast, brightness/contrast dialog, reset contrast, LUT
  menu, layer visibility/channel selector.
- **Stacks:** duplicate, crop/substack, split stack, project, reslice/view mode.
- **Channels/color:** split channels, merge channels, make composite, make RGB.
- **Measure/ROI:** ROI manager, histogram, line profile, measurements.
- **Panels:** projection, segmentation, FRC, PSF, colocalization, multicolor.

Use `QToolBar` with icon-first actions and tooltips. Keep text only for combo
boxes or where an icon would be ambiguous. The existing runtime "Load tool"
combo remains for optional panels/processors.

The first implementation can use standard Qt icons or the existing ImSwitch icon
path; a later visual pass can replace them with a curated microscopy icon set.

---

## 4. Action Model

Create a small descriptor layer, for example
`imswitch/improcess/model/image_actions.py`:

```python
@dataclass(frozen=True)
class ImageActionSpec:
    id: str
    text: str
    tooltip: str
    category: str
    icon_name: str | None = None
    shortcut: str | None = None
    checkable: bool = False
    kind: Literal["view", "result", "panel"] = "view"
```

The view owns `QAction` creation. A new `ImageToolbarController` owns enablement
and execution. It listens to:

- `CommunicationChannel.sigCurrentResultChanged`
- Napari active-layer changes when available
- reconstruction-list selection changes through `ReconstructionViewController`

Execution tiers:

1. **View actions** operate on the active Napari image layer:
   auto contrast, reset contrast, LUT changes, visibility, histogram preview.
2. **Result actions** operate on the active `ProcessingResult` and emit a new
   result:
   duplicate, crop/substack, split stack, project, split channels, merge/RGB.
3. **Panel actions** call `ensureRuntimeAnalysisWidget(...)` or open a focused
   dialog:
   ROI manager, projection, segmentation, FRC, PSF, colocalization, multicolor.

This keeps the toolbar thin and prevents GUI-only code from leaking into
processors.

---

## 5. Brightness / Contrast Dialog

This should be the first toolbar feature because it improves every modality and
extends the current simple min/max behavior in `ReconstructionViewController`.

### Required behavior

- Open from a toolbar action and a menu action.
- Show a histogram for the active image layer or active channel.
- Numeric `Min` and `Max` fields.
- Sliders for display min/max over `contrast_limits_range`.
- Buttons:
  `Auto`, `Reset`, `Set`, `Apply`, `Current Slice`, `Whole Stack`.
- Percentile/saturation control for auto contrast.
- Scope selector:
  active layer, all image layers, active channel, all channels.
- Mode selector:
  display only, bake into new result.
- Preserve per-result display levels:
  call `ProcessingResult.setDispLevels(...)` for the active result when the
  change is display-only.

### Data handling

The dialog must not assume the full array is cheap:

- for NumPy data, use direct `np.nanpercentile` / histogram;
- for lazy or future virtual arrays, sample by slices or chunks;
- expose "Use stack histogram" vs "current plane" like ImageJ, but default to
  stack/global for consistency with current ImProcess display-level behavior;
- never force `DataObj.data` materialization from a display-only histogram.

### Implementation shape

Add:

- `view/ContrastBrightnessDialog.py`
- `model/contrast.py` with pure helpers:
  `finite_range`, `sample_histogram`, `auto_levels`, `clip_or_rescale`
- controller methods on `ImageToolbarController`:
  `open_contrast_dialog`, `set_display_levels`, `apply_levels_to_new_result`

The "Apply" button should create a new result, not mutate the source result in
place. That keeps the processing history visible in the reconstruction list.

---

## 6. Essential Fiji-like Operations

### P0 - Always-needed view operations

- Auto contrast.
- Reset contrast to data min/max.
- Brightness/contrast dialog.
- LUT/colormap selection.
- Histogram for active layer.
- Reset view / fit view.

These are display operations and should not create new results unless the user
chooses "Apply".

### P1 - Stack operations

- Duplicate active result or active slice.
- Crop/substack by axis ranges.
- Split stack into per-slice results.
- Make substack from selected range.
- Project along an axis using max/mean/sum/median/std.
- Reuse the existing `projection` processor for projection outputs.

For large arrays, crop/substack and duplicate should keep views/lazy slices when
possible and materialize only when saving or applying an operation that requires
contiguous NumPy data.

### P2 - Channel and color operations

- Split channels from an axis labeled `C`, `Channel`, `Base`, or an explicitly
  chosen axis.
- Merge selected grayscale results into a channel stack.
- Make composite:
  represent channels as separate `DisplayLayerSpec` layers with independent
  LUTs and contrast levels.
- Make RGB:
  explicitly create an RGB result with channel-last data for display/export.
- Per-channel LUT and contrast:
  active channel first, all channels as an explicit scope.

Composite should be preferred for analysis because it preserves original
bit-depths and channels. RGB is useful for figures and export, but it is a
derived visualization result.

### P3 - Measurement and ROI conveniences

- Open ROI manager from the toolbar.
- Quick histogram for active layer/ROI.
- Quick measure for active layer/ROI, appending to `ResultsTableWidget`.
- Line profile action, reusing the existing profile panel.
- Segmentation/threshold shortcut, opening the segmentation panel.

---

## 7. Processor Additions

Some toolbar commands should become processor plugins so they can also be used
from batch/chain workflows:

| Processor id | Purpose |
|---|---|
| `stack-subset` | Crop/substack by labeled axes or numeric ranges |
| `stack-split` | Split one axis into multiple `ProcessingResult`s |
| `channel-split` | Split `C`/`Base`/chosen axis into per-channel results |
| `channel-merge` | Merge selected compatible results into one channel stack |
| `make-composite` | Produce a result with `DisplayLayerSpec` channel layers |
| `make-rgb` | Produce an explicit RGB visualization/export result |
| `contrast-apply` | Bake display min/max into a new scaled/clipped result |

Toolbar result actions can call these processors directly, then emit their
outputs through `sigResultProduced`.

`Processor.apply(...)` now supports both single-result returns and
`ProcessorOutput` multi-result returns. Controllers normalize both shapes and
emit every produced `ProcessingResult` through `sigResultProduced`, with the
last produced result becoming current.

---

## 8. Integration Points

Minimal code touch points for phase 1:

- `ImProcessMainView`
  - add `_imageToolbar`
  - add action creation helpers
  - expose `setImageToolbarActionsEnabled(...)` if the controller needs a batch
    state update
- `ImProcessMainController`
  - instantiate `ImageToolbarController`
  - pass it the main view, communication channel, and reconstruction controller
- `ReconstructionView`
  - expose active image layer helper
  - expose all image layers helper
  - keep `getImageDisplayLevels`, `setImageDisplayLevels`,
    `setImageDisplayLevelsRange`
- `ReconstructionViewController`
  - expose active result and current display image/layer state
  - add a display-level setter that updates both layer and result metadata
- `ProcessingResult`
  - keep `display_levels`
  - optionally add `copy_with(...)` helper to simplify duplicate/crop processors

Do not overload `DataObj.data`. The toolbar operates on `ProcessingResult`
objects and active viewer layers. When it needs raw source data later, it should
use `data_handle` / virtual array APIs explicitly.

---

## 9. Test Plan

Unit tests:

- contrast helper auto levels on uint16, float, NaNs, constant arrays;
- histogram sampling does not materialize virtual arrays;
- crop/substack preserves axis labels/scales;
- split/merge channels round trips a small `CZYX` array;
- make RGB clips/scales correctly and labels the result explicitly.

Qt/offscreen tests:

- toolbar constructs under `QT_QPA_PLATFORM=offscreen`;
- actions are disabled with no active result;
- contrast dialog updates active layer levels;
- display-only contrast persists when switching reconstruction-list items;
- "Apply" creates a new result and leaves the source result unchanged.

Regression tests:

- MoNaLISA display-layer results keep independent contrast per layer;
- SNOUTY `ZYX` / `TZYX` projections and channel tools stay enabled only where
  their axis predicates make sense;
- lazy HDF5/Zarr/TIFF source handles are sampled, not materialized, by histogram
  and auto-contrast.

---

## 10. Implementation Sequence

1. Add `ImageToolbarController` and a minimal toolbar shell with disabled
   actions when no result is selected. **Implemented.**
2. Implement `Auto`, `Reset`, and `Brightness/Contrast...` display-only paths.
   **Implemented.**
3. Persist display levels through `ProcessingResult.display_levels` and verify
   reconstruction-list switching. **Implemented for the active image layer.**
4. Add projection and duplicate actions by reusing existing result/processor
   contracts. **Implemented for whole-result duplicate and one-click max
   projection.**
5. Add stack subset/split processors and wire toolbar dialogs. **Implemented
   for crop/substack range selection and one-click split stack.**
6. Add channel split/merge/composite/RGB processors. **Implemented.**
7. Promote panel-open shortcuts for ROI manager, projection, segmentation, FRC,
   PSF, colocalization, and multicolor.
8. Add menus mirroring the toolbar categories so keyboard users can discover
   the same operations without icons.

The first shippable slice should be steps 1-3. That gives users a Fiji-like
brightness/contrast workflow immediately and creates the controller/action
backbone for the remaining operations.

---

## 11. First Slice Notes

Implemented files:

- `model/contrast.py` — finite range, percentile auto-levels and histogram
  helpers.
- `view/ContrastBrightnessDialog.py` — modeless histogram/min/max dialog.
- `controller/ImageToolbarController.py` — action enablement and display-level
  execution.
- `processors/base.py` — `ProcessorOutput` and output normalization for
  processor commands that emit multiple results.
- `processors/stack_split` and `processors/channel_split` — Fiji-like stack and
  channel split processors.
- `processors/stack_subset` and `view/StackSubsetDialog.py` — Fiji-like
  crop/substack range selection with per-axis first/last/step controls.
- `ProcessingResult` display settings and `processors/make_composite` —
  persistent LUT/contrast settings for display layers and composite channel
  rendering.
- `view/ChannelControlsDialog.py` — per-display-layer visibility and LUT
  controls, including the `hot` LUT.
- `processors/channel_merge` and `processors/make_rgb` — merge selected
  compatible grayscale results into channel stacks and bake channel-like axes
  into RGB visualization/export results.
- `model/runtime_tools.py` — Fiji-like runtime panel shortcut descriptors for
  ROI manager, projection and segmentation.
- `view/icons.py` — semantic QtAwesome-backed toolbar icons with Qt fallback
  icons for environments that have not installed the new dependency yet.
- `ImProcessMainView` — persistent File/Image/Analyze menus and toolbar
  shortcuts.
- `ReconstructionView` / `ReconstructionViewController` — active-layer display
  accessors and display-level persistence.

Current scope:

- display-only Auto contrast;
- display-only Reset contrast;
- display-only LUT selection with per-result and per-display-layer persistence;
- display-only Channels dialog with per-layer visibility and LUT persistence;
- modeless Brightness/Contrast dialog;
- Duplicate active result;
- Crop/Substack dialog using `stack-subset`, preserving rank and axis metadata;
- one-click Max projection using the existing projection processor's default
  stack-axis selection;
- one-click Split stack using `stack-split`, publishing one result per plane;
- one-click Split channels using `channel-split`, publishing one result per
  C/Channel/Base plane;
- Merge channels using `channel-merge`, publishing one C-axis stack from
  selected compatible results;
- Make composite using `make-composite`, rendering channel-like axes as colored
  display layers without baking an RGB image;
- Make RGB using `make-rgb`, publishing a channel-last RGB visualization result;
- File toolbar shortcuts for eager quick load, virtual/lazy load and save
  reconstruction;
- Analysis toolbar panel shortcuts for ROI manager, projection, segmentation
  and Results;
- semantic icon mapping for all always-present file, image and analysis
  actions;
- Reset view action;
- action enablement from the active result.

Next implementation slice:

- add keyboard shortcuts for the most common always-present toolbar actions.
