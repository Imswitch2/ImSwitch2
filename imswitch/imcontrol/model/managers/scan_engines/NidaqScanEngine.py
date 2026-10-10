"""The NI-DAQ as a scan engine (docs/design/plans/
scan-engines-and-expert-scan.md §5.1, M1).

The engine is the NI-DAQ manager seen through the engine interface: its
signals ARE the manager's signals, and a scan the Advanced controller builds
today runs through ``run_signals`` exactly as it did through
``NidaqManager.runScan``. What it adds is the declaration: what the board
drives, on which grid, and what it cannot do, so a panel and a run can ask.

Ownership is today's behaviour, written down:

- a digital line is driven when its name contains ``Dev`` -- the filter
  ``runScan`` applies to every TTL line (``NidaqManager.runScan``);
- an analog channel is driven when it is an NI-DAQmx analog-output channel,
  ``<device>/ao<n>``.
"""

from __future__ import annotations

import re

from imswitch.imcontrol.model.managers.NidaqManager import (
    SCAN_CLOCK_RATE_HZ, SCAN_CLOCK_TERMINAL,
)
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EngineCapabilities, ScanEngine, setup_channels,
)

_NI_ANALOG_OUTPUT = re.compile(r'[^/]+/ao\d+')


class NidaqScanEngine(ScanEngine):
    def __init__(self, engineInfo=None, name='nidaq', *, nidaqManager, setupInfo=None,
                 **_lowLevelManagers):
        self.name = name
        self._nidaq = nidaqManager
        self._setupInfo = setupInfo

    # The manager's own signals: relaying them through the engine changes
    # nothing about when or how often anyone hears them.
    @property
    def sigScanBuilt(self):
        return self._nidaq.sigScanBuilt

    @property
    def sigScanStarted(self):
        return self._nidaq.sigScanStarted

    @property
    def sigScanDone(self):
        return self._nidaq.sigScanDone

    @property
    def sigScanBuildFailed(self):
        return self._nidaq.sigScanBuildFailed

    @property
    def manager(self):
        return self._nidaq

    def owns(self, channel: str) -> bool:
        channel = str(channel)
        return 'Dev' in channel or bool(_NI_ANALOG_OUTPUT.fullmatch(channel))

    def capabilities(self) -> EngineCapabilities:
        analog, digital, triggers = {}, [], []
        if self._setupInfo is not None:
            for entry in setup_channels(self._setupInfo).values():
                if entry.analog and self.owns(entry.analog):
                    props = {}
                    info = (getattr(self._setupInfo, 'positioners', {}) or {}).get(entry.device)
                    if info is not None:
                        props = info.managerProperties or {}
                    analog[entry.analog] = AnalogOut(entry.analog, props.get('minVolt'),
                                                     props.get('maxVolt'))
                if entry.digital and self.owns(entry.digital):
                    digital.append(entry.digital)
                    if entry.kind == 'detector':
                        triggers.append(entry.device)
            scan = getattr(self._setupInfo, 'scan', None)
            for key, marker in (('lineClockLine', 'line_clock'),
                                ('frameStartClockLine', 'frame_start_clock'),
                                ('frameEndClockLine', 'frame_end_clock')):
                if scan is not None and getattr(scan, key, None):
                    triggers.append(marker)
        return EngineCapabilities(
            name=self.name, analog=analog, digital=tuple(sorted(set(digital))),
            streaming=True, stepping='emulated',
            transitions=frozenset({'jump', 'linear', 'smooth'}), intra_step_offsets=True,
            timing_grid_s=1.0 / SCAN_CLOCK_RATE_HZ,
            waits=frozenset({'time'}), detector_inputs=frozenset({'counter', 'ai'}),
            triggers_out=tuple(triggers), abort='none',
            sources={
                'timing_grid_s': f'NidaqManager: scans run on {SCAN_CLOCK_TERMINAL} '
                                 f'({SCAN_CLOCK_RATE_HZ} Hz)',
                'digital': 'NidaqManager.runScan drives lines whose name contains "Dev"',
                'abort': 'NI-DAQ scans cannot be stopped mid-iteration today',
            },
        )

    @property
    def rate_hz(self):
        return SCAN_CLOCK_RATE_HZ

    # -- running what today's designers build ----------------------------------

    def run_signals(self, signalDict, scanInfoDict):
        return self._nidaq.runScan(signalDict, scanInfoDict)

    def resolveScanTTLDevices(self, signalDict):
        return self._nidaq.resolveScanTTLDevices(signalDict)

    def abort(self) -> str:
        # An NI-DAQ scan iteration runs to its end; the run stops after it.
        return 'none'


__all__ = ['NidaqScanEngine']


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
