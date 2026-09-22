"""Free-text notes an operator attaches to this session's recordings.

Plenty of what matters about an experiment never reaches a widget: the power
measured in the back focal plane this morning, which coverslip batch is on the
stage, that the 561 line was behaving oddly after lunch. This dialog is the
place for it. Whatever it holds when a recording starts is written into that
recording's metadata, so the note travels with the data rather than with the
person who remembers it.

"Session" means until ImSwitch is closed: the text is deliberately not saved to
disk with the other widget state, because a note about today's alignment would
be a lie tomorrow.
"""

from qtpy import QtCore, QtWidgets


class SessionNotesDialog(QtWidgets.QDialog):
    """Modeless editor for the session note.

    Modeless on purpose -- the point is to jot something down *while* running
    an experiment, not to be locked out of the rest of the GUI first. The text
    is published on every keystroke, so what the box shows is always what the
    next recording will carry; there is no Apply button to forget.
    """

    #: (text) -- emitted whenever the note changes.
    sigNotesChanged = QtCore.Signal(str)

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Session notes')
        self.setModal(False)

        self.informationLabel = QtWidgets.QLabel(
            'Free-text notes attached to the metadata of every recording and\n'
            'snapshot saved from now until ImSwitch is closed. Use it for what\n'
            'the rest of the GUI cannot capture, e.g. "measured 10 mW in the\n'
            'BFP for the 405 laser".'
        )

        self.notesEdit = QtWidgets.QPlainTextEdit()
        self.notesEdit.setPlaceholderText('Notes about this session…')
        self.notesEdit.setTabChangesFocus(True)
        self.notesEdit.textChanged.connect(self._onTextChanged)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setEnabled(False)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Close, QtCore.Qt.Horizontal, self
        )
        self.clearButton = self.buttons.addButton(
            'Clear', QtWidgets.QDialogButtonBox.ResetRole
        )
        self.clearButton.clicked.connect(self.notesEdit.clear)
        self.buttons.rejected.connect(self.close)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.informationLabel)
        layout.addWidget(self.notesEdit)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        self.resize(460, 320)
        self._updateStatus()

    def getNotes(self) -> str:
        return self.notesEdit.toPlainText()

    def setNotes(self, notes: str) -> None:
        """Show ``notes`` without re-announcing them as an edit."""
        text = '' if notes is None else str(notes)
        if text == self.notesEdit.toPlainText():
            return
        blocked = self.notesEdit.blockSignals(True)
        try:
            self.notesEdit.setPlainText(text)
        finally:
            self.notesEdit.blockSignals(blocked)
        self._updateStatus()

    def _onTextChanged(self) -> None:
        self._updateStatus()
        self.sigNotesChanged.emit(self.getNotes())

    def _updateStatus(self) -> None:
        if self.getNotes().strip():
            self.statusLabel.setText('Attached to new recordings.')
        else:
            self.statusLabel.setText('Empty — nothing is attached.')


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
