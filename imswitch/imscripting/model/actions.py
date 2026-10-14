import importlib.util
import logging
import os
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from imswitch.imcommon.framework import Signal, FrameworkUtils
from imswitch.imcommon.model import (
    APIExport, cancellableSleep, checkpoint, generateAPI, initLogger,
)


class _Actions:
    """ Additional functions intended to be made available through the
    scripting API. """

    def __init__(self, scriptScope, scriptPath=None):
        self._scriptScope = scriptScope
        self._scriptPath = scriptPath
        self._scriptLogger = initLogger('script')

    @APIExport()
    def importScript(self, path: str) -> Any:
        """ Imports the script at the specified path (either absolute or
        relative to the main script) and returns it as a module variable. """

        # Convert to absolute path
        if self._scriptPath is not None:
            path = os.path.join(os.path.dirname(self._scriptPath), path)

        # Load module
        spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        # Set script/actions scope
        script.__dict__.update(self._scriptScope)
        script.__dict__.update(getActionsScope(self._scriptScope, self._scriptPath))

        # Return
        return script

    @APIExport()
    def getScriptDirPath(self) -> str:
        """ Returns the path to the directory containing the running script.
        """
        return os.path.dirname(self._scriptPath)

    @APIExport()
    def getLogger(self) -> logging.LoggerAdapter:
        """ Returns a logger instance that can be used to print formatted
        messages to the console. """
        return self._scriptLogger

    @APIExport()
    def getWaitForSignal(self, signal: Signal,
                         pollIntervalSeconds: float = 0.05,
                         timeout: Optional[float] = None) -> Callable[[], None]:
        """ Returns a function that will wait for the specified signal to emit.
        The returned function will wait until the signal has been emitted
        since its creation, so **create it before triggering the action** that
        emits the signal (e.g. before ``api.imcontrol.runScan()``); an
        emission that happened before creation is not seen.

        The returned function raises ``TimeoutError`` if ``timeout`` seconds
        pass without an emission (``None`` waits indefinitely) and
        ``OperationCancelled`` if the script is stopped. It accepts an
        optional ``timeout`` argument of its own that overrides the one given
        here. The polling interval defaults to 50 ms. """

        emitted = False

        def setEmitted(*_args, **_kwargs):
            nonlocal emitted
            emitted = True

        signal.connect(setEmitted)
        connected = True

        def wait(timeout: Optional[float] = timeout) -> None:
            nonlocal connected
            deadline = None if timeout is None else time.monotonic() + float(timeout)
            try:
                while not emitted:
                    checkpoint()
                    FrameworkUtils.processPendingEventsCurrThread()
                    if emitted:
                        break
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f'Timed out after {timeout:g} s waiting for the '
                                f'signal to be emitted'
                            )
                        time.sleep(min(pollIntervalSeconds, remaining))
                    else:
                        time.sleep(pollIntervalSeconds)
            finally:
                close()

        def close() -> None:
            """ Stops listening without waiting. """
            nonlocal connected
            if connected:
                connected = False
                try:
                    signal.disconnect(setEmitted)
                except (TypeError, RuntimeError):
                    pass  # Already disconnected or the emitter is gone

        wait.close = close
        return wait

    @APIExport()
    def callAndWaitForSignal(self, signal: Signal, func: Callable, *args,
                             timeout: Optional[float] = None, **kwargs) -> Any:
        """ Creates a waiter for ``signal``, then calls ``func(*args,
        **kwargs)``, then waits for the signal. Returns what ``func``
        returned. This is the safe way to call an API function and wait for
        the event it causes, because the waiter exists before the call: a
        signal emitted synchronously inside the call is caught too.

        Example: ``callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
        api.imcontrol.stopRecording, timeout=60)``. Raises ``TimeoutError``
        after ``timeout`` seconds and ``OperationCancelled`` if the script is
        stopped. """
        wait = self.getWaitForSignal(signal, timeout=timeout)
        try:
            result = func(*args, **kwargs)
        except BaseException:
            wait.close()
            raise
        wait()
        return result

    @APIExport()
    def runScanAndWait(self, timeout: Optional[float] = None,
                       source: Optional[str] = None) -> None:
        """ Starts one scan through ``api.imcontrol.runScan`` and waits for
        exactly that scan to end, whatever the timing of its signals. Raises
        ``RuntimeError`` if the start was refused (``ScanRequestRejectedError``)
        or the scan ended unsuccessfully, ``TimeoutError`` after ``timeout``
        seconds, and ``OperationCancelled`` immediately when the script is
        stopped (the running scan then finishes on its own). ``source`` picks
        the scan controller on rigs with several. """
        api = self._scriptScope.get('api')
        imcontrol = getattr(api, 'imcontrol', None)
        if imcontrol is None:
            raise RuntimeError('runScanAndWait requires the imcontrol module')
        handle = imcontrol.runScan(source) if source is not None else imcontrol.runScan()
        if not handle.wait(timeout):
            raise TimeoutError(
                f'Timed out after {timeout:g} s waiting for scan {handle.requestId} '
                'to end'
            )
        if not handle.successful:
            raise RuntimeError(f'Scan {handle.requestId} failed: {handle.message}')

    @APIExport()
    def sleep(self, seconds: float) -> None:
        """ Sleeps for the specified number of seconds. Unlike ``time.sleep``,
        this returns immediately (raising ``OperationCancelled``) when the
        script is stopped. """
        cancellableSleep(seconds)

    @APIExport()
    def waitUntil(self, predicate: Callable[[], bool],
                  timeout: Optional[float] = None,
                  pollIntervalSeconds: float = 0.05) -> None:
        """ Waits until ``predicate()`` returns a true value. Raises
        ``TimeoutError`` after ``timeout`` seconds (``None`` waits
        indefinitely) and ``OperationCancelled`` if the script is stopped. """
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            checkpoint()
            if predicate():
                return
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f'Timed out after {timeout:g} s waiting for the condition'
                    )
                time.sleep(min(pollIntervalSeconds, remaining))
            else:
                time.sleep(pollIntervalSeconds)

    @APIExport()
    def show(self, image: Any, name: Optional[str] = None,
             scale: Optional[Sequence[float]] = None) -> None:
        """ Shows an array in ImControl's image view, as a layer called
        ``name`` ('script' when not given). Showing under the same name again
        replaces that layer's data, so a loop can update one picture. The
        array is copied, so changing it afterwards does not change the
        picture. A stack ``(frames, rows, columns)`` gets a slider. ``scale``
        is the pixel size per axis, last axes last (``(0.1, 0.1)`` for 0.1 µm
        pixels). A dict of arrays, as recordScanToMemory returns for several
        detectors, shows one layer per entry. Raises RuntimeError when no
        image view is loaded (the Image widget of ImControl). """
        import numpy as np

        if isinstance(image, dict):
            for key, value in image.items():
                self.show(value, f'{name}: {key}' if name else str(key), scale)
            return
        array = np.array(image, copy=True)
        if array.ndim < 2:
            raise ValueError(
                f'show() displays images, arrays of two or more dimensions; '
                f'this one has shape {array.shape}'
            )
        if array.dtype == bool:
            array = array.astype(np.uint8)
        channel = self._scriptScope.get('moduleCommChannel')
        signal = getattr(channel, 'sigShowImage', None)
        try:
            listening = signal is not None and channel.receivers(signal) > 0
        except (AttributeError, TypeError):
            listening = signal is not None
        if not listening:
            raise RuntimeError(
                "Nothing can show the image: load ImControl's Image widget "
                '(availableWidgets in the setup file)'
            )
        signal.emit(str(name or 'script'), array,
                    None if scale is None else tuple(float(v) for v in scale))


    @APIExport()
    def temporaryDetectorSettings(self, detector: str,
                                  settings: Optional[Mapping[str, Any]] = None):
        """ A context manager: sets ``settings`` ({parameter name: value},
        names as in the Settings widget) on the detector, and on leaving the
        ``with`` block -- also when it raises or the script is stopped --
        puts back the values those parameters had::

            with temporaryDetectorSettings('Camera', {'Set exposure time': 0.01}):
                frames = api.imcontrol.recordFramesToMemory('Camera', 10)

        Without ``settings`` it only puts back what the block changes of the
        detector's editable parameters. """
        imcontrol = self._imcontrol()
        if settings is None:
            names = [name for name, p in imcontrol.getDetectorParameters(detector).items()
                     if p.get('editable') and not p.get('configOwned')]
        else:
            names = list(settings)
        previous = {name: imcontrol.getDetectorParameter(detector, name) for name in names}

        def restore():
            return [(f'{detector}: {name}',
                     lambda name=name, value=value:
                     imcontrol.setDetectorParameter(detector, name, value))
                    for name, value in previous.items()
                    if imcontrol.getDetectorParameter(detector, name) != value]

        def apply():
            for name, value in (settings or {}).items():
                imcontrol.setDetectorParameter(detector, name, value)

        return _Restoring(apply, restore, f'the settings of {detector}', self._scriptLogger)

    @APIExport()
    def temporaryScanParameters(self, parameters: Optional[Mapping[str, Any]] = None):
        """ A context manager: puts ``parameters`` (a dict from
        getScanParameters, edited) into the Scan widget, and the scan it held
        back on leaving the ``with`` block, also when the block raises or the
        script is stopped. Without ``parameters`` it only puts back what the
        block changes::

            params = api.imcontrol.getScanParameters()
            params['analogParameterDict']['axis_step_size'][0] = 0.05
            with temporaryScanParameters(params):
                frames = api.imcontrol.recordScanToMemory('Camera')
        """
        imcontrol = self._imcontrol()
        previous = imcontrol.getScanParameters()

        def apply():
            if parameters is not None:
                imcontrol.setScanParameters(parameters)

        def restore():
            return [('the scan parameters', lambda: imcontrol.setScanParameters(previous))]

        return _Restoring(apply, restore, 'the scan parameters', self._scriptLogger)

    @APIExport()
    def temporaryLaserSettings(self, settings: Mapping[str, Any]):
        """ A context manager: sets the lasers in ``settings`` -- {laser: its
        value} or {laser: {'value': ..., 'on': True}} -- and on leaving the
        ``with`` block, also when it raises or the script is stopped, puts
        back each one's value and whether it was on. A laser that was off is
        switched off before its value is restored, one that was on gets its
        value first::

            with temporaryLaserSettings({'488': {'value': 5.0, 'on': True}}):
                frames = api.imcontrol.recordScanToMemory('APD')
        """
        imcontrol = self._imcontrol()
        previous = {}
        for laser in settings:
            binary = imcontrol.getLaserInfo(laser).get('binary')
            previous[laser] = (bool(imcontrol.isLaserActive(laser)),
                               None if binary else imcontrol.getLaserValue(laser))

        def apply():
            for laser, wanted in settings.items():
                wanted = wanted if isinstance(wanted, Mapping) else {'value': wanted}
                if 'value' in wanted:
                    imcontrol.setLaserValue(laser, wanted['value'])
                if 'on' in wanted:
                    imcontrol.setLaserActive(laser, bool(wanted['on']))

        def restore():
            steps = []
            for laser, (wasOn, value) in previous.items():
                if not wasOn:
                    steps.append((f'{laser}: off',
                                  lambda laser=laser: imcontrol.setLaserActive(laser, False)))
                if value is not None:
                    steps.append((f'{laser}: value', lambda laser=laser, value=value:
                                  imcontrol.setLaserValue(laser, value)))
                if wasOn:
                    steps.append((f'{laser}: on',
                                  lambda laser=laser: imcontrol.setLaserActive(laser, True)))
            return steps

        return _Restoring(apply, restore, 'the laser settings', self._scriptLogger)

    @APIExport()
    def returningToPositions(self, stages: Optional[Sequence[str]] = None,
                             rotators: Optional[Sequence[str]] = None):
        """ A context manager: remembers where ``stages`` (positioner names;
        every axis of each) and ``rotators`` are, and moves them back there on
        leaving the ``with`` block, also when it raises or the script is
        stopped. Separate from the settings managers on purpose: putting a
        stage back is a move, and a script decides on it by itself::

            with returningToPositions(stages=['X', 'Y']):
                for x in range(5):
                    api.imcontrol.movePositioner('X', 'X', 10)
                    ...
        """
        imcontrol = self._imcontrol()
        positions = imcontrol.getPositionerPositions() if stages else {}
        stagePositions = {stage: dict(positions[stage]) for stage in (stages or [])}
        angles = {rotator: imcontrol.getRotatorPosition(rotator) for rotator in (rotators or [])}

        def restore():
            steps = []
            for stage, axes in stagePositions.items():
                for axis, position in axes.items():
                    steps.append((f'{stage} {axis}', lambda stage=stage, axis=axis, position=position:
                                  imcontrol.setPositioner(stage, axis, position)))
            for rotator, angle in angles.items():
                steps.append((rotator, lambda rotator=rotator, angle=angle:
                              imcontrol.setRotatorPosition(rotator, angle)))
            return steps

        return _Restoring(lambda: None, restore, 'the positions', self._scriptLogger)

    def _imcontrol(self):
        imcontrol = getattr(self._scriptScope.get('api'), 'imcontrol', None)
        if imcontrol is None:
            raise RuntimeError('This needs the imcontrol module')
        return imcontrol


class _Restoring:
    """A ``with`` block that applies something on entry and restores on
    exit. ``restore()`` returns the steps to take, ``(what, callable)``; each
    runs even when an earlier one failed. When the block itself raised (or
    was stopped), a failed restore is logged and the block's exception goes
    on; otherwise a failed restore raises RuntimeError naming what was not
    put back."""

    def __init__(self, apply, restore, what, logger):
        self._apply, self._restore, self._what, self._logger = apply, restore, what, logger

    def __enter__(self):
        try:
            self._apply()
        except BaseException:
            self._restoreAll(raised=True)
            raise
        return self

    def __exit__(self, excType, exc, tb):
        self._restoreAll(raised=excType is not None)
        return False

    def _restoreAll(self, *, raised):
        failed = []
        try:
            steps = self._restore()
        except Exception as error:
            steps, failed = [], [(self._what, error)]
        for what, step in steps:
            try:
                step()
            except Exception as error:
                failed.append((what, error))
        if not failed:
            return
        message = (f'Could not put back {self._what}: '
                   + '; '.join(f'{what} ({error})' for what, error in failed))
        if raised:
            self._logger.error(message)
        else:
            raise RuntimeError(message) from failed[0][1]


def getActionsScope(otherScope, scriptPath=None):
    """ Returns the script scope for the actions. """
    return generateAPI([_Actions(otherScope, scriptPath)])._asdict()


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
