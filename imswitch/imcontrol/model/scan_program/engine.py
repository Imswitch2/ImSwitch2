"""Scan engines: what runs a scan program, and what it can do
(docs/design/plans/scan-engines-and-expert-scan.md §3, §5.0).

An engine declares its capabilities. Assessing a program against them gives a
feasibility report: every feature the program uses is native, emulated (with
its cost), impossible (with the reason and the remedy) or unknown (the engine
does not declare something the feature needs -- never guessed). The panels
grey out controls from the report, and a run refuses from it: one authority.

Which engine drives a device follows from the device's channel. Each engine
says which channels it owns; a channel two engines claim is a setup error.

This is the experimental plugin API (``imswitch.pluginapi.experimental``):
nothing here is a compatibility promise before M3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

NATIVE = 'native'
EMULATED = 'emulated'
IMPOSSIBLE = 'impossible'
UNKNOWN = 'unknown'


@dataclass(frozen=True)
class AnalogOut:
    channel: str
    min_v: Optional[float] = None
    max_v: Optional[float] = None


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine can do. ``None`` means the engine does not declare it:
    a feature that needs it is assessed as unknown, never given a default.
    ``sources`` says where each declared value came from."""
    name: str
    analog: Mapping[str, AnalogOut] = field(default_factory=dict)
    digital: Tuple[str, ...] = ()
    streaming: bool = False
    stepping: str = 'none'                   # 'native', 'emulated' or 'none'
    transitions: FrozenSet[str] = frozenset({'jump'})
    intra_step_offsets: bool = False
    timing_grid_s: Optional[float] = None
    min_pulse_s: Optional[float] = None
    max_samples: Optional[int] = None
    max_steps: Optional[int] = None
    trigger_latency_s: Optional[float] = None
    #: The shortest pulse the trigger input takes (a follower's, §5.4).
    trigger_min_pulse_s: Optional[float] = None
    clock_accuracy_ppm: Optional[float] = None
    deadlines: bool = False
    waits: FrozenSet[str] = frozenset()      # 'time', 'start-trigger', 'step-trigger'
    sync_in: FrozenSet[str] = frozenset()    # 'start', 'clock'
    detector_inputs: FrozenSet[str] = frozenset()   # 'counter', 'ai'
    triggers_out: Tuple[str, ...] = ()
    safe_state: str = 'illumination off, no window open, positions held'
    abort: str = 'none'                      # 'none', 'step-boundary', 'immediate'
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def rate_hz(self) -> Optional[float]:
        return None if self.timing_grid_s is None else 1.0 / self.timing_grid_s


@dataclass(frozen=True)
class FeatureVerdict:
    feature: str
    verdict: str
    reason: str = ''
    remedy: str = ''
    cost: str = ''

    def message(self) -> str:
        text = f'{self.feature}: {self.reason}' if self.reason else self.feature
        return f'{text} {self.remedy}'.strip()


@dataclass
class FeasibilityReport:
    engine: str
    verdicts: List[FeatureVerdict] = field(default_factory=list)

    def add(self, feature, verdict, reason='', remedy='', cost=''):
        self.verdicts.append(FeatureVerdict(feature, verdict, reason, remedy, cost))

    @property
    def runnable(self) -> bool:
        return not any(v.verdict in (IMPOSSIBLE, UNKNOWN) for v in self.verdicts)

    def blocking(self) -> List[FeatureVerdict]:
        return [v for v in self.verdicts if v.verdict in (IMPOSSIBLE, UNKNOWN)]

    def verdict(self, feature) -> Optional[FeatureVerdict]:
        for item in self.verdicts:
            if item.feature == feature:
                return item
        return None

    def reasons(self) -> List[str]:
        return [v.message() for v in self.blocking()]


class ScanEngineRefused(ValueError):
    """A program the engine cannot run; carries the feasibility report."""

    def __init__(self, report: FeasibilityReport):
        self.report = report
        super().__init__(f'{report.engine} cannot run this scan:\n- '
                         + '\n- '.join(report.reasons()))


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceChannels:
    """The lines a device is wired to, as the setup names them."""
    device: str
    analog: Optional[str] = None
    digital: Optional[str] = None
    kind: str = ''


def setup_channels(setupInfo) -> Dict[str, DeviceChannels]:
    """Every device's channels from a setup: positioners' and lasers' analog
    channels, lasers' and detectors' digital lines."""
    out = {}
    for kind, section in (('positioner', 'positioners'), ('laser', 'lasers'),
                          ('detector', 'detectors')):
        devices = getattr(setupInfo, section, None)
        if not isinstance(devices, Mapping):
            continue
        for name, info in devices.items():
            analog = getattr(info, 'analogChannel', None)
            digital = getattr(info, 'digitalLine', None)
            analog = None if analog in (None, 'None', '') else str(analog)
            digital = None if digital in (None, 'None', '') else str(digital)
            if analog or digital:
                out[name] = DeviceChannels(name, analog, digital, kind)
    return out


def assign_owners(engines: Sequence['ScanEngine'], channels: Mapping[str, DeviceChannels]
                  ) -> Dict[str, Optional[str]]:
    """``{channel: engine name}`` for every channel in the setup; ``None`` for a
    channel no engine owns. A channel two engines claim is a setup error that
    names both."""
    owners = {}
    conflicts = []
    for entry in channels.values():
        for channel in (entry.analog, entry.digital):
            if channel is None or channel in owners:
                continue
            claiming = [engine.name for engine in engines if engine.owns(channel)]
            if len(claiming) > 1:
                conflicts.append(f'{channel} ({entry.device}) is claimed by '
                                 f'{" and ".join(claiming)}')
            owners[channel] = claiming[0] if claiming else None
    if conflicts:
        raise SetupError('Channel ownership must be unambiguous:\n- ' + '\n- '.join(conflicts))
    return owners


class ScanEngine(ABC):
    """An engine that runs scans. Subclasses provide the signals
    ``sigScanBuilt``, ``sigScanStarted``, ``sigScanDone`` and
    ``sigScanBuildFailed`` (the NI-DAQ manager's, for the NI-DAQ engine), and
    may add ``sigBarrierReleased``, ``sigScanStopped`` and ``sigScanFailed``.

    A run ends in exactly one of three ways, and a panel ends its run on the
    last signal: done -- ``sigScanDone``; stopped -- ``sigScanStopped`` then
    ``sigScanDone``; failed -- ``sigScanFailed(reason)`` then
    ``sigScanBuildFailed``."""

    name: str = ''

    @abstractmethod
    def capabilities(self) -> EngineCapabilities: ...

    @abstractmethod
    def owns(self, channel: str) -> bool: ...

    def assess(self, program, channels: Mapping[str, DeviceChannels]) -> FeasibilityReport:
        """The feasibility of ``program`` on this engine (structure only; the
        timing realization is checked when it is compiled)."""
        return assess_program(program, self.capabilities(), self.owns, channels)

    @abstractmethod
    def abort(self) -> str:
        """Stop what runs; returns what that meant ('none': the iteration
        finishes, 'step-boundary', 'immediate')."""


def _transition_shapes(program):
    return {drive.motion.transition for drive in program.drives}


def assess_program(program, caps: EngineCapabilities, owns, channels) -> FeasibilityReport:
    """The generic structural assessment every engine starts from."""
    report = FeasibilityReport(caps.name)
    stepped = [loop for loop in program.loops if loop.mode == 'stepped'
               and any(loop.id in d.weights_um or d.table_loop == loop.id
                       for d in program.drives)]
    swept = [loop for loop in program.loops if loop.mode == 'swept']

    if stepped:
        verdict = {'native': NATIVE, 'emulated': EMULATED}.get(caps.stepping, IMPOSSIBLE)
        report.add('stepped positions', verdict,
                   '' if verdict != IMPOSSIBLE else f'{caps.name} cannot step a position.',
                   cost='played as sampled waveforms' if verdict == EMULATED else '')
    if swept:
        report.add('swept loop', NATIVE if caps.streaming else IMPOSSIBLE,
                   '' if caps.streaming else f'{caps.name} cannot sweep: it does not stream.',
                   'Make the axis stepped.' if not caps.streaming and caps.stepping != 'none'
                   else '')
    for shape in sorted(_transition_shapes(program)):
        if shape in caps.transitions:
            report.add(f'{shape} transition', NATIVE)
        else:
            report.add(f'{shape} transition', IMPOSSIBLE,
                       f'{caps.name} moves with {", ".join(sorted(caps.transitions))} only.')

    trajectory = getattr(program, 'trajectory', None)
    if trajectory is not None:
        report.add('trajectory', NATIVE if caps.streaming else IMPOSSIBLE,
                   '' if caps.streaming else
                   f'{caps.name} cannot follow a path: it does not stream.')
        for device in trajectory.devices:
            entry = channels.get(device)
            if entry is None or entry.analog is None:
                report.add(f'path {device}', IMPOSSIBLE, f'{device} has no analog channel.')
            elif not owns(entry.analog):
                report.add(f'path {device}', IMPOSSIBLE,
                           f'{device} is wired to {entry.analog}, which {caps.name} does not '
                           'drive.')
            else:
                report.add(f'path {device}', NATIVE)

    for drive in program.drives:
        entry = channels.get(drive.device)
        if entry is None or entry.analog is None:
            report.add(f'drive {drive.device}', IMPOSSIBLE,
                       f'{drive.device} has no analog channel.')
        elif not owns(entry.analog):
            report.add(f'drive {drive.device}', IMPOSSIBLE,
                       f'{drive.device} is wired to {entry.analog}, which {caps.name} does not '
                       'drive.')
        else:
            report.add(f'drive {drive.device}', NATIVE)

    seen = set()
    for event in program.timeline:
        key = (event.device, event.kind)
        if key in seen:
            continue
        seen.add(key)
        feature = f'{event.kind} {event.device}'
        entry = channels.get(event.device)
        if event.kind in ('ttl', 'detector'):
            line = entry.digital if entry else None
            if line is None:
                report.add(feature, IMPOSSIBLE, f'{event.device} has no digital line.')
            elif not owns(line):
                report.add(feature, IMPOSSIBLE,
                           f'{event.device} is wired to {line}, which {caps.name} does not '
                           'drive.', 'Wire it to this engine, or run the scan on its engine.')
            else:
                report.add(feature, NATIVE)
        elif event.kind == 'marker':
            report.add(feature, NATIVE if event.device in caps.triggers_out
                       or owns(event.device) else UNKNOWN,
                       '' if event.device in caps.triggers_out or owns(event.device)
                       else f'{caps.name} does not declare a {event.device} output.')
        elif event.kind == 'level':
            line = entry.analog if entry else None
            report.add(feature, NATIVE if line and owns(line) else IMPOSSIBLE,
                       '' if line and owns(line) else
                       f'{event.device} has no analog channel {caps.name} drives.')
        elif event.kind == 'offset':
            line = entry.analog if entry else None
            ok = line and owns(line) and caps.intra_step_offsets
            report.add(feature, NATIVE if ok else IMPOSSIBLE,
                       '' if ok else f'{event.device} cannot take an intra-step offset on '
                                     f'{caps.name}.')

    for wait in program.waits:
        if wait.trigger == 'time':
            report.add('time wait', NATIVE if 'time' in caps.waits else IMPOSSIBLE,
                       '' if 'time' in caps.waits else f'{caps.name} cannot wait a fixed time.')
            continue
        kind = 'start-trigger' if wait.at == 'start' else 'step-trigger'
        if kind not in caps.waits:
            report.add(f'trigger wait at {wait.at}', IMPOSSIBLE,
                       f'{caps.name} cannot wait for an external trigger '
                       f'({"before the run" if kind == "start-trigger" else "between steps"}).')
        elif caps.trigger_latency_s is None:
            report.add(f'trigger wait at {wait.at}', UNKNOWN,
                       f'{caps.name} does not declare its trigger latency.')
        else:
            report.add(f'trigger wait at {wait.at}', NATIVE)
    return report


__all__ = [
    'AnalogOut', 'DeviceChannels', 'EMULATED', 'EngineCapabilities', 'FeasibilityReport',
    'FeatureVerdict', 'IMPOSSIBLE', 'NATIVE', 'ScanEngine', 'ScanEngineRefused',
    'SetupError', 'UNKNOWN', 'assess_program', 'assign_owners', 'setup_channels',
]


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
