import uuid

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import PythonCodeEditor

from .guitools import BetterPushButton

#: The editor moved to imcommon so ImProcess's Python step can share it.
Scintilla = PythonCodeEditor


class EditorView(QtWidgets.QTabWidget):
    """ View that displays a variable amount of editor instances in different
    tabs, where the user can edit scripts. """

    sigRunAllClicked = QtCore.Signal(str, str)  # (instanceID, text)
    sigRunSelectedClicked = QtCore.Signal(str, str, int)  # (instanceID, selectedText, firstLine)
    sigRunCellClicked = QtCore.Signal(str, bool)  # (instanceID, advance)
    sigResetSessionClicked = QtCore.Signal(str)  # (instanceID)
    sigStopClicked = QtCore.Signal(str)  # (instanceID)
    sigTextChanged = QtCore.Signal(str)  # (instanceID)
    sigInstanceCloseClicked = QtCore.Signal(str)  # (instanceID)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setTabsClosable(True)
        self._running = False

        self.tabCloseRequested.connect(
            lambda index: self.sigInstanceCloseClicked.emit(self.widget(index).getID())
        )

    def addEditor(self, name):
        """ Opens a new editor tab. Returns the created editor instance. """
        editorInstance = EditorInstanceView()
        editorInstance.sigRunAllClicked.connect(
            lambda text: self.sigRunAllClicked.emit(editorInstance.getID(), text)
        )
        editorInstance.sigRunSelectedClicked.connect(
            lambda text, line: self.sigRunSelectedClicked.emit(editorInstance.getID(), text, line)
        )
        editorInstance.sigRunCellClicked.connect(
            lambda advance: self.sigRunCellClicked.emit(editorInstance.getID(), advance)
        )
        editorInstance.sigResetSessionClicked.connect(
            lambda: self.sigResetSessionClicked.emit(editorInstance.getID())
        )
        editorInstance.sigTextChanged.connect(
            lambda: self.sigTextChanged.emit(editorInstance.getID())
        )
        editorInstance.sigStopClicked.connect(
            lambda: self.sigStopClicked.emit(editorInstance.getID())
        )
        editorInstance.setRunning(self._running)
        self.addTab(editorInstance, name)
        self.setCurrentWidget(editorInstance)
        return editorInstance

    def setEditorName(self, instanceID, name):
        """ Sets the title of an editor tab. """
        for i in range(self.count()):
            if self.widget(i).getID() == instanceID:
                self.setTabText(i, name)
                return

    def getCurrentInstance(self):
        """ Returns the currently active (displayed) editor instance. """
        return self.currentWidget()

    def setCurrentInstanceByID(self, instanceID):
        """ Sets the active (displayed) editor instance to the editor instance
        with the specified ID. """
        for i in range(self.count()):
            widget = self.widget(i)
            if widget.getID() == instanceID:
                self.setCurrentWidget(widget)
                return

    def getInstanceByID(self, instanceID):
        """ Returns the editor instance with the specified ID, or None if no
        editor instance matches. """
        for i in range(self.count()):
            widget = self.widget(i)
            if widget.getID() == instanceID:
                return widget

    def setStopping(self, stopping):
        """ Shows in every editor instance whether the running script is
        currently being stopped. """
        for i in range(self.count()):
            instance = self.widget(i)
            if hasattr(instance, 'setStopping'):
                instance.setStopping(stopping)

    def setRunning(self, running):
        """ Shows in every editor instance whether a script runs: the session
        cannot be reset meanwhile. """
        self._running = bool(running)
        for i in range(self.count()):
            instance = self.widget(i)
            if hasattr(instance, 'setRunning'):
                instance.setRunning(running)

    def closeInstance(self, instanceID):
        """ Closes the editor instance with the specified ID. """
        for i in range(self.count()):
            widget = self.widget(i)
            if widget.getID() == instanceID:
                widget.deleteLater()
                self.removeTab(i)
                return


class EditorInstanceView(QtWidgets.QWidget):
    """ A single editor instance where the user can edit scripts.

    *Run all* runs the whole script in a fresh namespace. *Run cell*
    (Ctrl+Enter; Shift+Enter also moves to the next cell) and *Run selection*
    (F9; the current line when nothing is selected) run in the session, whose
    variables live on between runs and are shared with the console. *Reset
    session* forgets them. """

    sigRunAllClicked = QtCore.Signal(str)  # (text)
    sigRunSelectedClicked = QtCore.Signal(str, int)  # (selectedText, firstLine)
    sigRunCellClicked = QtCore.Signal(bool)  # (advance to the next cell)
    sigResetSessionClicked = QtCore.Signal()
    sigStopClicked = QtCore.Signal()
    sigTextChanged = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.id = str(uuid.uuid4())

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        topContainer = QtWidgets.QHBoxLayout()
        topContainer.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(topContainer, 0)

        topContainer.addSpacerItem(
            QtWidgets.QSpacerItem(
                40, 20, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum
            )
        )

        self.runAllButton = BetterPushButton('Run all')
        self.runAllButton.setToolTip(
            'Runs the whole script in a fresh namespace: the same result every '
            'time, nothing left over from earlier runs.'
        )
        self.runAllButton.clicked.connect(
            lambda: self.sigRunAllClicked.emit(self.getText())
        )
        topContainer.addWidget(self.runAllButton, 0)

        self.runCellButton = BetterPushButton('Run cell')
        self.runCellButton.setToolTip(
            'Runs the cell the cursor is in -- from a "# %%" line to the next -- '
            'in the session, where variables of earlier cells and selections '
            'are, and the console sees them (Ctrl+Enter; Shift+Enter also moves '
            'to the next cell).'
        )
        self.runCellButton.clicked.connect(lambda: self.sigRunCellClicked.emit(False))
        topContainer.addWidget(self.runCellButton, 0)

        self.runSelectionButton = BetterPushButton('Run selection')
        self.runSelectionButton.setToolTip(
            'Runs the selected lines, or the line the cursor is on, in the '
            'session (F9).'
        )
        self.runSelectionButton.clicked.connect(self._emitRunSelection)
        topContainer.addWidget(self.runSelectionButton, 0)

        self.resetSessionButton = BetterPushButton('Reset session')
        self.resetSessionButton.setToolTip(
            'Forgets every variable of Run cell, Run selection and the console. '
            'Not while a script runs.'
        )
        self.resetSessionButton.clicked.connect(self.sigResetSessionClicked)
        topContainer.addWidget(self.resetSessionButton, 0)

        self.stopButton = BetterPushButton('Stop')
        self.stopButton.clicked.connect(
            lambda: self.sigStopClicked.emit()
        )
        topContainer.addWidget(self.stopButton, 0)

        self.scintilla = Scintilla()
        self.scintilla.textChanged.connect(self.sigTextChanged)
        self.scintilla.installEventFilter(self)
        layout.addWidget(self.scintilla, 3)

    def eventFilter(self, source, event):
        # Before the editor sees the key: QScintilla would insert a line
        # break for Ctrl/Shift+Enter.
        if source is self.scintilla and event.type() == QtCore.QEvent.KeyPress:
            key, modifiers = event.key(), event.modifiers()
            if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                if modifiers & QtCore.Qt.ControlModifier:
                    self.sigRunCellClicked.emit(False)
                    return True
                if modifiers & QtCore.Qt.ShiftModifier:
                    self.sigRunCellClicked.emit(True)
                    return True
            elif key == QtCore.Qt.Key_F9 and not modifiers:
                self._emitRunSelection()
                return True
        return super().eventFilter(source, event)

    def _emitRunSelection(self):
        text = self.getSelectedText()
        if text.strip():
            self.sigRunSelectedClicked.emit(text, self.scintilla.selectionFirstLine())
        else:
            line = self.cursorLine()
            lines = self.getText().split('\n')
            if 0 <= line < len(lines):
                self.sigRunSelectedClicked.emit(lines[line], line)

    def getID(self):
        """ Returns the ID of the editor instance. Can be passed to certain
        methods in the containing EditorView. """
        return self.id

    def getSelectedText(self):
        """ Returns the selected text in the editor instance. """
        return self.scintilla.selectedText()

    def cursorLine(self):
        """ The 0-based line the cursor is on. """
        return self.scintilla.cursorLine()

    def setCursorLine(self, line):
        """ Puts the cursor at the start of the 0-based ``line``. """
        self.scintilla.setCursorLine(line)

    def getText(self):
        """ Returns all text in the editor instance. """
        return self.scintilla.text()

    def setText(self, text):
        """ Sets the text in the editor instance. """
        self.scintilla.setText(text)

    def setStopping(self, stopping):
        """ Disables the stop button and labels it while a script is being
        stopped. """
        self.stopButton.setEnabled(not stopping)
        self.stopButton.setText('Stopping…' if stopping else 'Stop')

    def setRunning(self, running):
        """ The session cannot be reset while a script runs. """
        self.resetSessionButton.setEnabled(not running)


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
