import numpy as np

from .ScanManagerBase import SuperScanManager


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
        dispatch = {
            'rasterScan':                    self._runRasterScan,
            'pLS-RESOLFTScan':               self._runpLSRESOLFTScan,
            'GalvoDetectionScan':            self._runpLSRESOLFTGalvoScan,
            'MulticolorScan':                self._runMulticolorScan,
            'pLS-RESOLFT_multicolor_Scan':   self._runpLSRESOLFTMulticolorScan,
            'LSXYRScan':                     self._runLSXYRScan,
        }
        handler = dispatch.get(scan_type)
        if handler is None:
            self._logger.error(f'Unknown TriggerScope scan type "{scan_type}"')
            return
        handler(parameterDict)

    # ------------------------------------------------------------------
    # Private scan sequence methods
    # ------------------------------------------------------------------

    def _setParam(self, name, value):
        self._ts.setParameter(name, value)

    def _send(self, cmd):
        self._ts.send(cmd)

    def _chanDAC(self, deviceName):
        return self._ts.deviceInfo[deviceName]['DACChannel']

    def _lineTTL(self, deviceName):
        return self._ts.deviceInfo[deviceName]['TTLLine']

    # ---- pLS-RESOLFT -------------------------------------------------

    def _runpLSRESOLFTScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',   self._lineTTL(dp['onLaser']))
        self._setParam('offLaserTTLChan',  self._lineTTL(dp['offLaser']))
        self._setParam('roLaserTTLChan',   self._lineTTL(dp['roLaser']))
        self._setParam('roScanDACChan',    self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan', self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._send('pLS-RESOLFT_SCAN')
        self._ts.sigScanStarted.emit()

    # ---- pLS-RESOLFT multicolor --------------------------------------

    def _runpLSRESOLFTMulticolorScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',        self._lineTTL(dp['onLaser']))
        self._setParam('offLaserTTLChan',       self._lineTTL(dp['offLaser']))
        self._setParam('roLaserTTLChan',        self._lineTTL(dp['roLaser']))
        self._setParam('Laser2TTLChan',         self._lineTTL(dp['Laser2']))
        self._setParam('Laser3TTLChan',         self._lineTTL(dp['Laser3']))
        self._setParam('CameraTTLChan',         self._lineTTL(dp['CameraTTL']))
        self._setParam('roScanDACChan',         self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan',      self._chanDAC(dp['cycleScanDevice']))
        self._setParam('multicolorScanDACChan', self._chanDAC(dp['MulticolorScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._send('pLS-RESOLFT-Multicolor_SCAN')
        self._ts.sigScanStarted.emit()

    # ---- galvo detection scan ----------------------------------------

    def _runpLSRESOLFTGalvoScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',   self._lineTTL(dp['onLaser']))
        self._setParam('offLaserTTLChan',  self._lineTTL(dp['offLaser']))
        self._setParam('roLaserTTLChan',   self._lineTTL(dp['roLaser']))
        self._setParam('roScanDACChan',    self._chanDAC(dp['roScanDevice']))
        self._setParam('galvoScanDACChan', self._chanDAC(dp['galvoScanDevice']))
        self._setParam('cycleScanDACChan', self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._send('galvo_Detection_SCAN')
        self._ts.sigScanStarted.emit()

    # ---- multicolor scan ---------------------------------------------

    def _runMulticolorScan(self, params):
        dp = params['deviceParameters']
        self._setParam('Laser1TTLChan',         self._lineTTL(dp['Laser1']))
        self._setParam('Laser2TTLChan',         self._lineTTL(dp['Laser2']))
        self._setParam('Laser3TTLChan',         self._lineTTL(dp['Laser3']))
        self._setParam('Laser4TTLChan',         self._lineTTL(dp['Laser4']))
        self._setParam('Laser5TTLChan',         self._lineTTL(dp['Laser5']))
        self._setParam('CameraTTLChan',         self._lineTTL(dp['CameraTTL']))
        self._setParam('roScanDACChan',         self._chanDAC(dp['roScanDevice']))
        self._setParam('multicolorScanDACChan', self._chanDAC(dp['MulticolorScanDevice']))
        self._setParam('cycleScanDACChan',      self._chanDAC(dp['cycleScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._send('multicolor_SCAN')
        self._ts.sigScanStarted.emit()

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
        self._send('RASTER_SCAN')
        self._ts.sigScanStarted.emit()

    # ---- LS-XY-RESOLFT scan ------------------------------------------

    def _runLSXYRScan(self, params):
        dp = params['deviceParameters']
        self._setParam('onLaserTTLChan',     self._lineTTL(dp['onLaser']))
        self._setParam('offLaserTTLChan',    self._lineTTL(dp['offLaser']))
        self._setParam('roLaserTTLChan',     self._lineTTL(dp['roLaser']))
        self._setParam('CameraTTLChan',      self._lineTTL(dp['CameraTTL']))
        self._setParam('roScanDACChan',      self._chanDAC(dp['roScanDevice']))
        self._setParam('cycleScanDACChan',   self._chanDAC(dp['cycleScanDevice']))
        self._setParam('rasterXScanDACChan', self._chanDAC(dp['rasterXScanDevice']))
        self._setParam('rasterYScanDACChan', self._chanDAC(dp['rasterYScanDevice']))
        for key, value in params['scanParameters'].items():
            self._setParam(key, value)
        self._logger.debug('Parameters set')
        self._send('LS-XY-RESOLFT_SCAN')
        self._ts.sigScanStarted.emit()
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
