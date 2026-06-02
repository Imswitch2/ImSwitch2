from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class LightSheetMulticolorWidget(Widget):
    """ Widget for multicolor light-sheet / pLS-RESOLFT scans via TriggerScope. """

    sigSaveScanClicked = QtCore.Signal()
    sigLoadScanClicked = QtCore.Signal()
    sigRunScanClicked = QtCore.Signal()
    sigParameterChanged = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumHeight(200)

        self.scannerLabel = QtWidgets.QLabel('pLS-multicolor')
        self.scannerLabel.setStyleSheet('font-size: 14pt; font-weight: bold')

        self.saveScanBtn = guitools.BetterPushButton('Save Scan')
        self.loadScanBtn = guitools.BetterPushButton('Load Scan')

        autoStartRecLabel = QtWidgets.QLabel('Auto-start REC')
        autoStartRecLabel.setAlignment(QtCore.Qt.AlignRight)
        self.autoStartRec = QtWidgets.QCheckBox()
        autoStopRecLabel = QtWidgets.QLabel('Auto-stop REC')
        autoStopRecLabel.setAlignment(QtCore.Qt.AlignRight)
        self.autoStopRec = QtWidgets.QCheckBox()
        self.scanButton = guitools.BetterPushButton('Run Scan')

        self.scrollContainer = QtWidgets.QGridLayout()
        self.scrollContainer.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.scrollContainer)

        self.grid = QtWidgets.QGridLayout()
        self.gridContainer = QtWidgets.QWidget()
        self.gridContainer.setLayout(self.grid)

        self.scrollArea = QtWidgets.QScrollArea()
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setWidget(self.gridContainer)
        self.scrollArea.setWidgetResizable(True)
        self.scrollContainer.addWidget(self.scrollArea)
        self.gridContainer.installEventFilter(self)

        # ---- Spin-box parameters ----
        timeLapsePointsLabel = QtWidgets.QLabel('Time lapse timepoints')
        self.timeLapsePointsEdit = guitools.BetterSpinBox(allowScrollChanges=False)
        self.timeLapsePointsEdit.editingFinished.connect(self.sigParameterChanged)

        timeLapseDelayLabel = QtWidgets.QLabel('Time lapse delay (sec)')
        self.timeLapseDelayEdit = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
        self.timeLapseDelayEdit.setMaximum(1000)
        self.timeLapseDelayEdit.editingFinished.connect(self.sigParameterChanged)

        def _dbl(label_text, maximum=None, minimum=None, decimals=None):
            lbl = QtWidgets.QLabel(label_text)
            sb = guitools.BetterDoubleSpinBox(allowScrollChanges=False)
            if maximum is not None:
                sb.setMaximum(maximum)
            if minimum is not None:
                sb.setMinimum(minimum)
            if decimals is not None:
                sb.setDecimals(decimals)
            sb.editingFinished.connect(self.sigParameterChanged)
            return lbl, sb

        Laser1OnLabel,         self.Laser1OnEdit         = _dbl('Laser 1 on time (ms)')
        DelayAfterLaser1Label, self.DelayAfterLaser1Edit = _dbl('Delay after Laser 1 (ms)', maximum=1000)
        Laser2OnLabel,         self.Laser2OnEdit         = _dbl('Laser 2 on time (ms)')
        DelayAfterLaser2Label, self.DelayAfterLaser2Edit = _dbl('Delay after Laser 2 (ms)', maximum=1000)
        Laser3OnLabel,         self.Laser3OnEdit         = _dbl('Laser 3 on time (ms)')
        DelayAfterLaser3Label, self.DelayAfterLaser3Edit = _dbl('Delay after Laser 3 (ms)', maximum=1000)
        Laser4OnLabel,         self.Laser4OnEdit         = _dbl('Laser 4 on time (ms)')
        DelayAfterLaser4Label, self.DelayAfterLaser4Edit = _dbl('Delay after Laser 4 (ms)', maximum=1000)
        Laser5OnLabel,         self.Laser5OnEdit         = _dbl('Laser 5 on time (ms)')
        DelayAfterLaser5Label, self.DelayAfterLaser5Edit = _dbl('Delay after Laser 5 (ms)', maximum=1000)

        MulticolorScanFirstLabel,  self.MulticolorScanFirstEdit  = _dbl('Multicolor scan first position (V)',  minimum=-500, maximum=500)
        MulticolorScanSecondLabel, self.MulticolorScanSecondEdit = _dbl('Multicolor scan second position (V)', minimum=-500, maximum=500)

        roRestingPosUmLabel, self.roRestingPosUmEdit = _dbl('RO scan resting position (um)', minimum=-200, maximum=200)
        roStartPosUmLabel,   self.roStartPosUmEdit   = _dbl('RO scan start (um)',             minimum=-200, maximum=200)
        roStepSizeUmLabel,   self.roStepSizeUmEdit   = _dbl('RO scan step size (um)',         minimum=-200, maximum=200)

        roStepsLabel = QtWidgets.QLabel('RO scan steps')
        self.roStepsEdit = guitools.BetterSpinBox(allowScrollChanges=False)
        self.roStepsEdit.setMaximum(10000)
        self.roStepsEdit.editingFinished.connect(self.sigParameterChanged)

        cycleStartPosUmLabel, self.cycleStartPosUmEdit = _dbl('Cycle scan start (um)',     minimum=-200, maximum=200)
        cycleStepSizeUmLabel, self.cycleStepSizeUmEdit = _dbl('Cycle scan step size (um)', minimum=-10,  maximum=10, decimals=3)

        cycleStepsLabel = QtWidgets.QLabel('Cycle scan steps')
        self.cycleStepsEdit = guitools.BetterSpinBox(allowScrollChanges=False)
        self.cycleStepsEdit.setMaximum(1000)
        self.cycleStepsEdit.editingFinished.connect(self.sigParameterChanged)

        # ---- Combo-box device selectors ----
        def _combo(label_text, enabled=True):
            lbl = QtWidgets.QLabel(label_text)
            cb = guitools.BetterComboBox(allowScrollChanges=False)
            if not enabled:
                cb.setEnabled(False)
            return lbl, cb

        Laser1Label,              self.Laser1Edit              = _combo('Laser 1')
        Laser2Label,              self.Laser2Edit              = _combo('Laser 2')
        Laser3Label,              self.Laser3Edit              = _combo('Laser 3')
        Laser4Label,              self.Laser4Edit              = _combo('Laser 4')
        Laser5Label,              self.Laser5Edit              = _combo('Laser 5')
        CameraTTLLabel,           self.CameraTTLEdit           = _combo('Camera used for detection')
        roScanDeviceLabel,        self.roScanDeviceEdit        = _combo('RO scan device')
        MulticolorScanDeviceLabel,self.MulticolorScanDeviceEdit = _combo('Multicolor scan device')
        cycleScanDeviceLabel,     self.cycleScanDeviceEdit     = _combo('Cycle scan device (same as RO)', enabled=False)

        # ---- Layout ----
        r = 0
        self.grid.addItem(QtWidgets.QSpacerItem(20, 40, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding), r, 0, 1, -1); r += 1
        self.grid.addWidget(self.scannerLabel, r, 0); r += 1
        self.grid.addWidget(self.loadScanBtn, r, 0)
        self.grid.addWidget(self.saveScanBtn, r, 1)
        self.grid.addWidget(autoStartRecLabel, r, 2)
        self.grid.addWidget(self.autoStartRec, r, 3); r += 1
        self.grid.addWidget(autoStopRecLabel, r, 2)
        self.grid.addWidget(self.autoStopRec, r, 3); r += 1
        self.grid.addWidget(self.scanButton, r, 3); r += 1
        self.grid.addItem(QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding), r, 0, 1, 4); r += 1
        self.grid.addWidget(timeLapsePointsLabel, r, 0); self.grid.addWidget(self.timeLapsePointsEdit, r, 1)
        self.grid.addWidget(timeLapseDelayLabel, r, 2);  self.grid.addWidget(self.timeLapseDelayEdit, r, 3); r += 1
        self.grid.addItem(QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding), r, 0, 1, 4); r += 1
        self.grid.addWidget(Laser1OnLabel, r, 0);         self.grid.addWidget(self.Laser1OnEdit, r, 1)
        self.grid.addWidget(roRestingPosUmLabel, r, 2);   self.grid.addWidget(self.roRestingPosUmEdit, r, 3); r += 1
        self.grid.addWidget(DelayAfterLaser1Label, r, 0); self.grid.addWidget(self.DelayAfterLaser1Edit, r, 1)
        self.grid.addWidget(roStartPosUmLabel, r, 2);     self.grid.addWidget(self.roStartPosUmEdit, r, 3); r += 1
        self.grid.addWidget(Laser2OnLabel, r, 0);         self.grid.addWidget(self.Laser2OnEdit, r, 1)
        self.grid.addWidget(roStepSizeUmLabel, r, 2);     self.grid.addWidget(self.roStepSizeUmEdit, r, 3); r += 1
        self.grid.addWidget(DelayAfterLaser2Label, r, 0); self.grid.addWidget(self.DelayAfterLaser2Edit, r, 1)
        self.grid.addWidget(roStepsLabel, r, 2);           self.grid.addWidget(self.roStepsEdit, r, 3); r += 1
        self.grid.addWidget(Laser3OnLabel, r, 0);         self.grid.addWidget(self.Laser3OnEdit, r, 1)
        self.grid.addWidget(cycleStartPosUmLabel, r, 2);  self.grid.addWidget(self.cycleStartPosUmEdit, r, 3); r += 1
        self.grid.addWidget(DelayAfterLaser3Label, r, 0); self.grid.addWidget(self.DelayAfterLaser3Edit, r, 1)
        self.grid.addWidget(cycleStepSizeUmLabel, r, 2);  self.grid.addWidget(self.cycleStepSizeUmEdit, r, 3); r += 1
        self.grid.addWidget(Laser4OnLabel, r, 0);         self.grid.addWidget(self.Laser4OnEdit, r, 1)
        self.grid.addWidget(cycleStepsLabel, r, 2);        self.grid.addWidget(self.cycleStepsEdit, r, 3); r += 1
        self.grid.addWidget(DelayAfterLaser4Label, r, 0); self.grid.addWidget(self.DelayAfterLaser4Edit, r, 1)
        self.grid.addWidget(Laser5OnLabel, r, 2);          self.grid.addWidget(self.Laser5OnEdit, r, 3); r += 1
        self.grid.addWidget(MulticolorScanFirstLabel, r, 0);  self.grid.addWidget(self.MulticolorScanFirstEdit, r, 1)
        self.grid.addWidget(MulticolorScanSecondLabel, r, 2); self.grid.addWidget(self.MulticolorScanSecondEdit, r, 3); r += 1
        self.grid.addItem(QtWidgets.QSpacerItem(40, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding), r, 0, 1, 4); r += 1
        self.grid.addWidget(Laser1Label, r, 0);              self.grid.addWidget(self.Laser1Edit, r, 1)
        self.grid.addWidget(roScanDeviceLabel, r, 2);         self.grid.addWidget(self.roScanDeviceEdit, r, 3); r += 1
        self.grid.addWidget(Laser2Label, r, 0);              self.grid.addWidget(self.Laser2Edit, r, 1)
        self.grid.addWidget(MulticolorScanDeviceLabel, r, 2);self.grid.addWidget(self.MulticolorScanDeviceEdit, r, 3); r += 1
        self.grid.addWidget(Laser3Label, r, 0);              self.grid.addWidget(self.Laser3Edit, r, 1)
        self.grid.addWidget(Laser5Label, r, 2);               self.grid.addWidget(self.Laser5Edit, r, 3); r += 1
        self.grid.addWidget(Laser4Label, r, 0);              self.grid.addWidget(self.Laser4Edit, r, 1)
        self.grid.addWidget(CameraTTLLabel, r, 2);            self.grid.addWidget(self.CameraTTLEdit, r, 3); r += 1
        self.grid.addWidget(cycleScanDeviceLabel, r, 2);     self.grid.addWidget(self.cycleScanDeviceEdit, r, 3)

        # ---- Signals ----
        self.saveScanBtn.clicked.connect(self.sigSaveScanClicked)
        self.loadScanBtn.clicked.connect(self.sigLoadScanClicked)
        self.scanButton.clicked.connect(self.sigRunScanClicked)

    # ------------------------------------------------------------------
    # Getters / setters
    # ------------------------------------------------------------------

    def getTimeLapsePoints(self):    return self.timeLapsePointsEdit.value()
    def setTimeLapsePoints(self, v): self.timeLapsePointsEdit.setValue(v)
    def getTimeLapseDelayS(self):    return self.timeLapseDelayEdit.value()
    def setTimeLapseDelayS(self, v): self.timeLapseDelayEdit.setValue(v)

    def getLaser1OnMs(self):           return self.Laser1OnEdit.value()
    def setLaser1OnMs(self, v):        self.Laser1OnEdit.setValue(v)
    def getDelayAfterLaser1Ms(self):   return self.DelayAfterLaser1Edit.value()
    def setDelayAfterLaser1Ms(self, v):self.DelayAfterLaser1Edit.setValue(v)
    def getLaser2OnMs(self):           return self.Laser2OnEdit.value()
    def setLaser2OnMs(self, v):        self.Laser2OnEdit.setValue(v)
    def getDelayAfterLaser2Ms(self):   return self.DelayAfterLaser2Edit.value()
    def setDelayAfterLaser2Ms(self, v):self.DelayAfterLaser2Edit.setValue(v)
    def getLaser3OnMs(self):           return self.Laser3OnEdit.value()
    def setLaser3OnMs(self, v):        self.Laser3OnEdit.setValue(v)
    def getDelayAfterLaser3Ms(self):   return self.DelayAfterLaser3Edit.value()
    def setDelayAfterLaser3Ms(self, v):self.DelayAfterLaser3Edit.setValue(v)
    def getLaser4OnMs(self):           return self.Laser4OnEdit.value()
    def setLaser4OnMs(self, v):        self.Laser4OnEdit.setValue(v)
    def getDelayAfterLaser4Ms(self):   return self.DelayAfterLaser4Edit.value()
    def setDelayAfterLaser4Ms(self, v):self.DelayAfterLaser4Edit.setValue(v)
    def getLaser5OnMs(self):           return self.Laser5OnEdit.value()
    def setLaser5OnMs(self, v):        self.Laser5OnEdit.setValue(v)
    def getDelayAfterLaser5Ms(self):   return self.DelayAfterLaser5Edit.value()
    def setDelayAfterLaser5Ms(self, v):self.DelayAfterLaser5Edit.setValue(v)

    def getRoRestingPosUm(self):          return self.roRestingPosUmEdit.value()
    def setRoRestingPosUm(self, v):       self.roRestingPosUmEdit.setValue(v)
    def getRoStartPosUm(self):            return self.roStartPosUmEdit.value()
    def setRoStartPosUm(self, v):         self.roStartPosUmEdit.setValue(v)
    def getRoStepSizeUm(self):            return self.roStepSizeUmEdit.value()
    def setRoStepSizeUm(self, v):         self.roStepSizeUmEdit.setValue(v)
    def getRoSteps(self):                 return self.roStepsEdit.value()
    def setRoSteps(self, v):              self.roStepsEdit.setValue(v)
    def getMulticolorScanFirstUm(self):   return self.MulticolorScanFirstEdit.value()
    def setMulticolorScanFirstUm(self, v):self.MulticolorScanFirstEdit.setValue(v)
    def getMulticolorScanSecondUm(self):  return self.MulticolorScanSecondEdit.value()
    def setMulticolorScanSecondUm(self,v):self.MulticolorScanSecondEdit.setValue(v)
    def getCycleStartPosUm(self):         return self.cycleStartPosUmEdit.value()
    def setCycleStartPosUm(self, v):      self.cycleStartPosUmEdit.setValue(v)
    def getCycleStepSizeUm(self):         return self.cycleStepSizeUmEdit.value()
    def setCycleStepSizeUm(self, v):      self.cycleStepSizeUmEdit.setValue(v)
    def getCycleSteps(self):              return self.cycleStepsEdit.value()
    def setCycleSteps(self, v):           self.cycleStepsEdit.setValue(v)

    def _getComboValue(self, cb):         return cb.currentText()
    def _setComboValue(self, cb, value):  cb.setCurrentIndex(cb.findText(value))

    def getLaser1(self):             return self._getComboValue(self.Laser1Edit)
    def setLaser1(self, v):          self._setComboValue(self.Laser1Edit, v)
    def getLaser2(self):             return self._getComboValue(self.Laser2Edit)
    def setLaser2(self, v):          self._setComboValue(self.Laser2Edit, v)
    def getLaser3(self):             return self._getComboValue(self.Laser3Edit)
    def setLaser3(self, v):          self._setComboValue(self.Laser3Edit, v)
    def getLaser4(self):             return self._getComboValue(self.Laser4Edit)
    def setLaser4(self, v):          self._setComboValue(self.Laser4Edit, v)
    def getLaser5(self):             return self._getComboValue(self.Laser5Edit)
    def setLaser5(self, v):          self._setComboValue(self.Laser5Edit, v)
    def getCameraTTL(self):          return self._getComboValue(self.CameraTTLEdit)
    def setCameraTTL(self, v):       self._setComboValue(self.CameraTTLEdit, v)
    def getRoScanDevice(self):       return self._getComboValue(self.roScanDeviceEdit)
    def setRoScanDevice(self, v):    self._setComboValue(self.roScanDeviceEdit, v)
    def getMulticolorScanDevice(self):    return self._getComboValue(self.MulticolorScanDeviceEdit)
    def setMulticolorScanDevice(self, v): self._setComboValue(self.MulticolorScanDeviceEdit, v)
    def getCycleScanDevice(self):    return self._getComboValue(self.cycleScanDeviceEdit)
    def setCycleScanDevice(self, v): self._setComboValue(self.cycleScanDeviceEdit, v)

    def setScanButtonChecked(self, checked):
        self.scanButton.setEnabled(not checked)
        self.scanButton.setCheckable(checked)
        self.scanButton.setChecked(checked)

    def setRepeatEnabled(self, enabled):
        pass  # no repeat control in this widget

    def eventFilter(self, source, event):
        if source is self.gridContainer and event.type() == QtCore.QEvent.Resize:
            width = (self.gridContainer.minimumSizeHint().width()
                     + self.scrollArea.verticalScrollBar().width())
            self.scrollArea.setMinimumWidth(width)
            self.setMinimumWidth(width)
        return False


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
