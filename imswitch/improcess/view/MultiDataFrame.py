from qtpy import QtCore, QtGui, QtWidgets

from .guitools import BetterPushButton


class MultiDataFrame(QtWidgets.QFrame):
    # Signals
    sigAddDataClicked = QtCore.Signal()
    sigLoadCurrentDataClicked = QtCore.Signal()
    sigLoadAllDataClicked = QtCore.Signal()
    sigUnloadCurrentDataClicked = QtCore.Signal()
    sigUnloadAllDataClicked = QtCore.Signal()
    sigDeleteCurrentDataClicked = QtCore.Signal()
    sigDeleteAllDataClicked = QtCore.Signal()
    sigSaveCurrentDataClicked = QtCore.Signal()
    sigSaveAllDataClicked = QtCore.Signal()
    sigSetAsCurrentDataClicked = QtCore.Signal()
    sigSelectedItemChanged = QtCore.Signal()

    # Methods
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.dataList = QtWidgets.QListWidget()
        self.dataList.currentItemChanged.connect(self.sigSelectedItemChanged)
        self.dataList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

        self.dataLoadedStatus = QtWidgets.QLabel('Data loaded: —')
        self.dataLoadedStatus.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self.dataLoadedStatus.setStyleSheet('color: palette(mid); font-size: 9pt;')

        # Standalone actions (no Selected/All twin)
        self.addDataBtn = BetterPushButton('Add data')
        self.addDataBtn.clicked.connect(self.sigAddDataClicked)
        self.setDataBtn = BetterPushButton('Set as current data')
        self.setDataBtn.clicked.connect(self.sigSetAsCurrentDataClicked)

        # Paired actions collapse into one QToolButton each, with a popup
        # menu offering 'Selected' and 'All' variants. Keep the original
        # button attributes as QActions so the existing set*Enabled helpers
        # in this widget (and any external callers) keep working.
        loadBtn, self.loadCurrDataBtn, self.loadAllDataBtn = self._makeMenuButton(
            'Load',
            'Selected', self.sigLoadCurrentDataClicked,
            'All',      self.sigLoadAllDataClicked,
        )
        saveBtn, self.saveDataBtn, self.saveAllDataBtn = self._makeMenuButton(
            'Save',
            'Selected', self.sigSaveCurrentDataClicked,
            'All',      self.sigSaveAllDataClicked,
        )
        unloadBtn, self.unloadDataBtn, self.unloadAllDataBtn = self._makeMenuButton(
            'Unload',
            'Selected', self.sigUnloadCurrentDataClicked,
            'All',      self.sigUnloadAllDataClicked,
        )
        removeBtn, self.delDataBtn, self.delAllDataBtn = self._makeMenuButton(
            'Remove',
            'Selected', self.sigDeleteCurrentDataClicked,
            'All',      self.sigDeleteAllDataClicked,
        )

        # Two-pane layout: list on the left, single column of compact controls
        # on the right. The old 11-button grid was hard to scan and had a
        # duplicate addWidget for Unload-all.
        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(4)
        self.setLayout(layout)

        layout.addWidget(self.dataList, 0, 0, -1, 1)

        layout.addWidget(self.dataLoadedStatus, 0, 1)
        layout.addWidget(self.addDataBtn, 1, 1)
        layout.addWidget(self.setDataBtn, 2, 1)
        layout.addWidget(loadBtn, 3, 1)
        layout.addWidget(saveBtn, 4, 1)
        layout.addWidget(unloadBtn, 5, 1)
        layout.addWidget(removeBtn, 6, 1)
        layout.setRowStretch(7, 1)
        layout.setColumnStretch(0, 1)

    def _makeMenuButton(self, title, label_a, signal_a, label_b, signal_b):
        """Build a single QToolButton that pops up two QActions.

        Returns ``(button, action_a, action_b)``. Each QAction is fully
        responsible for invoking the matching signal, so external code can
        toggle the actions individually via ``setEnabled`` exactly like the
        old buttons.
        """
        button = QtWidgets.QToolButton()
        button.setText(f'{title} ▾')
        button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        button.setSizePolicy(
            QtWidgets.QSizePolicy.MinimumExpanding, QtWidgets.QSizePolicy.Fixed
        )

        action_a = QtWidgets.QAction(label_a, self)
        action_a.triggered.connect(signal_a)
        button.addAction(action_a)

        action_b = QtWidgets.QAction(label_b, self)
        action_b.triggered.connect(signal_b)
        button.addAction(action_b)

        return button, action_a, action_b

    def requestFilePathsFromUser(self, defaultFolder=None):
        return QtWidgets.QFileDialog().getOpenFileNames(directory=defaultFolder)[0]

    def requestDeleteSelectedConfirmation(self):
        result = QtWidgets.QMessageBox.question(
            self, 'Remove selected?', 'Remove the selected item?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        return result == QtWidgets.QMessageBox.Yes

    def requestDeleteAllConfirmation(self):
        result = QtWidgets.QMessageBox.question(
            self, 'Remove all?', 'Remove all items?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        return result == QtWidgets.QMessageBox.Yes

    def requestOverwriteConfirmation(self, name):
        result = QtWidgets.QMessageBox.question(
            self, 'Overwrite file?', f'A file named {name} already exists. Overwrite it?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        return result == QtWidgets.QMessageBox.Yes

    def addDataObj(self, name, datasetName, dataObj):
        listItem = QtWidgets.QListWidgetItem('')
        listItem.setData(1, dataObj)
        listItem.setData(3, name)
        listItem.setData(4, datasetName)
        listItem.setText(self.getTextForItem(listItem))
        self.dataList.addItem(listItem)
        self.dataList.setCurrentItem(listItem)

    def setDataObjMemoryFlag(self, dataObj, inMemory):
        for i in range(self.dataList.count()):
            item = self.dataList.item(i)
            if item.data(1) == dataObj:
                itemText = self.getTextForItem(item)
                if inMemory:
                    itemText += ' (MEMORY)'
                item.setText(itemText)

    def getTextForItem(self, item):
        name = item.data(3)
        datasetName = item.data(4)

        text = f'{name}: {datasetName}' if datasetName is not None else name

        duplicateNum = item.data(5)
        if duplicateNum is None:
            duplicateNum = 0
            for i in range(self.dataList.count()):
                otherItem = self.dataList.item(i)
                if (item is not otherItem and name == otherItem.data(3)
                        and datasetName == otherItem.data(4) and duplicateNum <= otherItem.data(5)):
                    duplicateNum += 1
            item.setData(5, duplicateNum)
        if duplicateNum > 0:
            text = f'{name} [{duplicateNum}]: {datasetName}' if datasetName is not None else name

        return text

    def getSelectedDataObj(self):
        currentItem = self.dataList.currentItem()
        return self.dataList.currentItem().data(1) if currentItem is not None else None

    def getSelectedDataObjs(self):
        for i in range(self.dataList.count()):
            if self.dataList.item(i).isSelected():
                yield self.dataList.item(i).data(1)

    def getAllDataObjs(self):
        for i in range(self.dataList.count()):
            yield self.dataList.item(i).data(1)

    def delDataByDataObj(self, dataObj):
        for i in reversed(range(self.dataList.count())):
            if self.dataList.item(i) is not None and self.dataList.item(i).data(1) is dataObj:
                self.dataList.takeItem(i)

    def setCurrentRowHighlighted(self, highlighted):
        self.dataList.currentItem().setBackground(
            QtGui.QColor('green' if highlighted else 'transparent')
        )

    def setAllRowsHighlighted(self, highlighted):
        for i in range(self.dataList.count()):
            self.dataList.item(i).setBackground(
                QtGui.QColor('green' if highlighted else 'transparent')
            )

    def setLoadedStatusText(self, text):
        text = (text or '').strip() or '—'
        self.dataLoadedStatus.setText(f'Data loaded: {text}')

    def setAddButtonEnabled(self, value):
        self.addDataBtn.setEnabled(value)

    def setSetCurrentButtonEnabled(self, value):
        self.setDataBtn.setEnabled(value)

    def setLoadButtonEnabled(self, value):
        self.loadCurrDataBtn.setEnabled(value)

    def setLoadAllButtonEnabled(self, value):
        self.loadAllDataBtn.setEnabled(value)

    def setUnloadButtonEnabled(self, value):
        self.unloadDataBtn.setEnabled(value)

    def setUnloadAllButtonEnabled(self, value):
        self.unloadAllDataBtn.setEnabled(value)

    def setDeleteButtonEnabled(self, value):
        self.delDataBtn.setEnabled(value)

    def setDeleteAllButtonEnabled(self, value):
        self.delAllDataBtn.setEnabled(value)

    def setSaveButtonEnabled(self, value):
        self.saveDataBtn.setEnabled(value)

    def setSaveAllButtonEnabled(self, value):
        self.saveAllDataBtn.setEnabled(value)


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
