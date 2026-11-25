from qtpy import QtCore, QtWidgets


class PickSetupDialog(QtWidgets.QDialog):
    """ Dialog for picking the setup to use for imcontrol. """

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, QtCore.Qt.WindowSystemMenuHint | QtCore.Qt.WindowTitleHint,
                         *args, **kwargs)
        self.setWindowTitle('Select hardware setup')

        self.informationLabel = QtWidgets.QLabel(
            'Select the configuration file for your hardware setup:'
        )
        # Which folder the list comes from: the setup folder is a setting
        # (Preferences > Setup folder…), so it is not always the default one.
        self.folderLabel = QtWidgets.QLabel()
        self.folderLabel.setWordWrap(True)
        self.folderLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.folderLabel.hide()
        self.setupPicker = QtWidgets.QComboBox()
        self.setupPicker.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal,
            self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.informationLabel)
        layout.addWidget(self.folderLabel)
        layout.addWidget(self.setupPicker)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

    def setFolder(self, folder):
        """ Say which folder the setups are listed from. """
        self.folderLabel.setText(f'Setup folder: {folder}' if folder else '')
        self.folderLabel.setVisible(bool(folder))

    def setSetups(self, setupList):
        self.setupPicker.clear()
        for setup in setupList:
            self.setupPicker.addItem(setup)

    def getSelectedSetup(self):
        return self.setupPicker.currentText()

    def setSelectedSetup(self, setup):
        index = self.setupPicker.findText(setup)
        if index > -1:
            self.setupPicker.setCurrentIndex(index)


def askForSetup(setups, selectedSetup, information=None, parent=None, folder=None):
    """ Show the dialog and wait for it: the setup picked, or None when the
    user cancelled or there was nothing to pick. """
    dialog = PickSetupDialog(parent)
    if information is not None:
        dialog.informationLabel.setText(information)
    if folder is not None:
        dialog.setFolder(folder)
    dialog.setSetups(setups)
    dialog.setSelectedSetup(selectedSetup)
    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return None
    return dialog.getSelectedSetup() or None


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
