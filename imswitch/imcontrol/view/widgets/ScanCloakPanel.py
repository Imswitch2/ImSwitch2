"""The Scan dock of a scan cloak (docs/simple-point-scan-plan.md §10).

A :class:`ScanCloakPanel` holds two pages, one switch apart: the cloak's
simple page (a :class:`ScanCloakView`) and the backend panel's own widget,
built unchanged. The controller runs the backend's scans from either page;
what the pages show is kept in step by the controller, not here.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from .basewidgets import Widget

SIMPLE = 'simple'
ADVANCED = 'advanced'


class ScanCloakView(QtWidgets.QWidget):
    """The base of a cloak's simple page.

    It has what every simple page needs -- a run row (Live, Start, Stop),
    the time the next scan takes, the running scan's progress, and a message
    line -- and a ``content``
    layout the cloak fills with its own controls. Mode buttons go at the
    front of ``runRow``.
    """

    sigStartClicked = QtCore.Signal()
    sigStopClicked = QtCore.Signal()
    sigLiveToggled = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self.liveBox = QtWidgets.QCheckBox('Live')
        self.liveBox.setToolTip('Repeat the scan until Stop.')
        self.liveBox.toggled.connect(self.sigLiveToggled)
        self.startButton = QtWidgets.QPushButton('Start')
        self.startButton.clicked.connect(self.sigStartClicked)
        self.stopButton = QtWidgets.QPushButton('Stop')
        self.stopButton.setToolTip('No further frame. A frame already running completes.')
        self.stopButton.setEnabled(False)
        self.stopButton.clicked.connect(self.sigStopClicked)
        self.runRow = QtWidgets.QHBoxLayout()
        self.runRow.addStretch(1)
        for widget in (self.liveBox, self.startButton, self.stopButton):
            self.runRow.addWidget(widget)

        # What the next scan takes.
        self.estimateTitle = QtWidgets.QLabel('Scan time')
        self.frameTimeLabel = QtWidgets.QLabel('–')
        font = self.frameTimeLabel.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.frameTimeLabel.setFont(font)
        self.estimateNote = QtWidgets.QLabel('')
        self.estimateNote.setWordWrap(True)
        estimateRow = QtWidgets.QHBoxLayout()
        estimateRow.addWidget(self.estimateTitle)
        estimateRow.addWidget(self.frameTimeLabel)
        estimateRow.addStretch(1)

        # The running scan's progress, as the backend panel shows it.
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setRange(0, 1000)
        self.progressBar.setTextVisible(False)
        self.progressLabel = QtWidgets.QLabel()
        self.progressRow = QtWidgets.QWidget()
        progressLayout = QtWidgets.QHBoxLayout(self.progressRow)
        progressLayout.setContentsMargins(0, 0, 0, 0)
        progressLayout.addWidget(self.progressBar, 1)
        progressLayout.addWidget(self.progressLabel)
        self.progressRow.setVisible(False)

        self.content = QtWidgets.QVBoxLayout()
        self.messageLabel = QtWidgets.QLabel('')
        self.messageLabel.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(self.runRow)
        layout.addLayout(estimateRow)
        layout.addWidget(self.estimateNote)
        layout.addWidget(self.progressRow)
        layout.addLayout(self.content)
        layout.addWidget(self.messageLabel)
        layout.addStretch(1)

    def liveEnabled(self) -> bool:
        return self.liveBox.isChecked()

    def setLive(self, live: bool):
        self.liveBox.setChecked(bool(live))

    def isRunning(self) -> bool:
        return self._running

    def setRunning(self, running: bool):
        self._running = bool(running)
        self.startButton.setEnabled(not self._running)
        self.stopButton.setEnabled(self._running)
        self._runningChanged(self._running)

    def _runningChanged(self, running: bool):
        """For a cloak's controls that must not change during a run."""

    def showProgress(self, fraction, text: str):
        """The running scan's progress; ``fraction`` None means unknown."""
        if fraction is None:
            self.progressBar.setRange(0, 0)     # busy indicator
        else:
            self.progressBar.setRange(0, 1000)
            self.progressBar.setValue(int(round(1000 * min(max(float(fraction), 0.0), 1.0))))
        self.progressLabel.setText(text)
        self.progressRow.setVisible(True)

    def hideProgress(self):
        self.progressRow.setVisible(False)
        self.progressBar.setRange(0, 1000)
        self.progressBar.setValue(0)
        self.progressLabel.clear()

    def showMessage(self, text, error=False):
        self.messageLabel.setText(text)
        self.messageLabel.setStyleSheet('color: #d9534f;' if error else '')


class ScanCloakPanel(Widget):
    """The Scan dock of a cloak: the simple page and the backend's own
    widget, with a switch between them.

    Subclasses name the two pages' classes. The backend widget is built as
    its own panel would build it and is not changed, except that its Start
    button's run state and its progress bar are also shown on the simple
    page.
    """

    #: The backend panel's widget class (e.g. ``ScanWidgetAdvanced``).
    backendWidgetClass = None
    #: The simple page's class (a :class:`ScanCloakView`).
    viewClass = None
    #: The dock's heading.
    title = ''

    # Both pages scroll on their own.
    scrollablePanel = False

    sigPageRequested = QtCore.Signal(str)

    def __init__(self, options, *args, napariViewer=None, **kwargs):
        super().__init__(options)
        self.setMinimumSize(0, 0)
        self.backend = self.backendWidgetClass(options)
        self.view = self.viewClass(napariViewer=napariViewer)

        # The scan base marks a run on the backend's Start button, whichever
        # page started it; the simple page shows the same.
        backendSetScanButtonChecked = self.backend.setScanButtonChecked

        def setScanButtonChecked(checked):
            backendSetScanButtonChecked(checked)
            self.view.setRunning(bool(checked))

        self.backend.setScanButtonChecked = setScanButtonChecked

        # ...and its progress bar, when the backend has one. The simple page
        # always shows it; the backend's own box for it is an Advanced one.
        backendShowProgress = getattr(self.backend, 'showScanProgress', None)
        backendHideProgress = getattr(self.backend, 'hideScanProgress', None)
        if callable(backendShowProgress) and callable(backendHideProgress):
            def showScanProgress(fraction, text):
                backendShowProgress(fraction, text)
                self.view.showProgress(fraction, text)

            def hideScanProgress():
                backendHideProgress()
                self.view.hideProgress()

            self.backend.showScanProgress = showScanProgress
            self.backend.hideScanProgress = hideScanProgress

        titleLabel = QtWidgets.QLabel(self.title)
        font = titleLabel.font()
        font.setBold(True)
        titleLabel.setFont(font)
        # The simple page's Load/Save; the Advanced page has its own.
        self.loadButton = QtWidgets.QPushButton('Load…')
        self.saveButton = QtWidgets.QPushButton('Save…')
        self.loadButton.setToolTip('Load a saved scan.')
        self.saveButton.setToolTip('Save this scan; it also opens in the Advanced panel.')
        self.loadButton.clicked.connect(self.backend.sigLoadScanClicked)
        self.saveButton.clicked.connect(self.backend.sigSaveScanClicked)
        self.simpleButton = QtWidgets.QPushButton('Simple')
        self.advancedButton = QtWidgets.QPushButton('Advanced')
        self.simpleButton.setToolTip('The simple panel.')
        self.advancedButton.setToolTip('The full Advanced scan panel, on the same scan.')
        group = QtWidgets.QButtonGroup(self)
        for button in (self.simpleButton, self.advancedButton):
            button.setCheckable(True)
            group.addButton(button)
        self.simpleButton.setChecked(True)
        self.simpleButton.clicked.connect(lambda: self.sigPageRequested.emit(SIMPLE))
        self.advancedButton.clicked.connect(lambda: self.sigPageRequested.emit(ADVANCED))
        header = QtWidgets.QHBoxLayout()
        header.addWidget(titleLabel)
        header.addStretch(1)
        for button in (self.loadButton, self.saveButton, self.simpleButton, self.advancedButton):
            header.addWidget(button)
        # A note that concerns both pages (e.g. why a scan opened on Advanced).
        self.noteLabel = QtWidgets.QLabel('')
        self.noteLabel.setWordWrap(True)
        self.noteLabel.setVisible(False)

        viewScroll = QtWidgets.QScrollArea()
        viewScroll.setWidgetResizable(True)
        viewScroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        viewScroll.setWidget(self.view)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(viewScroll)
        self.stack.addWidget(self.backend)
        self._pages = {SIMPLE: viewScroll, ADVANCED: self.backend}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.noteLabel)
        layout.addWidget(self.stack)

    def page(self) -> str:
        return SIMPLE if self.stack.currentWidget() is self._pages[SIMPLE] else ADVANCED

    def showPage(self, page: str):
        self.stack.setCurrentWidget(self._pages[page])
        self.simpleButton.setChecked(page == SIMPLE)
        self.advancedButton.setChecked(page == ADVANCED)
        for button in (self.loadButton, self.saveButton):
            button.setVisible(page == SIMPLE)

    def showNote(self, text: str = ''):
        self.noteLabel.setText(text)
        self.noteLabel.setVisible(bool(text))

    def confirmDiscardAdvanced(self, reason: str) -> bool:
        """Whether to drop Advanced settings the simple page cannot show."""
        answer = QtWidgets.QMessageBox.question(
            self, 'Back to the simple panel',
            f'The Advanced settings go beyond the simple panel:\n\n{reason}\n\n'
            'Discard them and go back to the simple settings?',
            QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        return answer == QtWidgets.QMessageBox.Discard


__all__ = ['ADVANCED', 'SIMPLE', 'ScanCloakPanel', 'ScanCloakView']


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
