# ImProcess workflow schema 2: exposed controls and bypass

Date: 2026-10-28 (draft 1)

Status: **Draft for review.** Designs the first two items of Phase 4 of
[improcess-workflow-editor-2.md](improcess-workflow-editor-2.md), the ones
that change what a workflow file means: **exposed controls** (a few
parameters promoted to the workflow and set at run time) and **bypass** (a
step that passes its input through without being deleted). The other Phase 4
items (groups, parameter sweeps, the batch grid) are left for later; nothing
here closes a door on them. Section 12 lists the decisions this draft needs.

## 1. Summary

- **Exposed controls.** A workflow names a few of its parameters as
  *controls*: `radius` sets `smooth.radius` (and may set several parameters
  at once). The value stays in the step, as today; a control only says
  "this one may be set when the workflow runs", gives it a stable name, a
  label and help. You set a value with `--set radius=3` on the command line,
  in a column of a batch manifest, in a dialog before **Workflows → Run
  workflow…**, or in the editor's Controls panel. A file run without values
  runs exactly as written.
- **Bypass.** A processing step, or a Save step, carries `bypass: true`: a
  bypassed processing step does not run and its `out` port is its input,
  unchanged; a bypassed Save writes nothing. The step keeps its place, its
  parameters and its cables, so it can be switched back on.
- **Schema.** Both need `schema: 2`. A file is written as schema 2 only when
  it uses one of them, so every other file stays schema 1 and still opens in
  older ImSwitch. A schema-2 file is refused by older ImSwitch with the
  message it already gives ("newer than this ImSwitch understands"), which is
  right: ignoring a bypass would silently give a different result.
- **Provenance does not change.** A result records the parameters it was
  actually made with and the steps that actually ran. A control value set at
  run time is in the parameters; a bypassed step is not in the graph. So
  *Export workflow of current result…* and replay keep producing plain
  schema-1 workflows.

## 2. What exists (schema 1)

The parts of the current format these features touch (`imswitch/improcess/workflows/steps.py`):

- A workflow is `name`, `schema` (1), `description`, `metadata`, and an
  ordered list of steps: `source`, `reconstruct`, `consolidate`, `process`,
  `save`. Later steps refer to earlier ones as `id` or `id.port`.
- A step's `params` hold only what differs from the plugin's defaults; what a
  run uses is `{**default_params(), **params}` (the editor calls this
  `effective_params`).
- Every plugin declares its parameters through `param_spec()`: one
  `ParamField` per key with its `type` (`int`, `float`, `bool`, `text`,
  `code`, `select`, `multiselect`, `path`, `json`), bounds, options, unit,
  label and help, and `ParamField.problem_with(value)` says why a value does
  not fit.
- `validate(workflow, registry)` refuses before anything runs; `run()`
  validates first. `Workflow.from_dict` already refuses a schema newer than it
  understands.
- `metadata.editor` holds the editor's positions and frames; these never
  change what a file computes, which is why they needed no schema change.
- Batch: `run_over(workflow, bindings_list, …)` runs once per bindings dict
  and writes a summary CSV (`index`, `ok`, `source:<id>`, `files`, …); a
  manifest is a CSV with one column per source id.
- Replay turns a result's provenance graph into a schema-1 workflow, with
  `metadata.provenance_nodes` naming the node each step came from.

## 3. Exposed controls

### 3.1 What it is for

- **Batch over a parameter.** "Run this over the folder with a threshold of
  0.2, then again with 0.3", without editing the file: `--set threshold=0.2`.
- **Per-file values.** A manifest row says which file and which threshold.
- **A workflow handed to someone else.** The person who wrote it marks the
  two values worth touching; the person running it is asked for those two in
  a dialog, and not shown the other forty.
- **One value, several places.** The same radius in two filters stays the
  same radius: one control, two targets.
- **A short surface in the editor.** The important values of a long workflow
  in one panel, the way FL Studio's *Surface* exposes a patch's knobs.

### 3.2 Format

```yaml
name: split-process-merge
schema: 2
controls:
  - name: radius
    targets: [smooth.radius, sharp.radius]
    label: Filter radius
    help: Radius of both channel filters, in pixels
  - name: method
    targets: [smooth.method]
steps:
  - {step: source, id: raw}
  - {step: reconstruct, id: rec, reconstructor: view-only, inputs: [raw]}
  - {step: process, id: split, processor: stack-split, params: {axis: C}, inputs: [rec]}
  - {step: process, id: smooth, processor: filter, params: {method: gaussian, radius: 2.0}, inputs: [split.C0]}
  - {step: process, id: sharp, processor: filter, params: {method: median, radius: 2.0}, inputs: [split.C1]}
  - {step: process, id: merge, processor: channel-merge, inputs: [smooth, sharp]}
  - {step: save, id: out, input: merge, fmt: hdf5, path_template: "{out_dir}/{source_stem}_diamond{ext}"}
```

A control has:

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | What the value is set by: `--set radius=3`, a manifest column, the dialog. Letters, digits and `_`, not starting with a digit; unique; not the id of a step (manifest columns are source ids). |
| `targets` | yes | One or more `step.param`: a parameter of a `reconstruct` or `process` step. |
| `label` | no | Shown in dialogs and the editor. Default: the first target's field label. |
| `help` | no | Tooltip and `controls` listing. Default: the first target's field help. |

`controls` is a top-level list: its order is the order a dialog shows them
in.

### 3.3 What a value means

- **The value lives in the step.** A control has no default of its own: its
  value is the *effective* value of its targets, the step's parameter or, when
  the step leaves it at the plugin's default, that default. So the steps of a
  schema-2 file read exactly as before, the editor's step forms still show
  real values, and dropping `controls` from a file changes nothing it
  computes.
- **Several targets agree.** All targets of one control must have the same
  effective value; validation refuses a file where they differ. The editor
  sets them together, so it never writes such a file.
- **Setting a value is writing the targets, for one run.** `apply_controls`
  returns a copy of the workflow with each set control's value written into
  every target's `params`; the run then validates and runs that copy as an
  ordinary workflow. Nothing downstream of `apply_controls` knows controls
  exist: the runner, the plugins and the provenance all see plain parameters.
- **Values are checked, then the workflow is.** A value is checked against
  the targets' field (`problem_with`: type, bounds, options, nullability);
  then the workflow, with the value in place, is validated like any other.
  So a value that changes a step's ports (a stack split's `axis`) and breaks
  a reference fails before anything runs, naming the reference.
- **Text from the command line or a CSV** is read as a YAML scalar (`3`,
  `2.5`, `true`, `gaussian`, `null`), then checked against the field: an
  integer is accepted for a `float` field, `null` or an empty cell only for a
  `nullable` one; a `select` value must be one of the options (compared as
  text when the options are numbers).

### 3.4 What a control may target

| Rule | Why |
| --- | --- |
| The target step is a `reconstruct` or `process` step, and exists | Sources are already set at run time (bindings); a consolidation takes no parameters; Save paths are a separate question (12, Q4). |
| The parameter is one the plugin declares (`param_keys`) | A typo must fail before the run, as an unknown parameter does today. |
| Its field type is `int`, `float`, `bool`, `text`, `select` or `path` | One value, typed on a command line or in a CSV cell. `code` (a Python step's script), `json` and `multiselect` are refused. |
| Not a Python step's `ports` | The ports are the step's outputs: changing them at run time would change the graph. |
| All targets of a control have the same field type, and for `select` the same options | One value has to fit all of them. Numbers take the intersection of their bounds. |
| A parameter is the target of at most one control | Otherwise two values could be set for it in one run. |
| A target appears once in its control | Trivial, refused for clarity. |
| A control has at least one target | An empty control sets nothing. |
| The targets' effective values agree (3.3) | A control has one value. |

All of these are `Issue`s from `validate`, so the editor lists them on its
chip and the command line prints them, like every other problem.

### 3.5 Setting values

- **Command line.** `run WF.yaml --input *.h5 --out results/ --set radius=3
  --set method=median` sets them for every row. `validate WF.yaml --set …`
  validates with the values applied. A new `controls WF.yaml [--json]`
  lists each control: name, type, current value, bounds or options, unit,
  targets, label and help (what to hand a colleague, or an LLM).
- **Manifest.** A column named after a control sets it for that row; an empty
  cell leaves the value as written (or as `--set` gave it). A column that is
  neither a source id, `<id>.dataset`, nor a control is ignored as today
  (manifests carry notes columns), but now said once on the console and in
  the summary's warnings ("column `radus` is not a source or a control"), so
  a typo in a header is not silent.
- **Summary CSV.** One `control:<name>` column per control, with the value
  each row ran with, beside the `source:<id>` columns.
- **Python.** `run(workflow, registry=…, controls={"radius": 3})`,
  `run_over(…, controls={…})` for every row and per-row values from a manifest
  through `controls_from_manifest(path, workflow)`, and `apply_controls(workflow,
  values, registry)` for code that wants the plain copy.
- **ImProcess.** When a workflow file has controls, **Workflows → Run
  workflow…**, **Run workflow on selected results…** and **Run workflow over
  files…** show a dialog before running: one field per control, generated
  from its targets' `ParamField` with the control's label and help, filled
  with the file's values. The values hold for that run (for every file of a
  run over files); they are not written to the file. Cancel runs nothing.
- **Editor.** See 6.3: the editor edits the file, so there a control's value
  *is* the targets' value, edited in one place.

### 3.6 Provenance and replay

A result records the parameters it was made with, so a value set by a
control is recorded as the parameter it set. The run's summary CSV records
which controls had which values. Replay, and *Export workflow of current
result…*, produce a schema-1 workflow with the values in its steps and no
controls: the record says what ran, not which of its values someone meant to
be adjustable. That is a loss only for someone who wants to re-run *the
recipe* rather than *the run*, and they have the recipe's file.

### 3.7 Alternatives considered

- **References in the parameters** (`radius: {control: radius}` in the step,
  the value in the control). One place per value, and the use is visible at
  the step. Rejected: every reader of `params` (the runner, `param_spec`
  checks, the editor's forms, `minimize_params`, fingerprints, third-party
  scripts that read workflow files) would have to resolve references first;
  a dict-valued parameter could be mistaken for one; a file without its
  controls would not run at all; and the steps would no longer say what they
  compute.
- **A default on the control** as well as the value in the step. Two values
  that can disagree, and the question of which wins. The value in the step is
  enough.
- **Raw overrides without controls** (`--set smooth.radius=3`). Needs no
  schema change at all, and is a fine escape hatch, but it couples every
  script and manifest to step ids and plugin parameter names, has no label to
  show in a dialog, and cannot keep two parameters equal. Left as an open
  question (12, Q3); it could be added later under the same `--set` without
  conflict, since a control name has no dot.

## 4. Bypass

### 4.1 What it is for

Trying a workflow without one step (does the background subtraction help?),
keeping a step around for later, or switching a Save off, without deleting
the step and losing its parameters and cables. Blender calls it *mute*, Nuke
*disable*, Ableton *deactivate*.

### 4.2 Format

```yaml
  - {step: process, id: smooth, processor: filter, params: {method: gaussian, radius: 2.0},
     inputs: [split.C0], bypass: true}
  - {step: save, id: preview_tiff, input: merge, fmt: tiff, bypass: true}
```

`bypass: true` on a step; absent (never written as `false`) otherwise.

### 4.3 What it means

- **A bypassed processing step does not run.** It has one port, `out`, and
  that port *is* its input: the same result object, with its own provenance.
  A step after it reads the input as if wired to it directly.
- **A bypassed Save writes nothing.** The run reports it as bypassed (not as
  "would write": that is what *Write files* off means in the editor).
- **Kinds pass through.** For validation, a bypassed step produces what its
  input produces, so a step after it that does not accept that kind is
  refused before the run ("bypassing `segment` hands `measure` an image, and
  `measure` takes labels").
- **Its plugin need not be installed**, and its parameters are not checked:
  it never runs. Its references are checked. So a file can be opened and run
  with a step bypassed whose drop-in plugin is not on this computer.

### 4.4 What may be bypassed

| Rule | Why |
| --- | --- |
| Only `process` and `save` steps | A source has no input; a reconstruction turns a recording into a result, and there is nothing it could pass through; a consolidation merges several. |
| A bypassed `process` step has exactly one input | With several there is no single result to pass through (a merge). A single-input processor given several inputs (it runs once per input, ports `out`, `out1`, …) could pass each through; left for later (12, Q5). |
| Every reference to it is to its default port (`smooth` or `smooth.out`) | A split's `C0`, `C1` or a Python step's named outputs have no input to stand for. "Cannot bypass `split`: `smooth` reads its port `C0`". |
| The kinds downstream accept the input's kind (4.3) | So a bypass that cannot run is refused before it starts. |

### 4.5 In the runner and the report

The runner's loop sees `bypass` before anything else for the step: a
processing step sets `results["<id>.out"]` to its input and adds the id to a
new `RunReport.bypassed`; a Save adds its id and writes nothing.
`on_step_done` is not called for a bypassed step (it made nothing). The batch
summary gets a `bypassed` column. Provenance: the downstream results record
their real input, so a bypassed step appears in no graph, and replay of such a
result yields a workflow without it.

### 4.6 Alternatives considered

- **Delete it, add it back later.** What one does today; loses parameters and
  cables, and the undo history does not survive closing the file.
- **`enabled: false`.** Same thing under a double negative, and "disabled"
  does not say what happens to the cables; *bypass* does.
- **Keep it in `metadata.editor`.** Not possible: a bypass changes what the
  file computes, and the command line must honour it exactly as the editor
  does.

## 5. The schema version

- `SCHEMA_VERSION` becomes 2: what this ImSwitch reads and writes at most.
- **Written at the lowest version that holds the file.** `Workflow.to_dict`
  writes `schema: 2` when the workflow has controls or a bypassed step, and
  `schema: 1` otherwise. So a file never needs a newer ImSwitch than its
  content does, and editing an old file without using the new features does
  not make it unreadable for a colleague on an older version.
- **Read strictly.** A file marked `schema: 1` that has `controls` or a
  `bypass` key is refused ("`controls` needs `schema: 2`"), so a hand edit
  cannot produce a file that an older ImSwitch would run differently. Older
  ImSwitch already refuses `schema: 2` ("workflow schema 2 is newer than
  this ImSwitch understands"), before it reads a single step.
- **The editor says it once.** The first save that makes a file schema 2
  says in the status bar that the file now uses controls or bypass and opens
  only in this version of ImSwitch or later.
- **Replay and export** keep writing schema 1 (3.6).
- **Provenance** (the graph a result carries) is not versioned by this.

## 6. The editor

### 6.1 Document model (`WorkflowDocument`, Qt-free, undoable)

- `add_control(name, target)`, `add_target(name, target)`,
  `remove_target(name, target)` (a control left without targets goes with
  it), `rename_control(old, new)`, `set_control_label(name, label, help)`,
  `remove_control(name)`; `controls` and `control_of(step_id, key)`.
- `set_bypass(step_id, on)`.
- Setting a parameter that is a control's target sets **all** its targets,
  in one undo entry; clearing it clears all of them. So the targets never
  disagree in a file the editor wrote.
- Upkeep, the same places positions and `provenance_nodes` are kept up today:
  `rename_step` rewrites targets; `delete_step` removes the deleted steps'
  targets; `set_plugin` leaves targets whose parameter no longer exists and
  validation reports them (the user decides); copy, duplicate and paste do
  not make the copies targets.
- `minimize_params` keeps working: a target left at the plugin default is
  still a target (its value is the effective one, 3.3).

### 6.2 Runs (`RunSession`)

- **Controls need nothing**: the value is in the step's parameters, which the
  fingerprints already include, so setting a control outdates exactly its
  targets and what follows them.
- **Bypass:** a reference to a bypassed step is resolved to that step's own
  input, recursively, before fingerprinting, the same way the runner resolves
  it. So a step after a bypassed one has the fingerprint it would have if
  wired straight to the input: bypassing outdates what follows, and switching
  the bypass off again finds the old results still cached (if the cache kept
  them). A bypassed step has no state of its own beyond "bypassed".

### 6.3 What it looks like

- **Bypassed box:** dimmed body, the header greyed with a "bypassed" strip,
  and a dashed line through the box from its input port to its `out` port, so
  the path the data takes is drawn. In List mode, the card is greyed with the
  same word. The inspector says "Bypassed: `merge` reads `split.C0` through
  this step unchanged" with **Stop bypassing** to switch it back on.
- **Toggling:** **Bypass** in the box's context menu and the Edit menu,
  shortcut **B** on the canvas (beside **F** fit and **H** tidy up; Blender's
  is M). A step that cannot be bypassed (4.4) has the action disabled, with
  the reason as its tooltip.
- **Exposing a parameter:** each row of a step's parameter form gets
  **Expose as control…** in its context menu: it asks for the name (default:
  the parameter's key) and offers, when a control of the same field type
  exists, to add this parameter to it instead. An exposed row shows a small
  "⇄ radius" chip; editing it edits the control, so all its targets.
- **Controls panel:** with nothing selected, the inspector shows the
  workflow itself: its name and description, and its controls, one row each
  with the value's field, the label, and the targets (click one to select its
  step); **Rename…** and **Stop exposing**. This is the short surface of 3.1:
  changing a value there marks its targets outdated and, with auto-run on,
  re-runs them.
- **Boxes of targets:** the parameter line of a target box marks the exposed
  parameter with the same chip, so the canvas shows what a run may change.

## 7. ImProcess runs

The dialog of 3.5, in `WorkflowController`: after a file is loaded and, if it
has Python steps, confirmed, a workflow with controls opens a modal form
(`ParamForm` over one field per control, built from the first target's
`ParamField`, with the control's label, help and intersected bounds) filled
with the file's values. **Run** applies them with `apply_controls`; the
status line and the run's report name the values. Runs from the editor do not
ask: the editor's values are the file's.

## 8. Code touched

| Where | What |
| --- | --- |
| `workflows/steps.py` | `Control` dataclass; `Workflow.controls`; `bypass` on `Process` and `Save`; (de)serialisation; lowest-version writing and strict reading; control and bypass validation; kinds through bypassed steps in `declared_kinds` |
| `workflows/controls.py` (new) | `apply_controls`, value parsing and checking against `ParamField`s, `describe_controls` for the CLI and dialogs |
| `workflows/runner.py` | `controls=` argument (apply, then validate); bypass in the step loop; `RunReport.bypassed` |
| `workflows/batch.py` | `controls=`; `controls_from_manifest`; a warning for manifest columns that are neither sources nor controls; `control:<name>` and `bypassed` summary columns |
| `workflows/__main__.py` | `--set` on `run` and `validate`; the `controls` command |
| `model/workfloweditor/document.py`, `session.py`, `nodes.py` | 6.1 and 6.2; the chip and bypass state in `NodeInfo` |
| `view/workfloweditor/` | canvas and list drawing, inspector rows and the Controls panel, the Bypass action |
| `controller/WorkflowController.py` | the control dialog before file runs |
| Docs | `improcess-workflows.rst` (format, CLI, editor), changelog, this plan's status |

## 9. Phases

| Phase | Content | Exit | New code incl. tests |
| --- | --- | --- | --- |
| S1 | Format, validation, `apply_controls`, runner bypass, batch and CLI | Every rule of 3.4 and 4.4 refused with its message; the equivalence tests of 10 pass; every shipped schema-1 example loads, validates and serialises with no schema-2 key | ~900–1300 lines |
| S2 | Editor: document operations, `RunSession` bypass, drawing, inspector, Controls panel | Expose, set, bypass and their undo through the window offscreen; fingerprint tests of 10 | ~900–1200 lines |
| S3 | ImProcess dialog, docs, screenshots, changelog | Dialog values reach the run; cancel runs nothing; real-window check | ~300–500 lines |

S1 is useful on its own (batch and command line); S2 and S3 can follow in
either order.

## 10. Tests

- **Format:** round trip of controls and bypass; a workflow without them is
  written as schema 1, and every shipped example serialises to the same
  `to_dict()` as before (no schema-2 key appears); a schema-1 file with
  `controls` or `bypass` is refused; schema 3 is refused.
- **Validation:** one test per rule of 3.4 and 4.4, each asserting its
  message.
- **Values:** YAML-scalar parsing and checking per field type, bounds,
  options, nullability, int for float, the intersection of bounds over
  targets, unknown control names listed.
- **Equivalence (the core of the design):** a run with `radius=3` equals the
  run of the same workflow with `radius: 3.0` written into both targets
  (pixels and provenance); a run with a bypassed step equals the run of the
  workflow with the step removed and its consumer wired to its input (pixels,
  and provenance minus the node); a file with `controls` removed computes the
  same as with them and no values set.
- **Runner and batch:** a bypassed Save writes nothing and is listed;
  manifest control columns per row, an unknown column warned about; summary
  columns; `--set` and the `controls` command through `main([...])`.
- **Replay:** a result made with a control value and a bypass replays to a
  schema-1 workflow with the value in its step and without the bypassed step.
- **Editor:** document operations and their undo; rename, delete and paste
  keep targets right; setting a target sets all; fingerprints: setting a
  control outdates exactly the targets and descendants; bypass outdates the
  descendants and un-bypass finds them cached; the Bypass action disabled with
  its reason; offscreen GUI tests for the chip, the panel and the dashed
  pass-through.
- **ImProcess:** the dialog's values reach `run`; cancel runs nothing; no
  dialog for a file without controls.

## 11. Risks

| Risk | Mitigation |
| --- | --- |
| A run-time value changes a step's ports and breaks a reference | Values are applied, then the workflow is validated (3.3); the run refuses before anything runs, naming the reference. |
| A colleague on an older ImSwitch cannot open a file | Written as schema 2 only when needed (5), with a status-bar note when a file first needs it; the older version's refusal names the schema. |
| Results that do not say which values were "the knobs" | Provenance records every parameter as run; the summary CSV records control values; the file itself is the recipe. |
| A bypass hides a broken step | A bypassed step is drawn as bypassed everywhere it appears, listed in every run report and batch summary, and its missing plugin is a note, not silence. |
| Two places to edit one value (the step and the panel) | They are the same value: the panel edits the targets, the step form edits the control (6.1). |
| Hand-edited targets that disagree | Refused by validation with both values named. |

## 12. Decisions needed

1. **Where the value lives:** in the step, with controls naming it (this
   draft, 3.3), or references in the parameters (3.7)? Recommended: in the
   step.
2. **Bypassing Save steps:** yes in this draft. It is cheap, and switching
   one output off without losing its path template is a common want.
3. **Raw overrides** (`--set smooth.radius=3`) without a control: not in this
   draft, to keep one documented mechanism; easy to add later.
4. **Controls on Save paths and formats** (`path_template`, `fmt`): not in
   this draft; run-time output naming is a separate question from parameters.
5. **Bypass of a single-input processor given several inputs** (pass each
   through to `out`, `out1`, …): later, if wanted.
6. **Narrower bounds or options on a control** than the plugin's (a "safe"
   range for the person running it): later; the format leaves room
   (`minimum`, `maximum`, `options` keys on a control).
