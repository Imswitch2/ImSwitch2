"""SimpleMoNaLISA cloak: compact profile-driven acquisition and screening panel."""

from __future__ import annotations

from typing import Optional

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.view.guitools import colorutils

from imswitch.imcontrol.model.monalisa_profiles import (
    MONALISA_BINDING_BOUND,
    profile_fingerprint,
)
from imswitch.imcontrol.model.simple_monalisa_scan import MoNaLISAScanPlan, SCAN_TYPES
from imswitch.imcontrol.model.simple_scan import log_position, log_value
from .ScanCloakPanel import ScanCloakPanel, ScanCloakView
from .ScanWidgetAdvanced import ScanWidgetAdvanced
from .MoNaLISAProfileEditor import MoNaLISAProfileEditor
from .MoNaLISASetupEditor import MoNaLISASetupEditor


_SLIDER_TICKS = 1000


class _AcquisitionIndicator(QtWidgets.QWidget):
    """Small deterministic red record marker (ring + filled centre)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setToolTip('An acquisition detector records during this step.')

    def paintEvent(self, event):
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        red = QtGui.QColor('#e31a1c')
        centre = self.rect().center()
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(red, 2.0))
        painter.drawEllipse(centre, 7, 7)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(red)
        painter.drawEllipse(centre, 3, 3)


class _RangeControl(QtWidgets.QWidget):
    """Compact configurable slider + precise spin box."""

    valueChanged = QtCore.Signal(float)
    editingFinished = QtCore.Signal(float)

    def __init__(self, *, suffix='', decimals=3, low=0.001, high=10000.0, parent=None):
        super().__init__(parent)
        self._updating = False
        self._low = float(low)
        self._high = float(high)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, _SLIDER_TICKS)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setDecimals(int(decimals))
        self.spin.setRange(0.0, 1000000000.0)
        self.spin.setSuffix(str(suffix))
        self.spin.setKeyboardTracking(False)
        self.spin.setMinimumWidth(88)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.slider.valueChanged.connect(self._sliderChanged)
        self.slider.sliderReleased.connect(self._sliderReleased)
        self.spin.valueChanged.connect(self._spinChanged)
        self.spin.editingFinished.connect(self._spinEditingFinished)
        self.setRange(self._low, self._high)

    def setRange(self, low, high):
        low = float(low)
        high = float(high)
        if high <= low:
            high = low + max(abs(low) * 0.01, 1e-9)
        self._low, self._high = low, high
        self.setValue(self.value())

    def _fractionForValue(self, value):
        value = min(max(float(value), self._low), self._high)
        if self._low > 0 and self._high > 0:
            return log_position(value, self._low, self._high)
        return (value - self._low) / (self._high - self._low)

    def _valueForFraction(self, fraction):
        fraction = min(max(float(fraction), 0.0), 1.0)
        if self._low > 0 and self._high > 0:
            return log_value(fraction, self._low, self._high)
        return self._low + fraction * (self._high - self._low)

    def _sliderChanged(self, position):
        if self._updating:
            return
        value = self._valueForFraction(position / _SLIDER_TICKS)
        self._updating = True
        self.spin.setValue(value)
        self._updating = False
        self.valueChanged.emit(self.spin.value())

    def _sliderReleased(self):
        if not self._updating:
            self.editingFinished.emit(self.value())

    def _spinChanged(self, value):
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(round(self._fractionForValue(value) * _SLIDER_TICKS))
        self._updating = False
        self.valueChanged.emit(float(value))

    def _spinEditingFinished(self):
        if not self._updating:
            self.editingFinished.emit(self.value())

    def setValue(self, value):
        value = max(0.0, float(value))
        self._updating = True
        self.spin.setValue(value)
        self.slider.setValue(round(self._fractionForValue(value) * _SLIDER_TICKS))
        self._updating = False

    def value(self):
        return self.spin.value()


class _SequenceStepDisplay(QtWidgets.QWidget):
    """Role-first sequence display with explicit custom-device badges."""

    _SEMANTIC_ROLES = {'on', 'off', 'readout'}

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.badgesWidget = QtWidgets.QWidget()
        self.badgesLayout = QtWidgets.QHBoxLayout(self.badgesWidget)
        self.badgesLayout.setContentsMargins(0, 0, 0, 0)
        self.badgesLayout.setSpacing(4)
        layout.addWidget(self.badgesWidget)
        self.acquireIndicator = _AcquisitionIndicator()
        self.acquireIndicator.hide()
        layout.addWidget(self.acquireIndicator)
        layout.addStretch(1)

    @staticmethod
    def _textColour(hex_colour):
        try:
            value = str(hex_colour).lstrip('#')
            r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            return '#111111' if luminance > 155 else '#ffffff'
        except Exception:
            return '#ffffff'

    @staticmethod
    def _deviceColour(device, wavelengths):
        wavelength = wavelengths.get(device)
        if wavelength is None:
            return '#666666'
        try:
            return colorutils.wavelengthToHex(float(wavelength), gamma=6.0)
        except Exception:
            return '#666666'

    def _clearBadges(self):
        while self.badgesLayout.count():
            item = self.badgesLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _addBadge(self, text, background, tooltip=''):
        badge = QtWidgets.QLabel(str(text))
        font = badge.font()
        font.setBold(True)
        badge.setFont(font)
        badge.setContentsMargins(10, 3, 10, 3)
        badge.setMinimumWidth(82)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        text_colour = self._textColour(background) if str(background).startswith('#') else '#111111'
        badge.setStyleSheet(
            'QLabel {'
            f' background: {background}; color: {text_colour};'
            ' border-radius: 5px; border: 1px solid rgba(0,0,0,55);'
            ' }'
        )
        if tooltip:
            badge.setToolTip(str(tooltip))
        self.badgesLayout.addWidget(badge)
        return badge

    def _semanticBackground(self, devices, wavelengths):
        colours = []
        for device in devices:
            wavelength = wavelengths.get(device)
            if wavelength is None:
                continue
            colour = self._deviceColour(device, wavelengths)
            if colour not in colours:
                colours.append(colour)
        if not colours:
            return '#666666'
        if len(colours) == 1:
            return colours[0]
        stops = []
        count = len(colours)
        for index, colour in enumerate(colours):
            start = index / count
            end = (index + 1) / count
            stops.append(f'stop:{start:.3f} {colour}, stop:{end:.3f} {colour}')
        return 'qlineargradient(x1:0,y1:0,x2:1,y2:0,' + ','.join(stops) + ')'

    def setStep(self, active_devices, wavelengths, detector_devices, *, step_label='', role=''):
        active_devices = tuple(active_devices or ())
        detector_devices = set(detector_devices or ())
        acquisition_devices = [d for d in active_devices if d in detector_devices]
        visible_devices = [d for d in active_devices if d not in detector_devices]
        role = str(role or '').strip().lower()
        self._clearBadges()

        details = [str(step_label or role or 'Step')]
        if role:
            details.append(f'Role: {role}')
        if visible_devices:
            details.append('Devices: ' + ' + '.join(visible_devices))
        if acquisition_devices:
            details.append('Acquire: ' + ' + '.join(acquisition_devices))
        tooltip = '\n'.join(details)

        if role in self._SEMANTIC_ROLES:
            background = self._semanticBackground(visible_devices, wavelengths)
            self._addBadge(role.upper(), background, tooltip)
        else:
            # Custom/alignment steps expose their actual hardware directly.
            # Detectors are represented separately by the red acquisition icon.
            if visible_devices:
                for device in visible_devices:
                    self._addBadge(
                        device,
                        self._deviceColour(device, wavelengths),
                        tooltip,
                    )
            else:
                self._addBadge(str(step_label or 'ACQUISITION').upper(), '#666666', tooltip)

        self.acquireIndicator.setVisible(bool(acquisition_devices))
        self.acquireIndicator.setToolTip(
            'Records during this step' + (
                ': ' + ' + '.join(acquisition_devices) if acquisition_devices else '.'
            )
        )
        self.setToolTip(tooltip)

def _format_length(um: float) -> str:
    if um < 1.0:
        return f'{um * 1000:.0f} nm'
    return f'{um:.3g} µm'


def _format_time(seconds: float) -> str:
    if seconds < 1e-3:
        return f'{seconds * 1e6:.0f} µs'
    if seconds < 1:
        return f'{seconds * 1e3:.3g} ms'
    if seconds < 60:
        return f'{seconds:.2f} s'
    if seconds < 3600:
        return f'{seconds / 60:.1f} min'
    return f'{seconds / 3600:.1f} h'


class MoNaLISAView(ScanCloakView):
    """MoNaLISA acquisition page with profile-driven Live screening controls."""

    sigPlanEdited = QtCore.Signal(object)
    sigProfileRequested = QtCore.Signal(str)
    sigModeRequested = QtCore.Signal(str)
    sigProfileNewRequested = QtCore.Signal()
    sigProfileDuplicateRequested = QtCore.Signal(str)
    sigProfileEditRequested = QtCore.Signal(str)
    sigProfileRenameRequested = QtCore.Signal(str)
    sigProfileDeleteRequested = QtCore.Signal(str)
    sigSetupEditRequested = QtCore.Signal()
    sigCameraReadoutAutoRequested = QtCore.Signal()
    sigScreeningRequested = QtCore.Signal()
    sigLiveSourceToggled = QtCore.Signal(str, bool)
    sigLiveSourcesEditRequested = QtCore.Signal()

    def __init__(self, parent=None, *, napariViewer=None):
        super().__init__(parent)
        self._updating = False
        self._plan = None
        self._limits = None
        self._binding = None
        self._stepWidgets = {}
        self._renderedProfileId = None
        self._renderedProfileFingerprint = None
        self._screeningActive = False
        self._liveAvailable = False
        self._liveUnavailableReason = ''
        self._liveSourceChecks = {}

        self.liveBox.hide()
        self.startButton.setText('Acquire')
        self.startButton.setToolTip('Run one MoNaLISA acquisition with these settings.')
        self.screeningButton = QtWidgets.QPushButton('Live')
        self.screeningButton.setToolTip(
            'Use the selected Live illumination sources at their current Live laser powers.'
        )
        self.screeningButton.clicked.connect(self.sigScreeningRequested.emit)
        # Base layout: stretch, hidden generic Live, Acquire, Stop. Reorder the
        # visible Simple controls as Live / Stop / Acquire.
        self.runRow.removeWidget(self.startButton)
        self.runRow.removeWidget(self.stopButton)
        self.runRow.addWidget(self.screeningButton)
        self.runRow.addWidget(self.stopButton)
        self.runRow.addSpacing(12)
        self.saveAcquireCheck = QtWidgets.QCheckBox('Save')
        self.saveAcquireCheck.setToolTip(
            'Record this acquisition using the Recording panel folder, file name and '
            'save format. Recording is armed before the scan starts.'
        )
        self.runRow.addWidget(self.saveAcquireCheck)
        self.runRow.addWidget(self.startButton)
        self.stopButton.setToolTip(
            'Switch off the lasers enabled by MoNaLISA Live screening and prepare '
            'the acquisition camera for external frame trigger. Camera live view '
            'remains running.'
        )
        # MoNaLISA keeps the operational controls at the very top. The scan
        # estimate is shown once, inside the compact Scan summary instead of
        # consuming a second top row.
        self.runTitle = QtWidgets.QLabel('LIVE')
        run_font = self.runTitle.font()
        run_font.setBold(True)
        self.runTitle.setFont(run_font)
        self.runRow.insertWidget(0, self.runTitle)
        self.liveSourcesButton = QtWidgets.QToolButton()
        self.liveSourcesButton.setText('…')
        self.liveSourcesButton.setAutoRaise(True)
        self.liveSourcesButton.setFixedWidth(24)
        self.liveSourcesButton.setToolTip(
            'Choose which illumination sources are available for Live screening.'
        )
        self.liveSourcesButton.clicked.connect(self.sigLiveSourcesEditRequested.emit)
        self.runRow.insertWidget(1, self.liveSourcesButton)
        self.estimateTitle.hide()
        self.frameTimeLabel.hide()
        self.estimateNote.hide()

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
        # The screening source selection belongs directly under the top action
        # row; it is intentionally not another boxed settings section.
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        self.content.addWidget(separator)

        self.liveRolesWidget = QtWidgets.QWidget()
        self.liveRolesLayout = QtWidgets.QHBoxLayout(self.liveRolesWidget)
        self.liveRolesLayout.setContentsMargins(4, 0, 4, 2)
        self.liveRolesLayout.setSpacing(10)
        self.liveRolesLayout.addStretch(1)
        self.liveRolesWidget.setToolTip(
            'Choose which configured illumination sources are currently enabled for '
            'screening. Live power remains owned by the Laser widget.'
        )
        self.content.addWidget(self.liveRolesWidget)

        # Profile + mode is the main acquisition recipe selector. Setup stays
        # one click away but no longer consumes a summary row in the main UI.
        box, layout = self._group('Profile')
        self.profileCombo = QtWidgets.QComboBox()
        self.profileCombo.setToolTip(
            'Profiles are selected only in Simple. Advanced edits may preserve or break the selection, '
            'but never select another profile automatically.'
        )
        self.profileCombo.currentIndexChanged.connect(self._profileChanged)
        self.modeCombo = QtWidgets.QComboBox()
        self.modeCombo.currentIndexChanged.connect(self._modeChanged)
        self.bindingLabel = QtWidgets.QLabel('')
        self.bindingLabel.setWordWrap(True)

        self.profileMenuButton = QtWidgets.QToolButton()
        self.profileMenuButton.setText('⋯')
        self.profileMenuButton.setToolTip('Create, duplicate, edit, rename or delete MoNaLISA profiles.')
        self.profileMenuButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        profileMenu = QtWidgets.QMenu(self.profileMenuButton)
        self.newProfileAction = profileMenu.addAction('New profile…')
        self.duplicateProfileAction = profileMenu.addAction('Duplicate…')
        self.editProfileAction = profileMenu.addAction('Edit profile…')
        self.renameProfileAction = profileMenu.addAction('Rename…')
        self.deleteProfileAction = profileMenu.addAction('Delete…')
        self.profileMenuButton.setMenu(profileMenu)
        self.newProfileAction.triggered.connect(self.sigProfileNewRequested.emit)
        self.duplicateProfileAction.triggered.connect(self._requestDuplicateProfile)
        self.editProfileAction.triggered.connect(self._requestEditProfile)
        self.renameProfileAction.triggered.connect(self._requestRenameProfile)
        self.deleteProfileAction.triggered.connect(self._requestDeleteProfile)

        self.setupButton = QtWidgets.QPushButton('Setup…')
        self.setupButton.setToolTip('Edit and persist all SimpleMoNaLISA setup parameters.')
        self.setupButton.clicked.connect(self.sigSetupEditRequested.emit)

        profileRow = QtWidgets.QWidget()
        profileRowLayout = QtWidgets.QHBoxLayout(profileRow)
        profileRowLayout.setContentsMargins(0, 0, 0, 0)
        profileRowLayout.setSpacing(6)
        profileRowLayout.addWidget(QtWidgets.QLabel('Profile'))
        profileRowLayout.addWidget(self.profileCombo, 2)
        profileRowLayout.addWidget(self.profileMenuButton)
        profileRowLayout.addSpacing(8)
        profileRowLayout.addWidget(QtWidgets.QLabel('Mode'))
        profileRowLayout.addWidget(self.modeCombo, 1)
        profileRowLayout.addWidget(self.setupButton)
        layout.addWidget(profileRow, 0, 0, 1, 2)
        layout.addWidget(self.bindingLabel, 1, 0, 1, 2)
        self.profileGeometryLabel = QtWidgets.QLabel('')
        self.profileGeometryLabel.hide()
        self.content.addWidget(box)

        # Scan geometry uses one visual language everywhere: slider + exact
        # spin box. Z controls disappear entirely when the selected scan does
        # not use Z.
        box, layout = self._group('Scan')
        self.scanTypeCombo = QtWidgets.QComboBox()
        self.scanTypeCombo.addItems(SCAN_TYPES)
        self.scanTypeCombo.currentTextChanged.connect(self._emitEdited)
        scanTypeRow = QtWidgets.QWidget()
        scanTypeLayout = QtWidgets.QHBoxLayout(scanTypeRow)
        scanTypeLayout.setContentsMargins(0, 0, 0, 0)
        scanTypeLayout.addWidget(QtWidgets.QLabel('Scan'))
        scanTypeLayout.addWidget(self.scanTypeCombo)
        scanTypeLayout.addStretch(1)
        self.scanSummaryLabel = QtWidgets.QLabel('–')
        self.scanSummaryLabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        scanTypeLayout.addWidget(self.scanSummaryLabel, 1)
        layout.addWidget(scanTypeRow, 0, 0, 1, 2)

        self.lateralStepControl = _RangeControl(suffix=' nm', decimals=1, low=5.0, high=1000.0)
        self.lateralStepControl.valueChanged.connect(self._emitEdited)
        self.lateralLengthControl = _RangeControl(suffix=' µm', decimals=4, low=0.05, high=10.0)
        self.lateralLengthControl.valueChanged.connect(self._emitEdited)
        self.zStepControl = _RangeControl(suffix=' nm', decimals=1, low=5.0, high=1000.0)
        self.zStepControl.valueChanged.connect(self._emitEdited)
        self.zLengthControl = _RangeControl(suffix=' µm', decimals=4, low=0.1, high=10.0)
        self.zLengthControl.valueChanged.connect(self._emitEdited)

        # Compatibility aliases used by controller/view code below while the
        # implementation remains based on the common control.
        self.lateralStepSpin = self.lateralStepControl.spin
        self.lateralStepSlider = self.lateralStepControl.slider
        self.lateralLengthSpin = self.lateralLengthControl.spin
        self.zStepSpin = self.zStepControl.spin
        self.zLengthSpin = self.zLengthControl.spin

        def field(label, control):
            widget = QtWidgets.QWidget()
            field_layout = QtWidgets.QVBoxLayout(widget)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(2)
            caption = QtWidgets.QLabel(label)
            caption_font = caption.font()
            caption_font.setPointSizeF(max(7.0, caption_font.pointSizeF() * 0.9))
            caption.setFont(caption_font)
            field_layout.addWidget(caption)
            field_layout.addWidget(control)
            return widget, caption

        self.pixelField, self.pixelCaption = field('Pixel size', self.lateralStepControl)
        self.lateralLengthField, self.lateralLengthCaption = field('Scan length', self.lateralLengthControl)
        self.zStepField, self.zStepCaption = field('Z step', self.zStepControl)
        self.zLengthField, self.zLengthCaption = field('Z scan length', self.zLengthControl)
        layout.addWidget(self.pixelField, 1, 0)
        layout.addWidget(self.lateralLengthField, 1, 1)
        layout.addWidget(self.zStepField, 2, 0)
        layout.addWidget(self.zLengthField, 2, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        self.content.addWidget(box)

        # Role-first acquisition recipe. Hardware is expert information in the
        # badge tooltip; detector use is surfaced automatically with ◉.
        box, layout = self._group('Illumination sequence')
        self.stepsWidget = QtWidgets.QWidget()
        self.stepsLayout = QtWidgets.QGridLayout(self.stepsWidget)
        self.stepsLayout.setContentsMargins(0, 2, 0, 0)
        self.stepsLayout.setHorizontalSpacing(10)
        self.stepsLayout.setVerticalSpacing(5)
        self.stepsLayout.setColumnStretch(1, 1)
        layout.addWidget(self.stepsWidget, 0, 0, 1, 2)

        self.cameraReadoutRow = QtWidgets.QWidget()
        readout_layout = QtWidgets.QHBoxLayout(self.cameraReadoutRow)
        readout_layout.setContentsMargins(0, 0, 0, 0)
        readout_layout.setSpacing(6)
        self.cameraReadoutInfo = QtWidgets.QLabel('Camera readout: –')
        info_font = self.cameraReadoutInfo.font()
        info_font.setPointSizeF(max(7.0, info_font.pointSizeF() * 0.9))
        self.cameraReadoutInfo.setFont(info_font)
        self.cameraReadoutStatus = QtWidgets.QLabel('')
        self.cameraReadoutStatus.setFont(info_font)
        self.cameraReadoutAutoButton = QtWidgets.QPushButton('Auto')
        self.cameraReadoutAutoButton.setMaximumWidth(58)
        self.cameraReadoutAutoButton.setToolTip(
            'Set this profile camera readout automatically from the current detector '
            'readout plus the configured safety margin.'
        )
        self.cameraReadoutAutoButton.clicked.connect(
            self.sigCameraReadoutAutoRequested.emit
        )
        readout_layout.addWidget(self.cameraReadoutInfo)
        readout_layout.addWidget(self.cameraReadoutStatus)
        readout_layout.addStretch(1)
        readout_layout.addWidget(self.cameraReadoutAutoButton)
        layout.addWidget(self.cameraReadoutRow, 1, 0, 1, 2)
        self.content.addWidget(box)

        self._samplingText = '–'
        self._estimateText = ''
        self._estimateRefusal = ''

    def editLiveSources(self, sources, selected_sources):
        """Choose the setup-level illumination sources shown in the LIVE row."""
        sources = tuple(str(source) for source in sources)
        selected = set(str(source) for source in (selected_sources or ()))

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Live screening sources')
        dialog.resize(420, 460)
        layout = QtWidgets.QVBoxLayout(dialog)

        note = QtWidgets.QLabel(
            'Choose which configured illumination sources are available in the '
            'Simple MoNaLISA LIVE row. This selection is independent of profiles.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        filterEdit = QtWidgets.QLineEdit()
        filterEdit.setPlaceholderText('Filter sources…')
        layout.addWidget(filterEdit)

        sourceList = QtWidgets.QListWidget()
        items = []
        for source in sources:
            item = QtWidgets.QListWidgetItem(source)
            item.setData(QtCore.Qt.UserRole, source)
            item.setCheckState(
                QtCore.Qt.Checked if source in selected else QtCore.Qt.Unchecked
            )
            sourceList.addItem(item)
            items.append(item)
        layout.addWidget(sourceList, 1)

        if not sources:
            empty = QtWidgets.QLabel('No illumination sources are configured.')
            empty.setEnabled(False)
            layout.insertWidget(2, empty)

        def filterSources(text):
            needle = str(text or '').strip().lower()
            for item in items:
                item.setHidden(needle not in item.text().lower())

        filterEdit.textChanged.connect(filterSources)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return tuple(
            item.data(QtCore.Qt.UserRole)
            for item in items
            if item.checkState() == QtCore.Qt.Checked
        )

    def setLiveSources(self, sources, selected_sources, *, available=True, reason=''):
        """Render the setup-owned Live screening illumination sources."""
        self._updating = True
        try:
            while self.liveRolesLayout.count():
                item = self.liveRolesLayout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            self._liveSourceChecks = {}
            selected = set(selected_sources or ())
            for source in sources:
                source = str(source)
                check = QtWidgets.QCheckBox(source)
                check.setChecked(source in selected)
                check.setToolTip(
                    f'{source} Live screening illumination. Uses the current Live power.'
                )
                check.toggled.connect(
                    lambda checked, source=source: self._liveSourceChanged(source, checked)
                )
                self.liveRolesLayout.addWidget(check)
                self._liveSourceChecks[source] = check
            self.liveRolesLayout.addStretch(1)
            self._liveAvailable = bool(available)
            self._liveUnavailableReason = str(reason or '')
            baseTip = (
                'Choose which illumination sources participate in screening. '
                'Use the button next to LIVE to change which sources are shown. '
                'Live power stays in the Laser widget.'
            )
            self.liveRolesWidget.setToolTip(
                self._liveUnavailableReason if self._liveUnavailableReason else baseTip
            )
        finally:
            self._updating = False
        self._updateControlState()

    def _liveSourceChanged(self, source, checked):
        if self._updating:
            return
        self.sigLiveSourceToggled.emit(str(source), bool(checked))

    def selectedLiveSources(self):
        return {
            source for source, check in self._liveSourceChecks.items()
            if check.isChecked()
        }

    def saveAcquisitionRequested(self) -> bool:
        return bool(self.saveAcquireCheck.isChecked())

    def setScreeningActive(self, active: bool):
        self._screeningActive = bool(active)
        self._updateControlState()

    def setSetupSummary(self, text: str, tooltip: str = ''):
        # Setup details stay out of the compact main UI; retain them as useful
        # hover information on the Setup button.
        details = str(tooltip or text or '')
        self.setupButton.setToolTip(
            'Edit and persist all SimpleMoNaLISA setup parameters.'
            + (f'\n\n{details}' if details else '')
        )

    def setCameraReadoutInfo(
        self, readout_ms: float, source_text: str, *, status_text: str = '',
        severity: str = '', auto_available: bool = False, tooltip: str = '',
    ):
        self.cameraReadoutInfo.setText(
            f'Camera readout: {float(readout_ms):.3g} ms · {str(source_text)}'
        )
        self.cameraReadoutInfo.setToolTip(str(tooltip or ''))
        self.cameraReadoutStatus.setText(str(status_text or ''))
        if severity == 'error':
            self.cameraReadoutStatus.setStyleSheet('color: #d9534f;')
        elif severity == 'warning':
            self.cameraReadoutStatus.setStyleSheet('color: #d98c10;')
        else:
            self.cameraReadoutStatus.setStyleSheet('')
        self.cameraReadoutStatus.setToolTip(str(tooltip or ''))
        self.cameraReadoutAutoButton.setVisible(bool(auto_available))
        self.cameraReadoutAutoButton.setEnabled(bool(auto_available))

    def _updateProfileGeometryLabel(self):
        if self._plan is None or self._limits is None:
            return
        profile = self._profile(self._plan.profile_id)
        try:
            period = self._limits.ulens_period_um(profile.id)
            period_text = _format_length(period)
        except Exception:
            period_text = 'uLens period missing'
        source = (
            'Scan length follows the uLens period.'
            if profile.scan_extent_source == 'ulens'
            else f'Manual scan length default: {_format_length(self._plan.lateral_length_um)}.'
        )
        owner = 'setup' if self._limits.ulens_period_policy == 'fixed' else 'profile'
        self.profileCombo.setToolTip(
            f'uLens: {period_text} ({owner}). {source}\n'
            'Use Edit profile… for profile-owned optical details.'
        )

    def configure(self, limits):
        self._limits = limits
        self._updating = True
        try:
            ranges = limits.slider_ranges
            self.lateralStepControl.setRange(*ranges['pixelSizeNm'])
            self.lateralLengthControl.setRange(*ranges['lateralScanLengthUm'])
            self.zStepControl.setRange(*ranges['zStepNm'])
            self.zLengthControl.setRange(*ranges['zScanLengthUm'])
            self.profileCombo.clear()
            for profile_id, profile in limits.profile_store.profiles.items():
                self.profileCombo.addItem(profile.label, profile_id)
            if not limits.profile_store.profiles:
                self.profileCombo.addItem('No profile configured', None)
                self.modeCombo.clear()
                self._clearStepRows()
                self._renderedProfileId = None
                self._renderedProfileFingerprint = None
        finally:
            self._updating = False
        self._updateProfileGeometryLabel()
        self._updateControlState()

    def _profile(self, profile_id=None):
        if self._limits is None:
            return None
        return self._limits.profile_store.profile(
            profile_id or (self._plan.profile_id if self._plan is not None else None)
        )

    def _rebuildModes(self, profile, selected_mode):
        self.modeCombo.clear()
        for mode_id, mode in profile.modes.items():
            self.modeCombo.addItem(mode.label, mode_id)
        index = self.modeCombo.findData(selected_mode)
        self.modeCombo.setCurrentIndex(max(0, index))

    def _clearStepRows(self):
        while self.stepsLayout.count():
            item = self.stepsLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._stepWidgets = {}

    def _rebuildSteps(self, profile):
        self._clearStepRows()
        self._renderedProfileId = profile.id
        self._renderedProfileFingerprint = profile_fingerprint(profile)
        duration_range = self._limits.slider_ranges['stepDurationMs']
        for row, step in enumerate(profile.steps):
            badge = _SequenceStepDisplay()
            if step.role == 'readout':
                duration = QtWidgets.QLabel(
                    _format_time(self._limits.readout_pulse_time_s)
                )
                duration.setToolTip(
                    'READOUT pulse duration is setup-owned. Change it from Setup….'
                )
            elif step.duration_editable:
                duration = _RangeControl(
                    suffix=' ms', decimals=3,
                    low=duration_range[0], high=duration_range[1],
                )
                duration.valueChanged.connect(self._emitEdited)
            else:
                duration = QtWidgets.QLabel(_format_time(step.default_duration_s))
            self.stepsLayout.addWidget(badge, row, 0)
            self.stepsLayout.addWidget(duration, row, 1)
            self._stepWidgets[step.id] = (badge, duration)

    def setPlan(self, plan: Optional[MoNaLISAScanPlan]):
        self._plan = plan
        if self._limits is None:
            return
        if plan is None:
            self._updating = True
            try:
                index = self.scanTypeCombo.findText(self._limits.default_scan_type)
                self.scanTypeCombo.setCurrentIndex(max(0, index))
                self.lateralLengthControl.setValue(self._limits.fixed_ulens_period_um)
                self.lateralStepControl.setValue(self._limits.default_lateral_step_um * 1000.0)
                self.zLengthControl.setValue(self._limits.default_z_scan_length_um)
                self.zStepControl.setValue(self._limits.default_z_step_um * 1000.0)
                self.modeCombo.clear()
                self._clearStepRows()
                self._renderedProfileId = None
                self._renderedProfileFingerprint = None
                self._samplingText = '–'
                self._refreshScanSummary()
            finally:
                self._updating = False
            self._updateControlState()
            return

        self._updating = True
        try:
            index = self.scanTypeCombo.findText(plan.scan_type)
            self.scanTypeCombo.setCurrentIndex(max(0, index))
            self.lateralLengthControl.setValue(plan.lateral_length_um)
            self.lateralStepControl.setValue(plan.lateral_step_um * 1000.0)
            self.zLengthControl.setValue(plan.z_length_um)
            self.zStepControl.setValue(plan.z_step_um * 1000.0)

            profile_index = self.profileCombo.findData(plan.profile_id)
            if profile_index < 0:
                self.profileCombo.addItem(plan.profile_id, plan.profile_id)
                profile_index = self.profileCombo.findData(plan.profile_id)
            self.profileCombo.setCurrentIndex(profile_index)
            profile = self._profile(plan.profile_id)
            current_fingerprint = profile_fingerprint(profile)
            if (
                self._renderedProfileId != profile.id
                or self._renderedProfileFingerprint != current_fingerprint
            ):
                self._rebuildModes(profile, plan.mode_id)
                self._rebuildSteps(profile)
            else:
                mode_index = self.modeCombo.findData(plan.mode_id)
                self.modeCombo.setCurrentIndex(max(0, mode_index))

            mode = profile.mode(plan.mode_id)
            for step in profile.steps:
                badge, duration = self._stepWidgets[step.id]
                if step.role == 'readout':
                    duration.setText(_format_time(self._limits.readout_pulse_time_s))
                elif step.duration_editable:
                    duration.setValue(
                        plan.step_durations_s.get(step.id, step.default_duration_s) * 1e3
                    )
                active_devices = mode.devices_for_step(step)
                badge.setStep(
                    active_devices,
                    self._limits.device_wavelengths_nm,
                    self._limits.detector_devices,
                    step_label=step.label,
                    role=step.role,
                )
                enabled = bool(active_devices)
                badge.setVisible(enabled)
                duration.setVisible(enabled)
                badge.setEnabled(enabled and self._bindingIsBound() and not self.isRunning())
                duration.setEnabled(enabled and self._bindingIsBound() and not self.isRunning())
                if step.role != 'readout' and not step.duration_editable:
                    duration.setText(_format_time(
                        plan.step_durations_s.get(step.id, step.default_duration_s)
                    ))
            self._updateDerived()
            self._updateProfileGeometryLabel()
        finally:
            self._updating = False

    def setBinding(self, binding):
        self._binding = binding
        if self._limits is None:
            return
        self._updating = True
        try:
            if not self._limits.profile_store.profiles:
                self.profileCombo.clear()
                self.profileCombo.addItem('No profile configured', None)
                self.modeCombo.clear()
                self._clearStepRows()
                self.bindingLabel.setText(
                    'Create a profile from the ⋯ menu to enable Simple MoNaLISA.'
                )
            elif binding is None or not getattr(binding, 'profile_id', None):
                custom = self.profileCombo.findData(None)
                if custom >= 0:
                    self.profileCombo.removeItem(custom)
                default_id = self._limits.profile_store.default_profile
                index = self.profileCombo.findData(default_id)
                if index >= 0:
                    self.profileCombo.setCurrentIndex(index)
                self.bindingLabel.setText(
                    'Profile geometry is incomplete. Edit/select a profile to enable acquisition.'
                )
            elif binding is not None and binding.status != MONALISA_BINDING_BOUND:
                if self.profileCombo.findData(None) < 0:
                    self.profileCombo.insertItem(0, 'Advanced / custom', None)
                self.profileCombo.setCurrentIndex(self.profileCombo.findData(None))
                profile = self._limits.profile_store.profile(binding.profile_id)
                mode = profile.mode(binding.mode_id)
                self.bindingLabel.setText(
                    f'Modified from {profile.label} · {mode.label}. '
                    'Select a profile to replace the custom illumination sequence.'
                )
            else:
                custom = self.profileCombo.findData(None)
                if custom >= 0:
                    self.profileCombo.removeItem(custom)
                if self._plan is not None:
                    self.profileCombo.setCurrentIndex(
                        self.profileCombo.findData(self._plan.profile_id)
                    )
                self.bindingLabel.setText('')
        finally:
            self._updating = False
        self._updateControlState()

    def setEstimate(self, seconds: float, note: str = '', refusal: str = ''):
        self._estimateRefusal = str(refusal or '')
        self._estimateText = '' if refusal else f'≈ {_format_time(seconds)}'
        self.scanSummaryLabel.setToolTip(str(note or refusal or ''))
        self._refreshScanSummary()

    def _refreshScanSummary(self):
        if self._estimateRefusal:
            self.scanSummaryLabel.setText(self._estimateRefusal)
            self.scanSummaryLabel.setStyleSheet('color: #d9534f;')
            return
        parts = [part for part in (self._samplingText, self._estimateText) if part and part != '–']
        self.scanSummaryLabel.setText(' · '.join(parts) if parts else '–')
        self.scanSummaryLabel.setStyleSheet('')

    def _bindingIsBound(self):
        return self._binding is not None and self._binding.status == MONALISA_BINDING_BOUND

    def currentProfileId(self):
        value = self.profileCombo.currentData()
        return str(value) if value else None

    def askProfileName(self, title, prompt, initial=''):
        text, ok = QtWidgets.QInputDialog.getText(self, title, prompt, text=str(initial))
        text = str(text).strip()
        return text if ok and text else None

    def editProfileDefinition(self, profile, available_devices, limits):
        dialog = MoNaLISAProfileEditor(
            profile,
            available_devices,
            self,
            ulens_period_policy=limits.ulens_period_policy,
            fixed_ulens_period_um=limits.fixed_ulens_period_um,
            readout_pulse_time_ms=limits.readout_pulse_time_s * 1e3,
            default_camera_readout_time_ms=limits.default_camera_readout_time_s * 1e3,
        )
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.profile()

    def editSetupDefinition(self, configured, resolved):
        dialog = MoNaLISASetupEditor(configured, resolved, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.configurationPatch()

    def confirmProfileDelete(self, label):
        result = QtWidgets.QMessageBox.question(
            self,
            'Delete MoNaLISA profile?',
            f'Delete profile "{label}"? This changes the current setup file.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return result == QtWidgets.QMessageBox.Yes

    def _requestDuplicateProfile(self):
        profile_id = self.currentProfileId()
        if profile_id:
            self.sigProfileDuplicateRequested.emit(profile_id)

    def _requestEditProfile(self):
        profile_id = self.currentProfileId()
        if profile_id:
            self.sigProfileEditRequested.emit(profile_id)

    def _requestRenameProfile(self):
        profile_id = self.currentProfileId()
        if profile_id:
            self.sigProfileRenameRequested.emit(profile_id)

    def _requestDeleteProfile(self):
        profile_id = self.currentProfileId()
        if profile_id:
            self.sigProfileDeleteRequested.emit(profile_id)

    def _updateProfileActions(self):
        profile_id = self.currentProfileId()
        available = profile_id is not None and not self.isRunning()
        self.duplicateProfileAction.setEnabled(available)
        self.editProfileAction.setEnabled(available)
        self.renameProfileAction.setEnabled(available)
        self.deleteProfileAction.setEnabled(available)
        self.newProfileAction.setEnabled(not self.isRunning())

    def _profileChanged(self, index):
        if self._updating:
            return
        profile_id = self.profileCombo.itemData(index)
        if profile_id:
            self.sigProfileRequested.emit(str(profile_id))

    def _modeChanged(self, index):
        if self._updating or not self._bindingIsBound():
            return
        mode_id = self.modeCombo.itemData(index)
        if mode_id:
            self.sigModeRequested.emit(str(mode_id))

    def _emitEdited(self, *args):
        if self._updating or self._limits is None or self._plan is None or not self._bindingIsBound():
            return
        durations = dict(self._plan.step_durations_s)
        profile = self._profile(self._plan.profile_id)
        for step in profile.steps:
            widgets = self._stepWidgets.get(step.id)
            if widgets is None or step.role == 'readout' or not step.duration_editable:
                continue
            durations[step.id] = widgets[1].value() * 1e-3
        plan = MoNaLISAScanPlan(
            scan_type=self.scanTypeCombo.currentText(),
            lateral_length_um=self.lateralLengthControl.value(),
            lateral_step_um=self.lateralStepControl.value() / 1000.0,
            z_length_um=self.zLengthControl.value(),
            z_step_um=self.zStepControl.value() / 1000.0,
            profile_id=self._plan.profile_id,
            mode_id=self._plan.mode_id,
            step_durations_s=durations,
        )
        self._plan = plan
        self._updateDerived()
        self._updateProfileGeometryLabel()
        self.sigPlanEdited.emit(plan)

    def _updateDerived(self):
        if self._plan is None or self._limits is None:
            return
        pixels = self._plan.pixels(self._limits)
        total = 1
        for value in pixels:
            total *= int(value)
        axes = ' × '.join(str(value) for value in pixels)
        self._samplingText = f'{axes} · {total} positions'
        self._refreshScanSummary()
        self._updateControlState()

    def _updateControlState(self):
        bound = self._bindingIsBound()
        running = self.isRunning()
        screening = self._screeningActive
        editable = bound and not running and not screening
        self.startButton.setEnabled(bound and not running)
        self.saveAcquireCheck.setEnabled(bound and not running)
        self.stopButton.setEnabled(screening and not running)
        self.screeningButton.setEnabled(
            bound and self._liveAvailable and not running and not screening
        )
        liveTip = self._liveUnavailableReason if not self._liveAvailable else (
            'Use the selected Live illumination sources at their current Live powers.'
        )
        self.screeningButton.setToolTip(liveTip)
        self.liveSourcesButton.setEnabled(not running and not screening)
        hasProfiles = bool(
            self._limits is not None and self._limits.profile_store.profiles
        )
        self.profileCombo.setEnabled(hasProfiles and not running and not screening)
        self.profileMenuButton.setEnabled(not running and not screening)
        self.setupButton.setEnabled(not running and not screening)
        self._updateProfileActions()
        self.modeCombo.setEnabled(editable)
        self.scanTypeCombo.setEnabled(editable)
        self.lateralStepControl.setEnabled(editable)

        z_used = self._plan is not None and 'Z' in self._plan.scan_type
        self.zStepField.setVisible(z_used)
        self.zLengthField.setVisible(z_used)
        self.zStepControl.setEnabled(editable and z_used)
        self.zLengthControl.setEnabled(editable and z_used)

        if self._plan is None or self._limits is None:
            self.lateralLengthControl.setEnabled(False)
            for check in self._liveSourceChecks.values():
                check.setEnabled(False)
            return
        profile = self._profile(self._plan.profile_id)
        manual_extent = profile.scan_extent_source == 'manual'
        self.lateralLengthControl.setEnabled(editable and manual_extent)
        self.lateralLengthControl.setToolTip(
            'Editable scan length for this profile.' if manual_extent
            else 'This profile locks lateral scan length to one uLens period.'
        )
        mode = profile.mode(self._plan.mode_id)
        for step in profile.steps:
            widgets = self._stepWidgets.get(step.id)
            if widgets is None:
                continue
            step_enabled = editable and bool(mode.devices_for_step(step))
            for widget in widgets:
                widget.setEnabled(step_enabled)
        for check in self._liveSourceChecks.values():
            check.setEnabled(bound and self._liveAvailable and not running)

    def _runningChanged(self, running: bool):
        self._updateControlState()


class ScanWidgetSimpleMoNaLISA(ScanCloakPanel):
    backendWidgetClass = ScanWidgetAdvanced
    viewClass = MoNaLISAView
    title = 'MoNaLISA scan'

    # A loaded scan may be perfectly valid while no longer matching the
    # current Simple profile/setup.  Keep those decisions separate from the
    # generic cloak note (which is also used for transient page/run messages).
    sigLoadResolutionActionRequested = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loadResolutionFrame = QtWidgets.QFrame()
        self.loadResolutionFrame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        frameLayout = QtWidgets.QVBoxLayout(self.loadResolutionFrame)
        frameLayout.setContentsMargins(8, 6, 8, 6)
        frameLayout.setSpacing(4)

        self.loadResolutionTitle = QtWidgets.QLabel('⚠  Loaded scan needs attention')
        titleFont = self.loadResolutionTitle.font()
        titleFont.setBold(True)
        self.loadResolutionTitle.setFont(titleFont)
        self.loadResolutionTitle.setStyleSheet('color: #d98c10;')
        frameLayout.addWidget(self.loadResolutionTitle)

        self.loadResolutionLabel = QtWidgets.QLabel('')
        self.loadResolutionLabel.setWordWrap(True)
        self.loadResolutionLabel.setStyleSheet('color: #d98c10;')
        frameLayout.addWidget(self.loadResolutionLabel)

        self.loadResolutionActions = QtWidgets.QHBoxLayout()
        self.loadResolutionActions.addStretch(1)
        frameLayout.addLayout(self.loadResolutionActions)
        self.loadResolutionButtons = {}
        self.loadResolutionFrame.setVisible(False)

        # Header is item 0; put load resolution directly below it and above
        # the generic note/stack so it remains visible on both pages.
        self.layout().insertWidget(1, self.loadResolutionFrame)

    def setLoadResolution(self, message: str = '', actions=()):
        while self.loadResolutionActions.count() > 1:
            item = self.loadResolutionActions.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.loadResolutionButtons = {}
        self.loadResolutionLabel.setText(str(message or ''))
        for action_id, label in actions:
            button = QtWidgets.QPushButton(str(label))
            button.clicked.connect(
                lambda checked=False, key=str(action_id):
                    self.sigLoadResolutionActionRequested.emit(key)
            )
            self.loadResolutionActions.addWidget(button)
            self.loadResolutionButtons[str(action_id)] = button
        self.loadResolutionFrame.setVisible(bool(message))

    def clearLoadResolution(self):
        self.setLoadResolution('', ())


__all__ = ['MoNaLISAView', 'ScanWidgetSimpleMoNaLISA']
