"""Editor for setup-owned SimpleMoNaLISA microscope parameters."""

from __future__ import annotations

from qtpy import QtWidgets

from imswitch.imcontrol.model.simple_monalisa_scan import (
    CONFIG_DEFAULTS,
    DEFAULT_SLIDER_RANGES,
    SCAN_TYPES,
)


class MoNaLISASetupEditor(QtWidgets.QDialog):
    """Edit setup-owned MoNaLISA parameters and return a config patch.

    Profiles are intentionally not edited here.  This dialog owns only values
    that describe the microscope / default SimpleMoNaLISA behavior and are
    persisted into ``scan.simpleMoNaLISA``.
    """

    def __init__(self, configured, resolved=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('MoNaLISA setup')
        self.resize(520, 500)
        self._configured = dict(configured or {})
        resolved = dict(resolved or {})

        root = QtWidgets.QVBoxLayout(self)

        detector_box = QtWidgets.QGroupBox('Detector-derived values')
        detector_form = QtWidgets.QFormLayout(detector_box)
        camera = str(resolved.get('detector') or 'No active profile camera')
        self.cameraLabel = QtWidgets.QLabel(camera)
        detector_form.addRow('Profile camera', self.cameraLabel)

        self.fovSource = QtWidgets.QComboBox()
        self.fovSource.addItem('Auto from camera ROI', 'detector')
        self.fovSource.addItem('Manual', 'manual')
        self.fovX = QtWidgets.QDoubleSpinBox()
        self.fovY = QtWidgets.QDoubleSpinBox()
        for spin in (self.fovX, self.fovY):
            spin.setRange(0.001, 100000.0)
            spin.setDecimals(3)
            spin.setSuffix(' µm')
            spin.setKeyboardTracking(False)
        fov_row = QtWidgets.QWidget()
        fov_layout = QtWidgets.QHBoxLayout(fov_row)
        fov_layout.setContentsMargins(0, 0, 0, 0)
        fov_layout.addWidget(self.fovX)
        fov_layout.addWidget(QtWidgets.QLabel('×'))
        fov_layout.addWidget(self.fovY)
        detector_form.addRow('FOV source', self.fovSource)
        detector_form.addRow('Manual FOV', fov_row)
        self.autoFovLabel = QtWidgets.QLabel(str(resolved.get('fov_text') or '–'))
        detector_form.addRow('Current auto FOV', self.autoFovLabel)

        self.detectorReadoutLabel = QtWidgets.QLabel(str(resolved.get('readout_text') or '–'))
        self.detectorReadoutLabel.setToolTip(
            'Current readout time reported by the profile camera. This is reference '
            'information only; it never changes scan timing automatically.'
        )
        detector_form.addRow('Current detector readout', self.detectorReadoutLabel)
        root.addWidget(detector_box)

        optics_box = QtWidgets.QGroupBox('Optics and timing')
        optics_form = QtWidgets.QFormLayout(optics_box)
        self.ulensPolicy = QtWidgets.QComboBox()
        self.ulensPolicy.addItem('Fixed for setup', 'fixed')
        self.ulensPolicy.addItem('Defined by each profile', 'per_profile')
        self.ulensSpin = QtWidgets.QDoubleSpinBox()
        self.ulensSpin.setRange(0.001, 100000.0)
        self.ulensSpin.setDecimals(4)
        self.ulensSpin.setSuffix(' µm')
        self.ulensSpin.setKeyboardTracking(False)
        self.delaySpin = QtWidgets.QDoubleSpinBox()
        self.delaySpin.setRange(0.0, 100000.0)
        self.delaySpin.setDecimals(4)
        self.delaySpin.setSuffix(' ms')
        self.delaySpin.setKeyboardTracking(False)
        self.readoutPulseSpin = QtWidgets.QDoubleSpinBox()
        self.readoutPulseSpin.setRange(1.0, 100000.0)
        self.readoutPulseSpin.setDecimals(4)
        self.readoutPulseSpin.setSuffix(' ms')
        self.readoutPulseSpin.setKeyboardTracking(False)
        self.readoutPulseSpin.setToolTip(
            'Duration of every semantic READOUT illumination step. '
            'This is setup-owned and cannot be shorter than 1 ms.'
        )
        self.defaultCameraReadoutSpin = QtWidgets.QDoubleSpinBox()
        self.defaultCameraReadoutSpin.setRange(0.0, 100000.0)
        self.defaultCameraReadoutSpin.setDecimals(4)
        self.defaultCameraReadoutSpin.setSuffix(' ms')
        self.defaultCameraReadoutSpin.setKeyboardTracking(False)
        self.defaultCameraReadoutSpin.setToolTip(
            'Camera readout gap inherited by profiles that do not define their own value.'
        )
        self.cameraReadoutSafetyMarginSpin = QtWidgets.QDoubleSpinBox()
        self.cameraReadoutToleranceSpin = QtWidgets.QDoubleSpinBox()
        self.cameraReadoutUnoptimizedSpin = QtWidgets.QDoubleSpinBox()
        for spin in (self.cameraReadoutSafetyMarginSpin, self.cameraReadoutToleranceSpin,
                     self.cameraReadoutUnoptimizedSpin):
            spin.setRange(0.0, 1000.0)
            spin.setDecimals(4)
            spin.setSuffix(' ms')
            spin.setKeyboardTracking(False)
        self.cameraReadoutSafetyMarginSpin.setToolTip(
            'Added to the detector-reported readout when Auto calculates a safe profile value.'
        )
        self.cameraReadoutToleranceSpin.setToolTip(
            'Differences smaller than this are ignored when checking for too-fast timing.'
        )
        self.cameraReadoutUnoptimizedSpin.setToolTip(
            'Only report a safe readout as slower than needed when it exceeds the recommended '
            'value by more than this amount.'
        )
        optics_form.addRow('uLens period policy', self.ulensPolicy)
        optics_form.addRow('Fixed uLens period', self.ulensSpin)
        optics_form.addRow('Inter-pulse delay', self.delaySpin)
        optics_form.addRow('READOUT pulse', self.readoutPulseSpin)
        optics_form.addRow('Default camera readout', self.defaultCameraReadoutSpin)
        optics_form.addRow('Readout safety margin', self.cameraReadoutSafetyMarginSpin)
        optics_form.addRow('Readout tolerance', self.cameraReadoutToleranceSpin)
        optics_form.addRow('Unoptimized threshold', self.cameraReadoutUnoptimizedSpin)
        root.addWidget(optics_box)

        defaults_box = QtWidgets.QGroupBox('Scan defaults')
        defaults_form = QtWidgets.QFormLayout(defaults_box)
        self.scanType = QtWidgets.QComboBox()
        self.scanType.addItems(SCAN_TYPES)
        self.lateralStep = QtWidgets.QDoubleSpinBox()
        self.lateralStep.setRange(1.0, 5000.0)
        self.lateralStep.setDecimals(0)
        self.lateralStep.setSuffix(' nm')
        self.zLength = QtWidgets.QDoubleSpinBox()
        self.zLength.setRange(0.001, 1000.0)
        self.zLength.setDecimals(3)
        self.zLength.setSuffix(' µm')
        self.zStep = QtWidgets.QDoubleSpinBox()
        self.zStep.setRange(1.0, 5000.0)
        self.zStep.setDecimals(0)
        self.zStep.setSuffix(' nm')
        for spin in (self.lateralStep, self.zLength, self.zStep):
            spin.setKeyboardTracking(False)
        defaults_form.addRow('Default scan type', self.scanType)
        defaults_form.addRow('Default lateral step', self.lateralStep)
        defaults_form.addRow('Default Z range', self.zLength)
        defaults_form.addRow('Default Z step', self.zStep)
        root.addWidget(defaults_box)

        ranges_box = QtWidgets.QGroupBox('Slider ranges')
        ranges_grid = QtWidgets.QGridLayout(ranges_box)
        ranges_grid.addWidget(QtWidgets.QLabel('Control'), 0, 0)
        ranges_grid.addWidget(QtWidgets.QLabel('Min'), 0, 1)
        ranges_grid.addWidget(QtWidgets.QLabel('Max'), 0, 2)
        self.rangeSpins = {}
        range_specs = (
            ('pixelSizeNm', 'Pixel size', ' nm', 3),
            ('lateralScanLengthUm', 'Lateral scan length', ' µm', 4),
            ('zStepNm', 'Z step', ' nm', 3),
            ('zScanLengthUm', 'Z scan length', ' µm', 4),
            ('stepDurationMs', 'Sequence duration', ' ms', 4),
        )
        for row, (key, label, suffix, decimals) in enumerate(range_specs, start=1):
            low_spin = QtWidgets.QDoubleSpinBox()
            high_spin = QtWidgets.QDoubleSpinBox()
            for spin in (low_spin, high_spin):
                spin.setRange(0.0, 1000000.0)
                spin.setDecimals(decimals)
                spin.setSuffix(suffix)
                spin.setKeyboardTracking(False)
            ranges_grid.addWidget(QtWidgets.QLabel(label), row, 0)
            ranges_grid.addWidget(low_spin, row, 1)
            ranges_grid.addWidget(high_spin, row, 2)
            self.rangeSpins[key] = (low_spin, high_spin)
        root.addWidget(ranges_box)

        note = QtWidgets.QLabel(
            'Save writes these values into the active setup configuration. '
            'Profiles and their sequence definitions are edited separately.'
        )
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.fovSource.currentIndexChanged.connect(self._updateEnabled)
        self.ulensPolicy.currentIndexChanged.connect(self._updateEnabled)
        self._load()

    def _value(self, key):
        return self._configured.get(key, CONFIG_DEFAULTS[key])

    def _setComboData(self, combo, value):
        index = combo.findData(str(value))
        combo.setCurrentIndex(max(0, index))

    def _load(self):
        self._setComboData(self.fovSource, self._value('fovSource'))
        fov = self._value('fovUm')
        if isinstance(fov, (int, float)):
            fov = (float(fov), float(fov))
        fov = list(fov or CONFIG_DEFAULTS['fovUm'])
        self.fovX.setValue(float(fov[0]))
        self.fovY.setValue(float(fov[1] if len(fov) > 1 else fov[0]))
        default_readout = self._configured.get(
            'defaultCameraReadoutTimeMs',
            self._configured.get('cameraReadoutTimeMs', CONFIG_DEFAULTS['defaultCameraReadoutTimeMs']),
        )
        self.defaultCameraReadoutSpin.setValue(float(default_readout))
        self.cameraReadoutSafetyMarginSpin.setValue(float(self._value('cameraReadoutSafetyMarginMs')))
        self.cameraReadoutToleranceSpin.setValue(float(self._value('cameraReadoutToleranceMs')))
        self.cameraReadoutUnoptimizedSpin.setValue(
            float(self._value('cameraReadoutUnoptimizedThresholdMs'))
        )
        self._setComboData(self.ulensPolicy, self._value('ulensPeriodPolicy'))
        self.ulensSpin.setValue(float(self._value('ulensPeriodUm')))
        self.delaySpin.setValue(float(self._value('interPulseDelayMs')))
        self.readoutPulseSpin.setValue(max(1.0, float(self._value('readoutPulseTimeMs'))))
        scan_type = str(self._value('defaultScanType')).upper()
        self.scanType.setCurrentIndex(max(0, self.scanType.findText(scan_type)))
        self.lateralStep.setValue(float(self._value('defaultLateralStepUm')) * 1000.0)
        self.zLength.setValue(float(self._value('defaultZScanLengthUm')))
        self.zStep.setValue(float(self._value('defaultZStepUm')) * 1000.0)
        configured_ranges = self._configured.get('sliderRanges', {})
        if not isinstance(configured_ranges, dict):
            configured_ranges = {}
        for key, (low_spin, high_spin) in self.rangeSpins.items():
            low, high = configured_ranges.get(key, DEFAULT_SLIDER_RANGES[key])
            low_spin.setValue(float(low))
            high_spin.setValue(float(high))
        self._updateEnabled()

    def _updateEnabled(self, *args):
        self.fovX.setEnabled(self.fovSource.currentData() == 'manual')
        self.fovY.setEnabled(self.fovSource.currentData() == 'manual')
        self.ulensSpin.setEnabled(self.ulensPolicy.currentData() == 'fixed')

    def accept(self):
        for key, (low_spin, high_spin) in self.rangeSpins.items():
            if high_spin.value() <= low_spin.value():
                QtWidgets.QMessageBox.warning(
                    self,
                    'Invalid slider range',
                    f'The maximum for {key} must be greater than its minimum.',
                )
                return
        super().accept()

    def configurationPatch(self):
        return {
            'fovSource': str(self.fovSource.currentData()),
            'fovUm': [self.fovX.value(), self.fovY.value()],
            'defaultCameraReadoutTimeMs': self.defaultCameraReadoutSpin.value(),
            'cameraReadoutSafetyMarginMs': self.cameraReadoutSafetyMarginSpin.value(),
            'cameraReadoutToleranceMs': self.cameraReadoutToleranceSpin.value(),
            'cameraReadoutUnoptimizedThresholdMs': self.cameraReadoutUnoptimizedSpin.value(),
            'ulensPeriodPolicy': str(self.ulensPolicy.currentData()),
            'ulensPeriodUm': self.ulensSpin.value(),
            'interPulseDelayMs': self.delaySpin.value(),
            'readoutPulseTimeMs': max(1.0, self.readoutPulseSpin.value()),
            'defaultScanType': self.scanType.currentText(),
            'defaultLateralStepUm': self.lateralStep.value() / 1000.0,
            'defaultZScanLengthUm': self.zLength.value(),
            'defaultZStepUm': self.zStep.value() / 1000.0,
            'sliderRanges': {
                key: [low_spin.value(), high_spin.value()]
                for key, (low_spin, high_spin) in self.rangeSpins.items()
            },
        }


__all__ = ['MoNaLISASetupEditor']
