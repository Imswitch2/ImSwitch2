"""Live reconstruction panel: an ImProcess reconstructor over the acquisition.

The controller decides everything; this panel only offers the choices (which
reconstructor, which detector, during scans or free-running, where to show
the result) and hosts the reconstructor's own parameter widget. The simple
in-panel image view is the fallback for when the main viewer is not wanted.
"""

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import dialogtools, pyqtgraphtools
from .basewidgets import Widget


MODE_SCAN = 'scan'
MODE_FREE = 'free'
DISPLAY_VIEWER = 'viewer'
DISPLAY_PANEL = 'panel'

_MODE_ITEMS = ((MODE_SCAN, 'During scans'), (MODE_FREE, 'Free-running'))
_DISPLAY_ITEMS = ((DISPLAY_VIEWER, 'Main viewer'), (DISPLAY_PANEL, 'This panel'))


class LiveReconWidget(Widget):
    """ Live reconstruction with an ImProcess reconstructor. """

    sigLiveToggled = QtCore.Signal(bool)
    sigReconstructorChanged = QtCore.Signal(str)      # plugin id
    sigLoadReconstructorRequested = QtCore.Signal(str)  # plugin id
    sigDetectorChanged = QtCore.Signal(str)
    sigModeChanged = QtCore.Signal(str)               # MODE_SCAN / MODE_FREE
    sigDisplayTargetChanged = QtCore.Signal(str)      # DISPLAY_VIEWER / DISPLAY_PANEL
    sigFramesPerUpdateChanged = QtCore.Signal(int)
    sigUseDisplayedToggled = QtCore.Signal(bool)
    sigFullLayerToggled = QtCore.Signal(bool)
    sigKeepLayerToggled = QtCore.Signal(bool)
    sigKeepRawToggled = QtCore.Signal(bool)
    sigClearRequested = QtCore.Signal()
    sigSendToImProcessRequested = QtCore.Signal()
    sigSaveRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._loadable = []   # [(id, name, description)] offered by "Load…"
        self._blockSignals = False

        self.reconstructorCombo = QtWidgets.QComboBox()
        self.reconstructorCombo.setToolTip(
            'Reconstructors registered in the setup\'s processing block; '
            '"Load…" adds any other one ImProcess knows.'
        )
        self.loadButton = QtWidgets.QPushButton('Load…')
        self.detectorCombo = QtWidgets.QComboBox()
        self.modeCombo = QtWidgets.QComboBox()
        for key, label in _MODE_ITEMS:
            self.modeCombo.addItem(label, key)
        self.modeCombo.setToolTip(
            'During scans: reconstruct each scan while it runs, one stack per '
            'scan. Free-running: reconstruct the detector stream in chunks of '
            '"frames per update" frames until switched off.'
        )
        self.framesPerUpdateSpin = QtWidgets.QSpinBox()
        self.framesPerUpdateSpin.setRange(1, 1000000)
        self.framesPerUpdateSpin.setValue(50)
        self.framesPerUpdateSpin.setToolTip('Frames per reconstruction update in free-running mode')
        self.useDisplayedCheck = QtWidgets.QCheckBox('Use displayed orientation')
        self.useDisplayedCheck.setToolTip(
            'Apply the detector\'s display rotation/flip to the frames before '
            'reconstruction, as the viewer shows them. Off: the raw frames, '
            'as a recording stores them.'
        )
        self.displayCombo = QtWidgets.QComboBox()
        for key, label in _DISPLAY_ITEMS:
            self.displayCombo.addItem(label, key)
        self.fullLayerCheck = QtWidgets.QCheckBox('Full N-D layer')
        self.fullLayerCheck.setToolTip(
            'Show every axis of the result in the main viewer (napari adds '
            'sliders). Off: only the latest 2D plane.'
        )
        self.keepLayerCheck = QtWidgets.QCheckBox('Keep result after run')
        self.keepLayerCheck.setChecked(True)
        self.keepRawCheck = QtWidgets.QCheckBox('Keep raw frames')
        self.keepRawCheck.setToolTip(
            'Keep the frames of the latest stack (one scan, or one free-running '
            'update) in RAM while reconstructing, so they can be saved with the '
            'reconstruction afterwards. Costs up to two stacks of frames in '
            'memory; the status line says how much is held.'
        )
        self.liveButton = QtWidgets.QPushButton('Live')
        self.liveButton.setCheckable(True)
        self.clearButton = QtWidgets.QPushButton('Clear')
        self.sendButton = QtWidgets.QPushButton('Send to ImProcess')
        self.sendButton.setToolTip(
            'Add the reconstruction shown here to ImProcess\'s result list '
            '(ImProcess must be loaded)'
        )
        self.sendButton.setEnabled(False)
        self.saveButton = QtWidgets.QPushButton('Save raw data and reconstruction…')
        self.saveButton.setToolTip(
            'Write the kept raw frames as an ImSwitch HDF5 recording and the '
            'reconstruction next to it'
        )
        self.saveButton.setEnabled(False)
        self.statusLabel = QtWidgets.QLabel('Idle')
        self.statusLabel.setWordWrap(True)

        self.paramGroup = QtWidgets.QGroupBox('Parameters')
        self.paramLayout = QtWidgets.QVBoxLayout()
        self.paramGroup.setLayout(self.paramLayout)
        self._paramWidget = None
        self._paramPlaceholder = QtWidgets.QLabel('No reconstructor selected')
        self.paramLayout.addWidget(self._paramPlaceholder)

        # Fallback image view (pyqtgraph, like the FFT panel).
        self.imageView = pg.GraphicsLayoutWidget()
        self.vb = self.imageView.addViewBox(row=0, col=0)
        self.vb.setMouseMode(pg.ViewBox.RectMode)
        self.img = pg.ImageItem(axisOrder='row-major')
        self.img.setTransform(self.img.transform().translate(-0.5, -0.5))
        self.vb.addItem(self.img)
        self.vb.setAspectLocked(True)
        self.hist = pg.HistogramLUTItem(image=self.img)
        self.hist.gradient.loadPreset('greyclip')
        for tick in self.hist.gradient.ticks:
            tick.hide()
        self.imageView.addItem(self.hist, row=0, col=1)
        self.imageView.setMinimumHeight(200)
        self.imageView.setVisible(False)

        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        row = 0
        grid.addWidget(QtWidgets.QLabel('Reconstructor'), row, 0)
        grid.addWidget(self.reconstructorCombo, row, 1, 1, 2)
        grid.addWidget(self.loadButton, row, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Detector'), row, 0)
        grid.addWidget(self.detectorCombo, row, 1, 1, 3)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Mode'), row, 0)
        grid.addWidget(self.modeCombo, row, 1)
        grid.addWidget(QtWidgets.QLabel('Frames per update'), row, 2)
        grid.addWidget(self.framesPerUpdateSpin, row, 3)
        row += 1
        grid.addWidget(self.useDisplayedCheck, row, 0, 1, 2)
        grid.addWidget(self.keepRawCheck, row, 2, 1, 2)
        row += 1
        grid.addWidget(QtWidgets.QLabel('Show in'), row, 0)
        grid.addWidget(self.displayCombo, row, 1)
        grid.addWidget(self.fullLayerCheck, row, 2)
        grid.addWidget(self.keepLayerCheck, row, 3)
        row += 1
        grid.addWidget(self.paramGroup, row, 0, 1, 4)
        row += 1
        grid.addWidget(self.liveButton, row, 0, 1, 3)
        grid.addWidget(self.clearButton, row, 3)
        row += 1
        grid.addWidget(self.sendButton, row, 0, 1, 2)
        grid.addWidget(self.saveButton, row, 2, 1, 2)
        row += 1
        grid.addWidget(self.statusLabel, row, 0, 1, 4)
        row += 1
        grid.addWidget(self.imageView, row, 0, 1, 4)
        grid.setRowStretch(row, 1)

        self.reconstructorCombo.currentIndexChanged.connect(self._onReconstructorIndex)
        self.loadButton.clicked.connect(self._onLoadClicked)
        self.detectorCombo.currentTextChanged.connect(self._emitIfNotBlocked(self.sigDetectorChanged))
        self.modeCombo.currentIndexChanged.connect(self._onModeIndex)
        self.displayCombo.currentIndexChanged.connect(self._onDisplayIndex)
        self.framesPerUpdateSpin.valueChanged.connect(self._emitIfNotBlocked(self.sigFramesPerUpdateChanged))
        self.useDisplayedCheck.toggled.connect(self._emitIfNotBlocked(self.sigUseDisplayedToggled))
        self.fullLayerCheck.toggled.connect(self._emitIfNotBlocked(self.sigFullLayerToggled))
        self.keepLayerCheck.toggled.connect(self._emitIfNotBlocked(self.sigKeepLayerToggled))
        self.keepRawCheck.toggled.connect(self._emitIfNotBlocked(self.sigKeepRawToggled))
        self.liveButton.toggled.connect(self.sigLiveToggled)
        self.clearButton.clicked.connect(self.sigClearRequested)
        self.sendButton.clicked.connect(self.sigSendToImProcessRequested)
        self.saveButton.clicked.connect(self.sigSaveRequested)
        self._syncModeControls()
        self._syncDisplayControls()

    # -- signal plumbing --------------------------------------------------

    def _emitIfNotBlocked(self, signal):
        def emit(value):
            if not self._blockSignals:
                signal.emit(value)
        return emit

    def _onReconstructorIndex(self, index):
        if self._blockSignals or index < 0:
            return
        self.sigReconstructorChanged.emit(str(self.reconstructorCombo.itemData(index) or ''))

    def _onModeIndex(self, index):
        self._syncModeControls()
        if not self._blockSignals and index >= 0:
            self.sigModeChanged.emit(str(self.modeCombo.itemData(index)))

    def _onDisplayIndex(self, index):
        self._syncDisplayControls()
        if not self._blockSignals and index >= 0:
            self.sigDisplayTargetChanged.emit(str(self.displayCombo.itemData(index)))

    def _syncModeControls(self):
        free = self.getMode() == MODE_FREE
        self.framesPerUpdateSpin.setEnabled(free)

    def _syncDisplayControls(self):
        panel = self.getDisplayTarget() == DISPLAY_PANEL
        self.imageView.setVisible(panel)
        self.fullLayerCheck.setEnabled(not panel)
        self.keepLayerCheck.setEnabled(not panel)

    def _onLoadClicked(self):
        if not self._loadable:
            QtWidgets.QMessageBox.information(
                self, 'Load reconstructor', 'Every known reconstructor is already offered.'
            )
            return
        labels = [f'{plugin_id} — {name}' if name else plugin_id
                  for plugin_id, name, _ in self._loadable]
        label, ok = QtWidgets.QInputDialog.getItem(
            self, 'Load reconstructor', 'Reconstructor', labels, 0, False
        )
        if not ok or not label:
            return
        plugin_id = self._loadable[labels.index(label)][0]
        self.sigLoadReconstructorRequested.emit(plugin_id)

    # -- reconstructors ---------------------------------------------------

    def setReconstructors(self, entries, selected=None):
        """Offer ``entries`` (``(id, name, tooltip)`` tuples) in the combo."""
        self._blockSignals = True
        try:
            current = selected or self.selectedReconstructor()
            self.reconstructorCombo.clear()
            for plugin_id, name, tooltip in entries:
                self.reconstructorCombo.addItem(name or plugin_id, plugin_id)
                index = self.reconstructorCombo.count() - 1
                if tooltip:
                    self.reconstructorCombo.setItemData(index, tooltip, QtCore.Qt.ToolTipRole)
            if current:
                index = self.reconstructorCombo.findData(current)
                if index >= 0:
                    self.reconstructorCombo.setCurrentIndex(index)
        finally:
            self._blockSignals = False

    def setLoadableReconstructors(self, entries):
        """Reconstructors "Load…" may add (``(id, name, description)`` tuples)."""
        self._loadable = list(entries)
        self.loadButton.setEnabled(bool(self._loadable))

    def selectedReconstructor(self):
        data = self.reconstructorCombo.currentData()
        return str(data) if data else None

    def setSelectedReconstructor(self, plugin_id):
        index = self.reconstructorCombo.findData(plugin_id)
        if index >= 0:
            self.reconstructorCombo.setCurrentIndex(index)
        return index >= 0

    def setParameterWidget(self, widget):
        """Host the reconstructor's own parameter widget (or a placeholder)."""
        while self.paramLayout.count():
            item = self.paramLayout.takeAt(0)
            old = item.widget()
            if old is not None:
                old.setParent(None)
                if old is not self._paramPlaceholder:
                    old.deleteLater()
        self._paramWidget = widget
        if widget is None:
            self._paramPlaceholder.setText('This reconstructor takes no parameters')
            self.paramLayout.addWidget(self._paramPlaceholder)
        else:
            self.paramLayout.addWidget(widget)

    def parameterWidget(self):
        return self._paramWidget

    # -- detectors --------------------------------------------------------

    def setDetectors(self, names, current=None):
        self._blockSignals = True
        try:
            self.detectorCombo.clear()
            for name in names:
                self.detectorCombo.addItem(name)
            if current is not None:
                index = self.detectorCombo.findText(current)
                if index >= 0:
                    self.detectorCombo.setCurrentIndex(index)
        finally:
            self._blockSignals = False

    def selectedDetector(self):
        text = self.detectorCombo.currentText()
        return text or None

    def setSelectedDetector(self, name):
        index = self.detectorCombo.findText(name)
        if index >= 0:
            self.detectorCombo.setCurrentIndex(index)
        return index >= 0

    # -- options ----------------------------------------------------------

    def getMode(self):
        return str(self.modeCombo.currentData() or MODE_SCAN)

    def setMode(self, mode):
        index = self.modeCombo.findData(mode)
        if index >= 0:
            self.modeCombo.setCurrentIndex(index)

    def getDisplayTarget(self):
        return str(self.displayCombo.currentData() or DISPLAY_VIEWER)

    def setDisplayTarget(self, target):
        index = self.displayCombo.findData(target)
        if index >= 0:
            self.displayCombo.setCurrentIndex(index)

    def getFramesPerUpdate(self):
        return int(self.framesPerUpdateSpin.value())

    def setFramesPerUpdate(self, value):
        self.framesPerUpdateSpin.setValue(int(value))

    def getUseDisplayed(self):
        return self.useDisplayedCheck.isChecked()

    def setUseDisplayed(self, enabled):
        self.useDisplayedCheck.setChecked(bool(enabled))

    def getFullLayer(self):
        return self.fullLayerCheck.isChecked()

    def setFullLayer(self, enabled):
        self.fullLayerCheck.setChecked(bool(enabled))

    def getKeepLayer(self):
        return self.keepLayerCheck.isChecked()

    def setKeepLayer(self, enabled):
        self.keepLayerCheck.setChecked(bool(enabled))

    def getKeepRaw(self):
        return self.keepRawCheck.isChecked()

    def setKeepRaw(self, enabled):
        self.keepRawCheck.setChecked(bool(enabled))

    # -- actions on the held result ----------------------------------------

    def setSendToImProcessEnabled(self, enabled):
        self.sendButton.setEnabled(bool(enabled))

    def setSaveEnabled(self, enabled):
        self.saveButton.setEnabled(bool(enabled))

    def askForSavePath(self, suggestedPath):
        """Ask where to write the raw frames; the reconstruction goes next to it.

        Returns the chosen path, or ``None`` when the dialog was cancelled.
        """
        return dialogtools.askForFilePath(
            self, 'Save raw data and reconstruction', defaultFolder=suggestedPath,
            nameFilter='HDF5 recording (*.h5)', isSaving=True,
        )

    def isLiveChecked(self):
        return self.liveButton.isChecked()

    def setLiveChecked(self, checked):
        self.liveButton.setChecked(bool(checked))

    def setLiveEnabled(self, enabled):
        self.liveButton.setEnabled(bool(enabled))

    def setStatusText(self, text):
        self.statusLabel.setText(str(text))

    def statusText(self):
        return self.statusLabel.text()

    # -- fallback image view ----------------------------------------------

    def setImage(self, im, autoLevels=None):
        """Show a 2D image in the panel view."""
        im = np.asarray(im)
        previous = self.img.image
        shapeChanged = previous is None or previous.shape != im.shape
        if autoLevels is None:
            autoLevels = shapeChanged
        self.img.setImage(im, autoLevels=autoLevels)
        if shapeChanged:
            pyqtgraphtools.setPGBestImageLimits(self.vb, im.shape[1], im.shape[0])
            self.vb.autoRange()

    def getImage(self):
        return self.img.image

    def clearImage(self):
        self.img.clear()
