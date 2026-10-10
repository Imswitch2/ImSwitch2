"""The Expert scan panel (docs/design/plans/scan-engines-and-expert-scan.md §4,
M3): the whole scan program, edited as tables.

- **Loops**, outermost first: id, kind, count, traversal, parity loops.
- **Drives**: a device's position as an offset plus a weight per loop
  (µm per count), or a table indexed by one loop; and how it moves.
- **Timeline**: what devices do inside every step, in ms from its start.
- **Waits** and **detector bindings**.

The panel edits the program's JSON form (``scan_program.serialize``); the
controller turns it into a program, and the report beside the tables says
what the scan engine makes of it. Times are shown in ms, positions in µm.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional, Sequence

import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

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


class _RowTable(QtWidgets.QWidget):
    """A table of rows of plain values, with add, remove and move buttons."""

    sigEdited = QtCore.Signal()

    def __init__(self, columns: Sequence[_Column], parent=None):
        super().__init__(parent)
        self._columns = list(columns)
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
        where = f'{column.label} in row {row + 1}'
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
            raise ValueError(f'{where}: {text!r} is not a {column.kind.replace("-", " ")}') \
                from None
        return text

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
    """A preset's parameters as a form; OK only when they make a program."""

    def __init__(self, preset, devices: Dict[str, Sequence[str]], initial: Dict,
                 validate: Callable[[Dict], str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(preset.title)
        self._preset, self._devices, self._validate = preset, devices, validate
        self._editors = {}
        form = QtWidgets.QFormLayout()
        for field in preset.fields:
            editor = self._editor(field, initial.get(field.name))
            if field.help:
                editor.setToolTip(field.help)
            unit = {'seconds': ' (ms)', 'micrometres': ' (µm)'}.get(field.kind, '')
            form.addRow(field.label + unit, editor)
            self._editors[field.name] = (field, editor)
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


class ScanWidgetExpert(SuperScanWidget):
    """The Expert scan panel."""

    sigProgramEdited = QtCore.Signal()
    sigPresetRequested = QtCore.Signal(str)
    sigPlotClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._devices: Dict[str, List[str]] = {}
        self.loops = _RowTable([
            _Column('id', 'Id'), _Column('kind', 'Kind', 'choice', LOOP_KINDS),
            _Column('count', 'Count', 'int'),
            _Column('traversal', 'Order', 'choice', TRAVERSALS),
            _Column('parity_loops', 'Alternates with', 'names')])
        self.drives = _RowTable(self._driveColumns(()))
        self.timeline = _RowTable(self._timelineColumns())
        self.waits = _RowTable(self._waitColumns(()))
        self.bindings = _RowTable(self._bindingColumns())
        self.tabs = QtWidgets.QTabWidget()
        for name, table in (('Loops', self.loops), ('Drives', self.drives),
                            ('Timeline', self.timeline), ('Waits', self.waits),
                            ('Detectors', self.bindings)):
            self.tabs.addTab(table, name)
        self.loops.sigEdited.connect(self._loopsEdited)
        for table in (self.drives, self.timeline, self.waits, self.bindings):
            table.sigEdited.connect(self.sigProgramEdited)

        self.stepPar = QtWidgets.QLineEdit('')
        self.stepPar.setToolTip('The length of every innermost step, in ms.')
        self.stepPar.textChanged.connect(lambda _t: self.sigProgramEdited.emit())
        self.policyPar = QtWidgets.QComboBox()
        self.policyPar.setToolTip(
            'How requested times meet the engine\'s clock. strict-v1: every time must sit on '
            'it, and one that does not is refused with its neighbours.')
        self.policyPar.currentIndexChanged.connect(lambda _i: self.sigProgramEdited.emit())
        self.presetPar = QtWidgets.QComboBox()
        self.presetButton = QtWidgets.QPushButton('Make…')
        self.presetButton.setToolTip('Make a program from a preset\'s parameters.')
        self.presetButton.clicked.connect(
            lambda: self.sigPresetRequested.emit(self.presetPar.currentData() or ''))
        self.plotButton = QtWidgets.QPushButton('Plot')
        self.plotButton.clicked.connect(self.sigPlotClicked)
        self.engineLabel = QtWidgets.QLabel('')
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(0)
        self.plot = pg.PlotWidget()
        self.plot.addLegend()
        self.plot.setLabel('bottom', 'time', units='s')
        self.plot.setVisible(False)
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

    # -- the panel ------------------------------------------------------------

    def initControls(self, positionerNames, TTLDeviceNames, TTLTimeUnits=None, *,
                     detectorNames=(), presets=(), policies=(), engine=''):
        self._devices = {'positioner': list(positionerNames), 'laser': list(TTLDeviceNames),
                         'detector': list(detectorNames)}
        row = 0
        self.grid.addWidget(self.scanButton, row, 0)
        self.grid.addWidget(self.repeatBox, row, 1)
        self.grid.addWidget(self.loadScanBtn, row, 2)
        self.grid.addWidget(self.saveScanBtn, row, 3)
        self.grid.addItem(QtWidgets.QSpacerItem(20, 20, QtWidgets.QSizePolicy.Expanding,
                                                QtWidgets.QSizePolicy.Minimum), row, 4)
        self.grid.addWidget(self.presetPar, row, 5)
        self.grid.addWidget(self.presetButton, row, 6)
        self.grid.addWidget(self.plotButton, row, 7)
        row += 1
        self.grid.addWidget(self.engineLabel, row, 0, 1, 8)
        row += 1
        self.grid.addWidget(self.progressRow, row, 0, 1, 8)
        row += 1
        self.grid.addWidget(self.tabs, row, 0, 1, 8)
        row += 1
        self.grid.addWidget(QtWidgets.QLabel('Step (ms):'), row, 0)
        self.grid.addWidget(self.stepPar, row, 1)
        self.grid.addWidget(QtWidgets.QLabel('Timing:'), row, 2)
        self.grid.addWidget(self.policyPar, row, 3, 1, 2)
        row += 1
        self.grid.addWidget(self.report, row, 0, 1, 8)
        row += 1
        self.grid.addWidget(self.plot, row, 0, 1, 8)
        for preset_id, title in presets:
            self.presetPar.addItem(title, preset_id)
        self.policyPar.addItems(list(policies))
        self.engineLabel.setText(engine)
        self.drives.setColumns(self._driveColumns(()))
        self.timeline.setColumns(self._timelineColumns())
        self.bindings.setColumns(self._bindingColumns())

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
        devices = tuple(self._devices.get('laser', ()) + self._devices.get('detector', ())
                        + self._devices.get('positioner', ()))
        return [_Column('device', 'Device', 'choice', devices),
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

    def _loopsEdited(self):
        ids = self._loopIds()
        current = [c.key[2:] for c in self.drives.columns() if c.key.startswith('w:')]
        if ids != current:
            rows = self.drives.rows(raw=True)
            self.drives.setColumns(self._driveColumns(ids))
            self.drives.setRows([])
            for row in rows:
                self.drives.addRow(row, raw=True)
            self.waits.setColumns(self._waitColumns(ids))
        self.sigProgramEdited.emit()

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

    def programDict(self) -> Dict:
        """The program the tables describe, in its JSON form (without
        ``format``); a cell that does not parse raises ValueError naming it."""
        loops = []
        for row in self.loops.rows():
            loops.append({'id': row['id'], 'kind': row['kind'], 'count': row['count'],
                          'traversal': row['traversal'],
                          'parity_loops': row['parity_loops']})
        motion_keys = ('transition', 'move_s', 'settle_s', 'placement', 'return_s', 'origin',
                       'restore')
        drives = []
        for row in self.drives.rows():
            drives.append({
                'device': row['device'], 'offset_um': row['offset_um'],
                'weights_um': {k[2:]: v for k, v in row.items()
                               if k.startswith('w:') and v is not None},
                'table_loop': row['table_loop'], 'table_um': row['table_um'],
                'motion': {k: row[k] for k in motion_keys}})
        timeline = []
        for row in self.timeline.rows():
            event = dict(row)
            event['anchor'] = event['anchor'] or 'step'
            timeline.append(event)
        step = self.stepPar.text().strip()
        try:
            step_s = stored(step, MS)
        except ValueError:
            raise ValueError(f'Step: {step!r} is not a number of ms') from None
        return {'loops': loops, 'drives': drives, 'step_s': step_s, 'timeline': timeline,
                'waits': self.waits.rows(), 'detectors': self.bindings.rows(),
                'timing_policy': self.policyPar.currentText()}

    # -- what the controller shows ----------------------------------------------

    def setReport(self, text):
        self.report.setPlainText(text)

    def reportText(self):
        return self.report.toPlainText()

    def askPresetParams(self, preset, initial, validate) -> Optional[Dict]:
        dialog = PresetDialog(preset, self._devices, initial or {}, validate, self)
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

    def showTraces(self, time_s, traces: Dict[str, object]):
        self.plot.clear()
        for index, (name, values) in enumerate(traces.items()):
            self.plot.plot(time_s, values, name=name, pen=pg.intColor(index, len(traces)))
        self.plot.setVisible(True)

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

    def hideScanProgress(self):
        self.scanProgressBar.setRange(0, _PROGRESS_RANGE)
        self.scanProgressBar.setValue(0)
        self.scanProgressLabel.clear()
        self.progressRow.setVisible(False)

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
