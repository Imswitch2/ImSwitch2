# ImProcess workflow schema 2: exposed controls and bypass

Date: 2026-11-02 (draft 2, after review 1)

Status: **Draft 2 for review.** Designs the first two items of Phase 4 of
[improcess-workflow-editor-2.md](improcess-workflow-editor-2.md), the ones
that change what a workflow file means: **exposed controls** (a few
parameters promoted to the workflow and set at run time) and **bypass** (a
step that passes its input through without being deleted). The other Phase 4
items (groups, parameter sweeps, the batch grid) are left for later; nothing
here closes a door on them.

Draft 1 (2026-10-28) was reviewed against the runner, editor, validation,
batch and GUI code with targeted probes; review 1 kept the architecture and
raised seven findings and three smaller decisions. Draft 2 answers all of
them (section 15 maps each to its answer). The largest change is a single
**preparation boundary** (section 3): every run turns the file into a plain
workflow first, so the runner, the editor's partial runs, the batch rows and
the GUI publisher never see a control definition or a bypassed step. The six
decisions of draft 1 are settled as recommended (section 14).

## 1. Summary

- **Exposed controls.** A workflow names a few of its parameters as
  *controls*: `radius` sets `smooth.radius` and `sharp.radius`. The value
  stays in the steps, written out explicitly; a control says "this one may
  be set when the workflow runs", gives it a stable name, a label and help.
  A value is set with `--set radius=3`, in a column of a batch manifest, in a
  dialog before **Workflows → Run workflow…**, or in the editor's Controls
  panel. A file run without values runs exactly as written.
- **Bypass.** A processing step or a Save step carries `bypass: true`. A
  bypassed processing step is taken out of the run and whatever read it reads
  its input instead; a bypassed Save writes nothing. The step keeps its place,
  parameters and cables in the file, so it can be switched back on.
- **One preparation boundary.** The file (the *recipe*) is never run as it
  is. `prepare_run` checks it, takes bypassed steps out, cuts it to the steps
  a partial run needs, works out which control targets are active, applies
  the values given for this run, drops the control definitions and validates
  the result. What it hands the runner is a plain workflow with schema-1
  meaning.
- **Schema.** Both features need `schema: 2`. A file is written as schema 2
  only when it uses one of them, so every other file still opens in older
  ImSwitch, which refuses schema 2 outright rather than run it differently.
- **Provenance does not change.** A result records the parameters it was
  made with and the steps that ran. A control value is in the parameters; a
  bypassed step is in no graph. *Export workflow of current result…* and
  replay keep producing schema-1 workflows.

## 2. What exists (schema 1)

The parts of the current code these features touch:

- **Format** (`workflows/steps.py`): `name`, `schema` (1), `description`,
  `metadata`, and an ordered list of `source`, `reconstruct`, `consolidate`,
  `process` and `save` steps; later steps refer to earlier ones as `id` or
  `id.port`. A step's `params` hold what differs from the plugin's defaults;
  a run uses `{**default_params(), **params}` (the editor's
  `effective_params`). `Workflow.from_dict` refuses a schema newer than it
  knows before reading any step; an omitted schema reads as 1.
- **Parameter fields**: every plugin declares `param_spec()`, one
  `ParamField` per key (`int`, `float`, `bool`, `text`, `code`, `select`,
  `multiselect`, `path`, `json`; bounds, options, unit, decimals, step,
  nullability, label, help). `ParamField.problem_with(value)` checks a value;
  it does not reject NaN inside bounds and does no textual matching.
- **Validation and the runner** (`workflows/runner.py`): `run()` validates,
  then walks the steps. Bindings can hand in results for steps, and
  `steps_replaced_by_bindings` skips the bound steps and everything that only
  fed them, before any step-type handling.
- **Declared ports**: a processor's `output_spec(params, input_kinds)` gives
  fixed ports or a pattern. The stack and channel splits declare
  `[A-Za-z]+\d+` whatever their `axis`, so a consumer of `split.C0` validates
  even with `axis: Z`. Whether a data-dependent port exists is known only
  after the step ran.
- **Editor runs** (`model/workfloweditor/session.py`): `RunSession.plan()`
  builds the workflow it runs from the requested steps and their ancestors,
  keeping the recipe's top-level fields, and binds cached results by step id.
- **GUI publishing** (`controller/WorkflowController._publish`): every key
  of `report.results` that is not in `report.bound` is published to the
  reconstruction list; `on_step_done` plays no part.
- **Batch** (`workflows/batch.py`): `run_over(workflow, bindings_list, …)`
  runs once per bindings dict; a manifest has one column per source id;
  other columns are ignored silently; failures are rows, not exceptions; a
  summary CSV lists `source:<id>`, files and warnings per row.
- **Replay** turns a result's provenance graph into a schema-1 workflow.

## 3. Preparing a run

### 3.1 Recipe and execution workflow

The **recipe** is the file: schema 1 or 2, with controls and bypass flags.
The **execution workflow** is what runs: no `controls`, no `bypass`, every
step present will run (or be replaced by a binding, as today), schema-1
meaning. Every way of running — `run()`, `run_over()`, the command line,
the ImProcess run commands and the editor's `RunSession` — goes through one
function:

```python
prepared = prepare_run(recipe, registry=registry,
                       values={"radius": 3},        # this run's control values, or None
                       bindings=bindings,           # as run() takes them today
                       subset=["smooth"])           # the editor's Run to here, or None
prepared.workflow    # the execution workflow
prepared.bypassed    # step ids taken out
prepared.values      # every control's canonical value for this run (given or from the file)
prepared.inactive    # control -> [(target, why)] for controls with no active target
```

For a schema-1 recipe with no values, `prepare_run` returns a workflow equal
to the recipe: schema-1 files run exactly as today.

### 3.2 The order, and what each stage guarantees

1. **Check the recipe.** Schema rules (section 7), control entries, target
   syntax and existence, bypass eligibility (5.4). Nothing here needs the
   plugins except target field checks, which skip a target whose plugin is
   missing on a bypassed step (4.5).
2. **Take bypassed steps out.** Each reference to a bypassed processing step
   is rewritten to that step's own input, following a chain of bypassed steps
   to the first one that runs; bypassed processing and Save steps are removed.
   A binding that names a bypassed step is refused: "`smooth` is bypassed;
   bind `split`, which it passes through". After this stage no bypassed step
   exists anywhere.
3. **Cut to the subset** (editor partial runs): the requested steps and their
   ancestors in the rewritten graph. Requesting a bypassed step requests the
   step it passes through.
4. **Work out replaced steps**: `steps_replaced_by_bindings` on the rewritten,
   cut graph, unchanged. The replaced steps stay in the execution workflow
   (the runner skips them, as today).
5. **Work out activity.** A control target is *active* when its step is in
   the execution workflow and is not replaced. Otherwise it is inactive, for
   one of four reasons: *bypassed*, *replaced by a bound result*, *only feeds a
   replaced step*, *outside this run* (stage 3). A control with no active
   target is *inactive*.
6. **Apply values.** File values, then values for the whole run, then the
   row's values (4.7). Each named control must exist and be active (an
   inactive one is an error naming every target's reason); each value is
   converted and checked by the value contract (4.6) against the control's
   combined field (4.4) and every active target's `ParamField`; the canonical
   value is written into every active target's `params`.
7. **Drop the controls.** The execution workflow has no `controls` key. This
   is what keeps a partial run valid when a shared control's other target
   was cut away in stage 3.
8. **Validate the execution workflow** with the registry, as `run()` does
   today, with the precise port declarations of section 6. A message about a
   rewritten reference says so: "merge: … `split.C0` (read through bypassed
   `smooth`) …".

Any problem raises one `WorkflowError` listing everything, before a step
runs. `check_recipe(recipe, registry)` returns stages 1, 5 (for a plain run)
and 8 as `Issue`s for the editor's chip, without raising.

### 3.3 What this settles

- **Partial editor runs** never carry a control definition, so a control
  whose other target is outside the run, or whose targets are all outside it,
  cannot make the run invalid (stages 3, 7).
- **Bypass and bindings** have one order: bypass first (stage 2), replaced
  steps after (stage 4), on the rewritten graph. A cached downstream result
  bound by the editor skips its ancestors exactly as today; a bypassed
  ancestor is not among them because it no longer exists. Consecutive
  bypassed steps are one rewrite.
- **Publishing** sees only results the run made or was handed: no step key
  is an alias of another step's result, so `_publish` stays as it is and
  publishes each result once.
- **A bypassed step whose plugin is missing** runs fine: it is removed before
  the registry is consulted (stage 8).

## 4. Exposed controls

### 4.1 What it is for

- **Batch over a parameter**: the folder with a threshold of 0.2, then 0.3,
  without editing the file.
- **Per-file values**: a manifest row says which file and which threshold.
- **A workflow handed to someone else**: its author marks the two values
  worth touching; the person running it is asked for those two, not shown the
  other forty.
- **One value, several places**: the same radius in two filters stays the
  same radius.
- **A short surface in the editor**: the important values of a long workflow
  in one panel, as FL Studio's *Surface* exposes a patch's knobs.

### 4.2 Format

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

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | What the value is set by (`--set radius=3`, a manifest column, the dialog). Letters, digits and `_`, not starting with a digit; unique; not a step id. |
| `targets` | yes | A non-empty list of `step.param`: parameters of `reconstruct` or `process` steps. |
| `label` | no | Shown in dialogs and the editor; default from the combined field (4.4). |
| `help` | no | Tooltip and the `controls` listing; default from the combined field. |

`controls` is a top-level list; its order is the order dialogs show. Any
other key in an entry (`target`, `default`) is refused, so a typo is not
ignored.

### 4.3 The value lives in the steps, owned by the control

- **No default of its own.** A control's value is its active targets' value.
  Dropping `controls` from a file changes nothing it computes, and the step
  forms show real values.
- **Written explicitly.** A target's value is always in its step's `params`,
  even when it equals the plugin default: `minimize_params` skips targets. So
  a later change of a plugin default cannot make two targets disagree.
- **Active targets agree.** Validation refuses a file whose active targets
  differ, naming each value. Inactive targets may differ (a bypassed step
  keeps what it had); they adopt the control's value when they become active
  in the editor (8.1).
- **Owned by the control.** In the editor a target changes only through its
  control: editing it in the step form sets the control (all its targets);
  resetting it resets the control; bulk edits leave it alone unless they set
  it (8.1).

### 4.4 The combined field

A control's field is computed from its **active** targets' `ParamField`s:

| Property | Rule |
| --- | --- |
| `type` | The same for all, else refused. |
| `options` (`select`) | The same list for all, else refused. |
| `nullable` | Only if every active target is nullable. |
| `min`, `max` | The intersection: the largest minimum, the smallest maximum; refused when empty. |
| `suffix` (unit) | The same for all, else refused ("`radius` is in px in `smooth` and µm in `deconv`"): two units are two meanings. |
| `decimals`, `step` | The most decimals and the smallest step of the targets, so no target's precision is lost. |
| `label`, `help` | The control's own, else the first active target's. |
| Reset value | The default the active targets share; none when they differ (then Reset is disabled, 8.1). |

A control with no active target has no combined field.

### 4.5 What a control may target

| Rule | Why |
| --- | --- |
| The target step exists and is a `reconstruct` or `process` step | Sources are set at run time by bindings; a consolidation has no parameters; Save paths are deferred (14). |
| The parameter is one the plugin declares (`param_keys`) | A typo must fail, as an unknown parameter does today. Checked for every target whose plugin is installed, active or not. |
| Its field type is `int`, `float`, `bool`, `text`, `select` or `path` | One value, typed on a command line or in a cell. `code`, `json` and `multiselect` are refused. |
| Not a Python step's `ports` | The ports are the step's outputs: changing them at run time changes the graph. |
| Active targets combine (4.4) and agree (4.3) | A control has one field and one value. |
| A parameter is the target of at most one control; once in it | Otherwise one run could set two values for it. |

**Inactive targets.** A target on a bypassed step is checked only as far as
its plugin allows: the step and syntax always, the key and field type when
the plugin is installed; its value is not compared, and it takes no part in
the combined field. A control whose targets are all inactive in the file
(all bypassed) is reported once as a note, not an error: the file is fine,
the control has no effect until a step is switched back on.

**Values for an inactive control are an error**, for this run: `--set
radius=3` when `radius` only targets bypassed steps, or only the
reconstruction a *Run workflow on selected results…* replaces, fails before
anything runs, saying why each target is inactive. A value that would change
nothing is almost always a mistake, and a batch that quietly ignores it gives
results the person did not ask for. The ImProcess dialog shows such controls
disabled with the reason instead of offering them (4.7).

### 4.6 The value contract

The same rules for `--set name=TEXT` (everything after the first `=`) and a
manifest cell. Dialogs produce typed values from their fields, then make the
same checks.

**Three different things:**

| State | Written as | Means |
| --- | --- | --- |
| Missing | No `--set` for it; no manifest column for it; an empty cell | No value for this run: the next source of values applies (4.7). |
| Null | the unquoted token `null` | `None`, allowed only when the combined field is nullable. |
| Empty text | the token `''` | The empty string, for `text`, `path` and `select` fields only. |

`--set name=` with nothing after `=` is an error ("no value for `name`;
write '' for empty text or null for none").

**Conversion by the field, not by YAML.** General YAML parsing would turn
`001` into `1`, `on` into `true` and `2026-11-02` into a date, so text is
converted by the target field's type:

| Field | Accepts | Becomes |
| --- | --- | --- |
| `int` | `[+-]?[0-9]+` | `int` (`007` → 7) |
| `float` | a decimal or scientific number (`2`, `2.5`, `-1e-3`) | `float` (`3` → 3.0); `nan`, `inf` and other non-finite values refused |
| `bool` | `true` or `false`, any case | `bool`; `on`, `yes`, `1` refused |
| `select` | text equal to `str(option)` for exactly one option | that option, typed (`1` matches the option 1 and becomes the integer) |
| `text`, `path` | anything | the text as written (`001`, `on`, `2026-11-02` stay strings) |

A value in single quotes (`'…'`, with `''` for a quote inside) is literal
text, for `text`, `path` and `select` fields: `'null'` is the text *null*.
Quotes on a number or boolean are refused.

**Canonical values.** What is written into `params`, recorded in provenance,
listed in the summary and hashed into the editor's fingerprints is the
converted value: `3` and `3.0` given to a `float` field are the same run.

**Checks after conversion**: the combined field (bounds, options,
nullability), finiteness, then `ParamField.problem_with` for every active
target. `problem_with` itself is not changed here (it would change what
schema-1 files accept); making it reject NaN is a separate follow-up.

### 4.7 Setting values

**Precedence**: the file's value → values for the whole run (`--set`, the
ImProcess dialog, `controls=` in Python) → the row's values (a manifest
cell). An empty cell is missing, so the run-wide value applies.

- **Command line.** `run WF.yaml … --set radius=3 --set method=median`;
  `validate WF.yaml --set …` validates with the values applied; a new
  `controls WF.yaml [--json]` lists each control: name, combined type, value,
  bounds or options, unit, targets, whether it is active, label and help.
- **Manifest rows.** `rows_from_manifest(path, recipe)` returns
  `ManifestRow(line, bindings, values)`, the bindings and the raw text of the
  control columns of one line, kept together. `run_over(recipe, rows, …,
  controls=None)` takes these or plain bindings dicts as today.
  - Checked once, before any row: run-wide values (unknown name, inactive
    control, bad value); a control column whose control is inactive for this
    batch while any of its cells is filled. Either refuses the batch.
  - Checked per row: the row's cells. A bad cell fails that row without
    running it (`ok=False`, the error naming line and column); the other rows
    run.
  - A column that is neither a source id, `<id>.dataset` nor a control is
    still ignored (manifests carry notes columns) but now said once on the
    console and in the summary's warnings, so a typo in a header is not
    silent.
- **Summary CSV.** One `control:<name>` column per control with the
  canonical value each row ran with (the file's when not given; empty for an
  inactive control), and a `bypassed` column.
- **Python.** `prepare_run` (3.1), `run(recipe, registry=…, controls={…})`,
  `run_over(recipe, rows, controls={…})`.
- **ImProcess.** When a file has controls, **Run workflow…**, **Run workflow
  on selected results…** and **Run workflow over files…** show a dialog
  before running, one field per control from its combined field, filled with
  the file's values. Activity is worked out for that run (no bindings; the
  reconstruction replaced; the source bound): a control with no active target
  is shown disabled, with the reason ("`rec` is replaced by the selected
  result", "`smooth` is bypassed"), and sends no value. The values hold for
  that run (every file of a run over files) and are not written to the file.
  Cancel runs nothing. A file without controls opens no dialog.
- **Editor.** No run-time values: there a control's value *is* its targets'
  value, edited in one place (8.3).

### 4.8 Provenance and replay

A result records the parameters it was made with, so a control's value is
recorded as the parameter it set; the summary records which controls had
which values. Replay and *Export workflow of current result…* produce a
schema-1 workflow with the values in its steps and no controls: the record
says what ran, not which values someone meant to be adjustable.

### 4.9 Alternatives considered

- **References in the parameters** (`radius: {control: radius}`): every
  reader of `params` would have to resolve them; a dict-valued parameter
  could be mistaken for one; a file without its controls would not run; the
  steps would no longer say what they compute.
- **A default on the control** as well as the value in the step: two values
  that can disagree.
- **Raw overrides** (`--set smooth.radius=3`) without a control: deferred
  (14); can be added later under the same `--set`, since a control name has
  no dot.

## 5. Bypass

### 5.1 What it is for

Trying a workflow without one step, keeping a step for later, switching a
Save off, without deleting the step and losing its parameters and cables.
Blender calls it *mute*, Nuke *disable*.

### 5.2 Format

```yaml
  - {step: process, id: smooth, processor: filter, params: {method: gaussian, radius: 2.0},
     inputs: [split.C0], bypass: true}
  - {step: save, id: preview_tiff, input: merge, fmt: tiff, bypass: true}
```

`bypass` is a boolean (`true`, `false`); written only as `true`. Any other
value — the string `"false"`, `0`, `1`, `null` — is refused naming the step,
so no value ever becomes a bypass by truthiness.

### 5.3 What it means

Resolved entirely in preparation (3.2, stage 2):

- **A bypassed processing step is taken out**, and every reference to it is
  rewritten to its input (following chains). The step after it reads that
  input as if wired to it, so kinds are checked on the real producer: "`measure`
  takes labels; `segment` is bypassed and `split.C0` is an image".
- **A bypassed Save is taken out**: it writes nothing and the report lists it
  as bypassed (not as "would write", which is what *Write files* off means
  in the editor).
- **A Save that reads a bypassed step** saves its input, and its
  `{input_step}` placeholder names the step that made the data (`split`, not
  `smooth`); `{step}` is the Save's own id, unchanged. So the file name says
  what the file holds.
- **Its plugin need not be installed** and its parameters are not checked: it
  never runs (3.3).

### 5.4 What may be bypassed

| Rule | Why |
| --- | --- |
| Only `process` and `save` steps | A source has no input; a reconstruction turns a recording into a result; a consolidation merges several. |
| A bypassed `process` step has exactly one input | With several there is no single result to stand in. A single-input processor given several inputs is deferred (14). |
| Every reference to it is to its default port (`smooth` or `smooth.out`) | A split's `C0` or a Python step's named outputs have nothing to stand for: "cannot bypass `split`: `smooth` reads its port `C0`". |
| No binding names it (3.2) | A result handed in for a step that does not run is a contradiction. |

### 5.5 Reports

`RunReport.bypassed` lists the steps taken out; `report.results` holds only
what ran or was bound (no aliases); `on_step_done` is never called for a
bypassed step. The batch summary has a `bypassed` column. Provenance: the
results downstream record their real input, so a bypassed step is in no
graph and replay of such a result gives a workflow without it.

### 5.6 Alternatives considered

- **Delete it, add it back later**: loses parameters and cables, and the
  undo history ends with the file.
- **Pass-through at run time** (draft 1: the step's `out` port set to its
  input during the run): makes every result key a possible alias, so the GUI
  publisher republishes results; needs an order against bound and skipped
  steps; and still asks the registry about a plugin that will not run.
  Removing the step in preparation has none of these.
- **`enabled: false`**: a double negative, and "disabled" does not say what
  happens to the cables.

## 6. Ports a value can change

Draft 1 promised that a value changing a step's ports (a split's `axis`) is
refused before the run. That holds only as far as the plugins declare their
ports, and today the splits declare any letter-and-number port. So:

- **The guarantee, narrowed.** References that a plugin's declaration
  decides are checked before anything runs, with the run's values applied
  (3.2, stage 8). Whether a data-dependent port exists (`C3` when the data
  has two channels) is checked when its step has run, at the reference that
  needs it, as today ("`split.C3`: no such result (have `split.C0`,
  `split.C1`)").
- **Precise declarations in S1.** Both splits name their ports after the
  data's label of the axis they split (`_axis_split.split_port_keys`), and an
  explicit `axis` is matched to that label exactly or ignoring case
  (`_explicit_axis_index`). So for every option but `Auto` the declaration
  can be exact: `(?i:<axis>)\d+`, the label escaped. `Auto` lets the data pick
  the axis and keeps the general pattern. With `axis: Z`, from the file or
  from a control, a consumer of `split.C0` is refused before the run.
- **An audit in S1** of every built-in processor whose `output_spec`
  ignores a parameter that shapes its ports. Known today: the two splits
  (`axis`); the Python step's `ports`, which controls may not target anyway.
  A drop-in plugin's declaration is its own; with a loose one, its consumers
  fail at the reference instead.

## 7. The schema version and how files are read

- `SCHEMA_VERSION` becomes 2: what this ImSwitch reads and writes at most.
- **Written at the lowest version that holds the file**: `schema: 2` when the
  workflow has a control or a bypassed step, `schema: 1` otherwise. Editing an
  old file without the new features does not make it unreadable for a
  colleague on an older version.
- **Reading the version.** Omitted: 1, explicitly (today's reader falls back
  to `SCHEMA_VERSION`, which would make an unversioned file schema 2).
  Otherwise an integer of at least 1 (a string of digits is accepted, as
  today); a boolean is refused (Python would read `true` as 1); a version
  above 2 is refused, as today.
- **New keys need schema 2, even when empty or false.** A schema-1 file with a
  `controls` key (even `[]`) or a `bypass` key (even `false`) is refused, so
  a hand edit cannot make a file an older ImSwitch would run differently.
  `controls: []` is accepted in schema 2 and dropped when writing; so is
  `bypass: false`.
- **Older ImSwitch** refuses `schema: 2` with "workflow schema 2 is newer than
  this ImSwitch understands", before reading a step: it never runs a schema-2
  file without its bypasses.
- **The editor says it once**: the first save that makes a file schema 2 says
  in the status bar that it now opens only in this version of ImSwitch or
  later.
- **Replay and export** keep writing schema 1 (4.8). Provenance is not
  versioned by this.

## 8. The editor

### 8.1 Document model (`WorkflowDocument`, Qt-free, undoable)

New operations: `add_control(name, target)`, `add_target(name, target)`,
`remove_target(name, target)` (a control left without targets goes with it),
`rename_control(old, new)`, `set_control_label(name, label, help)`,
`remove_control(name)`, `set_control_value(name, value)`,
`reset_control(name)`, `set_bypass(step_id, on)`; `controls`,
`control_of(step_id, key)`.

Their rules, each one undo entry:

| Operation | Rule |
| --- | --- |
| `add_control(name, target)` | The value is the target's current effective value, written into its `params`. |
| `add_target(name, target)` | The target must pass 4.5 and combine with the control's active targets (4.4). It **adopts the control's value**, written into its `params`; refused, with the reason, when that value does not fit its field (bounds, options, nullability). |
| `set_control_value(name, value)` | Checked against the combined field; written into **every** target, active or not. |
| `set_param` on a target | Is `set_control_value` for its control. |
| `reset_control(name)`, `clear_param` on a target, the row's Reset | Writes the control's reset value (4.4) into every target, explicitly. **Disabled when the active targets' defaults differ**, with the reason ("`smooth.radius` defaults to 2.0, `deconv.radius` to 4.0: set a value instead"). |
| `set_params` (bulk) and *Reset all to defaults* on a step | Non-target keys change as asked; a target keeps its control's value unless the call sets it, which is then `set_control_value`. Two targets of one control given different values in one call: refused. The step form says "exposed parameters keep their control's value; reset them in the Controls panel". |
| `set_bypass(step, False)` | Every target on the step adopts its control's value in the same entry, so the newly active targets agree. |
| `set_plugin` | Targets whose key or field no longer fits are left for validation to report; the user decides. |
| `rename_step`, `delete_step`, copy, duplicate, paste | Targets renamed; targets of deleted steps removed; copies are not targets. |

`minimize_params` skips targets (4.3).

### 8.2 Runs (`RunSession`)

- **Through `prepare_run`.** `plan()` calls `prepare_run` with the requested
  steps as `subset` and its cached and result bindings; it runs the execution
  workflow and maps the results back to the recipe's steps. Controls need no
  run-time values (the values are in the steps), and stage 7 drops their
  definitions, so a shared control whose other target is cut away, or a
  control entirely outside the run, cannot make a partial run invalid.
- **Fingerprints** resolve a reference to a bypassed step to what it passes
  through, with the same function stage 2 uses. A step after a bypassed one
  has the fingerprint it would have wired straight to the input: bypassing
  outdates what follows, and switching the bypass off again finds the old
  results if the cache kept them. Setting a control outdates exactly its
  targets and what follows them, because its value is in their parameters.
- **A bypassed step's box** shows "bypassed" and, as its output, the output
  of the step it passes through, dimmed. *Run to here* on it runs that step
  (3.2, stage 3).

### 8.3 What it looks like

- **Bypassed box**: dimmed body, a greyed header with a "bypassed" strip, and
  a dashed line from its input port to its `out` port, so the path the data
  takes is drawn. List mode greys the card with the same word. The inspector
  says "Bypassed: `merge` reads `split.C0` through this step unchanged", with
  **Stop bypassing**.
- **Toggling**: **Bypass** in the box's context menu and the Edit menu,
  shortcut **B** on the canvas (beside **F** and **H**; free today). Disabled,
  with the reason as its tooltip, on a step 5.4 refuses.
- **Exposing a parameter**: **Expose as control…** in each parameter row's
  context menu asks for a name (default: the key) and, when a control of a
  compatible field exists, offers to add the parameter to it (adopting its
  value, 8.1). An exposed row shows a "⇄ radius" chip; editing it edits the
  control.
- **Controls panel**: with nothing selected, the inspector shows the
  workflow: name, description, and its controls, each with its field, label,
  targets (click to select the step), **Reset** (disabled with the reason
  when 8.1 says so), **Rename…** and **Stop exposing**. An inactive control
  is shown with "no effect while `smooth` is bypassed" and stays editable (it
  writes the parameters for when the step is switched back on).
- **Boxes of targets** mark the exposed parameter on their parameter line
  with the same chip.

## 9. ImProcess runs

The dialog of 4.7, in `WorkflowController`: after a file is loaded and, if it
has Python steps, confirmed, a workflow with controls opens a modal form
(`ParamForm` over one field per active control, built from its combined
field, with its label and help; inactive controls listed disabled with their
reasons), filled with the file's values. **Run** passes the values to
`prepare_run`; the status line and the report name them. Runs from the editor
do not ask.

## 10. Code touched

| Where | What |
| --- | --- |
| `workflows/steps.py` | `Control`; `Workflow.controls`; `bypass` on `Process` and `Save`; strict reading (section 7); lowest-version writing; recipe checks |
| `workflows/prepare.py` (new) | `prepare_run`, `check_recipe`, bypass rewriting, activity, the combined field, the value contract |
| `workflows/runner.py` | `run()` goes through `prepare_run`; `controls=`; `RunReport.bypassed` and `.controls` |
| `workflows/batch.py` | `ManifestRow`, `rows_from_manifest`, `controls=`, run-wide and per-row checks, unknown-column warnings, `control:<name>` and `bypassed` columns |
| `workflows/__main__.py` | `--set` on `run` and `validate`; `controls` command |
| `processors/stack_split`, `processors/channel_split` | Axis-labelled port declarations (section 6) |
| `model/workfloweditor/document.py`, `session.py`, `nodes.py` | 8.1 and 8.2; the chip and the bypassed state in `NodeInfo` |
| `view/workfloweditor/` | Bypass drawing and action, parameter-row actions, the Controls panel |
| `controller/WorkflowController.py` | The dialog of section 9 |
| Docs | `improcess-workflows.rst` (format, value contract, CLI, editor), changelog, this plan's status |

## 11. Phases

| Phase | Content | Exit | New code incl. tests |
| --- | --- | --- | --- |
| S1 | Format and reading, `prepare_run`, value contract, runner and batch rows, CLI, split port declarations | Every rule of 4.5, 5.4 and section 7 refused with its message; the equivalence and preparation tests of 12 pass; every shipped example loads, validates and serialises with no schema-2 key | ~1300–1800 lines |
| S2 | Editor: document rules of 8.1, `RunSession` through `prepare_run`, drawing, Controls panel | The editor tests of 12 offscreen | ~1000–1400 lines |
| S3 | ImProcess dialog, docs, screenshots, changelog | Dialog values and disabled controls through the controller; real-window check | ~300–500 lines |

S1 is useful on its own for batch runs and the command line.

## 12. Tests

- **Reading**: round trip of controls and bypass; lowest-version writing,
  every shipped example serialising to the same `to_dict()` as before;
  omitted schema is 1; `schema: true` refused; schema 3 refused; `controls`
  (even `[]`) and `bypass` (even `false`) refused in schema 1; `bypass:
  "false"`, `0`, `1`, `null` refused; an unknown key in a control entry
  refused.
- **Recipe rules**: one test per rule of 4.5 and 5.4, each asserting its
  message; a target on a bypassed step with a missing plugin accepted; a
  control whose targets are all bypassed reported as a note.
- **Value contract**: per field type, the table of 4.6 both ways (`007` for
  an int is 7, for a text stays `007`; `on`, `yes`, `1` refused for a bool;
  `2026-11-02` stays text; `nan`, `inf`, `-inf` refused; `1` matches a
  numeric option and becomes the integer; `'null'` is text, `null` is None
  only when nullable; `''` empty text; `--set x=` refused); canonical values
  (`3` and `3.0` give the same params and fingerprint); combined fields
  (intersected bounds, empty intersection, different units, nullability,
  precision, reset value).
- **Preparation**: a shared control crossing two branches with a partial
  run to one of them; a control whose targets are all outside the partial
  run; a value for a control with no active target refused, with each reason;
  a binding that names a bypassed step refused; consecutive bypassed steps;
  requesting a bypassed step runs what it passes through; a schema-1 recipe
  prepared without values is unchanged.
- **Equivalence**: a run with `radius=3` equals the run with `radius: 3.0`
  written into both targets (pixels and provenance); a run with a bypassed
  step equals the run with the step removed and its consumer wired to its
  input (pixels; provenance minus the node); removing `controls` from a file
  computes the same as keeping them with no values.
- **Ports**: `split.axis: Z`, in the file and as a control value, with a
  consumer of `split.C0`: refused before the run; with `Auto`: a runtime
  failure at the reference naming the ports that exist.
- **Runner and GUI publishing**: a bypassed Save writes nothing and is
  listed; `report.results` has no alias keys; `_publish` publishes each result
  once for a bypassed step whose input was computed and for one whose input
  was bound from the list (the bound result is not republished); a cached
  downstream consumer of a bypassed step skips its ancestors.
- **Batch**: precedence file → `--set` → cell, empty cell inheriting; a bad
  cell fails its row only; an inactive control set run-wide or in a filled
  column refuses the batch; unknown columns warned once; `control:<name>` and
  `bypassed` columns; `--set` and `controls` through `main([...])`.
- **Replay**: a result made with a control value and a bypass replays to a
  schema-1 workflow with the value in its step and without the bypassed step.
- **Editor**: every row of 8.1 and its undo (adoption by `add_target` and
  when un-bypassing; Reset disabled with different defaults and enabled with
  a shared one; bulk edits keeping targets; conflicting bulk values refused);
  rename, delete and paste keeping targets right; fingerprints (setting a
  control outdates exactly targets and descendants; bypass outdates
  descendants and un-bypassing finds them cached); partial runs through
  `prepare_run` with shared and outside controls; the Bypass action disabled
  with its reason; offscreen checks of the chip, the panel, inactive
  controls and the dashed pass-through.
- **ImProcess**: dialog values reach `prepare_run`; inactive controls
  disabled with their reasons in each of the three run kinds (a control on
  the replaced reconstruction in *Run on selected results*); cancel runs
  nothing; no dialog without controls.

## 13. Risks

| Risk | Mitigation |
| --- | --- |
| A run-time value changes a step's ports | Values are applied before validation (stage 8); declared ports are checked then, with the splits declaring precisely (section 6); data-dependent existence fails at the reference, as today. |
| A partial or result-bound run trips over a control definition | Definitions never reach the execution workflow (stage 7); activity is worked out per run. |
| A bypass interacts with cached and bound results | Bypass is resolved before bindings, on the rewritten graph, and no bypassed step exists at run time (3.3). |
| Results published twice | No aliases in `report.results` (5.5); covered by a GUI test. |
| A value that does nothing passes unnoticed | A value for an inactive control is an error; the dialog disables such controls with the reason; the summary shows the value each row ran with. |
| Two targets drift apart | Explicit values, ownership by the control, adoption on `add_target` and un-bypass, reset only with a shared default, validation of active targets. |
| Command-line text read wrongly | Field-aware conversion, no general YAML parsing; canonical values; non-finite numbers refused. |
| A colleague on an older ImSwitch cannot open a file | Schema 2 only when needed, a status-bar note the first time, and the older version's refusal names the schema. |
| A bypass hides a broken step | Drawn as bypassed everywhere, listed in every report and summary; a missing plugin is a note. |

## 14. Decisions

Settled in review 1, as draft 1 recommended:

1. Values live in the steps, controls name them.
2. Save steps can be bypassed.
3. Raw `--set step.param=value` without a control: deferred.
4. Controls on Save paths and formats: deferred.
5. Bypass of a single-input processor given several inputs: deferred.
6. Narrower bounds or options on a control than the plugins': deferred (the
   entry format can take `minimum`, `maximum`, `options` keys later; today
   they are refused as unknown keys).

Made in this draft, for review 2 to confirm:

7. A value for a control with no active target is an **error** (4.5), and the
   dialog disables such controls with the reason.
8. **Reset** writes the active targets' shared default explicitly and is
   **disabled** when they have none (8.1); `add_target` and un-bypassing
   **adopt** the control's value.
9. Target values are **always explicit** in `params` (4.3), so a plugin's
   default changing later cannot split a control.
10. An empty manifest cell is **missing**; `null` and `''` are the explicit
    tokens (4.6); text is converted by the field, never by general YAML.
11. Units must **match** across targets; precision takes the finest (4.4).
12. Bypass is **removed in preparation**, not passed through at run time
    (5.6), and a binding on a bypassed step is refused.

## 15. Review record

Review 1 (2026-11-02) of draft 1, against the code with probes.

| # | Finding | Answer |
| --- | --- | --- |
| 1 | Partial editor runs keep top-level fields, so a shared control loses a target and the run becomes invalid; draft said "controls need nothing" | The preparation boundary (3.2): cut to the subset, then drop control definitions (stage 7); `RunSession.plan` goes through it (8.2); tests for a shared control across branches and one entirely outside the run (12). |
| 2 | Validating after applying values does not catch a changed split axis: the splits accept any letter-plus-number port | Guarantee narrowed to what declarations decide; S1 makes the splits declare axis-labelled ports and audits the built-ins; data-dependent existence stays a runtime check at the reference (section 6). |
| 3 | Bypass "before anything else" conflicts with cached runs that skip ancestors; binding a bypassed step undefined | Bypass resolved in preparation before bindings are considered (stages 2, 4); a binding on a bypassed step refused; tests for a cached downstream consumer, a binding on the bypassed node and consecutive bypassed steps (12). |
| 4 | Missing plugins on bypassed steps vs control checks; a control on a reconstruction that *Run on selected results* replaces would be offered | Activity per run (stage 5); inactive targets checked as far as their plugin allows and left out of the combined field (4.5); a value for an inactive control is an error; the dialog disables it with the reason (4.7). |
| 5 | Clearing all targets re-splits them when plugin defaults differ; `add_target` and bulk edits undefined | Explicit target values; Reset writes a shared default or is disabled; `add_target` and un-bypassing adopt the control's value; bulk edits keep targets unless they set them (4.3, 8.1). |
| 6 | Pass-through aliases are published as new results by `_publish`, republishing bound ones | No aliases exist: bypassed steps are removed in preparation (5.5, 5.6); GUI test for computed and bound inputs (12). |
| 7 | Empty cell meant both null and inherit; YAML turns `001`, `on`, dates into other types; `problem_with` has no textual select matching and accepts NaN | The value contract (4.6): missing, null and empty text kept apart; field-aware conversion and canonical values; non-finite refused; the same rules for the command line and manifests, dialogs checked alike. |
| — | Per-row API and precedence | `ManifestRow` keeps bindings and values together; file → run-wide → row; run-wide and column problems refuse the batch, a bad cell fails its row (4.7). |
| — | Combined field specification | Section 4.4: nullability needs every target, bounds intersect, units must match, finest precision, reset value only when shared. |
| — | Schema parsing edge cases | Section 7: omitted schema is 1; booleans refused; new keys need schema 2 even when empty or false; `bypass` must be a boolean. |
