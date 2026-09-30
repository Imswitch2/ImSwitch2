from qtpy import QtCore, QtWidgets

# guitools widgets used here (BetterPushButton, askForFolderPath) are shared
# imcommon widgets; import them from imcommon directly rather than through
# imcontrol's re-export, so improcess does not depend on imcontrol.
from imswitch.imcommon.view import guitools


class DirectoryWatcherFrame(QtWidgets.QFrame):
    """Frame for live-reconstructing timelapse folders as they appear.

    Pick a root folder, tick "Watch directory (live)", and
    ``LiveModeController`` watches it for new timelapse sub-folders and streams
    each one through the active reconstructor.

    The folder here is the *root* holding timelapse sub-folders, i.e. one level
    above the ``.zarr`` stores themselves.
    """

    sigLiveChanged = QtCore.Signal(bool)  # (enabled)
    sigResetClicked = QtCore.Signal()
    sigSkipClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.path = ''
        self.folderEdit = QtWidgets.QLineEdit(self.path)

        self.browseFolderButton = guitools.BetterPushButton('Browse')
        self.liveCheck = QtWidgets.QCheckBox('Start monitoring')
        self.saveCheck = QtWidgets.QCheckBox('Save reconstruction(s) (.tif)')
        self.saveCheck.setToolTip(
            'When a timelapse finishes or is skipped, write its reconstruction '
            'to <watched folder>_recon/<name>_recon.tif, next to the watched '
            'folder.'
        )
        self.skipButton = guitools.BetterPushButton('Skip directory')
        self.skipButton.setToolTip(
            'Stop reconstructing the current timelapse and move on to the next '
            'one. What has already been reconstructed is kept; the timepoint in '
            'progress is finished first.'
        )
        self.resetButton = guitools.BetterPushButton('Reset')
        self.resetButton.setToolTip(
            'Forget which timelapse folders have been processed, so the watched '
            'folder is reconstructed again from the start. Already-reconstructed '
            'timelapses get their own new entries.'
        )

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)

        layout.addWidget(self.browseFolderButton, 0, 0)
        layout.addWidget(self.folderEdit, 0, 1)
        layout.addWidget(self.liveCheck, 1, 0)
        layout.addWidget(self.saveCheck, 1, 1)
        layout.addWidget(self.skipButton, 2, 0)
        layout.addWidget(self.resetButton, 3, 0)

        self.liveCheck.toggled.connect(self.sigLiveChanged)
        self.skipButton.clicked.connect(self.sigSkipClicked)
        self.resetButton.clicked.connect(self.sigResetClicked)
        self.browseFolderButton.clicked.connect(self.browse)

    def browse(self):
        path = guitools.askForFolderPath(self, defaultFolder=self.path)
        if path:
            self.path = path
            self.folderEdit.setText(self.path)


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
