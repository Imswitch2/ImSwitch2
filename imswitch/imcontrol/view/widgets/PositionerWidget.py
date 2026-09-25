from qtpy import QtCore, QtWidgets
from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget


class PositionerWidget(Widget):
    """Widget in control of manual positioner movement."""

    sigJoystickToggled = QtCore.Signal(bool, str)
    sigStepUpClicked = QtCore.Signal(str, str)
    sigStepDownClicked = QtCore.Signal(str, str)
    sigsetSpeedClicked = QtCore.Signal()
    sigSettingsClicked = QtCore.Signal()
    sigSettingsChanged = QtCore.Signal(object)
    sigStepModeChanged = QtCore.Signal(bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.numPositioners = 1
        self.pars = {}
        self._positionUnits = {}
        self._positionerAxes = {}
        self._coarseMode = False
        self._coarseStepMultiplier = 5.0

        self.gridContainer = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout()
        self.gridContainer.setLayout(self.grid)

        self._buildHeader()

        self.scrollArea = QtWidgets.QScrollArea()
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setMinimumSize(0, 0)
        self.scrollArea.setWidget(self.gridContainer)
        self.scrollArea.setWidgetResizable(True)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.scrollArea)
        self.setLayout(layout)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)

    def _buildHeader(self):
        self.pars['StepModeContainer'] = QtWidgets.QWidget()
        stepModeContainerLayout = QtWidgets.QHBoxLayout(self.pars['StepModeContainer'])
        stepModeContainerLayout.setContentsMargins(0, 0, 0, 0)
        stepModeContainerLayout.setSpacing(6)

        self.pars['StepModeWidget'] = QtWidgets.QWidget()
        stepModeLayout = QtWidgets.QHBoxLayout(self.pars['StepModeWidget'])
        stepModeLayout.setContentsMargins(0, 0, 0, 0)
        stepModeLayout.setSpacing(0)

        self.pars['CoarseModeButton'] = guitools.BetterPushButton('Coarse')
        self.pars['FineModeButton'] = guitools.BetterPushButton('Fine')
        self.pars['CoarseModeButton'].setObjectName('coarseModeBtn')
        self.pars['FineModeButton'].setObjectName('fineModeBtn')
        self.pars['CoarseModeButton'].setCheckable(True)
        self.pars['FineModeButton'].setCheckable(True)
        self.pars['FineModeButton'].setChecked(True)

        stepModeStyle = """
        QPushButton { padding: 2px 10px; border: 1px solid rgba(255,255,255,60); }
        QPushButton#fineModeBtn { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
        QPushButton#coarseModeBtn { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
        QPushButton:hover { border: 1px solid rgba(255,255,255,120); }
        QPushButton:checked {
            background-color: rgba(120,180,255,120);
            border: 1px solid rgba(120,180,255,200);
        }
        """
        self.pars['CoarseModeButton'].setStyleSheet(stepModeStyle)
        self.pars['FineModeButton'].setStyleSheet(stepModeStyle)
        self._updateCoarseModeTooltip()

        self._stepModeButtonGroup = QtWidgets.QButtonGroup(self)
        self._stepModeButtonGroup.setExclusive(True)
        self._stepModeButtonGroup.addButton(self.pars['CoarseModeButton'])
        self._stepModeButtonGroup.addButton(self.pars['FineModeButton'])
        stepModeLayout.addWidget(self.pars['FineModeButton'], 1)
        stepModeLayout.addWidget(self.pars['CoarseModeButton'], 1)
        self.pars['CoarseModeButton'].clicked.connect(lambda: self.sigStepModeChanged.emit(True))
        self.pars['FineModeButton'].clicked.connect(lambda: self.sigStepModeChanged.emit(False))
        stepModeContainerLayout.addStretch(1)
        stepModeContainerLayout.addWidget(self.pars['StepModeWidget'], 1)
        self.grid.addWidget(self.pars['StepModeContainer'], 0, 5)

        self.pars['SettingsButton'] = guitools.BetterPushButton('Settings')
        self.grid.addWidget(self.pars['SettingsButton'], 0, 6, alignment=QtCore.Qt.AlignRight)
        self.pars['SettingsButton'].clicked.connect(self.sigSettingsClicked.emit)

    def addJoystick(self, pName):
        self.joystickCheck = QtWidgets.QCheckBox('Enable Joystick')
        self.joystickCheck.setCheckable(True)
        self.grid.addWidget(self.joystickCheck, 0, 0)
        self.joystickCheck.clicked.connect(lambda state: self.sigJoystickToggled.emit(state, pName))

    def addPositioner(self, positionerName, axes, speed, joystick, shortcutModifier=None, unit='µm'):
        self._positionerAxes[positionerName] = list(axes)
        for axis in axes:
            parNameSuffix = self._getParNameSuffix(positionerName, axis)
            label = f'{positionerName} -- {axis}' if positionerName != axis else positionerName
            self._positionUnits[parNameSuffix] = unit

            self.pars['Label' + parNameSuffix] = QtWidgets.QLabel(f'<strong>{label}</strong>')
            self.pars['Position' + parNameSuffix] = QtWidgets.QLabel(f'<strong>{0:.2f} {unit}</strong>')
            self.pars['UpButton' + parNameSuffix] = guitools.BetterPushButton('+')
            self.pars['DownButton' + parNameSuffix] = guitools.BetterPushButton('-')
            self.pars['FineStepLabel' + parNameSuffix] = QtWidgets.QLabel('Fine Step')
            self.pars['StepEdit' + parNameSuffix] = QtWidgets.QLineEdit('25' if positionerName == 'Stage' else '0.05')
            self.pars['StepValuesWidget' + parNameSuffix] = QtWidgets.QWidget()
            stepValuesLayout = QtWidgets.QHBoxLayout(self.pars['StepValuesWidget' + parNameSuffix])
            stepValuesLayout.setContentsMargins(0, 0, 0, 0)
            stepValuesLayout.setSpacing(6)
            self.pars['CoarseStepPreview' + parNameSuffix] = QtWidgets.QLabel()
            self.pars['StepUnit' + parNameSuffix] = QtWidgets.QLabel(f' {unit}')
            stepValuesLayout.addWidget(self.pars['StepEdit' + parNameSuffix], 1)
            stepValuesLayout.addWidget(self.pars['CoarseStepPreview' + parNameSuffix], 1)

            self.grid.addWidget(self.pars['Label' + parNameSuffix], self.numPositioners, 0)
            self.grid.addWidget(self.pars['Position' + parNameSuffix], self.numPositioners, 1)
            self.grid.addWidget(self.pars['UpButton' + parNameSuffix], self.numPositioners, 2)
            self.grid.addWidget(self.pars['DownButton' + parNameSuffix], self.numPositioners, 3)
            self.grid.addWidget(self.pars['FineStepLabel' + parNameSuffix], self.numPositioners, 4)
            self.grid.addWidget(self.pars['StepValuesWidget' + parNameSuffix], self.numPositioners, 5)
            self.grid.addWidget(self.pars['StepUnit' + parNameSuffix], self.numPositioners, 6)

            self.pars['UpButton' + parNameSuffix].clicked.connect(
                lambda *args, axis=axis: self.sigStepUpClicked.emit(positionerName, axis)
            )
            self.pars['DownButton' + parNameSuffix].clicked.connect(
                lambda *args, axis=axis: self.sigStepDownClicked.emit(positionerName, axis)
            )
            self.pars['StepEdit' + parNameSuffix].textChanged.connect(
                lambda *args, positionerName=positionerName, axis=axis:
                self._updateCoarseStepPreview(positionerName, axis)
            )
            self._updateCoarseStepPreview(positionerName, axis)

            if speed:
                self.pars['Speed'] = QtWidgets.QLabel(f'<strong>{0:.2f} {unit}/s</strong>')
                self.pars['ButtonSpeedEnter'] = guitools.BetterPushButton('Enter')
                self.pars['SpeedEdit'] = QtWidgets.QLineEdit('1000')
                self.pars['SpeedUnit'] = QtWidgets.QLabel(f' {unit}/s')
                self.grid.addWidget(self.pars['Speed'], self.numPositioners, 7)
                self.grid.addWidget(self.pars['SpeedEdit'], self.numPositioners, 10)
                self.grid.addWidget(self.pars['SpeedUnit'], self.numPositioners, 11)
                self.grid.addWidget(self.pars['ButtonSpeedEnter'], self.numPositioners, 12)
                self.pars['ButtonSpeedEnter'].clicked.connect(lambda *args: self.sigsetSpeedClicked.emit())

            self.numPositioners += 1
        self._refreshStepModeStyles()

    def getStepSize(self, positionerName, axis):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        return float(self.pars['StepEdit' + parNameSuffix].text())

    def setStepSize(self, positionerName, axis, stepSize):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        self.pars['StepEdit' + parNameSuffix].setText(str(stepSize))
        self._updateCoarseStepPreview(positionerName, axis)

    def getSpeed(self):
        return float(self.pars['SpeedEdit'].text())

    def setSpeedSize(self, positionerName, axis, speedSize):
        self.pars['SpeedEdit'].setText(str(speedSize))

    def updatePosition(self, positionerName, axis, position):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        unit = self._positionUnits.get(parNameSuffix, 'µm')
        self.pars['Position' + parNameSuffix].setText(f'<strong>{position:.2f} {unit}</strong>')

    def setStepMode(self, coarseMode):
        self._coarseMode = bool(coarseMode)
        self.pars['CoarseModeButton'].setChecked(self._coarseMode)
        self.pars['FineModeButton'].setChecked(not self._coarseMode)
        self._refreshStepModeStyles()

    def setCoarseStepMultiplier(self, multiplier):
        self._coarseStepMultiplier = float(multiplier)
        self._updateCoarseModeTooltip()
        for positionerName, axes in self._positionerAxes.items():
            for axis in axes:
                self._updateCoarseStepPreview(positionerName, axis)
        self._refreshStepModeStyles()

    def showSettingsDialog(self, liveUpdateIntervalMs, positionerSettings, joystickSettings):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Positioner Settings')
        layout = QtWidgets.QVBoxLayout(dialog)

        form = QtWidgets.QFormLayout()
        intervalSpinBox = QtWidgets.QSpinBox()
        intervalSpinBox.setRange(100, 2000)
        intervalSpinBox.setSingleStep(50)
        intervalSpinBox.setSuffix(' ms')
        intervalSpinBox.setValue(int(liveUpdateIntervalMs))
        form.addRow('Live update interval', intervalSpinBox)

        coarseMultiplierSpinBox = QtWidgets.QDoubleSpinBox()
        coarseMultiplierSpinBox.setRange(1.0, 1000.0)
        coarseMultiplierSpinBox.setDecimals(2)
        coarseMultiplierSpinBox.setSingleStep(0.5)
        coarseMultiplierSpinBox.setValue(float(joystickSettings.get('coarseStepMultiplier', 5.0)))
        form.addRow('Coarse multiplier', coarseMultiplierSpinBox)
        layout.addLayout(form)

        self._addSettingsSectionTitle(layout, 'Live Update')
        liveUpdateChecks = {}
        for positionerName, settings in positionerSettings.items():
            check = QtWidgets.QCheckBox(positionerName)
            available = bool(settings.get('liveUpdateAvailable', False))
            check.setEnabled(available)
            check.setChecked(available and bool(settings.get('liveUpdateEnabled', False)))
            liveUpdateChecks[positionerName] = check
            layout.addWidget(check)

        self._addSettingsSectionTitle(layout, 'Joystick')
        joystickAvailable = bool(joystickSettings.get('joystickAvailable', False))
        joystickAutoReenableCheck = QtWidgets.QCheckBox('Automatically re-enable joystick after software moves')
        joystickAutoReenableCheck.setEnabled(joystickAvailable)
        joystickAutoReenableCheck.setChecked(bool(joystickSettings.get('joystickAutoReenable', True)))
        layout.addWidget(joystickAutoReenableCheck)

        delayForm = QtWidgets.QFormLayout()
        delaySpinBox = QtWidgets.QDoubleSpinBox()
        delaySpinBox.setRange(0.1, 30.0)
        delaySpinBox.setDecimals(1)
        delaySpinBox.setSingleStep(0.5)
        delaySpinBox.setSuffix(' s')
        delaySpinBox.setValue(float(joystickSettings.get('joystickAutoReenableDelayS', 5.0)))
        delaySpinBox.setEnabled(joystickAvailable and joystickAutoReenableCheck.isChecked())
        joystickAutoReenableCheck.toggled.connect(
            lambda checked: delaySpinBox.setEnabled(joystickAvailable and checked)
        )
        delayForm.addRow('Re-enable delay', delaySpinBox)
        layout.addLayout(delayForm)

        shortcutNote = QtWidgets.QLabel('Keyboard shortcuts are configured globally from the Shortcuts editor.')
        shortcutNote.setWordWrap(True)
        layout.addWidget(shortcutNote)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.sigSettingsChanged.emit({
                'liveUpdateIntervalMs': intervalSpinBox.value(),
                'liveUpdateEnabled': {
                    name: check.isChecked() for name, check in liveUpdateChecks.items()
                },
                'coarseStepMultiplier': coarseMultiplierSpinBox.value(),
                'joystickAutoReenable': joystickAutoReenableCheck.isChecked(),
                'joystickAutoReenableDelayS': delaySpinBox.value(),
            })

    def _addSettingsSectionTitle(self, layout, title):
        label = QtWidgets.QLabel(title)
        label.setStyleSheet('font-weight: bold; margin-top: 8px;')
        layout.addWidget(label)

    def _updateCoarseStepPreview(self, positionerName, axis):
        parNameSuffix = self._getParNameSuffix(positionerName, axis)
        preview = self.pars.get('CoarseStepPreview' + parNameSuffix)
        if preview is None:
            return
        try:
            fineStep = float(self.pars['StepEdit' + parNameSuffix].text())
            preview.setText(f'Coarse: {self._formatStepValue(fineStep * self._coarseStepMultiplier)}')
        except ValueError:
            preview.setText('Coarse: -')

    def _refreshStepModeStyles(self):
        fineStyle = 'color: gray;' if self._coarseMode else ''
        coarseStyle = 'font-weight: bold;' if self._coarseMode else 'color: gray;'
        for positionerName, axes in self._positionerAxes.items():
            for axis in axes:
                suffix = self._getParNameSuffix(positionerName, axis)
                self.pars['FineStepLabel' + suffix].setStyleSheet(fineStyle)
                self.pars['StepEdit' + suffix].setStyleSheet(fineStyle)
                self.pars['CoarseStepPreview' + suffix].setStyleSheet(coarseStyle)

    def _formatStepValue(self, value):
        return f'{value:.6g}'

    def _updateCoarseModeTooltip(self):
        if 'CoarseModeButton' in self.pars:
            self.pars['CoarseModeButton'].setToolTip(f'{self._formatStepValue(self._coarseStepMultiplier)}x')

    def stepAxis(self, positionerName, axis, direction):
        if direction == 'plus':
            self.sigStepUpClicked.emit(positionerName, axis)
        elif direction == 'minus':
            self.sigStepDownClicked.emit(positionerName, axis)

    def _getParNameSuffix(self, positionerName, axis):
        return f'{positionerName}--{axis}'


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
# ImSwitch is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
