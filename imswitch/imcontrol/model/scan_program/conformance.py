"""Engine conformance: what an engine did, against what the program means
(docs/design/plans/scan-engines-and-expert-scan.md §2.4, §2.7, §5.0).

Two checks, kept apart:

1. **Execution.** ``expected_log(schedule)`` turns the interpreter's realized
   schedule into the records an engine logs -- every DAC set-point, every
   line's on-interval, per segment. An engine's log must match it. Segments
   restart at each barrier, so this checks what the engine does after a
   release, not whether the release was right.
2. **Trigger handling.** ``trigger_problems`` drives a wait with arrival
   times chosen by the test, not by the engine, and checks the engine's
   releases as outputs: none before its arrival, each within the declared
   latency, none missing, timeouts as ``on_timeout`` says. Replaying an
   engine's own releases could not catch an engine that releases early or
   ignores its trigger.

Qt-free.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .schedule import RealizedSchedule


def _close(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def expected_log(schedule: RealizedSchedule) -> Dict[str, list]:
    """``{'dac': [(segment, tick, device, µm)], 'on': [(segment, start, end,
    device)]}``: what the program means, as an engine logs it."""
    dac = []
    held = {}

    def write(segment, tick, device, value):
        if device not in held or not _close(held[device], value):
            held[device] = value
            dac.append((segment, tick, device, value))

    first = True
    for realized in schedule.spans:
        span = realized.span
        if first and span.pieces:
            # The first positions are set when the run starts, and held
            # through any wait before the first step.
            for name, piece in span.pieces.items():
                write(0, 0, name, piece.start_um)
            first = False
        for name, piece in span.pieces.items():
            if piece.where == 'end':
                tick = (realized.start_tick + realized.ticks - realized.move_ticks[name]
                        - realized.settle_ticks[name])
                write(realized.segment, tick, name, piece.end_um)
            elif piece.where in ('whole', 'start'):
                write(realized.segment, realized.start_tick, name, piece.end_um)
    on = sorted((e.segment, e.start_tick, e.end_tick, e.device) for e in schedule.events)
    return {'dac': dac, 'on': on}


def engine_log(log) -> Dict[str, list]:
    """The same records from a step engine's ``RunLog``."""
    dac = [(segment, tick, device, value) for _, segment, tick, device, value in log.of('dac')]
    starts = {}
    on = []
    for kind, segment, tick, device in [e for e in log.entries if e[0] in ('on', 'off')]:
        if kind == 'on':
            starts.setdefault((segment, device), []).append(tick)
        else:
            start = starts[(segment, device)].pop(0)
            on.append((segment, start, tick, device))
    return {'dac': dac, 'on': sorted(on)}


def log_differences(engine: Dict[str, list], expected: Dict[str, list],
                    resolution_um: Dict[str, float] = None) -> List[str]:
    """Every way the engine's log differs from the program's meaning.
    ``resolution_um``: a device's smallest step on an engine that sets the
    nearest of its DAC's codes; its positions then match within half of it."""
    problems = []
    resolution_um = resolution_um or {}
    if len(engine['dac']) != len(expected['dac']):
        problems.append(f'{len(engine["dac"])} DAC writes, expected {len(expected["dac"])}')
    for got, want in zip(engine['dac'], expected['dac']):
        within = (abs(got[3] - want[3]) <= resolution_um[want[2]] / 2
                  if want[2] in resolution_um else _close(got[3], want[3]))
        if got[:3] != want[:3] or not within:
            problems.append(f'DAC write {got}, expected {want}')
            break
    if engine['on'] != expected['on']:
        missing = sorted(set(expected['on']) - set(engine['on']))[:3]
        extra = sorted(set(engine['on']) - set(expected['on']))[:3]
        problems.append(f'line intervals differ: missing {missing}, unexpected {extra}')
    return problems


def trigger_problems(releases: List[Tuple[int, int, object]], arrivals: List[int],
                     wait_starts: List[int], latency_ticks: int, timeout_ticks=None,
                     on_timeout='fail') -> List[str]:
    """Check releases ``(segment, release_wall_tick, arrival_or_None)``
    against arrival times the test chose. ``wait_starts`` are the wall ticks
    each wait began (from the program: its schedule before the barrier)."""
    problems = []
    # Accept a board's log entries as logged: ('release', segment, wall, arrival).
    releases = [tuple(entry[-3:]) for entry in releases]
    pending = sorted(arrivals)
    for index, start in enumerate(wait_starts):
        due = next((a for a in pending if a >= start), None)
        if due is not None and timeout_ticks is not None and due > start + timeout_ticks:
            due = None
        if index >= len(releases):
            problems.append(f'wait {index}: no release (trigger at {due} ignored?)')
            continue
        _, release, arrival = releases[index]
        if due is None:
            if on_timeout == 'continue' and timeout_ticks is not None:
                if release != start + timeout_ticks:
                    problems.append(f'wait {index}: timed out at {release}, expected '
                                    f'{start + timeout_ticks}')
            else:
                problems.append(f'wait {index}: released at {release} without a trigger')
            continue
        if release < due:
            problems.append(f'wait {index}: released at {release}, before its trigger at {due}')
        elif release > due + latency_ticks:
            problems.append(f'wait {index}: released at {release}, later than {due} + the '
                            f'declared {latency_ticks}-tick latency')
        pending.remove(due)
    if len(releases) > len(wait_starts):
        problems.append(f'{len(releases) - len(wait_starts)} release(s) with no wait')
    return problems


__all__ = ['engine_log', 'expected_log', 'log_differences', 'trigger_problems']


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
