"""The ImProcess console: a code editor over pyqtgraph's Python console.

The console below is pyqtgraph's (as ImScripting's): one-line commands with
history, run on the GUI thread, in a namespace the controller fills
(:class:`~imswitch.improcess.processors.python_step.console.ConsoleSession`).
The editor above it is for code of more than a line: **Run** runs the selected
lines, or all of it, in that same namespace, with what it prints and any error
shown in the console. **Send to Python step** hands the editor's text to the
Python step, which is the recorded, replayable form of it.
"""

from __future__ import annotations

from pyqtgraph.console import ConsoleWidget
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.model import routeThisThreadsOutputTo
from imswitch.imcommon.view.guitools import PythonCodeEditor
from imswitch.improcess.processors.python_step.context import ScriptError

#: The compile filename of the editor's code, so its error lines say where they are.
EDITOR_FILENAME = "<console editor>"

STARTER_TEXT = '''\
# Runs in the console below (Ctrl+Enter). data is the selected result's array;
# publish(array) adds a result to the list. "Send to Python step" records the
# code as a step: there, outputs = {...} replaces publish(...).
'''


class _ConsoleSink:
    """Where the editor code's ``print`` goes: the console's output pane."""

    def __init__(self, repl):
        self._repl = repl
        self.wrote = False
        self.endsWithNewline = True

    def write(self, text):
        if text:
            self.wrote = True
            self.endsWithNewline = text.endswith("\n")
            self._repl.write(text)
        return len(text)

    def flush(self):
        pass


class PythonConsoleWidget(QtWidgets.QWidget):
    sigSendToPythonStep = QtCore.Signal(str)   # (the editor's code)
    sigCommandStarting = QtCore.Signal()
    """About to run code (a console line or the editor's): the controller rebinds
    ``data`` first if the selection moved."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editor = PythonCodeEditor()
        self.editor.setText(STARTER_TEXT)
        self.console = ConsoleWidget(namespace={})
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.console.output.document().setDefaultFont(font)
        self.console.repl.sigCommandEntered.connect(lambda *_args: self.sigCommandStarting.emit())

        self.runButton = QtWidgets.QPushButton("Run")
        self.runButton.setToolTip(
            "Run the selected lines, or all the code, in the console below (Ctrl+Enter)"
        )
        self.sendButton = QtWidgets.QPushButton("Send to Python step")
        self.sendButton.setToolTip(
            "Put this code in a Python step, which records it with its results and can replay it"
        )
        self.runButton.clicked.connect(self.runEditor)
        self.sendButton.clicked.connect(self.sendToPythonStep)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(keys), self.editor)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.runEditor)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(QtWidgets.QLabel("Code"))
        buttons.addStretch(1)
        buttons.addWidget(self.runButton)
        buttons.addWidget(self.sendButton)
        top = QtWidgets.QWidget()
        topLayout = QtWidgets.QVBoxLayout(top)
        topLayout.setContentsMargins(0, 0, 0, 0)
        topLayout.addLayout(buttons)
        topLayout.addWidget(self.editor, 1)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.console)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

    def namespace(self) -> dict:
        """The dict the console's commands (and the editor's code) run in."""
        return self.console.localNamespace

    # -- running the editor's code ---------------------------------------------

    def runEditor(self) -> bool:
        """Run the editor's selection, or all of it; returns whether it finished."""
        selected = self.editor.selectedText()
        return self.runCode(selected if selected.strip() else self.editor.text())

    def runCode(self, code: str) -> bool:
        """Run ``code`` in the console's namespace, showing its output and any error.

        On the GUI thread, like every console command: a long script freezes the
        window until it returns. Its ``print`` is routed to the console pane for
        this thread only.
        """
        if not code.strip():
            return True
        repl = self.console.repl
        lines = len(code.strip().splitlines())
        repl.write(f"# editor: {lines} line{'s' if lines != 1 else ''}\n", "command")
        self.sigCommandStarting.emit()
        sink = _ConsoleSink(repl)
        finished = False
        try:
            compiled = compile(code, EDITOR_FILENAME, "exec")
            with routeThisThreadsOutputTo(sink):
                exec(compiled, self.namespace())  # noqa: S102 - the feature: code runs as typed
            finished = True
        except (Exception, SystemExit) as exc:
            if not sink.endsWithNewline:
                repl.write("\n")
            repl.write(ScriptError.from_exception(exc, code, EDITOR_FILENAME).traceback_text + "\n")
        if finished and sink.wrote and not sink.endsWithNewline:
            repl.write("\n")
        return finished

    def sendToPythonStep(self) -> bool:
        """Offer the editor's code to the Python step; returns whether there was any."""
        code = self.editor.text()
        if not code.strip():
            return False
        self.sigSendToPythonStep.emit(code)
        return True


__all__ = ["EDITOR_FILENAME", "PythonConsoleWidget", "STARTER_TEXT"]
