"""SimpleMoNaLISA cloak: compact profile-driven acquisition and screening panel."""

from __future__ import annotations

from typing import Optional

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model.monalisa_profiles import (
    MONALISA_BINDING_BOUND,
    profile_fingerprint,
)
from imswitch.imcontrol.model.simple_monalisa_scan import MoNaLISAScanPlan, SCAN_TYPES
from .ScanCloakPanel import ScanCloakPanel, ScanCloakView
from .ScanWidgetAdvanced import ScanWidgetAdvanced
from .MoNaLISAProfileEditor import MoNaLISAProfileEditor


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
    sigScreeningRequested = QtCore.Signal()
    sigLiveRoleToggled = QtCore.Signal(str, bool)

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
        self._liveRoleChecks = {}

        self.liveBox.hide()
        self.startButton.setText('Acquire')
        self.startButton.setToolTip('Run one MoNaLISA acquisition with these settings.')
        self.screeningButton = QtWidgets.QPushButton('Live')
        self.screeningButton.setToolTip(
            'Use the selected profile roles for screening at their current Live laser powers.'
        )
        self.screeningButton.clicked.connect(self.sigScreeningRequested.emit)
        # Base layout: stretch, hidden generic Live, Acquire, Stop.
        self.runRow.insertWidget(2, self.screeningButton)
        self.stopButton.setToolTip(
            'Switch off the lasers enabled by MoNaLISA Live screening. '
            'The camera live view and trigger mode are left running.'
        )
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
        box, layout = self._group('Microscope')
        self.fovValue = self._info_value()
        self.periodValue = self._info_value()
        self.delayValue = self._info_value()
        self.cameraValue = self._info_value()
        layout.addWidget(QtWidgets.QLabel('Imaging FOV'), 0, 0)
        layout.addWidget(self.fovValue, 0, 1)
        layout.addWidget(QtWidgets.QLabel('Scan length'), 1, 0)
        layout.addWidget(self.periodValue, 1, 1)
        layout.addWidget(QtWidgets.QLabel('Inter-pulse delay'), 2, 0)
        layout.addWidget(self.delayValue, 2, 1)
        layout.addWidget(QtWidgets.QLabel('Camera readout'), 3, 0)
        layout.addWidget(self.cameraValue, 3, 1)
        box.setToolTip('Fixed microscope/setup facts; they are not acquisition choices.')
        self.content.addWidget(box)

        box, layout = self._group('Scan')
        self.scanTypeCombo = QtWidgets.QComboBox()
        self.scanTypeCombo.addItems(SCAN_TYPES)
        self.scanTypeCombo.currentTextChanged.connect(self._emitEdited)

        self.lateralStepSpin = QtWidgets.QDoubleSpinBox()
        self.lateralStepSpin.setDecimals(0)
        self.lateralStepSpin.setRange(1, 5000)
        self.lateralStepSpin.setSuffix(' nm')
        self.lateralStepSpin.setKeyboardTracking(False)
        self.lateralStepSpin.valueChanged.connect(self._emitEdited)

        self.zStepSpin = QtWidgets.QDoubleSpinBox()
        self.zStepSpin.setDecimals(0)
        self.zStepSpin.setRange(1, 5000)
        self.zStepSpin.setSuffix(' nm')
        self.zStepSpin.setKeyboardTracking(False)
        self.zStepSpin.valueChanged.connect(self._emitEdited)

        self.zLengthSpin = QtWidgets.QDoubleSpinBox()
        self.zLengthSpin.setDecimals(3)
        self.zLengthSpin.setRange(0.001, 1000.0)
        self.zLengthSpin.setSuffix(' µm')
        self.zLengthSpin.setKeyboardTracking(False)
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

        box, layout = self._group('Illumination sequence')
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

        profileRow = QtWidgets.QWidget()
        profileRowLayout = QtWidgets.QHBoxLayout(profileRow)
        profileRowLayout.setContentsMargins(0, 0, 0, 0)
        profileRowLayout.setSpacing(4)
        profileRowLayout.addWidget(self.profileCombo, 1)
        profileRowLayout.addWidget(self.profileMenuButton)

        layout.addWidget(QtWidgets.QLabel('Profile'), 0, 0)
        layout.addWidget(profileRow, 0, 1)
        layout.addWidget(QtWidgets.QLabel('Mode'), 1, 0)
        layout.addWidget(self.modeCombo, 1, 1)
        layout.addWidget(self.bindingLabel, 2, 0, 1, 2)

        self.stepsWidget = QtWidgets.QWidget()
        self.stepsLayout = QtWidgets.QGridLayout(self.stepsWidget)
        self.stepsLayout.setContentsMargins(0, 6, 0, 0)
        self.stepsLayout.setHorizontalSpacing(8)
        self.stepsLayout.setVerticalSpacing(5)
        self.stepsLayout.addWidget(QtWidgets.QLabel('Role'), 0, 0)
        self.stepsLayout.addWidget(QtWidgets.QLabel('Step'), 0, 1)
        self.stepsLayout.addWidget(QtWidgets.QLabel('Devices'), 0, 2)
        self.stepsLayout.addWidget(QtWidgets.QLabel('Duration'), 0, 3)
        layout.addWidget(self.stepsWidget, 3, 0, 1, 2)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addWidget(line, 4, 0, 1, 2)
        self.dwellValue = self._info_value()
        layout.addWidget(QtWidgets.QLabel('Pixel dwell'), 5, 0)
        layout.addWidget(self.dwellValue, 5, 1)
        self.content.addWidget(box)

        box, layout = self._group('Live screening')
        self.liveScreeningBox = box
        self.liveRolesWidget = QtWidgets.QWidget()
        self.liveRolesLayout = QtWidgets.QHBoxLayout(self.liveRolesWidget)
        self.liveRolesLayout.setContentsMargins(0, 0, 0, 0)
        self.liveRolesLayout.setSpacing(10)
        self.liveRolesLayout.addStretch(1)
        layout.addWidget(QtWidgets.QLabel('Illumination'), 0, 0)
        layout.addWidget(self.liveRolesWidget, 0, 1)
        box.setToolTip(
            'Choose which illumination roles participate in screening. '
            'Only laser ON/OFF state is controlled here; Live power stays in the Laser widget.'
        )
        self.content.addWidget(box)

    def setLiveRoles(self, roles, selected_roles, *, available=True, reason=''):
        """Render profile-role screening choices.

        ``roles`` is an iterable of ``(role, label, devices)`` tuples. The
        profile/mode owns the candidate roles; the controller owns the saved
        selection and hardware behavior.
        """
        self._updating = True
        try:
            while self.liveRolesLayout.count():
                item = self.liveRolesLayout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            self._liveRoleChecks = {}
            selected = set(selected_roles or ())
            for role, label, devices in roles:
                devices = tuple(devices or ())
                text = str(label)
                if devices:
                    text += f' ({" + ".join(devices)})'
                check = QtWidgets.QCheckBox(text)
                check.setChecked(str(role) in selected)
                check.setToolTip(
                    f'{label} screening illumination. Uses the current Live power.\n'
                    + ('Devices: ' + ' + '.join(devices) if devices else 'No laser devices.')
                )
                check.toggled.connect(
                    lambda checked, role=str(role): self._liveRoleChanged(role, checked)
                )
                self.liveRolesLayout.addWidget(check)
                self._liveRoleChecks[str(role)] = check
            self.liveRolesLayout.addStretch(1)
            self._liveAvailable = bool(available)
            self._liveUnavailableReason = str(reason or '')
            baseTip = (
                'Choose which illumination roles participate in screening. '
                'Only laser ON/OFF state is controlled here; Live power stays in the Laser widget.'
            )
            self.liveScreeningBox.setToolTip(
                self._liveUnavailableReason if self._liveUnavailableReason else baseTip
            )
        finally:
            self._updating = False
        self._updateControlState()

    def _liveRoleChanged(self, role, checked):
        if self._updating:
            return
        self.sigLiveRoleToggled.emit(str(role), bool(checked))

    def selectedLiveRoles(self):
        return {
            role for role, check in self._liveRoleChecks.items()
            if check.isChecked()
        }

    def setScreeningActive(self, active: bool):
        self._screeningActive = bool(active)
        self._updateControlState()

    def configure(self, limits):
        self._limits = limits
        fx, fy = limits.fov_um
        self.fovValue.setText(f'{fx:.3g} × {fy:.3g} µm')
        self.periodValue.setText(f'{_format_length(limits.ulens_period_um)} (uLens period)')
        self.delayValue.setText(_format_time(limits.inter_pulse_delay_s))
        self.cameraValue.setText(_format_time(limits.camera_readout_time_s))
        self._updating = True
        try:
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
        for row in range(self.stepsLayout.rowCount() - 1, 0, -1):
            for column in range(4):
                item = self.stepsLayout.itemAtPosition(row, column)
                if item is None:
                    continue
                widget = item.widget()
                if widget is not None:
                    self.stepsLayout.removeWidget(widget)
                    widget.deleteLater()
        self._stepWidgets = {}

    def _rebuildSteps(self, profile):
        self._clearStepRows()
        self._renderedProfileId = profile.id
        self._renderedProfileFingerprint = profile_fingerprint(profile)
        for row, step in enumerate(profile.steps, start=1):
            role = QtWidgets.QLabel(step.role.upper())
            role.setToolTip('MoNaLISA semantic role; sequence order is defined by the row order.')
            label = QtWidgets.QLabel(step.label)
            devices = QtWidgets.QLabel(' + '.join(step.ttl_devices))
            devices.setWordWrap(True)
            if step.duration_editable:
                duration = QtWidgets.QDoubleSpinBox()
                duration.setDecimals(3)
                duration.setRange(0.001, 100000.0)
                duration.setSuffix(' ms')
                duration.setKeyboardTracking(False)
                duration.valueChanged.connect(self._emitEdited)
            else:
                duration = QtWidgets.QLabel(_format_time(step.default_duration_s))
            self.stepsLayout.addWidget(role, row, 0)
            self.stepsLayout.addWidget(label, row, 1)
            self.stepsLayout.addWidget(devices, row, 2)
            self.stepsLayout.addWidget(duration, row, 3)
            self._stepWidgets[step.id] = (role, label, devices, duration)

    def setPlan(self, plan: Optional[MoNaLISAScanPlan]):
        self._plan = plan
        if self._limits is None:
            return
        if plan is None:
            self._updating = True
            try:
                index = self.scanTypeCombo.findText(self._limits.default_scan_type)
                self.scanTypeCombo.setCurrentIndex(max(0, index))
                self.lateralStepSpin.setValue(self._limits.default_lateral_step_um * 1000.0)
                self.zLengthSpin.setValue(self._limits.default_z_scan_length_um)
                self.zStepSpin.setValue(self._limits.default_z_step_um * 1000.0)
                self.modeCombo.clear()
                self._clearStepRows()
                self._renderedProfileId = None
                self._renderedProfileFingerprint = None
                self.pointsLabel.setText('–')
                self.dwellValue.setText('–')
            finally:
                self._updating = False
            self._updateControlState()
            return
        self._updating = True
        try:
            index = self.scanTypeCombo.findText(plan.scan_type)
            self.scanTypeCombo.setCurrentIndex(max(0, index))
            self.lateralStepSpin.setValue(plan.lateral_step_um * 1000.0)
            self.zLengthSpin.setValue(plan.z_length_um)
            self.zStepSpin.setValue(plan.z_step_um * 1000.0)

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
                # The profile id is stable across live edits. Rebuild both the
                # mode combo and step rows whenever the profile definition
                # itself changes so additions/deletions/renames are visible
                # immediately without requiring a profile switch.
                self._rebuildModes(profile, plan.mode_id)
                self._rebuildSteps(profile)
            else:
                mode_index = self.modeCombo.findData(plan.mode_id)
                self.modeCombo.setCurrentIndex(max(0, mode_index))
            mode = profile.mode(plan.mode_id)
            for step in profile.steps:
                widgets = self._stepWidgets[step.id]
                devices_label = widgets[2]
                duration = widgets[3]
                if step.duration_editable:
                    duration.setValue(
                        plan.step_durations_s.get(step.id, step.default_duration_s) * 1e3
                    )
                active_devices = mode.devices_for_step(step)
                devices_label.setText(' + '.join(active_devices) if active_devices else '—')
                devices_label.setToolTip(
                    'Profile devices: ' + ' + '.join(step.ttl_devices)
                    + ('\nMode enables: ' + ' + '.join(active_devices) if active_devices else '\nDisabled in this mode')
                )
                enabled = bool(active_devices)
                for widget in widgets:
                    widget.setEnabled(enabled and self._bindingIsBound() and not self.isRunning())
                if not step.duration_editable:
                    duration.setText(_format_time(
                        plan.step_durations_s.get(step.id, step.default_duration_s)
                    ))
            self._updateDerived()
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
        if refusal:
            self.frameTimeLabel.setText('–')
            self.estimateNote.setText(refusal)
            self.estimateNote.setStyleSheet('color: #d9534f;')
            return
        self.frameTimeLabel.setText(f'≈ {_format_time(seconds)}')
        self.estimateNote.setText(note)
        self.estimateNote.setStyleSheet('')

    def _bindingIsBound(self):
        return self._binding is not None and self._binding.status == MONALISA_BINDING_BOUND

    def currentProfileId(self):
        value = self.profileCombo.currentData()
        return str(value) if value else None

    def askProfileName(self, title, prompt, initial=''):
        text, ok = QtWidgets.QInputDialog.getText(self, title, prompt, text=str(initial))
        text = str(text).strip()
        return text if ok and text else None

    def editProfileDefinition(self, profile, available_devices):
        dialog = MoNaLISAProfileEditor(profile, available_devices, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.profile()

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
            if widgets is None or not step.duration_editable:
                continue
            durations[step.id] = widgets[3].value() * 1e-3
        plan = MoNaLISAScanPlan(
            scan_type=self.scanTypeCombo.currentText(),
            lateral_step_um=self.lateralStepSpin.value() / 1000.0,
            z_length_um=self.zLengthSpin.value(),
            z_step_um=self.zStepSpin.value() / 1000.0,
            profile_id=self._plan.profile_id,
            mode_id=self._plan.mode_id,
            step_durations_s=durations,
        )
        self._plan = plan
        self._updateDerived()
        self.sigPlanEdited.emit(plan)

    def _updateDerived(self):
        if self._plan is None or self._limits is None:
            return
        pixels = self._plan.pixels(self._limits)
        self.pointsLabel.setText(' × '.join(str(value) for value in pixels) + ' positions')
        try:
            self.dwellValue.setText(_format_time(self._plan.dwell_s(self._limits)))
        except Exception:
            self.dwellValue.setText('–')
        self._updateControlState()

    def _updateControlState(self):
        bound = self._bindingIsBound()
        running = self.isRunning()
        screening = self._screeningActive
        editable = bound and not running and not screening
        self.startButton.setEnabled(bound and not running)
        self.stopButton.setEnabled(screening and not running)
        self.screeningButton.setEnabled(
            bound and self._liveAvailable and not running and not screening
        )
        liveTip = self._liveUnavailableReason if not self._liveAvailable else (
            'Use the selected profile roles for screening at their current Live laser powers.'
        )
        self.screeningButton.setToolTip(liveTip)
        hasProfiles = bool(
            self._limits is not None and self._limits.profile_store.profiles
        )
        self.profileCombo.setEnabled(hasProfiles and not running and not screening)
        self.profileMenuButton.setEnabled(not running and not screening)
        self._updateProfileActions()
        self.modeCombo.setEnabled(editable)
        self.scanTypeCombo.setEnabled(editable)
        self.lateralStepSpin.setEnabled(editable)
        z_used = self._plan is not None and 'Z' in self._plan.scan_type
        self.zLengthSpin.setEnabled(editable and z_used)
        self.zStepSpin.setEnabled(editable and z_used)
        if self._plan is None or self._limits is None:
            return
        profile = self._profile(self._plan.profile_id)
        mode = profile.mode(self._plan.mode_id)
        for step in profile.steps:
            widgets = self._stepWidgets.get(step.id)
            if widgets is None:
                continue
            step_enabled = editable and bool(mode.devices_for_step(step))
            for widget in widgets:
                widget.setEnabled(step_enabled)
        for check in self._liveRoleChecks.values():
            check.setEnabled(bound and self._liveAvailable and not running)

    def _runningChanged(self, running: bool):
        self._updateControlState()


class ScanWidgetSimpleMoNaLISA(ScanCloakPanel):
    backendWidgetClass = ScanWidgetAdvanced
    viewClass = MoNaLISAView
    title = 'MoNaLISA scan'


__all__ = ['MoNaLISAView', 'ScanWidgetSimpleMoNaLISA']
