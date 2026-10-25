"""The right-hand panel: the selected step, in plain words, editable.

A header in the step's colour (plugin name, editable id, what it does and
what it takes and makes), then what it reads, its parameters (the shared
:class:`ParamForm`), and, for a Python step, the code editor with its ports.
Below sits the output of the last run (Phase 2: filled by the window).

Every widget edit goes through the window's ``editor`` callable, which makes
it one undoable document edit and refreshes the views; the inspector is only
rebuilt when the edit changed its own shape (another plugin, other inputs).
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR
from imswitch.improcess.model.workfloweditor.nodes import kinds_of, save_file_name, title_of
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


class Inspector(QtWidgets.QScrollArea):
    """See the module docstring."""

    sigMessage = QtCore.Signal(str)
    sigRenamed = QtCore.Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(340)
        self._document = None
        self._editor = None
        self._step_id: str | None = None
        self._param_form: ParamForm | None = None
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
        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        if self._step_id is None:
            help_text = QtWidgets.QLabel(_HELP)
            help_text.setWordWrap(True)
            help_text.setTextFormat(QtCore.Qt.RichText)
            layout.addWidget(help_text)
        else:
            step = document.step(self._step_id)
            self._build_header(layout, step)
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
        for index, ref in enumerate(refs):
            row = QtWidgets.QHBoxLayout()
            dot = QtWidgets.QLabel("●")
            kind = kinds.get(step.inputs[index].step, "unknown")
            dot.setStyleSheet(f"color: {st.KIND_COLOURS.get(kind, st.KIND_COLOURS['unknown'])};")
            dot.setToolTip(st.KIND_WORDS.get(kind, kind))
            row.addWidget(dot)
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
        layout.addWidget(_muted("Names: np, data (the first input's array), inputs, axes, scales, unit, axis('Z'), "
                                "results, make_result, make_labels. Set outputs = {port: array} or out = array."))

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
        form.addRow(_muted(f"Writes {save_file_name(step)} in the output folder."))
        placeholders = ", ".join("{%s}" % name for name in (catalog.path_placeholders if catalog else ()))
        form.addRow(_muted(f"Placeholders: {placeholders}."))

    def _add_param_form(self, layout, step) -> None:
        entry = self._document.entry_for(step)
        layout.addWidget(_section("Parameters"))
        form = ParamForm()
        form.set_plugin(entry, step.params)
        form.sigValueChanged.connect(self._set_param)
        form.sigValueCleared.connect(self._clear_param)
        form.setMinimumHeight(160)
        self._param_form = form
        layout.addWidget(form, 1)

    # -- edits --------------------------------------------------------------------------

    def _edit(self, label, action, *, structural: bool = False, merge=None) -> bool:
        if self._silent or self._editor is None or self._document is None or self._step_id is None:
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
