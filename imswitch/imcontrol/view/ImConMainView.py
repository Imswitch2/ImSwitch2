from dataclasses import dataclass
from typing import Any, Dict

from pyqtgraph.dockarea import Dock, DockArea
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.view import PickDatasetsDialog
from . import widgets
from .PickSetupDialog import PickSetupDialog


class ImConMainView(QtWidgets.QMainWindow):
    sigLoadParamsFromHDF5 = QtCore.Signal()
    sigPickSetup = QtCore.Signal()
    sigClosing = QtCore.Signal()
    sigSaveWidgetState = QtCore.Signal()
    sigLoadWidgetState = QtCore.Signal()
    sigOpenShortcutEditor = QtCore.Signal()

    def __init__(self, options, viewSetupInfo, *args, **kwargs):
        self.__logger = initLogger(self)
        self.__logger.debug('Initializing')
        
        super().__init__(*args, **kwargs)

        self.pickSetupDialog = PickSetupDialog(self)
        self.pickDatasetsDialog = PickDatasetsDialog(self, allowMultiSelect=False)

        self.viewSetupInfo = viewSetupInfo

        # Widget factory
        self.factory = widgets.WidgetFactory(options)
        self.docks = {}
        self.widgets = {}

        # Menu Bar
        menuBar = self.menuBar()
        file = menuBar.addMenu('&File')
        tools = menuBar.addMenu('&Tools')
        self.shortcutsMenu = menuBar.addMenu('&Shortcuts')

        self.loadParamsAction = QtWidgets.QAction('Load parameters from saved HDF5 file…', self)
        self.loadParamsAction.triggered.connect(self.sigLoadParamsFromHDF5)
        file.addAction(self.loadParamsAction)
        
        file.addSeparator()
        
        self.saveWidgetStateAction = QtWidgets.QAction('Save Widget States…', self)
        self.saveWidgetStateAction.triggered.connect(self.sigSaveWidgetState)
        file.addAction(self.saveWidgetStateAction)
        
        self.loadWidgetStateAction = QtWidgets.QAction('Load Widget States…', self)
        self.loadWidgetStateAction.triggered.connect(self.sigLoadWidgetState)
        file.addAction(self.loadWidgetStateAction)

        self.pickSetupAction = QtWidgets.QAction('Pick hardware setup…', self)
        self.pickSetupAction.triggered.connect(self.sigPickSetup)
        tools.addAction(self.pickSetupAction)
        
        # Add Configure Shortcuts action to Shortcuts menu
        self.configureShortcutsAction = QtWidgets.QAction('Configure Shortcuts…', self)
        self.configureShortcutsAction.triggered.connect(self.sigOpenShortcutEditor)
        self.shortcutsMenu.addAction(self.configureShortcutsAction)
        self.shortcutsMenu.addSeparator()

        # Window
        self.setWindowTitle('ImSwitch')

        # Hide the QMainWindow status bar — it is unused and wastes vertical
        # space when this window is embedded inside MultiModuleWindow's tab widget.
        self.statusBar().hide()

        self.cwidget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.cwidget.setLayout(layout)
        self.setCentralWidget(self.cwidget)

        # Dock area — use layout from setup JSON when present, else the defaults.
        if viewSetupInfo.widgetLayout is None:
            rightDockInfos = _DEFAULT_RIGHT_DOCK_INFOS
            leftDockInfos = _DEFAULT_LEFT_DOCK_INFOS
        else:
            rightDockInfos = dict(_build_dock_infos_from_layout(viewSetupInfo.widgetLayout.right))
            leftDockInfos = dict(_build_dock_infos_from_layout(viewSetupInfo.widgetLayout.left))
            # Widgets not mentioned in the explicit layout fall back to their default
            # panel placement so nothing enabled is silently dropped.
            placed = set(rightDockInfos) | set(leftDockInfos)
            for key, info in _DEFAULT_RIGHT_DOCK_INFOS.items():
                if key not in placed:
                    rightDockInfos[key] = info
            for key, info in _DEFAULT_LEFT_DOCK_INFOS.items():
                if key not in placed:
                    leftDockInfos[key] = info

        otherDockKeys = ['Image']
        allDockKeys = list(rightDockInfos.keys()) + list(leftDockInfos.keys()) + otherDockKeys

        self.dockArea = DockArea()
        enabledDockKeys = self.viewSetupInfo.availableWidgets
        if enabledDockKeys is False:
            enabledDockKeys = []
        elif enabledDockKeys is True:
            enabledDockKeys = allDockKeys

        if 'Image' in enabledDockKeys:
            self.docks['Image'] = Dock('Image Display', size=(1, 1))
            self.widgets['Image'] = self.factory.createWidget(widgets.ImageWidget)
            self.docks['Image'].addWidget(self.widgets['Image'])
            self.factory.setArgument('napariViewer', self.widgets['Image'].napariViewer)

        rightDocks = self._addDocks(
            {k: v for k, v in rightDockInfos.items() if k in enabledDockKeys},
            self.dockArea, 'right'
        )

        if 'Image' in enabledDockKeys:
            self.dockArea.addDock(self.docks['Image'], 'left')

        self._addDocks(
            {k: v for k, v in leftDockInfos.items() if k in enabledDockKeys},
            self.dockArea, 'left'
        )

        # Add dock area to layout
        layout.addWidget(self.dockArea)

        # Maximize window
        self.showMaximized()
        self.hide()  # Minimize time the window is displayed while loading multi module window

        # Adjust dock sizes (the window has to be maximized first for this to work properly)
        if 'Settings' in self.docks:
            self.docks['Settings'].setStretch(1, 5)
            self.docks['Settings'].container().setStretch(3, 1)
        if len(rightDocks) > 0:
            rightDocks[-1].setStretch(1, 5)
        if 'Image' in self.docks:
            self.docks['Image'].setStretch(16, 1)

        # Reset to a compact size so this widget's size hint doesn't push
        # MultiModuleWindow below the screen bottom when embedded.
        # setStretch ratios are proportional so they survive the resize.
        self.resize(800, 600)

    def addShortcuts(self, shortcuts):
        """Legacy method - shortcuts are now managed by ShortcutManager.
        
        This method is retained for backward compatibility but is no longer used.
        The ShortcutManager handles all shortcut binding and menu population.
        """
        pass
        
    def updateMenuActionShortcuts(self, effectiveBindings: Dict[str, str]) -> None:
        """Update File menu actions to display their effective shortcut keys.
        
        Updates the QAction text to include the shortcut hint (\tKey) for display.
        The actual keyboard binding is managed by ShortcutManager to avoid conflicts.
        
        Args:
            effectiveBindings: Dict mapping actionId to effective key sequence
        """
        actionInfo = {
            'app.loadParams': (self.loadParamsAction, 'Load parameters from saved HDF5 file…'),
            'app.saveWidgetStates': (self.saveWidgetStateAction, 'Save Widget States…'),
            'app.loadWidgetStates': (self.loadWidgetStateAction, 'Load Widget States…'),
        }
        
        for actionId, (qAction, baseText) in actionInfo.items():
            keySeq = effectiveBindings.get(actionId)
            if keySeq:
                # Format: "Menu Text\tShortcut" - Qt displays shortcut on the right
                if isinstance(keySeq, list):
                    keySeq = keySeq[0]  # Use first key if multiple
                qAction.setText(f"{baseText}\t{keySeq}")
            else:
                # Action is disabled or has no binding
                qAction.setText(baseText)

    def showPickSetupDialogBlocking(self):
        result = self.pickSetupDialog.exec_()
        return result == QtWidgets.QDialog.Accepted

    def showPickDatasetsDialogBlocking(self):
        result = self.pickDatasetsDialog.exec_()
        return result == QtWidgets.QDialog.Accepted

    def getLayoutState(self) -> Dict[str, Any]:
        """Return passive GUI layout state for persistence."""
        return {
            'dock_area': self.dockArea.saveState(),
        }

    def setLayoutState(self, state: Dict[str, Any]) -> None:
        """Restore passive GUI layout state if it is compatible with this setup."""
        dockAreaState = state.get('dock_area')
        if dockAreaState is None:
            return

        try:
            self.dockArea.restoreState(dockAreaState, missing='ignore', extra='bottom')
        except Exception as e:
            self.__logger.warning(f'Failed to restore GUI dock layout: {e}')

    def closeEvent(self, event):
        self.sigClosing.emit()
        event.accept()

    def _addDocks(self, dockInfoDict, dockArea, position):
        docks = []

        prevDock = None
        prevDockYPosition = -1
        for widgetKey, dockInfo in dockInfoDict.items():
            self.widgets[widgetKey] = self.factory.createWidget(
                getattr(widgets, f'{widgetKey}Widget')
                if widgetKey != 'Scan' else
                getattr(widgets, f'{widgetKey}Widget{self.viewSetupInfo.scan.scanWidgetType}')
            )
            self.docks[widgetKey] = Dock(dockInfo.name, size=(1, 1))
            self.docks[widgetKey].addWidget(self.widgets[widgetKey])
            if prevDock is None:
                dockArea.addDock(self.docks[widgetKey], position)
            elif dockInfo.yPosition > prevDockYPosition:
                dockArea.addDock(self.docks[widgetKey], 'bottom', prevDock)
            else:
                dockArea.addDock(self.docks[widgetKey], 'above', prevDock)
            prevDock = self.docks[widgetKey]
            prevDockYPosition = dockInfo.yPosition
            docks.append(prevDock)

        return docks


@dataclass
class _DockInfo:
    name: str
    yPosition: int


# Display names for every known widget key.  Used when widgetLayout is present
# in the setup JSON so the dock title doesn't have to be specified separately.
# Falls back to the raw key name for unknown/future widgets.
_DOCK_DISPLAY_NAMES = {
    'Autofocus': 'Autofocus',
    'FocusLock': 'Focus Lock',
    'EtSTED': 'EtSTED',
    'EtMonalisa': 'EtMonalisa',
    'Positioner': 'Positioner',
    'Laser': 'Laser Control',
    'Rotator': 'Rotator',
    'MotCorr': 'Motorized Correction Collar',
    'SLMs': 'SLMs',
    'SLM': 'SLM',
    'Scan': 'Scan',
    'RotationScan': 'RotationScan',
    'BeadRec': 'Bead Rec',
    'AlignmentLine': 'Alignment Tool',
    'AlignAverage': 'Axial Alignment Tool',
    'AlignXY': 'Rotational Alignment Tool',
    'ULenses': 'uLenses Tool',
    'FFT': 'FFT Tool',
    'FLIMHist': 'FLIM Lifetime Histogram',
    'FlipMirror': 'Flip Mirrors',
    'Watcher': 'File Watcher',
    'Tiling': 'Tiling',
    'BFTimelapse': 'BFTimelapse',
    'LightSheetMulticolor': 'Light-Sheet Multicolor',
    'WellPlate': 'Well Plate',
    'Et': 'Et',
    'EtSnouty': 'EtSnouty',
    'SetupStatus': 'Setup Status',
    'SetupModes': 'Setup Modes',
    'TriggerScopeRaster': 'TriggerScope Raster Scan',
    'TriggerScopePLSR': 'TriggerScope pLS-RESOLFT',
    'TriggerScopeGalvoDetection': 'TriggerScope Galvo Detection',
    'TriggerScopePLSRMulticolor': 'TriggerScope pLS-RESOLFT Multicolor',
    'TriggerScopeScan': 'TriggerScope Scan',
    'TriggerScopeLSXYR': 'TriggerScope LS-XY-RESOLFT',
    'LeicaStand': 'Stand',
    'Settings': 'Detector Settings',
    'View': 'Image Controls',
    'ViewerTools': 'Viewer Tools',
    'LineProfile': 'Line Profile',
    'Recording': 'Recording',
    'Console': 'Console',
    'Image': 'Image Display',
}

# Default dock layout, mirroring the previous hard-coded behaviour exactly.
# Used whenever widgetLayout is absent from the setup JSON.
_DEFAULT_RIGHT_DOCK_INFOS = {
    'Autofocus':     _DockInfo(name='Autofocus',                     yPosition=0),
    'FocusLock':     _DockInfo(name='Focus Lock',                    yPosition=0),
    'EtSTED':        _DockInfo(name='EtSTED',                        yPosition=0),
    'EtMonalisa':    _DockInfo(name='EtMonalisa',                    yPosition=0),
    'Positioner':    _DockInfo(name='Positioner',                    yPosition=0),
    'BSC203':        _DockInfo(name='BSC203 Stage',                  yPosition=0),
    'Laser':         _DockInfo(name='Laser Control',                 yPosition=0),
    'Rotator':       _DockInfo(name='Rotator',                       yPosition=1),
    'MotCorr':       _DockInfo(name='Motorized Correction Collar',   yPosition=1),
    'SLMs':          _DockInfo(name='SLMs',                          yPosition=2),
    'Scan':          _DockInfo(name='Scan',                          yPosition=2),
    'RotationScan':  _DockInfo(name='RotationScan',                  yPosition=2),
    'BeadRec':       _DockInfo(name='Bead Rec',                      yPosition=3),
    'AlignmentLine': _DockInfo(name='Alignment Tool',                yPosition=3),
    'AlignAverage':  _DockInfo(name='Axial Alignment Tool',          yPosition=3),
    'AlignXY':       _DockInfo(name='Rotational Alignment Tool',     yPosition=3),
    'ULenses':       _DockInfo(name='uLenses Tool',                  yPosition=3),
    'FFT':           _DockInfo(name='FFT Tool',                      yPosition=3),
    'FLIMHist':      _DockInfo(name='FLIM Lifetime Histogram',       yPosition=3),
    'FlipMirror':    _DockInfo(name='Flip Mirrors',                  yPosition=0),
    'Watcher':               _DockInfo(name='File Watcher',           yPosition=3),
    'Tiling':               _DockInfo(name='Tiling',                 yPosition=3),
    'LightSheetMulticolor': _DockInfo(name='Light-Sheet Multicolor', yPosition=3),
    'WellPlate':            _DockInfo(name='Well Plate',             yPosition=3),
    'Et':                          _DockInfo(name='Et',                                    yPosition=3),
    'EtSnouty':                    _DockInfo(name='EtSnouty',                              yPosition=0),
    'SetupStatus':                 _DockInfo(name='Setup Status',                          yPosition=0),
    'SetupModes':                  _DockInfo(name='Setup Modes',                           yPosition=0),
    'TriggerScopeRaster':          _DockInfo(name='TriggerScope Raster Scan',               yPosition=3),
    'TriggerScopePLSR':            _DockInfo(name='TriggerScope pLS-RESOLFT',               yPosition=3),
    'TriggerScopeGalvoDetection':  _DockInfo(name='TriggerScope Galvo Detection',           yPosition=3),
    'TriggerScopePLSRMulticolor':  _DockInfo(name='TriggerScope pLS-RESOLFT Multicolor',    yPosition=3),
    'TriggerScopeScan':            _DockInfo(name='TriggerScope Scan',                       yPosition=3),
    'TriggerScopeLSXYR':           _DockInfo(name='TriggerScope LS-XY-RESOLFT',             yPosition=3),
}
_DEFAULT_LEFT_DOCK_INFOS = {
    'LeicaStand':  _DockInfo(name='Stand',              yPosition=0),
    'Settings':    _DockInfo(name='Detector Settings',  yPosition=1),
    'View':        _DockInfo(name='Image Controls',     yPosition=2),
    'ViewerTools': _DockInfo(name='Viewer Tools',       yPosition=3),
    'LineProfile': _DockInfo(name='Line Profile',       yPosition=4),
    'Recording':   _DockInfo(name='Recording',          yPosition=5),
    'Console':     _DockInfo(name='Console',            yPosition=6),
}


def _build_dock_infos_from_layout(tab_groups):
    """Convert a list-of-tab-groups into a {key: _DockInfo} dict.

    Each inner list in *tab_groups* is a tab group (same yPosition).
    Successive inner lists get increasing yPositions so they stack vertically.
    """
    result = {}
    for y_pos, group in enumerate(tab_groups):
        for key in group:
            result[key] = _DockInfo(name=_DOCK_DISPLAY_NAMES.get(key, key), yPosition=y_pos)
    return result


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
