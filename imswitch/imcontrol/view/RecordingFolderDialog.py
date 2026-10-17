"""Preferences > Recordings folder…: where recordings are saved by default.

The folder lives in ``imcontrol_options.json`` (``Options.recording``)
because it describes the computer -- which disk has room, which drive the
lab backs up -- not the microscope. Until this dialog it could only be
changed by editing that file, and the Recording widget's own folder field
forgets whatever is typed into it when ImSwitch closes. This dialog shows
what is in force, where the next recording would land, and hands the
values to the controller, which saves them and points the Recording
widget at the new folder at once.
"""

import os

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import FolderPathEdit


class RecordingFolderDialog(QtWidgets.QDialog):
    """Edit the default recordings folder; the controller saves and applies it."""

    #: ({'outputFolder': str, 'includeDateInOutputFolder': bool}) -- the
    #: operator asked to save these values.
    sigSaveRequested = QtCore.Signal(dict)

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Recordings folder')
        self.setModal(True)

        from imswitch.imcontrol.model.Options import RecordingOptions
        self._defaults = RecordingOptions()

        intro = QtWidgets.QLabel(
            'Where the Recording widget saves recordings and snapshots when '
            'ImSwitch starts. Stored in imcontrol_options.json on this '
            'computer, not in the setup file. Saving also points the '
            'Recording widget at the new folder now; a recording already '
            'running keeps its file.'
        )
        intro.setWordWrap(True)

        self.folderEdit = FolderPathEdit(caption='Recordings folder',
                                         fallback=self._defaults.outputFolder)
        self.folderEdit.setObjectName('outputFolder')
        self.folderEdit.setMinimumWidth(420)

        self.dateSubfolderBox = QtWidgets.QCheckBox(
            'Put each day’s recordings in a subfolder named by date (YYYY-MM-DD)'
        )
        self.dateSubfolderBox.setObjectName('includeDateInOutputFolder')

        self.previewLabel = QtWidgets.QLabel()
        self.previewLabel.setWordWrap(True)
        self.previewLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.folderEdit.textChanged.connect(self._updatePreview)
        self.dateSubfolderBox.toggled.connect(self._updatePreview)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal, self,
        )
        self.restoreButton = self.buttons.addButton(
            'Restore default', QtWidgets.QDialogButtonBox.ResetRole
        )
        self.restoreButton.clicked.connect(self.restoreDefaults)
        self.buttons.accepted.connect(self._requestSave)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(intro)
        layout.addWidget(self.folderEdit)
        layout.addWidget(self.dateSubfolderBox)
        layout.addWidget(self.previewLabel)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)
        self.resize(600, 0)
        self.setStatus('')

    # -- values ---------------------------------------------------------------

    def setValues(self, recordingOptions) -> None:
        """Show what the options file holds (the defaults when it holds none)."""
        options = recordingOptions if recordingOptions is not None else self._defaults
        self.folderEdit.setText(str(getattr(options, 'outputFolder', '') or ''))
        self.dateSubfolderBox.setChecked(
            bool(getattr(options, 'includeDateInOutputFolder', True))
        )
        self.setStatus('')
        self._updatePreview()

    def values(self) -> dict:
        return {
            'outputFolder': self.folderEdit.text().strip(),
            'includeDateInOutputFolder': self.dateSubfolderBox.isChecked(),
        }

    def restoreDefaults(self) -> None:
        self.folderEdit.setText(self._defaults.outputFolder)
        self.dateSubfolderBox.setChecked(self._defaults.includeDateInOutputFolder)
        self.setStatus('Default restored; Save to keep it.')

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text or ''))
        self.statusLabel.setVisible(bool(text))

    # -- internals ------------------------------------------------------------

    def _updatePreview(self, *_args) -> None:
        from imswitch.imcontrol.model.Options import RecordingOptions

        folder = os.path.expanduser(self.folderEdit.text().strip())
        if not folder:
            self.previewLabel.setText('Enter a folder.')
            return
        target = RecordingOptions(
            outputFolder=folder,
            includeDateInOutputFolder=self.dateSubfolderBox.isChecked(),
        ).folderFor()
        note = '' if os.path.isdir(target) else ' (created when the first file is saved)'
        self.previewLabel.setText(f'Recordings made today go to: {target}{note}')

    def _requestSave(self) -> None:
        # Not accept(): the controller closes the dialog once the folder is
        # saved, and leaves it open with the reason when it cannot be.
        self.sigSaveRequested.emit(self.values())
