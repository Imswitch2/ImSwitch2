from typing import Any, Dict, List, Optional, Union

import numpy as np

from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcontrol.model.devices.status import device_usable, not_connected_reason
from imswitch.imcontrol.model.managers.deformableMirrors.DeformableMirrorManager import (
    _ranges, modeDictionary,
)
from ..basecontrollers import ImConWidgetController

_attrCategory = 'DeformableMirror'


class DeformableMirrorController(ImConWidgetController):
    """ The deformable mirrors: the DeformableMirror panel when the setup
    shows it, and the scripting functions either way.

    Every command sent is recorded with the frames
    (``DeformableMirror:<name>:Command``, and ``...:Modes`` when it was a sum
    of modes), as the lasers' and positioners' values are. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)
        self._lastModes: Dict[str, Dict[int, float]] = {}
        self._pendingModes: Dict[str, Dict[int, float]] = {}
        self._listeners = {}
        for name, mirror in self._mirrors():
            if self._widget is not None:
                self._widget.addMirror(name, mirror.actuatorCount, mirror.maxCommand,
                                       mirror.modeNumbers)
            listener = (lambda n: lambda _name: self._commandSent(n))(name)
            mirror.addCommandListener(listener)
            self._listeners[name] = listener
            self._record(name)
        if self._widget is not None:
            self._widget.sigFlatten.connect(lambda name: self._fromPanel(name, self.flattenMirror))
            self._widget.sigZero.connect(lambda name: self._fromPanel(name, self.zeroMirror))
            self._widget.sigReset.connect(lambda name: self._fromPanel(name, self.resetMirror))
            self._widget.sigSaveAsFlat.connect(self._useAsFlat)
            self._widget.sigPoke.connect(lambda name, actuator, value: self._fromPanel(
                name, self.pokeMirrorActuator, actuator, value))
            self._widget.sigApplyModes.connect(lambda name, modes: self._fromPanel(
                name, self.setMirrorModes, modes))
            self._widget.sigLoadCommand.connect(lambda name, path: self._fromPanel(
                name, self.loadMirrorCommand, path))
            self._widget.sigSaveCommand.connect(self._saveFromPanel)
            for name, _ in self._mirrors():
                self._refreshPanel(name)

        self._deviceLifecycleListener = None
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None:
            self._deviceLifecycleListener = self._deviceLifecycleChanged
            lifecycleService.addListener(self._deviceLifecycleListener)

    def closeEvent(self):
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None and self._deviceLifecycleListener is not None:
            lifecycleService.removeListener(self._deviceLifecycleListener)
            self._deviceLifecycleListener = None
        for name, mirror in self._mirrors():
            listener = self._listeners.pop(name, None)
            if listener is not None:
                mirror.removeCommandListener(listener)

    # ------------------------------------------------------------- helpers
    def _mirrors(self):
        manager = getattr(self._master, 'deformableMirrorsManager', None)
        return list(manager) if manager is not None else []

    def _mirror(self, name):
        manager = getattr(self._master, 'deformableMirrorsManager', None)
        if manager is None or name not in dict(self._mirrors()):
            known = ', '.join(n for n, _ in self._mirrors()) or 'none'
            raise KeyError(f'No deformable mirror named "{name}" (the setup has: {known})')
        return manager[name]

    def _commandSent(self, name):
        # Modes are recorded only for the command setMirrorModes sent.
        self._lastModes[name] = self._pendingModes.pop(name, None)
        self._invokeOnControllerThreadIfNeeded(lambda: (self._record(name),
                                                       self._refreshPanel(name)))

    def _record(self, name):
        mirror = self._mirror(name)
        self._commChannel.sharedAttrs[(_attrCategory, name, 'Command')] = [
            float(v) for v in mirror.command]
        modes = self._lastModes.get(name)
        self._commChannel.sharedAttrs[(_attrCategory, name, 'Modes')] = (
            {str(k): v for k, v in modes.items()} if modes else {})

    def _refreshPanel(self, name):
        if self._widget is None:
            return
        mirror = self._mirror(name)
        self._widget.setCommand(name, mirror.command, mirror.flat)
        usable = device_usable(mirror)
        self._widget.setMirrorUsable(name, usable, '' if usable else not_connected_reason(mirror))
        if mirror.isSimulated and usable:
            state = 'Mock mirror'
        elif usable:
            state = 'Connected'
        else:
            state = 'Not connected'
        self._widget.setStatus(name, f'{state}; {mirror.actuatorCount} actuators, '
                                     f'commands within +/-{mirror.maxCommand:g}'
                               + (f'; modes {_ranges(mirror.modeNumbers)}'
                                  if mirror.modeNumbers else '; no mode matrix'))

    def _fromPanel(self, name, function, *args):
        try:
            function(name, *args)
        except Exception as e:
            self.__logger.warning(f'{name}: {e}')
            self._widget.showMessage(name, str(e), error=True)
        else:
            self._widget.showMessage(name, '')

    def _useAsFlat(self, name):
        try:
            self.setMirrorFlat(name)
        except Exception as e:
            self._widget.showMessage(name, str(e), error=True)
        else:
            self._widget.showMessage(name, 'The current command is the flat now '
                                           '(Save... writes it to a file).')
            self._refreshPanel(name)

    def _saveFromPanel(self, name, path):
        try:
            written = self.saveMirrorCommand(name, path)
        except Exception as e:
            self._widget.showMessage(name, str(e), error=True)
        else:
            self._widget.showMessage(name, f'Saved to {written}')

    def _deviceLifecycleChanged(self, result):
        names = {
            device_id.name for device_id in getattr(result, 'affected_device_ids', ())
            if getattr(device_id, 'kind', None) == 'deformable_mirror'
        }
        known = {name for name, _ in self._mirrors()}
        for name in names & known:
            self._invokeOnControllerThreadIfNeeded(lambda n=name: self._refreshPanel(n))

    def _sent(self, name):
        return [float(v) for v in self._mirror(name).command]

    # ----------------------------------------------------------------- API
    @APIExport()
    def getDeformableMirrorNames(self) -> List[str]:
        """ Returns the names of the setup's deformable mirrors. """
        return [name for name, _ in self._mirrors()]

    @APIExport()
    def getMirrorInfo(self, name: str) -> Dict[str, Any]:
        """ Returns what is known about the mirror ``name``: ``actuators``,
        ``maxCommand``, ``modes`` (the mode numbers ``setMirrorModes``
        takes), ``simulated`` and ``usable`` (commands reach it). """
        mirror = self._mirror(name)
        return dict(actuators=mirror.actuatorCount, maxCommand=mirror.maxCommand,
                    modes=mirror.modeNumbers, simulated=mirror.isSimulated,
                    usable=device_usable(mirror))

    @APIExport()
    def getMirrorCommand(self, name: str) -> List[float]:
        """ Returns the last command sent to the mirror ``name``, one value
        per actuator. """
        return [float(v) for v in self._mirror(name).command]

    @APIExport()
    def getMirrorFlat(self, name: str) -> List[float]:
        """ Returns the flat command of the mirror ``name``. """
        return [float(v) for v in self._mirror(name).flat]

    @APIExport()
    def getMirrorModeMatrix(self, name: str) -> Optional[List[List[float]]]:
        """ Returns the mirror's mode matrix (a row per mode, a column per
        actuator), or None without one. """
        matrix = self._mirror(name).modeMatrix
        return None if matrix is None else matrix.tolist()

    @APIExport(runOnUIThread=True)
    def setMirrorCommand(self, name: str, values: List[float]) -> List[float]:
        """ Sends ``values`` (one per actuator) to the mirror ``name`` and
        returns them. A value beyond ``maxCommand`` refuses the whole command
        (ValueError); nothing is sent. """
        self._mirror(name).setActuators(values)
        return self._sent(name)

    @APIExport(runOnUIThread=True)
    def flattenMirror(self, name: str) -> List[float]:
        """ Sends the flat command to the mirror ``name``. """
        self._mirror(name).flatten()
        return self._sent(name)

    @APIExport(runOnUIThread=True)
    def zeroMirror(self, name: str) -> List[float]:
        """ Sends all zeros (not the flat) to the mirror ``name``. """
        self._mirror(name).zero()
        return self._sent(name)

    @APIExport(runOnUIThread=True)
    def resetMirror(self, name: str) -> None:
        """ The mirror driver's reset: all actuators of ``name`` to zero. """
        self._mirror(name).reset()
        self._sent(name)

    @APIExport(runOnUIThread=True)
    def pokeMirrorActuator(self, name: str, actuator: int, value: float) -> List[float]:
        """ Sends the flat with ``value`` added to one actuator (numbered
        from 0) of the mirror ``name``. """
        self._mirror(name).poke(actuator, value)
        return self._sent(name)

    @APIExport(runOnUIThread=True)
    def setMirrorModes(self, name: str,
                       amplitudes: Union[Dict[int, float], List[float]]) -> List[float]:
        """ Sends the flat plus a sum of modes to the mirror ``name``:
        ``{mode number: amplitude}``, or a list in the matrix's row order.
        Needs the mirror's ``modesFile``. """
        mirror = self._mirror(name)
        modes = modeDictionary(mirror, amplitudes)
        self._pendingModes[name] = modes
        try:
            mirror.setModes(amplitudes)
        finally:
            self._pendingModes.pop(name, None)
        if self._widget is not None:
            self._widget.setModeValues(name, modes)
        return self._sent(name)

    @APIExport(runOnUIThread=True)
    def setMirrorFlat(self, name: str, values: Optional[List[float]] = None) -> List[float]:
        """ Makes ``values`` (the last command sent, by default) the flat of
        the mirror ``name``, in memory; ``saveMirrorCommand`` writes it to a
        file. Nothing is sent. """
        flat = self._mirror(name).setFlat(values)
        self._refreshPanel(name)
        return [float(v) for v in flat]

    @APIExport(runOnUIThread=True)
    def loadMirrorCommand(self, name: str, path: str) -> List[float]:
        """ Sends a command read from ``path`` (.csv, .txt, .npy or the old
        GUI's .xlsx) to the mirror ``name``. """
        from imswitch.imcontrol.model.managers.deformableMirrors.mirror_files import read_command
        mirror = self._mirror(name)
        mirror.setActuators(read_command(path, mirror.actuatorCount))
        return self._sent(name)

    @APIExport()
    def saveMirrorCommand(self, name: str, path: str,
                          values: Optional[List[float]] = None) -> str:
        """ Writes ``values`` (the last command sent, by default) of the
        mirror ``name`` to ``path`` (.csv or .npy); returns the path. """
        return str(self._mirror(name).saveCommand(
            path, None if values is None else np.asarray(values, dtype=float)))


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
