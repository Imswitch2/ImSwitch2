from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view import guitools
from .basewidgets import Widget
from .LightSheetMulticolorWidget import LightSheetMulticolorWidget
from .TriggerScopePLSRMulticolorWidget import TriggerScopePLSRMulticolorWidget


class TriggerScopeScanWidget(Widget):
    """Unified TriggerScope scan widget for the RESOLFT family.

    Hosts the two cousin scan panels (multicolor light-sheet and pLS-RESOLFT
    multicolor) behind a single mode selector + stacked panel, with shared
    Run / Save / Load / auto-REC chrome. The per-panel chrome built by the
    embedded widgets is hidden via their ``hideChrome()`` method; this widget
    owns the shared controls and forwards the relevant signals to a single
    controller (TriggerScopeScanController).
    """

    sigSaveScanClicked = QtCore.Signal()
    sigLoadScanClicked = QtCore.Signal()
    sigRunScanClicked = QtCore.Signal()
    sigParameterChanged = QtCore.Signal()
    sigModeChanged = QtCore.Signal()

    MODE_LIGHTSHEET = 'lightsheet'
    MODE_PLSR = 'plsr'
    _MODE_ORDER = (MODE_LIGHTSHEET, MODE_PLSR)
    _MODE_LABELS = {
        MODE_LIGHTSHEET: 'Light-Sheet Multicolor',
        MODE_PLSR: 'pLS-RESOLFT Multicolor',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No minimum height of its own: docks stack vertically and a
        # splitter's minimum is the sum of its children's, so a panel that
        # insists on 200 px makes the window that much taller to open --
        # and a few of them together make it taller than the screen, at
        # which point Qt keeps the window at its minimum and the bottom is
        # cut off. Each mode page scrolls its own parameter form.
        self.setMinimumSize(0, 0)

        # ---- embedded mode panels ----
        self.lsPage = LightSheetMulticolorWidget(self._options)
        self.plsrPage = TriggerScopePLSRMulticolorWidget(self._options)
        self._pages = {
            self.MODE_LIGHTSHEET: self.lsPage,
            self.MODE_PLSR: self.plsrPage,
        }
        for page in self._pages.values():
            page.hideChrome()

        # ---- shared chrome ----
        self.scannerLabel = QtWidgets.QLabel('TriggerScope Scan')
        self.scannerLabel.setStyleSheet('font-size: 14pt; font-weight: bold')

        modeLabel = QtWidgets.QLabel('Scan type')
        self.modeCombo = guitools.BetterComboBox(allowScrollChanges=False)
        for mode in self._MODE_ORDER:
            self.modeCombo.addItem(self._MODE_LABELS[mode])

        self.loadScanBtn = guitools.BetterPushButton('Load Scan')
        self.saveScanBtn = guitools.BetterPushButton('Save Scan')
        self.scanButton = guitools.BetterPushButton('Run Scan')

        autoStartRecLabel = QtWidgets.QLabel('Auto-start REC')
        autoStartRecLabel.setAlignment(QtCore.Qt.AlignRight)
        self.autoStartRec = QtWidgets.QCheckBox()
        _autoStartTip = ('When checked, a recording is started automatically '
                         'as the scan begins.')
        autoStartRecLabel.setToolTip(_autoStartTip)
        self.autoStartRec.setToolTip(_autoStartTip)
        autoStopRecLabel = QtWidgets.QLabel('Auto-stop REC')
        autoStopRecLabel.setAlignment(QtCore.Qt.AlignRight)
        self.autoStopRec = QtWidgets.QCheckBox()
        _autoStopTip = ('When checked, the recording is stopped automatically '
                        'when the scan finishes.')
        autoStopRecLabel.setToolTip(_autoStopTip)
        self.autoStopRec.setToolTip(_autoStopTip)

        self.stack = QtWidgets.QStackedWidget()
        for mode in self._MODE_ORDER:
            self.stack.addWidget(self._pages[mode])

        # ---- layout ----
        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        r = 0
        layout.addWidget(self.scannerLabel, r, 0, 1, 2)
        r += 1
        layout.addWidget(modeLabel, r, 0)
        layout.addWidget(self.modeCombo, r, 1, 1, 3)
        r += 1
        layout.addWidget(self.loadScanBtn, r, 0)
        layout.addWidget(self.saveScanBtn, r, 1)
        layout.addWidget(autoStartRecLabel, r, 2)
        layout.addWidget(self.autoStartRec, r, 3)
        r += 1
        layout.addWidget(self.scanButton, r, 1)
        layout.addWidget(autoStopRecLabel, r, 2)
        layout.addWidget(self.autoStopRec, r, 3)
        r += 1
        layout.addWidget(self.stack, r, 0, 1, 4)
        layout.setRowStretch(r, 1)

        # ---- signals ----
        self.lsPage.sigParameterChanged.connect(self.sigParameterChanged)
        self.plsrPage.sigParameterChanged.connect(self.sigParameterChanged)
        self.saveScanBtn.clicked.connect(self.sigSaveScanClicked)
        self.loadScanBtn.clicked.connect(self.sigLoadScanClicked)
        self.scanButton.clicked.connect(self.sigRunScanClicked)
        self.modeCombo.currentIndexChanged.connect(self._onModeChanged)

    # ------------------------------------------------------------------
    # Mode handling
    # ------------------------------------------------------------------

    def _onModeChanged(self, index):
        self.stack.setCurrentIndex(index)
        self.sigModeChanged.emit()

    def currentMode(self):
        return self._MODE_ORDER[self.modeCombo.currentIndex()]

    def setMode(self, mode):
        self.modeCombo.setCurrentIndex(self._MODE_ORDER.index(mode))

    def setCurrentMode(self, mode):
        """Compatibility alias used by scan state restore code."""
        self.setMode(mode)

    # ------------------------------------------------------------------
    # Shared chrome helpers (mirror the cousin widgets' API)
    # ------------------------------------------------------------------

    def setScanButtonChecked(self, checked):
        self.scanButton.setEnabled(not checked)
        self.scanButton.setCheckable(checked)
        self.scanButton.setChecked(checked)

    def setRepeatEnabled(self, enabled):
        pass


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
