# ImProcess result unification — design doc

**Status:** Proposed (2026-07-08). Audit complete; phased plan below not yet
started. Refined with the maintainer's "one selected result, many visible
layers" concern: **full unification** — every producing operation yields a
`ProcessingResult`, persistent `napari add_*` lives only in the one render path,
interactive tools are cleanly separated, and multi-layer overlays are owned by
the selected result rather than floating independently.

**Scope:** `imswitch/improcess/` — the result model, the reconstruction viewer,
the analysis panels, and the processor/reconstructor plugins that feed them.

Supersedes the layer-side half of the "Results list vs. napari layers" contract
documented in `docs/improcess.rst` (that section will be updated in the final
phase). Builds on [improcess-analysis-widgets.md](improcess-analysis-widgets.md)
(which introduced the panels) and the SMLM `LocalizationResult`
([smlm-localization-port.md](smlm-localization-port.md)).

## Problem (from maintainer)

> "The difference between the reconstruction pane and the napari layers isn't
> 100% clean. E.g. the projection widget creates a new napari layer instead of a
> new reconstructed object. Generalize this for ALL reconstructors and
> processors."

## Audit — two parallel "result" worlds

**World A — the `ProcessingResult` pipeline (canonical).** A reconstructor or
processor returns a `ProcessingResult`; the producer emits
`CommunicationChannel.sigResultProduced`; the result lands in the
**reconstruction list** (`ReconstructionView.reconList`); the viewer renders it;
and `plot_payloads()` / `table_records()` auto-wire to the Graph and
Results-table docks (`GraphController`, `ResultsTableWidget`). The Image toolbar
(`ImageToolbarController`) and the generic `ResultProcessorController` both use
this path. Operations that are already correct: *duplicate, crop/substack,
max-projection, split stack, split channels, merge channels, make composite,
make RGB*.

**World B — direct napari layers (the smell).** Three legacy analysis panels
take the raw `napariViewer` and call `viewer.add_*` directly. Their output only
ever becomes a floating napari layer — never a reconstruction-list entry, so it
cannot be selected as the "current result", further processed, saved, or plotted
through the pipeline:

| Panel | Direct call(s) | Registered processor that already does it as a result |
|---|---|---|
| `view/ProjectionWidget.py` | `add_image` | `ProjectionProcessor` |
| `view/SegmentationWidget.py` | `add_labels` (commit; preview is allowed ephemeral overlay) | `SegmentationProcessor` → `SegmentationResult` |
| `view/MulticolorWidget.py` | `add_image`, `add_shapes`, `add_points` | `MakeCompositeProcessor` / `MakeRGBProcessor` (partial) |

The projection case the maintainer hit exists **twice**: the toolbar's "Max
projection" produces a `ProcessingResult`; the Projection *panel* produces a bare
layer. Same for segmentation.

### Two root-cause gaps enable World B

1. **The render path is image-only.** `model/result.py::DisplayLayerSpec` and
   `view/ReconstructionView.py::setDisplayLayers` only ever `add_image`. There is
   no way for a result to declare itself as a *labels*, *points*, or *shapes*
   layer. Consequence: even `SegmentationResult` (whose `data` **is** an
   `int32` labels array) renders as a grey image through the default `setImage`
   path, and the panels reach for `add_labels`/`add_points` directly because the
   pipeline cannot express those layer types.
2. **Panels predate the registry.** `ProjectionWidget`/`SegmentationWidget`/
   `MulticolorWidget` are constructed with only `napariViewer` and were written
   before `Processor` + `ResultProcessorController` existed, so they never learned
   to publish results. (`ResultProcessorController.runProcessor` is exactly the
   generic "run a registered processor on the selected result, publish via
   `sigResultProduced`" path they should use.)

### What is already right (do not disturb)

- `Processor.apply(result, params) -> ProcessingResult | ProcessorOutput` and
  `normalize_processor_output` — clean, modality-agnostic contract.
- `ResultProcessorController` — generic run→publish for any registered processor.
- `GraphController` auto-renders any result's `plot_payloads()`; the
  results-table dock consumes `table_records()`/`table_columns()`.
- `DisplayLayerSpec` + `applyDisplayLayerSettings` + per-layer persistence — the
  multi-layer plumbing exists; it is only missing a layer *kind*.
- The task #5 "pragmatic sync" (main image layer named after the selected
  result; recon-list selection re-activates it) — this plan finishes that job.

## Design — collapse to one world

**Invariant:** the reconstruction list of `ProcessingResult`s is the single
source of truth. napari is a *pure, disposable rendering* of the currently
selected result's declared display layers plus ephemeral preview overlays. The
**only** code that calls `viewer.add_*` for persistent content is the one render
path in `ReconstructionView`. Everything that produces something visible or
further-processable is a `ProcessingResult`.

Important refinement: **one selected reconstruction result may render as many
napari layers**. This is required for workflows like segmentation, where the
labels are only interpretable when seen over the source image. The ownership
model is:

- The reconstruction-list selection defines the active processing target.
- That selected result owns the napari layers shown for it (`image`, `labels`,
  `points`, `shapes`, etc.).
- Napari layer visibility and layer selection remain useful for inspection, but
  they do not silently redefine processor input.
- Processor input is either the selected result, one explicit component of it,
  or named input slots for processors that genuinely need multiple results.

This avoids making arbitrary napari multi-selection the meaning of "current
processor input", while still allowing rich overlays in the viewer.

Three pillars.

### Pillar 1 — Typed, owned display layers

Extend `DisplayLayerSpec` with a `kind` field and lightweight ownership/role
metadata:

```python
kind: str = "image"      # "image" | "labels" | "points" | "shapes" | ...
role: str = "primary"    # "primary" | "context" | "overlay"
component: str | None = None
layer_kwargs: dict | None = None
```

`kind` selects the napari add/update path. `role` tells the processing UI how to
interpret the layer: `primary` is the result's canonical output, `context` is a
background/source layer shown to interpret the output, and `overlay` is an
auxiliary mask/points/shapes layer. `component` is the stable id used for
display-setting persistence and explicit component inputs. `layer_kwargs` carries
kind-specific napari options (e.g. `properties`/`size`/`face_color` for points,
`shape_type`/`edge_color` for shapes).

Extend `ReconstructionView.setDisplayLayers` to dispatch on `kind`
(`add_image`/`add_labels`/`add_points`/`add_shapes`/…) instead of always
`add_image`. Everything stays backward-compatible: default `kind="image"` and
`role="primary"`.

Now **one result** can carry an image + its mask overlay + detected points as
separate typed layers, all as **one reconstruction-list entry**:

- `SegmentationResult.display_layers()` →
  `[image(source, role="context"), labels(mask, role="primary")]`.
- `LocalizationResult` can offer a histogram image plus a low-count gated
  `points` overlay.
- Multicolor composites carry their per-channel image layers (already do) plus
  annotation overlays with explicit roles.

Pure model + view change; unit-testable with fake viewers (see
`test_reconstruction_viewer_sot.py` fakes).

### Pillar 2 — Panels are processor UIs, not result producers

Collapse "analysis panel" and "processor". A panel becomes just a registered
processor's `make_param_widget()` hosted in a dock, wired to the generic
`ResultProcessorController` run→publish path. Concretely:

- Delete every `viewer.add_*` from `ProjectionWidget`/`SegmentationWidget`/
  `MulticolorWidget`, and their bespoke `_active_image_layer` scanning (the code
  hand-aligned in task #5 goes away entirely — there is one accessor).
- "Run"/"Segment"/"Project" runs the registered processor on the current result
  and emits `sigResultProduced`; the output appears in the list and renders via
  Pillar 1 (labels as labels, etc.).
- **Live preview stays ephemeral.** The SMLM detection preview and segmentation
  mask preview remain clearly-marked, non-persisted napari overlays for tuning;
  **commit produces a result.** Rule: *preview = ephemeral overlay, commit =
  result.*
- When a processor can operate on a specific display component, the generic
  processor widget offers explicit component choices (`Whole result`,
  `labels`, `source image`, etc.) from `processor_input_choices()` rather than
  inferring from napari's active layer.

Net effect: one implementation per operation (the processor), one behavior
(produces a result). Any future processor gets a consistent panel + list
behavior for free.

### Pillar 3 — A crisp producer/tool rule

Some panels are genuinely *interactive tools* on the current rendering, not
producers: ROI manager (draw shapes), Profile (line + live plot), ROI stats /
FRC / PSF / colocalization (measure the active layer). They stay napari-side,
but (a) they read the current result through the one canonical accessor, and
(b) where they yield a persistent artifact they publish a result too (e.g. "ROIs
→ shapes result", "profile → table/plot result"). The documented rule:

> **Producing → `ProcessingResult` (list). Interacting / measuring → napari tool
> on the active result's layers.**

Multi-selection is deliberately not the default primitive for processing. It is
reserved for processors whose contract explicitly needs multiple inputs (merge,
registration, colocalization, compare/overlay). Those UIs should expose named
slots such as `Image A`, `Image B`, `Mask`, or `Reference`, and populate each
slot from reconstruction-list results or explicit result components. A raw
"whatever napari layers happen to be selected" interpretation is too ambiguous
for repeatable processing.

## Design decisions (resolved 2026-07-08)

Two load-bearing questions the audit surfaced, decided before Phase 1:

1. **Non-image primary vs. the protected `imgLayer`.** `imgLayer` is created
   `protected=True`; `EmbeddedNapari` monkeypatches layer removal to refuse
   deleting protected layers, so `imgLayer` is permanently present and
   permanently an napari *Image* layer (a layer's type is fixed at creation —
   reassigning `.data` cannot turn it into labels/points). Today
   `setDisplayLayers` forces every result's layer 0 through `imgLayer`, so a
   labels-primary result (segmentation) cannot render as its natural type.
   **Decision:** decouple "primary render target" from the `imgLayer` object.
   The view tracks a `_primaryComponent` and owns a dynamic set of managed
   layers keyed by `component`. Reuse `imgLayer` when the primary/context is an
   image (keeps the stable reference the contrast toolbar and active-image
   accessors depend on, avoids churn and the vispy ndim-patch); when the
   primary is non-image, hide `imgLayer` (1×1, invisible — it can't be removed)
   and render the labels/points as a managed primary layer that the toolbar and
   active-image accessors target by role. This finishes the task #5 job without
   the layer-naming workaround.

2. **Source context image for segmentation.** `SegmentationAnalysis` keeps
   `labels`/`mask`/`processed_image` but not the raw source, and
   `SegmentationResult` receives only the analysis — so "labels over the source
   image" needs the source threaded in. **Decision:** the segmentation
   *processor* (which has the input `result` in `apply()`) stores the segmented
   **2D slice** on `SegmentationResult` and exposes it as a `role="context"`
   image layer. Store the slice itself (bounded ~one frame, self-contained,
   saveable, survives source deletion) — not a live reference to the source
   result (avoids lifetime coupling and retaining a whole stack).

Smaller decisions folded in:

- `processor_input_choices()` **skips `role="context"` layers** — a context
  image is display-only, never offered as a processor input.
- Typed-layer persistence keys **strictly on `component`** (today
  `_imageLayerId` falls back to `name`), so labels-vs-context display settings
  round-trip unambiguously.
- **Phase 1 stays tight:** land the `kind`/`role`/`component`/`layer_kwargs`
  machinery + segmentation-as-source+labels only. The `LocalizationResult`
  points overlay is independent and deferred to a later phase.

## Phased plan

Each phase is independently shippable and testable. Load-bearing view dispatch
(Pillar 1) done in-house; panel reroutes (Pillar 2/3) are OpenHands-friendly with
review + gated commit.

- ✅ **Phase 1 — Typed, owned layer contract** (2026-07-08, committed
  `8d338180`). `DisplayLayerSpec.kind`/`role`/`component`/`layer_kwargs`;
  `setDisplayLayers` dispatches on kind and decouples the primary target from
  the protected imgLayer (reuse for first image, hide for non-image primary,
  track `_primaryLayer`/`_primaryComponent`); `SegmentationResult` carries the
  source slice and renders as context image + real labels on one list item;
  context layers excluded from `processor_input_choices()`. No panel rerouting
  yet. 109 tests pass (`test_typed_display_layers.py` new).
- ✅ **Phase 2 — Producing panels → processors** (2026-07-08, committed
  `975d98a7`). Projection retired → generic result-processor panel;
  Segmentation keeps its custom panel (preview + ROI export) but the Segment
  button emits `sigRunRequested` → `ResultProcessorController` publishes a
  `SegmentationResult` (renders source+labels). `_wire_runtime_result_processors`
  generalized to bind a controller to any panel exposing `sigRunRequested`.
  Registry-wide generality tests added (every processor conforms + an
  architectural allowlist test locking `viewer.add_*` to the render path,
  the seg preview, and the deferred Multicolor). Review caught + fixed a
  param-key mismatch that would have silently dropped the panel's settings on
  commit. 162 tests pass.
- ⬜ **Phase 3 — Multicolor split.** Separate true *results* (composite/RGB
  images) from *annotations* (scale bar, ROIs, points). Results publish;
  annotations become typed overlay layers on the active result. (Trickiest —
  multicolor mixes display styling and image production.)
- ⬜ **Phase 4 — Tool/producer audit.** ROI manager, Profile, ROI stats, FRC,
  PSF resolution, colocalization: confirm each is tool-only or add a "→ result"
  publish; delete now-redundant `_active_image_layer` copies in favor of the one
  accessor.
- ⬜ **Phase 5 — Docs.** Update `docs/improcess.rst` "Results list vs. napari
  layers" with the finalized invariant and the producer/tool rule; ROADMAP M12
  note.

## Open questions / risks

- **Points vs. histogram for SMLM.** `LocalizationResult` currently renders a
  lazy histogram image (fast, handles millions of points). A real points layer
  is nicer at low counts but expensive at high counts. Proposal: keep the
  histogram as the default `data`, offer a points display layer gated by a count
  threshold. Decide in Phase 1.
- **Multicolor annotations.** Scale bars / ROI outlines are arguably pure
  display decoration, not results. Phase 3 must draw the line carefully so we
  don't turn every cosmetic overlay into a list entry.
- **Persistence.** Typed layers must round-trip per-result display settings
  (levels/colormap/visibility) the same way image layers do today
  (`_display_layer_settings`, `applyDisplayLayerSettings`).
- **Primary layer vs. context layer.** Some results, especially segmentation,
  need a context image behind the canonical output. The result-list item remains
  one result; roles decide which layer is canonical (`primary`) and which is
  supporting context (`context`). The renderer must support a non-image primary
  layer without forcing labels through the protected image layer.
- **Multi-selection ambiguity.** Multi-result workflows should be explicit
  named-input processors, not implicit napari layer selection. This may require
  a small reusable "input slots" widget after Phase 2.
- **`ProcessingResult` still demands a viewable `data` array.** Pure-table
  results (colocalization metrics, FRC numbers) fake one or lean on
  `plot_payloads()`. Longer term, `data` could become optional with the result
  declaring only display layers + payloads; out of scope here but noted.
