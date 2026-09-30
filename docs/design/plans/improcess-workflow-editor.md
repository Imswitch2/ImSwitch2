# ImProcess Workflow Editor: Feasibility And Implementation Plan

Date: 2026-09-30

Status: Implemented through Phase 2 (2026-09-30) — Phase 0 contract fixes,
`param_spec()` with the defaults-derived fallback and contract test, the
Qt-free catalogue and document model, and the editor window wired into
ImProcess (`File → Workflow editor…`, `Edit workflow of current result…`).
Phase 3 (rich specs for every built-in) and Phase 4 are open.

## Summary

A GUI editor for ImProcess workflows, built on the same principle as the
setup config editor ("Config Studio"), is feasible, and most of the hard part
already exists. The verdict, in three lines:

- **Knowing every processor and reconstructor is solved today.** The
  workflow runtime already builds a complete plugin catalogue at runtime,
  drop-in plugins included, with each plugin's id, name, category, arity,
  output ports, parameter keys and default values
  (`imswitch/improcess/workflows/runtime.py:88-109`, `describe_registry`).
  Nothing has to be extracted from source the way the config editor extracts
  `managerProperties`: ImProcess plugins are importable and already declare
  a headless parameter contract that a test pins to their widgets.
- **What is missing is field-level typing.** Choices, ranges, units, labels
  and help text exist only inside each plugin's hand-written Qt widget, and
  no widget can be filled from a saved step (`set_values()` exists nowhere).
  A small declarative `param_spec()` contract with a defaults-derived
  fallback closes that gap for every plugin at once, and a drift test pins
  it to `default_params()` the way the widget is pinned today.
- **The editor itself is ordinary UI work** over data structures that exist:
  `Workflow`/`Step` dataclasses with YAML/JSON I/O, static `validate()`
  returning per-step issues, a runner with progress and cancel hooks, replay
  from any saved result, and a `WorkflowController` that already runs
  workflow files from the File menu.

The recommended shape is a non-modal single-instance window, opened from
ImProcess's File menu, with a plugin palette on the left, the step list in
the middle over a validation panel, and a per-step form on the right, driven
by a Qt-free `WorkflowDocument`. Phases are ordered so that the editor works
for **every** plugin from the first phase (plain typed fields from the
defaults) and gets richer forms as specs are declared.

## 1. The config editor, and what carries over

The config editor is three layers (`docs/design/plans/config-editor-discovery-and-schema.md`,
`config-editor-schema-extraction.md`):

| Config editor | Workflow editor counterpart | Status |
| --- | --- | --- |
| Catalog of managers (`imcontrol/model/configeditor/catalog.py:128`, registry + legacy scan) | Plugin catalogue from `bootstrap_registry()` + `describe_registry()` (`improcess/workflows/runtime.py:20,88`) | exists |
| Per-manager JSON Schema extracted from source (`extraction.py`, `schemagen.py`, checked-in `schemas/`, `--check` drift guard) | `default_params()` / `param_keys()` per plugin (`processors/base.py:219-244`, `reconstructors/base.py:206-222`), pinned to the widget by `_test/test_plugin_param_contract.py` | exists (keys + defaults), missing (types, choices, ranges, help) |
| `FieldSpec` + `normalized_fields()` merge of schema and template overlay (`schemas.py:61-156`) | `ParamField` + `param_spec()` with a defaults-derived fallback (this plan, §4.2) | to build |
| `FieldWidget` dispatch by type, `SectionEditorDialog` flat form (`view/configeditor/editor.py:1293, 2833`) | Generic parameter form over pyqtgraph `ParameterTree`, which four reconstructor widgets already use (§4.5) | to build |
| Validation service returning `SetupDiagnostic` (`imcontrol/model/plugins/validation.py:254`) | `validate(workflow, registry) -> list[Issue(step, message)]` (`workflows/steps.py:371-448`) | exists |
| Round-trip discipline: never rewrite what the operator did not change | `params` holds only what differs from the defaults (`docs/improcess-workflows.rst`, "Parameters and defaults") | exists as a file-format rule; the form must honour it |
| Wiring: lazy import, one non-modal window, `sig_closed`, restart offer (`imcontrol/controller/ImConMainController.py:600-670`) | Same pattern in `WorkflowController`; no restart needed, a workflow is data | to build |
| Templates as presentation overlays | Not needed: labels/tooltips come from the spec | n/a |

Things the config editor's own audit says to avoid, and which this plan
avoids from the start: import-time side effects in the view module, sentinel
signals, one 4.7k-line view file, duplicated tab-building code.

## 2. What exists for workflows today

Everything below is in the current checkout; nothing has to be invented.

**Data model and file format** (`imswitch/improcess/workflows/steps.py`).
`Workflow(name, steps, schema=1, description, metadata)` holds an ordered
list of `Source`, `Reconstruct`, `Consolidate`, `Process` and `Save`
dataclasses. Steps refer to earlier ones by `id` or `id.port`, so the list
is a DAG in execution order. `to_dict`/`from_dict`, `save`/`load` write YAML
or JSON by suffix. `Workflow.metadata` is free-form and round-trips; a step
has no free-form slot (an unknown key on a non-source step fails to load,
`steps.py:199-202`).

**Static validation** (`steps.py:371-448`). With a registry, `validate()`
reports unknown plugins, dangling or non-earlier references, ports that the
upstream step cannot produce (through `output_spec(params)`, patterns and
fan-out included), arity, unknown parameter keys, GUI-only plugins, bad
consolidations and unknown save formats. Each `Issue` carries the step id,
which is exactly what an editor needs for per-row markers.

**Runner** (`workflows/runner.py:200-417`). Synchronous, takes `progress`
and `cancel` callbacks, binds sources or in-memory results, runs every step
through the GUI's own code paths. `RunReport` maps `step.port` to results.

**Replay** (`workflows/replay.py`). `workflow_from_provenance` and
`workflow_from_file` turn any result's provenance graph into a `Workflow`.
This is the "macro recorder": every GUI action is already recorded.

**GUI** (`view/ImProcessMainView.py:79-86, 216-251`;
`controller/WorkflowController.py`). Four File-menu actions: export the
current result's workflow, run a workflow file, run it on selected results,
run it over files. `WorkflowController` owns a `QThread` worker, the
overwrite question and result publishing. It uses the **full** registry
(`WorkflowController.py:167-180`), not the setup-filtered one, so an
editor built on it sees every installed plugin.

**Plugin catalogue** (`workflows/runtime.py`). `bootstrap_registry()` builds a
fresh registry with all built-ins plus drop-ins; `describe_registry()` gives
per plugin: `name`, `version`, `params` (the defaults), `gui_only`, and for
processors `kinds`, `inputs [min, max]` and `ports`. It backs
`python -m imswitch.improcess.workflows list --json`.

**Plugin parameter contract** (`processors/base.py:168-280`,
`reconstructors/base.py:160-264`, `model/plugin_contract.py`).
`default_params()` is required and pinned to `make_param_widget().get_values()`
by `test_plugin_param_contract.py`; `param_keys()` = defaults +
`extra_param_keys`; `params_version`/`migrate_params`; `encode_params`/
`decode_params`; `output_spec(params)`; `kinds`, `min_inputs`/`max_inputs`,
`accepts_roi`, `roi_modes`; reconstructors add `prepare_params`,
`supports_consolidation`, `file_extensions`, `accepted_source_kinds` and
`inspect_source() -> SourceInspection` with data-dependent `choices`.

**Discovery** (`processors/__init__.py:39-69`, `reconstructors/__init__.py:31-42`,
`plugins/user_plugins.py`). Explicit class tables for the 29 built-in
processors and 10 built-in reconstructors, merged with drop-in `.py` files
from `~/ImSwitchConfig/improcess_plugins`; built-in ids win collisions.

## 3. Feasibility, requirement by requirement

### 3.1 Enumerating every plugin: solved

`describe_registry(bootstrap_registry())` is the catalogue. It includes
drop-ins and marks GUI-only plugins with the reason. The config editor's
`processing` section already takes its reconstructor and processor id lists
from the same tables.

### 3.2 Parameter keys and defaults: solved

Every built-in overrides `default_params()`; a test refuses one that does
not, and `validate()` refuses a step that sets a key outside `param_keys()`.
So an editor can list exactly the keys a step may set, with their defaults,
for every plugin that can run in a workflow at all.

### 3.3 Field-level types, choices, ranges, units, help: not solved

A static census of the 39 built-ins (29 processors, 10 reconstructors) over
their `default_params()` literals:

| Default value kind | Count | What an editor can infer from it |
| --- | --- | --- |
| `int` | 45 | integer field, no bounds |
| `float` | 55 | float field, no bounds, no units |
| `str` | 57 | text; most are really a choice from a combo box |
| `bool` | 25 | check box |
| `None` | 14 | nothing: type unknown (projection `start`/`stop`, time-lapse `detector`, tiling `detector`/`channel`/`max_shift_px`, widefield-starss `counterpart_path`/`split_y`/`intensity_threshold`/batch fields, smlm-localizer `pixel_size_nm`, beadrec `roi`, monalisa-legacy `pattern`) |
| `list` / `dict` | 2 / 0 (+2 dict via constants) | JSON field (stack-subset `ranges`, monalisa-legacy `scan_params`, `axis_label_map`) |
| module constant | 5 | resolves at runtime to one of the above |

So defaults alone give a *working* but plain form for about nine in ten
fields. The rest of the information lives only in imperative Qt code, in two
regular patterns and a tail of special cases:

- **Inline `QFormLayout` closures** (about 25 processors, e.g.
  `processors/filters/processor.py:51-88`): `QComboBox.addItems(...)`,
  `setRange`, `setDecimals`, `setSuffix`, `setToolTip`, `addRow("Label:", w)`,
  and a `get_values()` dict literal mapping key to widget.
- **pyqtgraph `ParameterTree` dict lists** (monalisa, snouty,
  snouty-projections, smlm-localizer, widefield-starss;
  e.g. `reconstructors/snouty/params_widget.py:28-48`): already declarative
  (`type`, `value`, `limits`, `values`, `suffix`, `tip`, groups), but keyed by
  display name, with the key mapping only in `get_values()`.
- **Special cases**: placeholder widgets whose values come from a toolbar
  dialog (channel-merge, stack-combine); a result-bound custom widget
  (stack-subset's `StackSubsetRangesWidget`); dedicated GUI panels that
  replace the plugin widget (`model/runtime_tools.py:27-41`: segmentation,
  colocalization, psf-resolution, multicolor-*); MoNaLISA special-cased by
  id to a legacy tree (`controller/ReconstructorManagerController.py:466-474`);
  `monalisa-legacy` returning no widget; widgets fed data-dependent choices
  through `set_source_inspection` (tiling, time-lapse).

Two ways to get this information into a Qt-free schema were weighed:

1. **Static extraction from widget code**, as the config editor does for
   managers. Feasible for the two regular patterns (the `get_values` dict
   literal is the key-to-widget map the config editor never had), but it is
   the wrong tool here: the config editor extracts because managers cannot
   be imported (drivers, hardware); ImProcess plugins can be, and already
   are, by the CLI. Extraction would also make the schema depend on how a
   widget happens to be written.
2. **A declarative `param_spec()` on the plugin**, next to `default_params()`,
   with a fallback derived from the defaults for plugins that declare
   nothing. This matches the existing contract culture (declare it, a test
   pins it) and gives the CLI, the docs and the LLM route the same field
   information for free.

This plan recommends option 2, with a one-off AST drafting script as an
accelerator for writing the 39 built-in specs (§5, Phase 3).

### 3.4 Loading a saved step into a form: not solved

Plugin widgets expose `get_values()` only; none of the 38 implementations
has a `set_values()`. Embedding the plugins' own widgets in the editor would
therefore need a new widget contract *and* would inherit every special case
above (widgets that need a live result, dialogs, batch UI). A generic form
built from the spec avoids both. Adding `set_values()` stays worthwhile as a
separate, optional improvement ("send this step's parameters to the
Parameters dock"), not as the editor's foundation.

### 3.5 Contract gaps the editor would expose (pre-existing)

Found while checking: these plugins read parameters in `apply()`/`process()`
that they neither declare in `default_params()` nor permit through
`extra_param_keys`, so `validate()` refuses a workflow that sets them while
the GUI path can:

- `image-calculator` reads `name` (`processors/image_calculator/processor.py:117`);
- `make-composite` reads `colormaps` (`processors/make_composite/processor.py:71`);
- `make-rgb` reads `channels` and `channel_levels` (`processors/make_rgb/processor.py:68-73`);
- `smlm-localizer` reads `loop_selection` (`reconstructors/smlm/localizer.py:298`).

Also: the dedicated GUI panels add undeclared session keys (`rois`) or omit
declared ones (segmentation's `t_index`/`z_index`/`c_index`/`axis_indices`),
and MoNaLISA's legacy tree lacks `auto_scan_orientation`. None of this blocks
the editor, but an editor makes it visible, so the small fixes belong in
Phase 0.

### 3.6 Validating that steps chain: partly solved

Ports and arity are validated statically today. **Kinds are not**: a
processor declares what it accepts (`kinds`) but not what it produces, and
`applies_to` needs a live result. The runner catches a kind mismatch at run
time (`runner.py:364-371`). The editor can filter the palette by port
availability and arity now; filtering by kind needs an optional
`output_kinds` declaration (Phase 4).

### 3.7 Data-dependent parameters

Some reconstructor choices come from the recording (`inspect_source()`,
`SourceInspection.choices`; time-lapse and tiling detectors; snouty's
`load_from_attrs`). An unbound source has no such choices. The form falls
back to a free text field for those keys; a bound source can be probed on
request (Phase 4). This is the same trade-off the CLI makes today.

### Verdict

Feasible, with a favourable ratio: the parts that would have been hard (a
plugin catalogue, a headless parameter contract, a file format, validation,
execution, recording) exist and are tested. New work is one small contract
addition, a Qt-free document model, and a moderate amount of Qt. No change
to the workflow file format is needed.

## 4. Design

### 4.1 Principles (the config editor's, restated for workflows)

- **The plugin is the type authority.** Keys and defaults come from
  `default_params()`; field typing from `param_spec()`; nothing is
  duplicated in the editor.
- **Qt-free model, thin view.** Catalogue, spec, document and validation
  import no Qt, so tests run headless and the CLI can print the same
  catalogue.
- **`params` holds only what you change.** A field left at its default is
  not written; a field the file set is written back as it was unless edited
  (the config editor's untouched-returns-original rule).
- **The editor never needs a live result.** It edits files; running is
  delegated to `WorkflowController`.
- **Do not change the step schema for UI needs.** Editor-only state (layout
  hints, collapsed groups) goes under `Workflow.metadata["editor"]`, which
  round-trips and which the runner ignores.

### 4.2 The parameter spec contract

A new Qt-free module `imswitch/improcess/model/param_spec.py`:

```python
@dataclass(frozen=True)
class ParamField:
    key: str
    type: str                      # int | float | bool | text | select | multiselect | path | json
    default: object
    label: str = ""                # key if empty
    help: str = ""                 # tooltip
    options: tuple = ()            # select / multiselect
    min: float | None = None
    max: float | None = None
    step: float | None = None
    decimals: int | None = None
    suffix: str = ""               # units
    group: str = ""                # form group; "" = top level
    nullable: bool = False         # None means "unset" (projection start/stop, pixel_size_nm)
    advanced: bool = False         # collapsed by default
```

On `Processor` and `Reconstructor`:

```python
@classmethod
def param_spec(cls) -> tuple[ParamField, ...]:
    """Field-level description of default_params(); the framework default
    derives one field per key from the default value's type."""
    return spec_from_defaults(cls.default_params())
```

Rules, pinned by a test over every built-in and the shipped examples
(`test_plugin_param_contract.py` gains the cases):

- the spec's keys equal `default_params()`'s keys, in any order;
- each field's `default` equals the declared default;
- a `select` default is one of its options; a bounded number's default is
  within bounds; a `nullable` field may default to `None`, nothing else may;
- `spec_from_defaults` is total: every JSON-encodable default maps to a
  field (`None` becomes a nullable `json` field, lists and dicts become
  `json`).

The vocabulary is the config editor's `FieldSpec.type` set minus `ref` and
`bool_auto`, so a field can be rendered by either editor's widgets, and it
maps one-to-one onto pyqtgraph `Parameter` options (`type`, `value`,
`limits`, `values`, `suffix`, `tip`, `title`, groups), which is what the
reconstructor widgets already write. `describe_registry()` grows a `fields`
list per plugin, so `workflows list --json` and the LLM prompt in
`docs/improcess-workflows.rst` get choices and ranges without further work.

`extra_param_keys` stay as they are: a key that is permitted but has no
default (MoNaLISA's `scan_params`) is shown as an optional `json` field.

### 4.3 The catalogue

`imswitch/improcess/model/workfloweditor/catalog.py` (Qt-free):

```python
@dataclass(frozen=True)
class PluginEntry:
    id: str; kind: str            # "processor" | "reconstructor"
    name: str; category: str; version: str
    fields: tuple[ParamField, ...]
    extra_keys: tuple[str, ...]
    min_inputs: int; max_inputs: int | None
    ports: OutputSpec | None      # processors; evaluated with the step's params
    accepted_kinds: tuple[str, ...]
    supports_consolidation: bool
    accepts_roi: bool
    gui_only: str | None          # contract_problem(), shown greyed with the reason

def build_catalog(registry=None) -> Catalog   # bootstrap_registry() when None
```

The catalogue is built when the editor opens (drop-ins may have changed) and
on an explicit Reload. Save formats come from `save_protocol.normalize_format`'s
table; path-template placeholders from `runner.render_save_path`'s set.

### 4.4 The document model

`imswitch/improcess/model/workfloweditor/document.py` (Qt-free) wraps one
`Workflow` and offers the operations the view needs, each returning the
issues that result:

- `add_step(kind, plugin_id=None, after=None)`: allocates an id
  (`src1`, `rec1`, `proc3`… the replayer's convention), seeds `inputs` with
  the selected step's first port, positions the step after its inputs;
- `remove_step`, `move_step` (refused if it would put a step before one of
  its inputs), `rename_step` (rewrites references);
- `set_plugin`, `set_params` (stores only non-default values), `set_inputs`,
  `set_source`, `set_save`;
- `port_options(step_id)`: the ports each earlier step can offer, from
  `output_spec(defaults + params)` and its pattern (a pattern port is typed);
- `issues()`: `validate(workflow, registry)` plus editor-level checks that
  `validate` leaves to run time and that are cheap to do statically (an
  unbound source, two saves rendering the same template, a path template
  with an unknown placeholder);
- `load(path)`, `save(path)`, `dirty`, `to_yaml()` for a read-only preview;
- `from_result(result)`: `workflow_from_provenance` on a result's graph, the
  in-editor form of "Export workflow of current result…".

A hand-written YAML file loses comments and flow style on save, exactly as
a setup file loses formatting in the config editor. The editor says so once,
on first save of a file it did not create.

### 4.5 The view

`imswitch/improcess/view/workfloweditor/` (several small modules, no
import-time side effects):

```
+----------------------------------------------------------------------+
| New  Open  Save  Save as   Validate   Run…  Run on selected results… |
+---------------+-----------------------------------+-------------------+
| Files         | Steps (execution order)           | Step              |
|  wf1.yaml     |  ▸ raw    source    (unbound)     |  id  [proc2     ] |
|  wf2.yaml     |  ▸ rec    reconstruct view-only   |  processor [filter ▾] |
|               |  ▸ split  process  stack-split    |  inputs           |
| Palette       |  ▸ smooth process  filter  ⚠      |    [split ▾].[C0 ▾] (+)|
|  Sources      |  ▸ merge  process  channel-merge  |  parameters       |
|  Reconstruct  |  ▸ out    save     hdf5           |    Filter   [gaussian ▾]|
|   monalisa    |                                   |    Radius   [ 2.00 ] px |
|   view-only   +-----------------------------------+    Amount   [ 0.60 ] (off)|
|  Process      | Validation                        |  restriction: none |
|   Filters ▸   |  smooth: port 'C0' is not one     |                   |
|   SMLM ▸      |    'split' produces (/[A-Za-z]+\d+/)|  [Reset to defaults]|
|  Save         |                                   |                   |
+---------------+-----------------------------------+-------------------+
```

- **Palette**: from the catalogue; processors grouped by `category`,
  drop-ins under their own root (as `available_processor_specs(origin=...)`
  already separates them), GUI-only plugins greyed with the reason.
  Double-click or drag adds a step after the selection.
- **Step list**: a `QTreeWidget` in execution order with id, kind, plugin,
  inputs and an issue marker; up/down and drag reorder through
  `move_step`. A graph canvas is deliberately not in the first version: the
  file format is an ordered list with references, and a canvas is a second
  view over the same document (Phase 4).
- **Step form**, one per step kind:
  - *Source*: path with Browse…, dataset, source kind, or "bind at run".
  - *Reconstruct/Consolidate*: reconstructor combo (consolidate limited to
    `supports_consolidation`), the one source input, parameter form.
  - *Process*: processor combo, ordered inputs with per-input port combos
    (from `port_options`), arity shown as "2..∞", parameter form, and the
    ROI restriction as a read-only summary with Clear (a restriction is
    recorded by the GUI, not typed).
  - *Save*: input, format combo, path template with placeholder help and a
    live rendered example.
- **Parameter form**: one generic widget built from `ParamField`s on a
  pyqtgraph `ParameterTree` (`title` for labels, `limits`, `suffix`, `tip`,
  groups, `advanced` collapsed). Fields at their default are shown with a
  "default" marker and are not written; a per-field reset and a form-wide
  "Reset to defaults" exist. A `json` field opens `JsonEditorDialog` from
  `imcommon.view.guitools` for lists and dicts. Values are written back
  through the plugin's `encode_params`, so a spec cannot produce something
  the runner would not accept.
- **Validation panel**: every `Issue`, click selects the step and focuses
  the offending field where the message names one.

### 4.6 Wiring

- `ImProcessMainView` gains `sigOpenWorkflowEditor` and a **File → Workflow
  editor…** action next to the four existing workflow actions (grouping all
  five under a *Workflows* submenu is a one-line follow-up).
- `WorkflowController.openEditor()` mirrors `ImConMainController.openConfigEditor`
  (`ImConMainController.py:600-631`): lazy import, one window, `show/raise`
  on a second trigger, `sig_closed`, construction failure reported in a
  message box. No restart prompt: a workflow file changes nothing about the
  running session.
- Editor → controller: `sig_run_requested(workflow)` and
  `sig_run_on_results_requested(workflow)`. `_runBatch` already accepts an
  in-memory workflow (`WorkflowController.py:299-340`); it becomes public for
  this. Running passes the runner's `progress` callback into a status-bar
  progress and adds a Cancel action, which the runner supports and the GUI
  does not use yet.
- Controller → editor: `editFromResult(result)` for a new **File → Edit
  workflow of current result…**, the editing form of the export action.
- The editor validates against the same full registry
  `WorkflowController._registry` builds, so what validates in the editor is
  what runs.

### 4.7 Files and folders

Workflow files get a default folder, following the just-landed
Preferences → Default folders… pattern (`model/folder_preferences.py`):
a third field, `workflowFolder`, empty by default, falling back to
`~/ImSwitchConfig/improcess_workflows` (a sibling of `improcess_plugins`).
The editor's file list, the four existing file dialogs and the run dialogs
start there. Relative source paths are resolved against the workflow file's
folder, as the CLI does (`source_root`), which also fixes the current GUI
behaviour of resolving them against the process's working directory.

### 4.8 Module layout

```
imswitch/improcess/model/param_spec.py                 ParamField, spec_from_defaults, checks
imswitch/improcess/model/workfloweditor/__init__.py
imswitch/improcess/model/workfloweditor/catalog.py     PluginEntry, Catalog, build_catalog
imswitch/improcess/model/workfloweditor/document.py    WorkflowDocument
imswitch/improcess/view/workfloweditor/__init__.py     MainWindow export (lazy)
imswitch/improcess/view/workfloweditor/editor.py       MainWindow, toolbar, layout
imswitch/improcess/view/workfloweditor/palette.py
imswitch/improcess/view/workfloweditor/steplist.py
imswitch/improcess/view/workfloweditor/stepforms.py    one form per step kind
imswitch/improcess/view/workfloweditor/paramform.py    ParamField -> ParameterTree
imswitch/improcess/view/workfloweditor/validation.py
imswitch/improcess/controller/WorkflowController.py    openEditor, editFromResult, progress/cancel
imswitch/improcess/_test/test_param_spec.py
imswitch/improcess/_test/test_workflow_editor_model.py
imswitch/improcess/_test/test_workflow_editor_gui.py   offscreen, pytest-qt
docs/improcess-workflows.rst                            "Editing workflows in the GUI"
docs/improcess.rst                                      param_spec in the headless contract section
docs/changelog.rst
```

## 5. Phases

Each phase leaves the suite green and is independently useful.

### Phase 0: contract fixes (small, no editor code)

- Declare the keys read but undeclared (§3.5): `image-calculator.name`,
  `make-composite.colormaps`, `make-rgb.channels`/`channel_levels`,
  `smlm-localizer.loop_selection` (as `extra_param_keys` where no widget
  default exists; as defaults where one does). Add `auto_scan_orientation`
  to MoNaLISA's legacy tree or record it as volatile.
- Exit criterion: a workflow that sets any of those keys validates, and
  `test_plugin_param_contract.py` still passes.

### Phase 1: parameter spec + catalogue (Qt-free)

- `ParamField`, `spec_from_defaults`, `param_spec()` on both base classes,
  the contract test cases, `describe_registry()` `fields`, `list --json`.
- `build_catalog`, `WorkflowDocument` with every operation in §4.4 and
  model tests: add/remove/move/rename with reference rewriting, port
  options for named and pattern ports, non-default-only `params`, load and
  save round trip of the four `examples/improcess_workflows/*.yaml` files
  (kind-strict, as the config editor's corpus test), `from_result` on a
  synthetic provenance graph.
- Exit criterion: `python -m imswitch.improcess.workflows list --json`
  prints a typed field for every key of every built-in, and the document
  model round-trips the examples unchanged.

### Phase 2: editor window (minimum useful)

- Palette, step list, step forms, generic parameter form from the fallback
  specs, validation panel, open/save/save-as, Run… through
  `WorkflowController` with progress and cancel, File-menu wiring, single
  instance.
- GUI tests (offscreen): open the editor on an example, add a step from the
  palette, change a parameter and confirm only that key is written, break a
  reference and see the issue, save and reload, run a synthetic workflow
  through the controller from the editor.
- Exit criterion: the four shipped example workflows can be opened, edited,
  validated, saved and run from the editor without touching a text file.

### Phase 3: rich specs for every built-in

- `param_spec()` for all 39 built-ins and the shipped example plugins:
  choices, bounds, units, tooltips and groups as the widgets have them.
  A throw-away AST script (`tools/draft_improcess_param_specs.py`) can draft
  the inline-closure and pyqtgraph cases from `addItems`/`setRange`/
  `setSuffix`/`setToolTip`/`addRow` and the `get_values` mapping; the tail
  is written by hand.
- Exit criterion: no built-in field of type `text` whose widget is a combo
  box, and no `json` field except stack-subset `ranges`, MoNaLISA
  `scan_params` and the legacy dicts.

### Phase 4: beyond the config editor's scope (optional, in any order)

- `output_kinds` on processors and kind-aware palette filtering and
  validation (§3.6).
- "Probe source…" for a bound source: `inspect_source()` choices into the
  form (§3.7).
- `set_values()` on plugin widgets, pinned by the contract test, and
  "Send to Parameters dock" / "Take from Parameters dock" between the
  editor and the live panels.
- A default `make_param_widget` generated from `param_spec()`, so a plugin
  that declares a spec needs no widget code and the two cannot drift;
  existing widgets keep overriding.
- A graph canvas as a second view over the document (pyqtgraph's flowchart
  module is available; a read-only DAG picture is enough at first).
- Undo/redo through `QUndoStack` on the document operations.

### Effort

| Phase | New code (incl. tests) | Mostly |
| --- | --- | --- |
| 0 | ~100 lines | plugin edits |
| 1 | ~900-1200 lines | Qt-free Python, tests |
| 2 | ~1500-2000 lines | Qt |
| 3 | ~700-1000 lines | data (specs), one script |
| 4 | open-ended | per item |

## 6. Alternatives considered

- **Embed each plugin's own widget in the editor and add `set_values()`.**
  Identical look to the panels and no spec to write, but every widget
  needs a new method, several need a live result (`setResult`,
  `set_source_inspection`, stack-subset), some are placeholders or batch
  UIs, MoNaLISA is special-cased by id, and there is no headless catalogue
  for the CLI or the docs. Kept as a Phase 4 extra, not the foundation.
- **Extract specs from widget source at build time, checked in with a
  drift guard, as the config editor does.** Right for managers that cannot
  be imported; unnecessary here, and it would tie the schema to widget
  idioms. Used only as a one-off drafting aid.
- **Start with a node-graph canvas.** Attractive, but the file format is a
  list with references, and the first version should edit exactly what the
  file can express. A canvas is a view to add once the document model is
  stable.
- **Extend the step schema with editor fields (`enabled`, `label`,
  positions).** Rejected: a non-source step with an unknown key fails to
  load in every existing ImSwitch, and the runner would have to learn to
  ignore them. `Workflow.metadata["editor"]` round-trips already.

## 7. Risks and open questions

- **Parameter values are never type-checked at run time** (`validate`
  checks keys only). The form's typing is therefore the only guard; a
  hand-written file with a wrong type still fails inside `apply`. A
  follow-up could have `validate` check values against `param_spec()`.
- **Choices that depend on the data** stay free text until a source is
  bound and probed (§3.7).
- **Replay export carries full parameter sets**, not minimal overrides.
  `from_result` should drop values equal to the defaults so the editor shows
  the same "only what you change" view for exported and hand-written files.
- **Where should the editor live long-term:** ImProcess only (this plan),
  or also standalone like `utility_scripts/imswitch_config_editor.py`? The
  Qt-free model makes both possible; the plan wires ImProcess first.
- **Name clash to keep in mind:** `imswitch/imcontrol/model/workflows/` and
  the scripting docs are about acquisition workflows; menu text and module
  names here say "ImProcess workflow" where it could be ambiguous.
