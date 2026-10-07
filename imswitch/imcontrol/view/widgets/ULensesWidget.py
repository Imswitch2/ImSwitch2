from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcommon.algorithms.grid_localization import (
    LOCALIZATION_DEFAULTS, LOCALIZATION_PARAMETER_INFO,
)
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
        self.gridSettingsButton = guitools.BetterPushButton('Grid settings…')
        self._gridLocalizationSettings = dict(LOCALIZATION_DEFAULTS)
        self._gridLocalizationSettings.update({
            'stagger_mode': 'auto',
            'stagger_axis': 'auto',
            'stagger_fraction': 0.5,
        })
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
        ulensesLayout.addWidget(self.gridSettingsButton, 9, 2, 1, 2)
        ulensesLayout.addWidget(self.statusLabel, 10, 0, 1, 4)

        # Connect signals
        self.ulensesButton.clicked.connect(self.sigULensesClicked)
        self.ulensesCheck.toggled.connect(self.sigUShowLensesChanged)
        self.localizeBtn.clicked.connect(self.sigLocalizeBtnClicked)
        self.gridSettingsButton.clicked.connect(self._editGridLocalizationSettings)
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
                       self.nRowsLabel, self.nRowsSpin,
                       self.gridSettingsButton):
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

    def getGridLocalizationSettings(self):
        """Return the SLMcore-compatible numerical localization settings."""
        return dict(self._gridLocalizationSettings)

    def _editGridLocalizationSettings(self):
        """Edit grid-localizer parameters without expanding the main widget."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('uLenses grid localization settings')
        dialog.resize(560, 680)
        root = QtWidgets.QVBoxLayout(dialog)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        body_layout = QtWidgets.QVBoxLayout(body)
        editors = {}

        specs = {
            'stagger_mode': {
                'label': 'Stagger source', 'choices': ('auto', 'none', 'manual'),
                'tooltip': 'Auto infers stagger from the image; None forces zero; Manual imposes the value below.',
                'group': 'Grid geometry',
            },
            'stagger_axis': {
                'label': 'Stagger axis', 'choices': ('auto', 'rows', 'columns'),
                'tooltip': 'Rows is the canonical SLMcore representation; Columns uses the same backend on a transposed image.',
                'group': 'Grid geometry',
            },
            'stagger_fraction': {
                'label': 'Stagger', 'minimum': 0.0, 'maximum': 1.0, 'step': 0.05, 'decimals': 3,
                'tooltip': 'Manual alternating shift as a fraction of the corresponding lattice period.',
                'group': 'Grid geometry',
            },
        }
        for name, info in LOCALIZATION_PARAMETER_INFO.items():
            specs[name] = dict(info)

        groups = {}
        advanced_container = QtWidgets.QWidget()
        advanced_layout = QtWidgets.QVBoxLayout(advanced_container)
        advanced_layout.setContentsMargins(0, 0, 0, 0)

        def group_form(group_name, advanced=False):
            key = (bool(advanced), str(group_name))
            if key not in groups:
                box = QtWidgets.QGroupBox(str(group_name))
                form = QtWidgets.QFormLayout(box)
                (advanced_layout if advanced else body_layout).addWidget(box)
                groups[key] = form
            return groups[key]

        for name, info in specs.items():
            advanced = bool(info.get('advanced', False))
            group_name = str(info.get('group') or 'Localization')
            form = group_form(group_name, advanced=advanced)
            value = self._gridLocalizationSettings[name]
            choices = info.get('choices')
            if choices is not None:
                editor = QtWidgets.QComboBox()
                for choice in choices:
                    editor.addItem(str(choice).replace('_', ' ').title(), choice)
                index = editor.findData(value)
                if index >= 0:
                    editor.setCurrentIndex(index)
            elif isinstance(value, int) and not isinstance(value, bool):
                editor = QtWidgets.QSpinBox()
                editor.setRange(int(info.get('minimum', -1000000)), int(info.get('maximum', 1000000)))
                editor.setSingleStep(int(info.get('step', 1)))
                editor.setValue(int(value))
            else:
                editor = QtWidgets.QDoubleSpinBox()
                editor.setRange(float(info.get('minimum', -1e9)), float(info.get('maximum', 1e9)))
                editor.setSingleStep(float(info.get('step', 0.1)))
                editor.setDecimals(int(info.get('decimals', 3)))
                editor.setValue(float(value))
            editor.setToolTip(str(info.get('tooltip', '')))
            label = QtWidgets.QLabel(str(info.get('label', name)))
            label.setToolTip(editor.toolTip())
            form.addRow(label, editor)
            editors[name] = editor

        show_advanced = QtWidgets.QCheckBox('Show advanced parameters')
        show_advanced.setChecked(False)
        advanced_container.setVisible(False)
        show_advanced.toggled.connect(advanced_container.setVisible)
        body_layout.addWidget(show_advanced)
        body_layout.addWidget(advanced_container)

        def update_stagger_controls():
            mode = editors['stagger_mode'].currentData()
            manual = mode == 'manual'
            editors['stagger_axis'].setEnabled(mode != 'none')
            editors['stagger_fraction'].setEnabled(manual)
            if manual and editors['stagger_axis'].currentData() == 'auto':
                index = editors['stagger_axis'].findData('rows')
                if index >= 0:
                    editors['stagger_axis'].setCurrentIndex(index)

        editors['stagger_mode'].currentIndexChanged.connect(update_stagger_controls)
        update_stagger_controls()

        body_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        exec_dialog = dialog.exec_ if hasattr(dialog, 'exec_') else dialog.exec
        if exec_dialog() != QtWidgets.QDialog.Accepted:
            return
        values = {}
        for name, editor in editors.items():
            if isinstance(editor, QtWidgets.QComboBox):
                values[name] = editor.currentData()
            elif isinstance(editor, QtWidgets.QSpinBox):
                values[name] = int(editor.value())
            else:
                values[name] = float(editor.value())
        self._gridLocalizationSettings.update(values)

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
