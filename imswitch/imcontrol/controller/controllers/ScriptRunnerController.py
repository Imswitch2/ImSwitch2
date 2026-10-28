import json
import os
import uuid

from qtpy import QtCore

from imswitch.imcommon.model import (
    convertInputText, dirtools, findScriptInputs, initLogger,
)
from ..basecontrollers import ImConWidgetController

_scriptsFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'scripts')
_stateFilePath = os.path.join(dirtools.UserFileDirs.Config, 'script_runner.json')


class ScriptRunnerController(ImConWidgetController):
    """ Linked to ScriptRunnerWidget. Builds the input form from the
    ``scriptInput(...)`` calls of the chosen script (parsed, never run) and
    asks the imscripting module to run the script with the typed values.

    An empty field is not passed, so the script uses its own default. Values
    the user typed are remembered per script, also across restarts; fields
    left alone follow the script's defaults when the file changes. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

        self._scriptPath = None
        self._specs = []
        self._parseError = None
        self._editedTexts = {}  # { scriptPath: { inputName: text } }
        self._runningIds = set()  # ScriptRunResult ids of runs started and not finished
        self._requestId = None  # of the run this widget started, while it runs
        self._stopping = False

        self._fileWatcher = QtCore.QFileSystemWatcher()
        self._fileWatcher.fileChanged.connect(self._onFileChanged)

        self._widget.sigBrowseClicked.connect(self.browse)
        self._widget.sigRunClicked.connect(self.run)
        self._widget.sigStopClicked.connect(self.stop)
        self._widget.sigInputEdited.connect(self._onInputEdited)

        self._moduleCommChannel.sigScriptRunStarted.connect(self._onRunStarted)
        self._moduleCommChannel.sigScriptRunFinished.connect(self._onRunFinished)

        state = _loadState()
        self._editedTexts = state.get('values', {})
        if state.get('scriptPath'):
            self.setScriptPath(state['scriptPath'])
        else:
            self._updateButtons()

    # ------------------------------------------------------------------ #
    # Script and form                                                     #
    # ------------------------------------------------------------------ #
    def browse(self):
        folder = os.path.dirname(self._scriptPath) if self._scriptPath else _scriptsFolderPath
        path = self._widget.askForScriptPath(folder)
        if path:
            self.setScriptPath(path)

    def setScriptPath(self, path):
        if self._scriptPath:
            self._fileWatcher.removePath(self._scriptPath)
        self._scriptPath = os.path.normpath(path) if path else None
        self._widget.setScriptPath(self._scriptPath)
        self._widget.setStatus('')
        if self._scriptPath and os.path.isfile(self._scriptPath):
            self._fileWatcher.addPath(self._scriptPath)
        self.reload()
        self._saveState()

    def reload(self):
        """ Parses the script again and rebuilds the form. Returns whether
        the script could be read and parsed. """
        self._specs = []
        self._parseError = None
        if not self._scriptPath:
            self._widget.setInputs([], {})
            self._widget.setMessage('')
            self._updateButtons()
            return False

        try:
            with open(self._scriptPath, 'rb') as file:
                source = file.read().decode('utf-8-sig', errors='replace')
        except OSError as e:
            self._parseError = f'Cannot read the script: {e.strerror or e}'
        else:
            scan = findScriptInputs(source)
            self._parseError = scan.error
            self._specs = scan.inputs

        edited = self._editedTexts.get(self._scriptPath, {})
        texts = {spec.name: edited.get(spec.name, spec.defaultText()) for spec in self._specs}
        self._widget.setInputs(self._specs, texts)
        for spec in self._specs:
            self._validate(spec, texts[spec.name])

        if self._parseError:
            self._widget.setMessage(self._parseError)
        else:
            notes = [] if self._specs else ['This script declares no inputs.']
            self._widget.setMessage('\n'.join(scan.warnings + notes))
        self._updateButtons()
        return self._parseError is None

    def _onFileChanged(self, path):
        # Editors that save by replacing the file drop it from the watcher.
        if os.path.isfile(path) and path not in self._fileWatcher.files():
            self._fileWatcher.addPath(path)
        if self._scriptPath and os.path.normpath(path) == self._scriptPath:
            self.reload()

    def _onInputEdited(self, name, text):
        if not self._scriptPath:
            return
        self._editedTexts.setdefault(self._scriptPath, {})[name] = text
        spec = self._spec(name)
        if spec is not None:
            self._validate(spec, text)

    def _captureTexts(self):
        """ Remembers what the fields hold before the form is rebuilt, in
        case text got there without an edit signal. """
        edited = self._editedTexts.setdefault(self._scriptPath, {})
        for name, text in self._widget.getInputTexts().items():
            spec = self._spec(name)
            if name in edited or (spec is not None and text != spec.defaultText()):
                edited[name] = text

    def _validate(self, spec, text):
        """ Converts text for spec, marking the field when it does not fit.
        Returns (ok, value); an empty text is ok and means the default. """
        if not text.strip():
            self._widget.setInputError(spec.name, None)
            return True, None
        try:
            value = convertInputText(text, spec.type)
        except ValueError as e:
            self._widget.setInputError(spec.name, str(e))
            return False, None
        self._widget.setInputError(spec.name, None)
        return True, value

    def _spec(self, name):
        return next((spec for spec in self._specs if spec.name == name), None)

    # ------------------------------------------------------------------ #
    # Running                                                             #
    # ------------------------------------------------------------------ #
    def run(self):
        if not self._scriptPath or self._requestId is not None or self._runningIds:
            return
        self._captureTexts()
        if not self.reload():
            return

        texts = self._widget.getInputTexts()
        inputs = {}
        invalid = []
        for spec in self._specs:
            ok, value = self._validate(spec, texts.get(spec.name, ''))
            if not ok:
                invalid.append(spec.name)
            elif value is not None:
                inputs[spec.name] = value
        if invalid:
            self._widget.setStatus('Check the marked input(s): ' + ', '.join(invalid))
            return

        self._saveState()
        self._requestId = uuid.uuid4().hex
        self._stopping = False
        self._widget.setStatus('Starting…')
        self.__logger.info(f'Running {self._scriptPath} with {inputs}')
        self._moduleCommChannel.sigRunScriptFile.emit(self._scriptPath, inputs, self._requestId)
        self._updateButtons()

    def stop(self):
        if self._requestId is None or self._stopping:
            return
        self._stopping = True
        self._widget.setStatus('Stopping…')
        self._moduleCommChannel.sigStopScriptRun.emit(self._requestId)
        self._updateButtons()

    def _onRunStarted(self, result):
        self._runningIds.add(result.id)
        if self._isOwn(result):
            self._widget.setStatus('Running…')
        self._updateButtons()

    def _onRunFinished(self, result):
        self._runningIds.discard(result.id)
        if self._isOwn(result):
            self._requestId = None
            self._stopping = False
            self._widget.setStatus(*_describeResult(result))
        self._updateButtons()

    def _isOwn(self, result):
        return self._requestId is not None and result.request_id == self._requestId

    def _updateButtons(self):
        if self._requestId is not None:
            self._widget.setRunEnabled(False, 'This script is running.')
        elif self._runningIds:
            self._widget.setRunEnabled(False, 'Another script is running.')
        elif not self._moduleCommChannel.isModuleRegistered('imscripting'):
            self._widget.setRunEnabled(
                False, 'Scripts run in the Scripting module; enable it to run them from here.'
            )
        elif not self._scriptPath:
            self._widget.setRunEnabled(False, 'Choose a script first.')
        elif self._parseError:
            self._widget.setRunEnabled(False, self._parseError)
        else:
            self._widget.setRunEnabled(True, f'Run {os.path.basename(self._scriptPath)}')
        self._widget.setStopEnabled(self._requestId is not None and not self._stopping)

    # ------------------------------------------------------------------ #
    # Remembered state                                                    #
    # ------------------------------------------------------------------ #
    def _saveState(self):
        state = {'scriptPath': self._scriptPath, 'values': self._editedTexts}
        try:
            os.makedirs(os.path.dirname(_stateFilePath), exist_ok=True)
            with open(_stateFilePath, 'w', encoding='utf-8') as file:
                json.dump(state, file, indent=2)
        except OSError as e:
            self.__logger.warning(f'Could not save the script runner state: {e}')

    def closeEvent(self):
        self._saveState()
        super().closeEvent()


def _loadState():
    try:
        with open(_stateFilePath, encoding='utf-8') as file:
            state = json.load(file)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    if not isinstance(state.get('values'), dict):
        state['values'] = {}
    return state


def _describeResult(result):
    """ (status text, tooltip) for a finished ScriptRunResult. """
    status = getattr(result.status, 'value', result.status)
    if status == 'succeeded':
        seconds = (result.ended_at - result.started_at).total_seconds()
        return f'Finished in {seconds:.1f} s', ''
    if status == 'cancelled':
        if getattr(result, 'cleanup_timed_out', False):
            return 'Stopped (cleanup did not finish in time)', ''
        return 'Stopped', ''
    error = (result.error or '').strip()
    lastLine = error.splitlines()[-1] if error else 'unknown error'
    return f'Failed: {lastLine}', error


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
