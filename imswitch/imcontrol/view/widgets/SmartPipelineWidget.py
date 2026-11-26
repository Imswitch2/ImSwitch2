import os

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol.view import guitools
from .basewidgets import Widget

RUN_MODES = ('visualize', 'validate', 'experiment')
_MODE_LABELS = {'visualize': 'Visualize', 'validate': 'Validate', 'experiment': 'Experiment'}
_MODE_TIPS = {
    'visualize': 'Actions are skipped; the processing runs and the log fills.',
    'validate': 'Actions are checked and logged as "would …"; nothing moves. Devices are reserved.',
    'experiment': 'Actions are dispatched to the hardware. Roles are applied.',
}
_MAX_LOG_LINES = 2000
THRESHOLD_METHODS = ('otsu', 'triangle', 'yen', 'local', 'manual')


class SmartPipelineWidget(Widget):
    """ Chooses a smart-pipeline file, arms it in a run mode, shows its
    status and event log. """

    sigBrowseClicked = QtCore.Signal()
    sigReloadClicked = QtCore.Signal()
    sigDesignClicked = QtCore.Signal()
    sigArmClicked = QtCore.Signal()
    sigStopClicked = QtCore.Signal()
    sigSaveLogClicked = QtCore.Signal()
    sigModeChanged = QtCore.Signal(str)
    # The ROIs
    sigSegmentClicked = QtCore.Signal()
    sigDrawClicked = QtCore.Signal(str)                # rectangle / ellipse / polygon
    sigDeleteRoisClicked = QtCore.Signal()
    sigClearRoisClicked = QtCore.Signal()
    sigSaveRoisClicked = QtCore.Signal()
    sigLoadRoisClicked = QtCore.Signal()
    sigShowRoisToggled = QtCore.Signal(bool)
    sigUseRoisToggled = QtCore.Signal(bool)
    sigRoiSelectionChanged = QtCore.Signal(object)     # [ids]
    sigShapeDrawn = QtCore.Signal(str, object)         # (kind, vertices in displayed pixels)
    sigRoiClicked = QtCore.Signal(int)

    def __init__(self, *args, napariViewer=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.pipelineLabel = QtWidgets.QLabel()
        self.pipelineLabel.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                         QtWidgets.QSizePolicy.Preferred)
        self.browseButton = guitools.BetterPushButton('Choose pipeline…')
        self.reloadButton = guitools.BetterPushButton('Reload')
        self.designButton = guitools.BetterPushButton('Design…')
        self.designButton.setToolTip('Open the pipeline in the workflow editor (a new one when none is chosen)')

        self.summaryLabel = QtWidgets.QLabel()
        self.summaryLabel.setWordWrap(True)
        self.issuesLabel = QtWidgets.QLabel()
        self.issuesLabel.setWordWrap(True)
        self.issuesLabel.setStyleSheet('color: #d9534f;')
        self.issuesLabel.hide()
        # What arming checked: one line per check, coloured by its level.
        self.preflightLabel = QtWidgets.QLabel()
        self.preflightLabel.setWordWrap(True)
        self.preflightLabel.setTextFormat(QtCore.Qt.RichText)
        self.preflightLabel.hide()

        self.modeBox = QtWidgets.QComboBox()
        for mode in RUN_MODES:
            self.modeBox.addItem(_MODE_LABELS[mode], mode)
            self.modeBox.setItemData(self.modeBox.count() - 1, _MODE_TIPS[mode], QtCore.Qt.ToolTipRole)
        self.armButton = guitools.BetterPushButton('Arm')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)

        self.stateLabel = QtWidgets.QLabel('Idle')
        self.stateLabel.setStyleSheet('font-weight: bold; padding: 2px 6px; border-radius: 3px;')
        self.countersLabel = QtWidgets.QLabel()
        self.countersLabel.setWordWrap(True)
        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                       QtWidgets.QSizePolicy.Preferred)

        self.logView = QtWidgets.QPlainTextEdit()
        self.logView.setReadOnly(True)
        self.logView.setMaximumBlockCount(_MAX_LOG_LINES)
        self.logView.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.saveLogButton = guitools.BetterPushButton('Save log…')
        self.clearLogButton = guitools.BetterPushButton('Clear')

        fileRow = QtWidgets.QHBoxLayout()
        fileRow.addWidget(QtWidgets.QLabel('Pipeline:'))
        fileRow.addWidget(self.pipelineLabel, 1)
        fileRow.addWidget(self.browseButton)
        fileRow.addWidget(self.reloadButton)
        fileRow.addWidget(self.designButton)

        runRow = QtWidgets.QHBoxLayout()
        runRow.addWidget(QtWidgets.QLabel('Mode:'))
        runRow.addWidget(self.modeBox)
        runRow.addWidget(self.armButton)
        runRow.addWidget(self.stopButton)
        runRow.addWidget(self.statusLabel, 1)

        logRow = QtWidgets.QHBoxLayout()
        logRow.addWidget(QtWidgets.QLabel('Event log'))
        logRow.addStretch(1)
        logRow.addWidget(self.clearLogButton)
        logRow.addWidget(self.saveLogButton)

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        layout.addLayout(fileRow)
        layout.addWidget(self.summaryLabel)
        layout.addWidget(self.issuesLabel)
        layout.addWidget(self.preflightLabel)
        layout.addLayout(runRow)
        stateRow = QtWidgets.QHBoxLayout()
        stateRow.addWidget(self.stateLabel)
        stateRow.addWidget(self.countersLabel, 1)
        layout.addLayout(stateRow)
        layout.addWidget(self._buildRoiBox())
        layout.addLayout(logRow)
        layout.addWidget(self.logView, 1)

        # The ROIs on the live view (outlines, ids, a layer to draw on); none
        # without a viewer (a headless panel).
        self.roiOverlay = None
        if napariViewer is not None:
            from ._roi_overlay import RoiOverlay

            self.roiOverlay = RoiOverlay(napariViewer, self)
            self.roiOverlay.sigShapeDrawn.connect(self.sigShapeDrawn)
            self.roiOverlay.sigRoiClicked.connect(self.sigRoiClicked)

        # The overlay sink's marks on the live view (added to the viewer by
        # the controller while a pipeline runs).
        self.overlay = naparitools.VispyScatterVisual(color='yellow', symbol='o')
        self.overlay.hide()

        self.browseButton.clicked.connect(self.sigBrowseClicked)
        self.reloadButton.clicked.connect(self.sigReloadClicked)
        self.designButton.clicked.connect(self.sigDesignClicked)
        self.armButton.clicked.connect(self.sigArmClicked)
        self.stopButton.clicked.connect(self.sigStopClicked)
        self.saveLogButton.clicked.connect(self.sigSaveLogClicked)
        self.clearLogButton.clicked.connect(self.clearLog)
        self.modeBox.currentIndexChanged.connect(
            lambda _index: self.sigModeChanged.emit(self.getMode())
        )

        self.setPipelinePath(None)

    # ------------------------------------------------------------------ #
    # The ROIs                                                            #
    # ------------------------------------------------------------------ #
    def _buildRoiBox(self):
        box = QtWidgets.QGroupBox('ROIs')
        box.setToolTip('The cells the pipeline measures: see them on the camera image before arming, '
                       're-segment, draw, delete, save and load them.')
        self.roiSummaryLabel = QtWidgets.QLabel('No ROIs yet: segment the camera image or draw them.')
        self.roiSummaryLabel.setWordWrap(True)
        self.showRoisCheck = QtWidgets.QCheckBox('Show on image')
        self.showRoisCheck.setChecked(True)
        self.useRoisCheck = QtWidgets.QCheckBox('Use these ROIs when arming')
        self.useRoisCheck.setToolTip('Checked: the pipeline measures exactly these ROIs (their ids stay the '
                                     'same in the log and the files). Unchecked: the pipeline segments at arm '
                                     'as its document says.')

        self.thresholdBox = QtWidgets.QComboBox()
        self.thresholdBox.addItems(THRESHOLD_METHODS)
        self.thresholdValue = QtWidgets.QDoubleSpinBox()
        self.thresholdValue.setRange(-1e9, 1e9)
        self.thresholdValue.setDecimals(2)
        self.thresholdValue.setToolTip('Manual threshold (counts); used when the method is "manual"')
        self.smoothSigma = QtWidgets.QDoubleSpinBox()
        self.smoothSigma.setRange(0, 100)
        self.smoothSigma.setDecimals(2)
        self.smoothSigma.setToolTip('Gaussian smoothing before the threshold, pixels')
        self.minArea = QtWidgets.QSpinBox()
        self.minArea.setRange(1, 10 ** 7)
        self.minArea.setToolTip('Smallest ROI kept, pixels')
        self.averageFrames = QtWidgets.QSpinBox()
        self.averageFrames.setRange(1, 1000)
        self.averageFrames.setToolTip('Camera frames averaged into the image that is segmented')
        self.fillHolesCheck = QtWidgets.QCheckBox('Fill holes')
        self.clearBorderCheck = QtWidgets.QCheckBox('Drop ROIs on the border')
        self.segmentButton = guitools.BetterPushButton('Segment now')
        self.segmentButton.setToolTip('Segment the next camera frames with these settings; drawn ROIs are kept, '
                                      'the segmented ones are replaced (with new ids)')

        self.drawRectButton = guitools.BetterPushButton('Rectangle')
        self.drawEllipseButton = guitools.BetterPushButton('Ellipse')
        self.drawPolygonButton = guitools.BetterPushButton('Polygon')
        for button, kind in ((self.drawRectButton, 'rectangle'), (self.drawEllipseButton, 'ellipse'),
                             (self.drawPolygonButton, 'polygon')):
            button.setToolTip(f'Draw a {kind} ROI on the camera image (a polygon ends with a double click)')
            button.clicked.connect(lambda _checked=False, kind=kind: self.sigDrawClicked.emit(kind))
        self.deleteRoisButton = guitools.BetterPushButton('Delete selected')
        self.deleteRoisButton.setToolTip('Delete the ROIs selected in the list or clicked on the image; '
                                         'the other ROIs keep their ids')
        self.clearRoisButton = guitools.BetterPushButton('Clear')
        self.saveRoisButton = guitools.BetterPushButton('Save…')
        self.loadRoisButton = guitools.BetterPushButton('Load…')

        self.roiList = QtWidgets.QListWidget()
        self.roiList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.roiList.setMaximumHeight(110)

        form = QtWidgets.QGridLayout()
        form.addWidget(QtWidgets.QLabel('Threshold:'), 0, 0)
        form.addWidget(self.thresholdBox, 0, 1)
        form.addWidget(self.thresholdValue, 0, 2)
        form.addWidget(QtWidgets.QLabel('Smooth σ:'), 1, 0)
        form.addWidget(self.smoothSigma, 1, 1)
        form.addWidget(QtWidgets.QLabel('Min area:'), 1, 2)
        form.addWidget(self.minArea, 1, 3)
        form.addWidget(QtWidgets.QLabel('Average frames:'), 2, 0)
        form.addWidget(self.averageFrames, 2, 1)
        form.addWidget(self.fillHolesCheck, 2, 2)
        form.addWidget(self.clearBorderCheck, 2, 3)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.roiSummaryLabel, 1)
        top.addWidget(self.showRoisCheck)
        segmentRow = QtWidgets.QHBoxLayout()
        segmentRow.addWidget(self.segmentButton)
        segmentRow.addStretch(1)
        segmentRow.addWidget(self.useRoisCheck)
        drawRow = QtWidgets.QHBoxLayout()
        drawRow.addWidget(QtWidgets.QLabel('Draw:'))
        for button in (self.drawRectButton, self.drawEllipseButton, self.drawPolygonButton):
            drawRow.addWidget(button)
        drawRow.addStretch(1)
        editRow = QtWidgets.QHBoxLayout()
        for button in (self.deleteRoisButton, self.clearRoisButton, self.saveRoisButton, self.loadRoisButton):
            editRow.addWidget(button)

        boxLayout = QtWidgets.QVBoxLayout(box)
        boxLayout.addLayout(top)
        boxLayout.addLayout(form)
        boxLayout.addLayout(segmentRow)
        boxLayout.addLayout(drawRow)
        boxLayout.addWidget(self.roiList)
        boxLayout.addLayout(editRow)

        self.segmentButton.clicked.connect(self.sigSegmentClicked)
        self.deleteRoisButton.clicked.connect(self.sigDeleteRoisClicked)
        self.clearRoisButton.clicked.connect(self.sigClearRoisClicked)
        self.saveRoisButton.clicked.connect(self.sigSaveRoisClicked)
        self.loadRoisButton.clicked.connect(self.sigLoadRoisClicked)
        self.showRoisCheck.toggled.connect(self.sigShowRoisToggled)
        self.useRoisCheck.toggled.connect(self.sigUseRoisToggled)
        self.roiList.itemSelectionChanged.connect(
            lambda: self.sigRoiSelectionChanged.emit(self.getSelectedRois()))
        return box

    def getSegmentationParameters(self):
        return {'threshold_method': self.thresholdBox.currentText(),
                'threshold_value': float(self.thresholdValue.value()),
                'smooth_sigma': float(self.smoothSigma.value()), 'min_area': int(self.minArea.value()),
                'fill_holes': self.fillHolesCheck.isChecked(), 'clear_border': self.clearBorderCheck.isChecked(),
                'reference_frames': int(self.averageFrames.value())}

    def setSegmentationParameters(self, params):
        params = dict(params or {})
        if params.get('threshold_method') in THRESHOLD_METHODS:
            self.thresholdBox.setCurrentText(params['threshold_method'])
        for key, spin, cast in (('threshold_value', self.thresholdValue, float),
                                ('smooth_sigma', self.smoothSigma, float), ('min_area', self.minArea, int),
                                ('reference_frames', self.averageFrames, int)):
            if key in params:
                spin.setValue(cast(params[key]))
        if 'fill_holes' in params:
            self.fillHolesCheck.setChecked(bool(params['fill_holes']))
        if 'clear_border' in params:
            self.clearBorderCheck.setChecked(bool(params['clear_border']))

    def setRois(self, rows, summary, selected=()):
        """The ROI list (rows of ``id``, ``source``, ``area``) and the summary line."""
        selected = set(int(i) for i in selected)
        blocker = QtCore.QSignalBlocker(self.roiList)
        self.roiList.clear()
        for row in rows:
            item = QtWidgets.QListWidgetItem(f"{row['id']:>4}  {row['source']:<8} {row['area']} px")
            item.setData(QtCore.Qt.UserRole, int(row['id']))
            self.roiList.addItem(item)
            item.setSelected(int(row['id']) in selected)
        del blocker
        self.roiSummaryLabel.setText(summary)

    def getSelectedRois(self):
        return [int(item.data(QtCore.Qt.UserRole)) for item in self.roiList.selectedItems()]

    def setRoiControlsEnabled(self, enabled, toolTip=''):
        for widget in (self.segmentButton, self.drawRectButton, self.drawEllipseButton, self.drawPolygonButton,
                       self.loadRoisButton):
            widget.setEnabled(enabled)
        if toolTip:
            self.segmentButton.setToolTip(toolTip)

    def setSegmenting(self, segmenting):
        self.segmentButton.setEnabled(not segmenting)
        self.segmentButton.setText('Collecting frames…' if segmenting else 'Segment now')

    def isShowRois(self):
        return self.showRoisCheck.isChecked()

    def isUseRois(self):
        return self.useRoisCheck.isChecked()

    def setUseRois(self, use):
        self.useRoisCheck.setChecked(bool(use))

    def askForRoisPath(self, defaultFolder, saving):
        return guitools.askForFilePath(self, 'Save ROIs' if saving else 'Load ROIs', defaultFolder=defaultFolder,
                                       nameFilter='ROI sets (*.tif *.tiff *.json *.npy)', isSaving=saving)

    def showRoiOverlay(self, liveLayerName, outline, centres, ids, selected=()):
        if self.roiOverlay is not None:
            self.roiOverlay.show(liveLayerName, outline, centres, ids, selected)

    def hideRoiOverlay(self):
        if self.roiOverlay is not None:
            self.roiOverlay.hide()

    def startDrawingRoi(self, kind):
        if self.roiOverlay is not None:
            self.roiOverlay.startDrawing(kind)

    def askForPipelinePath(self, defaultFolder):
        return guitools.askForFilePath(self, 'Choose pipeline', defaultFolder=defaultFolder,
                                       nameFilter='Pipelines (*.json *.yaml *.yml)')

    def askForLogPath(self, defaultFolder):
        return guitools.askForFilePath(self, 'Save event log', defaultFolder=defaultFolder,
                                       nameFilter='CSV (*.csv);;JSON (*.json)', isSaving=True)

    def setPipelinePath(self, path):
        if path:
            self.pipelineLabel.setText(os.path.basename(path))
            self.pipelineLabel.setToolTip(path)
        else:
            self.pipelineLabel.setText('(none)')
            self.pipelineLabel.setToolTip('')
        self.reloadButton.setEnabled(bool(path))

    def setSummary(self, text):
        self.summaryLabel.setText(text or '')
        self.summaryLabel.setVisible(bool(text))

    def setIssues(self, issues):
        issues = list(issues or [])
        self.issuesLabel.setText('\n'.join(f'• {issue}' for issue in issues))
        self.issuesLabel.setVisible(bool(issues))

    def setPreflight(self, checks):
        """``checks``: (level, text) pairs, level ok / warn / error."""
        import html

        colours = {'ok': '#3c9a3c', 'warn': '#c88a00', 'error': '#d9534f'}
        marks = {'ok': '✔', 'warn': '⚠', 'error': '✖'}
        lines = [f'<span style="color:{colours.get(level, "#888")}">{marks.get(level, "•")} {html.escape(text)}</span>'
                 for level, text in checks]
        self.preflightLabel.setText('<br>'.join(lines))
        self.preflightLabel.setVisible(bool(lines))

    def getPreflightText(self):
        return self.preflightLabel.text()

    def getMode(self):
        return self.modeBox.currentData()

    def setMode(self, mode):
        index = self.modeBox.findData(mode)
        if index >= 0:
            self.modeBox.setCurrentIndex(index)

    def setModeEnabled(self, enabled):
        self.modeBox.setEnabled(enabled)

    def setDesignEnabled(self, enabled, toolTip=''):
        self.designButton.setEnabled(enabled)
        if toolTip:
            self.designButton.setToolTip(toolTip)

    def setArmEnabled(self, enabled, toolTip=''):
        self.armButton.setEnabled(enabled)
        self.armButton.setToolTip(toolTip)

    def setStopEnabled(self, enabled):
        self.stopButton.setEnabled(enabled)

    _STATE_COLOURS = {'idle': ('#555', '#e6e6e6'), 'warming': ('#000', '#ffd27a'), 'dry': ('#fff', '#3a7bd5'),
                      'armed': ('#fff', '#2e9e44'), 'scanning': ('#fff', '#8e44ad'), 'cooling': ('#000', '#a9dfbf'),
                      'stopped': ('#fff', '#888'), 'failed': ('#fff', '#d9534f')}

    def setRunState(self, kind, text, counters=''):
        """The unmistakable state (``kind`` picks the colour) and the counters line."""
        foreground, background = self._STATE_COLOURS.get(kind, self._STATE_COLOURS['idle'])
        self.stateLabel.setText(text)
        self.stateLabel.setStyleSheet(f'font-weight: bold; padding: 2px 6px; border-radius: 3px; '
                                      f'color: {foreground}; background: {background};')
        self.countersLabel.setText(counters)

    def getRunState(self):
        return self.stateLabel.text(), self.countersLabel.text()

    def setStatus(self, text, toolTip=''):
        self.statusLabel.setText(text)
        self.statusLabel.setToolTip(toolTip or text)

    def appendLog(self, text):
        self.logView.appendPlainText(text)

    def getOverlay(self):
        return self.overlay

    def setOverlayPoints(self, x, y):
        """ Marks at the (x, y) detector pixels; empty lists clear them. """
        self.overlay.setData(x=list(x), y=list(y))
        self.overlay.setVisible(bool(len(x)))

    def clearLog(self):
        self.logView.clear()


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
