"""The right-hand panel: the selected step, in plain words, editable.

A header in the step's colour (plugin name, editable id, what it does and
what it takes and makes), then what it reads, its parameters (the shared
:class:`ParamForm`), and, for a Python step, the code editor with its ports.
Below sits the output of the last run (Phase 2: filled by the window).

Every widget edit goes through the window's ``editor`` callable, which makes
it one undoable document edit and refreshes the views; the inspector is only
rebuilt when the edit changed its own shape (another plugin, other inputs).

With nothing selected it shows the workflow itself: its name, description
and **controls** (schema 2), each with its field, its targets (click one to
select its step), Reset, Rename… and Stop exposing. A parameter row's context
menu exposes it as a control or adds it to one; a bypassed step says what it
passes through, with *Stop bypassing*.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR
from imswitch.improcess.model.workfloweditor.nodes import code_name, kinds_of, save_file_name, title_of
from imswitch.improcess.workflows.sources import SOURCE_KINDS
from imswitch.improcess.workflows.steps import Consolidate, Process, Reconstruct, Save, Source

from .canvas import style as st
from .outputpanel import OutputPanel
from .paramform import ParamForm

_HELP = (
    "<h3>Building a workflow</h3>"
    "<p><b>Add</b> a step by dragging it from the list on the left onto the canvas, or double-click "
    "the canvas (or press Tab) and search.</p>"
    "<p><b>Connect</b> by dragging from a box's right side (what it makes) to another box's left side "
    "(what it reads). Drop a cable in empty space to pick a step that takes it.</p>"
    "<p><b>Colours</b> say what flows: grey recordings, blue images, orange labels, green curves, "
    "purple tables.</p>"
    "<p><b>Select</b> a box to edit it here. Del deletes, Ctrl+Z undoes.</p>"
)


def _combo() -> QtWidgets.QComboBox:
    """A combo box that does not widen the inspector to its longest entry."""
    combo = QtWidgets.QComboBox()
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(10)
    return combo


def _muted(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setEnabled(False)
    return label


def _section(title: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(title.upper())
    label.setObjectName("inspectorSection")
    label.setStyleSheet("font-weight: bold; letter-spacing: 1px; padding-top: 8px;")
    label.setEnabled(False)
    return label


def python_names_text(step) -> str:
    """The names a Python step's code starts with, its inputs by what they read."""
    refs = [str(ref) for ref in step.inputs]
    if refs:
        named = [f"data = inputs[0] (reads {refs[0]})"]
        named += [f"inputs[{i}] (reads {ref})" for i, ref in enumerate(refs[1:], start=1)]
        inputs = "Inputs: " + ", ".join(named) + "; results[i] is input i's result."
    else:
        inputs = "Inputs: none connected yet; the first becomes data (inputs[0]), the next inputs[1]."
    return (f"{inputs} Also np, axes / scales / unit (the first input's), axis('Z'), make_result, "
            "make_labels. Set outputs = {port: array} or out = array.")


class Inspector(QtWidgets.QScrollArea):
    """See the module docstring."""

    sigMessage = QtCore.Signal(str)
    sigRenamed = QtCore.Signal(str, str)
    #: A control's target was clicked in the Controls panel: select that step.
    sigSelectStep = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(340)
        self._document = None
        self._editor = None
        self._step_id: str | None = None
        self._param_form: ParamForm | None = None
        #: The Controls panel's forms, by control name (nothing selected).
        self.control_forms: dict[str, ParamForm] = {}
        self.control_buttons: dict[str, dict[str, QtWidgets.QPushButton]] = {}
        self._python = None
        #: The code box holds typing the document has not seen yet.
        self._code_dirty = False
        #: The fields shown write nothing (they are being replaced): set while
        #: a body is retired or after discard_pending, cleared by show_step.
        self._silent = False
        self._code_timer = QtCore.QTimer(self)
        self._code_timer.setSingleShot(True)
        self._code_timer.setInterval(500)
        self._code_timer.timeout.connect(self._commit_code)
        #: What the selected step made in this session (filled by the window).
        self.output = OutputPanel()
        self.output_area = QtWidgets.QWidget()
        self.output_area.setLayout(QtWidgets.QVBoxLayout())
        self.output_area.layout().setContentsMargins(0, 0, 0, 0)
        self.output_area.layout().addWidget(self.output)
        #: What a source (or a step started from a result) runs on in this
        #: session, and the buttons that change it (filled by the window).
        self.binding_area = QtWidgets.QWidget()
        self.binding_area.setLayout(QtWidgets.QVBoxLayout())
        self.binding_area.layout().setContentsMargins(0, 0, 0, 0)
        self.show_step(None)

    def set_document(self, document) -> None:
        if document is not self._document:
            self.discard_pending()        # typing belongs to the document it was typed into
        self._document = document
        self.show_step(None)

    def set_editor(self, editor) -> None:
        """``editor(label, action, structural=False, merge=None) -> bool`` makes one edit."""
        self._editor = editor

    @property
    def step_id(self) -> str | None:
        return self._step_id

    @property
    def param_form(self) -> ParamForm | None:
        return self._param_form

    @property
    def python_editor(self):
        return self._python

    # -- building ----------------------------------------------------------------------

    def discard_pending(self) -> None:
        """Forget typing not yet written to the document (before an undo, a
        redo or another document, which replace what the fields show). The
        fields stay silent until ``show_step`` builds new ones: call it next."""
        self._code_timer.stop()
        self._code_dirty = False
        self._silent = True

    def show_step(self, step_id: str | None) -> None:
        if step_id != self._step_id:
            self._finish_typing()
        self._commit_code()
        self._retire_body()
        document = self._document
        self._step_id = step_id if step_id and document is not None and document.has(step_id) else None
        self._param_form = None
        self._python = None
        self.control_forms = {}
        self.control_buttons = {}
        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        if self._step_id is None:
            if document is not None and (document.steps or document.controls):
                self._build_workflow(layout)
            help_text = QtWidgets.QLabel(_HELP)
            help_text.setWordWrap(True)
            help_text.setTextFormat(QtCore.Qt.RichText)
            layout.addWidget(help_text)
        else:
            step = document.step(self._step_id)
            self._build_header(layout, step)
            if getattr(step, "bypass", False):
                self._build_bypassed(layout, step)
            form = QtWidgets.QFormLayout()
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
            layout.addLayout(form)
            if isinstance(step, Source):
                self._build_source(form, step)
            elif isinstance(step, Reconstruct):
                self._build_reconstruct(layout, form, step)
            elif isinstance(step, Consolidate):
                self._build_consolidate(layout, form, step)
            elif isinstance(step, Process):
                self._build_process(layout, form, step)
            elif isinstance(step, Save):
                self._build_save(form, step)
            if isinstance(step, (Source, Reconstruct, Process)):
                layout.insertWidget(1, self.binding_area)
            layout.addWidget(self.output_area)
        layout.addStretch(1)
        self.setWidget(body)
        self._silent = False

    def _finish_typing(self) -> None:
        """Write what was typed into the focused field to its own step before
        another is shown: a selection made by a shortcut or the program moves
        no focus, so the field would not finish by itself. A new name is the
        exception (renaming selects the renamed step); it is dropped."""
        body = self.widget()
        focused = QtWidgets.QApplication.focusWidget()
        if body is None or not isinstance(focused, QtWidgets.QLineEdit) or not body.isAncestorOf(focused):
            return
        if focused.isModified() and focused is not getattr(self, "id_edit", None):
            focused.setModified(False)
            focused.editingFinished.emit()

    def _retire_body(self) -> None:
        """Take the shown body out before the next is built.

        The output and binding areas outlive the bodies they are shown in.
        The old fields are silenced first (``_edit`` refuses while
        ``_silent``): hiding a focused line edit makes it emit
        ``editingFinished``, which would otherwise write its text to whatever
        step is shown next, or over an undo. The body may hold the widget
        whose signal brought us here, so the event loop deletes it.
        """
        self._silent = True
        old = self.takeWidget()
        if old is None:
            return
        for area in (self.output_area, self.binding_area):
            if old.isAncestorOf(area):
                area.setParent(None)
        old.hide()
        old.setParent(None)
        old.deleteLater()

    def _build_header(self, layout, step) -> None:
        document = self._document
        entry = document.entry_for(step)
        colour = st.step_colour(step.kind, entry.category if entry is not None else "")
        header = QtWidgets.QFrame()
        header.setObjectName("inspectorHeader")
        header.setStyleSheet(
            f"QFrame#inspectorHeader {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {colour.name()},"
            f" stop:1 {colour.darker(140).name()}); border-radius: 8px; }}"
            "QFrame#inspectorHeader QLabel { color: white; background: transparent; }"
            "QFrame#inspectorHeader QLineEdit { background: rgba(0,0,0,60); color: white; border: 1px solid"
            " rgba(255,255,255,70); border-radius: 4px; padding: 2px 5px; }"
        )
        box = QtWidgets.QVBoxLayout(header)
        box.setContentsMargins(12, 10, 12, 10)
        title = QtWidgets.QLabel(title_of(document, step))
        title.setStyleSheet("font-size: 12.5pt; font-weight: bold;")
        title.setWordWrap(True)
        box.addWidget(title)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("id"))
        self.id_edit = QtWidgets.QLineEdit(step.id)
        self.id_edit.setToolTip("The step's name in the file: letters, digits, '_' and '-'")
        self.id_edit.editingFinished.connect(self._rename)
        row.addWidget(self.id_edit, 1)
        box.addLayout(row)
        words = entry.description if entry is not None and entry.description else {
            "source": "A recording: a file, or the one bound when the workflow runs.",
            "save": "Writes one result to a file.",
            "consolidate": "Merges the reconstructions it reads into one result.",
        }.get(step.kind, "")
        if words:
            description = QtWidgets.QLabel(words)
            description.setWordWrap(True)
            description.setStyleSheet("font-size: 8.8pt;")
            box.addWidget(description)
        flow = self._flow_words(step, entry)
        if flow:
            line = QtWidgets.QLabel(flow)
            line.setStyleSheet("font-size: 8.5pt; color: rgba(255,255,255,200);")
            line.setWordWrap(True)
            box.addWidget(line)
        layout.addWidget(header)
        issues = document.issues_for(step.id)
        for issue in issues:
            warning = QtWidgets.QLabel(f"⚠ {issue.message}")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #e8913a;")
            layout.addWidget(warning)

    def _flow_words(self, step, entry) -> str:
        kinds = kinds_of(self._document)
        makes = kinds.get(step.id, "")
        makes_words = st.KIND_WORDS.get(makes, makes)
        if isinstance(step, Source):
            return "makes a recording"
        if isinstance(step, Save):
            return ""
        if isinstance(step, Process) and entry is not None:
            takes = ", ".join(st.KIND_WORDS.get(k, k) for k in entry.accepted_kinds) or "anything"
            return f"takes {takes} ({entry.arity} input{'s' if entry.arity != '1' else ''}) · makes {makes_words}"
        if isinstance(step, Reconstruct):
            return f"reads a recording · makes {makes_words}"
        return f"makes {makes_words}" if makes else ""

    def _plugin_combo(self, entries, current: str, *, by_category: bool = False) -> QtWidgets.QComboBox:
        combo = _combo()
        found = False
        for entry in entries:
            label = f"{entry.category} · {entry.name}" if by_category and entry.kind == PROCESSOR else entry.name
            combo.addItem(label, entry.id)
            index = combo.count() - 1
            if entry.gui_only:
                item = combo.model().item(index)
                item.setEnabled(False)
                item.setToolTip(entry.gui_only)
            if entry.id == current:
                combo.setCurrentIndex(index)
                found = True
        if not found:
            combo.addItem(f"{current} (not installed)", current)
            combo.setCurrentIndex(combo.count() - 1)
        combo.activated.connect(lambda _index, c=combo: self._set_plugin(c))
        return combo

    def _build_source(self, form, step: Source) -> None:
        spec = step.source
        sid = step.id
        path_row = QtWidgets.QWidget()
        path_layout = QtWidgets.QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QtWidgets.QLineEdit(spec.path or "")
        self.path_edit.setPlaceholderText("No file: bound when the workflow runs")
        path_edit = self.path_edit
        path_edit.editingFinished.connect(lambda: self._edit(
            "Change the recording", lambda: self._document.set_source(sid, path=path_edit.text().strip() or None),
            structural=True))
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse_source)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(browse)
        form.addRow("File", path_row)
        dataset = QtWidgets.QLineEdit(spec.dataset or "")
        dataset.setPlaceholderText("Only when the file holds several datasets")
        dataset.editingFinished.connect(lambda e=dataset: self._edit(
            "Change the dataset", lambda: self._document.set_source(sid, dataset=e.text().strip() or None)))
        form.addRow("Dataset", dataset)
        kind = QtWidgets.QComboBox()
        kind.addItems(list(SOURCE_KINDS))
        kind.setCurrentText(spec.source_kind)
        kind.activated.connect(lambda _index, c=kind: self._edit(
            "Change the source kind", lambda: self._document.set_source(sid, source_kind=c.currentText())))
        form.addRow("Kind", kind)
        if spec.fingerprint:
            form.addRow(_muted("Carries a recorded fingerprint; a replay checks the file against it."))

    def _build_reconstruct(self, layout, form, step: Reconstruct) -> None:
        catalog = self._document.catalog
        form.addRow("Reconstructor", self._plugin_combo(catalog.reconstructors if catalog else (), step.reconstructor))
        sources = [s.id for s in self._document.steps if isinstance(s, Source)]
        combo = _combo()
        combo.addItem("(not connected)", "")
        for source_id in sources:
            combo.addItem(source_id, source_id)
        current = step.inputs[0].step if step.inputs else ""
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.activated.connect(lambda _index, c=combo: self._set_inputs([c.currentData()] if c.currentData() else []))
        form.addRow("Reads", combo)
        self._add_param_form(layout, step)

    def _build_consolidate(self, layout, form, step: Consolidate) -> None:
        catalog = self._document.catalog
        form.addRow("Reconstructor", self._plugin_combo(catalog.consolidators() if catalog else (), step.reconstructor))
        chooser = QtWidgets.QListWidget()
        chosen = [ref.step for ref in step.inputs]
        for other in self._document.steps:
            if isinstance(other, Reconstruct):
                item = QtWidgets.QListWidgetItem(f"{other.id} ({other.reconstructor})")
                item.setData(QtCore.Qt.UserRole, other.id)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Checked if other.id in chosen else QtCore.Qt.Unchecked)
                chooser.addItem(item)
        chooser.setMaximumHeight(120)
        chooser.itemChanged.connect(lambda _item, c=chooser: self._set_inputs([
            c.item(i).data(QtCore.Qt.UserRole) for i in range(c.count())
            if c.item(i).checkState() == QtCore.Qt.Checked
        ]))
        form.addRow("Merges", chooser)

    def _build_process(self, layout, form, step: Process) -> None:
        catalog = self._document.catalog
        form.addRow("Processor", self._plugin_combo(catalog.processors if catalog else (), step.processor, by_category=True))
        layout.addWidget(_section("Reads"))
        layout.addWidget(self._inputs_editor(step))
        if step.processor == "python":
            self._build_python(layout, step)
        else:
            self._add_param_form(layout, step)
        restriction = step.restriction or {}
        if restriction:
            row = QtWidgets.QHBoxLayout()
            rois = restriction.get("rois") or []
            row.addWidget(QtWidgets.QLabel(
                f"Within {len(rois)} ROI(s), {restriction.get('mode', '?')}"
                + (" (by reference: cannot run)" if restriction.get("by_reference") else "")), 1)
            clear = QtWidgets.QPushButton("Clear")
            clear.clicked.connect(lambda: self._edit("Clear the ROI restriction",
                                                     lambda: self._document.set_restriction(self._step_id, None),
                                                     structural=True))
            row.addWidget(clear)
            layout.addLayout(row)

    def _inputs_editor(self, step) -> QtWidgets.QWidget:
        document = self._document
        kinds = kinds_of(document)
        options = [option for option in document.port_options(None) if option.step != step.id]
        widget = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        refs = [str(ref) for ref in step.inputs]
        entry = document.entry_for(step)
        several = entry is not None and (entry.max_inputs is None or entry.max_inputs > 1)
        python = isinstance(step, Process) and step.processor == "python"
        for index, ref in enumerate(refs):
            row = QtWidgets.QHBoxLayout()
            dot = QtWidgets.QLabel("●")
            kind = kinds.get(step.inputs[index].step, "unknown")
            dot.setStyleSheet(f"color: {st.KIND_COLOURS.get(kind, st.KIND_COLOURS['unknown'])};")
            dot.setToolTip(st.KIND_WORDS.get(kind, kind))
            row.addWidget(dot)
            if python:
                # The name the code reads this input by, beside what it reads.
                name = QtWidgets.QLabel("data" if index == 0 else f"inputs[{index}]")
                name.setObjectName("codeName")
                name.setStyleSheet("font-family: monospace;")
                # One width for all, so the inputs line up whatever their names.
                name.setMinimumWidth(name.fontMetrics().horizontalAdvance(f"inputs[{max(10, len(refs))}]"))
                name.setToolTip(f"In the code: {code_name(index)}; results[{index}] is its result "
                                "(name, axes, scales)")
                row.addWidget(name)
            combo = _combo()
            combo.setEditable(True)
            for option in options:
                combo.addItem(option.ref if not option.pattern else option.label, option.ref)
            combo.setCurrentText(ref)
            combo.setToolTip("What this input reads; a split's member can be typed (split.C2)")
            combo.activated.connect(lambda _i, c=combo, i=index: self._replace_input(i, c.currentText()))
            combo.lineEdit().editingFinished.connect(lambda c=combo, i=index: self._replace_input(i, c.currentText()))
            row.addWidget(combo, 1)
            if several and len(refs) > 1:
                up = QtWidgets.QToolButton()
                up.setText("▲")
                up.setToolTip("Earlier (inputs are used in this order)")
                up.setEnabled(index > 0)
                up.clicked.connect(lambda _c=False, i=index: self._move_input(i, -1))
                down = QtWidgets.QToolButton()
                down.setText("▼")
                down.setEnabled(index < len(refs) - 1)
                down.clicked.connect(lambda _c=False, i=index: self._move_input(i, +1))
                row.addWidget(up)
                row.addWidget(down)
            remove = QtWidgets.QToolButton()
            remove.setText("✕")
            remove.setToolTip("Disconnect this input")
            remove.clicked.connect(lambda _c=False, i=index: self._edit(
                "Disconnect", lambda: self._document.disconnect(self._step_id, i), structural=True))
            row.addWidget(remove)
            box.addLayout(row)
        if not refs:
            box.addWidget(_muted("Not connected: drag a cable onto this box, or pick an input below."))
        if not refs or several or (entry is not None and entry.max_inputs == 1):
            add = _combo()
            add.addItem("+ Read from…", "")
            for option in options:
                if not option.pattern:
                    add.addItem(option.ref, option.ref)
            add.setToolTip("Add an input (a single-input step given two runs once for each)")
            add.activated.connect(lambda _i, c=add: self._add_input(c.currentData()))
            box.addWidget(add)
        return widget

    def _build_python(self, layout, step: Process) -> None:
        from imswitch.imcommon.view.guitools import PythonCodeEditor
        from imswitch.improcess.processors.python_step.context import DEFAULT_CODE, DEFAULT_PORTS

        params = self._document.effective_params(step.id)
        layout.addWidget(_section("Code"))
        editor = PythonCodeEditor()
        editor.setText(str(params.get("code", DEFAULT_CODE)))
        editor.setMinimumHeight(220)
        self._code_dirty = False
        editor.textChanged.connect(self._code_typed)
        self._python = editor
        layout.addWidget(editor, 1)
        row = QtWidgets.QFormLayout()
        ports_edit = QtWidgets.QLineEdit(str(params.get("ports", DEFAULT_PORTS)))
        ports_edit.setToolTip("Comma-separated names the code sets in `outputs`; each is an output of the box")
        sid = step.id
        ports_edit.editingFinished.connect(lambda: self._edit(
            "Change the output ports", lambda: self._document.set_param(sid, "ports", ports_edit.text()),
            structural=True))
        self.ports_edit = ports_edit
        row.addRow("Output ports", self.ports_edit)
        layout.addLayout(row)
        buttons = QtWidgets.QHBoxLayout()
        load = QtWidgets.QPushButton("Load snippet…")
        load.setToolTip("Replace the code and ports with a saved snippet (ImSwitch ships a few recipes)")
        load.clicked.connect(self._load_snippet)
        buttons.addWidget(load)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(_muted(python_names_text(step)))

    def mark_line(self, line: int | None) -> None:
        """Mark the line a Python step failed at (``None`` clears the mark)."""
        editor = self._python
        if editor is None:
            return
        try:
            from imswitch.imcommon.view.guitools.CodeEditor import QSCI_AVAILABLE
        except ImportError:  # pragma: no cover
            QSCI_AVAILABLE = False
        if QSCI_AVAILABLE and hasattr(editor, "markerDefine"):
            from qtpy import QtGui

            marker = 8
            editor.markerDefine(editor.Background, marker)
            editor.setMarkerBackgroundColor(QtGui.QColor("#5a1f22"), marker)
            editor.markerDeleteAll(marker)
            if line:
                editor.markerAdd(int(line) - 1, marker)
                editor.ensureLineVisible(int(line) - 1)
            return
        selections = []
        if line:
            from qtpy import QtGui

            block = editor.document().findBlockByNumber(int(line) - 1)
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setBackground(QtGui.QColor("#5a1f22"))
            selection.format.setProperty(QtGui.QTextFormat.FullWidthSelection, True)
            selection.cursor = QtGui.QTextCursor(block)
            selections.append(selection)
        editor.setExtraSelections(selections)

    def _code_typed(self) -> None:
        self._code_dirty = True
        self._code_timer.start()

    def _commit_code(self) -> None:
        """Write what was typed into the code box to the document. Only typing:
        an edit made elsewhere (an undo, a loaded snippet) is never overwritten
        with the text the box still shows."""
        self._code_timer.stop()
        editor = self._python
        if not self._code_dirty:
            return
        self._code_dirty = False
        if editor is None or self._step_id is None or self._document is None or not self._document.has(self._step_id):
            return
        try:
            text = editor.text().replace("\r\n", "\n").replace("\r", "\n")
        except RuntimeError:
            return
        if text == self._document.effective_params(self._step_id).get("code"):
            return
        step_id = self._step_id
        self._edit("Edit the code", lambda: self._document.set_param(step_id, "code", text), merge=("code", step_id))

    def _load_snippet(self) -> None:
        from imswitch.improcess.model import snippets

        names = snippets.list_snippets()
        if not names:
            self.sigMessage.emit("No snippets yet.")
            return
        name, accepted = QtWidgets.QInputDialog.getItem(self, "Load snippet", "Snippet:", names, 0, False)
        if not accepted or not name:
            return
        try:
            code, ports = snippets.load_snippet(name)
        except (OSError, ValueError) as exc:
            self.sigMessage.emit(f"Could not load snippet {name!r}: {exc}")
            return
        step_id = self._step_id

        def apply():
            self._document.set_param(step_id, "code", code)
            self._document.set_param(step_id, "ports", ports)

        self._edit(f"Load snippet {name}", apply, structural=True)

    def _build_save(self, form, step: Save) -> None:
        catalog = self._document.catalog
        sid = step.id
        input_combo = _combo()
        input_combo.setEditable(True)
        for option in self._document.port_options(None):
            if option.step != step.id:
                input_combo.addItem(option.ref if not option.pattern else option.label, option.ref)
        input_combo.setCurrentText(str(step.input))
        input_combo.activated.connect(lambda _i, c=input_combo: self._set_inputs([c.currentText()]))
        input_combo.lineEdit().editingFinished.connect(lambda c=input_combo: self._set_inputs([c.currentText()]))
        form.addRow("Saves", input_combo)
        fmt = QtWidgets.QComboBox()
        fmt.addItems(list(catalog.save_formats) if catalog else [step.fmt])
        fmt.setCurrentText(step.fmt)
        fmt.activated.connect(lambda _i, c=fmt: self._edit(
            "Change the format", lambda: self._document.set_save(sid, fmt=c.currentText()), structural=True))
        form.addRow("Format", fmt)
        template = QtWidgets.QLineEdit(step.path_template)
        template.editingFinished.connect(lambda e=template: self._edit(
            "Change the file name", lambda: self._document.set_save(sid, path_template=e.text()),
            structural=True))
        form.addRow("File name", template)
        from imswitch.improcess.workflows.prepare import bypass_upstream, resolve_ref

        made_by = resolve_ref(step.input, bypass_upstream(self._document.workflow)).step
        form.addRow(_muted(f"Writes {save_file_name(step, made_by)} in the output folder."))
        placeholders = ", ".join("{%s}" % name for name in (catalog.path_placeholders if catalog else ()))
        form.addRow(_muted(f"Placeholders: {placeholders}."))

    def _add_param_form(self, layout, step) -> None:
        document = self._document
        entry = document.entry_for(step)
        layout.addWidget(_section("Parameters"))
        form = ParamForm()
        exposed = {}
        for key, name in document.exposed(step.id).items():
            reset, _problem = document.control_reset(name)
            exposed[key] = (name, reset)
        form.set_plugin(entry, step.params, exposed=exposed, actions=True)
        form.sigValueChanged.connect(self._set_param)
        form.sigValueCleared.connect(self._clear_param)
        form.sigContextAction.connect(self._param_action)
        form.setMinimumHeight(160)
        self._param_form = form
        layout.addWidget(form, 1)

    # -- bypass ------------------------------------------------------------------------

    def _build_bypassed(self, layout, step) -> None:
        from imswitch.improcess.workflows.prepare import bypass_upstream

        document = self._document
        stand_in = bypass_upstream(document.workflow).get(step.id)
        if isinstance(step, Save):
            words = "Bypassed: this save writes nothing when the workflow runs."
        elif stand_in is not None:
            through = str(stand_in)
            readers = [other.id for other in document.steps
                       if any(ref.step == step.id for ref in ([other.input] if isinstance(other, Save)
                                                              else list(getattr(other, "inputs", []) or [])))]
            who = f"<b>{readers[0]}</b> reads" if len(readers) == 1 else (
                "the steps after it read" if readers else "anything connected after it reads")
            words = f"Bypassed: {who} <b>{through}</b> through this step unchanged."
        else:
            words = "Bypassed, but it cannot pass anything on (see the warning above): the workflow will not run."
        frame = QtWidgets.QFrame()
        frame.setObjectName("bypassBanner")
        frame.setStyleSheet("QFrame#bypassBanner { background: #3a4048; border-radius: 6px; }"
                            "QFrame#bypassBanner QLabel { color: #eef1f4; }")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(10, 6, 6, 6)
        label = QtWidgets.QLabel(words)
        label.setWordWrap(True)
        label.setTextFormat(QtCore.Qt.RichText)
        row.addWidget(label, 1)
        button = QtWidgets.QPushButton("Stop bypassing")
        button.setToolTip("Run this step again (B)")
        sid = step.id
        button.clicked.connect(lambda: self._edit(f"Stop bypassing {sid}", lambda: self._document.set_bypass(sid, False),
                                                  structural=True))
        self.stop_bypass_button = button
        row.addWidget(button)
        layout.addWidget(frame)

    # -- controls ----------------------------------------------------------------------

    def _param_action(self, key: str, action: str) -> None:
        if action == "expose":
            self._ask_expose(key)
        elif action == "unexpose":
            name = self._document.exposed(self._step_id).get(key)
            if name:
                target = f"{self._step_id}.{key}"
                self._edit(f"Stop exposing {target}", lambda: self._document.remove_target(name, target),
                           structural=True)

    def compatible_controls(self, key: str) -> list[str]:
        """Controls ``step.key`` could join (a dry run of ``add_target``)."""
        import copy

        document = self._document
        target = f"{self._step_id}.{key}"
        found = []
        for control in document.controls:
            trial = type(document)(copy.deepcopy(document.workflow), catalog=document.catalog)
            try:
                trial.add_target(control.name, target)
            except Exception:  # noqa: BLE001 - it just does not fit
                continue
            found.append(control.name)
        return found

    def expose_param(self, key: str, name: str | None = None, *, join: str | None = None) -> bool:
        """Expose ``key`` of the shown step: as a new control ``name`` (default
        the key), or as another target of control ``join`` (adopting its value)."""
        target = f"{self._step_id}.{key}"
        if join:
            return self._edit(f"Add {target} to {join}", lambda: self._document.add_target(join, target),
                              structural=True)
        name = name or key
        return self._edit(f"Expose {target} as {name}", lambda: self._document.add_control(name, target),
                          structural=True)

    def _ask_expose(self, key: str) -> None:
        joinable = self.compatible_controls(key)
        if joinable:
            new_item = f"A new control named '{key}'…"
            items = [new_item] + [f"Add to control '{name}' (takes its value)" for name in joinable]
            choice, accepted = QtWidgets.QInputDialog.getItem(
                self, "Expose as control", f"Expose {self._step_id}.{key} as:", items, 0, False)
            if not accepted:
                return
            if choice != new_item:
                self.expose_param(key, join=joinable[items.index(choice) - 1])
                return
        name, accepted = QtWidgets.QInputDialog.getText(
            self, "Expose as control",
            f"Name of the control for {self._step_id}.{key}\n(what --set, a manifest column and the run dialog use):",
            text=key)
        if accepted and name.strip():
            self.expose_param(key, name.strip())

    def _build_workflow(self, layout) -> None:
        """The workflow's own panel: name, description, controls."""
        document = self._document
        layout.addWidget(_section("Workflow"))
        form = QtWidgets.QFormLayout()
        name = QtWidgets.QLineEdit(document.name)
        name.editingFinished.connect(lambda e=name: e.text().strip() != document.name and self._edit(
            "Rename the workflow", lambda: document.set_name(e.text().strip()), need_step=False))
        form.addRow("Name", name)
        description = QtWidgets.QLineEdit(document.workflow.description or "")
        description.setPlaceholderText("What it is for")
        description.editingFinished.connect(lambda e=description: e.text() != (document.workflow.description or "")
                                            and self._edit("Describe the workflow",
                                                           lambda: document.set_description(e.text()),
                                                           need_step=False))
        form.addRow("Description", description)
        layout.addLayout(form)
        layout.addWidget(_section("Controls"))
        if not document.controls:
            layout.addWidget(_muted(
                "No controls. Right-click a step's parameter and choose Expose as control… to set it by name "
                "when the workflow runs (--set, a manifest column, the run dialog)."))
            return
        for control in document.controls:
            layout.addWidget(self._control_card(control))

    def _control_card(self, control) -> QtWidgets.QWidget:
        from imswitch.improcess.model.param_spec import ParamField

        document = self._document
        name = control.name
        state = document.control_state(name)
        card = QtWidgets.QFrame()
        card.setObjectName("controlCard")
        card.setStyleSheet("QFrame#controlCard { border: 1px solid #2e3e4f; border-radius: 6px; }")
        box = QtWidgets.QVBoxLayout(card)
        box.setContentsMargins(8, 6, 8, 6)
        spec = state.spec if state is not None else None
        title = QtWidgets.QLabel(f"<b>{(spec.label if spec is not None else '') or control.label or name}</b>"
                                 f" &nbsp;<span style='font-family: monospace;'>⇄ {name}</span>")
        title.setTextFormat(QtCore.Qt.RichText)
        box.addWidget(title)
        if spec is not None:
            from imswitch.improcess.workflows.prepare import check_typed

            held = document.control_value(name)
            try:
                value = check_typed(held, spec)
            except ValueError as exc:
                # A hand-edited value of the wrong type: say so, and show the
                # field at its reset (nothing is written until it is edited).
                box.addWidget(_muted(f"Its parameters hold {held!r}, which does not fit ({exc}); "
                                     "set a value here."))
                value = spec.reset if spec.has_reset else None
            field = ParamField(name, spec.type, spec.reset if spec.has_reset else value,
                               label=spec.label or name, help=spec.help, options=spec.options, min=spec.min,
                               max=spec.max, step=spec.step, decimals=spec.decimals, suffix=spec.suffix,
                               nullable=spec.nullable)
            form = ParamForm()
            form.set_plugin(SimpleNamespace(fields=[field], extra_keys=()), {name: value} if value is not None else {})
            form.set_reset_visible(False)
            form.setFixedHeight(36)
            form.sigValueChanged.connect(lambda _key, value, n=name: self._edit(
                f"Set {n}", lambda: document.set_control_value(n, value), merge=("control", n), need_step=False))
            self.control_forms[name] = form
            box.addWidget(form)
        else:
            box.addWidget(_muted("Its targets cannot share one field: see the issues."))
        links = []
        for target in (state.targets if state is not None else []):
            why = f" ({target.reason})" if target.reason else ""
            links.append(f"<a href='{target.step}'>{target.target}</a>{why}")
        known = {t.target for t in state.targets} if state is not None else set()
        links += [f"{t} (missing)" for t in control.targets if t not in known]
        targets = QtWidgets.QLabel("Sets " + ", ".join(links))
        targets.setTextFormat(QtCore.Qt.RichText)
        targets.setWordWrap(True)
        targets.linkActivated.connect(self.sigSelectStep.emit)
        box.addWidget(targets)
        if state is not None and not state.active and state.targets:
            steps = sorted({t.step for t in state.targets})
            box.addWidget(_muted(f"No effect while {', '.join(steps)} {'is' if len(steps) == 1 else 'are'} "
                                 "bypassed; a value set here is written for when it runs again."))
        row = QtWidgets.QHBoxLayout()
        reset_value, reset_problem = document.control_reset(name)
        reset = QtWidgets.QPushButton("Reset")
        reset.setEnabled(reset_problem is None)
        reset.setToolTip(reset_problem or f"Back to the default its targets share ({reset_value!r})")
        reset.clicked.connect(lambda _c=False, n=name: self._edit(f"Reset {n}", lambda: document.reset_control(n),
                                                                  structural=True, need_step=False))
        rename = QtWidgets.QPushButton("Rename…")
        rename.clicked.connect(lambda _c=False, n=name: self._ask_rename_control(n))
        label = QtWidgets.QPushButton("Label…")
        label.setToolTip("The label and help the run dialog shows")
        label.clicked.connect(lambda _c=False, n=name: self._ask_label_control(n))
        stop = QtWidgets.QPushButton("Stop exposing")
        stop.setToolTip("Remove the control; its parameters keep their value")
        stop.clicked.connect(lambda _c=False, n=name: self._edit(f"Stop exposing {n}",
                                                                 lambda: document.remove_control(n),
                                                                 structural=True, need_step=False))
        # Two rows: the inspector is narrow, and a wider body would clip
        # everything else it shows.
        second = QtWidgets.QHBoxLayout()
        for layout_row, buttons in ((row, (reset, rename)), (second, (label, stop))):
            for button in buttons:
                layout_row.addWidget(button)
            layout_row.addStretch(1)
            box.addLayout(layout_row)
        self.control_buttons[name] = {"reset": reset, "rename": rename, "label": label, "stop": stop}
        return card

    def _ask_rename_control(self, name: str) -> None:
        new, accepted = QtWidgets.QInputDialog.getText(self, "Rename control", "New name:", text=name)
        if accepted and new.strip() and new.strip() != name:
            self.rename_control(name, new.strip())

    def rename_control(self, old: str, new: str) -> bool:
        return self._edit(f"Rename {old}", lambda: self._document.rename_control(old, new), structural=True,
                          need_step=False)

    def _ask_label_control(self, name: str) -> None:
        control = self._document.control(name)
        label, accepted = QtWidgets.QInputDialog.getText(self, "Control label", f"Label for '{name}':",
                                                         text=control.label)
        if not accepted:
            return
        help_text, accepted = QtWidgets.QInputDialog.getText(self, "Control help", f"Help for '{name}':",
                                                             text=control.help)
        if accepted:
            self._edit(f"Label {name}", lambda: self._document.set_control_label(name, label, help_text),
                       structural=True, need_step=False)

    # -- edits --------------------------------------------------------------------------

    def _edit(self, label, action, *, structural: bool = False, merge=None, need_step: bool = True) -> bool:
        if self._silent or self._editor is None or self._document is None or (need_step and self._step_id is None):
            return False
        return bool(self._editor(label, action, structural=structural, merge=merge))

    def _rename(self) -> None:
        new_id = self.id_edit.text().strip()
        old = self._step_id
        if not new_id or new_id == old:
            self.id_edit.setText(old or "")
            return
        if self._edit(f"Rename {old}", lambda: self._document.rename_step(old, new_id)):
            self._step_id = new_id
            self.sigRenamed.emit(old, new_id)
        else:
            self.id_edit.setText(old or "")

    def _set_plugin(self, combo) -> None:
        plugin_id = combo.currentData()
        self._edit("Change the plugin", lambda: self._document.set_plugin(self._step_id, plugin_id), structural=True)

    def _set_inputs(self, refs) -> None:
        refs = [str(ref).strip() for ref in refs if str(ref).strip()]
        self._edit("Change the inputs", lambda: self._document.set_inputs(self._step_id, refs), structural=True)

    def _replace_input(self, index: int, ref: str) -> None:
        ref = str(ref).strip()
        step = self._document.step(self._step_id)
        refs = [str(r) for r in step.inputs]
        if not ref or index >= len(refs) or refs[index] == ref:
            return
        self._edit("Change an input", lambda: self._document.connect(ref, self._step_id, index=index), structural=True)

    def _add_input(self, ref) -> None:
        if not ref:
            return
        self._edit("Add an input", lambda: self._document.connect(ref, self._step_id, add=True), structural=True)

    def _move_input(self, index: int, delta: int) -> None:
        step = self._document.step(self._step_id)
        refs = [str(ref) for ref in step.inputs]
        other = index + delta
        if not 0 <= other < len(refs):
            return
        refs[index], refs[other] = refs[other], refs[index]
        self._edit("Reorder the inputs", lambda: self._document.set_inputs(self._step_id, refs), structural=True)

    def _set_param(self, key: str, value) -> None:
        step_id = self._step_id
        self._edit(f"Change {key}", lambda: self._document.set_param(step_id, key, value), merge=("param", step_id, key))

    def _clear_param(self, key: str) -> None:
        step_id = self._step_id
        self._edit(f"Clear {key}", lambda: self._document.clear_param(step_id, key))

    def _browse_source(self) -> None:
        try:
            from imswitch.improcess.model.dataset_sources import SOURCE_SPECS, file_dialog_filter

            name_filter = file_dialog_filter(SOURCE_SPECS)
        except Exception:
            name_filter = "All files (*)"
        start = str(Path(self.path_edit.text()).parent) if self.path_edit.text() else ""
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Recording", start, name_filter)
        if path:
            self.path_edit.setText(path)
            self._edit("Change the recording", lambda: self._document.set_source(self._step_id, path=path),
                       structural=True)


__all__ = ["Inspector"]
