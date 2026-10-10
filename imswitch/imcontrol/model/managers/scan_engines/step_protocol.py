"""The step-engine serial protocol, IMSWITCH_STEP/1 (docs/design/plans/
scan-engines-and-expert-scan.md §5.2, M5). ``docs/devices/step-engine-protocol.rst``
is the specification a firmware implements; this module is its codec, shared
by the host engine (``StepProtocolScanEngine``) and the simulated board
(``SimulatedStepBoard``).

A board that steps -- the TriggerScope once its firmware is rewritten, or any
microcontroller with DACs and digital lines -- is uploaded a plan derived from
the scan model, not from any firmware's routines, and runs it on its own:
loops, drives as affine weights per DAC channel (the board evaluates
``offset + Σ weight × counter``) or tables, the step's events per phase,
waits, the preparation after a release, the captured and park positions, and
every line's safe level.

Lines are ASCII, ``\\n``-terminated, fields comma-separated. Times are whole
ticks of the board's grid; values are decimal numbers in the board's units
(volts for a DAC, the electrical level 0 or 1 for a line); loops are indices,
outermost first; ``-`` is "none", ``;`` separates the items of a list. A text
(a reason) is always the last field and may contain commas.

Qt-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .step_executor import RunLog, StepPlan

PROTOCOL = 'IMSWITCH_STEP/1'
NONE = '-'

#: The specification's integer widths: counts, durations and indices are
#: unsigned 32-bit; event times, relative to their anchor (a pass end's are
#: <= 0), signed 32-bit; the run clock in messages and logs unsigned 64-bit.
UINT32_MAX = 2 ** 32 - 1
INT32_MIN, INT32_MAX = -2 ** 31, 2 ** 31 - 1
UINT64_MAX = 2 ** 64 - 1

#: Error codes (``ERR,<code>,<text>``).
SYNTAX, RANGE, STATE, CAPACITY, CHANNEL = 'syntax', 'range', 'state', 'capacity', 'channel'

_CHANNEL = re.compile(r'^(dac|ttl|time)(\d+)$')


class ProtocolError(ValueError):
    """A line that is not this protocol, a number out of its range, or a
    reply the host did not expect. ``code`` is the ``ERR`` code a board
    answers with."""

    def __init__(self, message, code=SYNTAX):
        super().__init__(message)
        self.code = code


def text(value) -> str:
    """A reason as one line."""
    return ' '.join(str(value).split())


def channel(token) -> Tuple[str, int]:
    """``('dac', 0)`` for ``dac0``; ProtocolError for anything else. A
    ``time<k>`` channel drives nothing: a drive on it is timing only."""
    match = _CHANNEL.match(str(token))
    if match is None:
        raise ProtocolError(f'{token!r} is not a channel (dac<i>, ttl<j> or time<k>)',
                            CHANNEL)
    return match.group(1), int(match.group(2))


def _num(value) -> str:
    """A number as the wire writes it: integers plain, floats exactly."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _float(value, where) -> float:
    try:
        return float(value)
    except ValueError:
        raise ProtocolError(f'{where}: {value!r} is not a number') from None


def _whole(value, where, low, high, width) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ProtocolError(f'{where}: {value!r} is not a whole number') from None
    if not low <= number <= high:
        raise ProtocolError(f'{where}: {number} does not fit {width}', RANGE)
    return number


def _uint(value, where) -> int:
    return _whole(value, where, 0, UINT32_MAX, 'an unsigned 32-bit number')


def _int(value, where) -> int:
    return _whole(value, where, INT32_MIN, INT32_MAX, 'a signed 32-bit number')


def _wall(value, where) -> int:
    return _whole(value, where, 0, UINT64_MAX, 'an unsigned 64-bit number')


def _opt(value, convert, where):
    return None if value == NONE else convert(value, where)


def _list(items) -> str:
    items = list(items)
    return ';'.join(_num(i) for i in items) if items else NONE


def _parse_list(value, convert, where) -> list:
    return [] if value == NONE else [convert(item, where) for item in value.split(';')]


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class WirePlan(StepPlan):
    """A plan as a board holds it: devices are its channels (``dac0``,
    ``ttl3``), positions volts, and every line it switches has a safe level --
    where a stop, a failure and the end of a run leave it. A DAC holds its
    position."""
    safe: Dict[str, float] = field(default_factory=dict)


def plan_to_lines(plan: WirePlan) -> List[str]:
    """``PROGRAM,BEGIN`` .. ``PROGRAM,END``."""
    index = {loop[0]: i for i, loop in enumerate(plan.loops)}

    def loop(loop_id):
        return NONE if loop_id is None else str(index[loop_id])

    lines = ['PROGRAM,BEGIN']
    for _, count, traversal, parity in plan.loops:
        lines.append(f'LOOP,{count},{traversal},{_list(index[p] for p in parity)}')
    lines.append(f'PHASE,{loop(plan.phase_loop)}')
    lines.append(f'STEP,{plan.step_ticks},{plan.return_ticks}')
    for d in plan.drives:
        lines.append(f'DRIVE,{d["device"]},{_num(d["offset"])},{d["move"]},{d["settle"]},'
                     f'{d["placement"]},{d["origin"]},{d["restore"]}')
        for loop_id, weight in d['weights'].items():
            lines.append(f'WEIGHT,{d["device"]},{index[loop_id]},{_num(weight)}')
        if d['table'] is not None:
            lines.append(f'TABLE,{d["device"]},{index[d["table_loop"]]},{_list(d["table"])}')
    for e in plan.events:
        phases = NONE if e['phases'] is None else _list(sorted(e['phases']))
        lines.append(f'EVENT,{e["device"]},{e["start"]},{e["end"]},{e["anchor"]},'
                     f'{loop(e["anchor_loop"])},{phases},{_num(e["on"])},{_num(e["off"])}')
    for w in plan.waits:
        at = w['at'] if w['at'] in ('start', 'step') else str(index[w['at']])
        lines.append(f'WAIT,{at},{"time" if w["trigger"] == "time" else "trigger"},'
                     f'{NONE if w["interval"] is None else w["interval"]},'
                     f'{NONE if w["timeout"] is None else w["timeout"]},{w["on_timeout"]}')
    for name, ticks in plan.preparation.items():
        lines.append(f'PREPARE,{name},{ticks}')
    for name, value in plan.captured_um.items():
        lines.append(f'CAPTURED,{name},{_num(value)}')
    for name, value in plan.park_um.items():
        lines.append(f'PARK,{name},{_num(value)}')
    for name, value in plan.safe.items():
        lines.append(f'SAFE,{name},{_num(value)}')
    lines.append('PROGRAM,END')
    return lines


_CHOICES = {
    'traversal': ('forward', 'reverse', 'serpentine'),
    'placement': ('in-step', 'between-steps'),
    'origin': ('absolute', 'captured'),
    'restore': ('captured', 'first', 'park', 'hold'),
    'anchor': ('step', 'pass-start', 'pass-end'),
    'on_timeout': ('fail', 'continue'),
}


def _choice(value, what, where):
    if value not in _CHOICES[what]:
        raise ProtocolError(f'{where}: {what} {value!r} is not one of '
                            f'{", ".join(_CHOICES[what])}')
    return value


def lines_to_plan(lines: Sequence[str]) -> WirePlan:
    """The plan a board decodes from ``PROGRAM,BEGIN`` .. ``PROGRAM,END``."""
    lines = [line.strip() for line in lines if line.strip()]
    if not lines or lines[0] != 'PROGRAM,BEGIN' or lines[-1] != 'PROGRAM,END':
        raise ProtocolError('A program is sent between PROGRAM,BEGIN and PROGRAM,END.')
    loops, drives, events, waits = [], {}, [], []
    plan = dict(phase_loop=None, step_ticks=None, return_ticks=0, preparation={},
                captured_um={}, park_um={}, safe={})

    for number, line in enumerate(lines[1:-1], start=2):
        kind, *f = line.split(',')
        where = f'line {number} ({kind})'

        def loop_id(value):
            if value == NONE:
                return None
            i = _uint(value, where)
            if i >= len(loops):
                raise ProtocolError(f'{where}: loop {i} is not declared before it', RANGE)
            return f'L{i}'

        def device(value):
            channel(value)
            return value

        if kind == 'LOOP' and len(f) == 3:
            parity = tuple(loop_id(i) for i in ([] if f[2] == NONE else f[2].split(';')))
            loops.append((f'L{len(loops)}', _uint(f[0], where),
                          _choice(f[1], 'traversal', where), parity))
        elif kind == 'PHASE' and len(f) == 1:
            plan['phase_loop'] = loop_id(f[0])
        elif kind == 'STEP' and len(f) == 2:
            plan['step_ticks'], plan['return_ticks'] = _uint(f[0], where), _uint(f[1], where)
        elif kind == 'DRIVE' and len(f) == 7:
            drives[device(f[0])] = dict(
                device=f[0], channel=f[0], output=channel(f[0])[0] != 'time',
                offset=_float(f[1], where), weights={},
                table_loop=None, table=None, move=_uint(f[2], where),
                settle=_uint(f[3], where), placement=_choice(f[4], 'placement', where),
                origin=_choice(f[5], 'origin', where), restore=_choice(f[6], 'restore', where))
        elif kind == 'WEIGHT' and len(f) == 3:
            _drive(drives, f[0], where)['weights'][loop_id(f[1])] = _float(f[2], where)
        elif kind == 'TABLE' and len(f) == 3:
            drive = _drive(drives, f[0], where)
            drive['table_loop'] = loop_id(f[1])
            drive['table'] = _parse_list(f[2], _float, where)
        elif kind == 'EVENT' and len(f) == 8:
            level = _level(device(f[0]))
            events.append(dict(
                device=f[0], kind='event', start=_int(f[1], where),
                end=_int(f[2], where), anchor=_choice(f[3], 'anchor', where),
                anchor_loop=loop_id(f[4]),
                phases=None if f[5] == NONE else set(_parse_list(f[5], _uint, where)),
                value=None, on=level(f[6], where), off=level(f[7], where)))
        elif kind == 'WAIT' and len(f) == 5:
            at = f[0] if f[0] in ('start', 'step') else loop_id(f[0])
            if f[1] not in ('time', 'trigger'):
                raise ProtocolError(f'{where}: a wait is for time or the trigger, not {f[1]!r}')
            waits.append(dict(at=at, trigger=f[1], interval=_opt(f[2], _uint, where),
                              timeout=_opt(f[3], _uint, where),
                              on_timeout=_choice(f[4], 'on_timeout', where)))
        elif kind == 'PREPARE' and len(f) == 2:
            plan['preparation'][device(f[0])] = _uint(f[1], where)
        elif kind in ('CAPTURED', 'PARK') and len(f) == 2:
            key = {'CAPTURED': 'captured_um', 'PARK': 'park_um'}[kind]
            plan[key][device(f[0])] = _float(f[1], where)
        elif kind == 'SAFE' and len(f) == 2:
            plan['safe'][device(f[0])] = _level(f[0])(f[1], where)
        else:
            raise ProtocolError(f'{where}: not a program line of {PROTOCOL}: {line!r}')
    if plan['step_ticks'] is None:
        raise ProtocolError('The program has no STEP line.')
    if not loops:
        raise ProtocolError('The program has no LOOP line.')
    for w in waits:
        if w['trigger'] == 'time' and w['interval'] is None:
            raise ProtocolError('A time wait needs its interval.')
    return WirePlan(loops=loops, drives=list(drives.values()), events=events, waits=waits,
                    **plan)


def _level(name):
    """How a level is read: a line's electrical level is 0 or 1, a DAC's is
    volts (a laser's power inside a step, and where a stop leaves it)."""
    return _bit if channel(name)[0] == 'ttl' else _float


def _bit(value, where) -> int:
    return _whole(value, where, 0, 1, 'a line level (0 or 1)')


def level_of(name, value, where):
    """A channel's value as the wire reads it: a line's 0 or 1, a DAC's
    volts."""
    return _level(name)(value, where)


def _drive(drives, name, where):
    if name not in drives:
        raise ProtocolError(f'{where}: {name} has no DRIVE line before it')
    return drives[name]


# ---------------------------------------------------------------------------
# Identification and capabilities
# ---------------------------------------------------------------------------

@dataclass
class BoardCaps:
    """What a board says about itself (``*IDN?``, ``*CAPS?``). ``None``: not
    declared, and a scan that needs it is refused as unknown."""
    board: str
    firmware: str
    grid_ns: Optional[int] = None
    dacs: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    ttl_lines: int = 0
    max_steps: Optional[int] = None
    trigger_in: bool = False
    trigger_latency_ticks: Optional[int] = None
    #: The shortest pulse its trigger input takes, in ticks (M6).
    trigger_min_ticks: Optional[int] = None
    #: Its clock's accuracy, in parts per million (M6).
    clock_ppm: Optional[float] = None
    #: Each DAC's resolution in bits: it sets the nearest of 2**bits codes
    #: over its range (§7.8). Not declared: the volts it is sent.
    dac_bits: Dict[int, int] = field(default_factory=dict)
    #: While a program runs, a message at least this often, in ticks
    #: (``WAITING`` while it waits): so a host can tell a board that fell
    #: silent from one that waits (§7.8). Not declared: it cannot.
    alive_ticks: Optional[int] = None

    def dac_step(self, index) -> Optional[float]:
        """DAC ``index``'s smallest step in volts, or ``None``."""
        bits = self.dac_bits.get(index)
        if bits is None or index not in self.dacs:
            return None
        low, high = self.dacs[index]
        return (high - low) / (2 ** bits - 1)

    def dac_value(self, index, volts) -> float:
        """What DAC ``index`` sets for ``volts``: the nearest code."""
        step = self.dac_step(index)
        if step is None:
            return float(volts)
        low = self.dacs[index][0]
        return low + round((float(volts) - low) / step) * step

    def idn(self) -> str:
        return f'{PROTOCOL},{self.board},{self.firmware}'

    def lines(self) -> List[str]:
        out = []
        if self.grid_ns is not None:
            out.append(f'CAPS,grid_ns,{self.grid_ns}')
        for index, (low, high) in sorted(self.dacs.items()):
            out.append(f'CAPS,dac,{index},{_num(low)},{_num(high)}')
        out.append(f'CAPS,ttl,{self.ttl_lines}')
        if self.max_steps is not None:
            out.append(f'CAPS,max_steps,{self.max_steps}')
        out.append(f'CAPS,trigger_in,{_num(self.trigger_in)}')
        if self.trigger_latency_ticks is not None:
            out.append(f'CAPS,trigger_latency_ticks,{self.trigger_latency_ticks}')
        if self.trigger_min_ticks is not None:
            out.append(f'CAPS,trigger_min_ticks,{self.trigger_min_ticks}')
        if self.clock_ppm is not None:
            out.append(f'CAPS,clock_ppm,{_num(self.clock_ppm)}')
        for index, bits in sorted(self.dac_bits.items()):
            out.append(f'CAPS,dac_bits,{index},{bits}')
        if self.alive_ticks is not None:
            out.append(f'CAPS,alive_ticks,{self.alive_ticks}')
        return out + ['CAPS,END']

    def has(self, token) -> bool:
        try:
            kind, index = channel(token)
        except ProtocolError:
            return False
        if kind == 'time':
            return True             # it drives nothing, so every board has it
        return index in self.dacs if kind == 'dac' else index < self.ttl_lines


def parse_idn(line: str) -> Tuple[str, str]:
    """``(board, firmware)`` from an ``*IDN?`` answer; ProtocolError when the
    board speaks another protocol, or another version of this one."""
    protocol, *rest = (line or '').strip().split(',')
    if protocol != PROTOCOL:
        raise ProtocolError(f'The board answers {text(line)!r}; this ImSwitch speaks '
                            f'{PROTOCOL}.', STATE)
    return (rest[0] if rest else ''), ','.join(rest[1:])


def parse_caps(board: str, firmware: str, lines: Iterable[str]) -> BoardCaps:
    caps = BoardCaps(board, firmware)
    for line in lines:
        parts = line.strip().split(',')
        if len(parts) < 2 or parts[0] != 'CAPS':
            raise ProtocolError(f'Not a capability: {line!r}')
        key, f = parts[1], parts[2:]
        where = f'CAPS {key}'
        if key == 'END':
            return caps
        if key in ('grid_ns', 'ttl', 'max_steps', 'trigger_in', 'trigger_latency_ticks',
                   'trigger_min_ticks', 'clock_ppm', 'alive_ticks') and len(f) != 1 \
                or key == 'dac' and len(f) != 3 or key == 'dac_bits' and len(f) != 2:
            raise ProtocolError(f'{where}: {line!r} has the wrong number of fields')
        if key == 'grid_ns':
            caps.grid_ns = _uint(f[0], where)
        elif key == 'dac':
            caps.dacs[_uint(f[0], where)] = (_float(f[1], where), _float(f[2], where))
        elif key == 'ttl':
            caps.ttl_lines = _uint(f[0], where)
        elif key == 'max_steps':
            caps.max_steps = _uint(f[0], where)
        elif key == 'trigger_in':
            caps.trigger_in = bool(_uint(f[0], where))
        elif key == 'trigger_latency_ticks':
            caps.trigger_latency_ticks = _uint(f[0], where)
        elif key == 'trigger_min_ticks':
            caps.trigger_min_ticks = _uint(f[0], where)
        elif key == 'clock_ppm':
            caps.clock_ppm = _float(f[0], where)
        elif key == 'dac_bits':
            caps.dac_bits[_uint(f[0], where)] = _uint(f[1], where)
        elif key == 'alive_ticks':
            caps.alive_ticks = _uint(f[0], where)
        # Any other key is a capability this host does not use.
    raise ProtocolError('The capabilities end without CAPS,END.')


# ---------------------------------------------------------------------------
# Messages while a board runs, and its log
# ---------------------------------------------------------------------------

def message(line: str) -> Tuple[str, List[str]]:
    """``(kind, fields)``; a text (ERR's, FAILED's) keeps its commas."""
    kind, _, rest = line.strip().partition(',')
    if kind == 'ERR':
        code, _, reason = rest.partition(',')
        return kind, [code, reason]
    if kind == 'FAILED':
        step, _, reason = rest.partition(',')
        return kind, [step, reason]
    return kind, rest.split(',') if rest else []


_LOG_KINDS = {'dac': 'DAC', 'on': 'ON', 'off': 'OFF', 'release': 'RELEASE', 'end': 'FINISH',
              'safe-state': 'SAFE', 'failed': 'FAILED'}
_LOG_NAMES = {wire: name for name, wire in _LOG_KINDS.items()}


def log_lines(log: RunLog) -> List[str]:
    """``LOG,...`` for every entry of a run log, then ``LOG,END``."""
    out = []
    for kind, *fields in log.entries:
        if kind == 'failed':
            fields = [fields[0], text(fields[1])]
        values = [NONE if v is None else (v if isinstance(v, str) else _num(v))
                  for v in fields]
        out.append(','.join(['LOG', _LOG_KINDS[kind], *values]))
    return out + ['LOG,END']


def parse_log(lines: Iterable[str]) -> RunLog:
    log = RunLog()
    for line in lines:
        _, wire, *f = line.strip().split(',')
        if wire == 'END':
            return log
        kind = _LOG_NAMES.get(wire)
        where = f'LOG {wire}'
        if kind == 'dac':
            log.add('dac', _uint(f[0], where), _wall(f[1], where), f[2], _float(f[3], where))
        elif kind in ('on', 'off'):
            log.add(kind, _uint(f[0], where), _wall(f[1], where), f[2], _float(f[3], where))
        elif kind == 'release':
            log.add('release', _uint(f[0], where), _wall(f[1], where), _opt(f[2], _wall, where))
        elif kind == 'end':
            log.add('end', _uint(f[0], where), _wall(f[1], where), _wall(f[2], where))
        elif kind == 'safe-state':
            log.add('safe-state', _wall(f[0], where))
        elif kind == 'failed':
            log.add('failed', _wall(f[0], where), ','.join(f[1:]))
        else:
            raise ProtocolError(f'Not a log line of {PROTOCOL}: {line!r}')
    raise ProtocolError('The log ends without LOG,END.')


__all__ = ['BoardCaps', 'CAPACITY', 'CHANNEL', 'INT32_MAX', 'INT32_MIN', 'NONE', 'PROTOCOL',
           'ProtocolError', 'RANGE', 'STATE', 'SYNTAX', 'UINT32_MAX', 'UINT64_MAX', 'WirePlan',
           'channel', 'level_of', 'lines_to_plan', 'log_lines', 'message', 'parse_caps', 'parse_idn',
           'parse_log', 'plan_to_lines', 'text']


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
