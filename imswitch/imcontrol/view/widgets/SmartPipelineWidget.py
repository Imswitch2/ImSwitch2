import os

from qtpy import QtCore, QtGui, QtWidgets

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

    def __init__(self, *args, **kwargs):
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

        self.modeBox = QtWidgets.QComboBox()
        for mode in RUN_MODES:
            self.modeBox.addItem(_MODE_LABELS[mode], mode)
            self.modeBox.setItemData(self.modeBox.count() - 1, _MODE_TIPS[mode], QtCore.Qt.ToolTipRole)
        self.armButton = guitools.BetterPushButton('Arm')
        self.stopButton = guitools.BetterPushButton('Stop')
        self.stopButton.setEnabled(False)

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
        layout.addLayout(runRow)
        layout.addLayout(logRow)
        layout.addWidget(self.logView, 1)

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

    def setStatus(self, text, toolTip=''):
        self.statusLabel.setText(text)
        self.statusLabel.setToolTip(toolTip or text)

    def appendLog(self, text):
        self.logView.appendPlainText(text)

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
