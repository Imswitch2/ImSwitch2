"""SimpleMoNaLISA scan cloak: a compact acquisition-only MoNaLISA panel."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model.simple_monalisa_scan import MoNaLISAScanPlan, SCAN_TYPES
from .ScanCloakPanel import ScanCloakPanel, ScanCloakView
from .ScanWidgetAdvanced import ScanWidgetAdvanced


def _format_length(um: float) -> str:
    if um < 1.0:
        return f'{um * 1000:.0f} nm'
    return f'{um:.3g} µm'


def _format_time(seconds: float) -> str:
    if seconds < 1e-3:
        return f'{seconds * 1e6:.0f} µs'
    if seconds < 1:
        return f'{seconds * 1e3:.2g} ms'
    if seconds < 60:
        return f'{seconds:.2f} s'
    if seconds < 3600:
        return f'{seconds / 60:.1f} min'
    return f'{seconds / 3600:.1f} h'


class MoNaLISAView(ScanCloakView):
    """The first MoNaLISA simple page: acquisition only, no overview."""

    sigPlanEdited = QtCore.Signal(object)

    def __init__(self, parent=None, *, napariViewer=None):
        super().__init__(parent)
        self._updating = False
        self._plan = None
        self._limits = None

        # No overview exists in this cloak.  Keep the generic run plumbing but
        # present a single acquisition action to the user.
        self.liveBox.hide()
        self.startButton.setText('Acquire')
        self.startButton.setToolTip('Run one MoNaLISA acquisition with these settings.')
        self.estimateTitle.setText('Total scan time')

        self._buildUi()

    def _group(self, title):
        box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QGridLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(7)
        return box, layout

    def _info_value(self):
        label = QtWidgets.QLabel('–')
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _buildUi(self):
        # Fixed microscope facts: visible, but deliberately not editable.
        box, layout = self._group('Microscope')
        self.fovValue = self._info_value()
        self.periodValue = self._info_value()
        self.sequenceDevicesValue = QtWidgets.QLabel('–')
        self.sequenceDevicesValue.setWordWrap(True)
        layout.addWidget(QtWidgets.QLabel('Imaging FOV'), 0, 0)
        layout.addWidget(self.fovValue, 0, 1)
        layout.addWidget(QtWidgets.QLabel('Scan length'), 1, 0)
        layout.addWidget(self.periodValue, 1, 1)
        layout.addWidget(QtWidgets.QLabel('Sequence'), 2, 0)
        layout.addWidget(self.sequenceDevicesValue, 2, 1)
        box.setToolTip(
            'These values describe the microscope and come from scan.simpleMoNaLISA '
            'in the setup file; they are not acquisition choices.')
        self.content.addWidget(box)

        # Scan geometry: fixed span, user-selected scan axes and sampling.
        box, layout = self._group('Scan')
        self.scanTypeCombo = QtWidgets.QComboBox()
        self.scanTypeCombo.addItems(SCAN_TYPES)
        self.scanTypeCombo.setToolTip('Axes scanned for this acquisition, fast axis first.')
        self.scanTypeCombo.currentTextChanged.connect(self._emitEdited)

        self.lateralStepSpin = QtWidgets.QDoubleSpinBox()
        self.lateralStepSpin.setDecimals(0)
        self.lateralStepSpin.setRange(1, 5000)
        self.lateralStepSpin.setSuffix(' nm')
        self.lateralStepSpin.setKeyboardTracking(False)
        self.lateralStepSpin.setToolTip('One shared X/Y step size.')
        self.lateralStepSpin.valueChanged.connect(self._emitEdited)

        self.zStepSpin = QtWidgets.QDoubleSpinBox()
        self.zStepSpin.setDecimals(0)
        self.zStepSpin.setRange(1, 5000)
        self.zStepSpin.setSuffix(' nm')
        self.zStepSpin.setKeyboardTracking(False)
        self.zStepSpin.setToolTip('Axial step size. Suggested default: 150 nm.')
        self.zStepSpin.valueChanged.connect(self._emitEdited)

        self.zLengthSpin = QtWidgets.QDoubleSpinBox()
        self.zLengthSpin.setDecimals(3)
        self.zLengthSpin.setRange(0.001, 1000.0)
        self.zLengthSpin.setSuffix(' µm')
        self.zLengthSpin.setKeyboardTracking(False)
        self.zLengthSpin.setToolTip('Editable axial scan span. Default: 1 µm.')
        self.zLengthSpin.valueChanged.connect(self._emitEdited)

        self.pointsLabel = QtWidgets.QLabel('–')
        self.pointsLabel.setWordWrap(True)
        layout.addWidget(QtWidgets.QLabel('Scan type'), 0, 0)
        layout.addWidget(self.scanTypeCombo, 0, 1)
        layout.addWidget(QtWidgets.QLabel('Lateral step'), 1, 0)
        layout.addWidget(self.lateralStepSpin, 1, 1)
        layout.addWidget(QtWidgets.QLabel('Z scan length'), 2, 0)
        layout.addWidget(self.zLengthSpin, 2, 1)
        layout.addWidget(QtWidgets.QLabel('Z step'), 3, 0)
        layout.addWidget(self.zStepSpin, 3, 1)
        layout.addWidget(QtWidgets.QLabel('Sampling'), 4, 0)
        layout.addWidget(self.pointsLabel, 4, 1)
        self.content.addWidget(box)

        # Sequence: the named devices and fixed readout timing come from setup;
        # OFF and ON durations are the intentionally small user-facing subset.
        box, layout = self._group('RESOLFT sequence')
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.addItem('RESOLFT', 'resolft')
        self.modeCombo.addItem('Confocal', 'confocal')
        self.modeCombo.setToolTip('Confocal uses the same sequence without the OFF pulse.')
        self.modeCombo.currentIndexChanged.connect(self._emitEdited)

        self.offLabel = QtWidgets.QLabel('OFF')
        self.offTimeSpin = QtWidgets.QDoubleSpinBox()
        self.offTimeSpin.setDecimals(3)
        self.offTimeSpin.setRange(0.001, 1000.0)
        self.offTimeSpin.setSuffix(' ms')
        self.offTimeSpin.setKeyboardTracking(False)
        self.offTimeSpin.valueChanged.connect(self._emitEdited)

        self.onLabel = QtWidgets.QLabel('ON')
        self.onTimeSpin = QtWidgets.QDoubleSpinBox()
        self.onTimeSpin.setDecimals(3)
        self.onTimeSpin.setRange(0.001, 1000.0)
        self.onTimeSpin.setSuffix(' ms')
        self.onTimeSpin.setKeyboardTracking(False)
        self.onTimeSpin.valueChanged.connect(self._emitEdited)

        self.readoutLabel = QtWidgets.QLabel('Read-out')
        self.readoutValue = self._info_value()
        self.delayValue = self._info_value()
        self.cameraLabel = QtWidgets.QLabel('Camera readout')
        self.cameraValue = self._info_value()
        self.dwellValue = self._info_value()
        self.dwellValue.setToolTip(
            'Pulse durations + a setup-configured initial delay and delays between active pulses + camera readout.'
        )

        layout.addWidget(QtWidgets.QLabel('Mode'), 0, 0)
        layout.addWidget(self.modeCombo, 0, 1)
        layout.addWidget(self.onLabel, 1, 0)
        layout.addWidget(self.onTimeSpin, 1, 1)
        layout.addWidget(self.offLabel, 2, 0)
        layout.addWidget(self.offTimeSpin, 2, 1)
        layout.addWidget(self.readoutLabel, 3, 0)
        layout.addWidget(self.readoutValue, 3, 1)
        layout.addWidget(QtWidgets.QLabel('Inter-pulse delay'), 4, 0)
        layout.addWidget(self.delayValue, 4, 1)
        layout.addWidget(self.cameraLabel, 5, 0)
        layout.addWidget(self.cameraValue, 5, 1)
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addWidget(line, 6, 0, 1, 2)
        layout.addWidget(QtWidgets.QLabel('Pixel dwell'), 7, 0)
        layout.addWidget(self.dwellValue, 7, 1)
        self.content.addWidget(box)

    def configure(self, limits):
        self._limits = limits
        fx, fy = limits.fov_um
        self.fovValue.setText(f'{fx:.3g} × {fy:.3g} µm')
        self.periodValue.setText(f'{_format_length(limits.ulens_period_um)} (uLens period)')
        pieces = [
            f'ON: {limits.on_laser or "not set"}',
            f'OFF: {limits.off_laser or "not set"}',
            f'read-out: {limits.readout_laser or "not set"}',
        ]
        if limits.readout_detector:
            pieces.append(f'camera: {limits.readout_detector}')
        self.sequenceDevicesValue.setText('  ·  '.join(pieces))
        self.offLabel.setText(f'OFF time  ({limits.off_laser or "not set"})')
        self.onLabel.setText(f'ON time  ({limits.on_laser or "not set"})')
        self.readoutLabel.setText(f'Read-out  ({limits.readout_laser or "not set"})')
        self.cameraLabel.setText(
            f'Camera readout  ({limits.readout_detector})'
            if limits.readout_detector else 'Camera readout')
        self.readoutValue.setText(_format_time(limits.readout_time_s))
        self.delayValue.setText(_format_time(limits.inter_pulse_delay_s))
        self.cameraValue.setText(_format_time(limits.camera_readout_time_s))

    def setPlan(self, plan: MoNaLISAScanPlan):
        self._plan = plan
        self._updating = True
        try:
            index = self.scanTypeCombo.findText(plan.scan_type)
            self.scanTypeCombo.setCurrentIndex(max(0, index))
            self.lateralStepSpin.setValue(plan.lateral_step_um * 1000.0)
            self.zLengthSpin.setValue(plan.z_length_um)
            self.zStepSpin.setValue(plan.z_step_um * 1000.0)
            mode_index = self.modeCombo.findData(plan.mode)
            self.modeCombo.setCurrentIndex(max(0, mode_index))
            self.offTimeSpin.setValue(plan.off_time_s * 1000.0)
            self.onTimeSpin.setValue(plan.on_time_s * 1000.0)
            self._updateDerived()
        finally:
            self._updating = False

    def setEstimate(self, seconds: float, note: str = '', refusal: str = ''):
        if refusal:
            self.frameTimeLabel.setText('–')
            self.estimateNote.setText(refusal)
            self.estimateNote.setStyleSheet('color: #d9534f;')
            return
        self.frameTimeLabel.setText(f'≈ {_format_time(seconds)}')
        self.estimateNote.setText(note)
        self.estimateNote.setStyleSheet('')

    def _emitEdited(self, *args):
        if self._updating or self._limits is None:
            return
        plan = MoNaLISAScanPlan(
            scan_type=self.scanTypeCombo.currentText(),
            lateral_step_um=self.lateralStepSpin.value() / 1000.0,
            z_length_um=self.zLengthSpin.value(),
            z_step_um=self.zStepSpin.value() / 1000.0,
            mode=str(self.modeCombo.currentData()),
            off_time_s=self.offTimeSpin.value() * 1e-3,
            on_time_s=self.onTimeSpin.value() * 1e-3,
        )
        self._plan = plan
        self._updateDerived()
        self.sigPlanEdited.emit(plan)

    def _updateDerived(self):
        if self._plan is None or self._limits is None:
            return
        z_used = 'Z' in self._plan.scan_type
        self.zLengthSpin.setEnabled(z_used and not self.isRunning())
        self.zStepSpin.setEnabled(z_used and not self.isRunning())
        self.offTimeSpin.setEnabled(self._plan.mode == 'resolft' and not self.isRunning())
        pixels = self._plan.pixels(self._limits)
        labels = [str(value) for value in pixels]
        self.pointsLabel.setText(' × '.join(labels) + ' positions')
        self.dwellValue.setText(_format_time(self._plan.dwell_s(self._limits)))

    def _runningChanged(self, running: bool):
        enabled = not running
        for widget in (
            self.scanTypeCombo, self.lateralStepSpin, self.modeCombo, self.onTimeSpin,
        ):
            widget.setEnabled(enabled)
        self.zLengthSpin.setEnabled(
            enabled and self._plan is not None and 'Z' in self._plan.scan_type)
        self.zStepSpin.setEnabled(enabled and self._plan is not None and 'Z' in self._plan.scan_type)
        self.offTimeSpin.setEnabled(
            enabled and self._plan is not None and self._plan.mode == 'resolft')


class ScanWidgetSimpleMoNaLISA(ScanCloakPanel):
    backendWidgetClass = ScanWidgetAdvanced
    viewClass = MoNaLISAView
    title = 'MoNaLISA scan'


__all__ = ['MoNaLISAView', 'ScanWidgetSimpleMoNaLISA']
