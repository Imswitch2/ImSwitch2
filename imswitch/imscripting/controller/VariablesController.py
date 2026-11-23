from imswitch.imscripting.model import describeValue, isShowable
from .basecontrollers import ImScrWidgetController


class VariablesController(ImScrWidgetController):
    """ Connected to VariablesView: lists the session's variables when no
    script runs, and shows an array in ImControl's image view on request. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._executor = None
        self._widget.sigShowRequested.connect(self.showVariable)

    def attachExecutor(self, executor, console=None):
        """ Lists executor's session: again whenever a run ends, the
        session is reset, or console ran a command. """
        self._executor = executor
        executor.sigExecutionStarted.connect(lambda _result: self._widget.setRunning(True))
        executor.sigExecutionFinished.connect(lambda _result: self.refresh())
        executor.sigSessionReset.connect(self.refresh)
        if console is not None:
            console.sigCommandExecuted.connect(self.refresh)
        self.refresh()

    def refresh(self):
        """ Lists the variables, unless a script runs (the table would read
        a namespace the script thread is changing). """
        if self._executor is None:
            return
        running = self._executor.isBusy()
        self._widget.setRunning(running)
        if running:
            return
        rows = [(name, describeValue(value), isShowable(value))
                for name, value in self._executor.session.userVariables().items()]
        self._widget.setVariables(rows)

    def showVariable(self, name):
        """ Shows the variable name in ImControl's image view, in a layer
        of the same name. """
        if self._executor is None or self._executor.isBusy():
            return
        value = self._executor.session.namespace.get(name)
        try:
            self._scriptScope['show'](value, name)
        except Exception as error:  # shown to the user, not raised into Qt
            self._widget.setMessage(f'Could not show {name}: {error}')
        else:
            self._widget.setMessage(f'{name} is shown in the image view of ImControl.')


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
