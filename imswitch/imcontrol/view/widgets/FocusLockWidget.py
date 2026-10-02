import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget
from .DetectorSettingsTree import DetectorSettingsTree


class FocusLockWidget(Widget):
    """Widget containing focus lock interface."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Focus lock
        self.kpEdit = QtWidgets.QLineEdit('1')
        self.kpLabel = QtWidgets.QLabel('kp')
        self.kiEdit = QtWidgets.QLineEdit('0')
        self.kiLabel = QtWidgets.QLabel('ki')

        self.lockButton = guitools.BetterPushButton('Lock')
        self.lockButton.setCheckable(True)
        self.lockButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                      QtWidgets.QSizePolicy.Expanding)

        # Checked by default: this is an opt-*out*. It used to be an unchecked,
        # unpersisted opt-in, so every fresh session silently allowed an active
        # focus lock to fight a hardware Z scan on the same piezo.
        self.ScanBlock = QtWidgets.QCheckBox('Pause during scans')
        self.ScanBlock.setChecked(True)
        self.ScanBlock.setToolTip(
            'Suspend focus correction while a scan drives the focus axis, and '
            'wait for the signal to come back before resuming.\n'
            'Unchecking this lets the lock oppose the scan waveform when both '
            'reach the same actuator.'
        )
        self.twoFociBox = QtWidgets.QCheckBox('Two foci')

        self.lockStateLabel = QtWidgets.QLabel('Unlocked')
        self.lockStateLabel.setToolTip(
            'Focus-lock state. "Suspended" means a scan currently owns the '
            'focus axis; "Reacquiring" means it is waiting for the focus '
            'signal to return before correcting again.'
        )

        # Focus-camera acquisition is an explicit runtime resource, just like
        # the main View widget's live acquisition. It defaults ON to preserve
        # the historical focus-lock behaviour, but can now be stopped when the
        # focus camera is not needed (for example before reconnecting it).
        self.cameraAcqButton = guitools.BetterPushButton('Stop Cam')
        self.cameraAcqButton.setCheckable(True)
        self.cameraAcqButton.setChecked(True)
        self.cameraAcqButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                           QtWidgets.QSizePolicy.Expanding)
        self.cameraAcqButton.setToolTip(
            'Start/stop camera live acquisition.'
        )

        # Focus lock calibration
        self.calibFromLabel = QtWidgets.QLabel('From (µm)')
        self.calibFromEdit = QtWidgets.QLineEdit('-1')
        self.calibToLabel = QtWidgets.QLabel('To (µm)')
        self.calibToEdit = QtWidgets.QLineEdit('1')
        self.focusCalibButton = guitools.BetterPushButton('Calib')
        self.focusCalibButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                            QtWidgets.QSizePolicy.Expanding)
        self.calibCurveButton = guitools.BetterPushButton('See calib')
        self.calibrationDisplay = QtWidgets.QLineEdit('No calibration')
        self.calibrationDisplay.setReadOnly(True)
        self.focusCalibrationWindow = FocusCalibrationWindow()

        # Focus lock graph
        self.focusLockGraph = pg.GraphicsLayoutWidget()
        self.focusLockGraph.setAntialiasing(True)
        self.focusPlot = self.focusLockGraph.addPlot(row=1, col=0)
        self.focusPlot.setLabels(bottom=('Time', 's'), left=('Laser position', 'px'))
        self.focusPlot.showGrid(x=True, y=True)
        self.focusPlotCurve = self.focusPlot.plot(pen='y')  # updated by controller

        # Focus-camera image stays permanently visible to the right of both tabs.
        self.webcamGraph = pg.GraphicsLayoutWidget()
        self.camImg = pg.ImageItem(border='w')
        self.camImg.setImage(np.zeros((100, 100)))
        self.vb = self.webcamGraph.addViewBox(invertY=True, invertX=False)
        self.vb.setAspectLocked(True)
        self.vb.addItem(self.camImg)
        self.center = pg.InfiniteLine()
        self.vb.addItem(self.center)
        self.center.setVisible(True)
        self.focusCameraROI = None
        self._focusCameraCroppable = False

        # The left side switches between the focus-lock controls and the
        # settings of the dedicated focus camera. The camera settings tree is
        # inserted later by FocusLockController once focusLock.camera is known.
        self.tabs = QtWidgets.QTabWidget()
        self.focusLockTab = QtWidgets.QWidget()
        self.cameraTab = QtWidgets.QWidget()
        self.tabs.addTab(self.focusLockTab, 'Focus lock')
        self.tabs.addTab(self.cameraTab, 'Camera')

        focusGrid = QtWidgets.QGridLayout()
        self.focusLockTab.setLayout(focusGrid)
        focusGrid.addWidget(self.focusLockGraph, 0, 0, 1, 7)
        focusGrid.addWidget(self.calibFromLabel, 1, 0)
        focusGrid.addWidget(self.calibFromEdit, 1, 1)
        focusGrid.addWidget(self.calibToLabel, 2, 0)
        focusGrid.addWidget(self.calibToEdit, 2, 1)
        focusGrid.addWidget(self.focusCalibButton, 1, 2, 2, 1)
        focusGrid.addWidget(self.calibrationDisplay, 3, 0, 1, 2)
        focusGrid.addWidget(self.calibCurveButton, 3, 2)
        focusGrid.addWidget(self.kpLabel, 1, 3)
        focusGrid.addWidget(self.kpEdit, 1, 4)
        focusGrid.addWidget(self.kiLabel, 2, 3)
        focusGrid.addWidget(self.kiEdit, 2, 4)
        focusGrid.addWidget(self.lockButton, 1, 5, 2, 1)
        focusGrid.addWidget(self.ScanBlock, 3, 3, 1, 2)
        focusGrid.addWidget(self.twoFociBox, 3, 5)
        focusGrid.addWidget(self.cameraAcqButton, 1, 6, 2, 1)
        focusGrid.addWidget(self.lockStateLabel, 3, 6)

        self._cameraTabLayout = QtWidgets.QVBoxLayout()
        self.cameraTab.setLayout(self._cameraTabLayout)
        self.cameraSettingsTree = None
        self._cameraSettingsPlaceholder = QtWidgets.QLabel(
            'Focus camera settings unavailable.'
        )
        self._cameraSettingsPlaceholder.setWordWrap(True)
        self._cameraTabLayout.addWidget(self._cameraSettingsPlaceholder)
        self._cameraTabLayout.addStretch(1)

        layout = QtWidgets.QHBoxLayout()
        self.setLayout(layout)
        layout.addWidget(self.tabs, 3)
        layout.addWidget(self.webcamGraph, 2)

    def setFocusCameraSettings(self, detectorName, detectorModel, detectorParameters,
                               detectorActions, supportedBinnings, roiInfos,
                               croppable=True):
        """Create the reusable settings tree for ``focusLock.camera``.

        Focus Lock owns its ROI workflow instead of reusing SettingsController:
        the focus camera must be stopped at the worker/lease boundary before a
        crop is changed. Binning and saved frame modes therefore stay read-only
        here, while custom X/Y/width/height plus Apply/New ROI/Abort ROI are
        available when the detector reports that it is croppable.
        """
        if self.cameraSettingsTree is not None:
            self._cameraTabLayout.removeWidget(self.cameraSettingsTree)
            self.cameraSettingsTree.deleteLater()

        if self._cameraSettingsPlaceholder is not None:
            self._cameraTabLayout.removeWidget(self._cameraSettingsPlaceholder)
            self._cameraSettingsPlaceholder.deleteLater()
            self._cameraSettingsPlaceholder = None

        tree = DetectorSettingsTree(
            detectorParameters,
            detectorActions,
            supportedBinnings,
            roiInfos,
        )
        self.cameraSettingsTree = tree
        self._focusCameraCroppable = bool(croppable)

        modelParam = tree.p.param('Model')
        modelParam.setValue(detectorModel)
        modelParam.setOpts(tip=f'Focus camera: {detectorName}')

        frameParam = tree.p.param('Image frame')
        frameParam.addChild({
            'name': 'Full chip', 'type': 'action', 'title': 'Full chip'
        })
        frameParam.setOpts(
            tip=(
                'Focus-camera ROI. Applying a crop safely stops the dedicated '
                'focus acquisition first, reads the camera geometry back, and '
                'then restores acquisition when appropriate.'
            )
        )
        frameParam.param('Binning').setOpts(enabled=False)
        frameParam.param('Mode').setOpts(enabled=False)
        for name in (
            'X0', 'Y0', 'Width', 'Height', 'Apply', 'New ROI', 'Abort ROI',
            'Full chip',
        ):
            frameParam.param(name).setOpts(enabled=self._focusCameraCroppable)

        # These are global Settings-widget concepts, not part of the dedicated
        # focus-camera workflow. Keeping them hidden avoids implying that Focus
        # Lock can edit shared ROI presets or other detectors.
        for name in ('Save mode', 'Delete mode', 'Update all detectors'):
            frameParam.param(name).hide()

        self._cameraTabLayout.insertWidget(0, tree)
        return tree

    def updateFocusCameraFrameReadback(self, *, detectorModel, binning,
                                       frameStart, shape, fullShape):
        """Show the current (or pending edit) focus-camera frame geometry."""
        tree = self.cameraSettingsTree
        if tree is None:
            return

        tree.p.param('Model').setValue(detectorModel)
        frameParam = tree.p.param('Image frame')
        frameParam.param('Binning').setValue(binning)
        frameParam.param('Mode').setValue('Custom')
        frameParam.param('X0').setLimits((0, max(0, int(fullShape[0]) - 1)))
        frameParam.param('Y0').setLimits((0, max(0, int(fullShape[1]) - 1)))
        frameParam.param('X0').setValue(int(frameStart[0]))
        frameParam.param('Y0').setValue(int(frameStart[1]))
        frameParam.param('Width').setLimits((1, max(1, int(fullShape[0]))))
        frameParam.param('Width').setValue(int(shape[0]))
        frameParam.param('Height').setLimits((1, max(1, int(fullShape[1]))))
        frameParam.param('Height').setValue(int(shape[1]))

    def setFocusCameraRoiEditing(self, editing):
        """Reflect whether the graphical full-frame ROI editor is active."""
        tree = self.cameraSettingsTree
        if tree is None:
            return
        frameParam = tree.p.param('Image frame')
        enabled = self._focusCameraCroppable
        frameParam.param('New ROI').setOpts(enabled=enabled and not editing)
        frameParam.param('Apply').setOpts(enabled=enabled)
        frameParam.param('Abort ROI').setOpts(enabled=enabled)

    def showFocusCameraROI(self, position, size, bounds):
        """Show the graphical focus-camera ROI in focus-image coordinates."""
        if self.focusCameraROI is None:
            self.focusCameraROI = pg.RectROI(
                position,
                size,
                maxBounds=QtCore.QRectF(0, 0, float(bounds[0]), float(bounds[1])),
            )
            self.vb.addItem(self.focusCameraROI)
        else:
            self.focusCameraROI.maxBounds = QtCore.QRectF(
                0, 0, float(bounds[0]), float(bounds[1])
            )
            self.focusCameraROI.blockSignals(True)
            try:
                self.focusCameraROI.setPos(position)
                self.focusCameraROI.setSize(size)
            finally:
                self.focusCameraROI.blockSignals(False)
        self.focusCameraROI.show()
        return self.focusCameraROI

    def setFocusCameraROISelection(self, position, size):
        roi = self.focusCameraROI
        if roi is None:
            return
        roi.blockSignals(True)
        try:
            roi.setPos(position)
            roi.setSize(size)
        finally:
            roi.blockSignals(False)

    def getFocusCameraROISelection(self):
        roi = self.focusCameraROI
        if roi is None:
            return None
        pos = roi.pos()
        size = roi.size()
        return ((float(pos.x()), float(pos.y())),
                (float(size.x()), float(size.y())))

    def hideFocusCameraROI(self):
        if self.focusCameraROI is None:
            return
        try:
            self.vb.removeItem(self.focusCameraROI)
        finally:
            self.focusCameraROI = None

    def setFocusCameraActive(self, active):
        """Sync the acquisition button without re-triggering the controller."""
        self.cameraAcqButton.blockSignals(True)
        try:
            self.cameraAcqButton.setChecked(bool(active))
        finally:
            self.cameraAcqButton.blockSignals(False)
            self._setCamAcqButtonLabel()

    def _setCamAcqButtonLabel(self):
        if self.cameraAcqButton.isChecked():
            self.cameraAcqButton.setText('Stop Cam')
        else:
            self.cameraAcqButton.setText('Start Cam')

    def setKp(self, kp):
        self.kpEdit.setText(str(kp))

    def setKi(self, ki):
        self.kiEdit.setText(str(ki))

    def setLockState(self, state):
        """Show the lifecycle state, so the button never stands alone.

        The lock button says "Unlock" whenever a lock is *wanted*, which is not
        the same as one being active: a scan can have suspended it, or
        reacquisition can have failed. Without this the operator has no way to
        tell those apart from the button.
        """
        text, colour = self._LOCK_STATE_DISPLAY.get(state, (state, 'gray'))
        self.lockStateLabel.setText(text)
        self.lockStateLabel.setStyleSheet(f'color: {colour};')

    _LOCK_STATE_DISPLAY = {
        'unlocked': ('Unlocked', 'gray'),
        'locked': ('Locked', 'limegreen'),
        'suspended': ('Suspended (scan)', 'orange'),
        'reacquiring': ('Reacquiring…', 'orange'),
        'reacquire-failed': ('Lock lost after scan', 'red'),
    }

    def showCalibrationCurve(self, data):
        self.focusCalibrationWindow.run(data)
        self.focusCalibrationWindow.show()


class FocusCalibrationWindow(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.__focusCalibGraph = FocusCalibrationGraph()
        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        grid.addWidget(self.__focusCalibGraph, 0, 0)

    def run(self, data):
        self.__focusCalibGraph.draw(data)


class FocusCalibrationGraph(pg.GraphicsLayoutWidget):
    def __init__(self):
        super().__init__()
        self.plot = self.addPlot(row=1, col=0)
        self.plot.setLabels(bottom=('Set z position', 'µm'),
                            left=('Laser spot position', 'px'))
        self.plot.showGrid(x=True, y=True)

    def draw(self, data):
        self.plot.clear()
        self.positionData = data['positionData']
        self.signalData = data['signalData']
        self.poly = data['poly']
        self.plot.plot(self.positionData,
                       self.signalData, pen=None, symbol='o')
        self.plot.plot(self.positionData,
                       np.polyval(self.poly, self.positionData), pen='r')


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
