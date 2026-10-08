from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import colorutils
from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class LaserWidget(Widget):
    """Laser widget with contextual Manual and Scan power views."""

    sigEnableChanged = QtCore.Signal(str, bool)
    sigValueChanged = QtCore.Signal(str, float)

    sigModEnabledChanged = QtCore.Signal(str, bool)
    sigFreqChanged = QtCore.Signal(str, int)
    sigDutyCycleChanged = QtCore.Signal(str, int)

    sigPowerContextChanged = QtCore.Signal(str)
    # Selecting a combo entry is an immediate load request for the current
    # context. Programmatic combo changes are deliberately silent.
    sigPresetSelected = QtCore.Signal(str)
    sigUpdatePresetClicked = QtCore.Signal()
    sigSavePresetAsClicked = QtCore.Signal()
    sigSavePresetToClicked = QtCore.Signal(str)
    sigRenamePresetClicked = QtCore.Signal()
    sigDeletePresetClicked = QtCore.Signal()
    sigClearPresetClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.laserModules = {}
        self._powerContext = 'manual'

        self.setMinimumSize(0, 0)
        self.layout = QtWidgets.QGridLayout()
        self.setLayout(self.layout)

        # Laser rows remain scrollable; the context/preset bar stays fixed at the top.
        self.lasersGrid = QtWidgets.QGridLayout()
        self.lasersGrid.setContentsMargins(4, 4, 4, 4)
        self.lasersGridContainer = QtWidgets.QWidget()
        self.lasersGridContainer.setLayout(self.lasersGrid)

        self.scrollArea = QtWidgets.QScrollArea()
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.scrollArea.setMinimumSize(0, 0)
        self.scrollArea.setWidget(self.lasersGridContainer)
        self.scrollArea.setWidgetResizable(True)
        self.lasersGridContainer.installEventFilter(self)

        self.topBar = QtWidgets.QHBoxLayout()

        self.contextFrame = QtWidgets.QWidget()
        contextLayout = QtWidgets.QHBoxLayout(self.contextFrame)
        contextLayout.setContentsMargins(0, 0, 0, 0)
        contextLayout.setSpacing(0)
        self.manualModeButton = QtWidgets.QPushButton('MANUAL')
        self.scanModeButton = QtWidgets.QPushButton('SCAN')
        for button in (self.manualModeButton, self.scanModeButton):
            button.setCheckable(True)
            button.setMinimumHeight(38)
            button.setMinimumWidth(115)
            font = button.font()
            font.setBold(True)
            if font.pointSizeF() > 0:
                font.setPointSizeF(font.pointSizeF() + 1)
            button.setFont(font)
        self.contextButtonGroup = QtWidgets.QButtonGroup(self)
        self.contextButtonGroup.setExclusive(True)
        self.contextButtonGroup.addButton(self.manualModeButton)
        self.contextButtonGroup.addButton(self.scanModeButton)
        contextLayout.addWidget(self.manualModeButton)
        contextLayout.addWidget(self.scanModeButton)
        self.manualModeButton.clicked.connect(
            lambda checked: checked and self._contextClicked('manual')
        )
        self.scanModeButton.clicked.connect(
            lambda checked: checked and self._contextClicked('scan')
        )

        self.scanEditingLabel = QtWidgets.QLabel('EDITING SCAN POWERS')
        self.scanEditingLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.scanEditingLabel.setStyleSheet(
            'font-weight: bold; padding: 5px 12px; '
            'border: 1px solid #a8832f; border-radius: 4px; '
            'background-color: #d6b15d; color: #2f2818;'
        )
        self.scanEditingLabel.setToolTip(
            'Scan power values are being edited. Changes are stored for acquisition '
            'and do not directly control Manual illumination.'
        )
        self.scanEditingLabel.hide()

        self.presetsList = QtWidgets.QComboBox()
        self.presetsList.setMinimumWidth(150)
        # ``activated`` is user-only: rebuilding the combo or switching context
        # never accidentally loads a preset.
        self.presetsList.activated.connect(self._presetActivated)

        self.moreButton = QtWidgets.QToolButton()
        self.moreButton.setText('More…')
        self.moreButton.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.moreMenu = QtWidgets.QMenu(self.moreButton)
        self.moreButton.setMenu(self.moreMenu)

        self.updatePresetAction = self.moreMenu.addAction('Update selected')
        self.updatePresetAction.triggered.connect(self.sigUpdatePresetClicked)
        self.savePresetAsAction = self.moreMenu.addAction('Save as…')
        self.savePresetAsAction.triggered.connect(self.sigSavePresetAsClicked)
        self.savePresetToMenu = self.moreMenu.addMenu('Save current values to…')
        self.moreMenu.addSeparator()
        self.renamePresetAction = self.moreMenu.addAction('Rename…')
        self.renamePresetAction.triggered.connect(self.sigRenamePresetClicked)
        self.deletePresetAction = self.moreMenu.addAction('Delete…')
        self.deletePresetAction.triggered.connect(self.sigDeletePresetClicked)
        self.clearPresetAction = self.moreMenu.addAction('Clear selection')
        self.clearPresetAction.triggered.connect(self.sigClearPresetClicked)

        self.topBar.addWidget(self.contextFrame, 0)
        self.topBar.addStretch(1)
        self.topBar.addWidget(self.scanEditingLabel, 0, QtCore.Qt.AlignCenter)
        self.topBar.addStretch(1)
        self.topBar.addWidget(self.presetsList, 0)
        self.topBar.addWidget(self.moreButton, 0)
        self.layout.addLayout(self.topBar, 0, 0)
        self.layout.addWidget(self.scrollArea, 1, 0)

        self.setPowerContext('manual', emitSignal=False)
        self.setPresetNames([], selected=None)

    def _contextClicked(self, context):
        if context == self._powerContext:
            return
        self._powerContext = context
        for module in self.laserModules.values():
            module.setContext(context)
        self._updateContextAppearance()
        self.sigPowerContextChanged.emit(context)

    def _updateContextAppearance(self):
        """Show the explicit Scan-editing banner for the active context."""
        self.scanEditingLabel.setVisible(self._powerContext == 'scan')

    def _presetActivated(self, index):
        # Qt bindings normally emit the combo index here, but accepting the
        # text overload as well keeps the widget robust across bindings.
        if isinstance(index, int):
            name = self.presetsList.itemData(index)
        else:
            text = str(index)
            found = self.presetsList.findData(text)
            if found < 0:
                found = self.presetsList.findText(text)
            name = self.presetsList.itemData(found) if found >= 0 else None
        if name:
            self.sigPresetSelected.emit(str(name))

    def addLaser(self, laserName, valueUnits, valueDecimals, wavelength, valueRange=None,
                 valueRangeStep=1, frequencyRange=(0, 0, 0)):
        control = LaserModule(
            valueUnits=valueUnits, valueDecimals=valueDecimals, valueRange=valueRange,
            tickInterval=5, singleStep=valueRangeStep,
            initialPower=valueRange[0] if valueRange is not None else 0,
            frequencyRange=frequencyRange
        )
        control.sigEnableChanged.connect(
            lambda enabled: self.sigEnableChanged.emit(laserName, enabled)
        )
        control.sigValueChanged.connect(
            lambda value: self.sigValueChanged.emit(laserName, value)
        )

        if all(num > 0 for num in frequencyRange):
            control.sigModEnabledChanged.connect(
                lambda enabled: self.sigModEnabledChanged.emit(laserName, enabled)
            )
            control.sigFreqChanged.connect(
                lambda frequency: self.sigFreqChanged.emit(laserName, frequency)
            )
            control.sigDutyCycleChanged.connect(
                lambda dutyCycle: self.sigDutyCycleChanged.emit(laserName, dutyCycle)
            )

        nameLabel = QtWidgets.QLabel(laserName)
        color = colorutils.wavelengthToHex(wavelength)
        nameLabel.setStyleSheet(
            f'font-size: 16px; font-weight: bold; padding: 0 6px 0 12px;'
            f'border-left: 4px solid {color}'
        )

        self.lasersGrid.addWidget(nameLabel, len(self.laserModules), 0)
        self.lasersGrid.addWidget(control, len(self.laserModules), 1)
        self.laserModules[laserName] = control
        control.setContext(self._powerContext)

    def getPowerContext(self):
        return self._powerContext

    def setPowerContext(self, context, emitSignal=False):
        context = str(getattr(context, 'value', context)).lower()
        if context not in ('manual', 'scan'):
            raise ValueError(f'Unknown laser power context: {context!r}')
        changed = context != self._powerContext
        self._powerContext = context
        self.manualModeButton.setChecked(context == 'manual')
        self.scanModeButton.setChecked(context == 'scan')
        for module in self.laserModules.values():
            module.setContext(context)
        self._updateContextAppearance()
        if emitSignal and changed:
            self.sigPowerContextChanged.emit(context)

    def isLaserActive(self, laserName):
        return self.laserModules[laserName].isActive()

    def getValue(self, laserName):
        """Return the value currently displayed in the primary power editor."""
        return self.laserModules[laserName].getValue()

    def setEditable(self, editable):
        self.setEnabled(editable)

    def setLaserActive(self, laserName, active, emitSignal=True):
        self.laserModules[laserName].setActive(active, emitSignal=emitSignal)

    def setLaserActivatable(self, laserName, activatable):
        self.laserModules[laserName].setActivatable(activatable)

    def setLaserEditable(self, laserName, editable):
        self.laserModules[laserName].setEditable(editable)

    def setLaserPowerEditable(self, laserName, editable):
        self.laserModules[laserName].setPowerEditable(editable)

    def setLaserEnableEditable(self, laserName, editable):
        self.laserModules[laserName].setEnableEditable(editable)

    def setValue(self, laserName, value, emitSignal=True):
        self.laserModules[laserName].setValue(value, emitSignal=emitSignal)

    def setLaserAOOwned(self, laserName, aoOwned, tooltip=''):
        self.laserModules[laserName].setAOOwned(aoOwned, tooltip=tooltip)

    def setLaserScanActive(self, laserName, active):
        self.laserModules[laserName].setScanActive(bool(active))

    def setLaserSecondaryStatus(
        self, laserName, text, *, emphasized=False, muted=False, tooltip='', badge=False
    ):
        self.laserModules[laserName].setSecondaryStatus(
            text, emphasized=emphasized, muted=muted, tooltip=tooltip, badge=badge
        )

    def setModulationFrequency(self, laserName, value):
        self.laserModules[laserName].setModulationFrequency(value)

    def setModulationDutyCycle(self, laserName, value):
        self.laserModules[laserName].setModulationDutyCycle(value)

    def getCurrentPreset(self):
        return self.presetsList.currentData()

    def setCurrentPreset(self, name, emitSignal=False):
        index = self.presetsList.findData(name) if name else -1
        self.presetsList.setCurrentIndex(index)
        selected = self.getCurrentPreset()
        hasSelection = bool(selected)
        self.updatePresetAction.setEnabled(hasSelection)
        self.renamePresetAction.setEnabled(hasSelection)
        self.deletePresetAction.setEnabled(hasSelection)
        self.clearPresetAction.setEnabled(hasSelection)
        self.updatePresetAction.setText(
            f'Update "{selected}"' if selected else 'Update selected'
        )
        if emitSignal and selected:
            self.sigPresetSelected.emit(str(selected))

    def setPresetNames(self, names, selected=None):
        names = sorted({str(name) for name in names})
        self.presetsList.blockSignals(True)
        try:
            self.presetsList.clear()
            for name in names:
                self.presetsList.addItem(name, name)
            self.setCurrentPreset(selected, emitSignal=False)
        finally:
            self.presetsList.blockSignals(False)

        self.savePresetToMenu.clear()
        for name in names:
            action = self.savePresetToMenu.addAction(name)
            action.triggered.connect(
                lambda _checked=False, target=name: self.sigSavePresetToClicked.emit(target)
            )
        self.savePresetToMenu.setEnabled(bool(names))

    # Compatibility helpers retained for controller/tests that update one name.
    def addPreset(self, name):
        names = [self.presetsList.itemData(i) for i in range(self.presetsList.count())]
        selected = self.getCurrentPreset()
        if name not in names:
            names.append(name)
        self.setPresetNames(names, selected=selected)

    def removePreset(self, name):
        names = [
            self.presetsList.itemData(i) for i in range(self.presetsList.count())
            if self.presetsList.itemData(i) != name
        ]
        selected = self.getCurrentPreset()
        self.setPresetNames(names, selected=None if selected == name else selected)

    def eventFilter(self, source, event):
        if source is self.lasersGridContainer and event.type() == QtCore.QEvent.Resize:
            self.scrollArea.updateGeometry()
        return False


class LaserModule(QtWidgets.QWidget):
    """ Module from LaserWidget to handle a single laser. """

    sigEnableChanged = QtCore.Signal(bool)  # (enabled)
    sigValueChanged = QtCore.Signal(float)  # (value)

    sigModEnabledChanged = QtCore.Signal(bool) # (modulation enabled)
    sigFreqChanged = QtCore.Signal(int)        # (frequency)
    sigDutyCycleChanged = QtCore.Signal(int)   # (duty cycle)

    def __init__(self, valueUnits, valueDecimals, valueRange, tickInterval, singleStep,
                 initialPower, frequencyRange, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.valueDecimals = valueDecimals
        self.valueUnits = valueUnits
        self.isBinary = valueRange is None
        self.isModulated = all(num > 0 for num in frequencyRange)
        self._context = 'manual'

        isBinary = self.isBinary
        isModulated = self.isModulated

        # Graphical elements
        self.setPointLabel = QtWidgets.QLabel(f'Setpoint [{valueUnits}]')
        self.setPointLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.setPointEdit = QtWidgets.QLineEdit(str(initialPower))
        self.setPointEdit.setFixedWidth(50)
        self.setPointEdit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.minpower = QtWidgets.QLabel()
        self.minpower.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.maxpower = QtWidgets.QLabel()
        self.maxpower.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

        self.slider = guitools.FloatSlider(QtCore.Qt.Horizontal, self, allowScrollChanges=False,
                                           decimals=valueDecimals)
        self.slider.setFocusPolicy(QtCore.Qt.NoFocus)

        if not isBinary:
            valueRangeMin, valueRangeMax = valueRange

            self.minpower.setText(str(valueRangeMin))
            self.maxpower.setText(str(valueRangeMax))

            self.slider.setMinimum(valueRangeMin)
            self.slider.setMaximum(valueRangeMax)
            self.slider.setTickInterval(tickInterval)
            self.slider.setSingleStep(singleStep)
            self.slider.setValue(0)

        self.powerFrame = QtWidgets.QFrame(self)
        self.powerGrid = QtWidgets.QGridLayout()
        self.powerFrame.setFrameStyle(QtWidgets.QFrame.Panel | QtWidgets.QFrame.Plain)
        self.powerFrame.setLayout(self.powerGrid)
        powerFrame = self.powerFrame

        self.powerGrid.addWidget(self.setPointLabel, 0, 0, 2, 1)
        self.powerGrid.addWidget(self.setPointEdit, 0, 1, 2, 1)
        self.powerGrid.addWidget(self.minpower, 0, 2, 2, 1)
        self.powerGrid.addWidget(self.slider, 0, 3, 2, 1)
        self.powerGrid.addWidget(self.maxpower, 0, 4, 2, 1)
        self._powerWidgets = [
            self.setPointLabel, self.setPointEdit, self.minpower, self.slider, self.maxpower
        ]
        self.aoIndicator = QtWidgets.QLabel('AO')
        self.aoIndicator.setAlignment(QtCore.Qt.AlignCenter)
        self.aoIndicator.setStyleSheet('font-weight: bold; padding: 6px;')
        self.aoIndicator.hide()
        self.powerGrid.addWidget(self.aoIndicator, 0, 0, 2, 5)

        self._powerOpacity = QtWidgets.QGraphicsOpacityEffect(self.powerFrame)
        self.powerFrame.setGraphicsEffect(self._powerOpacity)
        self._powerOpacity.setOpacity(1.0)

        self.modulationGroup = None
        if isModulated:
            freqRangeMin, freqRangeMax, initialFrequency = frequencyRange
            # laser modulation widgets
            # enable button
            self.modulationEnable = guitools.BetterPushButton("ON")
            self.modulationEnable.setSizePolicy(QtWidgets.QSizePolicy.Minimum,
                                            QtWidgets.QSizePolicy.Expanding)
            self.modulationEnable.setCheckable(True)

            # frequency slider
            self.modulationFrequencyLabel = QtWidgets.QLabel("Frequency [Hz]")
            self.modulationFrequencyLabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.modulationFrequencyEdit = QtWidgets.QLineEdit(str(initialFrequency))
            self.modulationFrequencyEdit.setFixedWidth(50)
            self.modulationFrequencyEdit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.modulationFrequencyMinLabel = QtWidgets.QLabel(str(freqRangeMin))
            self.modulationFrequencyMaxLabel = QtWidgets.QLabel(str(freqRangeMax))
            self.modulationFrequencySlider = guitools.BetterSlider(QtCore.Qt.Horizontal)
            self.modulationFrequencySlider.setRange(freqRangeMin, freqRangeMax)
            self.modulationFrequencySlider.setValue(initialFrequency)

            # duty cycle slider
            self.modulationDutyCycleLabel = QtWidgets.QLabel("Duty cycle [%]")
            self.modulationDutyCycleLabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.modulationDutyCycleEdit = QtWidgets.QLineEdit(str(50))
            self.modulationDutyCycleEdit.setFixedWidth(50)
            self.modulationDutyCycleEdit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.modulationDutyCycleMinLabel = QtWidgets.QLabel(str(1))
            self.modulationDutyCycleMaxLabel = QtWidgets.QLabel(str(99))
            self.modulationDutyCycleMinLabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.modulationDutyCycleMaxLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            self.modulationDutyCycleSlider = guitools.BetterSlider(QtCore.Qt.Horizontal)
            self.modulationDutyCycleSlider.setRange(1, 99)
            self.modulationDutyCycleSlider.setValue(50)

            self.modulationGroup = QtWidgets.QGroupBox("Frequency modulation")
            self.modulationLayout = QtWidgets.QGridLayout()

            self.modulationLayout.addWidget(self.modulationFrequencyLabel, 0, 0)
            self.modulationLayout.addWidget(self.modulationFrequencyEdit, 0, 1)
            self.modulationLayout.addWidget(self.modulationFrequencyMinLabel, 0, 2)
            self.modulationLayout.addWidget(self.modulationFrequencySlider, 0, 3)
            self.modulationLayout.addWidget(self.modulationFrequencyMaxLabel, 0, 4)

            self.modulationLayout.addWidget(self.modulationDutyCycleLabel, 1, 0)
            self.modulationLayout.addWidget(self.modulationDutyCycleEdit, 1, 1)
            self.modulationLayout.addWidget(self.modulationDutyCycleMinLabel, 1, 2)
            self.modulationLayout.addWidget(self.modulationDutyCycleSlider, 1, 3)
            self.modulationLayout.addWidget(self.modulationDutyCycleMaxLabel, 1, 4)
            self.modulationLayout.addWidget(self.modulationEnable, 0, 5, 2, 1)
            self.modulationGroup.setLayout(self.modulationLayout)

            self.powerGrid.addWidget(self.modulationGroup, 2, 0, 1, 5)
                
        self.enableButton = guitools.BetterPushButton('ON')
        self.enableButton.setSizePolicy(QtWidgets.QSizePolicy.Minimum,
                                        QtWidgets.QSizePolicy.Expanding)
        self.enableButton.setCheckable(True)

        # Add elements to QHBoxLayout
        self.layout = QtWidgets.QHBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.layout)
        self.layout.addWidget(powerFrame)

        self.secondaryFrame = QtWidgets.QWidget()
        # Alternate-context text used to live permanently beside every row.
        # Keep the internal status widgets for compatibility/future compact
        # indicators, but hide the container so it consumes no row width.
        self.secondaryFrame.hide()
        self.secondaryFrame.setMinimumWidth(0)
        secondaryLayout = QtWidgets.QHBoxLayout(self.secondaryFrame)
        secondaryLayout.setContentsMargins(0, 0, 0, 0)
        secondaryLayout.setSpacing(4)

        self.secondaryLabel = QtWidgets.QLabel()
        self.secondaryLabel.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.secondaryLabel.setWordWrap(False)
        secondaryLayout.addWidget(self.secondaryLabel)

        self.secondaryAOIndicator = QtWidgets.QLabel('AO')
        self.secondaryAOIndicator.setAlignment(QtCore.Qt.AlignCenter)
        self.secondaryAOIndicator.setStyleSheet('font-weight: bold; padding: 2px 6px;')
        self.secondaryAOIndicator.setFrameStyle(QtWidgets.QFrame.Box | QtWidgets.QFrame.Plain)
        self.secondaryAOIndicator.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed
        )
        self.secondaryAOIndicator.hide()
        secondaryLayout.addWidget(self.secondaryAOIndicator)
        secondaryLayout.addStretch(1)
        # Intentionally not added to the row layout: alternate-context status is hidden.

        if isBinary:
            sizePolicy = powerFrame.sizePolicy()
            sizePolicy.setRetainSizeWhenHidden(True)
            powerFrame.setSizePolicy(sizePolicy)
            powerFrame.hide()
        self.layout.addWidget(self.enableButton)

        # Connect signals
        self.enableButton.toggled.connect(self.sigEnableChanged)
        self.slider.valueChanged.connect(
            lambda value: self.sigValueChanged.emit(value)
        )
        self.setPointEdit.returnPressed.connect(
            lambda: self.sigValueChanged.emit(self.getValue())
        )

        if isModulated:
            self.modulationEnable.toggled.connect(self.sigModEnabledChanged)
            self.modulationFrequencySlider.valueChanged.connect(
                lambda value: self.sigFreqChanged.emit(value)
            )
            self.modulationFrequencyEdit.returnPressed.connect(
                lambda: self.sigFreqChanged.emit(self.getFrequency())
            )
            self.modulationDutyCycleSlider.valueChanged.connect(
                lambda value: self.sigDutyCycleChanged.emit(value)
            )
            self.modulationDutyCycleEdit.returnPressed.connect(
                lambda: self.sigDutyCycleChanged.emit(self.getDutyCycle())
            )

    def isActive(self):
        """ Returns whether the laser is powered on. """
        return self.enableButton.isChecked()

    def getValue(self):
        """ Returns the value of the laser, in the units that the laser
        uses. """
        return float(self.setPointEdit.text())
    
    def getFrequency(self):
        """ Returns the selected frequency of the laser.
        """
        return int(self.modulationFrequencyEdit.text())
    
    def getDutyCycle(self):
        """ Returns the selected duty cycle of the laser.
        """
        return int(self.modulationDutyCycleEdit.text())

    def setActive(self, active, emitSignal=True):
        """ Sets whether the laser is powered on.

        If emitSignal is False, the button is updated without emitting the
        toggled/sigEnableChanged signal, so no laser-toggle handler runs. """
        if emitSignal:
            self.enableButton.setChecked(active)
        else:
            self.enableButton.blockSignals(True)
            try:
                self.enableButton.setChecked(active)
            finally:
                self.enableButton.blockSignals(False)

    def setActivatable(self, activatable):
        """ Sets whether the laser can be (de)activated by the user. """
        self.enableButton.setEnabled(activatable)

    def setEditable(self, editable):
        """ Sets whether the laser's values can be edited by the user. """
        self.setPointEdit.setEnabled(editable)
        self.slider.setEnabled(editable)
        self.enableButton.setEnabled(editable)

    def setEnableEditable(self, editable):
        """ Sets whether the on/off button can be used, leaving the power
        setpoint editable.

        A scan owns emission but not power: the scan's TTL gates the laser, so
        the on/off button would fight it, while the setpoint stays useful for
        tuning mid-scan. """
        self.enableButton.setEnabled(editable)

    def setValue(self, value, emitSignal=True):
        """ Sets the value of the laser, in the units that the laser uses. """
        if emitSignal:
            self.setPointEdit.setText(f'%.{self.valueDecimals}f' % value)
            self.slider.setValue(value)
            return

        self.slider.blockSignals(True)
        try:
            self.setPointEdit.setText(f'%.{self.valueDecimals}f' % value)
            self.slider.setValue(value)
        finally:
            self.slider.blockSignals(False)

    def setContext(self, context):
        self._context = str(getattr(context, 'value', context)).lower()
        manual = self._context == 'manual'
        self.enableButton.setVisible(manual)
        if self.modulationGroup is not None:
            self.modulationGroup.setVisible(manual)
        if not self.isBinary:
            self.powerFrame.setVisible(True)
        self.setAOOwned(False)

    def setPowerEditable(self, editable):
        self.setPointEdit.setEnabled(bool(editable))
        self.slider.setEnabled(bool(editable))

    def setAOOwned(self, aoOwned, tooltip=''):
        aoOwned = bool(aoOwned and self._context == 'scan' and not self.isBinary)
        for widget in self._powerWidgets:
            widget.setVisible(not aoOwned)
        self.aoIndicator.setVisible(aoOwned)
        self.aoIndicator.setToolTip(tooltip if aoOwned else '')
        self.powerFrame.setToolTip(tooltip if aoOwned else '')
        if aoOwned:
            self.setPowerEditable(False)

    def setScanActive(self, active):
        # Muting is visual only; inactive direct Scan powers remain editable.
        self._powerOpacity.setOpacity(1.0 if active or self._context == 'manual' else 0.55)

    def setSecondaryStatus(
        self, text, *, emphasized=False, muted=False, tooltip='', badge=False
    ):
        tooltip = str(tooltip or '')
        if badge:
            self.secondaryLabel.hide()
            self.secondaryAOIndicator.setText(str(text or 'AO'))
            style = ['font-weight: bold', 'padding: 2px 6px']
            if muted:
                style.append('color: palette(mid)')
            self.secondaryAOIndicator.setStyleSheet('; '.join(style))
            self.secondaryAOIndicator.setToolTip(tooltip)
            self.secondaryAOIndicator.show()
            return

        self.secondaryAOIndicator.hide()
        self.secondaryAOIndicator.setToolTip('')
        self.secondaryLabel.show()
        self.secondaryLabel.setText(str(text or ''))
        style = []
        if emphasized:
            style.append('font-weight: bold')
        if muted:
            style.append('color: palette(mid)')
        self.secondaryLabel.setStyleSheet('; '.join(style))
        self.secondaryLabel.setToolTip(tooltip)

    def setModulationFrequency(self, value):
        """ Sets the laser modulation frequency. """
        self.modulationFrequencyEdit.setText(f"{value}")
        self.modulationFrequencySlider.setValue(value)
    
    def setModulationDutyCycle(self, value):
        """ Sets the laser modulation duty cycle. """
        self.modulationDutyCycleEdit.setText(f"{value}")
        self.modulationDutyCycleSlider.setValue(value)


# Copyright (C) 2017 Federico Barabas 2020-2021 ImSwitch developers
# This file is part of Tormenta and ImSwitch.
#
# Tormenta and ImSwitch are free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Tormenta and Imswitch are distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
