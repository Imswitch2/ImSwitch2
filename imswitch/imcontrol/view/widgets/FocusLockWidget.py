import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.framework import Signal

from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget
from .DetectorSettingsTree import DetectorSettingsTree


class FocusLockWidget(Widget):
    """Widget containing focus lock interface."""

    #: dict of the hold / relock settings after an edit (controller validates).
    sigRobustnessSettingsChanged = Signal(dict)

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
        self.focusActuatorButton = QtWidgets.QToolButton()
        self.focusActuatorButton.setText('⚙')
        self.focusActuatorButton.setToolTip('Focus lock settings (including Z actuator)')
        self.focusActuatorButton.setAutoRaise(True)
        self.focusActuatorMenu = QtWidgets.QMenu(self.focusActuatorButton)
        self.changeFocusActuatorAction = self.focusActuatorMenu.addAction(
            'Change focus Z actuator…'
        )
        self.focusActuatorButton.setMenu(self.focusActuatorMenu)
        self.focusActuatorButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
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
        calibRow = QtWidgets.QHBoxLayout()
        calibRow.setContentsMargins(0, 0, 0, 0)
        calibRow.addWidget(self.calibCurveButton)
        calibRow.addWidget(self.focusActuatorButton)
        focusGrid.addLayout(calibRow, 3, 2)
        focusGrid.addWidget(self.kpLabel, 1, 3)
        focusGrid.addWidget(self.kpEdit, 1, 4)
        focusGrid.addWidget(self.kiLabel, 2, 3)
        focusGrid.addWidget(self.kiEdit, 2, 4)
        focusGrid.addWidget(self.lockButton, 1, 5, 2, 1)
        focusGrid.addWidget(self.ScanBlock, 3, 3, 1, 2)
        focusGrid.addWidget(self.twoFociBox, 3, 5)
        focusGrid.addWidget(self.cameraAcqButton, 1, 6, 2, 1)
        focusGrid.addWidget(self.lockStateLabel, 3, 6)

        focusGrid.addWidget(self._buildRobustnessBox(), 4, 0, 1, 7)

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

    def _buildRobustnessBox(self):
        """Hold / relock: live confidence, the failed trust test, the limits."""
        box = QtWidgets.QGroupBox('Hold and relock')
        box.setCheckable(False)
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(6, 4, 6, 4)
        self.confidenceLabel = QtWidgets.QLabel('Confidence: –')
        self.confidenceLabel.setToolTip(
            'Normalised correlation of the spot with the reference taken at lock '
            'time (1 = identical). Frames failing any trust test hold Z.')
        self.reasonLabel = QtWidgets.QLabel('')
        self.reasonLabel.setToolTip('Trust test(s) the last frame failed, or why the '
                                    'lock is held / lost.')
        self.reasonLabel.setWordWrap(True)
        self.relockButton = guitools.BetterPushButton('Re-lock')
        self.relockButton.setToolTip(
            'Step Z back toward the kept setpoint in bounded steps (needs a '
            'calibration), then lock again.')
        self.relockButton.setEnabled(False)
        self.reengageButton = guitools.BetterPushButton('Re-engage previous lock')
        self.reengageButton.setToolTip(
            'Load the setpoint, reference and calibration saved for this setup and '
            'focus actuator, and relock to it from the current Z (bounded). Never '
            'happens by itself.')
        self.reengageButton.setEnabled(False)

        def spin(minimum, maximum, step, decimals, suffix, tip):
            box_ = QtWidgets.QDoubleSpinBox()
            box_.setRange(minimum, maximum)
            box_.setSingleStep(step)
            box_.setDecimals(decimals)
            box_.setSuffix(suffix)
            box_.setToolTip(tip)
            box_.setKeyboardTracking(False)
            return box_

        self.goodFramesEdit = QtWidgets.QSpinBox()
        self.goodFramesEdit.setRange(1, 1000)
        self.goodFramesEdit.setToolTip('Consecutive trustworthy frames to leave holding')
        self.goodFramesEdit.setKeyboardTracking(False)
        self.holdTimeoutEdit = spin(0.1, 600, 0.5, 1, ' s', 'Hold this long, then give up '
                                    '(lost; setpoint kept)')
        self.relockStepEdit = spin(0.01, 10, 0.1, 2, ' µm', 'Largest single relock Z step')
        self.relockExcursionEdit = spin(0.01, 50, 0.5, 2, ' µm',
                                        'Largest total Z distance from the last good Z')
        self.relockTolEdit = spin(0.05, 50, 0.1, 2, ' px',
                                  'Error at which the loop takes over from relock')
        self.relockTimeoutEdit = spin(0.5, 600, 1, 1, ' s', 'Give up relocking after')
        self.searchBox = QtWidgets.QCheckBox('Z search')
        self.searchBox.setToolTip('When the spot is not on the camera, search ± range '
                                  'around the last good Z (bounded by the excursion '
                                  'limit). Off by default.')
        self.searchRangeEdit = spin(0.01, 50, 0.5, 2, ' µm', 'Search range ±')
        row = 0
        grid.addWidget(self.confidenceLabel, row, 0, 1, 2)
        grid.addWidget(self.reasonLabel, row, 2, 1, 3)
        grid.addWidget(self.relockButton, row, 5)
        grid.addWidget(self.reengageButton, row, 6, 1, 2)
        row += 1
        for col, (label, edit) in enumerate((
                ('Good frames', self.goodFramesEdit), ('Hold', self.holdTimeoutEdit),
                ('Step', self.relockStepEdit), ('Max ΔZ', self.relockExcursionEdit))):
            grid.addWidget(QtWidgets.QLabel(label), row, 2 * col)
            grid.addWidget(edit, row, 2 * col + 1)
        row += 1
        for col, (label, edit) in enumerate((
                ('Tolerance', self.relockTolEdit), ('Timeout', self.relockTimeoutEdit))):
            grid.addWidget(QtWidgets.QLabel(label), row, 2 * col)
            grid.addWidget(edit, row, 2 * col + 1)
        grid.addWidget(self.searchBox, row, 4)
        grid.addWidget(self.searchRangeEdit, row, 5)
        self._robustnessEdits = (self.goodFramesEdit, self.holdTimeoutEdit, self.relockStepEdit,
                                 self.relockExcursionEdit, self.relockTolEdit,
                                 self.relockTimeoutEdit, self.searchRangeEdit)
        for edit in self._robustnessEdits:
            edit.valueChanged.connect(self._emitRobustnessSettings)
        self.searchBox.toggled.connect(self._emitRobustnessSettings)
        return box

    def robustnessSettings(self):
        return {
            'goodFramesToResume': int(self.goodFramesEdit.value()),
            'holdTimeoutS': float(self.holdTimeoutEdit.value()),
            'maxStepUm': float(self.relockStepEdit.value()),
            'maxExcursionUm': float(self.relockExcursionEdit.value()),
            'tolerancePx': float(self.relockTolEdit.value()),
            'timeoutS': float(self.relockTimeoutEdit.value()),
            'searchEnabled': bool(self.searchBox.isChecked()),
            'searchRangeUm': float(self.searchRangeEdit.value()),
        }

    def _emitRobustnessSettings(self, *_):
        if not self.__dict__.get('_syncingRobustness', False):
            self.sigRobustnessSettingsChanged.emit(self.robustnessSettings())

    def setRobustnessSettings(self, hold, limits, *, storedAvailable=False):
        """Show the controller's values without echoing them back."""
        self._syncingRobustness = True
        try:
            self.goodFramesEdit.setValue(int(hold['goodFramesToResume']))
            self.holdTimeoutEdit.setValue(float(hold['holdTimeoutS']))
            self.relockStepEdit.setValue(float(limits['maxStepUm']))
            self.relockExcursionEdit.setValue(float(limits['maxExcursionUm']))
            self.relockTolEdit.setValue(float(limits['tolerancePx']))
            self.relockTimeoutEdit.setValue(float(limits['timeoutS']))
            self.searchBox.setChecked(bool(limits['searchEnabled']))
            self.searchRangeEdit.setValue(float(limits['searchRangeUm']))
        finally:
            self._syncingRobustness = False
        self.reengageButton.setEnabled(bool(storedAvailable))

    def setLockQuality(self, status):
        """Live confidence and the failed test(s), from the controller."""
        confidence = status.get('confidence')
        self.confidenceLabel.setText(
            'Confidence: –' if confidence is None else f'Confidence: {confidence:.3f}')
        state = status.get('state')
        if state == 'lost':
            text = status.get('lostReason') or 'lost'
        elif state == 'holding':
            text = 'held: ' + (', '.join(status.get('heldFor') or status.get('reasons') or [])
                               or 'untrusted frame')
        elif status.get('reasons'):
            text = 'last frame: ' + ', '.join(status['reasons'])
        else:
            text = ''
        if self.reasonLabel.text() != text:
            self.reasonLabel.setText(text)
        self.relockButton.setEnabled(state in ('lost', 'holding', 'paused'))

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
        'holding': ('Holding Z (frames untrusted)', 'orange'),
        'lost': ('Lost — setpoint kept', 'red'),
        'paused': ('Paused', 'orange'),
        'relocking': ('Relocking…', 'orange'),
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
