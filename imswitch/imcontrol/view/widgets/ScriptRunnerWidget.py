import os

from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget

_ERROR_STYLE = 'QLineEdit { border: 1px solid #d9534f; }'


class ScriptRunnerWidget(Widget):
    """ Runs one script file, with a text field for each input the script
    declares with ``scriptInput(...)``. """

    sigBrowseClicked = QtCore.Signal()
    sigRunClicked = QtCore.Signal()
    sigStopClicked = QtCore.Signal()
    sigInputEdited = QtCore.Signal(str, str)  # (inputName, text)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.scriptLabel = QtWidgets.QLabel()
        self.scriptLabel.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                       QtWidgets.QSizePolicy.Preferred)
        self.browseButton = guitools.BetterPushButton('Choose script…')

        self.inputsBox = QtWidgets.QWidget()
        self.inputsLayout = QtWidgets.QFormLayout(self.inputsBox)
        self.inputsLayout.setContentsMargins(0, 0, 0, 0)

        self.messageLabel = QtWidgets.QLabel()
        self.messageLabel.setWordWrap(True)
        self.messageLabel.hide()

        self.runButton = guitools.BetterPushButton('Run')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                       QtWidgets.QSizePolicy.Preferred)

        scriptRow = QtWidgets.QHBoxLayout()
        scriptRow.addWidget(QtWidgets.QLabel('Script:'))
        scriptRow.addWidget(self.scriptLabel, 1)
        scriptRow.addWidget(self.browseButton)

        runRow = QtWidgets.QHBoxLayout()
        runRow.addWidget(self.runButton)
        runRow.addWidget(self.stopButton)
        runRow.addWidget(self.statusLabel, 1)

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        layout.addLayout(scriptRow)
        layout.addWidget(self.inputsBox)
        layout.addWidget(self.messageLabel)
        layout.addLayout(runRow)
        layout.addStretch(1)

        self._inputEdits = {}  # { inputName: QLineEdit }

        self.browseButton.clicked.connect(self.sigBrowseClicked)
        self.runButton.clicked.connect(self.sigRunClicked)
        self.stopButton.clicked.connect(self.sigStopClicked)

        self.setScriptPath(None)

    def askForScriptPath(self, defaultFolder):
        return guitools.askForFilePath(self, 'Choose script', defaultFolder=defaultFolder,
                                       nameFilter='Python scripts (*.py)')

    def setScriptPath(self, path):
        if path:
            self.scriptLabel.setText(os.path.basename(path))
            self.scriptLabel.setToolTip(path)
        else:
            self.scriptLabel.setText('(none)')
            self.scriptLabel.setToolTip('')

    def setInputs(self, specs, texts):
        """ Replaces the input fields with one per ScriptInputSpec, filled with
        texts[name]. """
        while self.inputsLayout.rowCount():
            self.inputsLayout.removeRow(0)
        self._inputEdits = {}

        for spec in specs:
            edit = QtWidgets.QLineEdit(texts.get(spec.name, ''))
            edit.setPlaceholderText(spec.defaultText() or 'script default')
            edit.setToolTip(_inputToolTip(spec))
            edit.textEdited.connect(
                lambda text, name=spec.name: self.sigInputEdited.emit(name, text)
            )
            label = QtWidgets.QLabel(spec.name)
            label.setToolTip(edit.toolTip())
            self.inputsLayout.addRow(label, edit)
            self._inputEdits[spec.name] = edit
        self.inputsBox.setVisible(bool(specs))

    def getInputTexts(self):
        return {name: edit.text() for name, edit in self._inputEdits.items()}

    def setInputError(self, name, message):
        """ Marks the field of input name as invalid with message, or clears
        the mark when message is None. """
        edit = self._inputEdits.get(name)
        if edit is None:
            return
        edit.setStyleSheet(_ERROR_STYLE if message else '')
        baseToolTip = edit.toolTip().split('\n\n⚠ ')[0]
        edit.setToolTip(f'{baseToolTip}\n\n⚠ {message}' if message else baseToolTip)

    def setMessage(self, text):
        self.messageLabel.setText(text or '')
        self.messageLabel.setVisible(bool(text))

    def setStatus(self, text, toolTip=''):
        self.statusLabel.setText(text)
        self.statusLabel.setToolTip(toolTip or text)

    def setRunEnabled(self, enabled, toolTip=''):
        self.runButton.setEnabled(enabled)
        self.runButton.setToolTip(toolTip)

    def setStopEnabled(self, enabled):
        self.stopButton.setEnabled(enabled)


def _inputToolTip(spec):
    lines = [spec.description] if spec.description else []
    if spec.defaultKnown:
        default = spec.defaultText() or 'none'
        lines.append(f'Default: {default} ({spec.type.__name__})')
    else:
        lines.append('Default: computed by the script; leave empty to use it')
    lines.append(f'Declared on line {spec.line}')
    return '\n'.join(lines)


# Copyright (C) 2020-2026 ImSwitch developers
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
