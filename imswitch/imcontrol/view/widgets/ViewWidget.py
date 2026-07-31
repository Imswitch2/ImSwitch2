from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model.shortcut import shortcut
from imswitch.imcontrol.view import guitools as guitools
from .basewidgets import Widget


class ViewWidget(Widget):
    """ View settings (liveview) and detector acquisition selection. """

    sigLiveviewToggled = QtCore.Signal(bool)  # (enabled)
    sigDetectorSelectionChanged = QtCore.Signal(str, bool)  # (detector, selected)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # "with", not "from": a detector is the instrument an image is
        # acquired with, which is also how a methods section reads.
        self.detectorSelectionGroup = QtWidgets.QGroupBox('Acquire with')
        self._detectorSelectionLayout = QtWidgets.QVBoxLayout()
        self.detectorSelectionGroup.setLayout(self._detectorSelectionLayout)
        self.detectorSelectionGroup.setVisible(False)
        self.detectorSelectionBoxes = {}

        self.liveviewButton = guitools.BetterPushButton('LIVEVIEW')
        self.liveviewButton.setStyleSheet("font-size:20px")
        self.liveviewButton.setCheckable(True)
        self.liveviewButton.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                          QtWidgets.QSizePolicy.Expanding)
        self.liveviewButton.setEnabled(True)

        self.viewCtrlLayout = QtWidgets.QGridLayout()
        self.setLayout(self.viewCtrlLayout)
        self.viewCtrlLayout.addWidget(self.detectorSelectionGroup, 0, 0)
        self.viewCtrlLayout.addWidget(self.liveviewButton, 1, 0)

        self.liveviewButton.toggled.connect(self.sigLiveviewToggled)

    def setDetectorSelectionOptions(self, detectorNames):
        """ Build one checkbox per selectable detector.

        Hidden entirely with fewer than two detectors: there is nothing to
        choose between, and an always-ticked lone box only invites someone to
        untick it and wonder why nothing works.
        """
        for box in self.detectorSelectionBoxes.values():
            self._detectorSelectionLayout.removeWidget(box)
            box.deleteLater()
        self.detectorSelectionBoxes = {}

        for detectorName in detectorNames:
            box = QtWidgets.QCheckBox(detectorName)
            box.setChecked(True)
            box.setToolTip(
                f'Acquire with "{detectorName}".\n\n'
                f'Unticking leaves it out of live view and of scans, which is '
                f'how you stop paying for a detector you are not reading. It '
                f'still runs whenever a recording, workflow or event modality '
                f'is actively using it.'
            )
            box.toggled.connect(
                lambda checked, name=detectorName:
                    self.sigDetectorSelectionChanged.emit(name, checked)
            )
            self._detectorSelectionLayout.addWidget(box)
            self.detectorSelectionBoxes[detectorName] = box

        self.detectorSelectionGroup.setVisible(len(detectorNames) > 1)

    def setDetectorSelected(self, detectorName, selected):
        """ Set a checkbox without emitting, to sync the UI to real state. """
        box = self.detectorSelectionBoxes.get(detectorName)
        if box is None:
            return
        box.blockSignals(True)
        try:
            box.setChecked(selected)
        finally:
            box.blockSignals(False)

    def setDetectorSelectionPending(self, detectorName, pending):
        """ Mark a change that a running scan has deferred.

        The box shows what was asked for, italicised, so it is visibly a
        request rather than the current state — otherwise it would silently
        disagree with the hardware until the scan reached its next iteration.
        """
        box = self.detectorSelectionBoxes.get(detectorName)
        if box is None:
            return
        font = box.font()
        font.setItalic(pending)
        box.setFont(font)
        box.setToolTip(
            f'"{detectorName}" is in use by the running scan; this takes '
            f'effect at its next iteration.'
            if pending else
            f'Acquire with "{detectorName}".'
        )

    def getLiveViewActive(self):
        return self.liveviewButton.isChecked()

    def setLiveViewActive(self, active):
        """ Sets whether the LiveView is active. """
        self.liveviewButton.setChecked(active)

    @shortcut(actionId="view.toggleLiveView", defaultKey="Ctrl+L",
              displayName="Liveview", initiallyBound=True)
    def toggleLiveviewButton(self):
        self.liveviewButton.toggle()


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
