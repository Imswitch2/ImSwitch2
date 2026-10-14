import os

from imswitch.imcommon.model import dirtools
from imswitch.imscripting.model import ScriptExecutor, ScriptStore, ScriptEntry, cellAt
from imswitch.imscripting.model.acquisition_script import acquisitionScript
from imswitch.imcommon.model import initLogger
from imswitch.imscripting.view import guitools
from .basecontrollers import ImScrWidgetController


class EditorController(ImScrWidgetController):
    """ Connected to EditorView. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__logger = initLogger(self, tryInheritParent=True)

        self.scriptExecutor = ScriptExecutor(self._scriptScope)
        self.scriptStore = ScriptStore()
        self.loadingFile = False

        # Connect ScriptExecutor signals
        self.scriptExecutor.sigOutputAppended.connect(self._commChannel.sigOutputAppended)
        self.scriptExecutor.sigExecutionStarted.connect(self._onExecutionStarted)
        self.scriptExecutor.sigExecutionFinished.connect(self._onExecutionFinished)

        # Connect CommunicationChannel signals
        self._commChannel.sigNewFile.connect(self.newFile)
        self._commChannel.sigNewFromCurrentSettings.connect(self.newFromCurrentSettings)
        self._commChannel.sigOpenFile.connect(self.openFile)
        self._commChannel.sigOpenFileFromPath.connect(self.openFile)
        self._commChannel.sigSaveFile.connect(self.saveFile)
        self._commChannel.sigSaveAsFile.connect(self.saveAsFile)
        self._commChannel.sigRunScript.connect(self.runScript)

        # Connect EditorView signals
        self._widget.sigRunAllClicked.connect(self.runCurrentCode)
        self._widget.sigRunSelectedClicked.connect(self.runSelection)
        self._widget.sigRunCellClicked.connect(self.runCell)
        self._widget.sigResetSessionClicked.connect(self.resetSession)
        self._widget.sigStopClicked.connect(self.stopExecution)
        self._widget.sigTextChanged.connect(self.textChanged)
        self._widget.sigInstanceCloseClicked.connect(self.instanceCloseClicked)

        # Basic setup
        self.newFile()

    def newFile(self):
        """ Creates a new file. """
        instance = self._widget.addEditor(_untitledFileName)
        self.scriptStore[instance.getID()] = ScriptEntry(filePath=None)

    def newFromCurrentSettings(self):
        """ Opens a new, unsaved script that sets the detectors, lasers and
        scan as ImControl has them now and acquires as the Recording widget
        is set (see model.acquisition_script). """
        imcontrol = getattr(self._scriptScope.get('api'), 'imcontrol', None)
        if imcontrol is None:
            guitools.showWarning(self._widget, 'New script from current settings',
                                 'ImControl is not loaded, so there are no settings to copy.')
            return None
        try:
            text = acquisitionScript(imcontrol)
        except Exception as error:
            self.__logger.error('Could not copy the acquisition', exc_info=True)
            guitools.showWarning(self._widget, 'New script from current settings',
                                 f'Could not read the current settings: {error}')
            return None
        instance = self._widget.addEditor(f'{_untitledFileName}*')
        self.loadingFile = True
        try:
            instance.setText(text)
        finally:
            self.loadingFile = False
        entry = ScriptEntry(filePath=None)
        entry.unsaved = True
        self.scriptStore[instance.getID()] = entry
        return instance

    def openFile(self, scriptPath=None):
        """ Opens an existing file. If scriptPath is None, the user will
        specify which file will be opened. """

        if scriptPath is None:
            scriptPath = guitools.askForFilePath(
                self._widget, 'Open script', defaultFolder=_scriptsFolderPath, nameFilter='*.py'
            )
            if not scriptPath:
                return

        for existingInstanceID, existingEntry in self.scriptStore:
            if scriptPath == existingEntry.filePath:
                # File already open
                self._widget.setCurrentInstanceByID(existingInstanceID)
                return

        scriptEntry = ScriptEntry.loadFromFile(scriptPath)
        instance = self._widget.addEditor(os.path.basename(scriptPath))

        self.loadingFile = True
        try:
            instance.setText(scriptEntry.code)
        finally:
            self.loadingFile = False

        self.scriptStore[instance.getID()] = scriptEntry

    def saveFile(self):
        """ Saves the current file. If it's not already saved to a file, the
        user will specify the path to save to. """
        instance = self._widget.getCurrentInstance()
        scriptEntry = self.scriptStore[instance.getID()]
        if scriptEntry.isSavedToFile():
            scriptEntry.code = instance.getText()
            scriptEntry.save()
            self._widget.setEditorName(instance.getID(), os.path.basename(scriptEntry.filePath))
        else:
            self.saveAsFile()

    def saveAsFile(self):
        """ Saves the current file to the path specified by the user. """
        instance = self._widget.getCurrentInstance()

        scriptPath = guitools.askForFilePath(
            self._widget, 'Save script', defaultFolder=_scriptsFolderPath, nameFilter='*.py',
            isSaving=True
        )
        if not scriptPath:
            return

        self.scriptStore[instance.getID()].filePath = scriptPath
        self.saveFile()

    def runCurrentCode(self, instanceID, code):
        """ Run all: executes the specified code in a fresh namespace.
        instanceID is the ID of the instance that contains the code. """
        self._run(instanceID, code, session=False)

    def runSelection(self, instanceID, code, firstLine=0):
        """ Run selection: executes the code in the session. ``firstLine`` is
        the 0-based editor line it starts at. """
        self._run(instanceID, code, session=True, firstLine=firstLine)

    def runCell(self, instanceID, advance=False):
        """ Run cell: executes the cell around the cursor -- from a ``# %%``
        line to the next -- in the session; ``advance`` then puts the cursor
        on the next cell. """
        instance = self._widget.getInstanceByID(instanceID)
        if instance is None:
            return
        text = instance.getText()
        first, end, code = cellAt(text, instance.cursorLine())
        if not self._run(instanceID, code, session=True, firstLine=first):
            return
        if advance and end < len(text.split('\n')):
            instance.setCursorLine(end)

    def _run(self, instanceID, code, *, session, firstLine=0):
        if self.scriptExecutor.isExecuting():
            if not guitools.askYesNoQuestion(self._widget,
                                             'Warning: Already Executing',
                                             'A script is already running; it will be terminated if'
                                             ' you proceed. Do you want to proceed?'):
                return False

        self._commChannel.sigExecutionStarted.emit()
        self.scriptExecutor.execute(self.scriptStore[instanceID].filePath, code,
                                    session=session, firstLine=firstLine)
        return True

    def resetSession(self, instanceID=None):
        """ Forgets the variables of Run cell, Run selection and the console,
        after asking when there are any. Refused while a script runs. """
        if self.scriptExecutor.isBusy():
            guitools.showWarning(
                self._widget, 'Reset session',
                'A script is running; reset the session once it has ended.'
            )
            return False
        names = list(self.scriptExecutor.session.userVariables())
        if names and not guitools.askYesNoQuestion(
                self._widget, 'Reset session?',
                f'Forget {len(names)} variable{"s" if len(names) != 1 else ""} '
                f'({", ".join(names[:5])}{", ..." if len(names) > 5 else ""})?'):
            return False
        return self.scriptExecutor.resetSession()

    def stopExecution(self):
        """ Requests the currently running script to stop. Does nothing if no
        script is running. Never blocks: the script ends at its next
        cooperative wait (or is interrupted), and the editor shows
        "Stopping…" until it has. """

        if not self.scriptExecutor.isExecuting():
            return

        if self.scriptExecutor.isStopping():
            return  # Already stopping; the watchdog escalates by itself

        if not guitools.askYesNoQuestion(self._widget,
                                         'Stop execution?',
                                         'Are you sure that you want to stop the script?'):
            return

        self.scriptExecutor.cancel()
        self._widget.setStopping(True)

    def _onExecutionStarted(self, result):
        self._widget.setRunning(True)

    def _onExecutionFinished(self, result):
        self._widget.setStopping(False)
        self._widget.setRunning(self.scriptExecutor.isBusy())
        # The module-level signal carries no payload (the old signal-to-signal
        # connection dropped the result silently).
        self._moduleCommChannel.sigExecutionFinished.emit()

    def closeEvent(self):
        """ Module close: stop a running script with a bound. The application
        shutdown phase (ImScrMainController.prepareShutdown) normally runs
        first; this is the fallback for a module closed on its own. """
        self.scriptExecutor.shutdown()

    def textChanged(self, instanceID):
        if self.loadingFile or instanceID not in self.scriptStore:
            return  # This happens when the file has not been fully loaded yet

        self.scriptStore[instanceID].unsaved = True
        self._widget.setEditorName(instanceID, f'{self.getScriptName(instanceID)}*')

    def instanceCloseClicked(self, instanceID):
        if not self.verifyNotUnsaved(instanceID):
            return

        self._widget.closeInstance(instanceID)
        del self.scriptStore[instanceID]

    def verifyNotUnsaved(self, instanceID):
        if self.scriptStore[instanceID].unsaved:
            return guitools.askYesNoQuestion(self._widget,
                                             'Warning: Unsaved Changes',
                                             'Are you sure you want to close the file? There are'
                                             ' unsaved changes that will be lost.')

        return True

    def getScriptName(self, instanceID):
        """ Returns the name that should be used for the editor with the
        specified instance ID. """
        scriptEntry = self.scriptStore[instanceID]
        if scriptEntry.isSavedToFile():
            return os.path.basename(scriptEntry.filePath)
        else:
            return _untitledFileName

    def runScript(self, code):
        """Execute script code from external/remote source without a known file path."""
        self.scriptExecutor.execute(None, code)

_untitledFileName = '(untitled)'
_scriptsFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'scripts')


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
