from qtpy import QtCore, QtWidgets

# guitools widgets used here (BetterPushButton, askForFolderPath) are shared
# imcommon widgets; import them from imcommon directly rather than through
# imcontrol's re-export, so improcess does not depend on imcontrol.
from imswitch.imcommon.view import guitools


class DirectoryWatcherWidget(QtWidgets.QFrame):
    """Pick a root folder and start monitoring it."""

    sigMonitorChanged = QtCore.Signal(bool)  # (enabled)
    sigSkipEntryClicked = QtCore.Signal()
    sigResetClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.path = r"C:\Users\karl.hojlund\Documents\testalab\tl_data\live_testing"
        self.folderEdit = QtWidgets.QLineEdit(self.path)
        self.browseFolderButton = guitools.BetterPushButton("Browse")
        self.monitorFolderCheck = QtWidgets.QCheckBox("Monitor")
        self.skipEntryButton = guitools.BetterPushButton("Skip entry")
        self.skipEntryButton.setToolTip(
            "Stop reading the entry being reconstructed and move on to the next "
            "one. The frames already read are still reconstructed and shown."
        )
        self.skipEntryButton.setEnabled(False)  # only while monitoring
        self.resetButton = guitools.BetterPushButton("Reset")
        self.resetButton.setToolTip(
            "Stop monitoring and forget the entries already seen, so that the "
            "next time Monitor is ticked, every entry in the folder is "
            "reconstructed again."
        )

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)
        layout.addWidget(self.browseFolderButton, 0, 0)
        layout.addWidget(self.folderEdit, 0, 1)
        layout.addWidget(self.monitorFolderCheck, 1, 0)
        layout.addWidget(self.skipEntryButton, 2, 0)
        layout.addWidget(self.resetButton, 2, 1)

        self.monitorFolderCheck.toggled.connect(self.sigMonitorChanged)
        self.monitorFolderCheck.toggled.connect(self.skipEntryButton.setEnabled)
        self.skipEntryButton.clicked.connect(self.sigSkipEntryClicked)
        self.resetButton.clicked.connect(self.sigResetClicked)
        self.browseFolderButton.clicked.connect(self.browse)
        # The box is the single source of self.path: typing and Browse both
        # end up here, so neither can leave the other stale.
        self.folderEdit.textChanged.connect(self._setPath)

    def browse(self):
        path = guitools.askForFolderPath(self, defaultFolder=self.path)
        if path:
            self.folderEdit.setText(path)

    def showError(self, message: str):
        guitools.showWarning(self, "Directory watcher", message)

    def _setPath(self, text: str):
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
