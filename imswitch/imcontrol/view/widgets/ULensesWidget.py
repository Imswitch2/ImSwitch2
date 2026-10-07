from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol.view import guitools
from .basewidgets import NapariHybridWidget


class ULensesWidget(NapariHybridWidget):
    """Alignment widget that shows a localized uLens grid over the image."""

    sigULensesClicked = QtCore.Signal()
    sigUShowLensesChanged = QtCore.Signal(bool)  # (enabled)
    sigLocalizeBtnClicked = QtCore.Signal()
    sigLocalizationMethodChanged = QtCore.Signal(str)

    def __post_init__(self):
        # Graphical Elements
        self.localizationMethodCombo = QtWidgets.QComboBox()
        self.localizationMethodCombo.addItem('Legacy (axis-aligned)', 'legacy')
        self.localizationMethodCombo.addItem('Grid localization (experimental)', 'grid')

        self.ulensesButton = guitools.BetterPushButton('uLenses')
        self.ulensesCheck = QtWidgets.QCheckBox('Show uLenses')
        self.localizeBtn = guitools.BetterPushButton('Auto Localize')
        self.xEdit = QtWidgets.QLineEdit('0')
        self.yEdit = QtWidgets.QLineEdit('0')
        self.pxEdit = QtWidgets.QLineEdit('157.5')
        self.upxEdit = QtWidgets.QLineEdit('1182')
        self.upyEdit = QtWidgets.QLineEdit('1182')

        self.gridSizeLabel = QtWidgets.QLabel('Grid size')
        self.gridSizeCombo = QtWidgets.QComboBox()
        self.gridSizeCombo.addItem('Auto', 'auto')
        self.gridSizeCombo.addItem('Manual', 'manual')
        self.nColsLabel = QtWidgets.QLabel('Nx')
        self.nColsSpin = QtWidgets.QSpinBox()
        self.nColsSpin.setRange(2, 999)
        self.nColsSpin.setValue(6)
        self.nRowsLabel = QtWidgets.QLabel('Ny')
        self.nRowsSpin = QtWidgets.QSpinBox()
        self.nRowsSpin.setRange(2, 999)
        self.nRowsSpin.setValue(5)

        self.statusLabel = QtWidgets.QLabel('')
        self.statusLabel.setWordWrap(True)

        # Vispy visual to render in napari. Registered with the image viewbox by
        # ULensesController via sigAddItemToVb (like the other overlays) so it
        # receives the detector pixel-scale broadcast and stays aligned to the
        # scaled image -- rather than self-adding via addItemToViewer, which
        # bypassed that registration and left the grid unscaled.
        self.ulensesPlot = naparitools.VispyScatterVisual(color='red', symbol='x')
        self.ulensesPlot.hide()

        # Add elements to GridLayout
        ulensesLayout = QtWidgets.QGridLayout()
        self.setLayout(ulensesLayout)
        ulensesLayout.addWidget(QtWidgets.QLabel('Localization method'), 0, 0)
        ulensesLayout.addWidget(self.localizationMethodCombo, 0, 1, 1, 3)
        ulensesLayout.addWidget(QtWidgets.QLabel('Pixel Size (nm)'), 1, 0)
        ulensesLayout.addWidget(self.pxEdit, 1, 1)
        ulensesLayout.addWidget(QtWidgets.QLabel('Periodicity X'), 2, 0)
        ulensesLayout.addWidget(self.upxEdit, 2, 1)
        ulensesLayout.addWidget(QtWidgets.QLabel('Periodicity Y'), 3, 0)
        ulensesLayout.addWidget(self.upyEdit, 3, 1)
        ulensesLayout.addWidget(QtWidgets.QLabel('X offset'), 4, 0)
        ulensesLayout.addWidget(self.xEdit, 4, 1)
        ulensesLayout.addWidget(QtWidgets.QLabel('Y offset'), 5, 0)
        ulensesLayout.addWidget(self.yEdit, 5, 1)

        ulensesLayout.addWidget(self.gridSizeLabel, 6, 0)
        ulensesLayout.addWidget(self.gridSizeCombo, 6, 1)
        ulensesLayout.addWidget(self.nColsLabel, 6, 2)
        ulensesLayout.addWidget(self.nColsSpin, 6, 3)
        ulensesLayout.addWidget(self.nRowsLabel, 7, 2)
        ulensesLayout.addWidget(self.nRowsSpin, 7, 3)

        ulensesLayout.addWidget(self.ulensesButton, 8, 0)
        ulensesLayout.addWidget(self.ulensesCheck, 8, 1)
        ulensesLayout.addWidget(self.localizeBtn, 8, 2, 1, 2)
        ulensesLayout.addWidget(self.statusLabel, 9, 0, 1, 4)

        # Connect signals
        self.ulensesButton.clicked.connect(self.sigULensesClicked)
        self.ulensesCheck.toggled.connect(self.sigUShowLensesChanged)
        self.localizeBtn.clicked.connect(self.sigLocalizeBtnClicked)
        self.localizationMethodCombo.currentIndexChanged.connect(
            self._localizationMethodChanged
        )
        self.gridSizeCombo.currentIndexChanged.connect(self._updateLocalizationControls)
        self._updateLocalizationControls()

    def _localizationMethodChanged(self):
        self._updateLocalizationControls()
        self.sigLocalizationMethodChanged.emit(self.getLocalizationMethod())

    def _updateLocalizationControls(self):
        is_grid = self.getLocalizationMethod() == 'grid'
        manual_grid = is_grid and self.getGridSizeMode() == 'manual'

        # The legacy offsets cannot represent arbitrary grid tilt/stagger. Keep
        # their values intact so switching back to Legacy restores its state.
        self.xEdit.setEnabled(not is_grid)
        self.yEdit.setEnabled(not is_grid)
        offset_tip = (
            'Axis-aligned offsets are used only by the legacy localization method.'
            if is_grid else ''
        )
        self.xEdit.setToolTip(offset_tip)
        self.yEdit.setToolTip(offset_tip)

        for widget in (self.gridSizeLabel, self.gridSizeCombo,
                       self.nColsLabel, self.nColsSpin,
                       self.nRowsLabel, self.nRowsSpin):
            widget.setVisible(is_grid)
        self.nColsSpin.setEnabled(manual_grid)
        self.nRowsSpin.setEnabled(manual_grid)

    def setParameters(self, x=None, y=None, px=None, upx=None, upy=None):
        """Programmatically update uLens alignment parameters."""
        if x is not None:
            self.xEdit.setText(str(x))
        if y is not None:
            self.yEdit.setText(str(y))
        if px is not None:
            self.pxEdit.setText(str(px))
        if upx is not None:
            self.upxEdit.setText(str(upx))
        if upy is not None:
            self.upyEdit.setText(str(upy))

    def getParameters(self):
        """Return X/Y offset, pixel size and physical X/Y periodicity."""
        return (float(self.xEdit.text()),
                float(self.yEdit.text()),
                float(self.pxEdit.text()),
                float(self.upxEdit.text()),
                float(self.upyEdit.text()))

    def getLocalizationMethod(self):
        return str(self.localizationMethodCombo.currentData())

    def getGridSizeMode(self):
        return str(self.gridSizeCombo.currentData())

    def getGridSize(self):
        if self.getGridSizeMode() != 'manual':
            return None, None
        return int(self.nColsSpin.value()), int(self.nRowsSpin.value())

    def setPixelSizeFromDetector(self, pixel_size_nm, detector_name, source=None):
        """Display the authoritative current-detector pixel size."""
        self.pxEdit.setText(f'{float(pixel_size_nm):.6g}')
        self.pxEdit.setReadOnly(True)
        source_text = f' ({source})' if source else ''
        self.pxEdit.setToolTip(
            f'Automatically read from detector "{detector_name}"{source_text}. '
            'Camera binning is included. Change the detector pixel-size setting '
            'to update this value.'
        )

    def setPixelSizeManualFallback(self, reason=''):
        """Allow manual entry when current-detector pixel size is unavailable."""
        self.pxEdit.setReadOnly(False)
        text = 'Detector pixel size unavailable; enter the effective image pixel size in nm.'
        if reason:
            text += f' {reason}'
        self.pxEdit.setToolTip(text)

    def setLocalizationStatus(self, text, *, error=False):
        self.statusLabel.setText(str(text))
        self.statusLabel.setStyleSheet('color: #a33;' if error else 'color: #666;')
        self.statusLabel.setToolTip(str(text))

    def getPlotGraphicsItem(self):
        return self.ulensesPlot

    def setData(self, x, y):
        """Updates plot with new parameters."""
        self.ulensesPlot.setData(x=x, y=y)

    def setULensesVisible(self, visible):
        """Updates visibility of plot."""
        self.ulensesPlot.setVisible(visible)


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
