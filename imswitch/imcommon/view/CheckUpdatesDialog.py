import sys

from qtpy import QtCore, QtWidgets

import imswitch


class CheckUpdatesDialog(QtWidgets.QDialog):
    """ Dialog for checking for ImSwitch updates. """

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, QtCore.Qt.WindowSystemMenuHint | QtCore.Qt.WindowTitleHint,
                         *args, **kwargs)
        self.setWindowTitle('Check for updates')
        self.setMinimumWidth(540)

        self.informationLabel = QtWidgets.QLabel()
        self.informationLabel.setWordWrap(True)
        self.informationLabel.setStyleSheet('font-size: 10pt')

        self.linkLabel = QtWidgets.QLabel()
        self.linkLabel.setWordWrap(True)
        self.linkLabel.setTextFormat(QtCore.Qt.RichText)
        self.linkLabel.setOpenExternalLinks(True)
        self.linkLabel.setVisible(False)
        self.linkLabel.setStyleSheet('font-size: 10pt')

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok,
            QtCore.Qt.Horizontal,
            self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.setSpacing(16)
        layout.addWidget(self.informationLabel)
        layout.addWidget(self.linkLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

    def resetUpdateInfo(self):
        self.informationLabel.setText('Checking for updates, please wait…')
        self.linkLabel.setText('')
        self.linkLabel.setVisible(False)

    def showFailed(self):
        self.informationLabel.setText('Failed to check for updates.')
        self.linkLabel.setText('')
        self.linkLabel.setVisible(False)

    def showNoUpdateAvailable(self):
        self.informationLabel.setText('No updates available.')
        self.linkLabel.setText('')
        self.linkLabel.setVisible(False)

    #: What to do with the download, per platform.  The standalone builds are
    #: installers now -- a Setup .exe that upgrades in place and a disk image you
    #: drag across.  The text this replaced came from the 1.x zip bundles and told
    #: users to extract into a new folder and *not* overwrite the old install,
    #: which is the opposite of what both installers want.
    _BUNDLE_UPDATE_STEPS = {
        'win32': (
            'To update, download the installer below and run it. It upgrades this'
            ' installation in place, so there is no need to uninstall first.'
            '\n\nWindows will warn that the installer is unsigned: choose'
            ' "More info" and then "Run anyway".'
        ),
        'darwin': (
            'To update, download the disk image below and drag ImSwitch2 into your'
            ' Applications folder, replacing the version already there.'
            '\n\nmacOS will refuse to open it the first time because it is unsigned:'
            ' right-click ImSwitch2 and choose "Open".'
        ),
    }
    _BUNDLE_UPDATE_FALLBACK = 'To update, download the new version below.'

    def showPyInstallerUpdate(self, newVersion):
        steps = self._BUNDLE_UPDATE_STEPS.get(sys.platform, self._BUNDLE_UPDATE_FALLBACK)
        self.informationLabel.setText(
            f'ImSwitch2 {newVersion} is now available.'
            f' Your current version is {imswitch.__version__}.'
            f'\n\n{steps}'
            f'\n\nYour settings and hardware setups in ImSwitchConfig are kept.'
        )
        self.linkLabel.setText(
            'The new version may be downloaded from '
            f'<a href="https://github.com/{imswitch.__github_repo__}/releases/latest"'
            ' style="color: orange">'
            'the latest release'
            '</a>'
            '.'
        )
        self.linkLabel.setVisible(True)

    def showPyPIUpdate(self, newVersion):
        self.informationLabel.setText(
            f'ImSwitch2 {newVersion} is now available.'
            f' Your current version is {imswitch.__version__}.'
            f'\n\nTo update, run the command: pip install --upgrade {imswitch.__distname__}'
        )
        self.linkLabel.setText(
            'The changelog is available '
            f'<a href="https://github.com/{imswitch.__github_repo__}/blob/main/docs/changelog.rst"'
            ' style="color: orange">'
            'here'
            '</a>'
            '.'
        )
        self.linkLabel.setVisible(True)


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
