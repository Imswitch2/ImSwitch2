"""Preferences > Default folders…: where ImProcess's file dialogs start.

The two folders used to be set from the File menu with a bare folder picker,
which neither showed what was set nor offered a way to unset it, and the
choice was forgotten when ImSwitch closed. This dialog shows both, lets
either be cleared, and hands the values to FileIOController, which saves
them in ``improcess_options.json`` and applies them at once.
"""

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import FolderPathEdit


class FolderPreferencesDialog(QtWidgets.QDialog):
    """Edit the default data and save folders; the controller saves them."""

    #: ({'dataFolder': str, 'saveFolder': str}) -- the user asked to save
    #: these values ('' = no default).
    sigSaveRequested = QtCore.Signal(dict)

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Default folders')
        self.setModal(True)

        intro = QtWidgets.QLabel(
            'Where ImProcess’s open and save dialogs start. Stored in '
            'improcess_options.json on this computer and kept between '
            'sessions. Leave a folder empty to have no default.'
        )
        intro.setWordWrap(True)

        self.dataFolderEdit = FolderPathEdit(caption='Default data folder', clearable=True)
        self.dataFolderEdit.setObjectName('dataFolder')
        self.dataFolderEdit.setPlaceholderText('No default: the system chooses')
        self.dataFolderEdit.setMinimumWidth(420)
        self.saveFolderEdit = FolderPathEdit(caption='Default save folder', clearable=True)
        self.saveFolderEdit.setObjectName('saveFolder')
        self.saveFolderEdit.setPlaceholderText('No default: the data folder')

        form = QtWidgets.QFormLayout()
        form.addRow('Open data from:', self.dataFolderEdit)
        form.addRow('Save results to:', self.saveFolderEdit)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal, self,
        )
        self.buttons.accepted.connect(self._requestSave)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)
        self.resize(600, 0)
        self.setStatus('')

    def setValues(self, preferences) -> None:
        """Show the folders in force ('' or None for no default)."""
        self.dataFolderEdit.setText(getattr(preferences, 'dataFolder', '') or '')
        self.saveFolderEdit.setText(getattr(preferences, 'saveFolder', '') or '')
        self.setStatus('')

    def values(self) -> dict:
        return {
            'dataFolder': self.dataFolderEdit.text().strip(),
            'saveFolder': self.saveFolderEdit.text().strip(),
        }

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text or ''))
        self.statusLabel.setVisible(bool(text))

    def _requestSave(self) -> None:
        # Not accept(): the controller closes the dialog once the folders are
        # saved, and leaves it open with the reason when they cannot be.
        self.sigSaveRequested.emit(self.values())
