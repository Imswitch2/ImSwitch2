"""Live editor for data-driven SimpleMoNaLISA illumination profiles."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.model.monalisa_profiles import (
    MONALISA_STEP_ROLES,
    MoNaLISAProfileDefinition,
    MoNaLISAProfileError,
    mode_id_from_label,
    step_id_for_role,
)


class MoNaLISAProfileEditor(QtWidgets.QDialog):
    """Edit one profile as data; no profile-specific Python is generated."""

    def __init__(self, profile, available_devices, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Edit MoNaLISA profile — {profile.label}')
        self.resize(780, 590)
        self._profile_id = profile.id
        self._available_devices = tuple(str(name) for name in available_devices)
        self._updating = False
        self._steps = [
            {
                'id': step.id,
                'label': step.label,
                'role': step.role,
                'ttlDevices': list(step.ttl_devices),
                'defaultDurationMs': step.default_duration_s * 1e3,
                'durationEditable': step.duration_editable,
            }
            for step in profile.steps
        ]
        self._modes = {
            mode_id: {
                'label': mode.label,
                'enabledSteps': list(mode.enabled_steps),
                'enabledDevices': {
                    step_id: list(devices)
                    for step_id, devices in mode.enabled_devices.items()
                },
            }
            for mode_id, mode in profile.modes.items()
        }
        self._default_mode = profile.default_mode

        root = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QFormLayout()
        self.profileLabelEdit = QtWidgets.QLineEdit(profile.label)
        self.profileIdLabel = QtWidgets.QLabel(profile.id)
        self.profileIdLabel.setToolTip(
            'Stable internal id. Renaming a profile changes only its display label.'
        )
        self.defaultModeCombo = QtWidgets.QComboBox()
        self.defaultModeCombo.currentIndexChanged.connect(self._defaultModeChanged)
        top.addRow('Profile name', self.profileLabelEdit)
        top.addRow('Internal id', self.profileIdLabel)
        top.addRow('Default mode', self.defaultModeCombo)
        root.addLayout(top)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._buildStepsTab(), 'Sequence')
        tabs.addTab(self._buildModesTab(), 'Modes')
        root.addWidget(tabs, 1)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self._acceptIfValid)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._refreshStepsList(0)
        self._refreshModesList(0)
        self._refreshDefaultModes()

    def _buildStepsTab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(tab)

        left = QtWidgets.QVBoxLayout()
        self.stepsList = QtWidgets.QListWidget()
        self.stepsList.currentRowChanged.connect(self._loadStep)
        left.addWidget(self.stepsList, 1)
        row = QtWidgets.QHBoxLayout()
        for text, callback in (
            ('+', self._addStep), ('−', self._removeStep),
            ('↑', lambda: self._moveStep(-1)), ('↓', lambda: self._moveStep(1)),
        ):
            button = QtWidgets.QPushButton(text)
            button.setMaximumWidth(42)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch(1)
        left.addLayout(row)
        layout.addLayout(left, 1)

        detailBox = QtWidgets.QGroupBox('Selected step')
        form = QtWidgets.QFormLayout(detailBox)
        self.stepIdLabel = QtWidgets.QLabel('–')
        self.stepRoleCombo = QtWidgets.QComboBox()
        for role in MONALISA_STEP_ROLES:
            self.stepRoleCombo.addItem(role.upper(), role)
        self.stepLabelEdit = QtWidgets.QLineEdit()
        self.stepDurationSpin = QtWidgets.QDoubleSpinBox()
        self.stepDurationSpin.setDecimals(3)
        self.stepDurationSpin.setRange(0.001, 100000.0)
        self.stepDurationSpin.setSuffix(' ms')
        self.stepDurationSpin.setKeyboardTracking(False)
        self.stepEditableCheck = QtWidgets.QCheckBox('Editable in Simple scan')
        self.devicesList = QtWidgets.QListWidget()
        self.devicesList.setMinimumHeight(150)

        self.stepRoleCombo.currentIndexChanged.connect(self._stepDetailsChanged)
        self.stepLabelEdit.textEdited.connect(self._stepDetailsChanged)
        self.stepDurationSpin.valueChanged.connect(self._stepDetailsChanged)
        self.stepEditableCheck.toggled.connect(self._stepDetailsChanged)
        self.devicesList.itemChanged.connect(self._stepDevicesChanged)

        form.addRow('Internal id', self.stepIdLabel)
        form.addRow('Role', self.stepRoleCombo)
        form.addRow('Label', self.stepLabelEdit)
        form.addRow('TTL devices', self.devicesList)
        form.addRow('Default duration', self.stepDurationSpin)
        form.addRow('', self.stepEditableCheck)
        layout.addWidget(detailBox, 2)
        return tab

    def _buildModesTab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(tab)

        left = QtWidgets.QVBoxLayout()
        self.modesList = QtWidgets.QListWidget()
        self.modesList.currentRowChanged.connect(self._loadMode)
        left.addWidget(self.modesList, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton('+')
        remove = QtWidgets.QPushButton('−')
        add.setMaximumWidth(42)
        remove.setMaximumWidth(42)
        add.clicked.connect(self._addMode)
        remove.clicked.connect(self._removeMode)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        left.addLayout(row)
        layout.addLayout(left, 1)

        detailBox = QtWidgets.QGroupBox('Selected mode')
        form = QtWidgets.QFormLayout(detailBox)
        self.modeIdLabel = QtWidgets.QLabel('–')
        self.modeLabelEdit = QtWidgets.QLineEdit()
        self.modeDevicesTree = QtWidgets.QTreeWidget()
        self.modeDevicesTree.setHeaderLabels(['Step / device', 'Role'])
        self.modeDevicesTree.setMinimumHeight(260)
        self.modeLabelEdit.textEdited.connect(self._modeDetailsChanged)
        self.modeDevicesTree.itemChanged.connect(self._modeDevicesChanged)
        form.addRow('Internal id', self.modeIdLabel)
        form.addRow('Label', self.modeLabelEdit)
        form.addRow('Enabled devices', self.modeDevicesTree)
        layout.addWidget(detailBox, 2)
        return tab

    def _stepDisplay(self, step):
        devices = ' + '.join(step['ttlDevices']) or 'no device'
        return f"{step['role'].upper()} — {step['label']} — {devices}"

    def _refreshStepsList(self, selected=None):
        if selected is None:
            selected = self.stepsList.currentRow()
        self._updating = True
        try:
            self.stepsList.clear()
            for step in self._steps:
                self.stepsList.addItem(self._stepDisplay(step))
            if self._steps:
                self.stepsList.setCurrentRow(max(0, min(selected, len(self._steps) - 1)))
            self._refreshModeDeviceChoices()
        finally:
            self._updating = False
        self._loadStep(self.stepsList.currentRow())

    def _loadStep(self, row):
        if self._updating or row < 0 or row >= len(self._steps):
            return
        step = self._steps[row]
        self._updating = True
        try:
            self.stepIdLabel.setText(step['id'])
            self.stepRoleCombo.setCurrentIndex(self.stepRoleCombo.findData(step['role']))
            self.stepLabelEdit.setText(step['label'])
            self.stepDurationSpin.setValue(float(step['defaultDurationMs']))
            self.stepEditableCheck.setChecked(bool(step['durationEditable']))
            self.devicesList.clear()
            known = list(self._available_devices)
            for device in step['ttlDevices']:
                if device not in known:
                    known.append(device)
            for device in known:
                item = QtWidgets.QListWidgetItem(device)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(
                    QtCore.Qt.Checked if device in step['ttlDevices'] else QtCore.Qt.Unchecked
                )
                if device not in self._available_devices:
                    item.setText(f'{device}  [missing from setup]')
                    item.setData(QtCore.Qt.UserRole, device)
                else:
                    item.setData(QtCore.Qt.UserRole, device)
                self.devicesList.addItem(item)
        finally:
            self._updating = False

    def _stepDetailsChanged(self, *args):
        if self._updating:
            return
        row = self.stepsList.currentRow()
        if row < 0 or row >= len(self._steps):
            return
        step = self._steps[row]
        step['role'] = str(self.stepRoleCombo.currentData())
        step['label'] = self.stepLabelEdit.text().strip() or step['role'].upper()
        step['defaultDurationMs'] = self.stepDurationSpin.value()
        step['durationEditable'] = self.stepEditableCheck.isChecked()
        self.stepsList.item(row).setText(self._stepDisplay(step))
        self._refreshModeDeviceChoices()

    def _stepDevicesChanged(self, *args):
        if self._updating:
            return
        row = self.stepsList.currentRow()
        if row < 0 or row >= len(self._steps):
            return
        devices = []
        for index in range(self.devicesList.count()):
            item = self.devicesList.item(index)
            if item.checkState() == QtCore.Qt.Checked:
                devices.append(str(item.data(QtCore.Qt.UserRole)))
        self._steps[row]['ttlDevices'] = devices
        step_id = self._steps[row]['id']
        for mode in self._modes.values():
            overrides = mode.setdefault('enabledDevices', {})
            if step_id not in overrides:
                continue
            selected = [device for device in overrides[step_id] if device in devices]
            if selected:
                overrides[step_id] = selected
            else:
                overrides.pop(step_id, None)
                mode['enabledSteps'] = [
                    value for value in mode['enabledSteps'] if value != step_id
                ]
        self.stepsList.item(row).setText(self._stepDisplay(self._steps[row]))
        self._refreshModeDeviceChoices()

    def _addStep(self):
        existing = [step['id'] for step in self._steps]
        role = 'on'
        step_id = step_id_for_role(role, existing)
        device = self._available_devices[0] if self._available_devices else ''
        self._steps.append({
            'id': step_id,
            'label': role.upper(),
            'role': role,
            'ttlDevices': [device] if device else [],
            'defaultDurationMs': 1.0,
            'durationEditable': True,
        })
        current_mode = self._currentModeId()
        if current_mode in self._modes:
            self._modes[current_mode]['enabledSteps'].append(step_id)
        self._refreshStepsList(len(self._steps) - 1)
        self._refreshModesList(self.modesList.currentRow())

    def _removeStep(self):
        row = self.stepsList.currentRow()
        if row < 0 or len(self._steps) <= 1:
            return
        step_id = self._steps[row]['id']
        del self._steps[row]
        for mode in self._modes.values():
            mode['enabledSteps'] = [value for value in mode['enabledSteps'] if value != step_id]
            mode.setdefault('enabledDevices', {}).pop(step_id, None)
        self._refreshStepsList(max(0, row - 1))
        self._refreshModesList(self.modesList.currentRow())

    def _moveStep(self, delta):
        row = self.stepsList.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._steps):
            return
        self._steps[row], self._steps[target] = self._steps[target], self._steps[row]
        self._refreshStepsList(target)

    def _currentModeId(self):
        row = self.modesList.currentRow()
        if row < 0 or row >= len(self._modes):
            return None
        return list(self._modes)[row]

    def _refreshModesList(self, selected=None):
        ids = list(self._modes)
        if selected is None:
            selected = self.modesList.currentRow()
        self._updating = True
        try:
            self.modesList.clear()
            for mode_id in ids:
                mode = self._modes[mode_id]
                self.modesList.addItem(mode['label'])
            if ids:
                self.modesList.setCurrentRow(max(0, min(selected, len(ids) - 1)))
            self._refreshDefaultModes()
        finally:
            self._updating = False
        self._loadMode(self.modesList.currentRow())

    def _refreshDefaultModes(self):
        selected = self._default_mode
        self.defaultModeCombo.blockSignals(True)
        self.defaultModeCombo.clear()
        for mode_id, mode in self._modes.items():
            self.defaultModeCombo.addItem(mode['label'], mode_id)
        index = self.defaultModeCombo.findData(selected)
        self.defaultModeCombo.setCurrentIndex(max(0, index))
        self.defaultModeCombo.blockSignals(False)
        if self._modes and selected not in self._modes:
            self._default_mode = next(iter(self._modes))

    def _defaultModeChanged(self, index):
        if self._updating or index < 0:
            return
        mode_id = self.defaultModeCombo.itemData(index)
        if mode_id:
            self._default_mode = str(mode_id)

    def _loadMode(self, row):
        if self._updating:
            return
        ids = list(self._modes)
        if row < 0 or row >= len(ids):
            return
        mode_id = ids[row]
        mode = self._modes[mode_id]
        self._updating = True
        try:
            self.modeIdLabel.setText(mode_id)
            self.modeLabelEdit.setText(mode['label'])
            self._refreshModeDeviceChoices(mode_id)
        finally:
            self._updating = False

    def _refreshModeDeviceChoices(self, mode_id=None):
        if not hasattr(self, 'modeDevicesTree'):
            return
        mode_id = mode_id or self._currentModeId()
        if mode_id not in self._modes:
            return
        mode = self._modes[mode_id]
        enabled_steps = set(mode['enabledSteps'])
        overrides = mode.setdefault('enabledDevices', {})
        was_updating = self._updating
        self._updating = True
        try:
            self.modeDevicesTree.clear()
            for step in self._steps:
                step_item = QtWidgets.QTreeWidgetItem([step['label'], step['role'].upper()])
                step_item.setData(0, QtCore.Qt.UserRole, ('step', step['id']))
                step_item.setFlags(
                    step_item.flags()
                    | QtCore.Qt.ItemIsUserCheckable
                    | QtCore.Qt.ItemIsAutoTristate
                )
                self.modeDevicesTree.addTopLevelItem(step_item)
                if step['id'] in enabled_steps:
                    selected = set(overrides.get(step['id'], step['ttlDevices']))
                else:
                    selected = set()
                for device in step['ttlDevices']:
                    child = QtWidgets.QTreeWidgetItem([device, ''])
                    child.setData(0, QtCore.Qt.UserRole, ('device', step['id'], device))
                    child.setFlags(child.flags() | QtCore.Qt.ItemIsUserCheckable)
                    child.setCheckState(
                        0, QtCore.Qt.Checked if device in selected else QtCore.Qt.Unchecked
                    )
                    step_item.addChild(child)
                if not step['ttlDevices']:
                    step_item.setCheckState(0, QtCore.Qt.Unchecked)
                elif len(selected) == len(step['ttlDevices']):
                    step_item.setCheckState(0, QtCore.Qt.Checked)
                elif selected:
                    step_item.setCheckState(0, QtCore.Qt.PartiallyChecked)
                else:
                    step_item.setCheckState(0, QtCore.Qt.Unchecked)
                step_item.setExpanded(True)
        finally:
            self._updating = was_updating

    def _modeDetailsChanged(self, *args):
        if self._updating:
            return
        mode_id = self._currentModeId()
        if mode_id is None:
            return
        self._modes[mode_id]['label'] = self.modeLabelEdit.text().strip() or 'Mode'
        row = self.modesList.currentRow()
        self.modesList.item(row).setText(self._modes[mode_id]['label'])
        self._refreshDefaultModes()

    def _modeDevicesChanged(self, item=None, column=0):
        if self._updating:
            return
        mode_id = self._currentModeId()
        if mode_id is None:
            return
        if item is not None:
            data = item.data(0, QtCore.Qt.UserRole)
            if data and data[0] == 'step' and item.checkState(0) != QtCore.Qt.PartiallyChecked:
                state = item.checkState(0)
                self._updating = True
                try:
                    for child_index in range(item.childCount()):
                        item.child(child_index).setCheckState(0, state)
                finally:
                    self._updating = False
        enabled_steps = []
        enabled_devices = {}
        for index in range(self.modeDevicesTree.topLevelItemCount()):
            step_item = self.modeDevicesTree.topLevelItem(index)
            step_id = str(step_item.data(0, QtCore.Qt.UserRole)[1])
            selected = []
            all_devices = []
            for child_index in range(step_item.childCount()):
                child = step_item.child(child_index)
                device = str(child.data(0, QtCore.Qt.UserRole)[2])
                all_devices.append(device)
                if child.checkState(0) == QtCore.Qt.Checked:
                    selected.append(device)
            if not selected:
                continue
            enabled_steps.append(step_id)
            if selected != all_devices:
                enabled_devices[step_id] = selected
        self._modes[mode_id]['enabledSteps'] = enabled_steps
        self._modes[mode_id]['enabledDevices'] = enabled_devices

    def _addMode(self):
        mode_id = mode_id_from_label('mode', self._modes)
        self._modes[mode_id] = {
            'label': 'Mode',
            'enabledSteps': [step['id'] for step in self._steps],
            'enabledDevices': {},
        }
        if not self._default_mode:
            self._default_mode = mode_id
        self._refreshModesList(len(self._modes) - 1)

    def _removeMode(self):
        if len(self._modes) <= 1:
            return
        mode_id = self._currentModeId()
        if mode_id is None:
            return
        row = self.modesList.currentRow()
        del self._modes[mode_id]
        if self._default_mode == mode_id:
            self._default_mode = next(iter(self._modes))
        self._refreshModesList(max(0, row - 1))

    def profile(self):
        config = {
            'label': self.profileLabelEdit.text().strip(),
            'defaultMode': self._default_mode,
            'steps': [dict(step) for step in self._steps],
            'modes': {
                mode_id: {
                    'label': mode['label'],
                    'enabledSteps': list(mode['enabledSteps']),
                    **(
                        {'enabledDevices': {
                            step_id: list(devices)
                            for step_id, devices in mode.get('enabledDevices', {}).items()
                        }}
                        if mode.get('enabledDevices') else {}
                    ),
                }
                for mode_id, mode in self._modes.items()
            },
        }
        return MoNaLISAProfileDefinition.from_config(self._profile_id, config)

    def _acceptIfValid(self):
        try:
            profile = self.profile()
        except (MoNaLISAProfileError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, 'Invalid profile', str(error))
            return
        missing = sorted({
            device
            for step in profile.steps
            for device in step.ttl_devices
            if device not in self._available_devices
        })
        if missing:
            QtWidgets.QMessageBox.warning(
                self,
                'Unavailable TTL devices',
                'The profile still references devices not available in this setup:\n\n'
                + '\n'.join(missing),
            )
            return
        self.accept()


__all__ = ['MoNaLISAProfileEditor']
