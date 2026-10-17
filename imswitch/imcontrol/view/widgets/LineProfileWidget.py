from qtpy import QtWidgets

from imswitch.imcommon.view.guitools.ProfileWidget import ProfileWidget
from .basewidgets import Widget


class LineProfileWidget(Widget):
    """The Line Profile dock: the shared Profile widget on the live image.

    ImProcess's Profile panel is the same class
    (``imswitch.imcommon.view.guitools.ProfileWidget``), so both apps get the
    same line and rectangle profiles, width averaging, fits, Δx measurement
    and CSV export, and a fix to one is a fix to both. What imcontrol adds is
    set here:

    * the plot follows the live image as new frames arrive;
    * *Intensity vs T* traces the mean inside a rectangle over time;
    * distances are in micrometres, the unit live layers are scaled in.

    Viewer Tools' Line / Rectangle ROI / ROI Intensity vs T buttons drive this
    panel's modes (see ``ViewerToolsController``), so either set of buttons
    can be used.
    """

    #: Drawing modes offered, in button order.
    MODES = ('pan', 'line', 'rectangle', 'timetrace', 'zprofile')

    def __init__(self, *args, napariViewer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile = None
        if napariViewer is None:
            self.replaceWithError(
                'The line profile needs the image display: enable "Image" in'
                ' this setup\'s available widgets.'
            )
            return

        self.profile = ProfileWidget(
            napariViewer,
            modes=self.MODES,
            # There is no Results table or Graph panel in imcontrol to push to.
            pushTargets=False,
            liveUpdates=True,
            defaultUnit='µm',
            toolOwner='imcontrol.lineprofile',
            emptyHint='Draw a line or rectangle on the image',
        )
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.profile)
        self.setLayout(layout)


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
