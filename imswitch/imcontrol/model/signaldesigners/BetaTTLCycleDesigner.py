import numpy as np

from .basesignaldesigners import TTLCycleDesigner
from ..errors import ScanDesignRefusedError
from ..scan_parameters import dwell_request_if_changed, samples_for_duration


class BetaTTLCycleDesigner(TTLCycleDesigner):
    """ TTL cycle designer for camera-based applications where each pulse
    scheme is one frame.

    Designer params: None
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._expectedParameters = ['target_device',
                                    'TTL_start',
                                    'TTL_end',
                                    'sequence_time']

    @property
    def timeUnits(self):
        return 'ms'

    def make_signal(self, parameterDict, setupInfo, scanInfoDict=None):

        if not self.parameterCompatibility(parameterDict):
            self._logger.error('TTL parameters seem incompatible, this error should not be since'
                               ' this should be checked at program start-up')
            return None

        sampleRate = setupInfo.scan.sampleRate
        targets = parameterDict['target_device']
        # Counted by the rule BetaScanDesigner counts its pixels and returns
        # with (scan_parameters.samples_for_duration): a float product's
        # ceiling put one sample more into the cycle than the scan has in its
        # pixel for about 2 % of dwells (0.07 ms at 100 kHz: 7.000000000000001).
        if dwell_request_if_changed(parameterDict['sequence_time'], sampleRate) is not None:
            self._logger.warning('Non-integer number of sequence samples, rounding up')
        cycleSamples = samples_for_duration(parameterDict['sequence_time'], sampleRate)
        signalDict = {}
        tmpSigArr = np.zeros(cycleSamples, dtype='bool')
        for i, target in enumerate(targets):
            tmpSigArr[:] = False
            for j, start in enumerate(parameterDict['TTL_start'][i]):
                startSamp = samples_for_duration(start, sampleRate, 'nearest-even')
                endSamp = samples_for_duration(parameterDict['TTL_end'][i][j], sampleRate,
                                               'nearest-even')
                tmpSigArr[startSamp:endSamp] = True

            # A camera is triggered on its line's rising edge, one exposure
            # per pixel: a pulse that fills the whole cycle never falls
            # between pixels, so the camera saw one edge per line while the
            # recording waited for one frame per pixel (review 2026-10-16).
            if cycleSamples > 0 and tmpSigArr.all() and target in (
                    getattr(setupInfo, 'detectors', None) or {}):
                raise ScanDesignRefusedError(
                    f"{target}'s pulse fills the whole {parameterDict['sequence_time'] * 1e3:g} ms "
                    'cycle, so it never falls between pixels: the camera would see one '
                    'rising edge per line, not one per pixel. End it at least one sample '
                    f'({1e3 / float(sampleRate):g} ms) before the cycle ends.')
            signalDict[target] = np.copy(tmpSigArr)

        if scanInfoDict is not None:
            positions = scanInfoDict['positions']
            returnTime = scanInfoDict['return_time']

            # Calculate samples to zero pad TTL signals with: the scan's
            # return, counted as the scan counts it
            if dwell_request_if_changed(returnTime, sampleRate) is not None:
                self._logger.warning('Non-integer number of return samples, rounding up')
            TTLZeroPadSamples = samples_for_duration(returnTime, sampleRate)

            # Tile and pad TTL signals according to sync parameters
            for target, signal in signalDict.items():
                signal = np.tile(signal, positions[0])
                signal = np.append(signal, np.zeros(TTLZeroPadSamples, dtype='bool'))
                tileLength = positions[1] if len(positions) == 2 else positions[1] * positions[2]
                signal = np.tile(signal, tileLength)
                signalDict[target] = signal

        return signalDict


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
