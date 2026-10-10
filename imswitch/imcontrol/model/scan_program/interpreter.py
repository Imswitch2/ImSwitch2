"""The reference interpreter: what a scan program does, in order
(docs/design/plans/scan-engines-and-expert-scan.md §2.4, §2.7).

``expand(program, context)`` turns a program into a sequence of spans, in
seconds as requested:

- ``step``: one innermost step, every drive holding (or making its in-step
  transition at the end), with the step's timeline events;
- ``return``: the interval at the end of every innermost pass, in which the
  innermost drives return and every outer drive that changes moves;
- ``move``: motion between two steps for drives placed ``between-steps``;
- ``wait``: a barrier, unresolved until its trigger's release (or a fixed
  interval);
- ``preparation``: the scheduled stage after a barrier, in which only the
  devices that need it get ready.

It is the meaning of the model. Realizing the spans on an engine's grid under
a timing policy is ``schedule.py``; turning them into samples is
``densify.py``. An engine never executes by calling it (plan §2.7).

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from .program import DeviceDrive, ScanProgram, TimelineEvent, Wait, validate


class InterpretError(ValueError):
    """The program cannot be interpreted in this context (for example, a
    position it is relative to was not captured)."""


@dataclass(frozen=True)
class DeviceFacts:
    """What the setup says about a device that the program does not: its
    preparation after a barrier (§2.4), with where that came from."""
    preparation: object = 'notRequired'    # 'notRequired', 'unknown' or seconds
    source: str = ''


@dataclass(frozen=True)
class RunContext:
    """The run a program is interpreted for: positions captured before the
    scan (µm) and device facts from the setup."""
    captured_um: Mapping[str, float] = field(default_factory=dict)
    devices: Mapping[str, DeviceFacts] = field(default_factory=dict)


@dataclass
class Piece:
    """What one drive does during one span, in µm.

    ``where``: 'hold' (no change), 'end' (an in-step transition at the end of
    a step: ``move_s`` then ``settle_s``), 'whole' (a transition over the
    whole span, as a return), 'start' (a preparation: move, then settle,
    ready at the stage's end).
    """
    where: str
    start_um: float
    end_um: float
    shape: str = 'smooth'
    move_s: float = 0.0
    settle_s: float = 0.0


@dataclass
class EventInstance:
    """One occurrence of a timeline event, relative to its span: from the
    span's start, or -- ``from_end`` -- from its end (times <= 0)."""
    event: TimelineEvent
    start_s: float
    end_s: float
    from_end: bool = False


@dataclass
class Span:
    kind: str
    duration_s: Optional[float]
    pieces: Dict[str, Piece] = field(default_factory=dict)
    events: List[EventInstance] = field(default_factory=list)
    counters: Optional[Dict[str, int]] = None
    step_index: Optional[int] = None
    phase: int = 0
    wait: Optional[Wait] = None


@dataclass
class Expansion:
    program: ScanProgram
    spans: List[Span]
    visits: List[Dict[str, int]]
    first_um: Dict[str, float]
    restore_um: Dict[str, float]


def _iterations(loop, outer_ordinals):
    """A loop's counter values in traversal order, given the ordinals of the
    loops outside it (for a serpentine's parity)."""
    values = list(range(int(loop.count)))
    if loop.traversal == 'reverse':
        return values[::-1]
    if loop.traversal == 'serpentine':
        parity = sum(outer_ordinals[name] for name in loop.parity_loops) % 2
        return values[::-1] if parity else values
    return values


def visits(program: ScanProgram) -> List[Dict[str, int]]:
    """Every step's loop counters, in the order the scan visits them."""
    out = []

    def walk(depth, counters, ordinals):
        if depth == len(program.loops):
            out.append(dict(counters))
            return
        loop = program.loops[depth]
        for ordinal, value in enumerate(_iterations(loop, ordinals)):
            counters[loop.id] = value
            ordinals[loop.id] = ordinal
            walk(depth + 1, counters, ordinals)
        counters.pop(loop.id, None)
        ordinals.pop(loop.id, None)

    walk(0, {}, {})
    return out


def position_um(drive: DeviceDrive, counters, context: RunContext) -> float:
    """A drive's position at a step, in µm."""
    value = float(drive.offset_um)
    for loop_id, weight in drive.weights_um.items():
        value += float(weight) * counters[loop_id]
    if drive.table_loop is not None:
        value += float(drive.table_um[counters[drive.table_loop]])
    if drive.motion.origin == 'captured':
        if drive.device not in context.captured_um:
            raise InterpretError(
                f'{drive.device} is positioned relative to where it was before the scan, '
                'but that position was not captured.'
            )
        value += float(context.captured_um[drive.device])
    return value


def _loop_ids_outside(program, loop_id):
    ids = [loop.id for loop in program.loops]
    return ids[:ids.index(loop_id)]


def _starts_pass(program, visit_list, index, loop_id):
    if index == 0:
        return True
    outer = _loop_ids_outside(program, loop_id)
    return any(visit_list[index][o] != visit_list[index - 1][o] for o in outer)


def _ends_pass(program, visit_list, index, loop_id):
    if index == len(visit_list) - 1:
        return True
    outer = _loop_ids_outside(program, loop_id)
    return any(visit_list[index][o] != visit_list[index + 1][o] for o in outer)


def _moved_by(drive: DeviceDrive, loop_id) -> bool:
    return (float(drive.weights_um.get(loop_id, 0.0)) != 0.0
            or drive.table_loop == loop_id)


def _event_applies(event, phase):
    return event.phases is None or phase in event.phases


def _restore_um(drive, first, context, program):
    restore = drive.motion.restore
    if restore == 'captured':
        if drive.device not in context.captured_um:
            raise InterpretError(
                f'{drive.device} is restored to where it was before the scan, '
                'but that position was not captured.'
            )
        return float(context.captured_um[drive.device])
    if restore == 'first':
        return first
    if restore == 'park':
        if drive.device not in program.park:
            raise InterpretError(f'{drive.device} is restored to its park position, '
                                 'but the program parks it nowhere.')
        return float(program.park[drive.device])
    return None     # 'hold'


def expand(program: ScanProgram, context: Optional[RunContext] = None) -> Expansion:
    """The program as spans, in seconds as requested."""
    validate(program)
    context = context or RunContext()
    visit_list = visits(program)
    drives = list(program.drives)
    innermost = program.innermost.id
    phase_loop = program.phase_loop

    positions = [{d.device: position_um(d, v, context) for d in drives} for v in visit_list]
    first = dict(positions[0])
    restore = {}
    for drive in drives:
        target = _restore_um(drive, first[drive.device], context, program)
        restore[drive.device] = positions[-1][drive.device] if target is None else target

    inner_drives = [d for d in drives if _moved_by(d, innermost)]
    return_s = max((float(d.motion.return_s) for d in inner_drives), default=0.0)

    spans: List[Span] = []
    waits_at = {}
    for wait in program.waits:
        waits_at.setdefault(wait.at, []).append(wait)

    for index, counters in enumerate(visit_list):
        phase = counters.get(phase_loop.id, 0) if phase_loop else 0
        here = positions[index]

        # Barriers before this step: the run start, every iteration of a
        # loop that starts here, every step.
        for wait in _waits_before(program, waits_at, visit_list, index):
            spans.append(Span('wait', wait.interval_s if wait.trigger == 'time' else None,
                              wait=wait, phase=phase))
            if wait.trigger != 'time':
                spans.append(_preparation(program, drives, spans, here, context, phase))

        step = Span('step', float(program.step_s), counters=dict(counters),
                    step_index=index, phase=phase)
        last = index == len(visit_list) - 1
        pass_end = _ends_pass(program, visit_list, index, innermost)
        nxt = None if last else positions[index + 1]
        between = {}
        for drive in drives:
            name = drive.device
            motion = drive.motion
            if (not pass_end and nxt is not None and nxt[name] != here[name]
                    and motion.placement == 'in-step'):
                step.pieces[name] = Piece('end', here[name], nxt[name], motion.transition,
                                          float(motion.move_s), float(motion.settle_s))
            else:
                step.pieces[name] = Piece('hold', here[name], here[name])
                if (not pass_end and nxt is not None and nxt[name] != here[name]):
                    between[name] = drive
        for event in program.timeline:
            if not _event_applies(event, phase):
                continue
            if event.anchor == 'step':
                step.events.append(EventInstance(event, float(event.start_s), float(event.end_s)))
            elif event.anchor.startswith('pass-start:'):
                if _starts_pass(program, visit_list, index, event.anchor.split(':', 1)[1]):
                    step.events.append(EventInstance(event, float(event.start_s),
                                                     float(event.end_s)))
            elif event.anchor.startswith('pass-end:'):
                if _ends_pass(program, visit_list, index, event.anchor.split(':', 1)[1]):
                    step.events.append(EventInstance(event, float(event.start_s),
                                                     float(event.end_s), from_end=True))
        spans.append(step)

        if between:
            move = Span('move', max(float(d.motion.move_s) + float(d.motion.settle_s)
                                    for d in between.values()), phase=phase)
            for drive in drives:
                name = drive.device
                if name in between:
                    move.pieces[name] = Piece('start', here[name], nxt[name],
                                              drive.motion.transition,
                                              float(drive.motion.move_s),
                                              float(drive.motion.settle_s))
                else:
                    move.pieces[name] = Piece('hold', nxt[name], nxt[name])
            spans.append(move)

        if pass_end:
            target = restore if last else nxt
            ret = Span('return', return_s, phase=phase)
            for drive in drives:
                name = drive.device
                if target[name] != here[name] or name in [d.device for d in inner_drives]:
                    ret.pieces[name] = Piece('whole', here[name], target[name],
                                             drive.motion.transition)
                else:
                    ret.pieces[name] = Piece('hold', here[name], here[name])
            spans.append(ret)

    return Expansion(program, spans, visit_list, first, restore)


def _waits_before(program, waits_at, visit_list, index):
    out = []
    if index == 0:
        out.extend(waits_at.get('start', ()))
    for loop in program.loops:
        if loop.id in waits_at and _starts_iteration(program, visit_list, index, loop.id):
            out.extend(waits_at[loop.id])
    out.extend(waits_at.get('step', ()))
    return out


def _starts_iteration(program, visit_list, index, loop_id):
    """Whether this step is the first of an iteration of ``loop_id``."""
    if index == 0:
        return True
    ids = [loop.id for loop in program.loops]
    upto = ids[:ids.index(loop_id) + 1]
    return any(visit_list[index][o] != visit_list[index - 1][o] for o in upto)


def _preparation(program, drives, spans, here, context, phase):
    """The scheduled stage after a barrier (§2.4). Positioners whose
    segment-initial position differs from where they were held move and
    settle; devices with a declared preparation get it. Nothing else changes,
    and no timed event is placed in it."""
    held = {}
    for span in reversed(spans):
        if span.pieces:
            held = {name: piece.end_um for name, piece in span.pieces.items()}
            break
    stage = Span('preparation', 0.0, phase=phase)
    longest = 0.0
    for drive in drives:
        name = drive.device
        before = held.get(name, here[name])
        if before != here[name]:
            need = float(drive.motion.move_s) + float(drive.motion.settle_s)
            stage.pieces[name] = Piece('start', before, here[name], drive.motion.transition,
                                       float(drive.motion.move_s), float(drive.motion.settle_s))
            longest = max(longest, need)
        else:
            stage.pieces[name] = Piece('hold', here[name], here[name])
    used = {event.device for event in program.timeline}
    for name, facts in context.devices.items():
        if name not in used:
            continue
        if facts.preparation == 'unknown':
            raise InterpretError(
                f'{name} needs preparation after a wait, but its preparation time is '
                f'unknown ({facts.source or "not declared"}).'
            )
        if facts.preparation not in ('notRequired', None):
            longest = max(longest, float(facts.preparation))
    stage.duration_s = longest
    return stage


__all__ = [
    'DeviceFacts', 'EventInstance', 'Expansion', 'InterpretError', 'Piece',
    'RunContext', 'Span', 'expand', 'position_um', 'visits',
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
