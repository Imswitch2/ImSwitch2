import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from . import guitools


class ReconstructionView(QtWidgets.QFrame):
    """ Frame for showing the reconstructed image"""

    # Signals
    sigItemSelected = QtCore.Signal()
    sigAxisStepChanged = QtCore.Signal(tuple)
    sigViewChanged = QtCore.Signal()

    # Methods
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Image Widget
        naparitools.addNapariGrayclipColormap()
        self.napariViewer = naparitools.EmbeddedNapari()
        self.napariViewer.dims.events.connect(self.dimsChanged)
        naparitools.NapariUpdateLevelsWidget.addToViewer(self.napariViewer)

        self.imgLayer = self.napariViewer.add_image(
            np.zeros((1, 1)), rgb=False, name='Reconstruction', colormap='grayclip', protected=True
        )

        # Button group for choosing view
        self.chooseViewGroup = QtWidgets.QButtonGroup()
        self.chooseViewBox = QtWidgets.QGroupBox('Choose view')
        self.viewLayout = QtWidgets.QVBoxLayout()

        self.standardView = QtWidgets.QRadioButton('Standard view')
        self.standardView.viewName = 'standard'
        self.chooseViewGroup.addButton(self.standardView)
        self.viewLayout.addWidget(self.standardView)

        self.bottomView = QtWidgets.QRadioButton('Bottom side view')
        self.bottomView.viewName = 'bottom'
        self.chooseViewGroup.addButton(self.bottomView)
        self.viewLayout.addWidget(self.bottomView)

        self.leftView = QtWidgets.QRadioButton('Left side view')
        self.leftView.viewName = 'left'
        self.chooseViewGroup.addButton(self.leftView)
        self.viewLayout.addWidget(self.leftView)

        self.chooseViewBox.setLayout(self.viewLayout)
        self.chooseViewGroup.buttonClicked.connect(self.sigViewChanged)

        # List for storing sevral data sets
        self.reconList = QtWidgets.QListWidget()
        self.reconList.currentItemChanged.connect(self.sigItemSelected)
        self.reconList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        removeReconBtn = guitools.BetterPushButton('Remove current')
        removeReconBtn.clicked.connect(self.removeRecon)
        removeAllReconBtn = guitools.BetterPushButton('Remove all')
        removeAllReconBtn.clicked.connect(self.removeAllRecon)

        # Set initial states
        self.standardView.setChecked(True)
        self._defaultViewLabels = [
            (self.standardView, 'Standard view', 'standard'),
            (self.bottomView, 'Bottom side view', 'bottom'),
            (self.leftView, 'Left side view', 'left'),
        ]

        # Set layout
        layout = QtWidgets.QGridLayout()

        self.setLayout(layout)

        layout.addWidget(self.napariViewer.get_widget(), 0, 0, 4, 1)
        layout.addWidget(self.chooseViewBox, 0, 1, 1, 2)
        layout.addWidget(self.reconList, 0, 3, 2, 1)
        layout.addWidget(removeReconBtn, 2, 3)
        layout.addWidget(removeAllReconBtn, 3, 3)

        layout.setRowStretch(1, 1)
        layout.setColumnStretch(0, 100)
        layout.setColumnStretch(2, 5)

    def dimsChanged(self, event):
        if event.type == 'current_step':
            self.sigAxisStepChanged.emit(event.value)

    def addNewData(self, reconObj, name):
        ind = 0
        for i in range(self.reconList.count()):
            if name + '.' + str(ind) == self.reconList.item(i).data(0):
                ind += 1
        name = name + '.' + str(ind)

        listItem = QtWidgets.QListWidgetItem(name)
        listItem.setData(1, reconObj)
        self.reconList.addItem(listItem)
        self.reconList.setCurrentItem(listItem)

    def getCurrentItemIndex(self):
        return self.reconList.indexFromItem(self.reconList.currentItem()).row()

    def getDataAtIndex(self, index):
        return self.reconList.item(index).data(1)

    def getCurrentItemData(self):
        currentItem = self.reconList.currentItem()
        return currentItem.data(1) if currentItem is not None else None

    def getAllItemDatas(self):
        for i in range(self.reconList.count()):
            item = self.reconList.item(i)
            yield item.text(), item.data(1)

    def getViewName(self):
        return self.chooseViewGroup.checkedButton().viewName

    def setViewModes(self, viewModes):
        if not viewModes:
            for button, text, view_name in self._defaultViewLabels:
                button.setText(text)
                button.viewName = view_name
                button.setVisible(True)
            self.standardView.setChecked(True)
            return

        buttons = [self.standardView, self.bottomView, self.leftView]
        for index, button in enumerate(buttons):
            if index < len(viewModes):
                mode = viewModes[index]
                button.setText(mode.name)
                button.viewName = mode.name
                button.setVisible(True)
            else:
                button.setVisible(False)
        buttons[0].setChecked(True)

    def getImage(self):
        return self.imgLayer.data

    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px"):
        self.imgLayer.data = im
        self.napariViewer.dims.axis_labels = tuple(axisLabels)
        if axisScales is None:
            axisScales = [1.0 for _ in range(np.asarray(im).ndim)]
        try:
            self.imgLayer.scale = tuple(axisScales)
            self.imgLayer.metadata["scale_unit"] = scaleUnit
            self.napariViewer.scale_bar.unit = "µm" if scaleUnit == "um" else scaleUnit
        except Exception:
            pass

    def clearImage(self):
        self.imgLayer.data = np.zeros((1, 1))

    def getImageDisplayLevels(self):
        return self.imgLayer.contrast_limits

    def setImageDisplayLevels(self, minimum, maximum):
        self.imgLayer.contrast_limits = (minimum, maximum)

    def setImageDisplayLevelsRange(self, minimum, maximum):
        self.imgLayer.contrast_limits_range = (minimum, maximum)

    def removeRecon(self):
        numSelected = len(self.reconList.selectedIndexes())
        while not numSelected == 0:
            row = self.reconList.selectedIndexes()[0].row()
            self.reconList.takeItem(row)
            numSelected -= 1

    def removeAllRecon(self):
        for i in range(self.reconList.count()):
            currRow = self.reconList.currentRow()
            self.reconList.takeItem(currRow)

    def resetView(self):
        self.napariViewer.reset_view()


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
