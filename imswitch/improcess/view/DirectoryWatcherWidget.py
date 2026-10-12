from qtpy import QtCore, QtWidgets

# guitools widgets used here (BetterPushButton, askForFolderPath) are shared
# imcommon widgets; import them from imcommon directly rather than through
# imcontrol's re-export, so improcess does not depend on imcontrol.
from imswitch.imcommon.view import guitools


class DirectoryWatcherWidget(QtWidgets.QFrame):
    """Pick a root folder and start monitoring it."""

    sigLiveChanged = QtCore.Signal(bool)  # (enabled)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.path = ''
        self.folderEdit = QtWidgets.QLineEdit(self.path)
        self.browseFolderButton = guitools.BetterPushButton('Browse')
        self.liveCheck = QtWidgets.QCheckBox('Start monitoring')

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)
        layout.addWidget(self.browseFolderButton, 0, 0)
        layout.addWidget(self.folderEdit, 0, 1)
        layout.addWidget(self.liveCheck, 1, 0)

        self.liveCheck.toggled.connect(self.sigLiveChanged)
        self.browseFolderButton.clicked.connect(self.browse)
        # The box is the single source of self.path: typing and Browse both
        # end up here, so neither can leave the other stale.
        self.folderEdit.textChanged.connect(self._setPath)

    def browse(self):
        path = guitools.askForFolderPath(self, defaultFolder=self.path)
        if path:
            self.folderEdit.setText(path)

    def _setPath(self, text: str) -> None:
        self.path = text.strip()


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
