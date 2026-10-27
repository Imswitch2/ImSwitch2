# ImProcess Python Step: Freeform Processing Feasibility And Plan

Date: 2026-09-30

Status: Implemented 2026-09-30 on `feat/improcess-python-step`: Phases A and B (the step, panel, `code` field, block-style YAML, shared code editor, snippets, the notice before a file's Python code runs), Phase C (the console) and Phase D (processor runs on a worker thread with cancel and live output). Implementation brief: `docs/agent_tasks/improcess_python_step.md`; see *Implementation notes* at the end.

## Summary

A Fiji-macro-like escape hatch for ImProcess — a few lines of Python that
turn a result into new results, without writing a processor — is feasible,
and cheaper than it looks, because the framework already treats a
processor's parameters as the record of what was done. Make the **code a
parameter** of one built-in processor and everything the other steps get
comes along unchanged: the run path, the provenance graph, replay, batch
runs, the CLI, the workflow editor's forms, and the contract tests.

A sixty-line prototype of that processor (`python_step.py`, kept out of
the tree) was loaded as a drop-in and driven headlessly through the
existing machinery with no framework change:

| Check | Result |
| --- | --- |
| Headless contract (`check_plugin_contract`, `spec_problems`) | passes |
| `validate()` on a reference to a port the code does not declare | refused: `port 'c' is not one 'split' produces (a, b)` |
| The requested example (slices 0–2 → `a`, 3–5 → `b`, 6–8 → `a`, …) | `split.a` and `split.b` come out with the right slices |
| Chaining and saving (`filter` on `split.a`, `Save` of `split.b`) | works, file written through the staged save |
| Provenance | the code is recorded losslessly on the node; `replayable: True` |
| Replay to a workflow file | the same code comes back verbatim, as YAML |

What is not in place is the user-facing part: a code editor in the panel,
readable multi-line code in workflow files, tracebacks the user can see,
and an interactive console over the live results. Those are ordinary UI
work. The recommendation is to build the **step** first (it is what
workflows, batch runs and replay need) and the **console** second, on the
same execution contract.

## 1. The request, as it would look

The example given — along one axis, three slices at a time alternating
between two outputs — as a step in a workflow file:

```yaml
- step: process
  id: split
  processor: python
  params:
    ports: a, b
    code: |
      ax = axis("Z")                       # or an index
      group = (np.arange(data.shape[ax]) // 3) % 2
      outputs = {
          "a": np.take(data, np.flatnonzero(group == 0), axis=ax),
          "b": np.take(data, np.flatnonzero(group == 1), axis=ax),
      }
  inputs: [rec]
- step: process
  id: blur
  processor: filter
  params: {radius: 1.0}
  inputs: [split.a]
```

In the GUI: **Tools → Python step**, paste the code, type the port names,
**Run**; two results appear in the reconstruction list, each carrying the
code in its provenance, so **File → Export workflow of current result…**
writes the file above and **Run workflow over files…** applies it to a
folder. Nothing about this is special to the Python step; it is what every
processor gets.

## 2. What exists today

- **No scripting access to ImProcess.** ImScripting exports only
  `api.imcontrol` (`imscripting/controller/ImScrMainController.py:39-56`);
  `ImProcessMainController` has no `api` property, and nothing under
  `imswitch/improcess` uses `APIExport`. The raw controller is reachable
  as `controllers.improcess`, but its result accessors read the Qt list
  directly and are only safe on the GUI thread, and publishing would mean
  emitting a private signal with no provenance. Not a route to build on.
- **The napari console** ships (napari-console is a hard dependency of the
  installed napari) and is probably toggleable in the embedded viewer, but
  its namespace holds only `viewer`; a result computed there can come back
  only through *Import layer from napari as result…*, which records a
  non-replayable `napari-import` node (`model/napari_import.py:227`,
  `provenance.py:821`). Good for looking, not for recording.
- **Drop-in plugins** are the current freeform route
  (`~/ImSwitchConfig/improcess_plugins/*.py`): a `Processor` subclass with
  `id`, `default_params()`, a widget and `apply()`. Right for a tool used
  more than once; too much for "three slices at a time, this once".
- **Pieces to reuse:** ImScripting's QScintilla Python editor
  (`Scintilla` in `imscripting/view/EditorView.py:163-184`) and its
  worker-thread executor with cooperative cancel
  (`model/ScriptExecutor.py:320-363`); `ProcessorOutput` with named ports
  and `OutputSpec` (`processors/base.py:12-72`); the strict parameter
  codec, which stores a string of any length losslessly (only the legacy
  `processing_history` footprint truncates strings at 512 characters,
  `model/footprint.py:38`).
- **A constraint to know:** processors run synchronously on the GUI thread
  in the panels (`ResultProcessorController.py:50-85`); only the workflow
  runner is on a worker thread. A slow script freezes the window exactly
  as a slow built-in does today. Errors reach the user as one status-label
  line, with the traceback in the log only.

## 3. Design

### 3.1 The step

One built-in processor, id `python`, category *Scripting*, accepting the
array-backed kinds (`image`, `labels`, `composite`), `min_inputs = 1`,
`max_inputs = None`.

Parameters (both plain strings, so the file format, the provenance codec
and the editor need nothing new):

| Key | Field | Meaning |
| --- | --- | --- |
| `code` | `code` (new field type: multi-line, monospaced) | the script |
| `ports` | `text` | comma-separated output names, default `out`; what `output_spec()` declares, so `validate()` checks references before the run and the runner checks what the code produced after it |

The script's namespace, which is the whole of its API:

| Name | What it is |
| --- | --- |
| `np` | numpy |
| `data` | the first input's array (`np.asarray`, a lazy source materialised) |
| `inputs` | every input's array, in the order listed |
| `axes`, `scales`, `unit` | the first input's axis labels, pixel scales and unit |
| `axis(label)` | the index of an axis by label (`"Z"`), or the index itself when given a number |
| `results` | the input `ProcessingResult` objects, for metadata (read them, do not mutate them) |
| `make_result(array, *, axes=None, scales=None, name=None)` | an output with explicit axes, for an array whose dimensionality differs from the input |
| `make_labels(array, ...)` | a labels output (segmentation) |
| `outputs` / `out` | what the script sets: a dict port → array or `make_result(...)`, or one array for the single port `out` |

Rules the run enforces, with messages that name the rule: every declared
port must be set; an array with the input's dimensionality inherits its
axes and scales, any other must come through `make_result(..., axes=)`;
the output must be an ndarray (or a result). Anything else the script
raises is reported with the traceback trimmed to the script's own frames
and line numbers (`compile(code, "<python step 'split'>", "exec")`), and
`print` output is captured and shown with it.

Provenance and replay need nothing: `code` and `ports` are the recorded
parameters, `params_version = 1`, and a step is replayable exactly when
its code is, which it always is. Workflow files should write the code in
YAML block style (`code: |`) — a `SafeDumper` representer for multi-line
strings, checked to round-trip — so a file reads like a script rather than
a quoted line with `\n`.

### 3.2 GUI panel

The step's `make_param_widget` is a code editor above a port line and a
read-only output pane (stdout and the last traceback). The editor is
ImScripting's `Scintilla` (Python lexer) moved to
`imcommon.view.guitools.CodeEditor` so both modules share it, with a
`QPlainTextEdit` fallback when QScintilla is missing. The panel joins the
Tools toolbar through `runtime_tools._PROCESSOR_WIDGET_SPECS` like any
processor panel. `get_values()` returns the editor text and the ports, so
the contract test holds it to `default_params()` unchanged.

A **snippet folder**, `~/ImSwitchConfig/improcess_snippets/*.py`, with
*Load…* / *Save as…* in the panel, keeps the one-offs that turn out to be
used twice; the first line of a snippet may carry its ports
(`# ports: a, b`). A snippet that grows up becomes a drop-in plugin by the
documented template; nothing forces that step.

### 3.3 Workflow editor

`ParamField` gains the type `code`; the form renders it as a multi-line
editor (pyqtgraph's `text` parameter, or the shared code editor below the
tree when the field is the step's only large one). `ports` is a plain
text field, and the port options offered to later steps come from it
through `output_spec`, as they do for every processor. The catalogue and
`workflows list --json` print the field as they print any other.

### 3.4 The console (second stage)

An ImProcess console dock (pyqtgraph's `ConsoleWidget`, as ImScripting's,
on the GUI thread) with the same namespace plus the live list:
`current()` and `selected()` return results, `publish(array_or_result,
name=..., axes=...)` adds one to the list. A published result is recorded
with an `opaque` node ("made in the console") — honest, since the console
cannot know which lines made it — and the dock's **Send to Python step**
copies the editor's text into a step so the recorded, replayable form is
one click away. This is the exploration tool; the step is the record.

### 3.5 Running and trust

The step runs wherever processors run: on the GUI thread from the panel
(a freeze for a long script, as for any processor today) and on the
workflow controller's worker thread from the editor and the File menu.
Moving processor runs to a worker thread with cooperative cancel — the
ScriptExecutor pattern — is worth doing for every processor and is
listed under Phase D, not required by this one.

The code runs in-process with full Python, like a drop-in plugin, an
ImScripting script or a Fiji macro; there is no sandbox and the
documentation says so. A workflow file from someone else can therefore
run their code: the editor shows every step's code before **Run…**, and
the File menu's *Run workflow…* can say once per file that it contains
Python steps. That notice is the only guard worth having; anything
stronger would make the feature useless.

### 3.6 Module layout

```
imswitch/improcess/processors/python_step/__init__.py
imswitch/improcess/processors/python_step/context.py     Qt-free: namespace, helpers, exec, error formatting
imswitch/improcess/processors/python_step/processor.py   PythonStepProcessor (id "python"), widget, spec
imswitch/imcommon/view/guitools/CodeEditor.py           Scintilla editor shared with ImScripting
imswitch/improcess/model/param_spec.py                   + "code" field type
imswitch/improcess/view/workfloweditor/paramform.py      renders "code"
imswitch/improcess/workflows/steps.py                    block-style YAML for multi-line strings
imswitch/improcess/_test/test_python_step.py             context rules, errors, the interleave example, replay
examples/improcess_workflows/python_step_interleave.yaml the example above, runnable on synthetic data
docs/improcess.rst                                       "Python step" section; docs/improcess-workflows.rst example
```

## 4. Phases

| Phase | Content | Size |
| --- | --- | --- |
| A — the step | `context.py`, the processor, a `QPlainTextEdit` widget, registration and Tools panel, tests, docs and the example file | ~600 lines, half of it tests |
| B — files and editor | `code` field type in the spec and form, block-style YAML, shared Scintilla editor, snippet folder | ~400 lines |
| C — console | the dock, `current`/`selected`/`publish`, opaque provenance, *Send to Python step* | ~400 lines |
| D — robustness | processor runs on a worker thread with cancel and streamed output, for every processor | separate plan |

Phase A alone delivers the request: the example runs from the panel, is
recorded, exported, batched and replayed.

## 5. Alternatives considered

- **Extend ImScripting with `api.improcess`.** Gives scripts the live
  list, but a script is not a step: nothing it makes is recorded, replayed
  or batched, and the editor cannot show it. Worth adding later for
  automation (`api.improcess.runWorkflow`, `publish`), on top of the step,
  not instead of it.
- **Use the napari console and import layers back.** Available now with no
  work, and stays useful for looking; every imported result is
  non-replayable by construction.
- **Lower the cost of drop-in plugins** (a decorator turning a function
  into a processor). Helpful, and it composes with this: a snippet is the
  function body; the decorator is how it becomes a plugin. It does not
  remove the file, the id and the reload from a one-off.
- **A restricted expression language** (numexpr-style). Safer, but the
  example already needs `arange`, `take` and a modulo; the moment a user
  wants a loop, it is Python or nothing.

## 6. Open questions

- **Result kinds beyond arrays.** Tables and localizations have their own
  result classes; `make_table` / `make_localizations` helpers are natural
  but not needed for the first version.
- **ROI restriction.** `accepts_roi` could be `True` at once, since the
  restriction is applied around `apply` by the run path; whether a script
  should see the restriction it ran under is the only question.
- **Imports inside scripts.** `import scipy.ndimage` works; the provenance
  records ImSwitch's version, not scipy's. Recording the versions of
  modules a script imported is possible (`sys.modules` before and after)
  and would make the record complete.

## 7. Implementation notes (Phases A and B, 2026-09-30)

What was built follows the design; where it differs or decides something the
design left open:

- **`make_labels` builds `LabelsResult`** (`model/labels_result.py`, kind
  `labels`), not `SegmentationResult`. The latter is fixed to `("Y", "X")` and
  needs a full `SegmentationAnalysis`, so it cannot honour `axes=` / `scales=`
  or a stack of labels; `LabelsResult` is the same kind, takes any
  dimensionality and is what an imported labels layer already becomes.
- **The compile filename is `<python step>`**, not `<python step 'split'>`: a
  processor is not told its step's id. `run_script(..., step_name=)` names it
  for a caller that knows one. The `SyntaxError` line is the error's own.
- **`python` joins the kind allow-lists** in `test_result_kind_matrix.py`
  (labels and composite), with the reason beside them: its code decides what
  the values mean.
- **The hook is `notify_param_widget`**, a module-level function in
  `ResultProcessorController` rather than a method, because the existing
  controller tests bind `runProcessor` onto a stand-in object.
- **`SystemExit` from the script is an error**, not the end of the program; a
  `KeyboardInterrupt` is not caught.
- **Not done, on purpose:** what a script printed *before* it failed is not
  shown (the hook receives only the one-line failure message); `data` is the
  input's own array, not a read-only copy (the docs say to copy it before
  changing it in place); the versions of imported modules are not recorded
  (open question above).

## 8. Implementation notes (Phases C and D, 2026-09-30)

**Phase C, the console.** As designed, with these decisions the design left open:

- **Editor and console.** The dock is a code editor (the shared
  `PythonCodeEditor`) above pyqtgraph's console, both in one namespace. The editor
  is what *Send to Python step* sends; pyqtgraph's console alone has no multi-line
  editor, and feeding pasted code through its line-by-line REPL breaks on a blank
  line inside a block. **Run** (Ctrl+Enter) compiles the text as a whole.
- **The namespace** is the step's less `outputs` / `out` (nothing reads them in a
  console) plus `current()`, `selected()`, `publish()`. `data` and the names with
  it are bound to the selection (the current result when nothing is selected),
  rebound only when the selection *changes* and before a command, never while a
  `publish` runs: rebinding on every command would undo the user's own
  `data = data[0]`. A result that is not in memory stays lazy
  (`build_namespace(materialise=False)`); the step still reads its input once.
- **`publish`** takes `like=` (the result axes and calibration are inherited from;
  by default the first bound result) next to `name`, `axes`, `scales`, and shares
  the step's output rules (`result_from_value`).
- **Provenance.** An `opaque` node with *no inputs*: the console cannot know which
  lines made the array, so it claims no lineage; the selection at the time is kept
  as a label (`selected_when_published`). A step run on such a result chains on the
  node and is reported not replayable, with the reason.
- **Send to Python step** fills the step's code field through a new
  `set_values` hook and `ResultProcessorWidget.setParameterValues`, runs nothing,
  and tells the person `publish(...)` becomes `outputs = {...}` in a step.

**Phase D, worker-thread runs.** As the design's §3.5 proposed ("the ScriptExecutor
pattern"), reusing its pieces by moving them to `imcommon.model`
(`routeThisThreadsOutputTo`, `interruptThread`; `CancelToken` was already there):

- **Where.** Every panel that publishes through `ResultProcessorController` (the
  generic ones and the hand-built Segmentation, PSF resolution and Colocalization
  panels, which now have a Cancel button through a shared `RunState`), the image
  toolbar's processor operations, and workflow runs. Not converted: the
  **Multicolor** panel, which computes inside its own widget methods rather than
  through `Processor.apply`, and the toolbar's **Duplicate**, which is a copy.
- **Cancel** asks (token; a processor may call `checkpoint()`), then after 1.5 s
  injects `OperationCancelled` into the thread, again every second if it was
  caught. A cancelled run publishes nothing, even if it finished first. Shutdown is
  the one bounded wait; a thread that will not stop is parked, not destroyed.
- **Workflows** interrupt the thread only while a *processor* step runs: a save
  (staged, atomic) or a reconstruction is left to finish, so a file is never left
  half published. `run()` turns a cancellation raised inside a step into the same
  `RunError` ("split: cancelled", report attached) the between-steps check gives.
- **Streaming** is opt-in: a parameter widget that declares `output_appended`
  gets what the processor prints, from this thread only (routing is per thread, not
  a swap of `sys.stdout`, so the GUI thread's prints are neither captured nor lost).
  The framework's own failure log is kept out of it.
- **Inline fallback.** A controller without a runner, or an image toolbar whose view
  is not a real window, runs inline as before. That is what lets the existing
  synchronous tests stand, and what a headless caller gets; the asynchronous paths
  have their own tests with real widgets and real threads.
- **Still open.** A long call into compiled code (one numpy operation) cannot be
  interrupted until it returns; the Multicolor panel is still synchronous.

## 9. Recipes (2026-10-01)

Eight jobs no processor does shipped first (a ninth, `signal_trace`, followed with the curves of §10) as snippets in
`imswitch/_data/user_defaults/improcess_snippets/` (installed by the existing
user-defaults sync, so *Load snippet...* lists them with no setup) and as workflow
files in `examples/improcess_workflows/`. The snippet is the single source: the
workflow file is generated from it (`tools/make_python_recipe_workflows.py`), and
`test_python_step_recipes.py` fails when they disagree, checks each recipe's
numbers against a known answer, runs every workflow end to end, and requires the
docs and the examples README to mention each recipe. Decisions worth keeping:

- Recipes convert to float before arithmetic: `data` has the recording's dtype and
  16-bit arithmetic wraps silently (documented under *Things to know*).
- A recipe that needs a particular shape (two channels, a tile grid, a focus
  stack) checks it and says what is wrong; none fails with an index error.
- `despeckle` uses 8 noise widths, chosen by measuring false positives on pure
  Gaussian and Poisson noise (5 replaced genuine pixels at about 30 per million).
- Outputs of one dimension are curves (see below, 2026-10-01); this note used to say
  the opposite, that a one-dimensional result was accepted but could not be drawn and
  numbers should be reported with `print`. That was wrong: the image viewer cannot
  draw one, the Graph panel can.
- `tools/update_user_defaults_history.py` rebuilds the hash history from git; in a
  shallow clone that silently drops older hashes, so the new entries were
  added to the committed file instead of regenerating it.

## 10. One-dimensional outputs are curves (2026-10-01)

A script's one-dimensional output (a value per frame, per plane) used to become an
image result with nothing to show: a blank Graph panel, an empty viewer, and a TIFF
save that failed. The Graph panel is where a curve belongs, and the machinery was
already there (`kind = "curve"`, `plot_payloads()`, the Graph dock raised on
production, as for FRC). Decisions:

- `CurveResult` (`model/curve_result.py`) is an `ArrayProcessingResult` of kind
  `curve`: one named axis, `x = index * scale`. Evenly sampled by construction; an
  irregular axis is a table.
- `result_from_value` returns it for any 1-D, non-labels output, from a step and from
  the console's `publish`. A 0-D output gets its own error (print it, or make a
  one-element array); a 1-D output from a 3-D input still has to name its axis, and
  the error now shows the call (`axes=["Frame"]`). No axis is guessed from the length.
- The x axis carries a unit only when it has one: a calibrated spatial axis the
  result's pixel unit, a time axis seconds (as the OME writer assumes), anything else
  none. A result has one `scale_unit` for all its axes, so printing it unconditionally
  would caption a frame axis in micrometres.
- Saves: CSV (axis column, value column, and the provenance companion as for FRC),
  HDF5, Zarr. TIFF is refused up front by `supported_formats`, not by a writer error.
- Curves are not offered to any processor, including the Python step: the kind matrix
  pins that no processor accepts a `curve`, and loosening a pinned invariant for a
  convenience was not worth it. A workflow that feeds one into a later step passes
  validation (a script's output kind is not known statically) and stops at run time
  with the framework's "does not accept result ... kind 'curve'". The console can read
  a selected curve.
- Recipes: `best_focus` gained a second port, `sharpness`, and `signal_trace` returns
  two curves. The workflow generator needs to be told which ports are curves
  (`CURVE_PORTS`) to save them as CSV; a test fails if that list and what the scripts
  produce disagree.
- Found by running it in a real window: `graphPanel` is off by default, so the Graph
  dock did not exist and "reveal the Graph dock for curves" revealed nothing (FRC
  curves included); the controller now asks the view to open the panel, as a pushed
  plot does. And a Graph opened after startup was not wired to the Results dock, so
  its Push to table did nothing; `ensureRuntimeAnalysisWidget` now wires it like the
  other result-pushing panels. Both are small, pre-existing, and not specific to the
  Python step; the curve is what made them visible.
