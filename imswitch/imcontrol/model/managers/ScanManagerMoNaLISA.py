from .ScanManagerBase import SuperScanManager


class ScanManagerMoNaLISA(SuperScanManager):
    """ ScanManager helps with generating signals for scanning. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def TTLTimeUnits(self):
        self._checkScanDefined()
        return self._TTLCycleDesigner.timeUnits

    def makeFullScan(self, scanParameters, TTLParameters, staticPositioner=False):
        """ Generates stage and TTL scan signals. Raises
        ScanDesignRefusedError when the designer refuses the scan. """
        self._checkScanDefined()

        if not staticPositioner:
            scanSignalsDict, scanInfoDict = self._designScanSignals(scanParameters)
            TTLCycleSignalsDict = self.getTTLCycleSignalsDict(TTLParameters, scanInfoDict)
        else:
            TTLCycleSignalsDict = self.getTTLCycleSignalsDict(TTLParameters)
            scanSignalsDict = {}
            scanInfoDict = {}

        return (
            {'scanSignalsDict': scanSignalsDict,
             'TTLCycleSignalsDict': TTLCycleSignalsDict},
            scanInfoDict
        )


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
