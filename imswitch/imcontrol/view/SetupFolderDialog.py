"""Preferences > Setup folder…: where this computer keeps its setup files.

The folder lives in ``imcontrol_options.json`` (``Options.setupFolder``)
because it describes the computer -- a lab share, a git checkout of the
rig's configurations -- not a microscope. Setup files may be sorted into
subfolders of it; the setup in use is named relative to it.

The same dialog serves ImSwitch (Preferences menu of Hardware Control) and
the Config Studio (its Preferences menu). It shows which setups the typed
folder holds and lets the operator pick the one to use from it; the host
validates and saves the values -- ImSwitch restarts to load the setup, the
Config Studio switches its file list to the folder.
"""

import os

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import FolderPathEdit
from imswitch.imcontrol.model import setupfolder


class SetupFolderDialog(QtWidgets.QDialog):
    """Edit the setup folder and the setup to use; the host saves them."""

    #: ({'setupFolder': str, 'setupFileName': str}) -- the operator asked to
    #: save these values. ``setupFileName`` is relative to the folder, and ''
    #: when the folder holds no setup.
    sigSaveRequested = QtCore.Signal(dict)

    def __init__(self, parent=None, *args, appliesWhen='', **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Setup folder')
        self.setModal(True)
        self._currentSetup = ''

        intro = QtWidgets.QLabel(
            'Where this computer keeps its hardware setup files, subfolders '
            'included (stored in imcontrol_options.json). The shipped example '
            'setups stay in the default folder.'
            + (f' {appliesWhen}' if appliesWhen else '')
        )
        intro.setWordWrap(True)

        self.folderEdit = FolderPathEdit(caption='Setup folder',
                                         fallback=setupfolder.defaultSetupFolder())
        self.folderEdit.setObjectName('setupFolder')
        self.folderEdit.setMinimumWidth(460)

        self.setupPicker = QtWidgets.QComboBox()
        self.setupPicker.setObjectName('setupFileName')
        self.setupPicker.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLength)
        self.setupPicker.setMinimumContentsLength(40)

        form = QtWidgets.QFormLayout()
        form.addRow('Folder:', self.folderEdit)
        form.addRow('Setup to use:', self.setupPicker)

        self.previewLabel = QtWidgets.QLabel()
        self.previewLabel.setWordWrap(True)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        # Listing a folder walks its subfolders, so typing does not list every
        # partial path ("C:\" would be a whole drive): wait for a pause.
        self._refreshTimer = QtCore.QTimer(self)
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(250)
        self._refreshTimer.timeout.connect(self.refreshSetups)
        self.folderEdit.textChanged.connect(self._refreshTimer.start)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal, self,
        )
        self.restoreButton = self.buttons.addButton(
            'Restore default', QtWidgets.QDialogButtonBox.ResetRole
        )
        self.restoreButton.clicked.connect(self.restoreDefault)
        self.buttons.accepted.connect(self._requestSave)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.previewLabel)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)
        self.resize(640, 0)
        self.setStatus('')

    # -- values ---------------------------------------------------------------

    def setValues(self, folder, setupFileName='') -> None:
        """Show ``folder`` and preselect ``setupFileName`` (the setup in use)
        in it, or the setup of the same file name in a subfolder."""
        self._currentSetup = str(setupFileName or '')
        self.folderEdit.setText(str(folder or ''))
        self._refreshTimer.stop()
        self.refreshSetups()
        self.setStatus('')
        # Tall enough for the wrapped intro at the dialog's width: a size
        # computed before the text wrapped would cut its first lines off.
        self.resize(self.width(), self.heightForWidth(self.width())
                    if self.layout().hasHeightForWidth() else self.sizeHint().height())

    def values(self) -> dict:
        return {
            'setupFolder': self.folderEdit.text().strip(),
            'setupFileName': self.setupPicker.currentText(),
        }

    def restoreDefault(self) -> None:
        self.folderEdit.setText(setupfolder.defaultSetupFolder())
        self._refreshTimer.stop()
        self.refreshSetups()
        self.setStatus('Default restored; Save to keep it.')

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text or ''))
        self.statusLabel.setVisible(bool(text))

    # -- internals ------------------------------------------------------------

    def _typedFolder(self) -> str:
        return os.path.expanduser(self.folderEdit.text().strip())

    def refreshSetups(self) -> None:
        """List the setups of the typed folder in the picker, keeping the
        selection when the new folder has the same setup."""
        folder = self._typedFolder()
        previous = self.setupPicker.currentText()
        setups = (setupfolder.listSetupFiles(folder)
                  if folder and os.path.isabs(folder) else [])

        self.setupPicker.clear()
        self.setupPicker.addItems(setups)
        for wanted in (previous, self._currentSetup):
            found = setupfolder.findSetupFile(folder, wanted) if setups and wanted else None
            if found in setups:
                self.setupPicker.setCurrentIndex(setups.index(found))
                break
        self.setupPicker.setEnabled(bool(setups))

        if not folder:
            preview = 'Enter a folder.'
        elif not os.path.isabs(folder):
            preview = 'Enter a full path.'
        elif not os.path.isdir(folder):
            preview = 'This folder does not exist yet.'
        elif not setups:
            preview = 'No setup files (.json) in this folder yet.'
        else:
            preview = (f'{len(setups)} setup file{"s" if len(setups) != 1 else ""} '
                       f'in this folder and its subfolders.')
        self.previewLabel.setText(preview)

    def _requestSave(self) -> None:
        # Not accept(): the host closes the dialog once the values are saved,
        # and leaves it open with the reason when they cannot be.
        self._refreshTimer.stop()
        self.refreshSetups()
        self.sigSaveRequested.emit(self.values())


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
