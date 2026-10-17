import html
import logging
import os

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.model import ostools
from imswitch.imcommon.model.logging import logBuffer, logFilePath

#: Colours per level, picked to stay readable on the dark theme ImSwitch2 ships
#: with and on a light one.  Deliberately not reusing LEVEL_STYLES from the
#: logging module: those are terminal colour names for coloredlogs, not CSS.
_LEVEL_COLORS = {
    logging.DEBUG: '#8a8a8a',
    logging.INFO: '#c8c8c8',
    logging.WARNING: '#e5a50a',
    logging.ERROR: '#f66151',
    logging.CRITICAL: '#f66151',
}

_LEVEL_CHOICES = (
    ('Debug', logging.DEBUG),
    ('Info', logging.INFO),
    ('Warning', logging.WARNING),
    ('Error', logging.ERROR),
)


class LogWidget(QtWidgets.QWidget):
    """ Shows the ImSwitch2 log: everything buffered so far, then live records.

    Both ImControl and ImProcess embed one.  There is a single log and a single
    buffer behind them, so two open panels show the same thing -- which is what
    you want when a recording in ImControl is what ImProcess is reacting to.

    The history matters more than the live view.  A standalone bundle has no
    console at all on macOS, so by the time a user thinks to open this, the
    interesting records are minutes old; they are in
    :data:`~imswitch.imcommon.model.logging.logBuffer` and get rendered on
    construction. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._minLevel = logging.INFO
        self._textFilter = ''

        #: Records already rendered, counted the way the buffer counts them.
        self._renderedTotal = 0

        self._build()
        self.reload()

        # Polling, not a callback the logging threads push into.  Records are
        # logged from hardware threads, scan workers and the API server's
        # thread; pushing from there into a Qt widget means a cross-thread
        # signal, and a panel torn down without the event loop running then
        # emits into a dead receiver -- which segfaults the process, as it did
        # here.  A timer owned by this widget cannot outlive it, reads the
        # buffer on the GUI thread, and costs a deque length comparison.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._drainNewRecords)
        # Started by showEvent, not here: the dock is built hidden in both
        # modules, and a panel nobody is looking at has no reason to poll.

    # ------------------------------------------------------------------ build
    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(6)

        controls.addWidget(QtWidgets.QLabel('Level:'))
        self.levelCombo = QtWidgets.QComboBox()
        for label, levelno in _LEVEL_CHOICES:
            self.levelCombo.addItem(label, levelno)
        self.levelCombo.setCurrentIndex(
            [levelno for _, levelno in _LEVEL_CHOICES].index(self._minLevel)
        )
        self.levelCombo.setToolTip(
            'Records are always buffered at Debug, whatever the console shows,'
            ' so raising this reveals detail without restarting ImSwitch2.'
        )
        self.levelCombo.currentIndexChanged.connect(self._onLevelChanged)
        controls.addWidget(self.levelCombo)

        self.filterEdit = QtWidgets.QLineEdit()
        self.filterEdit.setPlaceholderText('Filter…')
        self.filterEdit.setClearButtonEnabled(True)
        self.filterEdit.textChanged.connect(self._onFilterChanged)
        controls.addWidget(self.filterEdit, stretch=1)

        self.autoScrollCheck = QtWidgets.QCheckBox('Follow')
        self.autoScrollCheck.setChecked(True)
        self.autoScrollCheck.setToolTip('Scroll to the newest record as it arrives')
        controls.addWidget(self.autoScrollCheck)

        self.copyButton = QtWidgets.QPushButton('Copy')
        self.copyButton.setToolTip('Copy everything shown to the clipboard')
        self.copyButton.clicked.connect(self.copyToClipboard)
        controls.addWidget(self.copyButton)

        self.saveButton = QtWidgets.QPushButton('Save…')
        self.saveButton.setToolTip('Write everything shown to a text file')
        self.saveButton.clicked.connect(self.saveToFile)
        controls.addWidget(self.saveButton)

        self.openFolderButton = QtWidgets.QPushButton('Log folder')
        self.openFolderButton.setToolTip('Open the folder holding the rotating log file')
        self.openFolderButton.clicked.connect(self.openLogFolder)
        controls.addWidget(self.openFolderButton)

        layout.addLayout(controls)

        self.textEdit = QtWidgets.QPlainTextEdit()
        self.textEdit.setReadOnly(True)
        self.textEdit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.textEdit.setUndoRedoEnabled(False)
        # Bounds the widget independently of the buffer: a long DEBUG session can
        # push a lot of text through here, and QPlainTextEdit keeps every block.
        self.textEdit.setMaximumBlockCount(20000)
        font = QtGui.QFont('Menlo')
        font.setStyleHint(QtGui.QFont.Monospace)
        font.setPointSize(10)
        self.textEdit.setFont(font)
        layout.addWidget(self.textEdit, stretch=1)

        self.pathLabel = QtWidgets.QLabel()
        self.pathLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.pathLabel.setStyleSheet('color: #8a8a8a; font-size: 9pt')
        layout.addWidget(self.pathLabel)
        self._refreshPathLabel()

    def _refreshPathLabel(self):
        path = logFilePath()
        if path:
            self.pathLabel.setText(f'Log file: {path}')
            self.openFolderButton.setEnabled(True)
        else:
            self.pathLabel.setText('No log file for this session — nothing is being written to disk.')
            self.openFolderButton.setEnabled(False)

    # --------------------------------------------------------------- lifetime
    def showEvent(self, event):
        super().showEvent(event)
        # Catch up on everything logged while hidden, then resume polling.  The
        # buffer kept it all, so a panel reopened after an hour is not missing
        # the hour.
        self.reload()
        self._timer.start()

    def hideEvent(self, event):
        # A closed or hidden panel stops polling entirely.  Beyond the wasted
        # work, a timer that outlives what anyone is looking at is how a widget
        # ends up touching its own half-torn-down children.
        self._timer.stop()
        super().hideEvent(event)

    # ----------------------------------------------------------------- render
    def _shouldShow(self, levelno, text):
        if levelno < self._minLevel:
            return False
        return not self._textFilter or self._textFilter in text.lower()

    def _append(self, levelno, text):
        color = _LEVEL_COLORS.get(levelno, _LEVEL_COLORS[logging.INFO])
        self.textEdit.appendHtml(
            f'<span style="color:{color}; white-space:pre">{html.escape(text)}</span>'
        )

    def reload(self):
        """ Re-render from the buffer.

        Filtering re-renders rather than hiding lines: the buffer is the source
        of truth, so widening the filter brings back records the widget never
        showed, instead of only those it happens to still hold. """

        self.textEdit.clear()
        # Read total() first: a record arriving between the two would otherwise
        # be rendered here and counted as new on the next tick, showing twice.
        self._renderedTotal = logBuffer.total()
        for levelno, _levelname, text in logBuffer.records():
            if self._shouldShow(levelno, text):
                self._append(levelno, text)
        self._scrollToEndIfFollowing()
        self._refreshPathLabel()

    def _scrollToEndIfFollowing(self):
        if self.autoScrollCheck.isChecked():
            self.textEdit.verticalScrollBar().setValue(
                self.textEdit.verticalScrollBar().maximum()
            )

    # ---------------------------------------------------------------- signals
    def _drainNewRecords(self):
        """ Append whatever has been logged since the last pass. """

        total = logBuffer.total()
        newCount = total - self._renderedTotal
        if newCount <= 0:
            self._renderedTotal = total  # buffer cleared under us
            return

        records = logBuffer.records()
        # Counting from total() rather than indexing means a burst larger than
        # the buffer just loses its oldest records instead of mis-slicing.
        appended = False
        for levelno, _levelname, text in records[-min(newCount, len(records)):]:
            if self._shouldShow(levelno, text):
                self._append(levelno, text)
                appended = True
        self._renderedTotal = total
        if appended:
            self._scrollToEndIfFollowing()

    def _onLevelChanged(self, index):
        self._minLevel = self.levelCombo.itemData(index)
        self.reload()

    def _onFilterChanged(self, text):
        self._textFilter = text.strip().lower()
        self.reload()

    # ---------------------------------------------------------------- actions
    def copyToClipboard(self):
        QtWidgets.QApplication.clipboard().setText(self.textEdit.toPlainText())

    def saveToFile(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save log', os.path.join(os.path.expanduser('~'), 'imswitch-log.txt'),
            'Text files (*.txt);;All files (*)'
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(self.textEdit.toPlainText())
        except OSError as err:
            QtWidgets.QMessageBox.warning(self, 'Could not save the log', str(err))

    def openLogFolder(self):
        path = logFilePath()
        if not path:
            return
        try:
            ostools.openFolderInOS(os.path.dirname(path))
        except ostools.OSToolsError as err:
            QtWidgets.QMessageBox.warning(self, 'Could not open the folder', str(err))


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
