import threading

import numpy as np

from .ScanManagerBase import SuperScanManager

# Per-thread capture of the commands a scan handler would send, so the same
# handlers serve both the synchronous ``runScan`` and ``runScanAsync``.
_capture = threading.local()


class ScanStartHandle:
    """One asynchronous scan start: parameter upload, boundary, scan command.

    ``onFinished(ok, message)`` is called exactly once, on the manager's
    thread, when the scan command has been written (``ok``) or the start
    failed or was cancelled. :meth:`cancel` stops the upload and, if the
    parameters are not yet complete, keeps the boundary and the scan command
    from being sent at all.
    """

    def __init__(self, onFinished):
        self._onFinished = onFinished
        self._job = None
        self._cancelled = False
        self._finished = False

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True
        if self._job is not None:
            self._job.cancel()

    def _setJob(self, job):
        self._job = job
        if self._cancelled:
            job.cancel()

    def _finish(self, ok, message=''):
        if self._finished:
            return
        self._finished = True
        self._onFinished(ok, message)


class ScanManagerTriggerScope(SuperScanManager):
    """ ScanManager for TriggerScope-based scan sequences.

    Instead of generating NI-DAQ waveform arrays, this manager translates
    parameter dicts from scan controllers into TriggerScope PARAMETER/command
    sequences sent over serial.  The TriggerScope firmware then runs the scan
    autonomously and emits ``"Scan done"`` when finished.

    ``sigScanDone`` and ``sigScanStarted`` are forwarded from the underlying
    ``TriggerScopeManager``.

    Usage (from a scan controller)::

        masterController.scanManager.runScan(parameterDict, scan_type)

    where ``scan_type`` is one of:
        ``'rasterScan'``, ``'pLS-RESOLFTScan'``, ``'GalvoDetectionScan'``,
        ``'MulticolorScan'``, ``'pLS-RESOLFT_multicolor_Scan'``, ``'LSXYRScan'``
    """

    def __init__(self, setupInfo, triggerScopeManager):
        super().__init__(setupInfo)
        self._ts = triggerScopeManager

    # Forward board-level signals so the rest of the UI can subscribe the same way
    @property
    def sigScanDone(self):
        return self._ts.sigScanDone

    @property
    def sigScanStarted(self):
        return self._ts.sigScanStarted

    @property
    def TTLTimeUnits(self):
        return 'ms'

    # ------------------------------------------------------------------
    # SuperScanManager ABC stubs — NI-DAQ waveform path not used here
    # ------------------------------------------------------------------

    def _parameterCompatibility(self, parameterDict):
        pass  # firmware validates parameters

    def makeFullScan(self, scanParameters, TTLParameters, staticPositioner=False):
        raise NotImplementedError(
            'TriggerScope does not use NI-DAQ waveform arrays. '
            'Call runScan(parameterDict, scan_type) instead.'
        )

    # ------------------------------------------------------------------
    # Scan dispatch
    # ------------------------------------------------------------------

    def runScan(self, parameterDict, scan_type):
        """ Dispatch a firmware scan sequence by type.

        :param parameterDict: dict with ``'deviceParameters'`` and
            ``'scanParameters'`` sub-dicts (format depends on scan type).
        :param scan_type: one of the supported firmware scan type strings.
        """
        handler = self._dispatch().get(scan_type)
        if handler is None:
            self._logger.error(f'Unknown TriggerScope scan type "{scan_type}"')
            return False
        handler(parameterDict)
        return True

    def runScanAsync(self, parameterDict, scan_type, onFinished):
        """ Like :meth:`runScan`, but nothing blocks the calling (GUI) thread.

        Everything that can be checked without the board -- unknown scan type,
        a device without a TriggerScope line -- is checked here, synchronously,
        and raises as ``runScan`` does. The PARAMETER lines are then written in
        order on the uploader thread; when they are all out, back on this
        thread the scan boundary is announced and only then is the scan
        command written, so the boundary still precedes the command exactly as
        in ``runScan``. Returns a :class:`ScanStartHandle`, or ``False`` for an
        unknown scan type.
        """
        dispatch = self._dispatch()
        handler = dispatch.get(scan_type)
        if handler is None:
            self._logger.error(f'Unknown TriggerScope scan type "{scan_type}"')
            return False

        _capture.parameters = []
        _capture.start = None
        try:
            handler(parameterDict)
            parameters, startCommand = _capture.parameters, _capture.start
        finally:
            _capture.parameters = None
        if startCommand is None:
            raise RuntimeError(f'Scan type "{scan_type}" has no scan command.')

        handle = ScanStartHandle(onFinished)

        def sendScanCommand():
            self._ts.sigScanStarted.emit()
            handle._setJob(self._ts.uploadCommands(
                [(startCommand, 0)],
                lambda ok, message: handle._finish(ok, message),
                label=f'{scan_type} start'))

        def afterParameters(ok, message):
            if not ok:
                handle._finish(False, message)
            elif handle.cancelled:
                handle._finish(False, 'cancelled before the scan command')
            else:
                try:
                    sendScanCommand()
                except Exception as error:
                    handle._finish(False, f'{type(error).__name__}: {error}')

        if parameters:
            handle._setJob(self._ts.uploadCommands(
                parameters, afterParameters, label=f'{scan_type} parameters'))
        else:
            sendScanCommand()
        return handle

    def _dispatch(self):
        return {
            'rasterScan':                    self._runRasterScan,
            'pLS-RESOLFTScan':               self._runpLSRESOLFTScan,
            'GalvoDetectionScan':            self._runpLSRESOLFTGalvoScan,
            'MulticolorScan':                self._runMulticolorScan,
            'pLS-RESOLFT_multicolor_Scan':   self._runpLSRESOLFTMulticolorScan,
            'LSXYRScan':                     self._runLSXYRScan,
        }

    # ------------------------------------------------------------------
    # Private scan sequence methods
    # ------------------------------------------------------------------

    def _setParam(self, name, value):
        captured = getattr(_capture, 'parameters', None)
        if captured is not None:
            captured.append(self._ts.parameterCommand(name, value))
        else:
            self._ts.setParameter(name, value)

    def _send(self, cmd):
        self._ts.send(cmd)

    def _startFirmwareScan(self, command):
        """Announce the scan boundary, then command the firmware.

        The order matters. ``sigScanStarted`` is what consumers use as the
        "frames from here on belong to this scan" boundary -- BeadRec
        registers its detector chunk consumer in that slot, which excludes
        every frame captured before it. Emitting AFTER the command let the
        firmware trigger the camera first, so those first frames were
        excluded from the reconstruction and it came out shifted by a few
        pixels.

        ``sigScanStarting`` cannot serve as the boundary either: it fires
        before the parameter upload, and ``setParameter`` sleeps 50 ms per
        parameter, so it precedes the scan command by roughly a second. A
        free-running camera would deposit a second of backlog into the
        reconstruction -- the same bug with the sign flipped. Emitting here
        leaves only the slot dispatch itself, during which the firmware is
        provably still idle because it has not been told to scan yet.
        """
        if getattr(_capture, 'parameters', None) is not None:
            _capture.start = command  # runScanAsync sends it after the upload
            return
        self._ts.sigScanStarted.emit()
        self._send(command)

    def _chanDAC(self, deviceName):
        return self._ts.deviceInfo[deviceName]['DACChannel']

    def _lineTTL(self, deviceName, role=None):
        """The TriggerScope TTL line a device is wired to.

        A device only appears in the registry when its setup entry names a
        ``Triggerscope/TTL<n>`` line, so a missing one is a configuration
        answer, not a lookup failure. It used to surface as a bare ``KeyError``
        raised part-way through uploading a scan's parameters, which says
        nothing about what to do -- and the case it fires on is a real one: a
        camera the firmware triggers can be named for recording purposes
        without ImSwitch knowing its line, but the modes that *program* that
        line need it.
        """
        try:
            return self._ts.deviceInfo[deviceName]['TTLLine']
        except KeyError:
            named = f'{role} ' if role else ''
            raise ValueError(
                f'The {named}device {deviceName!r} has no TriggerScope TTL '
                f'line, so this scan cannot tell the firmware which line to '
                f'drive for it. Give it "digitalLine": "Triggerscope/TTL<n>" '
                f'in the setup file, or choose a device that has one.'
            ) from None

    # ---- pLS-RESOLFT -------------------------------------------------

    def _runpLSRESOLFTScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',   self._lineTTL(dp['onLaser'], 'activation laser'))
        self._setParam('offLaserTTLChan',  self._lineTTL(dp['offLaser'], 'depletion laser'))
        self._setParam('roLaserTTLChan',   self._lineTTL(dp['roLaser'], 'readout laser'))
        self._setParam('roScanDACChan',    self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan', self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._startFirmwareScan('pLS-RESOLFT_SCAN')

    # ---- pLS-RESOLFT multicolor --------------------------------------

    def _runpLSRESOLFTMulticolorScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',        self._lineTTL(dp['onLaser'], 'activation laser'))
        self._setParam('offLaserTTLChan',       self._lineTTL(dp['offLaser'], 'depletion laser'))
        self._setParam('roLaserTTLChan',        self._lineTTL(dp['roLaser'], 'readout laser'))
        self._setParam('Laser2TTLChan',         self._lineTTL(dp['Laser2']))
        self._setParam('Laser3TTLChan',         self._lineTTL(dp['Laser3']))
        self._setParam('CameraTTLChan',         self._lineTTL(dp['CameraTTL'], 'camera'))
        self._setParam('roScanDACChan',         self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan',      self._chanDAC(dp['cycleScanDevice']))
        self._setParam('multicolorScanDACChan', self._chanDAC(dp['MulticolorScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._startFirmwareScan('pLS-RESOLFT-Multicolor_SCAN')

    # ---- galvo detection scan ----------------------------------------

    def _runpLSRESOLFTGalvoScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',   self._lineTTL(dp['onLaser'], 'activation laser'))
        self._setParam('offLaserTTLChan',  self._lineTTL(dp['offLaser'], 'depletion laser'))
        self._setParam('roLaserTTLChan',   self._lineTTL(dp['roLaser'], 'readout laser'))
        self._setParam('roScanDACChan',    self._chanDAC(dp['roScanDevice']))
        self._setParam('galvoScanDACChan', self._chanDAC(dp['galvoScanDevice']))
        self._setParam('cycleScanDACChan', self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._startFirmwareScan('galvo_Detection_SCAN')

    # ---- multicolor scan ---------------------------------------------

    def _runMulticolorScan(self, params):
        dp = params['deviceParameters']
        self._setParam('Laser1TTLChan',         self._lineTTL(dp['Laser1']))
        self._setParam('Laser2TTLChan',         self._lineTTL(dp['Laser2']))
        self._setParam('Laser3TTLChan',         self._lineTTL(dp['Laser3']))
        self._setParam('Laser4TTLChan',         self._lineTTL(dp['Laser4']))
        self._setParam('Laser5TTLChan',         self._lineTTL(dp['Laser5']))
        self._setParam('CameraTTLChan',         self._lineTTL(dp['CameraTTL'], 'camera'))
        self._setParam('roScanDACChan',         self._chanDAC(dp['roScanDevice']))
        self._setParam('multicolorScanDACChan', self._chanDAC(dp['MulticolorScanDevice']))
        self._setParam('cycleScanDACChan',      self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._startFirmwareScan('multicolor_SCAN')

    # ---- raster scan -------------------------------------------------

    def _runRasterScan(self, params):
        seqTime = params['Digital']['sequence_time']
        self._setParam('sequenceTimeUs', int(seqTime * 1e6))

        startTimes = params['Digital']['TTL_start']
        if startTimes:
            endTimes = params['Digital']['TTL_end']
            firstStart = min(startTimes)
            firstIndex = startTimes.index(firstStart)
            endOfPulse = endTimes[firstIndex]
            target = params['Digital']['target_device'][firstIndex]
            self._setParam('p1Line',    int(self._lineTTL(target)))
            self._setParam('p1StartUs', int(firstStart * 1e6))
            self._setParam('p1EndUs',   int(endOfPulse * 1e6))

        self._logger.debug('Setting analog parameters')
        for dim, prefix in enumerate(['dimOne', 'dimTwo', 'dimThree', 'dimFour']):
            try:
                chan = self._chanDAC(params['Analog']['targets'][dim])
                self._setParam(f'{prefix}Chan',         chan)
                self._setParam(f'{prefix}StartV',  params['Analog']['startPos'][dim])
                self._setParam(f'{prefix}LenV',    params['Analog']['lengths'][dim])
                self._setParam(f'{prefix}StepSizeV', params['Analog']['stepSizes'][dim])
            except IndexError:
                break

        self._setParam('angleRad', np.deg2rad(0))
        self._logger.debug('Parameters set')
        self._startFirmwareScan('RASTER_SCAN')

    # ---- LS-XY-RESOLFT scan ------------------------------------------

    def _runLSXYRScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',     self._lineTTL(dp['onLaser'], 'activation laser'))
        self._setParam('offLaserTTLChan',    self._lineTTL(dp['offLaser'], 'depletion laser'))
        self._setParam('roLaserTTLChan',     self._lineTTL(dp['roLaser'], 'readout laser'))
        self._setParam('CameraTTLChan',      self._lineTTL(dp['CameraTTL'], 'camera'))
        self._setParam('roScanDACChan',      self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan',   self._chanDAC(dp['cycleScanDevice']))
        self._setParam('rasterXScanDACChan', self._chanDAC(dp['rasterXScanDevice']))
        self._setParam('rasterYScanDACChan', self._chanDAC(dp['rasterYScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._startFirmwareScan('LS-XY-RESOLFT_SCAN')
        self._logger.debug('LSXYR scanParameters=%s deviceParameters=%s',
                           params.get('scanParameters'), params.get('deviceParameters'))


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
