from qtpy import QtCore, QtGui, QtWidgets

from .basewidgets import Widget


class _InstrumentBox(QtWidgets.QGroupBox):
    """One instrument: state, live values, settings and actions."""

    def __init__(self, name, quantities, settings, actions, parent=None):
        super().__init__(name, parent)
        self.name = name
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        top = QtWidgets.QHBoxLayout()
        self.stateLabel = QtWidgets.QLabel('Not connected')
        self.stateLabel.setWordWrap(True)
        self.connectButton = QtWidgets.QPushButton('Connect')
        self.liveCheck = QtWidgets.QCheckBox('Live')
        self.liveCheck.setChecked(True)
        self.liveCheck.setToolTip('Read the instrument continuously while it is connected '
                                  'and nobody else (a run, a script) holds it.')
        top.addWidget(self.stateLabel, 1)
        top.addWidget(self.liveCheck)
        top.addWidget(self.connectButton)
        layout.addLayout(top)

        values = QtWidgets.QGridLayout()
        values.setHorizontalSpacing(12)
        self.valueLabels = {}
        font = QtGui.QFont()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        for row, (key, label) in enumerate(quantities):
            values.addWidget(QtWidgets.QLabel(label), row, 0)
            value = QtWidgets.QLabel('—')
            value.setFont(font)
            value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            values.addWidget(value, row, 1)
            self.valueLabels[key] = value
        layout.addLayout(values)

        self.settingInputs = {}
        self.settingButtons = {}
        for spec in settings:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(spec.label))
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.0, 1e6)
            spin.setDecimals(1)
            spin.setSuffix(f' {spec.unit}' if spec.unit else '')
            button = QtWidgets.QPushButton('Set')
            row.addWidget(spin, 1)
            row.addWidget(button)
            layout.addLayout(row)
            self.settingInputs[spec.name] = spin
            self.settingButtons[spec.name] = button

        self.actionButtons = {}
        if actions:
            row = QtWidgets.QHBoxLayout()
            for spec in actions:
                button = QtWidgets.QPushButton(spec.label)
                button.setToolTip(spec.confirm or spec.label)
                row.addWidget(button)
                self.actionButtons[spec.name] = (button, spec)
            row.addStretch(1)
            layout.addLayout(row)

        self.messageLabel = QtWidgets.QLabel('')
        self.messageLabel.setWordWrap(True)
        self.messageLabel.setStyleSheet('color: gray;')
        layout.addWidget(self.messageLabel)


class InstrumentsWidget(Widget):
    """Live readings of the setup's instruments (power meters, polarimeters).

    Measurements are made by scripts and measurement runs; this panel shows
    what the instruments read, and connects, configures and zeroes them.
    """

    sigConnectRequested = QtCore.Signal(str, bool)       # name, connect (False: disconnect)
    sigLiveToggled = QtCore.Signal(str, bool)
    sigSettingRequested = QtCore.Signal(str, str, float)  # name, setting, value
    sigActionRequested = QtCore.Signal(str, str)          # name, action (confirmed)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.boxes = {}
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._empty = QtWidgets.QLabel('This setup has no instruments.')
        self._layout.addWidget(self._empty)
        self._layout.addStretch(1)

    def addInstrument(self, name, quantities, settings=(), actions=()):
        """``quantities``: ``(key, label)`` pairs in display order."""
        box = _InstrumentBox(name, quantities, settings, actions, self)
        self._empty.hide()
        self._layout.insertWidget(self._layout.count() - 1, box)
        self.boxes[name] = box
        box.connectButton.clicked.connect(
            lambda _=False, n=name: self.sigConnectRequested.emit(
                n, self.boxes[n].connectButton.text() == 'Connect'))
        box.liveCheck.toggled.connect(lambda on, n=name: self.sigLiveToggled.emit(n, on))
        for setting, button in box.settingButtons.items():
            button.clicked.connect(
                lambda _=False, n=name, s=setting: self.sigSettingRequested.emit(
                    n, s, float(self.boxes[n].settingInputs[s].value())))
        for action, (button, spec) in box.actionButtons.items():
            button.clicked.connect(
                lambda _=False, n=name, a=action, sp=spec: self._confirmAction(n, a, sp))

    def _confirmAction(self, name, action, spec):
        if spec.requires_dark or spec.confirm:
            text = spec.confirm or f'Run {spec.label} on {name}?'
            if spec.requires_dark:
                text += '\n\nOnly continue once no light reaches the sensor.'
            answer = QtWidgets.QMessageBox.question(
                self, f'{spec.label} — {name}', text,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self.sigActionRequested.emit(name, action)

    def setState(self, name, text, *, connected, usable):
        """``connected``: the instrument is connected (offer Disconnect);
        ``usable``: settings and actions may be sent now (connected and not
        held by a run or script)."""
        box = self.boxes.get(name)
        if box is None:
            return
        box.stateLabel.setText(text)
        box.connectButton.setText('Disconnect' if connected else 'Connect')
        for spin in box.settingInputs.values():
            spin.setEnabled(usable)
        for button in box.settingButtons.values():
            button.setEnabled(usable)
        for button, _spec in box.actionButtons.values():
            button.setEnabled(usable)
        if not connected:
            for label in box.valueLabels.values():
                label.setText('—')

    def setValues(self, name, texts):
        box = self.boxes.get(name)
        if box is None:
            return
        for key, text in texts.items():
            label = box.valueLabels.get(key)
            if label is not None:
                label.setText(text)

    def setSettingValue(self, name, setting, value):
        box = self.boxes.get(name)
        if box is None or setting not in box.settingInputs:
            return
        spin = box.settingInputs[setting]
        if not spin.hasFocus():          # never overwrite what is being typed
            spin.setValue(float(value))

    def setMessage(self, name, text):
        box = self.boxes.get(name)
        if box is not None:
            box.messageLabel.setText(text)

    def setBusy(self, name, busy):
        box = self.boxes.get(name)
        if box is not None:
            box.connectButton.setEnabled(not busy)
