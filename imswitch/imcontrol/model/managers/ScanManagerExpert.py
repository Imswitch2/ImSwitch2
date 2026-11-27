"""The Expert panel's scan manager (docs/design/plans/
scan-engines-and-expert-scan.md §4, M3).

The Expert panel builds no waveforms with the signal designers: its scan
programs compile on the scan engine (``ScanEngine.compile``). This manager is
here because every scan panel has one; it holds no designer, so a setup for
the Expert panel needs no ``scanDesigner`` or ``TTLCycleDesigner``.
"""

from imswitch.imcontrol.model.errors import ScanDesignRefusedError


class ScanManagerExpert:
    def __init__(self, setupInfo):
        self._setupInfo = setupInfo

    @property
    def sampleRate(self):
        return self._setupInfo.scan.sampleRate

    def makeFullScan(self, scanParameters, TTLParameters, staticPositioner=False):
        raise ScanDesignRefusedError(
            'The Expert panel compiles scan programs on the scan engine; it has no '
            'signal designers to build waveforms with.')


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
