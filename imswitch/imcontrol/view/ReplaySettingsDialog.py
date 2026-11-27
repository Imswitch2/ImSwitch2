"""Confirmation and remapping dialog for loading settings from a recording.

Shown every time a recording's settings are loaded into the running setup
(File -> Load parameters from saved HDF5 file / Zarr store).  It lists, by
category, every device the file refers to and every stored value with the
current value beside it, lets the user pick a replacement for a device this
setup does not have (or skip it), decide what to do with out-of-range values
(skip, clamp to the range, or type an in-range value) and opt in to moving
stages and rotators.  Nothing reaches hardware until **Apply**.

The dialog only edits the :class:`~imswitch.imcontrol.model.replay.ReplayPlan`
it is given; the controller applies it.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcontrol.model import replay

_SKIP_LABEL = 'Skip'
_ACTION_LABELS = {
    replay.APPLY: 'Apply',
    replay.SKIP: 'Skip',
    replay.CLAMP: 'Clamp to range',
    replay.EDIT: 'Edit value…',
}

COL_CATEGORY, COL_DEVICE, COL_STATUS, COL_TARGET, COL_VALUE, COL_CURRENT, \
    COL_STORED, COL_ACTION, COL_APPLIED, COL_NOTE = range(10)
_HEADERS = ['Category', 'Recorded device', 'Match', 'Use device', 'Value',
            'Current', 'Recorded', 'Action', 'Will apply', 'Note']

_OUT_OF_RANGE_COLOR = QtGui.QColor(255, 196, 140)
_INVALID_COLOR = QtGui.QColor(255, 170, 170)
_SHOWN_COLOR = QtGui.QColor(225, 225, 225)


def _fmt(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:g}'
    if isinstance(value, (list, dict)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    return text if len(text) <= 60 else text[:57] + '...'


class ReplaySettingsDialog(QtWidgets.QDialog):
    """Modal dialog that shows a replay plan and collects the user's choices."""

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Load settings from recording')
        self.setModal(True)
        self.resize(1100, 560)
        self._plan: Optional[replay.ReplayPlan] = None
        self._rows: List[tuple] = []  # (itemId, valueKey or None)
        self._rebuilding = False

        self.sourceLabel = QtWidgets.QLabel()
        self.sourceLabel.setWordWrap(True)
        self.sourceLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.table = QtWidgets.QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.movesCheck = QtWidgets.QCheckBox(
            'Move stages and rotators to the recorded positions')
        self.movesCheck.setChecked(False)
        self.movesCheck.toggled.connect(self._onMovesToggled)

        self.rememberCheck = QtWidgets.QCheckBox(
            'Remember my device choices for recordings from this setup '
            '(pre-fills this dialog next time)')
        self.rememberCheck.setChecked(False)

        self.safetyLabel = QtWidgets.QLabel(
            'Replay never switches a laser on. Out-of-range values are skipped unless '
            'you choose to clamp or edit them.')
        self.safetyLabel.setWordWrap(True)

        self.summaryLabel = QtWidgets.QLabel()
        self.summaryLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(QtCore.Qt.Horizontal, self)
        self.applyButton = self.buttons.addButton('Apply', QtWidgets.QDialogButtonBox.AcceptRole)
        self.cancelButton = self.buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.sourceLabel)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.movesCheck)
        layout.addWidget(self.rememberCheck)
        layout.addWidget(self.safetyLabel)
        layout.addWidget(self.summaryLabel)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------ API

    def askToApply(self, plan: replay.ReplayPlan, *, canRemember: bool = False) -> bool:
        """Show ``plan`` modally; True when the user pressed Apply."""
        self.setPlan(plan, canRemember=canRemember)
        return self.exec_() == QtWidgets.QDialog.Accepted

    def rememberRequested(self) -> bool:
        return self.rememberCheck.isVisible() and self.rememberCheck.isChecked()

    def setPlan(self, plan: replay.ReplayPlan, *, canRemember: bool = False) -> None:
        self._plan = plan
        plan.allowMoves = False
        self.movesCheck.blockSignals(True)
        self.movesCheck.setChecked(False)
        self.movesCheck.blockSignals(False)
        self.movesCheck.setVisible(any(
            v.requiresMoves for item in plan.items for v in item.values))
        self.rememberCheck.setVisible(bool(canRemember))
        self.rememberCheck.setChecked(False)
        self.sourceLabel.setText(self._sourceText(plan))
        self._rebuild()

    def plan(self) -> Optional[replay.ReplayPlan]:
        return self._plan

    # Programmatic equivalents of the per-row editors (used by the editors
    # themselves and by tests).
    def chooseTarget(self, itemId: str, target: Optional[str]) -> None:
        self._plan.retarget(itemId, target)
        self._rebuild()

    def chooseAction(self, itemId: str, key: str, action: str, editedValue: Any = None) -> None:
        value = next(v for v in self._plan.item(itemId).values if v.key == key)
        replay.setValueAction(value, action, editedValue)
        self._rebuild()

    def setAllowMoves(self, allow: bool) -> None:
        self.movesCheck.setChecked(bool(allow))

    def rowCount(self) -> int:
        return self.table.rowCount()

    def rowFor(self, itemId: str, key: Optional[str] = None) -> int:
        for row, ref in enumerate(self._rows):
            if ref[0] == itemId and (key is None or ref[1] == key):
                return row
        raise KeyError((itemId, key))

    def cellText(self, row: int, column: int) -> str:
        widget = self.table.cellWidget(row, column)
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentText()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        item = self.table.item(row, column)
        return item.text() if item is not None else ''

    def cellColor(self, row: int, column: int) -> Optional[QtGui.QColor]:
        item = self.table.item(row, column)
        return item.background().color() if item is not None else None

    # ---------------------------------------------------------- result

    def showResult(self, result: replay.ReplayResult, *, title: str = 'Settings loaded') -> None:
        """Summary of what was applied, skipped and failed, with reasons."""
        lines = result.summaryLines()
        box = QtWidgets.QMessageBox(self.parentWidget() or self)
        box.setIcon(QtWidgets.QMessageBox.Warning if result.failed
                    else QtWidgets.QMessageBox.Information)
        box.setWindowTitle(title)
        box.setText(lines[0])
        box.setDetailedText('\n'.join(lines[1:]))
        self.lastResultBox = box
        box.exec_()

    # ---------------------------------------------------------- internals

    @staticmethod
    def _sourceText(plan: replay.ReplayPlan) -> str:
        parsed = plan.parsed
        source = parsed.sourceFile or 'recording'
        if parsed.sourceDataset:
            source += f' ({parsed.sourceDataset})'
        parts = [f'<b>{source}</b>']
        if parsed.softwareVersion:
            parts.append(f'recorded with ImSwitch {parsed.softwareVersion}')
        if parsed.setupName:
            parts.append(f'setup "{parsed.setupName}"')
        if plan.sameSetup is True:
            parts.append('same setup configuration as now')
        elif plan.sameSetup is False:
            parts.append('<b>a different setup configuration</b> '
                         f'(now: "{plan.snapshot.setupName or "?"}")')
        else:
            parts.append('the file does not identify its setup; devices matched by name')
        return ', '.join(parts) + '.'

    def _onMovesToggled(self, checked: bool) -> None:
        if self._plan is not None:
            self._plan.allowMoves = bool(checked)
            self._rebuild()

    def _rebuild(self) -> None:
        plan = self._plan
        if plan is None:
            return
        self._rebuilding = True
        self.table.setRowCount(0)
        self._rows = []
        order = {kind: i for i, kind in enumerate(replay.KIND_LABELS)}
        items = sorted(plan.items, key=lambda it: (order.get(it.kind, 99), it.storedName))
        actions = replay.buildActions(plan)
        applied = {(a.itemId, a.key): a for a in actions.actions}
        for item in items:
            values = item.values or [None]
            for index, value in enumerate(values):
                self._addRow(item, value, first=(index == 0), applied=applied, actions=actions)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(COL_NOTE, max(self.table.columnWidth(COL_NOTE), 260))
        self._rebuilding = False

        skippedCount = len(actions.skipped)
        info = len(plan.parsed.informationalKeys)
        text = (f'{len(actions.actions)} change(s) will be applied, '
                f'{skippedCount} recorded value(s) will not.')
        if info:
            text += (f' {info} further recorded value(s) describe the acquisition itself '
                     '(recording mode, timestamps, notes) and are not replayed.')
        self.summaryLabel.setText(text)
        self.summaryLabel.setToolTip('\n'.join(plan.parsed.informationalKeys[:200]))
        self.applyButton.setEnabled(bool(actions.actions))

    def _setText(self, row: int, column: int, text: str, tooltip: str = '',
                 color: Optional[QtGui.QColor] = None) -> None:
        cell = QtWidgets.QTableWidgetItem(text)
        if tooltip:
            cell.setToolTip(tooltip)
        if color is _SHOWN_COLOR:
            # Shown only: dimmed text, no fill, readable in light and dark themes.
            cell.setForeground(QtGui.QBrush(QtGui.QColor(140, 140, 140)))
        elif color is not None:
            cell.setBackground(QtGui.QBrush(color))
            cell.setForeground(QtGui.QBrush(QtGui.QColor(0, 0, 0)))
        self.table.setItem(row, column, cell)

    def _addRow(self, item: replay.ReplayItem, value: Optional[replay.ValueCheck], *,
                first: bool, applied, actions) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows.append((item.id, value.key if value is not None else None))
        color = None
        if value is not None:
            if value.status == replay.V_OUT_OF_RANGE:
                color = _OUT_OF_RANGE_COLOR
            elif value.status == replay.V_INVALID:
                color = _INVALID_COLOR
            elif value.status in (replay.V_SHOWN, replay.V_NO_TARGET):
                color = _SHOWN_COLOR

        if first:
            self._setText(row, COL_CATEGORY, replay.KIND_LABELS.get(item.kind, item.kind))
            self._setText(row, COL_DEVICE, item.storedName)
            self._setText(row, COL_STATUS, replay.STATUS_LABELS.get(item.status, item.status),
                          tooltip=item.note)
            if item.replayable:
                combo = QtWidgets.QComboBox()
                combo.addItem(_SKIP_LABEL, None)
                for candidate in item.candidates:
                    combo.addItem(candidate, candidate)
                combo.setCurrentIndex(
                    0 if item.target is None else 1 + item.candidates.index(item.target))
                combo.setToolTip(item.note)
                combo.currentIndexChanged.connect(
                    lambda _i, c=combo, itemId=item.id: self._onTargetChanged(itemId, c))
                self.table.setCellWidget(row, COL_TARGET, combo)
            else:
                self._setText(row, COL_TARGET, '—', tooltip=item.note)
        if value is None:
            self._setText(row, COL_NOTE, item.note)
            return

        self._setText(row, COL_VALUE, value.label + (f' [{value.units}]' if value.units else ''))
        self._setText(row, COL_CURRENT, _fmt(value.current))
        self._setText(row, COL_STORED, _fmt(value.stored), color=color,
                      tooltip=json.dumps(value.stored, default=str))

        allowed = value.allowedActions
        if len(allowed) > 1:
            combo = QtWidgets.QComboBox()
            for action in allowed:
                combo.addItem(_ACTION_LABELS[action], action)
            combo.setCurrentIndex(allowed.index(value.action) if value.action in allowed else 0)
            combo.currentIndexChanged.connect(
                lambda _i, c=combo, itemId=item.id, key=value.key:
                self._onActionChanged(itemId, key, c))
            self.table.setCellWidget(row, COL_ACTION, combo)
        else:
            self._setText(row, COL_ACTION, _ACTION_LABELS.get(value.action, value.action))

        action = applied.get((item.id, value.key))
        if value.action == replay.EDIT:
            edit = QtWidgets.QLineEdit('' if value.editedValue is None else str(value.editedValue))
            if value.range is not None:
                edit.setPlaceholderText(f'{value.range[0]:g} … {value.range[1]:g}')
            edit.editingFinished.connect(
                lambda e=edit, itemId=item.id, key=value.key: self._onEdited(itemId, key, e))
            self.table.setCellWidget(row, COL_APPLIED, edit)
        elif item.kind == replay.SCAN and value.status == replay.V_OK and item.target:
            scanAction = applied.get((item.id, 'parameters'))
            self._setText(row, COL_APPLIED, 'yes (with the scan)' if scanAction else '')
        else:
            self._setText(row, COL_APPLIED, _fmt(action.value) if action is not None else '')

        reason = actions.skipped.get((item.id, value.key), '') if action is None else (
            action.note or value.reason)
        if first and item.note and item.note not in reason:
            reason = f'{item.note} {reason}'.strip()
        self._setText(row, COL_NOTE, reason, tooltip=reason)

    def _onTargetChanged(self, itemId: str, combo: QtWidgets.QComboBox) -> None:
        if self._rebuilding:
            return
        target = combo.currentData()
        QtCore.QTimer.singleShot(0, lambda: self.chooseTarget(itemId, target))

    def _onActionChanged(self, itemId: str, key: str, combo: QtWidgets.QComboBox) -> None:
        if self._rebuilding:
            return
        action = combo.currentData()
        QtCore.QTimer.singleShot(0, lambda: self.chooseAction(itemId, key, action))

    def _onEdited(self, itemId: str, key: str, edit: QtWidgets.QLineEdit) -> None:
        if self._rebuilding:
            return
        text = edit.text().strip()
        try:
            number = float(text)
        except ValueError:
            number = text or None
        QtCore.QTimer.singleShot(
            0, lambda: self.chooseAction(itemId, key, replay.EDIT, number))
