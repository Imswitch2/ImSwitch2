from dataclasses import dataclass
from typing import Any, Dict

from pyqtgraph.dockarea import Dock, DockArea
from pyqtgraph.dockarea.Container import HContainer, VContainer
from pyqtgraph.dockarea.DockArea import TempAreaWindow
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.view import PickDatasetsDialog
from . import widgets
from .PickSetupDialog import PickSetupDialog
from .SessionNotesDialog import SessionNotesDialog
from .MemoryLimitsDialog import MemoryLimitsDialog


class _ResizeOnlyWhatMoved:
    """ Mixin: a descendant's stretch change must not re-divide our own panes.

    pyqtgraph turns stretch factors into splitter sizes in
    ``Container.updateStretch()``, and every container between the dock and
    the top runs it whenever a *descendant's* stretch changes -- which any
    dock move does, because a column's stretch is the sum of its docks'.  So
    dragging a dock inside the right-hand column re-divided the whole window
    width from stretch factors alone and threw away every splitter the user
    had dragged, including ones nowhere near the dock they moved.

    The stretch value still has to travel upward (the containers above need it
    for the next real layout), so this suppresses only the resize.  Containers
    whose own children changed reach ``updateStretch()`` through
    ``insert()`` / ``childEvent_()`` instead, and still lay themselves out --
    room has to be made for a dock that just arrived.
    """

    _suppressResize = False

    def childStretchChanged(self):
        self._suppressResize = True
        try:
            self.updateStretch()
        finally:
            self._suppressResize = False

    def setSizes(self, sizes):
        if self._suppressResize:
            return
        super().setSizes(sizes)


class _StableVContainer(_ResizeOnlyWhatMoved, VContainer):
    pass


class _StableHContainer(_ResizeOnlyWhatMoved, HContainer):
    pass


class _LayoutPreservingDockArea(DockArea):
    """ A DockArea that only re-lays-out the part of the layout you changed. """

    def makeContainer(self, typ):
        if typ == 'vertical':
            return _StableVContainer(self)
        if typ == 'horizontal':
            return _StableHContainer(self)
        return super().makeContainer(typ)

    def addTempArea(self):
        """ Float docks into an area that preserves layout the same way.

        pyqtgraph hard-codes ``DockArea`` here, so without this the window a
        dock is floated into behaves like stock pyqtgraph -- and, more to the
        point, the area the dock *left* is resized by that area's ``addDock``
        rather than this class's.
        """
        if self.home is not None:
            return self.home.addTempArea()

        area = type(self)(temporary=True, home=self)
        self.tempAreas.append(area)
        window = TempAreaWindow(area)
        area.win = window
        window.show()
        return area

    def addDock(self, dock=None, position='bottom', relativeTo=None, **kwds):
        """ Add or move a dock without resizing the parts of the layout it left alone.

        Suppressing the stretch-change resize (see ``_ResizeOnlyWhatMoved``)
        is not enough on its own, because rearranging docks *restructures* the
        containers above them.  Tabbing the two docks of a column together,
        for instance, leaves that column holding a single tab container, so
        pyqtgraph dissolves the column and puts the tab container in its place
        -- a genuine child change for the splitter that holds the columns,
        which then re-divided the whole window width and undid the widths the
        user had dragged on the *other* side of the window.

        A container that ends up with the same number of children as it
        started with did not really gain or lose a pane, whatever happened
        inside it, so it keeps the sizes it had.  One that did (a new column
        was split off, or the last dock left one) is laid out by pyqtgraph as
        before -- the space has to come from somewhere.

        The area the dock is *leaving* gets the same protection.  Whoever
        gains a dock is the one running, so floating a dock out (pyqtgraph
        runs this on the new window's area) and dragging one back in are both
        covered from here, rather than needing every method that can move a
        dock to be wrapped.
        """
        areas = [self]
        source = getattr(dock, 'area', None)
        if source is not self and isinstance(source, _LayoutPreservingDockArea):
            areas.append(source)
        sizes = [(area, area._containerSizes()) for area in areas]
        try:
            return super().addDock(dock, position, relativeTo, **kwds)
        finally:
            for area, previous in sizes:
                area._restoreContainerSizes(previous)

    def _containerSizes(self):
        """ {container: sizes} for every splitter currently in the tree.

        A temporary area is torn down as its last dock leaves, so by the time
        this runs again its containers can be deleted C++ objects; touching
        one raises RuntimeError rather than AttributeError, which ``hasattr``
        does not catch.
        """
        sizes = {}

        def walk(container):
            if container is None:
                return
            if hasattr(container, 'sizes') and hasattr(container, 'count'):
                sizes[container] = container.sizes()
            if hasattr(container, 'count') and hasattr(container, 'widget'):
                for i in range(container.count()):
                    walk(container.widget(i))

        try:
            walk(self.topContainer)
        except RuntimeError:
            return {}
        return sizes

    def _restoreContainerSizes(self, sizes):
        # Walks the tree as it is *now*, so containers that were closed during
        # the move are never touched -- their Qt objects are already gone.
        for container in self._containerSizes():
            previous = sizes.get(container)
            if previous is not None and len(previous) == container.count():
                container.setSizes(previous)


class ImConMainView(QtWidgets.QMainWindow):
    sigLoadParamsFromHDF5 = QtCore.Signal()
    sigLoadParamsFromZarr = QtCore.Signal()
    sigPickSetup = QtCore.Signal()
    sigClosing = QtCore.Signal()
    sigSaveWidgetState = QtCore.Signal()
    sigLoadWidgetState = QtCore.Signal()
    sigOpenShortcutEditor = QtCore.Signal()
    sigOpenSessionNotes = QtCore.Signal()
    sigOpenConfigEditor = QtCore.Signal()
    sigOpenMemoryLimits = QtCore.Signal()
    # Emitted on show/hide (i.e. module tab switches in the multi-module
    # window) so the ShortcutManager only keeps the visible module's set live.
    sigModuleVisibilityChanged = QtCore.Signal(bool)

    # Set when a saved dock layout has been restored: its splitter sizes are
    # the user's own and must not be replaced by content-derived ones.
    _layoutRestored = False
    _initialDockLayoutPending = False

    def __init__(self, options, viewSetupInfo, *args, **kwargs):
        self.__logger = initLogger(self)
        self.__logger.debug('Initializing')
        
        super().__init__(*args, **kwargs)

        self.pickSetupDialog = PickSetupDialog(self)
        self.pickDatasetsDialog = PickDatasetsDialog(self, allowMultiSelect=False)
        self.sessionNotesDialog = SessionNotesDialog(self)
        self.memoryLimitsDialog = MemoryLimitsDialog(self)

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

        self.loadParamsZarrAction = QtWidgets.QAction('Load parameters from saved Zarr store…', self)
        self.loadParamsZarrAction.triggered.connect(self.sigLoadParamsFromZarr)
        file.addAction(self.loadParamsZarrAction)

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

        self.sessionNotesAction = QtWidgets.QAction('Session notes…', self)
        self.sessionNotesAction.setToolTip(
            'Free text attached to the metadata of every recording saved'
            ' during this session'
        )
        self.sessionNotesAction.triggered.connect(self.sigOpenSessionNotes)
        tools.addAction(self.sessionNotesAction)

        self.configEditorAction = QtWidgets.QAction('Edit hardware configuration…', self)
        self.configEditorAction.setToolTip(
            'Open the Config Studio on this microscope’s setup files.'
            ' Saved changes take effect when ImSwitch is restarted.'
        )
        self.configEditorAction.triggered.connect(self.sigOpenConfigEditor)
        tools.addAction(self.configEditorAction)

        self.memoryLimitsAction = QtWidgets.QAction('Memory limits…', self)
        self.memoryLimitsAction.setToolTip(
            'How much memory ImSwitch may use on this computer for recording'
            ' buffers and automatic ImProcess work. Applied when saved.'
        )
        self.memoryLimitsAction.triggered.connect(self.sigOpenMemoryLimits)
        tools.addAction(self.memoryLimitsAction)

        self.resetLayoutAction = QtWidgets.QAction('Reset panel layout', self)
        self.resetLayoutAction.setToolTip(
            'Put every panel back where this hardware setup puts it, at the'
            ' sizes its contents ask for.'
        )
        self.resetLayoutAction.triggered.connect(self.resetDockLayout)
        tools.addAction(self.resetLayoutAction)
        
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

        self.dockArea = _LayoutPreservingDockArea()
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

        self._addDocks(
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

        # The arrangement this setup file asks for, kept for Tools > Reset
        # panel layout.  Taken before anything has had a chance to move.
        self._defaultDockState = self.dockArea.saveState()

        # Maximize window
        self.showMaximized()
        self.hide()  # Minimize time the window is displayed while loading multi module window

        # First pass at the dock proportions.  The panels are still empty at
        # this point (their controllers populate them afterwards), so this is
        # only a starting point -- ImConMainController calls
        # applyContentAwareDockSizing() again once everything is built.
        self.applyContentAwareDockSizing()

        # Reset to a compact size so this widget's size hint doesn't push
        # MultiModuleWindow below the screen bottom when embedded.
        # setStretch ratios are proportional so they survive the resize.
        self.resize(800, 600)

        # showMaximized() above made this a top-level window for a moment, and
        # Qt writes a layout-derived *explicit* minimum onto a window when its
        # layout activates.  That minimum outlives the reparenting into
        # MultiModuleWindow's tab widget, where it is no longer recomputed --
        # it would pin the application window to whatever the panels happened
        # to need while they were still empty, and it takes precedence over
        # minimumSizeHint() below.
        self.setMinimumSize(0, 0)

    # ------------------------------------------------------------------
    # Dock sizing
    # ------------------------------------------------------------------

    def applyContentAwareDockSizing(self):
        """Give each dock a stretch factor that reflects how tall its panel is.

        pyqtgraph splits a container's space in proportion to its children's
        stretch factors, so the ``size=(1, 1)`` every dock is created with
        hands a one-row panel exactly as much height as a thirty-row one.
        What used to hide that was panels refusing to shrink: a splitter never
        goes below the sum of its children's minimum heights, so the layout
        was really being decided by minimums.  That is also what pushed the
        window past the bottom of the screen, and why dragging a dock (which
        makes pyqtgraph recompute every size from the stretch factors alone)
        rearranged everything else.

        Sizing from content instead makes both the startup layout and every
        later rearrangement land where it should, and lets the panels stay
        freely shrinkable -- see ``Widget.makeScrollable()``.
        """
        widestPanel = 1
        for widgetKey, dock in self.docks.items():
            widget = self.widgets.get(widgetKey)
            if widget is None or widgetKey == 'Image':
                continue

            hint = (widget.panelContentSizeHint()
                    if hasattr(widget, 'panelContentSizeHint') else widget.sizeHint())
            height = min(max(hint.height(), _MIN_DOCK_STRETCH), _MAX_DOCK_STRETCH)
            width = min(max(hint.width(), _MIN_DOCK_WIDTH_STRETCH),
                        _MAX_DOCK_WIDTH_STRETCH)
            widestPanel = max(widestPanel, width)
            # The splitter divides the column between whole docks, title bar
            # included, so a dock asking for exactly its panel's height opens
            # one title bar short of it -- which is enough to cut off the last
            # row of every panel at once.
            dock.setStretch(width, height + _dockTitleHeight(dock))

        if 'Image' in self.docks:
            # Stretch factors share one scale across the whole area, so the
            # viewer's width share has to be expressed against the panel
            # columns it sits next to rather than as a bare number.
            self.docks['Image'].setStretch(widestPanel * _IMAGE_WIDTH_MULTIPLE, 1)

    def resetDockLayout(self):
        """ Put the panels back where this hardware setup puts them.

        Dragging a dock makes pyqtgraph recompute every other dock's size, so
        a layout can end up somewhere nobody wanted it; this is the way back
        without restarting.
        """
        if getattr(self, '_defaultDockState', None) is None:
            return
        try:
            self.dockArea.restoreState(
                self._defaultDockState, missing='ignore', extra='bottom'
            )
        except Exception as e:
            self.__logger.warning(f'Failed to reset GUI dock layout: {e}')
            return
        self.applyContentAwareDockSizing()
        self._redistributeDockSpace()

    def scheduleInitialDockLayout(self):
        """ Ask for one more sizing pass the next time this window is shown.

        Panels are still empty while this view is being constructed, and a
        couple of them (the detector parameter trees) only report their real
        size once they have actually been on screen, so the proportions have
        to be settled after both have happened.  Skipped when a saved layout
        was restored: those splitter sizes are the user's own.
        """
        if self._layoutRestored:
            return
        if self.isVisible():
            QtCore.QTimer.singleShot(0, self._applyInitialDockLayout)
        else:
            self._initialDockLayoutPending = True

    def _applyInitialDockLayout(self):
        self.applyContentAwareDockSizing()
        self._redistributeDockSpace()

    def _redistributeDockSpace(self):
        """Re-run pyqtgraph's stretch-to-size pass at the window's real size.

        ``Container.updateStretch()`` turns stretch factors into splitter
        sizes using the container's height *at that moment*, and it runs while
        the docks are being added -- long before the window has been given the
        size it opens at.  Without this the proportions computed at startup
        are the ones for an 800x600 window.
        """
        def walk(container):
            # Depth first: a container's own stretch is the sum (or maximum) of
            # its children's, so the children have to be settled before the
            # parent divides its width between them.
            if container is None:
                return
            if hasattr(container, 'count') and hasattr(container, 'widget'):
                for i in range(container.count()):
                    child = container.widget(i)
                    if hasattr(child, 'updateStretch'):
                        walk(child)
            if hasattr(container, 'updateStretch'):
                container.updateStretch()

        walk(self.dockArea.topContainer)

    def minimumSizeHint(self):
        """Never demand more room than the screen has.

        This window is a page of MultiModuleWindow's tab widget, so whatever
        minimum it reports becomes the application window's minimum -- and a
        window that cannot be made as small as the screen opens with its
        bottom edge below it, which no amount of resizing fixes (maximising
        and restoring is the workaround people find).  Panels scroll their own
        contents, so being handed less than they asked for costs a scrollbar,
        not a missing row.
        """
        hint = super().minimumSizeHint()
        screen = self.screen() if hasattr(self, 'screen') else None
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            hint.setWidth(min(hint.width(), int(available.width() * _MAX_MINIMUM_SCREEN_FRACTION)))
            hint.setHeight(min(hint.height(), int(available.height() * _MAX_MINIMUM_SCREEN_FRACTION)))
        return hint

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
            'app.loadParamsZarr': (self.loadParamsZarrAction, 'Load parameters from saved Zarr store…'),
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

    def showMemoryLimitsDialog(self):
        """Raise the memory-limits editor."""
        self.memoryLimitsDialog.show()
        self.memoryLimitsDialog.raise_()
        self.memoryLimitsDialog.activateWindow()

    def showSessionNotesDialog(self):
        """Raise the (modeless) session-notes editor, opening it if needed."""
        self.sessionNotesDialog.show()
        self.sessionNotesDialog.raise_()
        self.sessionNotesDialog.activateWindow()

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
        else:
            # The saved splitter sizes are the user's own; don't overwrite them
            # with content-derived ones when the window is first shown.
            self._layoutRestored = True

    def closeEvent(self, event):
        self.sigClosing.emit()
        event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if self._initialDockLayoutPending:
            self._initialDockLayoutPending = False
            # Queued: the dock area has not been given its final size yet
            # while this event is being delivered, and a panel that fills
            # itself in lazily (the detector parameter trees) only reports
            # its real size once it has been shown.
            QtCore.QTimer.singleShot(0, self._applyInitialDockLayout)
        self.sigModuleVisibilityChanged.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.sigModuleVisibilityChanged.emit(False)

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


# Stretch factors are relative, and pyqtgraph turns them into splitter sizes
# in proportion to each other, so these are in "pixels of content" units.
_MIN_DOCK_STRETCH = 64     # a one-row panel still gets a usable slice
_MAX_DOCK_STRETCH = 420    # keeps a greedy parameter tree from eating the column
# A panel column narrower than this is unusable whatever its widgets claim --
# a tree or list reports Qt's default size hint, not the width of its rows.
_MIN_DOCK_WIDTH_STRETCH = 240
# ...and one unusually wide panel must not squeeze every other column (and the
# viewer) down to nothing. Panels scroll sideways too, so the cost of the cap
# is a horizontal scrollbar on that one panel.
_MAX_DOCK_WIDTH_STRETCH = 480
# Viewer width, in multiples of the widest panel column: with the usual one
# or two columns of panels beside it this leaves the image ~67-80% of the
# window, and unlike the hard-coded 16-against-3-against-1 it does not
# starve a second column of panels.
_IMAGE_WIDTH_MULTIPLE = 4
# The largest fraction of the screen this window may insist on. Anything above
# 1.0 puts the bottom edge off-screen with no way back.
_MAX_MINIMUM_SCREEN_FRACTION = 0.8


def _dockTitleHeight(dock):
    label = getattr(dock, 'label', None)
    if label is None or dock.labelHidden:
        return 0
    return label.sizeHint().height()


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
