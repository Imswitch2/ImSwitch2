import warnings

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
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
        self._logger = initLogger(self)

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

        # --- Left pane: napari viewer + the view-mode chooser --------------
        leftPane = QtWidgets.QWidget()
        leftLayout = QtWidgets.QGridLayout(leftPane)
        leftLayout.setContentsMargins(0, 0, 0, 0)
        leftLayout.addWidget(self.napariViewer.get_widget(), 0, 0, 4, 1)
        leftLayout.addWidget(self.chooseViewBox, 0, 1, 1, 2)
        leftLayout.setRowStretch(1, 1)
        leftLayout.setColumnStretch(0, 100)
        leftLayout.setColumnStretch(2, 5)

        # --- Right pane: reconstruction list + its two buttons -------------
        rightPane = QtWidgets.QWidget()
        rightLayout = QtWidgets.QVBoxLayout(rightPane)
        rightLayout.setContentsMargins(0, 0, 0, 0)
        rightLayout.setSpacing(2)
        rightLayout.addWidget(self.reconList, 1)
        rightLayout.addWidget(removeReconBtn)
        rightLayout.addWidget(removeAllReconBtn)
        # Keep the pane from being squeezed below readable width while still
        # allowing the user to drag it down to the handle (collapsed state).
        rightPane.setMinimumWidth(0)
        self.reconList.setMinimumWidth(80)
        self._reconListPane = rightPane

        # --- QSplitter: drag the handle left/right to resize the list, ----
        # drag fully right to snap it shut to a thin band that can be
        # dragged back to expand.
        self._reconSplitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._reconSplitter.addWidget(leftPane)
        self._reconSplitter.addWidget(rightPane)
        self._reconSplitter.setStretchFactor(0, 1)
        self._reconSplitter.setStretchFactor(1, 0)
        self._reconSplitter.setCollapsible(0, False)
        self._reconSplitter.setCollapsible(1, True)
        self._reconSplitter.setHandleWidth(8)
        # Initial size hint: ~80% viewer / 20% list. setSizes uses pixels
        # but Qt scales them to the widget's actual width on first show.
        self._reconSplitter.setSizes([800, 200])

        outerLayout = QtWidgets.QVBoxLayout(self)
        outerLayout.setContentsMargins(0, 0, 0, 0)
        outerLayout.addWidget(self._reconSplitter)

    # --- Recon list pane controls -----------------------------------------

    def toggleReconListPane(self) -> None:
        """Collapse the recon-list pane to a thin band, or restore it.

        Bound to the ``View > Reconstructions list`` menu action.  Remembers
        the last expanded width so the user gets back the size they had.
        """
        sizes = self._reconSplitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] == 0:
            restore_to = getattr(self, '_reconListLastSize', 0) or 200
            total = sum(sizes) or (restore_to + 800)
            self._reconSplitter.setSizes([total - restore_to, restore_to])
        else:
            self._reconListLastSize = sizes[1]
            total = sum(sizes)
            self._reconSplitter.setSizes([total, 0])

    def isReconListPaneCollapsed(self) -> bool:
        sizes = self._reconSplitter.sizes()
        return len(sizes) >= 2 and sizes[1] == 0

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
        im = np.asarray(im)
        old_ndim = self.imgLayer.data.ndim
        new_ndim = im.ndim
        if axisScales is None:
            axisScales = [1.0] * new_ndim

        self._logger.debug(
            "setImage: shape=%s  ndim %d→%d  labels=%s  scales=%s  unit=%s",
            im.shape, old_ndim, new_ndim,
            list(axisLabels), [f"{s:.4g}" for s in axisScales], scaleUnit,
        )

        # --- Fix napari 0.7 ndim-change bug -----------------------------------
        # VispyBaseLayer._world_to_layer_units_scale is a tuple set ONCE at
        # layer construction as (1,)*ndim and is NEVER updated when layer.data
        # ndim changes.  The set_data event fires _on_matrix_change which
        # indexes _world_to_layer_units_scale with dims_displayed for the NEW
        # ndim → IndexError when ndim grows (e.g. 2D placeholder → 3D result).
        #
        # Fix: reach into the vispy layer and pre-size the tuple to match the
        # incoming ndim before we touch layer.data so the callback is safe.
        # Access path confirmed by napari's own test suite:
        #   viewer.window._qt_viewer.canvas.layer_to_visual[layer]
        if old_ndim != new_ndim:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    canvas = self.napariViewer.window._qt_viewer.canvas
                vispy_layer = canvas.layer_to_visual[self.imgLayer]
                old_wts = vispy_layer._world_to_layer_units_scale
                vispy_layer._world_to_layer_units_scale = (1,) * new_ndim
                self._logger.debug(
                    "setImage: patched vispy _world_to_layer_units_scale  "
                    "len %d → len %d  (was %s)",
                    len(old_wts), new_ndim, old_wts,
                )
            except Exception as exc:
                self._logger.warning(
                    "setImage: could not patch vispy _world_to_layer_units_scale "
                    "(ndim %d→%d): %s — IndexError may still fire in napari callback",
                    old_ndim, new_ndim, exc,
                )

        # Set data — fires set_data → _on_matrix_change (safe now)
        self.imgLayer.data = im

        # Set scale after data so layer.ndim is already correct
        try:
            self.imgLayer.scale = tuple(axisScales)
        except Exception as exc:
            self._logger.warning("setImage: could not set layer scale %s: %s", axisScales, exc)

        try:
            self.napariViewer.dims.axis_labels = tuple(axisLabels)
        except Exception as exc:
            self._logger.warning("setImage: could not set axis_labels %s: %s", axisLabels, exc)

        try:
            self.imgLayer.metadata["scale_unit"] = scaleUnit
            self.napariViewer.scale_bar.unit = "µm" if scaleUnit == "um" else scaleUnit
        except Exception as exc:
            self._logger.debug("setImage: could not set scale_bar unit: %s", exc)

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
