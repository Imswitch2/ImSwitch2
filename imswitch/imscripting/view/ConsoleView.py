from pyqtgraph.console import ConsoleWidget
from qtpy import QtCore, QtGui

#: What the empty input line says, idle and while a script runs.
_IDLE_HINT = ('Inspect the session: variables of Run cell and Run selection '
              'are here. Acquire from the editor.')
_RUNNING_HINT = 'Disabled while a script runs: it may be changing these variables.'


class ConsoleView(ConsoleWidget):
    """ The inspection console. It works in the session -- the namespace of
    Run cell and Run selection -- and is disabled while a script runs, which
    may be changing that namespace. """

    #: A command was run (also one that raised): the variables may have changed.
    sigCommandExecuted = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.output.document().setDefaultFont(font)
        self.input.setPlaceholderText(_IDLE_HINT)
        self.input.setToolTip(_IDLE_HINT)

        repl = getattr(self, 'repl', None)  # pyqtgraph >= 0.13.4
        if repl is not None and hasattr(repl, 'sigCommandExecuted'):
            repl.sigCommandExecuted.connect(self.sigCommandExecuted)
            repl.sigCommandRaisedException.connect(lambda *_: self.sigCommandExecuted.emit())
        elif hasattr(self.input, 'sigExecuteCmd'):
            # Older consoles announce a command before running it.
            self.input.sigExecuteCmd.connect(
                lambda *_: QtCore.QTimer.singleShot(0, self.sigCommandExecuted.emit)
            )

    def setScriptScope(self, scope):
        self.localNamespace.update(scope)

    def setNamespace(self, namespace):
        """ Works in ``namespace`` from now on: the session's, shared with the
        editor's session runs. """
        self.localNamespace = namespace

    def setRunning(self, running):
        """ Disables the input while a script runs. """
        self.input.setEnabled(not running)
        hint = _RUNNING_HINT if running else _IDLE_HINT
        self.input.setPlaceholderText(hint)
        self.input.setToolTip(hint)

    def sessionReset(self):
        # Straight into the output pane: pyqtgraph's own write() moved between
        # versions, and 0.14's fails before the first command has been typed.
        cursor = self.output.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self.output.setTextCursor(cursor)
        self.output.insertPlainText('# Session reset: every variable forgotten.\n')
        self.output.ensureCursorVisible()


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
