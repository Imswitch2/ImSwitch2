"""What a setup can scan, before anyone builds a scan (docs/design/plans/
scan-engines-and-expert-scan.md §3.3; Hardware → What this setup can scan…).

For every scan engine of the setup: what the panel does with it (runs on it,
it follows that engine, or neither), every feature with its verdict --
native, emulated with the cost, impossible with the reason, unknown when the
engine does not declare what it needs -- the numbers it declares with where
each came from, and the setup's devices on its channels. Then the devices on
no engine's channel, which take part in no scan.

The verdicts are the engines' own: each feature is a small probe program on
the engine's own channels, taken through ``engine.assess`` -- the report a
scan is refused by -- and a follower's link through ``split``'s drift rule.
This restates nothing a run decides differently (§7.9). Qt-free.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import List, Optional

from imswitch.imcontrol.model.scan_program.program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait,
)
from imswitch.imcontrol.model.scan_program.engine import (
    DeviceChannels, EMULATED, IMPOSSIBLE, NATIVE, UNKNOWN, setup_channels,
)
from imswitch.imcontrol.model.scan_program.motion import TRANSITIONS
from imswitch.imcontrol.model.scan_program.split import Wire, max_pulse_interval_s

#: What an engine does with a device on each kind of channel.
_USE = {('positioner', 'analog'): 'its position',
        ('laser', 'analog'): 'its power level',
        ('laser', 'digital'): 'on and off',
        ('detector', 'digital'): 'its trigger'}

_STOP = {'immediate': (NATIVE, 'a stop takes effect at once.'),
         'step-boundary': (NATIVE, 'a stop takes effect at the end of the step.'),
         'none': (IMPOSSIBLE, 'a scan runs to the end of its iteration once started.')}


@dataclass(frozen=True)
class Entry:
    item: str
    text: str = ''
    #: A feature's verdict; ``None`` for a plain fact.
    verdict: Optional[str] = None

    def line(self) -> str:
        head = f'{self.verdict}: {self.item}' if self.verdict else self.item
        return f'{head} -- {self.text}' if self.text else head


@dataclass
class EngineSection:
    engine: str
    kind: str
    role: str
    features: List[Entry] = field(default_factory=list)
    numbers: List[Entry] = field(default_factory=list)
    devices: List[Entry] = field(default_factory=list)


@dataclass
class SetupCapabilities:
    engines: List[EngineSection]
    #: Devices on no engine's channel.
    unscanned: List[Entry]

    def section(self, engine) -> Optional[EngineSection]:
        return next((s for s in self.engines if s.engine == engine), None)

    def text(self) -> str:
        out = []
        for section in self.engines:
            out.append(f'{section.engine} ({section.kind})')
            out.append(f'  {section.role}')
            for title, entries in (('Features', section.features),
                                   ('Declared', section.numbers),
                                   ('Devices', section.devices)):
                if entries:
                    out.append(f'  {title}')
                    out.extend(f'    {entry.line()}' for entry in entries)
            out.append('')
        if self.unscanned:
            out.append('On no scan engine')
            out.extend(f'  {entry.line()}' for entry in self.unscanned)
        return '\n'.join(out).rstrip() + '\n'


def _seconds(value: float) -> str:
    for scale, unit in ((1.0, 's'), (1e-3, 'ms'), (1e-6, 'µs')):
        if abs(value) >= scale:
            return f'{value / scale:g} {unit}'
    return f'{value / 1e-9:g} ns'


def _declared(caps, key, text, *, needed, why=''):
    """A declared number with where it came from; one that is not declared
    is unknown where something needs it, a plain fact elsewhere."""
    source = caps.sources.get(key)
    if text is not None:
        return Entry(key_names[key], f'{text}, from {source}' if source else text)
    if needed:
        return Entry(key_names[key], f'not declared: {why}', UNKNOWN)
    return Entry(key_names[key], 'not declared')


key_names = {'timing_grid_s': 'timing grid', 'min_pulse_s': 'shortest pulse',
             'max_samples': 'most samples', 'max_steps': 'most steps',
             'trigger_latency_s': 'trigger latency',
             'trigger_min_pulse_s': 'shortest trigger pulse',
             'clock_accuracy_ppm': 'clock accuracy'}


#: How bad a verdict is, for the worst of several.
_SEVERITY = {NATIVE: 0, EMULATED: 1, UNKNOWN: 2, IMPOSSIBLE: 3}

#: Each feature a probe tries, and the feature names an engine's own
#: assessment reports it under (the generic ones, a pulse generator's own).
_PROBED = (
    ('swept axes and trajectories', ('swept loop',)),
    ('stepped positions', ('stepped loop',)),
    *((f'{shape} transition', (f'{shape} transition',)) for shape in TRANSITIONS),
    ('intra-step offsets', ('offset probe stage',)),
    ('time waits', ('time wait',)),
    ('trigger wait before the run', ('trigger wait at start', 'start trigger')),
    ('trigger wait between steps', ('trigger wait at step',)),
)


def _worst(verdicts):
    return max(verdicts, key=lambda v: _SEVERITY.get(v.verdict, _SEVERITY[UNKNOWN]))


def _probe_programs(caps):
    """``(channels, baseline, {feature: program})``: each feature tried as a
    small variation of a minimal program on the engine's own first line and
    first analog output. The probes' times are the engine's own ticks (two
    to a step, the line on for the first); an engine that declares no grid
    is unknown in every probe anyway. ``None`` when the engine has neither a
    line nor an analog output in this setup."""
    line = caps.digital[0] if caps.digital else None
    stage = next(iter(caps.analog), None)
    if line is None and stage is None:
        return None
    tick = caps.timing_grid_s if caps.timing_grid_s else 1.0
    channels, timeline, drives = {}, (), ()
    jump = Motion(transition='jump', origin='absolute', restore='first')
    if line is not None:
        channels['probe line'] = DeviceChannels('probe line', digital=line, kind='laser')
        timeline = (TimelineEvent('probe line', 'ttl', 0.0, tick, during='any'),)
    if stage is not None:
        channels['probe stage'] = DeviceChannels('probe stage', analog=stage,
                                                 kind='positioner')
        drives = (DeviceDrive('probe stage', weights_um={'x': 1.0}, motion=jump),)

    def program(loop_mode='stepped', drives=drives, timeline=timeline, waits=()):
        return ScanProgram(loops=(Loop('x', 'scan_x', 2, mode=loop_mode),), drives=drives,
                           step_s=2 * tick, timeline=timeline, waits=waits,
                           timing_policy='strict-v1', approach=False)

    probes = {'time waits': program(waits=(Wait('start', 'time', interval_s=tick),)),
              'trigger wait before the run': program(waits=(Wait('start', 'external'),)),
              'trigger wait between steps': program(waits=(Wait('step', 'external'),))}
    if stage is not None:
        probes['swept axes and trajectories'] = program(loop_mode='swept')
        probes['stepped positions'] = program()
        for shape in TRANSITIONS:
            probes[f'{shape} transition'] = program(drives=(dataclasses.replace(
                drives[0], motion=dataclasses.replace(jump, transition=shape)),))
        probes['intra-step offsets'] = program(timeline=timeline + (TimelineEvent(
            'probe stage', 'offset', 0.0, tick, value=1.0, during='any'),))
    return channels, program(), probes


def _features(engine, caps, unanswered) -> List[Entry]:
    """Each feature's verdict, as the engine's own assessment gives it: the
    probes go through ``engine.assess`` -- the report a scan is refused by
    -- so this restates nothing (§7.9)."""
    if unanswered:
        return [Entry('board', f'has not answered ({unanswered}), so it declares nothing '
                      'and nothing can be tried on it; opening this asks it again.', UNKNOWN)]
    probes = _probe_programs(caps)
    out = []
    if probes is None:
        out.append(Entry('every feature', 'no line or analog output of it is in this setup, '
                         'so nothing can be tried on it.', UNKNOWN))
    else:
        channels, baseline, programs = probes
        base = engine.assess(baseline, channels)
        floor = {v.feature for v in base.verdicts}
        for verdict in base.verdicts:
            if verdict.verdict != NATIVE and 'probe ' not in verdict.feature:
                out.append(Entry(f'any scan: {verdict.feature}', verdict.reason or verdict.cost,
                                 verdict.verdict))
        for feature, names in _PROBED:
            program = programs.get(feature)
            if program is None:
                out.append(Entry(feature, 'it has no analog output in this setup.', IMPOSSIBLE))
                continue
            report = engine.assess(program, channels)
            named = [v for v in report.verdicts if v.feature in names]
            new = named or [v for v in report.verdicts if v.feature not in floor]
            if not new:
                out.append(Entry(feature, '', NATIVE))
                continue
            worst = _worst(new)
            out.append(Entry(feature, worst.reason or worst.cost, worst.verdict))
    verdict, text = _STOP.get(caps.abort, (UNKNOWN, f'it declares {caps.abort!r}.'))
    source = caps.sources.get('abort')
    out.append(Entry('stop mid-scan', f'{text} ({source})' if source else text, verdict))
    return out


def _numbers(caps, *, linked) -> List[Entry]:
    triggered = bool({'start-trigger', 'step-trigger'} & set(caps.waits))
    grid = caps.timing_grid_s
    out = [_declared(caps, 'timing_grid_s',
                     None if grid is None else f'{_seconds(grid)} ({1.0 / grid:g} Hz)',
                     needed=True, why='no edge can be placed.')]
    if caps.min_pulse_s is not None:
        out.append(_declared(caps, 'min_pulse_s', _seconds(caps.min_pulse_s), needed=False))
    if caps.streaming:
        out.append(_declared(caps, 'max_samples', None if caps.max_samples is None
                             else f'{caps.max_samples:d}', needed=False))
    if caps.stepping == 'native' or caps.max_steps is not None:
        out.append(_declared(caps, 'max_steps', None if caps.max_steps is None
                             else f'{caps.max_steps:d}', needed=False))
    if triggered:
        out.append(_declared(caps, 'trigger_latency_s', None if caps.trigger_latency_s is None
                             else _seconds(caps.trigger_latency_s), needed=True,
                             why='where a triggered step runs is unknown.'))
        out.append(_declared(caps, 'trigger_min_pulse_s',
                             None if caps.trigger_min_pulse_s is None
                             else _seconds(caps.trigger_min_pulse_s), needed=linked,
                             why='how long a leader\'s pulse must be is unknown.'))
    out.append(_declared(caps, 'clock_accuracy_ppm', None if caps.clock_accuracy_ppm is None
                         else f'{caps.clock_accuracy_ppm:g} ppm', needed=linked,
                         why='the drift between it and the engine it is linked to is '
                             'unknown.'))
    out.append(Entry('safe state', caps.safe_state))
    return out


def _devices(name, caps, owners, channels) -> List[Entry]:
    out = []
    for entry in channels.values():
        for side, channel in (('analog', entry.analog), ('digital', entry.digital)):
            if channel is None or owners.get(channel) != name:
                continue
            what = f'{channel}: {_USE.get((entry.kind, side), side)}'
            out_range = caps.analog.get(channel) if side == 'analog' else None
            if out_range is not None and None not in (out_range.min_v, out_range.max_v):
                what += f', {out_range.min_v:g} to {out_range.max_v:g} V'
            out.append(Entry(f'{entry.device} ({entry.kind})', what))
    return out


def _follow(name, engine, line, leader, simulated) -> List[Entry]:
    """A follower's link to ``leader``: the checks a scan that uses it is
    assessed with (CompositeScanEngine), without a program."""
    from .CompositeScanEngine import can_lead, lead_refusal
    caps, lead = engine.capabilities(), leader.capabilities()
    if not can_lead(leader):
        return [Entry('follow step by step', lead_refusal(leader, [name]), IMPOSSIBLE)]
    out = []
    if 'step-trigger' not in caps.waits:
        out.append(Entry('follow step by step', 'it cannot wait for a pulse before every '
                         'step.', IMPOSSIBLE))
    elif caps.trigger_latency_s is None:
        out.append(Entry('follow step by step', 'it does not declare its trigger latency, so '
                         'where it runs in the step is unknown.', UNKNOWN))
    elif simulated(engine) != simulated(leader):
        out.append(Entry('follow step by step',
                         f'{"a simulated" if simulated(leader) else "a real"} {leader.name} '
                         f'cannot pulse {"a real" if simulated(leader) else "a simulated"} '
                         f'{name}.', IMPOSSIBLE))
    else:
        out.append(Entry('follow step by step', f'it starts {_seconds(caps.trigger_latency_s)} '
                         f'after each pulse on {line}.', NATIVE))
    wire = Wire(name, line, caps.trigger_latency_s, caps.trigger_min_pulse_s,
                caps.timing_grid_s, caps.clock_accuracy_ppm)
    longest = max_pulse_interval_s(lead.timing_grid_s, lead.clock_accuracy_ppm, wire)
    if longest is None:
        missing = [f'{who}\'s {what}' for who, what, value in (
            (leader.name, 'clock accuracy', lead.clock_accuracy_ppm),
            (name, 'clock accuracy', caps.clock_accuracy_ppm),
            (leader.name, 'timing grid', lead.timing_grid_s),
            (name, 'timing grid', caps.timing_grid_s)) if value is None]
        out.append(Entry('pulses apart', f'{" and ".join(missing)} not declared, so the drift '
                         'between pulses is unknown.', UNKNOWN))
    elif longest == math.inf:
        out.append(Entry('pulses apart', 'any time: both clocks are declared exact.', NATIVE))
    else:
        out.append(Entry('pulses apart', f'at most {_seconds(longest)} (a step and the moves '
                         'after it); longer, the clocks drift more than half a tick '
                         f'({lead.clock_accuracy_ppm:g} + {caps.clock_accuracy_ppm:g} ppm) and '
                         'the scan is refused.', NATIVE))
    return out


def setup_capabilities(scanEngines, setupInfo) -> SetupCapabilities:
    """The report for ``scanEngines`` (``build_scan_engines``) on
    ``setupInfo``."""
    from .CompositeScanEngine import can_lead, lead_refusal, simulated
    channels = setup_channels(setupInfo)
    owners = dict(scanEngines.owners)
    selected = scanEngines.selected
    composite = scanEngines.composite
    followers = dict(composite.followers) if composite is not None else {}
    leader = scanEngines.engines[selected]
    sections = []
    for name, engine in scanEngines.engines.items():
        caps = engine.capabilities()
        linked = name == selected and bool(followers) or name in followers
        devices = _devices(name, caps, owners, channels)
        if name == selected:
            role = 'The scan panel runs on it (scan.engine).'
            if followers and can_lead(leader):
                role += (f' It leads {", ".join(sorted(followers))}, pulsing the wired trigger '
                         'input at every step of a scan that uses a follower\'s devices.')
            elif followers:
                role += (f' {", ".join(sorted(followers))} {"is" if len(followers) == 1 else "are"}'
                         f' wired to follow it, but {lead_refusal(leader, sorted(followers))}')
        elif name in followers:
            role = (f'Follows {selected} step by step: its trigger input is wired to '
                    f'{followers[name][1]}.')
        else:
            role = f'In no scan: the panel runs on {selected}.'
            if not devices:
                role += ' No device of the setup is on its channels.'
            elif 'step-trigger' in caps.waits and can_lead(leader):
                role += (f' To scan its devices, wire its triggerInput to a line of {selected} '
                         'for it to follow, or make it scan.engine.')
            else:
                role += ' To scan its devices, make it scan.engine.'
        section = EngineSection(name, type(engine).__name__, role,
                                _features(engine, caps, getattr(engine, 'unanswered', None)),
                                _numbers(caps, linked=linked), devices)
        if name in followers:
            section.features += _follow(name, engine, followers[name][1], leader, simulated)
        sections.append(section)
    unscanned = []
    for entry in channels.values():
        for channel in (entry.analog, entry.digital):
            if channel is not None and owners.get(channel) is None:
                unscanned.append(Entry(f'{entry.device} ({entry.kind})',
                                       f'{channel} is on no scan engine\'s channels, so '
                                       f'{entry.device} takes part in no scan.', IMPOSSIBLE))
    return SetupCapabilities(sections, unscanned)


__all__ = ['Entry', 'EngineSection', 'SetupCapabilities', 'setup_capabilities']


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
