"""The NI-DAQ as a scan engine (docs/design/plans/
scan-engines-and-expert-scan.md §5.1, M1).

The engine is the NI-DAQ manager seen through the engine interface: its
signals ARE the manager's signals, and a scan the Advanced controller builds
today runs through ``run_signals`` exactly as it did through
``NidaqManager.runScan``. What it adds is the declaration: what the board
drives, on which grid, and what it cannot do, so a panel and a run can ask.

Ownership is ``runScan``'s, written down:

- a digital line is driven when it is an NI-DAQmx line or PFI terminal, on
  any device name (``nidaq_drives_line``);
- an analog channel is driven when it is an NI-DAQmx analog-output channel,
  ``<device>/ao<n>``.

A scan starts with its analog output: the AO task's start trigger starts the
rest (``nidaq.startTrigger``'s DO and timer tasks, and every APD's and PMT's
input task). A program with no analog output is refused where something
would wait for that trigger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

import numpy as np

from imswitch.imcontrol.model.managers.NidaqManager import (
    SCAN_CLOCK_RATE_HZ, SCAN_CLOCK_TERMINAL, nidaq_drives_line, nidaq_raw_line,
)
from imswitch.imcontrol.model.managers.lasers._power_volts import level_volts
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EMULATED, EngineCapabilities, FeasibilityReport, FeatureVerdict, IMPOSSIBLE,
    NATIVE, ScanEngine, ScanEngineRefused, UNKNOWN, setup_channels,
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

#: The detector managers whose input tasks start on the scan's AO start
#: trigger: they call ``startInputTask(..., 'ao/StartTrigger', ...)``
#: (APDManager, PMTManager; a test pins this against their source).
AO_TRIGGERED_DETECTORS = ('APDManager', 'PMTManager')


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
        return nidaq_drives_line(channel) or bool(_NI_ANALOG_OUTPUT.fullmatch(channel))

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
                'digital': 'NidaqManager.runScan drives NI-DAQmx digital lines '
                           '(<device>/port<n>/line<m>, <device>/PFI<n>)',
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
        clocks = {marker for _, marker in SCAN_CLOCKS}
        # A device's line, keyed by line: a marker there would be a second
        # signal on one line, which the DO task cannot write.
        on_line = {entry.digital: name for name, entry in channels.items() if entry.digital}
        for index, verdict in enumerate(report.verdicts):
            marker = verdict.feature[len('marker '):] if verdict.feature.startswith(
                'marker ') else None
            if verdict.verdict == UNKNOWN and marker in unwired:
                report.verdicts[index] = FeatureVerdict(
                    verdict.feature, EMULATED,
                    cost=f'not played: scan.{unwired[marker]} names no line')
            elif marker is not None and marker not in clocks and verdict.verdict == NATIVE:
                if marker in on_line:
                    report.verdicts[index] = FeatureVerdict(
                        verdict.feature, IMPOSSIBLE,
                        f'{marker} is {on_line[marker]}\'s line; switch {on_line[marker]} '
                        'instead.')
                elif not nidaq_raw_line(marker):
                    report.verdicts[index] = FeatureVerdict(
                        verdict.feature, IMPOSSIBLE,
                        f'{marker} is not an NI-DAQmx line (<device>/port<n>/line<m> or '
                        '<device>/PFI<n>), so no DO channel plays it.')
        marked = {e.device for e in program.timeline if e.kind == 'marker'}
        for key, marker in SCAN_CLOCKS:
            line = getattr(scan, key, None) if scan is not None else None
            if line and marker not in marked:
                report.add(f'clock {marker}', NATIVE,
                           f'scan.{key} wires it to {line}; the program marks nothing on it, '
                           'so it is held low.')
        waiting = self._waitsForAnalogStart()
        if waiting and not self._hasAnalogOutput(program):
            report.add('analog output', IMPOSSIBLE,
                       'The NI-DAQ starts a scan with its analog output, and this program '
                       f'drives none: {" and ".join(waiting)} would wait for its start '
                       'trigger forever.', 'Drive a positioner or a power level in it.')
        return report

    def _waitsForAnalogStart(self):
        """What waits for the AO task's start trigger in every scan: the DO
        and timer tasks with ``nidaq.startTrigger``, and the point detectors
        whose input tasks start on it."""
        waiting = []
        nidaq = getattr(self._setupInfo, 'nidaq', None)
        if nidaq is not None and getattr(nidaq, 'startTrigger', False):
            waiting.append('the DO and timer tasks (nidaq.startTrigger)')
        detectors = getattr(self._setupInfo, 'detectors', None) or {}
        triggered = sorted(name for name, info in detectors.items()
                           if getattr(info, 'managerName', None) in AO_TRIGGERED_DETECTORS)
        if triggered:
            waiting.append(', '.join(triggered))
        return waiting

    @staticmethod
    def _hasAnalogOutput(program):
        return bool(program.drives or getattr(program, 'trajectory', None) is not None
                    or any(e.kind == 'level' for e in program.timeline))

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
        analog = {}
        for device, positions_um in dense['analog'].items():
            factor = self._factor(device, report, f'conversion {device}')
            if factor is None:
                continue
            if not np.all(np.isfinite(positions_um)):
                # Nothing non-finite reaches a task: a NaN passes every range
                # comparison and would be written to the output as is.
                report.add(f'drive {device}', IMPOSSIBLE,
                           f'{device}\'s positions are not all finite numbers.')
                continue
            volts = positions_um / factor
            refusal = scan_voltage_range_refusal(
                self._setupInfo, device, (float(volts.min()), float(volts.max())))
            if refusal:
                report.add(f'drive {device}', IMPOSSIBLE, refusal)
            analog[device] = volts
        minmaxes = [[float(v.min()), float(v.max())] for v in analog.values()]
        # A power level inside a step: the laser's analog channel plays it,
        # converted as the laser is set by hand (§7.8). It used to be
        # reported native and then not played at all.
        lasers = getattr(self._setupInfo, 'lasers', {}) or {}
        for laser, values in dense['level'].items():
            if any(e.kind == 'level' and e.device == laser and e.shape != 'hold'
                   for e in program.timeline):
                report.add(f'level {laser}', IMPOSSIBLE,
                           f'A ramped power level of {laser} is not played; hold it.')
                continue
            volts, refusal = level_volts(laser, lasers.get(laser), values)
            if refusal:
                report.add(f'level {laser}', IMPOSSIBLE, refusal)
                continue
            analog[laser] = volts
        if not report.runnable:
            raise ScanEngineRefused(report)
        info = scan_info_for(program, schedule, self.rate_hz)
        info['minmaxes'] = minmaxes
        digital = self._withClocks(dense['digital'], schedule.segments[0].ticks)
        signals = {'scanSignalsDict': analog, 'TTLCycleSignalsDict': digital}
        return CompiledWaveforms(program, schedule, signals, info, report)

    def _withClocks(self, digital, samples):
        """``digital`` with every clock the setup wires: ``runScan`` writes
        each, and one the program does not mark is held low (the report
        says so)."""
        digital = dict(digital)
        scan = getattr(self._setupInfo, 'scan', None)
        for key, marker in SCAN_CLOCKS:
            if scan is not None and getattr(scan, key, None) and marker not in digital:
                digital[marker] = np.zeros(int(samples), dtype=bool)
        return digital

    def _positionerProps(self, device):
        positioners = getattr(self._setupInfo, 'positioners', {}) or {}
        return getattr(positioners.get(device), 'managerProperties', None) or {}

    def _factor(self, device, report, feature):
        """A positioner's µm per volt, or ``None`` with the reason in the
        report: an undeclared factor is unknown, never taken as 1."""
        factor = self._positionerProps(device).get('conversionFactor')
        if factor is None:
            report.add(feature, UNKNOWN, f'{device} declares no conversionFactor (µm per '
                       'volt), so the volts that put it anywhere are unknown.')
            return None
        try:
            value = float(factor)
        except (TypeError, ValueError):
            value = float('nan')
        if not np.isfinite(value) or value == 0:
            report.add(feature, IMPOSSIBLE, f'{device} declares conversionFactor {factor!r}, '
                       'which is not a finite, non-zero number of µm per volt.')
            return None
        return value

    def _volts(self, positions_um, report):
        analog = {}
        for device, positions in positions_um.items():
            factor = self._factor(device, report, f'conversion {device}')
            if factor is None:
                continue
            volts = positions / factor
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
        samples = len(next(iter(analog.values())))
        signals = {'scanSignalsDict': analog,
                   'TTLCycleSignalsDict': self._withClocks(realized.digital, samples)}
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
