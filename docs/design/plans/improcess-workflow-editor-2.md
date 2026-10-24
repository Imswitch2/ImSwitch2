# ImProcess Workflow Editor 2.0: a patcher canvas and interactive runs

Date: 2026-10-24

Status: Proposed. The mockups below are renders of a working spike that uses
the real plugin catalogue, the real `WorkflowDocument` and real run results;
the window around them is the proposal. Supersedes the "graph canvas" item of
Phase 4 in `improcess-workflow-editor.md`.

## Summary

Today's editor has every capability a workflow needs, but it shows a workflow
as a table of text and a form. A branching workflow's shape is only readable
in the *Inputs* column (`split.C0`, `smooth, sharp`), a port is a string in a
combo box, and running sends the results somewhere else: nothing in the editor
shows what a step produced.

The proposal, in four lines:

- **A patcher canvas** (after FL Studio's Patcher, Blender's node editor and
  ComfyUI): every step is a box with inputs on the left and outputs on the
  right; cables carry results; a port's colour says what flows (recording,
  image, labels, curve, table). A **list mode** shows the same document as a
  pipeline of cards, one click away.
- **Interactive runs inside the editor**: *Run*, *Run to here*, a per-step
  status, timing and a thumbnail of each step's output in its box, a large
  preview in the inspector. Changing a parameter marks that step and everything
  after it **outdated** and the next run re-runs only those; the rest comes
  from a cache.
- **Immediately understandable**: a start page (from the open recording, from
  a result's history, from a recipe, blank), a searchable palette, a
  "drag a cable into empty space" picker that offers only steps that accept
  what the cable carries, and plain words for every state.
- **Feasible without changing the workflow file format.** The runner can
  already start from in-memory results and skip what only fed them, which is
  the core of cached re-runs; positions and other view state go under
  `Workflow.metadata["editor"]`, which round-trips and which the runner
  ignores. New code is a canvas (custom `QGraphicsScene`, no new dependency),
  a Qt-free run session with a result cache, and three small runner and
  document extensions.

## 1. What is hard about today's editor

![Today's editor on the diamond example](../../images/workflow-editor-2/0_today.png)

*Today: `examples/improcess_workflows/split_process_merge_diamond.yaml`, step
`smooth` selected, in ImSwitch's own theme.*

Observed on the shipped examples:

1. **The shape is hidden.** The diamond (split → two filters → merge) reads as
   seven rows; who feeds whom is in the *Inputs* text. Fan-out, fan-in and
   unused outputs are invisible.
2. **Ports are strings.** `split.C0` is picked in a combo box; nothing says
   that `C0` is an image, which outputs exist before a run, or which steps
   could take it.
3. **Order is a chore.** *Up* / *Down* move rows in execution order, a detail
   a user of a graph should never need, and a move that would put a step
   before its input is refused with a message after the fact.
4. **Running is blind.** *Run* sends everything to the reconstruction list.
   There is no per-step state, no timing, no preview, and no way to run up to
   one step, look, change a parameter and try again.
5. **Adding a step is a hunt.** The palette is a long tree with no search and
   no descriptions; *Add step* inserts after the selection, so where a step
   lands depends on what happened to be selected.
6. **Space goes to the wrong things.** The file list and an issue panel that
   usually reads "No issues" take a third of the window; the parameter form,
   the part being edited, is a narrow column.
7. **Jargon.** `source`, `reconstruct`, `process`, `save`, ids like `rec`, and
   the word "port" are the file format's words, shown as the interface.

The editor's model is right (catalogue, typed parameter specs, validation, a
Qt-free document); what is missing is a view that shows the graph and the data.

## 2. Ideas, and where they come from

| Reference | What to borrow | Where it lands here |
| --- | --- | --- |
| **FL Studio Patcher** | Inputs left, outputs right; drag from a connector to a compatible one, which lights up; colour by data type (audio, events, automation); drag an input's cable end into empty space to detach it; drag onto a module's middle to pick from compatible targets; save a chain as a preset; a *Surface* of exposed controls | §4.3 gestures, §4.4 typed ports, recipes on the start page; *Surface* as "exposed controls" in Phase 4 |
| **Blender node editor** | Socket colours by type; search-to-add (Shift+A / Tab); **link-drag-search**: drop a cable in empty space and search what fits; drop a node on a cable to splice it in; mute/bypass; frames; node groups; a Viewer node | Quick-add (§4.3, mockup 4), splice, frames (Phase 3); bypass and groups wait for a format decision (Phase 4) |
| **ComfyUI** | Preview images inside nodes; re-running only nodes whose inputs changed (cache); a queue; failing node highlighted with its error | Interactive runs (§4.5): thumbnails, cached partial re-runs, error on the node |
| **KNIME** | A status light per node (not configured / configured / executed); *Execute up to here*; *Reset* | Status badges (done ✓, outdated ↻, error ✕, running), *Run to here* |
| **CellProfiler test mode** | Step through a pipeline on one image, see each module's output, then run the batch | The whole interaction model: one file interactively, then *Run over files…* |
| **Nuke** | Postage stamps (thumbnails) on nodes; any node viewable with one key | Thumbnails; *Show in viewer* |
| **Orange** | Widgets as nodes; a link is only allowed between compatible signals, and the link says what flows | Kind-checked connections; kind legend on the canvas |
| **Icy protocols** | Visual pipelines for bioimage analysis, run in batch over folders | Confirms the domain fit; batch stays the existing *Run over files…* |

Ideas specific to ImSwitch that none of them has:

- **Start from what you did.** Every GUI action is already recorded as
  provenance; *From what you just did* turns the selected result's history
  into a workflow (existing `workflow_from_provenance`).
- **Recipes are snippets.** The nine Python-step recipes become workflows on
  the start page (Source → View only → Python step → Save), built by the same
  logic as `tools/make_python_recipe_workflows.py`.
- **Curves and tables are first-class outputs.** A curve port is green and
  previews as a sparkline in its box; the inspector draws it with pyqtgraph.

## 3. The 2.0 at a glance

All six are rendered by the spike with the real catalogue and real results
(the synthetic recording from `examples/improcess_workflows/_synthetic.py`).

### 3.1 Start page

![Start page](../../images/workflow-editor-2/1_start.png)

Four ways in, with the one that runs on real data first. The right-hand panel
explains the canvas in five sentences for a first-time user and gives way to
the inspector once something is selected.

### 3.2 Graph mode after a run

![Graph mode after an interactive run](../../images/workflow-editor-2/2_graph_after_run.png)

The diamond after *Run*: every box says what it is (plugin name, step id,
the parameters that differ from the defaults), shows its output and how long it
took. `C0`/`C1` are labelled outputs of *Split stack*; the cable colour is the
kind (grey recording, blue image). The inspector holds the real parameter form
and a large preview of the selected step's output. Nothing was written to disk:
the Save box shows the file it *will* write.

### 3.3 Edit → outdated → Run to here

![Outdated steps and the step context menu](../../images/workflow-editor-2/3_outdated_run_to_here.png)

`smooth`'s radius went from 2 to 4. `smooth`, `merge` and `out` turn orange
(dashed border, ↻ badge, old thumbnail dimmed with "outdated"); the chip in the
toolbar says "3 steps outdated". *Run to here* on `merge` re-runs `smooth` and
`merge` only; `raw`, `rec`, `split` and `sharp` come from the cache. (*Bypass*
and *Group into a sub-workflow* in this menu are Phase 4 items; see §8.)

### 3.4 Drag a cable into empty space

![Quick add from a cable](../../images/workflow-editor-2/4_quick_add.png)

A cable dragged from `split › C1` into empty space opens a picker listing only
the steps that accept an image with one input, with their category and a
one-line description; typing narrows it, Enter adds the step and connects it.

### 3.5 List mode, with a real error

![List mode, stopped at an error](../../images/workflow-editor-2/5_list_mode_error.png)

The same document as a pipeline of cards; the gutter draws who feeds whom like
a git graph. This run is real: the `crosstalk` recipe on a 12-frame time lapse
fails at line 11 ("the matrix is for 2 channels, the data has 12 along axis
0"). The error sits on the step, the steps after it say they waited, the
inspector explains the failure in terms of the input and holds the code;
`raw` and `rec` stay cached, so fixing the code and pressing *Run* continues
from the failed step.

### 3.6 A Python step with curve outputs

![Python step with curve outputs](../../images/workflow-editor-2/6_python_curves.png)

`signal_trace`: two green curve ports (`level`, `area`), a sparkline per port
inside the box, the code's first lines on the box, the curve plotted in the
inspector, and the two CSV files the Save steps wrote.

## 4. Design

### 4.1 Principles

- **One document, two views, one inspector.** Graph and list are renderings of
  the same `WorkflowDocument`; selection, undo and the inspector are shared.
  Neither view owns data.
- **The file format does not change** in Phases 0–3. View state (positions,
  collapsed boxes, frames, the chosen mode) lives in `metadata.editor`. A file
  saved by 2.0 runs unchanged in every ImSwitch that runs today's files.
- **The plugin is the type authority**, as in 1.0: names, categories,
  parameter specs, arity, ports, and now output kinds come from the catalogue.
- **Interactive runs reuse the batch runner.** No second execution engine: a
  run session decides *what* to run and binds cached results; `run()` does the
  work, through the same code paths as the GUI and the CLI.
- **Memory policy controls automatic work, never results** (the
  `memory-budgets.md` principle). The cache is automatic work: evicting it
  costs a re-run, never a different answer.
- **Words, not codes.** "Outdated", "waits for 'script'", "will write
  cells_diamond.h5"; ids are shown, but under the plugin's name.

### 4.2 Layout

- **Toolbar**: workflow name; New, Open, Save; **Run**, *Run to selected*,
  *Stop*, *Auto-run*; the Graph/List switch; a status chip (ready, n outdated,
  stopped at a step, n issues) that opens the issue list.
- **Left**: search box over a categorised palette (Recent first, colour chips
  per category, descriptions as tooltips). The workflow-file list moves to the
  start page and the Open dialog.
- **Centre**: the canvas, with a kind legend and zoom/fit/tidy controls as
  overlays, or the card list.
- **Right**: the inspector (§4.6).
- **Status bar**: what the last run did ("Ran 7 steps in 0.2 s · results kept
  in memory; nothing saved yet").

### 4.3 Canvas gestures

| Gesture | Effect |
| --- | --- |
| Drag from palette / double-click empty canvas / Tab | Add a step (Tab and double-click open the search) |
| Drag from an output to an input | Connect; compatible inputs highlight, incompatible ones dim |
| Drop a cable on a box's body | Connect to its first free compatible input |
| Drop a cable in empty space | Quick-add picker filtered by kind and arity; the new step is connected |
| Drag an input's cable end into empty space | Disconnect |
| Drop a box on a cable | Splice it in between |
| Right-click a box | Run to here, Re-run from here, Show output in viewer, Duplicate, Delete |
| Right-click a cable | Insert step…, Delete |
| Del / Ctrl+D / Ctrl+C, Ctrl+V | Delete, duplicate, copy/paste (as YAML, so a fragment pastes into a text editor and back) |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo of every document operation |
| F5 / F6 / Esc | Run / Run to here / Stop |
| F / H | Fit / tidy up (layered auto-layout) |

Execution order is no longer the user's concern. Connecting reorders the
step list to a valid order (a stable topological sort that moves as little as
possible), and a connection that would make a cycle is refused while dragging,
not after.

### 4.4 Typed ports: output kinds

Port colours and kind-aware filtering need to know what a step produces before
it runs. Processors declare what they *accept* (`kinds`) but not what they
produce (the 1.0 plan's §3.6). The addition:

- `output_kinds(params, input_kinds) -> {port_or_pattern: kind}` on
  `Processor`, defaulting to "same as the first input" for 1:1 processors,
  with declarations on the built-ins that differ (Segmentation → labels, FRC →
  curve, PSF resolution → table, Make RGB → rgb, Make composite → composite,
  table-to-localizations → localization, the Python step → "any" until run).
- A contract test runs every built-in on the representative inputs of
  `test_result_kind_matrix.py` and holds the declared kind to the kind of
  what it actually returned.
- After a run, the actual kind replaces the declared one in the view (the
  Python step's ports turn green when they produce curves).
- Validation gains "this step does not accept a <kind>" before a run, where
  today the runner reports it during one.

### 4.5 Interactive runs

A Qt-free `RunSession` (new, `model/workfloweditor/session.py`) holds one
document's results between runs.

**Fingerprints.** Each step's fingerprint is a hash of its kind, plugin id
and version, effective parameters (the Python step's code is a parameter),
ROI restriction, and the fingerprints of the steps it reads (with ports). A
source's fingerprint adds the bound path, dataset, file size and modification
time (optionally a content hash, as `hash_sources` already computes).

**States.** A step is *done* when the cache holds results for its current
fingerprint, *outdated* when it holds results for an older one (shown dimmed),
*not run* when it holds none, *running*, *waiting* (queued behind a step), or
*failed* (with the error). Editing a parameter changes that step's fingerprint
and so, transitively, every step after it: "outdated" propagates without any
bookkeeping of its own.

**Run to here.** The session takes the selected step's ancestors, keeps the
ones whose cached results match, and hands the runner a reduced workflow with
those results **bound** (`run(bindings=...)` already skips every step that
only fed a bound one). *Run* is *Run to here* for every sink. *Re-run from
here* drops the cache for one step and its descendants.

**Runner extensions** (small, backwards compatible, tested headless):

1. A binding may be a `{port: result}` mapping, so a cached multi-port step
   (`split.C0`/`C1`, a Python step's ports) can be bound; today a binding fills
   only the `out` port.
2. An optional `on_step_done(step_id, {port: result}, seconds)` callback, so
   boxes update as each step finishes rather than at the end.
3. `write_saves: bool = True`; interactive runs pass `False` unless the user
   asks for files, and a Save box shows the path it *would* write
   (`render_save_path` as a dry run).

**Threading.** Runs go through `WorkflowController`'s existing worker and
cancel machinery (interrupt escalation from the Phase D work); results come
back to the GUI thread. Thumbnails are computed on the worker as small `uint8`
arrays (a middle plane or a sampled projection for large stacks, a sparkline
for curves, a label colouring for labels); only the `QPixmap` is made on the
GUI thread.

**Cache budget.** Results are kept up to a named budget under
`memory_limits` (a share of `processingWorkingSetBytes`), least recently used
first, never the selected step's; lazily backed results (an opened file) cost
nothing. An evicted step shows as *not run* and is recomputed when needed. The
status bar shows what the cache holds.

**Auto-run** (off by default): 600 ms after an edit, re-run the outdated steps
up to the selection, if their last measured time is under a threshold
(about 2 s); an edit during a run cancels it. Slow graphs show the *Run to
here* hint instead.

**Previews leave the editor only on request.** *Show in viewer* publishes a
temporary preview result (replaced on the next run, never saved); *Pin* adds it
to the results list as an ordinary result with its provenance.

**Binding the source.** A Source box accepts a dropped file, *Use the
recording that is open*, or a result from the list (binding the next step to
an in-memory result, as *Run on selected results* does today). The binding is
session state; the file keeps its own path, or none.

**Batch is unchanged.** *Run over files…* runs the document as today, with
saves; the interactive session is for one file at a time.

### 4.6 Inspector

Header in the step's colour with the plugin name, editable id and the
description; then:

- **Inputs**: what each input is connected to, with its kind; reorder for
  variadic steps (Merge channels).
- **Parameters**: the existing `ParamForm` (field types, ranges, units,
  defaults markers, per-field reset), unchanged.
- **Output**: a large preview of the selected port — an image with a plane
  slider and contrast, a curve in a pyqtgraph plot, the first rows of a table,
  labels in colour — with shape, axes, kind and timing; *Push to Graph panel*
  and *Push to table* for curves and tables.
- **Actions**: Run to here, Show in viewer, Pin.
- **Python step**: the shared `PythonCodeEditor`, the ports line, *Open in
  console*, *Load snippet…*; an error shows its line in the editor.

### 4.7 Start page

From the recording that is open (Source bound, View only added), from the
selected result's history (`workflow_from_provenance`), from a recipe (the
installed snippets, built into Source → View only → Python step → Save, with
CSV saves for curve ports), or a blank canvas; plus recent files and the
workflow folder.

### 4.8 Changes outside the editor

| Where | Change |
| --- | --- |
| `workflows/runner.py` | port-mapping bindings, `on_step_done`, `write_saves` (§4.5) |
| `processors/base.py` + built-ins | `output_kinds`, declarations, contract test (§4.4) |
| `model/workfloweditor/document.py` | `connect` / `disconnect` with stable topological reordering and cycle refusal, `insert_on(edge)`, positions and frames in `metadata.editor` kept in step with renames and removals, copy/paste as YAML fragments, undoable commands |
| `model/workfloweditor/session.py` (new) | `RunSession`: fingerprints, cache, states, run planning |
| `controller/WorkflowController.py` | runs a session's plan on the existing worker; previews to the viewer |
| `tools/make_python_recipe_workflows.py` | the recipe-to-workflow logic moves into the package so the start page can use it; the tool keeps calling it |

### 4.9 Module layout

```
imswitch/improcess/model/workfloweditor/session.py     RunSession, fingerprints, cache, states
imswitch/improcess/model/workfloweditor/graph.py       layered layout, topological helpers (Qt-free)
imswitch/improcess/model/workfloweditor/recipes.py     recipe snippets -> documents
imswitch/improcess/view/workfloweditor/canvas/         scene, view, node/port/edge items, quick-add, frames
imswitch/improcess/view/workfloweditor/listview.py     cards with the lane gutter
imswitch/improcess/view/workfloweditor/inspector.py    header, inputs, ParamForm, preview, actions
imswitch/improcess/view/workfloweditor/startpage.py
imswitch/improcess/view/workfloweditor/thumbnails.py   uint8 previews (worker side) -> QPixmap
imswitch/improcess/view/workfloweditor/editor.py       window: toolbar, mode switch, undo stack
```

The existing `steplist.py`, `stepforms.py` and `palette.py` are retired once
list mode and the inspector cover them; `paramform.py` and `validation.py`
stay.

## 5. Phases

Each phase leaves the suite green and ships something usable.

### Phase 0: foundations, Qt-free (no visible change)

- Document graph operations (connect/disconnect/splice, stable topological
  reordering, cycle refusal, positions in `metadata.editor`, undo commands).
- `output_kinds` on processors, declarations, contract test, kind-aware
  validation.
- Runner extensions 1–3 (§4.5).
- `RunSession` with fingerprints, cache, states and run planning.
- Exit: headless tests show that editing one parameter outdates exactly its
  descendants, that *Run to here* executes only outdated ancestors (counted),
  that a multi-port step binds from the cache, and that every shipped example
  round-trips unchanged with positions added.

### Phase 1: the canvas (edit)

- Scene, view, nodes, typed ports, cables, legend, zoom/fit, layered layout
  for files without positions, palette drag, quick-add picker, connect and
  disconnect gestures, splice, delete/duplicate, copy/paste, undo/redo.
- Inspector (header, inputs, `ParamForm`), Graph/List switch with today's
  table as the interim list.
- Exit: the shipped examples open as graphs, can be built from an empty
  canvas without the keyboard, saved, and run with today's *Run*.

### Phase 2: interactive runs

- Session wired to the worker: states, timing and thumbnails on boxes,
  *Run* / *Run to here* / *Stop* / *Re-run from here*, outdated propagation,
  errors on boxes (Python step: line in the editor), inspector previews,
  *Show in viewer* / *Pin*, source binding (drop, open recording, result).
- Exit: on the synthetic recording, change a parameter, see exactly the
  downstream steps outdated, *Run to here* refreshes them in the boxes, and
  the cache-hit count shows the upstream steps were not re-run; cancel works
  inside a slow step.

### Phase 3: polish and first-time use

- Start page with recipes and history, list mode as cards with the lane
  gutter, auto-run, frames/comments, the full shortcut set, tidy-up, tooltips
  with shape/axes/dtype on ports after a run, docs with these screenshots,
  changelog.
- Exit: a first-time user can go from the start page to a saved, run
  workflow with every step's output seen, without reading the manual
  (checked with two people who have not used the editor).

### Phase 4: needs a file-format decision (later)

- **Exposed controls** (FL's *Surface*): a few parameters promoted to the
  workflow ("radius", "threshold") and set at run time, for batch runs and
  for the CLI (`--set radius=3`).
- **Groups / sub-workflows** (Blender node groups): a selection collapsed
  into one box, reusable.
- **Bypass**: a step that passes its input through without being deleted.
- **Parameter sweeps**: one step run over several values, outputs side by
  side.
- **Batch in the editor**: a per-file status grid for *Run over files…*.

All five change what a file means and need a `schema: 2` decision; none is
needed for Phases 0–3.

### Effort

| Phase | New code incl. tests | Mostly |
| --- | --- | --- |
| 0 | ~1000–1300 lines | Qt-free model and runner, tests |
| 1 | ~1800–2400 lines | Qt (`QGraphicsScene`) |
| 2 | ~1200–1600 lines | Qt + worker wiring |
| 3 | ~1000–1400 lines | Qt, docs |
| 4 | per item | format design first |

## 6. Testing

- **Qt-free**: document graph operations (cycle refusal, reordering keeps the
  file valid, renames rewrite positions), fingerprints (property test: an edit
  outdates exactly the descendants), run planning (which steps run, which are
  bound), runner extensions, output-kind contract test.
- **Offscreen GUI (pytest-qt)**: build a graph through the scene API, connect
  by simulated drags, quick-add, undo/redo, inspector edits reaching the
  document, list/graph switch keeping the selection.
- **End to end**: the session on the synthetic recording (states, thumbnails
  present, cache-hit counts, cancel inside a step, a failing Python step with
  its line), the shipped examples as graphs.
- **Visual**: the mockup renderer becomes a screenshot smoke test under Xvfb
  (manual lane), which also produces the documentation images.
- **Scale**: a 100-step generated workflow renders and lays out in under
  100 ms; thumbnails for a 2 GB lazy stack are computed without reading it
  whole.

## 7. Alternatives considered

- **pyqtgraph's flowchart module.** Already a dependency, but it is its own
  execution engine with its own node and terminal model; adapting it to
  `WorkflowDocument` means fighting both, and its look is hard to bring to
  this standard. Rejected.
- **NodeGraphQt.** A capable Qt node editor, but a new dependency with its own
  graph model and serialisation, which would have to be kept in step with the
  document. The canvas needed here (boxes, typed ports, cables, thumbnails) is
  ordinary `QGraphicsScene` code that we fully control and can test offscreen:
  the spike's canvas module, list cards, thumbnails and layout included, is
  about 830 lines. Rejected for now.
- **A web canvas (litegraph, Rete) in a `QWebEngineView`.** Polished out of
  the box, but adds a browser engine to the dependency set, a JavaScript
  codebase, and a bridge for every interaction and preview. Rejected.
- **Graph only, drop the list.** A list stays useful for long linear chains,
  for keyboard-only use, and for reading a workflow top to bottom; as a second
  rendering of one document it costs little. Kept, as cards.
- **New step fields for the canvas (`position`, `enabled`, `label`).**
  Rejected for the same reason as in 1.0: an unknown key on a step fails to
  load in every existing ImSwitch. `metadata.editor` round-trips already.

## 8. Risks and open questions

**Risks**

- *Memory*: caching every step of a large reconstruction chain can hold
  several gigabytes. Mitigated by the budget, LRU eviction, lazy sources and
  showing what the cache holds; worst case is a re-run.
- *Stale results shown as fresh*: a fingerprint that misses an input (a file
  rewritten in place within the same second, a plugin changed without a
  version bump, a script reading an outside file) would reuse a wrong result.
  Mitigated by size+mtime (optional hash) on sources and plugin versions in
  the fingerprint; *Re-run from here* is the escape hatch; documented.
- *Thread safety*: results are produced on the worker and drawn on the GUI
  thread; the Phase D pattern (only plain data crosses, Qt objects stay on the
  GUI thread) applies.
- *Two views to maintain*: both are thin renderings over the document; the
  inspector, undo stack and selection are shared.

**Open questions (decisions for you)**

1. **Default mode.** Recommendation: Graph by default (that is where the
   previews are), List one click away, the choice remembered. You suggested
   the patcher as an alternative mode; it can equally be List by default.
2. **Do interactive previews appear in the results list?** Recommendation:
   no — they stay in the editor until *Show in viewer* (temporary) or *Pin*
   (permanent), so experimenting does not flood the list.
3. **Auto-run default.** Recommendation: off, with a toolbar toggle.
4. **Phase 4 (exposed controls, groups, bypass, sweeps).** Recommendation:
   decide after Phase 3 ships; they need a `schema: 2` design of their own.

## Appendix: how the mockups were made

A throwaway spike (about 1500 lines: an 830-line canvas module and a 710-line
mock window with the six screens; not in the repository): a `QGraphicsScene`
canvas whose boxes are built from `WorkflowDocument` + `build_catalog()`, laid
out by longest-path layering, filled with thumbnails from a real `run()` on
the synthetic recording (per-step times from the runner's `progress`
callback), inside a window that reuses the real `ParamForm`, the shared
`PythonCodeEditor` and pyqtgraph, rendered under Xvfb with ImSwitch's
stylesheet. Phase 1 can start from it.
