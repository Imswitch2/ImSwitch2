File: imswitch/improcess/processors/python_step/context.py (new)
File: imswitch/improcess/processors/python_step/processor.py (new)
File: imswitch/improcess/processors/python_step/__init__.py (new)
File: imswitch/improcess/processors/__init__.py
File: imswitch/improcess/model/param_spec.py
File: imswitch/improcess/view/workfloweditor/paramform.py
File: imswitch/improcess/controller/ResultProcessorController.py
File: imswitch/improcess/workflows/steps.py
File: imswitch/imcommon/view/guitools/CodeEditor.py (new)
File: imswitch/imscripting/view/EditorView.py
File: imswitch/improcess/model/snippets.py (new)
File: imswitch/improcess/_test/test_python_step.py (new)
File: examples/improcess_workflows/python_step_interleave.yaml (new)
File: docs/improcess.rst, docs/improcess-workflows.rst, docs/changelog.rst

Task summary
Add a **Python step** to ImProcess: a built-in processor whose parameters
are a few lines of Python (`code`) and the names of the outputs it
produces (`ports`), so a one-off transformation — three slices at a time
alternating between two outputs, say — is written in the panel or in a
workflow file instead of as a drop-in plugin. Because the code is an
ordinary parameter, the existing run path, provenance graph, replay,
batch runs, CLI and workflow editor carry it unchanged; a prototype of
exactly this ran end to end with no framework change (see the design,
`docs/design/plans/improcess-python-step.md`, §Summary). This brief is
Phases A and B of that design. Phase C (a console over the live results)
and Phase D (processor runs on a worker thread) are **not** part of it.

Branch: create `feat/improcess-python-step` from
`origin/docs/improcess-workflow-editor-plan` (the workflow editor branch),
not from `main`.

Todo list
- `code` field type in the parameter spec and the workflow editor's form
- Qt-free script context: namespace, output rules, error reporting
- `PythonStepProcessor` (id `python`) with panel widget, registered as a built-in
- Optional `after_run` hook on parameter widgets, so the panel shows stdout and tracebacks
- Block-style YAML for multi-line parameters; the interleave example file
- Shared QScintilla code editor (moved from ImScripting) with a plain fallback
- Snippet folder with load / save in the panel
- Optional one-time notice before running a workflow file that contains Python steps
- Docs, changelog, plan status; tests at every step

Do NOTs
- Do NOT change the workflow step dataclasses or the file schema in
  `imswitch/improcess/workflows/steps.py` (only `to_yaml`'s dumper style).
- Do NOT sandbox or restrict the code (no AST filtering, no import
  blocking): it runs like a drop-in plugin or an ImScripting script.
- Do NOT run the script on a new thread or add cancellation; processors
  run where they run today (Phase D is separate).
- Do NOT add the console (Phase C).
- Do NOT touch ImControl, hardware managers, or anything under
  `imswitch/imcontrol/`.
- Do NOT make `code` nullable, and do NOT store the code anywhere but in
  the step's `params` (no side files, no hashes standing in for it).
- Do NOT expose Qt objects, the view or controllers in the script namespace.
- Do NOT rename or renumber existing processors; `python` is a new id.
- Do NOT merge, open a pull request, or push to `main`; push the feature
  branch and stop.

Implementation
Read first: `docs/design/plans/improcess-python-step.md` (the design and
the namespace contract), the "What a plugin gets for free, and what it
must declare" section of `docs/improcess.rst`,
`imswitch/improcess/processors/base.py` (`Processor`, `OutputSpec`,
`ProcessorOutput`, `param_spec`), `processors/stack_split/processor.py`
(a multi-port processor), `processors/run.py` (how apply is called: a
multi-input processor gets its inputs as `params["results"]`),
`model/param_spec.py`, `_test/test_plugin_param_contract.py`,
`_test/test_param_spec_matches_widgets.py`, `_test/test_workflows.py`.
Every processor is held to those two contract tests automatically once it
is in the built-in table, so run them after step 3.

Set-up. `git fetch origin && git checkout -b feat/improcess-python-step
origin/docs/improcess-workflow-editor-plan`; `pip install -e .
-r requirements-dev.txt`; baseline `QT_QPA_PLATFORM=offscreen python -m
pytest imswitch/improcess/_test -q` must pass before you change anything
(3145 passed, 27 skipped when this brief was written).

1. The `code` field type (`imswitch/improcess/model/param_spec.py`,
   `imswitch/improcess/view/workfloweditor/paramform.py`).
   - Add `"code"` to `FIELD_TYPES`; in `ParamField.problem_with` treat it
     like `text` (a `str`, never `None` unless nullable). Extend the
     `ParamField` docstring: a `code` field is multi-line text a form shows
     in a code editor.
   - `ParamForm._widget_kind`: `code` → pyqtgraph's `"text"` type
     (multi-line); `_to_widget` / `_from_widget` handle it exactly like
     `text` (the value is the string as typed, no stripping of inner
     whitespace, only the trailing newline convention: keep what the user
     typed).
   - Tests: `_test/test_param_spec.py` — a `code` field accepts a
     multi-line `str`, rejects a number, round-trips through `to_dict`;
     `_test/test_workflow_editor_gui.py` — a `ParamForm` built from a
     synthetic `PluginEntry` with a `code` field shows it, `set_value`
     with a three-line string reports the same string back through
     `values()` and `sigValueChanged`.
   - `docs/improcess.rst`, the `param_spec()` entry: add `code` to the
     list of types.

2. The script context (`processors/python_step/context.py`, Qt-free —
   import nothing from `qtpy`).
   - Constants `DEFAULT_PORTS = "out"` and `DEFAULT_CODE`, a short
     commented template ending in `outputs = {"out": data}`.
   - `parse_ports(text) -> tuple[str, ...]`: split on commas, strip, drop
     empties; empty text means `("out",)`; each name must match
     `^[A-Za-z0-9_\-]+$` (the rule step references use); a duplicate or a
     bad name raises `ValueError` naming it.
   - `class ScriptError(RuntimeError)` with `line: int | None` and
     `traceback_text: str`; `str()` is one line, `line 3: NameError: name
     'x' is not defined`. Build it from the exception's traceback keeping
     only frames whose filename is the compile filename (below); a
     `SyntaxError` takes its own line number.
   - `ScriptOutput` (frozen dataclass: `array`, `axes`, `scales`, `name`,
     `kind`) and the helpers `make_result(array, *, axes=None,
     scales=None, name=None)` and `make_labels(...)` (kind `labels`).
   - `build_namespace(inputs) -> dict` with exactly: `np`; `data` (the
     first input's array via `np.asarray`, which materialises a lazy
     source); `inputs` (every input's array, in order); `axes`, `scales`,
     `unit` of the first input (use `axis_labels_for_result` and
     `axis_scales_for_result` from `processors/_axis_split.py` and
     `getattr(result, "scale_unit", "px")`); `axis(label_or_index)`
     returning an index, with a `ValueError` listing the labels for an
     unknown one; `results` (the input `ProcessingResult` objects);
     `make_result`, `make_labels`; `outputs = None`; `out = None`.
   - `run_script(code, inputs, ports, *, step_name="python") ->
     tuple[list[ProcessingResult], str]`: compile with
     `compile(code, "<python step>", "exec")`, `exec` in a fresh
     namespace, capture stdout and stderr with `contextlib.redirect_stdout`
     / `redirect_stderr` into one `io.StringIO` (returned as the second
     item). Then collect: `outputs` when set (must be a dict; when a bare
     array is given and `ports == ("out",)`, accept it as `{"out": array}`),
     else `out` (single port only), else `ScriptError("the code set neither
     'outputs' nor 'out'")`. Every declared port must be present and every
     produced key must be declared; say which are missing and which are
     extra, with the declared list. For each port in declared order: a
     `ScriptOutput` or anything `np.asarray` accepts; an array with the
     same number of dimensions as the input inherits its axes, scales and
     unit; any other dimensionality must come through `make_result(...,
     axes=[...])` or the error says so, naming the port and both
     dimensionalities. Build `ArrayProcessingResult(name, data,
     axis_labels, display_levels=finite_range(data), axis_scales=...,
     scale_unit=..., metadata={"operation": "python", "port": port})`
     (`finite_range` is in `model/contrast.py`); `make_labels` builds the
     labels result class used by the segmentation processor
     (`processors/segmentation/result.py`, `SegmentationResult`, kind
     `labels`) — read its constructor. Default name
     `f"{inputs[0].name} ({port})"`.
   - Tests (`_test/test_python_step.py`, Qt-free part): `parse_ports`
     rules; namespace keys; bare `out`; dict outputs; missing and extra
     ports; dimensionality mismatch refused, accepted through
     `make_result`; `make_labels`; stdout captured; a `NameError` reports
     line 2; a `SyntaxError` reports its line; and the interleave example
     — a `(12, 4, 4)` array of slice indices, `group = (np.arange(12) //
     3) % 2`, port `a` holds slices 0,1,2,6,7,8 and `b` the rest.

3. The processor (`processors/python_step/processor.py`, `__init__.py`,
   `processors/__init__.py`).
   - `class PythonStepProcessor(Processor)`: `name = "Python step"`,
     `id = "python"`, `category = "Scripting"`, `kinds = ("image",
     "labels", "composite")`, `min_inputs = 1`, `max_inputs = None`,
     `preserves_grid = None`, `accepts_roi = False`, `params_version = 1`.
   - `default_params()` → `{"code": DEFAULT_CODE, "ports": DEFAULT_PORTS}`;
     `param_spec()` → a `code` field (label "Code", help: the namespace in
     one paragraph) and a `text` field `ports` (label "Output ports", help
     "Comma-separated names the code must set in `outputs`").
   - `output_spec(params)` → `OutputSpec(ports=parse_ports(...))`; when
     `parse_ports` raises, return `OutputSpec(ports=(), pattern=None)` so
     that validation of a reference fails with the runtime's own message
     and `apply` raises the real error.
   - `applies_to` → `lambda result: True`.
   - `apply(result, params)`: `inputs = list(params.get("results") or
     [result])`; `run_script`; if stdout is non-empty, put it (cut to
     4000 characters) under `metadata["python_step"]["stdout"]` of every
     output; return `ProcessorOutput(results, keys=ports)`.
   - `make_param_widget(parent)`: a `QPlainTextEdit` for the code
     (monospace font, four-space tab stop, `setPlainText(DEFAULT_CODE)`),
     a `QLineEdit` for the ports (`DEFAULT_PORTS`), a read-only
     `QPlainTextEdit` "Output" pane below, and `get_values()` returning
     `{"code": editor.toPlainText(), "ports": ports.text()}`. Add an
     `after_run(results, failures)` method that writes stdout (from the
     results' metadata) and the failure message into the pane (step 4
     calls it). Step 6 swaps the editor for the shared one.
   - Register: import in `processors/__init__.py`, add `'python':
     PythonStepProcessor` to `_AVAILABLE_PROCESSOR_CLASSES`, export in
     `__all__`. The Tools toolbar lists every built-in processor
     automatically (`model/runtime_tools.runtime_analysis_tool_specs`),
     so no toolbar change is needed; the panel is built on demand and its
     processor registered by `register_processor_by_id`.
   - Tests: the two contract suites now cover it — run
     `_test/test_plugin_param_contract.py` and
     `_test/test_param_spec_matches_widgets.py` (the probe reads the ports
     line edit as a `text` field; the code editor is not probed). In
     `_test/test_python_step.py` add the workflow part, modelled on
     `_test/test_workflows.py`: with `bootstrap_registry(user_plugins=
     False)` and a synthetic HDF5 recording, a workflow `Source → view-only
     → Process("split", "python", {code, ports: "a, b"}) → Process("blur",
     "filter", inputs=["split.a"]) → Save(input="split.b")` validates, runs,
     gives the expected slices on `split.a` / `split.b`, writes the file;
     `validate` refuses a reference to `split.c`; the output node's
     `params["code"]` equals the code and `replayable` is true;
     `workflow_from_provenance` gives the same code back; a script that
     raises makes `run` raise `RunError` whose message names the step and
     the script line; a two-input step (`inputs=["rec", "rec"]`) sees
     `len(inputs) == 2`.

4. The panel hook (`controller/ResultProcessorController.py`,
   `processors/base.py`).
   - In `runProcessor`, right after `run_processor(...)`, call
     `hook = getattr(self._widget.paramWidget, "after_run", None)` and, if
     callable, `hook(results, failures)` inside a try/except that logs
     (a broken widget must not stop publishing).
   - Document the hook in one sentence in `Processor.make_param_widget`'s
     docstring and in the drop-in plugin section of `docs/improcess.rst`
     next to `setResult`.
   - Test (offscreen, `_test/test_python_step.py`): build
     `ResultProcessorWidget(PythonStepProcessor())`, drive `runProcessor`
     the way `_test/test_projection_widget_produces_results.py` drives a
     panel, and check the pane shows the traceback line after a failing
     script and the printed text after a good one.

5. Readable files and the example (`workflows/steps.py`,
   `examples/improcess_workflows/`).
   - `Workflow.to_yaml`: dump with a `yaml.SafeDumper` subclass whose
     `str` representer uses block style (`|`) for values containing a
     newline and the default style otherwise; `from_yaml` needs no change.
     Test: a workflow with a three-line `code` dumps with `code: |` and
     loads back equal (`_test/test_workflows.py`).
   - Add `examples/improcess_workflows/python_step_interleave.yaml`: the
     example from the design (`ax = 0`, three slices at a time to `a`
     and `b`, a `filter` on `split.a`, saves of both), runnable with
     `--input` on the synthetic recording `_synthetic.py` writes; a row in
     `examples/improcess_workflows/README.md`. Note that
     `_test/test_workflow_editor_model.py` loads, validates and round-trips
     every example automatically: it must report no issues.

6. The shared code editor (`imcommon/view/guitools/CodeEditor.py`,
   `imscripting/view/EditorView.py`).
   - Move the `Scintilla` class (`QsciScintilla` with `QsciLexerPython`,
     `EditorView.py:163-184`) to `imcommon/view/guitools/CodeEditor.py` as
     `PythonCodeEditor`, keeping its behaviour; add a fallback class on
     `QPlainTextEdit` (monospace, four-space tabs) used when `from PyQt5
     import Qsci` fails, both exposing `text()` and `setText()`. Export it
     from `imcommon/view/guitools/__init__.py` like `FolderPathEdit`.
   - `EditorView.py` imports it from there and keeps the name `Scintilla`
     as an alias so nothing else changes. Run
     `imswitch/imscripting/_test` afterwards.
   - The Python step widget uses `PythonCodeEditor`; `get_values` reads
     `text()`. The contract test must still pass.
   - Test: importing `CodeEditor` with `Qsci` made unimportable
     (monkeypatch `sys.modules["PyQt5.Qsci"] = None`) yields the fallback,
     and `setText`/`text` round-trip on it.

7. Snippets (`model/snippets.py`, the panel).
   - `snippets_directory(*, create=True)` → `UserFileDirs.Root/
     improcess_snippets` (copy the pattern of
     `model/workfloweditor/folders.py`); `list_snippets()` (stems of
     `*.py`, sorted); `load_snippet(name) -> (code, ports)`, where a first
     line `# ports: a, b` sets the ports and is stripped from the code;
     `save_snippet(name, code, ports)` writes that header and the code.
   - Panel buttons "Load snippet…" (`QInputDialog.getItem` over
     `list_snippets()`) and "Save as snippet…" (`QInputDialog.getText`),
     filling / reading the editor and the ports line.
   - Tests for the model functions with the directory monkeypatched to a
     temporary folder.

8. Optional: notice before running a file's Python code
   (`controller/WorkflowController.py`).
   - In `_runBatch`, when `path` is given (the workflow came from a file)
     and any `Process` step has `processor == "python"`, ask once with
     `guitools.askYesNoQuestion(self._mainView, "Run Python code?", ...)`
     naming the step ids, and stop when refused; put the check in a method
     `_confirmPythonSteps(workflow, path) -> bool` that returns True when
     `self._mainView` is not a `QWidget` (tests use a `SimpleNamespace`).
   - Test with `askYesNoQuestion` monkeypatched to return False: the run
     does not start and the status says why.

9. Docs, changelog, plan.
   - `docs/improcess.rst`: a "Python step" section (near the analysis
     panels): what the panel shows, the namespace as a table, the output
     rules, errors and stdout, snippets, the trust note ("runs like a
     drop-in plugin; a workflow file can carry code"), and the interleave
     example.
   - `docs/improcess-workflows.rst`: a short "Python steps" subsection
     under "Anatomy of a workflow" with the YAML example and a pointer to
     the ImProcess page; mention block style.
   - `docs/changelog.rst`: one entry under *Unreleased → New Features*.
   - `docs/design/plans/improcess-python-step.md`: set the status line to
     what was implemented; `docs/design/plans/README.md`: the index row.

Sanity checks
- `ruff check imswitch/improcess imswitch/imcommon imswitch/imscripting`
- `git diff --check`
- `QT_QPA_PLATFORM=offscreen python -m pytest imswitch/improcess/_test/test_python_step.py imswitch/improcess/_test/test_plugin_param_contract.py imswitch/improcess/_test/test_param_spec_matches_widgets.py imswitch/improcess/_test/test_workflows.py imswitch/improcess/_test/test_workflow_editor_model.py imswitch/improcess/_test/test_workflow_editor_gui.py -q`
- `QT_QPA_PLATFORM=offscreen python -m pytest imswitch/improcess/_test imswitch/imscripting/_test -q` (all green before the final push)
- `python -m imswitch.improcess.workflows validate examples/improcess_workflows/python_step_interleave.yaml`
- `python -m imswitch.improcess.workflows list --json | python -c "import json,sys; d=json.load(sys.stdin); print(d['processors']['python']['fields'])"` prints the `code` and `ports` fields

Commit instructions
- One commit per numbered step (steps 1–3 may be squashed into one if
  they land together), each leaving the suite green; message
  `improcess: <short summary>`.
- Push `feat/improcess-python-step` to origin; do not open a pull request
  and do not merge. End with a summary that a reviewer can read on its
  own: what was built, what each test proves, and what was left out
  (Phases C and D, and step 8 if skipped).
