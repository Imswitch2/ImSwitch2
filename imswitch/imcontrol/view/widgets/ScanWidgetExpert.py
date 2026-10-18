"""The Expert scan panel (docs/design/plans/scan-engines-and-expert-scan.md §4,
§7.11, §7.12): the whole scan program, described top to bottom in the user's
words (a loop is a *dimension*, a drive a *positioner*, the step length the
*dwell*; the model keeps its own names).

- A header: what the scan is, whether it can run and where, its steps,
  duration and frames, and Run.
- **Scan order**: the dimensions as nested blocks, slowest first, each with
  its count and order, and the positions a positioner visits in that order.
- **Positions**: each positioner as a sentence (starts at, moves per
  dimension), and how it moves.
- **Within one step**: the timeline drawn and edited by dragging, the time
  a positioner moves hatched.
- **Pauses and triggers** and **Recording** (detector bindings) as sentences.
- **Can it run?** (the engine's report) and a **Preview** with every device
  on its own lane, beside the editor when the panel is wide enough, below it
  when not.

**Tables** shows the same program as the tables underneath: the one state
the parts above edit (``_expert_scan_parts``). The panel edits the program's
JSON form (``scan_program.serialize``); the controller turns it into a
program and says what the scan engine makes of it. Times are shown in ms,
positions in µm.
"""

from __future__ import annotations

import dataclasses
import math
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional, Sequence

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcontrol.model.scan_program import visits
from imswitch.imcontrol.model.scan_program.program import Motion
from imswitch.imcontrol.model.scan_program.serialize import FORMAT, program_from_dict
from ._expert_scan_parts import (
    KIND_NAMES, AdaptiveColumns, CheckCard, Chip, FlowLayout, LanePreview, OrderCard,
    PositionsCard, RecordingCard, StartCard, StepCard, TrajectoryCard, WaitsCard, WheelGuard,
    device_color, duration_text, ms_text, role_color,
)
from .ScanWidgetBase import SuperScanWidget

#: Shown units per stored unit: times are stored in s and shown in ms. A
#: shown value is divided by it (the inverse of the display), so a value
#: survives a round trip through the table exactly.
MS = 1000.0


def stored(text, scale):
    """A value typed in shown units (``scale`` per stored unit) as stored:
    the decimal as typed, divided exactly, then the nearest float -- what
    typing the stored value would give. A float division may land an ULP
    off, which strict timing refuses as off the grid (§7.9)."""
    try:
        value = Decimal(str(text).strip()) / Decimal(repr(float(scale)))
    except InvalidOperation:
        raise ValueError(f'{text!r} is not a number') from None
    return float(value)

#: Choices the model accepts (scan_program.program), repeated here so the
#: panel offers exactly them; a test holds the two together.
LOOP_KINDS = ('scan_x', 'scan_y', 'scan_z', 'cycle', 'plane', 'time', 'condition',
              'repeat', 'tile', 'position')
TRAVERSALS = ('forward', 'reverse', 'serpentine')
TRANSITIONS = ('jump', 'linear', 'smooth')
PLACEMENTS = ('in-step', 'between-steps')
ORIGINS = ('captured', 'absolute')
RESTORES = ('captured', 'first', 'park', 'hold')
EVENT_KINDS = ('ttl', 'detector', 'level', 'offset', 'marker')
EVENT_SHAPES = ('hold', 'ramp')
DURING = ('stationary', 'any')
TIMEOUT_ACTIONS = ('fail', 'continue')
WAIT_HOLDS = ('idle', 'hold')
ASSEMBLIES = ('stream', 'image')
WINDOWS = ('event', 'step')


#: The progress bar's resolution: per mille.
_PROGRESS_RANGE = 1000

#: The timing policy a scan built by hand gets: the model's own default.
DEFAULT_POLICY = 'strict-v1'


class _Column:
    """One table column: how a cell reads and writes a value.

    ``kind``: 'text', 'optional-text', 'int', 'float', 'optional-float',
    'choice', 'names' (comma-separated text), 'optional-names' (the same,
    blank = not given), 'ints' (comma-separated integers, blank = none),
    'floats'. ``scale``: shown units per stored
    unit (``MS``: ms shown, s stored)."""

    def __init__(self, key, label, kind='text', choices=(), scale=1.0):
        self.key, self.label, self.kind = key, label, kind
        self.choices, self.scale = choices, scale


#: What a cell of each kind takes, in a message.
_KIND_WORDS = {'int': 'a whole number', 'float': 'a number', 'optional-float': 'a number',
               'names': 'a list of names', 'optional-names': 'a list of names',
               'ints': 'a list of whole numbers', 'floats': 'a list of numbers'}


class _RowTable(QtWidgets.QWidget):
    """A table of rows of plain values, with add, remove and move buttons."""

    sigEdited = QtCore.Signal()

    def __init__(self, columns: Sequence[_Column], parent=None, title='Row'):
        super().__init__(parent)
        self._columns = list(columns)
        self._title = title
        self.table = QtWidgets.QTableWidget(0, len(self._columns))
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(lambda _item: self._edited())
        buttons = QtWidgets.QHBoxLayout()
        self.addButton = QtWidgets.QPushButton('Add')
        self.removeButton = QtWidgets.QPushButton('Remove')
        self.upButton = QtWidgets.QPushButton('Up')
        self.downButton = QtWidgets.QPushButton('Down')
        for button in (self.addButton, self.removeButton, self.upButton, self.downButton):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.addButton.clicked.connect(lambda: self.addRow({}))
        self.removeButton.clicked.connect(self._removeSelected)
        self.upButton.clicked.connect(lambda: self._move(-1))
        self.downButton.clicked.connect(lambda: self._move(+1))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self._quiet = False
        self._setHeaders()

    def _setHeaders(self):
        self.table.setColumnCount(len(self._columns))
        self.table.setHorizontalHeaderLabels([c.label for c in self._columns])
        self.table.resizeColumnsToContents()

    def _edited(self):
        if not self._quiet:
            self.sigEdited.emit()

    def columns(self):
        return list(self._columns)

    def setColumns(self, columns):
        rows = self.rows(raw=True)
        self._columns = list(columns)
        self._quiet = True
        try:
            self.table.setRowCount(0)
            self._setHeaders()
            for row in rows:
                self.addRow(row, raw=True)
        finally:
            self._quiet = False

    def setChoices(self, key, choices):
        for column in self._columns:
            if column.key == key:
                column.choices = tuple(choices)
        self.setColumns(self._columns)

    # -- rows -----------------------------------------------------------------

    def addRow(self, values: Dict, raw=False):
        row = self.table.rowCount()
        self._quiet = True
        try:
            self.table.insertRow(row)
            for col, column in enumerate(self._columns):
                value = values.get(column.key)
                if column.kind == 'choice':
                    combo = QtWidgets.QComboBox()
                    combo.addItems([str(c) for c in column.choices])
                    text = '' if value is None else str(value)
                    if text and combo.findText(text) < 0:
                        combo.addItem(text)       # a value the setup lacks stays visible
                    if text:
                        combo.setCurrentText(text)
                    combo.currentIndexChanged.connect(lambda _i: self._edited())
                    self.table.setCellWidget(row, col, combo)
                else:
                    shown = value if raw else self._show(column, value)
                    self.table.setItem(row, col, QtWidgets.QTableWidgetItem(
                        '' if shown is None else str(shown)))
        finally:
            self._quiet = False
        self._edited()

    def setRows(self, rows: Sequence[Dict]):
        self._quiet = True
        try:
            self.table.setRowCount(0)
            for row in rows:
                self.addRow(row)
        finally:
            self._quiet = False
        self.table.resizeColumnsToContents()

    def rows(self, raw=False) -> List[Dict]:
        """The rows as stored values; ``raw`` keeps the cells' text."""
        out = []
        for row in range(self.table.rowCount()):
            values = {}
            for col, column in enumerate(self._columns):
                widget = self.table.cellWidget(row, col)
                text = widget.currentText() if widget is not None else (
                    self.table.item(row, col).text() if self.table.item(row, col) else '')
                values[column.key] = text if raw else self._parse(column, text, row)
            out.append(values)
        return out

    def _show(self, column, value):
        if value is None:
            return ''
        if column.kind in ('float', 'optional-float'):
            return f'{float(value) * column.scale:.12g}'
        if column.kind in ('names', 'optional-names', 'ints'):
            return ', '.join(str(v) for v in value)
        if column.kind == 'floats':
            return ', '.join(f'{float(v) * column.scale:.12g}' for v in value)
        return value

    def _parse(self, column, text, row):
        text = str(text).strip()
        where = f'{self._title} {row + 1}, {column.label}'
        try:
            if column.kind == 'int':
                return int(text)
            if column.kind == 'float':
                return stored(text, column.scale)
            if column.kind == 'optional-float':
                return None if text == '' else stored(text, column.scale)
            if column.kind == 'optional-text':
                return text or None
            if column.kind == 'names':
                return [part.strip() for part in text.split(',') if part.strip()]
            if column.kind == 'optional-names':
                return None if text == '' else [part.strip() for part in text.split(',')
                                                if part.strip()]
            if column.kind == 'ints':
                return None if text == '' else [int(p) for p in text.split(',') if p.strip()]
            if column.kind == 'floats':
                return None if text == '' else [stored(p, column.scale)
                                                for p in text.split(',') if p.strip()]
        except ValueError:
            raise ValueError(f'{where}: {text!r} is not {_KIND_WORDS.get(column.kind, "valid")}') \
                from None
        return text

    def setValue(self, row, key, text):
        """Write ``text`` (shown units) into a cell, as typing it would."""
        col = next(i for i, c in enumerate(self._columns) if c.key == key)
        widget = self.table.cellWidget(row, col)
        if widget is not None:
            if widget.findText(str(text)) < 0:
                widget.addItem(str(text))
            widget.setCurrentText(str(text))
            return
        item = self.table.item(row, col)
        if item is None:
            self.table.setItem(row, col, QtWidgets.QTableWidgetItem(str(text)))
        elif item.text() != str(text):
            item.setText(str(text))

    def removeRow(self, row):
        if 0 <= row < self.table.rowCount():
            self.table.removeRow(row)
            self._edited()

    def moveRow(self, row, delta):
        self.table.setCurrentCell(row, 0)
        self._move(delta)

    def _removeSelected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for row in rows:
            self.table.removeRow(row)
        self._edited()

    def _move(self, delta):
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        rows = self.rows(raw=True)
        rows[row], rows[target] = rows[target], rows[row]
        self._quiet = True
        try:
            self.table.setRowCount(0)
            for values in rows:
                self.addRow(values, raw=True)
        finally:
            self._quiet = False
        self.table.setCurrentCell(target, 0)
        self._edited()


class PresetDialog(QtWidgets.QDialog):
    """A template's values as a form, in sections; OK only when they make a
    program. An empty field is filled in from the rig where the rig knows
    the value: a positioner field with a distinct positioner, a first
    position with that positioner's tracked position (and it follows the
    positioner chosen until the field is typed in), a move, settle or return
    time with what the setup declares in ``scanDesignerParams``. Nothing is
    guessed: what the rig does not know stays empty and says *required*."""

    def __init__(self, preset, devices: Dict[str, Sequence[str]], initial: Dict,
                 validate: Callable[[Dict], str], parent=None, *,
                 positions: Optional[Dict[str, float]] = None,
                 setup: Optional[Dict[str, float]] = None):
        super().__init__(parent)
        self.setWindowTitle(preset.title)
        self._preset, self._devices, self._validate = preset, devices, validate
        self._positions = dict(positions or {})
        self._setup = dict(setup or {})
        self._editors = {}
        self._filled = {}            # position fields filled from the rig, by name
        form = QtWidgets.QFormLayout()
        group = None
        positioners = 0
        for field in preset.fields:
            if field.group and field.group != group:
                group = field.group
                title = QtWidgets.QLabel(group)
                title.setObjectName('expertCardTitle')
                form.addRow(title)
            value = initial.get(field.name)
            if field.kind == 'positioner' and value in (None, ''):
                names = list(self._devices.get('positioner', ()))
                value = names[positioners % len(names)] if names else None
            if field.kind == 'positioner':
                positioners += 1
            if field.from_setup and value in (None, '') and field.from_setup in self._setup:
                value = self._setup[field.from_setup]
            editor = self._editor(field, value)
            if field.help:
                editor.setToolTip(field.help)
            unit = {'seconds': ' (ms)', 'micrometres': ' (µm)'}.get(field.kind, '')
            form.addRow(field.label + unit, editor)
            self._editors[field.name] = (field, editor)
        for name, (field, editor) in self._editors.items():
            if field.position_of and isinstance(editor, QtWidgets.QLineEdit):
                device = self._editors.get(field.position_of)
                if device is not None and isinstance(device[1], QtWidgets.QComboBox):
                    self._followPosition(name, device[1])
                    device[1].currentIndexChanged.connect(
                        lambda _i, n=name, c=device[1]: self._followPosition(n, c))
        self.error = QtWidgets.QLabel('')
        self.error.setWordWrap(True)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        if preset.summary:
            summary = QtWidgets.QLabel(preset.summary)
            summary.setWordWrap(True)
            layout.addWidget(summary)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _followPosition(self, name, combo):
        """A first-position field left to the rig shows the chosen
        positioner's tracked position; one typed in is left alone."""
        field, editor = self._editors[name]
        if editor.text().strip() and self._filled.get(name) != editor.text():
            return
        position = self._positions.get(combo.currentText())
        text = '' if position is None else f'{position:.6g}'
        editor.setText(text)
        editor.setModified(False)
        self._filled[name] = text
        editor.setToolTip((field.help + ' ' if field.help else '')
                          + (f'Filled in from where {combo.currentText()} is now.' if text else
                             f'Where {combo.currentText()} is now is not known.'))

    def _editor(self, field, value):
        if field.kind in ('positioner', 'laser', 'detector', 'optional-laser', 'choice'):
            combo = QtWidgets.QComboBox()
            devices = 'laser' if field.kind == 'optional-laser' else field.kind
            choices = list(field.choices) if field.kind == 'choice' else \
                list(self._devices.get(devices, ()))
            if field.kind in ('detector', 'optional-laser'):
                choices = [''] + choices
            combo.addItems(choices)
            if value not in (None, '') and combo.findText(str(value)) < 0:
                combo.addItem(str(value))
            if value not in (None, ''):
                combo.setCurrentText(str(value))
            return combo
        if field.kind == 'flag':
            box = QtWidgets.QCheckBox()
            box.setChecked(bool(value))
            return box
        if field.kind == 'pulses':
            table = _RowTable([
                _Column('device', 'Laser', 'choice', tuple(self._devices.get('laser', ()))),
                _Column('on', 'On (ms)', 'float', scale=MS),
                _Column('gap', 'Gap after (ms)', 'float', scale=MS)])
            for device, on_s, gap_s in (value or ()):
                table.addRow({'device': device, 'on': on_s, 'gap': gap_s})
            return table
        if field.kind in ('points', 'code'):
            text = QtWidgets.QPlainTextEdit()
            if field.kind == 'points' and value:
                text.setPlainText('\n'.join(f'{x:g}, {y:g}' for x, y in value))
            elif value:
                text.setPlainText(str(value))
            return text
        line = QtWidgets.QLineEdit()
        line.setPlaceholderText('required')
        if field.kind == 'pair' and value:
            line.setText(', '.join(f'{float(v):g}' for v in value))
            return line
        if value not in (None, ''):
            line.setText(f'{float(value) * MS:.12g}' if field.kind == 'seconds' else str(value))
        return line

    def params(self) -> Dict:
        out = {}
        for name, (field, editor) in self._editors.items():
            if isinstance(editor, QtWidgets.QCheckBox):
                out[name] = editor.isChecked()
            elif isinstance(editor, QtWidgets.QComboBox):
                out[name] = editor.currentText()
            elif isinstance(editor, _RowTable):
                out[name] = [[r['device'], r['on'], r['gap']] for r in editor.rows()]
            elif isinstance(editor, QtWidgets.QPlainTextEdit):
                text = editor.toPlainText()
                if field.kind == 'points':
                    out[name] = [[float(v) for v in line.replace(',', ' ').split()]
                                 for line in text.splitlines() if line.strip()] or None
                else:
                    out[name] = text if text.strip() else None
            else:
                text = editor.text().strip()
                if text == '':
                    out[name] = None
                elif field.kind == 'pair':
                    out[name] = [float(v) for v in text.replace(',', ' ').split()]
                elif field.kind == 'number':
                    out[name] = float(text)
                elif field.kind == 'seconds':
                    out[name] = stored(text, MS)
                elif field.kind == 'micrometres':
                    out[name] = float(text)
                elif field.kind == 'count':
                    out[name] = int(text)
                else:
                    out[name] = text
        return out

    def _accept(self):
        try:
            problem = self._validate(self.params())
        except ValueError as error:
            problem = str(error)
        if problem:
            self.error.setText(problem)
            return
        self.accept()


def _style() -> str:
    """The panel's own look, on top of the application's style sheet: neutral
    greys, and text colours chosen for the theme it is drawn on."""
    muted, bad = role_color('muted'), role_color('bad')
    return f'''
QFrame#expertCard, QFrame#expertHeader {{
    border: 1px solid rgba(128, 128, 128, 0.45); border-radius: 8px; }}
QLabel#expertCardTitle {{ font-weight: bold; }}
QLabel#expertTitle {{ font-size: 14px; font-weight: bold; }}
QLabel#expertMuted {{ color: {muted}; }}
QLabel#expertProblems, QLabel#expertNotice {{ color: {bad}; }}
QLabel#expertChip {{
    border: 1px solid rgba(128, 128, 128, 0.6); border-radius: 8px; padding: 1px 8px; }}
QFrame#expertLoop {{ border: 1px solid rgba(128, 128, 128, 0.6); border-radius: 6px; }}
QFrame#expertInspector {{ background-color: rgba(128, 128, 128, 0.12); border-radius: 6px; }}
QPushButton#expertRun {{ background-color: #185FA5; color: white; font-weight: bold;
    padding: 4px 18px; border-radius: 4px; }}
QPushButton#expertRun:disabled {{ background-color: #444441; color: #B4B2A9; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}
'''


#: The tables' names in messages: a value that does not parse is named by its
#: part, row and column.
_TABLE_TITLES = {'loops': 'Scan order, dimension', 'drives': 'Positions, positioner',
                 'timeline': 'Within one step, event', 'waits': 'Pauses and triggers, pause',
                 'detectors': 'Recording, detector'}

#: Each table's tab in the Tables view.
_TAB_NAMES = {'loops': 'Dimensions', 'drives': 'Positioners', 'timeline': 'Timeline',
              'waits': 'Pauses', 'detectors': 'Detectors'}


class ScanWidgetExpert(SuperScanWidget):
    """The Expert scan panel."""

    sigProgramEdited = QtCore.Signal()
    sigPresetRequested = QtCore.Signal(str)
    sigPlotClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._devices: Dict[str, List[str]] = {}
        self._wavelengths: Dict[str, float] = {}
        self._pointDetectors: List[str] = []
        self._presets: Dict[str, str] = {}
        self._check: Dict = {}
        self._lastGrid: Optional[float] = None
        self._running = False
        self._undo: List[Dict] = []
        self._redo: List[Dict] = []
        self._restoring = False

        # -- the tables: the one state every part edits ------------------------
        self.loops = _RowTable([
            _Column('id', 'Id'), _Column('kind', 'Kind', 'choice', LOOP_KINDS),
            _Column('count', 'Count', 'int'),
            _Column('traversal', 'Order', 'choice', TRAVERSALS),
            _Column('parity_loops', 'Alternates with', 'names')],
            title=_TABLE_TITLES['loops'])
        self.drives = _RowTable(self._driveColumns(()), title=_TABLE_TITLES['drives'])
        self.timeline = _RowTable(self._timelineColumns(), title=_TABLE_TITLES['timeline'])
        self.waits = _RowTable(self._waitColumns(()), title=_TABLE_TITLES['waits'])
        self.bindings = _RowTable(self._bindingColumns(), title=_TABLE_TITLES['detectors'])
        self.tabs = QtWidgets.QTabWidget()
        for name, table in (('loops', self.loops), ('drives', self.drives),
                            ('timeline', self.timeline), ('waits', self.waits),
                            ('detectors', self.bindings)):
            self.tabs.addTab(table, _TAB_NAMES[name])
        self.loops.sigEdited.connect(self._loopsEdited)
        for table in (self.drives, self.timeline, self.waits, self.bindings):
            table.sigEdited.connect(self._edited)

        self.stepPar = QtWidgets.QLineEdit('')
        self.stepPar.setToolTip('The dwell: the length of every step, in ms.')
        self.stepPar.textChanged.connect(lambda _t: self._edited())
        self.policyPar = QtWidgets.QComboBox()
        self.policyPar.setToolTip(
            'How requested times meet the engine\'s clock. strict-v1: every time must sit on '
            'it, and one that does not is refused with its neighbours.')
        self.policyPar.currentIndexChanged.connect(lambda _i: self._edited())

        # -- the header ---------------------------------------------------------
        self.titleLabel = QtWidgets.QLabel('New scan')
        self.titleLabel.setObjectName('expertTitle')
        self.subtitleLabel = QtWidgets.QLabel('')
        self.subtitleLabel.setObjectName('expertMuted')
        self.templateValuesButton = QtWidgets.QToolButton()
        self.templateValuesButton.setText('Change template values…')
        self.templateValuesButton.setAutoRaise(True)
        self.templateValuesButton.setVisible(False)
        self.templateValuesButton.clicked.connect(
            lambda: self.sigPresetRequested.emit(self._check.get('preset', '')))
        self.statusChip = Chip('Not checked yet', 'muted')
        self.statusChip.setToolTip('Click for whether it can run, and why not.')
        self.noticeLabel = QtWidgets.QLabel('')
        self.noticeLabel.setObjectName('expertNotice')
        self.noticeLabel.setVisible(False)
        self.factsHost = QtWidgets.QWidget()
        self.facts = FlowLayout(self.factsHost)
        self.templateButton = QtWidgets.QToolButton()
        self.templateButton.setText('New from template ▾')
        self.templateButton.setToolTip('Write a new scan from a template\'s values, replacing '
                                       'this one; a scan made from a template reopens its form '
                                       'with its values.')
        self.templateButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.templateButton.setMenu(QtWidgets.QMenu(self.templateButton))
        self.tablesButton = QtWidgets.QToolButton()
        self.tablesButton.setText('Tables')
        self.tablesButton.setCheckable(True)
        self.tablesButton.setToolTip('The same program as tables, every field of it.')
        self.undoButton = QtWidgets.QToolButton()
        self.undoButton.setText('Undo')
        self.undoButton.setToolTip('Undo the last edit (Ctrl+Z).')
        self.undoButton.clicked.connect(self.undo)
        self.redoButton = QtWidgets.QToolButton()
        self.redoButton.setText('Redo')
        self.redoButton.setToolTip('Redo (Ctrl+Shift+Z).')
        self.redoButton.clicked.connect(self.redo)
        self.engineLabel = QtWidgets.QLabel('')
        self.engineLabel.setObjectName('expertMuted')
        self.scanButton.setText('Run')
        self.scanButton.setObjectName('expertRun')
        # Run is disabled while a run is in progress; Stop is its way out
        # (review 2026-10-16: the panel had none, only the Recording panel's).
        # the scan panels' own Stop (SuperScanWidget), as this panel words it
        self.stopButton.setObjectName('expertStop')
        self.stopButton.setToolTip(
            'Stop the run: on the NI-DAQ now (the scanners return to where it started), '
            'on a step engine at its next step.')
        self.loadScanBtn.setText('Open…')
        self.saveScanBtn.setText('Save…')
        self.scanProgressBar = QtWidgets.QProgressBar()
        self.scanProgressBar.setRange(0, _PROGRESS_RANGE)
        self.scanProgressBar.setTextVisible(False)
        self.scanProgressLabel = QtWidgets.QLabel()
        self.progressRow = QtWidgets.QWidget()
        progressLayout = QtWidgets.QHBoxLayout(self.progressRow)
        progressLayout.setContentsMargins(0, 0, 0, 0)
        progressLayout.addWidget(self.scanProgressBar, 1)
        progressLayout.addWidget(self.scanProgressLabel)
        self.progressRow.setVisible(False)

        # -- the parts ------------------------------------------------------------
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(0)
        self.plotButton = QtWidgets.QPushButton('Show preview')
        self.plotButton.setToolTip('Draw what every device does, from the same compile the '
                                   'run uses.')
        self.plotButton.clicked.connect(self.sigPlotClicked)
        self.start = StartCard(self)
        self.trajectoryCard = TrajectoryCard(self)
        self.order = OrderCard(self)
        self.positions = PositionsCard(self)
        self.step = StepCard(self)
        self.waitsCard = WaitsCard(self)
        self.recording = RecordingCard(self)
        self.check = CheckCard(self.report)
        self.preview = LanePreview(self.plotButton)
        self.plot = self.preview.plot
        self.columns = AdaptiveColumns(
            [self.start, self.trajectoryCard, self.order, self.positions, self.step,
             self.waitsCard, self.recording], [self.check, self.preview])
        tablesPage = QtWidgets.QWidget()
        tablesLayout = QtWidgets.QGridLayout(tablesPage)
        tablesLayout.setContentsMargins(0, 0, 0, 0)
        tablesLayout.addWidget(self.tabs, 0, 0, 1, 4)
        tablesLayout.addWidget(QtWidgets.QLabel('Dwell (ms):'), 1, 0)
        tablesLayout.addWidget(self.stepPar, 1, 1)
        tablesLayout.addWidget(QtWidgets.QLabel('Timing:'), 1, 2)
        tablesLayout.addWidget(self.policyPar, 1, 3)
        self.pages = QtWidgets.QStackedWidget()
        self.pages.addWidget(self.columns)
        self.pages.addWidget(tablesPage)
        self.tablesButton.toggled.connect(lambda on: self.pages.setCurrentIndex(1 if on else 0))
        self.statusChip.clicked.connect(self._showCheck)
        self.setStyleSheet(_style())
        self.wheelGuard = WheelGuard(self.scrollArea, self)
        for keys, slot in ((QtGui.QKeySequence.Undo, self.undo),
                           (QtGui.QKeySequence.Redo, self.redo)):
            shortcut = QtWidgets.QShortcut(keys, self)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        self._refreshTimer = QtCore.QTimer(self)
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(0)
        self._refreshTimer.timeout.connect(self.refreshParts)

    # -- the panel ------------------------------------------------------------

    def initControls(self, positionerNames, TTLDeviceNames, TTLTimeUnits=None, *,
                     detectorNames=(), presets=(), policies=(), engine='', wavelengths=None,
                     pointDetectorNames=()):
        self._devices = {'positioner': list(positionerNames), 'laser': list(TTLDeviceNames),
                         'detector': list(detectorNames)}
        self._wavelengths = dict(wavelengths or {})
        self._pointDetectors = list(pointDetectorNames)
        # The header stays put above the scrolling parts: what the scan is,
        # whether it runs, and Run, wherever the user has scrolled to.
        header = QtWidgets.QFrame()
        header.setObjectName('expertHeader')
        headerLayout = QtWidgets.QVBoxLayout(header)
        # Everything in the header wraps, so a narrow dock keeps Run and the
        # verdict in view instead of forcing its own width (§7.12).
        topHost = QtWidgets.QWidget()
        top = FlowLayout(topHost)
        self.subtitleLabel.setWordWrap(True)
        for widget in (self.titleLabel, self.templateValuesButton, self.statusChip,
                       self.scanButton, self.stopButton):
            top.addWidget(widget)
        headerLayout.addWidget(topHost)
        headerLayout.addWidget(self.subtitleLabel)
        headerLayout.addWidget(self.noticeLabel)
        headerLayout.addWidget(self.factsHost)
        toolsHost = QtWidgets.QWidget()
        tools = FlowLayout(toolsHost)
        for widget in (self.templateButton, self.loadScanBtn, self.saveScanBtn,
                       self.undoButton, self.redoButton, self.repeatBox, self.tablesButton):
            tools.addWidget(widget)
        headerLayout.addWidget(toolsHost)
        headerLayout.addWidget(self.engineLabel)
        headerLayout.addWidget(self.progressRow)
        self.scrollContainer.removeWidget(self.scrollArea)
        self.scrollContainer.addWidget(header, 0, 0)
        self.scrollContainer.addWidget(self.scrollArea, 1, 0)
        self.scrollContainer.setRowStretch(1, 1)
        self.grid.addWidget(self.pages, 0, 0)
        self.grid.setRowStretch(0, 1)
        menu = self.templateButton.menu()
        trajectories = None
        starters, paths = [], []
        for preset_id, title in presets:
            self._presets[preset_id] = title
            if preset_id.startswith('trajectory-'):
                trajectories = trajectories or menu.addMenu('Trajectories')
                target, text = trajectories, title.split(': ', 1)[-1]
                paths.append((preset_id, text))
            else:
                target, text = menu, title
                starters.append((preset_id, title))
            action = target.addAction(text)
            action.setData(preset_id)
            action.triggered.connect(lambda _c=False, p=preset_id: self.sigPresetRequested.emit(p))
        self.start.setTemplates(starters, paths)
        self.policyPar.addItems(list(policies))
        if self.policyPar.findText(DEFAULT_POLICY) >= 0:
            self.policyPar.setCurrentText(DEFAULT_POLICY)
        self.engineLabel.setText(engine)
        self.engineLabel.setVisible(bool(engine))
        self.drives.setColumns(self._driveColumns(()))
        self.timeline.setColumns(self._timelineColumns())
        self.bindings.setColumns(self._bindingColumns())
        self.refreshParts()
        self._undo = [self._snapshot()]

    def presetTitles(self) -> Dict[str, str]:
        """The templates offered: ``{preset id: title}``."""
        return dict(self._presets)

    def _driveColumns(self, loopIds):
        return ([_Column('device', 'Device', 'choice', tuple(self._devices.get('positioner', ()))),
                 _Column('offset_um', 'Offset (µm)', 'float')]
                + [_Column(f'w:{loop}', f'{loop} (µm/count)', 'optional-float')
                   for loop in loopIds]
                + [_Column('table_loop', 'Table loop', 'optional-text'),
                   _Column('table_um', 'Table (µm)', 'floats'),
                   _Column('transition', 'Move shape', 'choice', TRANSITIONS),
                   _Column('move_s', 'Move (ms)', 'float', scale=MS),
                   _Column('settle_s', 'Settle (ms)', 'float', scale=MS),
                   _Column('placement', 'Moves', 'choice', PLACEMENTS),
                   _Column('return_s', 'Return (ms)', 'float', scale=MS),
                   _Column('origin', 'Origin', 'choice', ORIGINS),
                   _Column('restore', 'After the scan', 'choice', RESTORES)])

    def _timelineColumns(self):
        return [_Column('device', 'Device', 'choice', tuple(self.timelineDevices())),
                _Column('kind', 'Kind', 'choice', EVENT_KINDS),
                _Column('start_s', 'Start (ms)', 'float', scale=MS),
                _Column('end_s', 'End (ms)', 'float', scale=MS),
                _Column('value', 'Value', 'optional-float'),
                _Column('shape', 'Shape', 'choice', EVENT_SHAPES),
                _Column('phases', 'Phases', 'ints'),
                _Column('during', 'During', 'choice', DURING),
                _Column('still', 'Still', 'optional-names'),
                _Column('anchor', 'Anchor'),
                _Column('id', 'Id', 'optional-text')]

    def _waitColumns(self, loopIds):
        return [_Column('at', 'Before', 'choice', ('start', 'step') + tuple(loopIds)),
                _Column('trigger', 'Until', 'choice', ('time', 'external')),
                _Column('interval_s', 'Interval (ms)', 'optional-float', scale=MS),
                _Column('timeout_s', 'Timeout (ms)', 'optional-float', scale=MS),
                _Column('on_timeout', 'On timeout', 'choice', TIMEOUT_ACTIONS),
                _Column('hold', 'Outputs', 'choice', WAIT_HOLDS)]

    def _bindingColumns(self):
        return [_Column('detector', 'Detector', 'choice', tuple(self._devices.get('detector', ()))),
                _Column('events', 'Events (id or device)'),
                _Column('per_step', 'Per step', 'int'),
                _Column('phases', 'Phases', 'ints'),
                _Column('window', 'Window', 'choice', WINDOWS),
                _Column('assembly', 'Assembly', 'choice', ASSEMBLIES)]

    def _loopIds(self):
        return [row['id'] for row in self.loops.rows(raw=True) if row['id']]

    def loopNames(self) -> Dict[str, str]:
        """Each dimension's id and what to call one iteration of it: its
        kind's word (*cycle*), with its id when two share a kind."""
        rows = [row for row in self.tableRows('loops') if row['id']]
        kinds = [row['kind'] for row in rows]
        names = {}
        for row in rows:
            one = KIND_NAMES.get(row['kind'], (row['kind'],) * 2)[0]
            names[row['id']] = f'{one} ({row["id"]})' if kinds.count(row['kind']) > 1 else one
        return names

    def _loopsEdited(self):
        ids = self._loopIds()
        current = [c.key[2:] for c in self.drives.columns() if c.key.startswith('w:')]
        if ids != current:
            # a loop renamed in place keeps what names it: its drives'
            # weights, the waits before it, the anchors on it
            renamed = ({old: new for old, new in zip(current, ids) if old != new}
                       if len(ids) == len(current) else {})
            rows = self.drives.rows(raw=True)
            for row in rows:
                for old, new in renamed.items():
                    row[f'w:{new}'] = row.pop(f'w:{old}', '')
            waits = self.waits.rows(raw=True)
            self.drives.setColumns(self._driveColumns(ids))
            self.drives.setRows([])
            for row in rows:
                self.drives.addRow(row, raw=True)
            self.waits.setColumns(self._waitColumns(ids))
            if renamed:
                self._renameReferences(renamed, waits)
        self._edited()

    def _renameReferences(self, renamed, waits=None):
        for index, wait in enumerate(waits if waits is not None else self.tableRows('waits')):
            if wait['at'] in renamed:
                self.waits.setValue(index, 'at', renamed[wait['at']])
        for index, event in enumerate(self.tableRows('timeline')):
            anchor = event['anchor'] or 'step'
            kind, _, loop = anchor.partition(':')
            if loop in renamed:
                self.timeline.setValue(index, 'anchor', f'{kind}:{renamed[loop]}')
        for index, loop in enumerate(self.tableRows('loops')):
            parity = [p.strip() for p in loop['parity_loops'].split(',') if p.strip()]
            if any(p in renamed for p in parity):
                self.loops.setValue(index, 'parity_loops',
                                    ', '.join(renamed.get(p, p) for p in parity))

    def _edited(self):
        self.sigProgramEdited.emit()
        self.preview.setStale(True)
        self._refreshTimer.start()

    def flushRefresh(self):
        """Draw the parts now if an edit is waiting to be drawn: a click that
        commits a field acts on the program as it now is."""
        if self._refreshTimer.isActive():
            self.refreshParts()

    # -- program <-> tables -----------------------------------------------------

    def setProgramDict(self, data: Optional[Dict]):
        """Show a program's JSON form (``program_to_dict``), or an empty one."""
        data = dict(data or {})
        blockers = [QtCore.QSignalBlocker(w) for w in (
            self.loops, self.drives, self.timeline, self.waits, self.bindings,
            self.stepPar, self.policyPar)]
        try:
            loops = data.get('loops') or []
            self.loops.setRows(loops)
            ids = [loop['id'] for loop in loops]
            self.drives.setColumns(self._driveColumns(ids))
            self.waits.setColumns(self._waitColumns(ids))
            rows = []
            for drive in data.get('drives') or []:
                row = {'device': drive['device'], 'offset_um': drive.get('offset_um', 0.0),
                       'table_loop': drive.get('table_loop'), 'table_um': drive.get('table_um')}
                row.update({f'w:{k}': v for k, v in (drive.get('weights_um') or {}).items()})
                row.update(drive.get('motion') or {})
                rows.append(row)
            self.drives.setRows(rows)
            self.timeline.setColumns(self._timelineColumns())
            self.timeline.setRows(data.get('timeline') or [])
            self.waits.setRows(data.get('waits') or [])
            self.bindings.setRows(data.get('detectors') or [])
            step = data.get('step_s')
            self.stepPar.setText('' if step is None else f'{float(step) * MS:.12g}')
            if data.get('timing_policy'):
                if self.policyPar.findText(data['timing_policy']) < 0:
                    self.policyPar.addItem(data['timing_policy'])
                self.policyPar.setCurrentText(data['timing_policy'])
        finally:
            del blockers
        self.preview.setStale(True)
        self.refreshParts()
        # a program opened is where undo starts
        self._undo, self._redo = [self._snapshot()], []
        self.undoButton.setEnabled(False)
        self.redoButton.setEnabled(False)

    def _loopDicts(self) -> List[Dict]:
        return [{'id': row['id'], 'kind': row['kind'], 'count': row['count'],
                 'traversal': row['traversal'], 'parity_loops': row['parity_loops']}
                for row in self.loops.rows()]

    def _driveDicts(self) -> List[Dict]:
        motion_keys = ('transition', 'move_s', 'settle_s', 'placement', 'return_s', 'origin',
                       'restore')
        return [{'device': row['device'], 'offset_um': row['offset_um'],
                 'weights_um': {k[2:]: v for k, v in row.items()
                                if k.startswith('w:') and v is not None},
                 'table_loop': row['table_loop'], 'table_um': row['table_um'],
                 'motion': {k: row[k] for k in motion_keys}}
                for row in self.drives.rows()]

    def programDict(self) -> Dict:
        """The program the tables describe, in its JSON form (without
        ``format``); a cell that does not parse raises ValueError naming it."""
        loops = self._loopDicts()
        drives = self._driveDicts()
        timeline = []
        for row in self.timeline.rows():
            event = dict(row)
            event['anchor'] = event['anchor'] or 'step'
            timeline.append(event)
        step = self.stepPar.text().strip()
        if not step:
            raise ValueError('Set the dwell (Within one step, in ms).')
        try:
            step_s = stored(step, MS)
        except ValueError:
            raise ValueError(f'The dwell {step!r} is not a number of ms.') from None
        return {'loops': loops, 'drives': drives, 'step_s': step_s, 'timeline': timeline,
                'waits': self.waits.rows(), 'detectors': self.bindings.rows(),
                'timing_policy': self.policyPar.currentText()}

    # -- undo -----------------------------------------------------------------

    def _snapshot(self) -> Dict:
        return {'tables': {name: self.tableRows(name) for name in _TABLE_TITLES},
                'step': self.stepPar.text(), 'policy': self.policyPar.currentText()}

    def _remember(self):
        if self._restoring:
            return
        snapshot = self._snapshot()
        if not self._undo or self._undo[-1] != snapshot:
            self._undo.append(snapshot)
            self._redo.clear()
        self.undoButton.setEnabled(len(self._undo) > 1)
        self.redoButton.setEnabled(bool(self._redo))

    def _restore(self, snapshot):
        self._restoring = True
        try:
            blockers = [QtCore.QSignalBlocker(w) for w in (
                self.loops, self.drives, self.timeline, self.waits, self.bindings,
                self.stepPar, self.policyPar)]
            try:
                tables = snapshot['tables']
                self.loops.setRows([])
                for row in tables['loops']:
                    self.loops.addRow(row, raw=True)
                ids = [row['id'] for row in tables['loops'] if row['id']]
                self.drives.setColumns(self._driveColumns(ids))
                self.waits.setColumns(self._waitColumns(ids))
                for name in ('drives', 'timeline', 'waits', 'detectors'):
                    table = self._table(name)
                    table.setRows([])
                    for row in tables[name]:
                        table.addRow(row, raw=True)
                self.stepPar.setText(snapshot['step'])
                self.policyPar.setCurrentText(snapshot['policy'])
            finally:
                del blockers
            self._edited()
            self.refreshParts()
        finally:
            self._restoring = False
        self.undoButton.setEnabled(len(self._undo) > 1)
        self.redoButton.setEnabled(bool(self._redo))

    def undo(self):
        self.flushRefresh()              # the state now, edits not yet drawn included
        if len(self._undo) > 1:
            self._redo.append(self._undo.pop())
            self._restore(self._undo[-1])

    def redo(self):
        self.flushRefresh()
        if self._redo:
            snapshot = self._redo.pop()
            self._undo.append(snapshot)
            self._restore(snapshot)

    # -- what the parts read and write --------------------------------------------

    @property
    def devices(self) -> Dict[str, List[str]]:
        return self._devices

    def _table(self, name) -> _RowTable:
        return {'loops': self.loops, 'drives': self.drives, 'timeline': self.timeline,
                'waits': self.waits, 'detectors': self.bindings}[name]

    def tableRows(self, name) -> List[Dict]:
        return self._table(name).rows(raw=True)

    def setTableValue(self, name, row, key, text):
        self._table(name).setValue(row, key, '' if text is None else str(text))

    def addTableRow(self, name, values) -> int:
        table = self._table(name)
        table.addRow(values)
        return table.table.rowCount() - 1

    def removeTableRow(self, name, row):
        self._table(name).removeRow(row)

    def moveTableRow(self, name, row, delta):
        self._table(name).moveRow(row, delta)

    def setTimes(self, row, start_s, end_s):
        """An event's times, as one edit."""
        blocker = QtCore.QSignalBlocker(self.timeline)
        try:
            self.timeline.setValue(row, 'start_s', ms_text(start_s))
        finally:
            del blocker
        self.timeline.setValue(row, 'end_s', ms_text(end_s))

    def addLoop(self, kind):
        ids = set(self._loopIds())
        name, n = kind, 2
        while name in ids:
            name, n = f'{kind}{n}', n + 1
        self.addTableRow('loops', {'id': name, 'kind': kind, 'count': 1,
                                   'traversal': 'forward', 'parity_loops': []})

    def moveLoop(self, index, delta):
        """A loop moved outwards or inwards: a serpentine's parity -- the
        loops right outside it -- is no longer what it named, so it goes."""
        self.moveTableRow('loops', index, delta)
        for row, loop in enumerate(self.tableRows('loops')):
            if loop['traversal'] == 'serpentine':
                self.loops.setValue(row, 'traversal', 'forward')
                self.loops.setValue(row, 'parity_loops', '')
                self.showNotice(f'Dimension {loop["id"]!r} moved out of serpentine order: '
                                'the dimensions outside it changed.')

    def removeLoop(self, index):
        """A dimension removed with what names it: the pauses before it, the
        events anchored on its passes (now every step), the serpentines
        alternating with it."""
        loops = self.tableRows('loops')
        loop = loops[index]['id']
        said = []
        for row in reversed(range(len(self.tableRows('waits')))):
            if self.tableRows('waits')[row]['at'] == loop:
                self.waits.removeRow(row)
                said.append('the pause before it')
        for row, event in enumerate(self.tableRows('timeline')):
            if (event['anchor'] or '').partition(':')[2] == loop:
                self.timeline.setValue(row, 'anchor', 'step')
                said.append(f'{event["device"]}\'s anchor')
        for row, other in enumerate(loops):
            parity = [p.strip() for p in other['parity_loops'].split(',') if p.strip()]
            if loop in parity:
                self.loops.setValue(row, 'traversal', 'forward')
                self.loops.setValue(row, 'parity_loops', '')
                said.append(f'{other["id"]}\'s serpentine')
        self.removeTableRow('loops', index)
        self.showNotice(f'Removed dimension {loop!r}' + (f', and {", ".join(sorted(set(said)))}'
                                                         if said else '') + '. Undo brings it back.')

    def renameLoop(self, index, name):
        if name in self._loopIds():
            self.showNotice(f'There is a dimension {name!r} already.')
            return
        self.setTableValue('loops', index, 'id', name)

    def addDrive(self, device):
        # the model's defaults, as a drive written without them gets
        self.addTableRow('drives', {'device': device, 'offset_um': 0.0,
                                    **dataclasses.asdict(Motion())})

    def addEvent(self, device, kind) -> Optional[int]:
        """An event after the last one in the step, to the step's end; over
        the whole step when the last one ends it."""
        step = self.stepSeconds()
        ends = []
        for row in self.tableRows('timeline'):
            if (row['anchor'] or 'step') == 'step':
                try:
                    ends.append(stored(row['end_s'], MS))
                except ValueError:
                    pass
        start = max(ends, default=0.0)
        if step is None or start >= step:
            start = 0.0
        return self.addTableRow('timeline', {
            'device': device, 'kind': kind, 'start_s': start,
            'end_s': step if step is not None else start, 'shape': 'hold',
            'during': 'stationary', 'anchor': 'step'})

    def addWait(self, trigger):
        ids = self._loopIds()
        self.addTableRow('waits', {'at': ids[0] if ids else 'start', 'trigger': trigger,
                                   'on_timeout': 'fail', 'hold': 'idle'})

    def isPointDetector(self, name) -> bool:
        return name in self._pointDetectors

    def addBinding(self, detector):
        point = self.isPointDetector(detector)
        self.addTableRow('detectors', {
            'detector': detector, 'events': detector, 'per_step': 1,
            'window': 'step' if point else 'event', 'assembly': 'image' if point else 'stream'})

    def setBindingDetector(self, row, detector):
        """A binding's detector, with the window and assembly its kind takes
        (the only ones the model accepts for it) and its own events."""
        point = self.isPointDetector(detector)
        blocker = QtCore.QSignalBlocker(self.bindings)
        try:
            self.bindings.setValue(row, 'window', 'step' if point else 'event')
            self.bindings.setValue(row, 'assembly', 'image' if point else 'stream')
            self.bindings.setValue(row, 'events', detector)
        finally:
            del blocker
        self.bindings.setValue(row, 'detector', detector)

    def timelineDevices(self) -> List[str]:
        names = (self._devices.get('laser', []) + self._devices.get('detector', [])
                 + self._devices.get('positioner', []))
        return list(dict.fromkeys(names))

    def eventChoices(self) -> List[str]:
        rows = self.tableRows('timeline')
        return list(dict.fromkeys([r['id'] for r in rows if r['id']]
                                  + [r['device'] for r in rows if r['device']]))

    def deviceColor(self, name) -> QtGui.QColor:
        kind = ('positioner' if name in self._devices.get('positioner', ()) else
                'detector' if name in self._devices.get('detector', ()) else 'laser')
        return device_color(name, self._wavelengths, kind)

    def stepSeconds(self) -> Optional[float]:
        try:
            value = stored(self.stepPar.text(), MS)
        except ValueError:
            return None
        return value if value > 0 else None

    def gridSeconds(self) -> Optional[float]:
        return (self._check.get('facts') or {}).get('grid_s') or self._lastGrid

    def phaseCount(self) -> int:
        for row in self.tableRows('loops'):
            if row['kind'] == 'condition':
                try:
                    return int(row['count'])
                except ValueError:
                    return 1
        return 1

    def stepContent(self):
        """The step's events as the canvas draws them, the time each drive
        moves inside the step, and what to say under the drawing."""
        events, notes = [], []
        latest = None
        for index, row in enumerate(self.tableRows('timeline')):
            try:
                start, end = stored(row['start_s'], MS), stored(row['end_s'], MS)
            except ValueError:
                notes.append(f'{row["device"]}\'s event has a time that is not a number, so it '
                             'is not drawn.')
                continue
            try:
                phases = (None if not row['phases'].strip() else
                          frozenset(int(p) for p in row['phases'].split(',') if p.strip()))
            except ValueError:
                phases = None
            anchor = row['anchor'] or 'step'
            tag = {'level': row['value'], 'offset': f'{row["value"]} µm'}.get(row['kind'], '')
            if anchor.startswith('pass-start:'):
                tag = (tag + ' ' if tag else '') + f'first of each {anchor.split(":", 1)[1]}'
            elif anchor.startswith('pass-end:'):
                tag = (tag + ' ' if tag else '') + f'last of each {anchor.split(":", 1)[1]}'
            if phases is not None:
                tag = (tag + ' ' if tag else '') + 'phase ' + ', '.join(
                    str(p) for p in sorted(phases))
            if anchor == 'step':
                latest = end if latest is None else max(latest, end)
            events.append({'row': index, 'device': row['device'], 'label': row['device'],
                           'kind': row['kind'], 'start_s': start, 'end_s': end,
                           'from_end': anchor.startswith('pass-end:'), 'phases': phases,
                           'color': self.deviceColor(row['device']), 'tag': tag})
        moving = []
        step = self.stepSeconds()
        if step is None:
            notes.append('Set the dwell.')
        elif latest is not None and latest > step:
            notes.append((f'The events end at {ms_text(latest)} ms, after the step.',
                          f'Make the dwell {ms_text(latest)} ms',
                          lambda _c=False, t=ms_text(latest): self.stepPar.setText(t)))
        for row in self.tableRows('drives'):
            try:
                busy = stored(row['move_s'], MS) + stored(row['settle_s'], MS)
            except ValueError:
                continue
            if busy <= 0:
                continue
            if row['placement'] == 'in-step' and step is not None:
                moving.append((max(0.0, step - busy), step, f'{row["device"]} moves'))
            elif row['placement'] == 'between-steps':
                notes.append(f'{row["device"]} moves between steps: '
                             f'{duration_text(busy)} after each.')
        return events, moving, notes

    def driveRange(self, index) -> str:
        """Where a positioner goes over the whole scan: from its dimensions'
        counts and weights (every order visits the same counters), or its
        table; and how far apart its positions are when they are evenly
        spaced."""
        try:
            row = self.tableRows('drives')[index]
            counts = {loop['id']: int(loop['count']) for loop in self.tableRows('loops')}
            offset = float(row['offset_um'])
            if row['table_loop']:
                table = [float(v) for v in row['table_um'].split(',') if v.strip()]
                low, high = offset + min(table), offset + max(table)
                steps = counts.get(row['table_loop'], len(table))
                positions = sorted(set(offset + v for v in table))
            else:
                low = high = offset
                steps = 1
                positions = [offset]
                for loop, count in counts.items():
                    text = row.get(f'w:{loop}', '').strip()
                    if not text or float(text) == 0:
                        continue
                    reach = float(text) * (count - 1)
                    low, high = low + min(0.0, reach), high + max(0.0, reach)
                    steps *= count
                    positions = sorted({p + float(text) * k for p in positions
                                        for k in range(count)})
        except (ValueError, KeyError, IndexError):
            return ''
        if steps <= 1:
            return f'Stays at {low:.6g} µm.'
        text = f'{steps} positions from {low:.6g} to {high:.6g} µm'
        gaps = {round(b - a, 9) for a, b in zip(positions, positions[1:])}
        if len(gaps) == 1:
            text += f', {gaps.pop():.6g} µm apart'
        return text + '.'

    def visitStrip(self, fit):
        """The positions the first positioner visits over the two fastest
        dimensions that move it (the others at their first iteration), at
        most ``fit`` of them: the scan's order, as a line through points.
        ``(None, why not)`` when it cannot be drawn."""
        drives = self.tableRows('drives')
        if not drives:
            return None, ''
        try:
            # the dimensions and the positioner alone: an event or a pause
            # that does not read yet does not hide the order
            loops = self._loopDicts()
            drive = self._driveDicts()[0]
        except ValueError as error:
            return None, ('The order visited is drawn once the dimensions and positioners '
                          f'read: {error}')
        moving = [loop['id'] for loop in loops
                  if loop['id'] in drive['weights_um'] and drive['weights_um'][loop['id']]
                  or loop['id'] == drive['table_loop']]
        if not moving:
            return None, f'{drive["device"]} does not move with the dimensions.'
        inner = moving[-2:]
        counts = {loop['id']: max(1, int(loop['count'])) for loop in loops}
        innermost = counts[inner[-1]]
        shown = {inner[-1]: min(innermost, fit)}
        if len(inner) > 1:
            shown[inner[0]] = max(1, min(counts[inner[0]], fit // max(1, shown[inner[-1]])))
        reduced = []
        for loop in loops:
            loop['count'] = shown.get(loop['id'], 1)
            reduced.append(loop)
        table = drive['table_um']
        if drive['table_loop'] and table:
            table = table[:shown.get(drive['table_loop'], 1)]
        try:
            program = program_from_dict({
                'format': FORMAT, 'loops': reduced, 'step_s': 1.0, 'timeline': [],
                'timing_policy': self.policyPar.currentText() or 'strict-v1',
                'drives': [{**drive, 'table_um': table}]})
        except Exception as error:
            return None, f'The order visited cannot be drawn: {error}'
        model = program.drives[0]
        positions, shades = [], []
        for counters in visits(program):
            if model.table_loop is not None:
                position = model.offset_um + model.table_um[counters[model.table_loop]]
            else:
                position = model.offset_um + sum(float(w) * counters[lp] for lp, w in
                                                 model.weights_um.items())
            positions.append(position)
            shades.append(counters[inner[0]] if len(inner) > 1 else 0)
        total = math.prod(counts[i] for i in inner)
        kinds = {loop['id']: loop['kind'] for loop in loops}
        names = [KIND_NAMES.get(kinds[i], (i, i))[1] for i in inner]
        text = f'{model.device} position at each step over the {" and ".join(names)}, in the ' \
               'order visited'
        if len(positions) != total:
            text += f' (the first {len(positions)} of {total})'
        return positions, shades, self.deviceColor(model.device), text

    # -- what the controller shows ----------------------------------------------

    def _mode(self) -> str:
        if self._check.get('trajectory'):
            return 'trajectory'
        if not any(self.tableRows(name) for name in ('loops', 'drives', 'timeline')):
            return 'empty'
        return 'program'

    def refreshParts(self):
        self._refreshTimer.stop()
        mode = self._mode()
        self.start.setVisible(mode == 'empty')
        self.trajectoryCard.setVisible(mode == 'trajectory')
        if mode == 'trajectory':
            self.trajectoryCard.setTrajectory(self._check['trajectory'])
        for part in (self.order, self.positions, self.step):
            part.setVisible(mode == 'program')
        for part in (self.waitsCard, self.recording):
            part.setVisible(mode != 'empty')
        for part in (self.order, self.positions, self.step, self.waitsCard, self.recording):
            part.refresh()
        self._refreshHeader()
        self.wheelGuard.watch(self.columns)
        self._remember()

    def _refreshHeader(self):
        check = self._check
        loops = self.tableRows('loops')
        parts = []
        for row in loops:
            many = KIND_NAMES.get(row['kind'], (row['kind'],) * 2)
            parts.append(f'{row["count"]} {many[1] if row["count"] != "1" else many[0]}')
        self.titleLabel.setText(check.get('title') or 'New scan')
        self.subtitleLabel.setText(' › '.join(parts) if parts else
                                   check.get('subtitle') or 'Nothing to scan yet')
        preset = check.get('preset') or ''
        self.templateValuesButton.setVisible(bool(preset) and self._mode() != 'empty')
        self.templateValuesButton.setToolTip(
            'Open the template\'s form with the values this scan was made from. The scan has '
            'been edited since: the form holds the template\'s values, not the edits.'
            if check.get('edited') else
            'Open the template\'s form with the values this scan was made from.')
        if self._running:
            self.statusChip.setText('Running')
            self.statusChip.setRole('accent')
        elif self._mode() == 'empty':
            self.statusChip.setText('Empty')
            self.statusChip.setRole('muted')
        elif not check:
            self.statusChip.setText('Not checked yet')
            self.statusChip.setRole('muted')
        elif check.get('runnable'):
            self.statusChip.setText(f'Ready to run on {check.get("engine")}')
            self.statusChip.setRole('ok')
        else:
            engine = check.get('engine')
            self.statusChip.setText(f'Cannot run on {engine}' if engine else 'Cannot run')
            self.statusChip.setRole('bad')
        problems = check.get('problems') or []
        if self._mode() != 'empty' and problems:
            self._setNotice(_first_reason(problems[0]), problems[0])
        _clear_layout(self.facts)
        facts = check.get('facts') or {}
        if facts.get('steps') is not None:
            self.facts.addWidget(Chip(f'{facts["steps"]} steps'))
        if facts.get('duration_s') is not None:
            self.facts.addWidget(Chip(duration_text(facts['duration_s'])))
        for detector, frames in (facts.get('frames') or {}).items():
            self.facts.addWidget(Chip(f'{frames} frames, {detector}'))
        if facts.get('changes'):
            # what the engine plays differently from the request: the one
            # thing to know before Run, beside the exact-timing chip
            changes = facts['changes']
            chip = Chip(f'{changes} time{"s" if changes != 1 else ""} played differently', 'warn')
            chip.setToolTip('Times the engine rounds, skips or moves: the check lists each.')
            chip.clicked.connect(self._showCheck)
            self.facts.addWidget(chip)
        if facts.get('grid_s'):
            # the policy: the program's, else the panel's own choice (a
            # program that does not parse has none yet)
            policy = facts.get('policy') or self.policyPar.currentText()
            clock = f'{duration_text(facts["grid_s"])} clock'
            if not policy:
                chip = Chip(clock)
                chip.setToolTip('The engine\'s clock: every time is played on it.')
            else:
                exact = policy == 'strict-v1'
                chip = Chip(f'{"exact" if exact else "rounded"} timing, {clock}')
                chip.setToolTip(f'Timing policy {policy}: '
                                + ('every time sits on the engine\'s clock.' if exact else
                                   'times are rounded as the policy says; the check lists '
                                   'them.'))
            self.facts.addWidget(chip)

    def _setNotice(self, text, tooltip=''):
        metrics = self.noticeLabel.fontMetrics()
        width = max(self.width() - 4 * metrics.averageCharWidth(), metrics.averageCharWidth())
        self.noticeLabel.setText(metrics.elidedText(text, QtCore.Qt.ElideRight, width))
        self.noticeLabel.setToolTip(tooltip or text)
        self.noticeLabel.setVisible(bool(text))

    def showNotice(self, text):
        """Say one thing under the header until the next check."""
        self._setNotice(text)

    def _showCheck(self):
        if self.tablesButton.isChecked():
            self.tablesButton.setChecked(False)
        self.scrollArea.ensureWidgetVisible(self.check)

    def setCheck(self, check: Dict):
        """What the engine makes of the program (the controller's report, as
        data): ``runnable``, ``engine``, ``title``, ``preset`` (the template
        it was made from, if any) and ``edited``, ``problems``, ``verdicts``
        ``[(verdict, feature, reason)]``, ``notes``, ``recorded``, ``facts``
        (``steps``, ``duration_s``, ``frames``, ``grid_s``, ``policy``) and,
        for a path, ``trajectory`` (``text``, ``events``, ``preset``)."""
        self._check = dict(check or {})
        grid = (self._check.get('facts') or {}).get('grid_s')
        if grid:
            self._lastGrid = grid
        self._setNotice('')
        # an empty scan is not a scan the engine refused: no verdict for it
        shown = dict(self._check)
        if self._mode() == 'empty':
            shown.pop('runnable', None)
        self.check.setCheck(shown)
        self.recording.setLines(self._check.get('recorded') or [])
        self.refreshParts()
        if self._check.get('runnable') and self.preview.live.isChecked() \
                and self.preview.drawn() and self._mode() != 'empty':
            self.sigPlotClicked.emit()

    def setReport(self, text):
        self.report.setPlainText(text)

    def reportText(self):
        return self.report.toPlainText()

    def askPresetParams(self, preset, initial, validate, *, positions=None,
                        setup=None) -> Optional[Dict]:
        """The template's form; ``positions``: each positioner's tracked
        position (µm), ``setup``: the setup's declared ``scanDesignerParams``
        -- what fills an empty field in, never a guess."""
        dialog = PresetDialog(preset, self._devices, initial or {}, validate, self,
                              positions=positions, setup=setup)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.params()
        return None

    def askApproveScript(self, code) -> bool:
        """Show a trajectory script a file brought and ask whether it may run."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('Run this trajectory script?')
        box.setText('This scan program holds a trajectory script: Python code that runs '
                    'with your permissions, unsandboxed, whenever the program is checked, '
                    'plotted or run.')
        box.setInformativeText('Run it only if you know where it comes from. Approved code '
                               'is remembered on this computer and does not ask again; changed '
                               'code asks again. The code is under Show Details.')
        box.setDetailedText(str(code))
        approve = box.addButton('Approve and run', QtWidgets.QMessageBox.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        box.exec_()
        return box.clickedButton() is approve

    def showTraces(self, time_s, traces: Dict[str, object], *, analog=(), windows=None,
                   note=''):
        """Every device's trace on its own lane; ``windows``: each zoom's
        (start_s, end_s) -- one step, one pass, all of it -- else the whole."""
        self.preview.setWindows(*(windows or (None, None, None)))
        colors = {name: self.deviceColor(name) for name in traces}
        self.preview.showTraces(time_s, traces, colors, analog=analog, note=note)

    def showPreviewProblem(self, text):
        """Why the preview is not drawn, where the drawing would be."""
        self.preview.showProblem(text)

    def showScanProgress(self, fraction, text):
        """The running scan's progress; ``fraction`` None: not known."""
        if fraction is None:
            self.scanProgressBar.setRange(0, 0)          # busy
        else:
            self.scanProgressBar.setRange(0, _PROGRESS_RANGE)
            self.scanProgressBar.setValue(
                int(round(_PROGRESS_RANGE * min(max(float(fraction), 0.0), 1.0))))
        self.scanProgressLabel.setText(text)
        self.progressRow.setVisible(True)
        if not self._running:
            self._running = True
            self._refreshHeader()

    def hideScanProgress(self):
        self.scanProgressBar.setRange(0, _PROGRESS_RANGE)
        self.scanProgressBar.setValue(0)
        self.scanProgressLabel.clear()
        self.progressRow.setVisible(False)
        if self._running:
            self._running = False
            self._refreshHeader()

    # -- SuperScanWidget ----------------------------------------------------------

    def getTTLIncluded(self, deviceName):
        return any(row['device'] == deviceName for row in self.timeline.rows(raw=True))

    def setScanMode(self):
        pass

    def getSeqTimePar(self):
        step = self.stepPar.text().strip()
        return stored(step, MS) if step else 0.0

    def unsetTTL(self, deviceName):
        pass

    def setScanCenterPos(self, positionerName, centerPos):
        # Expert positions are the program's offsets; nothing to center.
        pass


def _first_reason(problem: str) -> str:
    """The first reason of a problem: its first line, or the first item
    under a heading that ends in a colon (*The scan cannot run yet:*)."""
    lines = [line for line in str(problem).splitlines() if line.strip()]
    if not lines:
        return ''
    if lines[0].rstrip().endswith(':') and len(lines) > 1:
        return lines[1].strip().lstrip('- ').strip()
    return lines[0]


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
