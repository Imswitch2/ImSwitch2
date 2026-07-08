# ImProcess result unification — design doc

**Status:** Proposed (2026-07-08). Audit complete; phased plan below not yet
started. Ambition confirmed by maintainer: **full unification** — every
producing operation yields a `ProcessingResult`, `napari add_*` lives only in
the one render path, interactive tools are cleanly separated.

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
| `view/SegmentationWidget.py` | `add_labels` (commit + preview) | `SegmentationProcessor` → `SegmentationResult` |
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

Three pillars.

### Pillar 1 — Typed display layers

Extend `DisplayLayerSpec` with a `kind` field:

```python
kind: str = "image"   # "image" | "labels" | "points" | "shapes" | "tracks" | "vectors"
```

plus optional kind-specific payload (e.g. `properties`/`size`/`face_color` for
points, `shape_type`/`edge_color` for shapes). Extend
`ReconstructionView.setDisplayLayers` to dispatch on `kind`
(`add_image`/`add_labels`/`add_points`/`add_shapes`/…) instead of always
`add_image`. Everything stays backward-compatible: default `kind="image"`.

Now **one result** can carry an image + its mask overlay + detected points as
separate typed layers, all as **one reconstruction-list entry**:

- `SegmentationResult.display_layers()` → `[image(source), labels(mask)]`.
- `LocalizationResult` can offer a real `points` layer alongside its histogram.
- Multicolor composites carry their per-channel image layers (already do) plus
  any annotation layers as typed overlays.

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

## Phased plan

Each phase is independently shippable and testable. Load-bearing view dispatch
(Pillar 1) done in-house; panel reroutes (Pillar 2/3) are OpenHands-friendly with
review + gated commit.

- ⬜ **Phase 1 — Typed layer contract.** `DisplayLayerSpec.kind` + payload;
  `setDisplayLayers` dispatch; `SegmentationResult` renders as real labels.
  No behavior change elsewhere. *(model + view, low risk)*
- ⬜ **Phase 2 — Producing panels → processors.** Reroute Projection &
  Segmentation panels through `ResultProcessorController`; drop their `add_*`.
  Preview stays ephemeral; commit produces a result. Remove per-panel layer
  scanning.
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
- **`ProcessingResult` still demands a viewable `data` array.** Pure-table
  results (colocalization metrics, FRC numbers) fake one or lean on
  `plot_payloads()`. Longer term, `data` could become optional with the result
  declaring only display layers + payloads; out of scope here but noted.
