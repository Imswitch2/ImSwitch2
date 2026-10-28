from qtpy import QtCore, QtWidgets

from .guitools import BetterPushButton

_COLUMNS = ('Name', 'Type', 'Shape', 'dtype', 'Size', 'Value')
_EMPTY = 'Variables of Run cell, Run selection and the console appear here.'


class VariablesView(QtWidgets.QWidget):
    """ The variables of the session: name, type, shape, dtype, size in
    memory and a short value. An image (an array of two or more dimensions)
    can be shown in ImControl's image view. """

    sigShowRequested = QtCore.Signal(str)  # (name)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS) - 1):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda item: self._requestShow(item.row()))
        self.table.itemSelectionChanged.connect(self._updateShowButton)
        layout.addWidget(self.table, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.statusLabel = QtWidgets.QLabel(_EMPTY)
        self.statusLabel.setWordWrap(True)
        bottom.addWidget(self.statusLabel, 1)
        self.showButton = BetterPushButton('Show image')
        self.showButton.setToolTip(
            "Shows the selected array in ImControl's image view (double-click "
            'a row does the same).'
        )
        self.showButton.clicked.connect(lambda: self._requestShow(self.table.currentRow()))
        bottom.addWidget(self.showButton, 0)
        layout.addLayout(bottom)

        self._showable = {}
        self._running = False
        self._updateShowButton()

    def setVariables(self, rows):
        """ rows: (name, description, showable) per variable, the
        description as describeValue() gives it. """
        selected = self.selectedName()
        self.table.setRowCount(len(rows))
        self._showable = {}
        for row, (name, info, showable) in enumerate(rows):
            values = (name, info['type'], info['shape'], info['dtype'],
                      info['size'], info['value'])
            for column, text in enumerate(values):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 4:
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.table.setItem(row, column, item)
            self._showable[name] = showable
            if name == selected:
                self.table.selectRow(row)
        self.setMessage('' if rows else _EMPTY)
        self._updateShowButton()

    def selectedName(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def setRunning(self, running):
        """ The table is not updated while a script runs (it may be changing
        the variables); it says so. """
        self._running = bool(running)
        self.table.setEnabled(not running)
        if running:
            self.setMessage('Updated when the script has ended.')
        self._updateShowButton()

    def setMessage(self, text):
        self.statusLabel.setText(text)
        self.statusLabel.setVisible(bool(text))

    def _requestShow(self, row):
        item = self.table.item(row, 0) if row >= 0 else None
        if item is not None and not self._running and self._showable.get(item.text()):
            self.sigShowRequested.emit(item.text())

    def _updateShowButton(self):
        name = self.selectedName()
        self.showButton.setEnabled(
            not self._running and bool(name and self._showable.get(name))
        )


# Copyright (C) 2020-2021 ImSwitch developers
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
