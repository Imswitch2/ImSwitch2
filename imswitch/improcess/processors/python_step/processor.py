"""The Python step: code and output names as a processor's parameters.

Because the code is an ordinary parameter, everything a processor gets comes
with it unchanged: the run path, the provenance graph and replay, batch runs,
the CLI and the workflow editor. What the code sees and how its outputs are
collected is in :mod:`.context`; this module is the processor and its panel.
"""

from typing import Callable

from qtpy import QtGui, QtWidgets

from imswitch.imcommon.view.guitools import PythonCodeEditor
from imswitch.improcess.model import snippets
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import OutputSpec, Processor, ProcessorOutput
from imswitch.improcess.processors.python_step.context import (
    DEFAULT_CODE,
    DEFAULT_PORTS,
    parse_ports,
    run_script,
)

#: How much of what the code printed is kept on each output's metadata.
STDOUT_LIMIT = 4000

_CODE_HELP = (
    "Python run over the input. Names: np, data (the first input as an array), "
    "inputs (every input's array), axes / scales / unit (the first input's), "
    "axis('Z') (an axis index by label), results (the input results), "
    "make_result(array, axes=[...]) and make_labels(array) for an output whose "
    "dimensions differ from the input's. Set outputs = {port: array} with one "
    "entry for each output port, or out = array for a single port. print() "
    "output is shown below the editor."
)
_PORTS_HELP = "Comma-separated names the code must set in `outputs`"


class PythonStepProcessor(Processor):
    """Run a few lines of Python over one or several results."""

    name = "Python step"
    id = "python"
    category = "Scripting"
    kinds = ("image", "labels", "composite")
    min_inputs = 1
    max_inputs = None
    preserves_grid = None
    accepts_roi = False
    params_version = 1

    @classmethod
    def default_params(cls) -> dict:
        return {"code": DEFAULT_CODE, "ports": DEFAULT_PORTS}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("code", "code", DEFAULT_CODE, label="Code", help=_CODE_HELP),
            ParamField("ports", "text", DEFAULT_PORTS, label="Output ports", help=_PORTS_HELP),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: True

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        """The ports named in ``ports``; ``validate`` checks references to them.

        Unparseable names declare none, so a reference to any port fails the
        workflow's own validation, and ``apply`` raises the real error.
        """
        try:
            return OutputSpec(ports=parse_ports((params or {}).get("ports", DEFAULT_PORTS)))
        except ValueError:
            return OutputSpec(ports=(), pattern=None)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        editor = PythonCodeEditor()
        editor.setText(DEFAULT_CODE)
        editor.setMinimumHeight(160)
        editor.setToolTip(_CODE_HELP)

        ports = QtWidgets.QLineEdit(DEFAULT_PORTS)
        ports.setToolTip(_PORTS_HELP)

        output = QtWidgets.QPlainTextEdit()
        output.setReadOnly(True)
        output.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        output.setMaximumHeight(110)
        output.setPlaceholderText("What the code prints, and any error, appears here.")

        snippet_row = QtWidgets.QHBoxLayout()
        load_button = QtWidgets.QPushButton("Load snippet…")
        load_button.setToolTip("Replace the code and ports with a saved snippet")
        save_button = QtWidgets.QPushButton("Save as snippet…")
        save_button.setToolTip("Keep this code and its ports in the snippet folder")
        snippet_row.addWidget(QtWidgets.QLabel("Code"))
        snippet_row.addStretch(1)
        snippet_row.addWidget(load_button)
        snippet_row.addWidget(save_button)
        layout.addLayout(snippet_row)
        layout.addWidget(editor, 1)
        form = QtWidgets.QFormLayout()
        form.addRow("Output ports", ports)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel("Output"))
        layout.addWidget(output)

        def get_values():
            # QScintilla inserts the platform's line ending (CRLF on Windows);
            # the recorded code is the same text whichever machine typed it.
            code = editor.text().replace("\r\n", "\n").replace("\r", "\n")
            return {"code": code, "ports": ports.text()}

        def set_values(values):
            """Fill the editor and the ports line; a key not given is left as it is."""
            if "code" in values:
                editor.setText(str(values["code"]))
            if "ports" in values:
                ports.setText(str(values["ports"]))

        streamed = []

        def before_run():
            """A run is starting: clear the pane for what it prints."""
            streamed.clear()
            output.clear()

        def output_appended(text):
            """The running code printed ``text``: show it now."""
            streamed.append(text)
            output.moveCursor(QtGui.QTextCursor.End)
            output.insertPlainText(text)
            output.moveCursor(QtGui.QTextCursor.End)

        def after_run(results, failures):
            """Show what the last run printed and why it failed, if it did.

            What was streamed while it ran stays (a failure or a cancellation
            adds its message below it); a run that was not streamed, inline or
            headless, takes what it printed from the results' metadata.
            """
            printed = "".join(streamed)
            if not printed:
                for result in results:
                    text = ((getattr(result, "metadata", None) or {}).get("python_step") or {}).get("stdout")
                    # Every output of one run carries the same text: show it once.
                    if text and text != printed:
                        printed = text
            shown = [printed.rstrip("\n")] if printed else []
            shown.extend(str(message).rstrip("\n") for _input, message in failures)
            output.setPlainText("\n".join(shown))
            streamed.clear()

        def say(text):
            output.setPlainText(text)

        def load_snippet_dialog():
            names = snippets.list_snippets()
            if not names:
                say(f"No snippets yet. 'Save as snippet…' keeps the code in {snippets.snippets_directory(create=False)}.")
                return
            name, accepted = QtWidgets.QInputDialog.getItem(
                widget, "Load snippet", "Snippet:", names, 0, False,
            )
            if not accepted or not name:
                return
            try:
                code, port_names = snippets.load_snippet(name)
            except (OSError, ValueError) as exc:
                say(f"Could not load snippet {name!r}: {exc}")
                return
            editor.setText(code)
            ports.setText(port_names)
            say(f"Loaded snippet {name!r}.")

        def save_snippet_dialog():
            name, accepted = QtWidgets.QInputDialog.getText(widget, "Save snippet", "Snippet name:")
            name = str(name or "").strip()
            if not accepted or not name:
                return
            code = get_values()["code"]
            try:
                try:
                    path = snippets.save_snippet(name, code, ports.text(), overwrite=False)
                except FileExistsError:
                    answer = QtWidgets.QMessageBox.question(
                        widget, "Replace snippet?", f"A snippet called {name!r} already exists. Replace it?",
                    )
                    if answer != QtWidgets.QMessageBox.Yes:
                        return
                    path = snippets.save_snippet(name, code, ports.text())
            except (OSError, ValueError) as exc:
                say(f"Could not save snippet {name!r}: {exc}")
                return
            say(f"Saved snippet {path.stem!r} to {path}.")

        load_button.clicked.connect(load_snippet_dialog)
        save_button.clicked.connect(save_snippet_dialog)

        widget.get_values = get_values
        widget.set_values = set_values
        widget.before_run = before_run
        widget.output_appended = output_appended
        widget.after_run = after_run
        widget.loadButton = load_button
        widget.saveButton = save_button
        widget.codeEditor = editor
        widget.portsEdit = ports
        widget.outputPane = output
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        inputs = list(params.get("results") or [result])
        ports = parse_ports(params.get("ports", DEFAULT_PORTS))
        results, printed = run_script(params.get("code", DEFAULT_CODE), inputs, ports)
        if printed:
            for produced in results:
                produced.metadata.setdefault("python_step", {})["stdout"] = printed[:STDOUT_LIMIT]
        return ProcessorOutput(results, keys=ports)
