"""Base class of deformable mirror managers."""

from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

import numpy as np

from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin

Coefficients = Union[Sequence[float], Mapping[int, float], np.ndarray]


class DeformableMirrorManager(DeviceManagerStatusMixin, ABC):
    """ A deformable mirror: one value per actuator, sent as a whole command.

    The manager keeps the last command it sent, the mirror's *flat* (the
    command that makes it flat, all zeros unless ``flatFile`` gives one) and,
    with ``modesFile``, a matrix of mode shapes: row ``k`` is the command for
    one unit of mode ``firstMode + k``, or of the ``k``-th of
    ``modeNumbers`` (for example ALPAO's ``<serial>-Z2C.csv``, a matrix the
    AO calibration script wrote, or Microscope_GUI's modal control matrix).

    Commands outside ``±maxCommand`` are refused, never clipped.

    Manager properties every mirror reads:
        - flatFile (str, optional): The flat command (.csv, .txt, .npy, or
          Microscope_GUI's .xlsx). All zeros without one.
        - modesFile (str, optional): The mode matrix (.csv, .txt, .npy or
          .tif): a row per mode, a column per actuator (or the transpose).
        - firstMode (int, default 1): The number of the matrix's first row,
          e.g. 2 for a Zernike matrix that starts at tip (Noll 2).
        - modeNumbers (list of int, optional): The number of every row, for
          a matrix whose modes are not consecutive (Microscope_GUI's modal
          control matrix: Noll 1-13, 16, 17, 22). Replaces firstMode.
        - maxCommand (float, default 1.0): The largest |value| sent to any
          actuator.
    """

    #: Mutating commands admitted through the resource registry: refused
    #: while a script or run reserves the mirror; ``owner=<token>`` passes.
    _GUARDED_METHODS = ('setActuators', 'flatten', 'zero', 'setModes', 'poke', 'reset')

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _guard(cls)

    @abstractmethod
    def __init__(self, mirrorInfo, name: str, **_lowLevelManagers):
        self._mirrorInfo = mirrorInfo
        self.__name = name
        properties = mirrorInfo.managerProperties
        self._maxCommand = float(properties.get('maxCommand', 1.0))
        if not self._maxCommand > 0:
            raise ValueError(f'{name}: maxCommand must be positive, not {self._maxCommand}.')
        self._firstMode = int(properties.get('firstMode', 1))
        numbers = properties.get('modeNumbers')
        self._modeNumbers = None if numbers is None else [int(n) for n in numbers]
        if self._modeNumbers is not None and len(set(self._modeNumbers)) != len(self._modeNumbers):
            raise ValueError(f'{name}: modeNumbers repeats a mode: {self._modeNumbers}')
        self._flatFile = properties.get('flatFile') or None
        self._modesFile = properties.get('modesFile') or None
        self._actuatorCount = 0
        self._command = np.zeros(0)
        self._flat = np.zeros(0)
        self._modes = None
        self._listeners: List[Callable[[str], None]] = []

    def _loadFiles(self, actuatorCount: int) -> None:
        """Read ``flatFile`` and ``modesFile`` for ``actuatorCount`` actuators
        (call once the count is known)."""
        from .mirror_files import read_command, read_matrix
        self._actuatorCount = int(actuatorCount)
        self._command = np.zeros(self._actuatorCount)
        self._flat = np.zeros(self._actuatorCount)
        if self._flatFile:
            self._flat = read_command(self._flatFile, self._actuatorCount)
            self._check(self._flat, 'flatFile')
        if self._modesFile:
            self._modes = self._orient(read_matrix(self._modesFile), self._modesFile)
            if self._modeNumbers is not None and len(self._modeNumbers) != self._modes.shape[0]:
                raise ValueError(f'{self.name}: modeNumbers names {len(self._modeNumbers)} modes, '
                                 f'{self._modesFile} has {self._modes.shape[0]}')

    def _orient(self, matrix: np.ndarray, source) -> np.ndarray:
        n = self._actuatorCount
        if matrix.shape[1] == n:
            return matrix
        if matrix.shape[0] == n:
            return matrix.T.copy()
        raise ValueError(f'{self.name}: {source} is {matrix.shape[0]} x {matrix.shape[1]}; '
                         f'neither side is the {n} actuators')

    # ------------------------------------------------------------ properties
    @property
    def name(self) -> str:
        """ Unique mirror name, defined in the setup. """
        return self.__name

    @property
    def actuatorCount(self) -> int:
        return self._actuatorCount

    @property
    def maxCommand(self) -> float:
        return self._maxCommand

    @property
    def command(self) -> np.ndarray:
        """ The last command sent (a copy). """
        return self._command.copy()

    @property
    def flat(self) -> np.ndarray:
        """ The flat command (a copy). """
        return self._flat.copy()

    @property
    def modeNumbers(self) -> List[int]:
        """ The numbers of the modes ``setModes`` takes (none without a mode
        matrix). """
        if self._modes is None:
            return []
        if self._modeNumbers is not None:
            return list(self._modeNumbers)
        return list(range(self._firstMode, self._firstMode + self._modes.shape[0]))

    @property
    def modeMatrix(self) -> Optional[np.ndarray]:
        """ The mode matrix, a row per mode (a copy), or None. """
        return None if self._modes is None else self._modes.copy()

    @property
    def isSimulated(self) -> bool:
        """ True unless real hardware is connected now. """
        if self.backendHolder is not None:
            return not self.backendIsReal
        return False

    # -------------------------------------------------------------- commands
    def setActuators(self, values) -> np.ndarray:
        """ Send ``values`` (one per actuator); returns what was sent. """
        return self._apply(values)

    def flatten(self) -> np.ndarray:
        """ Send the flat command. """
        return self._apply(self._flat)

    def zero(self) -> np.ndarray:
        """ Send all zeros (not the flat). """
        return self._apply(np.zeros(self._actuatorCount))

    def poke(self, actuator: int, value: float, *, base=None) -> np.ndarray:
        """ ``base`` (the flat by default) with ``value`` added to one
        actuator (numbered from 0). """
        index = int(actuator)
        if not 0 <= index < self._actuatorCount:
            raise IndexError(f'{self.name} has actuators 0-{self._actuatorCount - 1}, '
                             f'not {actuator}')
        command = self._base(base)
        command[index] += float(value)
        return self._apply(command)

    def setModes(self, coefficients: Coefficients, *, base=None) -> np.ndarray:
        """ ``base`` (the flat by default) plus the modes: a sequence in
        matrix-row order, or ``{mode number: amplitude}``. """
        return self._apply(self.modeCommand(coefficients, base=base))

    def reset(self) -> None:
        """ The driver's reset (all actuators to zero). """
        self._reset()
        self._command = np.zeros(self._actuatorCount)
        self._notify()

    def setFlat(self, values=None) -> np.ndarray:
        """ Make ``values`` (the last command by default) the flat, in
        memory; ``saveCommand`` writes it to a file. """
        flat = self._command.copy() if values is None else self._vector(values)
        self._check(flat, 'the flat')
        self._flat = flat
        return flat.copy()

    def saveCommand(self, path, values=None):
        """ Write ``values`` (the last command by default) to ``path``
        (.csv or .npy); returns the path. """
        from .mirror_files import write_command
        return write_command(path, self._command if values is None else self._vector(values))

    def modeCommand(self, coefficients: Coefficients, *, base=None) -> np.ndarray:
        """ The command ``setModes`` would send, without sending it. """
        if self._modes is None:
            raise RuntimeError(f'{self.name} has no mode matrix (set modesFile)')
        weights = np.zeros(self._modes.shape[0])
        if isinstance(coefficients, Mapping):
            rowOf = {number: row for row, number in enumerate(self.modeNumbers)}
            for mode, amplitude in coefficients.items():
                if int(mode) not in rowOf:
                    raise KeyError(f'{self.name} has modes {_ranges(self.modeNumbers)}, '
                                   f'not {mode}')
                weights[rowOf[int(mode)]] = float(amplitude)
        else:
            values = np.asarray(coefficients, dtype=float).ravel()
            if values.size > weights.size:
                raise ValueError(f'{values.size} amplitudes for {weights.size} modes')
            weights[:values.size] = values
        return self._base(base) + weights @ self._modes

    def addCommandListener(self, listener: Callable[[str], None]) -> None:
        """ ``listener(name)`` after every command sent (on the caller's
        thread). """
        self._listeners.append(listener)

    def removeCommandListener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -------------------------------------------------------------- helpers
    def _base(self, base) -> np.ndarray:
        return self._flat.copy() if base is None else self._vector(base)

    def _vector(self, values) -> np.ndarray:
        vector = np.asarray(values, dtype=float).ravel()
        if vector.size != self._actuatorCount:
            raise ValueError(f'{self.name} has {self._actuatorCount} actuators, '
                             f'not {vector.size} values')
        return vector

    def _check(self, vector: np.ndarray, what: str) -> None:
        if not np.all(np.isfinite(vector)):
            raise ValueError(f'{self.name}: {what} holds values that are not finite')
        worst = int(np.argmax(np.abs(vector))) if vector.size else 0
        if vector.size and abs(vector[worst]) > self._maxCommand:
            raise ValueError(f'{self.name}: {what} puts actuator {worst} at '
                             f'{vector[worst]:+.3f}, beyond ±{self._maxCommand:g}; nothing sent')

    def _apply(self, values) -> np.ndarray:
        vector = self._vector(values)
        self._check(vector, 'the command')
        self._requireConnected('mirror command')
        self._send(vector)
        self._command = vector.copy()
        self._notify()
        return vector.copy()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self.name)
            except Exception:
                pass

    def _lifecycleReinitialise(self) -> None:
        # A reconnected mirror starts from its reset state.
        self._command = np.zeros(self._actuatorCount)
        self._notify()

    @abstractmethod
    def _send(self, vector: np.ndarray) -> None:
        """ Send one value per actuator to the hardware. """

    @abstractmethod
    def _reset(self) -> None:
        """ Reset the hardware (all actuators to zero). """

    def finalize(self) -> None:
        """ Reset and close the mirror. """
        pass


def _guard(cls) -> None:
    from imswitch.imcontrol.model.resources import deformable_mirror_key, guard_methods

    def key(manager):
        try:
            name = manager.name
        except Exception:
            name = f'{type(manager).__name__}@{id(manager):x}'
        return deformable_mirror_key(name)

    guard_methods(cls, key, cls._GUARDED_METHODS)


# The base class defines the commands itself, so wrap them here too (its
# subclasses are wrapped by __init_subclass__ when they override one).
_guard(DeformableMirrorManager)


def _ranges(numbers) -> str:
    """``[1, 2, 3, 5]`` as ``1-3, 5``."""
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(f'{start}' if start == n else f'{start}-{n}')
            start = None
    return ', '.join(parts)


def modeDictionary(manager: DeformableMirrorManager, coefficients) -> Dict[int, float]:
    """ ``coefficients`` (a sequence in row order or a mapping) as
    ``{mode number: amplitude}``. """
    if isinstance(coefficients, Mapping):
        return {int(k): float(v) for k, v in coefficients.items()}
    return {mode: float(value) for mode, value in zip(manager.modeNumbers, coefficients)}


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
