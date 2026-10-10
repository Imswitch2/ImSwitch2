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
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

import numpy as np

from imswitch.imcontrol.model.managers.NidaqManager import (
    SCAN_CLOCK_RATE_HZ, SCAN_CLOCK_TERMINAL,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EMULATED, EngineCapabilities, FeasibilityReport, FeatureVerdict, IMPOSSIBLE,
    ScanEngine, ScanEngineRefused, UNKNOWN, setup_channels,
)
from imswitch.imcontrol.model.scan_program.interpreter import RunContext, expand
from imswitch.imcontrol.model.scan_program.scan_info import (
    scan_info_for, trajectory_scan_info,
)
from imswitch.imcontrol.model.scan_program.schedule import ScheduleRefused, realize
from imswitch.imcontrol.model.scan_program.trajectory import (
    limits_refusals, realize_trajectory,
)
from imswitch.imcontrol.model.signaldesigners.basesignaldesigners import (
    scan_voltage_range_refusal,
)

_NI_ANALOG_OUTPUT = re.compile(r'[^/]+/ao\d+')

#: The clocks Advanced scans carry, and the setup key that wires each.
SCAN_CLOCKS = (('lineClockLine', 'line_clock'),
               ('frameStartClockLine', 'frame_start_clock'),
               ('frameEndClockLine', 'frame_end_clock'))


@dataclass
class CompiledWaveforms:
    """A program as the NI-DAQ plays it: the signals ``runScan`` takes and
    the ``scanInfoDict`` the detectors read."""
    program: object
    schedule: object
    signalDict: Dict[str, Dict[str, np.ndarray]]
    scanInfoDict: Dict[str, object]
    report: FeasibilityReport


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
            for key, marker in SCAN_CLOCKS:
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

    def assess(self, program, channels) -> FeasibilityReport:
        """As every engine, except a scan clock the setup does not wire: the
        NI-DAQ does not play it (``runScan`` drives a clock only on its
        ``scan.*ClockLine``), which is what Advanced scans do today. It is
        reported, not refused."""
        report = super().assess(program, channels)
        scan = getattr(self._setupInfo, 'scan', None)
        unwired = {marker: key for key, marker in SCAN_CLOCKS
                   if not (scan is not None and getattr(scan, key, None))}
        for index, verdict in enumerate(report.verdicts):
            marker = verdict.feature[len('marker '):] if verdict.feature.startswith(
                'marker ') else None
            if verdict.verdict == UNKNOWN and marker in unwired:
                report.verdicts[index] = FeatureVerdict(
                    verdict.feature, EMULATED,
                    cost=f'not played: scan.{unwired[marker]} names no line')
        return report

    # -- programs (plan §5.1): densified on the board's clock ------------------

    def compile(self, program, channels: Mapping, captured_um: Optional[Mapping] = None,
                devices: Optional[Mapping] = None) -> CompiledWaveforms:
        """The waveforms for ``program``, or :class:`ScanEngineRefused` with
        every reason at once."""
        report = self.assess(program, channels)
        if not report.runnable:
            raise ScanEngineRefused(report)
        if getattr(program, 'trajectory', None) is not None:
            return self._compileTrajectory(program, report, captured_um)
        try:
            schedule = realize(
                expand(program, RunContext(captured_um=dict(captured_um or {}),
                                           devices=dict(devices or {}))),
                self.rate_hz, rate_source=f'NidaqManager ({SCAN_CLOCK_TERMINAL})')
        except ScheduleRefused as error:
            report.add('timing', IMPOSSIBLE, str(error))
            raise ScanEngineRefused(report) from error
        if len(schedule.segments) > 1:
            report.add('segments', IMPOSSIBLE,
                       'The NI-DAQ plays one stream; it cannot wait for a trigger in it.')
            raise ScanEngineRefused(report)
        dense = densify(schedule)
        positioners = getattr(self._setupInfo, 'positioners', {}) or {}
        analog = {}
        for device, positions_um in dense['analog'].items():
            props = getattr(positioners.get(device), 'managerProperties', None) or {}
            volts = positions_um / props.get('conversionFactor', 1)
            refusal = scan_voltage_range_refusal(
                self._setupInfo, device, (float(volts.min()), float(volts.max())))
            if refusal:
                report.add(f'drive {device}', IMPOSSIBLE, refusal)
            analog[device] = volts
        if not report.runnable:
            raise ScanEngineRefused(report)
        info = scan_info_for(program, schedule, self.rate_hz)
        info['minmaxes'] = [[float(v.min()), float(v.max())] for v in analog.values()]
        signals = {'scanSignalsDict': analog, 'TTLCycleSignalsDict': dict(dense['digital'])}
        return CompiledWaveforms(program, schedule, signals, info, report)

    def _positionerProps(self, device):
        positioners = getattr(self._setupInfo, 'positioners', {}) or {}
        return getattr(positioners.get(device), 'managerProperties', None) or {}

    def _volts(self, positions_um, report):
        analog = {}
        for device, positions in positions_um.items():
            volts = positions / self._positionerProps(device).get('conversionFactor', 1)
            refusal = scan_voltage_range_refusal(
                self._setupInfo, device, (float(volts.min()), float(volts.max())))
            if refusal:
                report.add(f'path {device}', IMPOSSIBLE, refusal)
            analog[device] = volts
        return analog

    def _compileTrajectory(self, program, report, captured_um):
        try:
            realized = realize_trajectory(program, self.rate_hz, captured_um=captured_um)
        except ScheduleRefused as error:
            report.add('timing', IMPOSSIBLE, str(error))
            raise ScanEngineRefused(report) from error
        for refusal in limits_refusals(realized, {d: self._positionerProps(d)
                                                  for d in realized.positions_um}):
            report.add('path limits', IMPOSSIBLE, refusal)
        analog = self._volts(realized.positions_um, report)
        if not report.runnable:
            raise ScanEngineRefused(report)
        labels = []
        for device in program.trajectory.devices:
            info = (getattr(self._setupInfo, 'positioners', {}) or {}).get(device)
            axes = list(getattr(info, 'axes', None) or [])
            label = axes[0].lower() if axes and axes[0].lower() in ('x', 'y', 'z') else device
            labels.append(label)
        info = trajectory_scan_info(realized, labels)
        info['minmaxes'] = [[float(v.min()), float(v.max())] for v in analog.values()]
        signals = {'scanSignalsDict': analog, 'TTLCycleSignalsDict': dict(realized.digital)}
        return CompiledWaveforms(program, realized, signals, info, report)

    def run_signals(self, signalDict, scanInfoDict):
        return self._nidaq.runScan(signalDict, scanInfoDict)

    def resolveScanTTLDevices(self, signalDict):
        return self._nidaq.resolveScanTTLDevices(signalDict)

    def abort(self) -> str:
        # An NI-DAQ scan iteration runs to its end; the run stops after it.
        return 'none'


__all__ = ['CompiledWaveforms', 'NidaqScanEngine']


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
