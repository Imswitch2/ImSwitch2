"""Detector bindings (docs/design/plans/scan-engines-and-expert-scan.md §2.5,
§7.10): which events a detector takes, how many per step, in which phases,
over which window, and how it assembles them.

A binding is what the recording expects of a detector; its line is what the
detector gets. A camera takes a frame on every rising edge of its line, so a
binding is checked against that line, step by step, before anything runs:
a binding that expects frames the line does not fire, or a line that fires
where the binding expects none, refuses the program with the step and its
phase. The layout and the recording's frame counts then come from the
binding -- the program and the check cannot disagree. A detector without a
binding is recorded as its line fires.

What is built: a camera (a frame stream) takes ``event`` windows assembled
as a ``stream``; a point detector (APD, PMT) assembles an ``image`` with one
value per step, integrating every sample of the step (``step``), in every
phase. Anything else is refused with the reason, not approximated.

Qt-free.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .program import DetectorBinding


def _schedules(schedules) -> list:
    return list(schedules) if isinstance(schedules, (list, tuple)) else [schedules]


def binding_of(program, detector: str) -> Optional[DetectorBinding]:
    return next((b for b in program.detectors if b.detector == detector), None)


def wire_frames(schedules, detector: str) -> List[int]:
    """The frames ``detector`` takes in each step, in the order the steps run:
    the rising edges of its line within the step (touching windows are one
    pulse on the wire). ``schedules``: the run's schedule, or the leader's
    and each follower's -- the detector's line is on one of them."""
    candidates = _schedules(schedules)
    schedule = next((s for s in candidates
                     if any(e.device == detector for e in s.events)), candidates[0])
    windows: Dict[int, list] = {}
    for event in schedule.events:
        if event.device == detector and event.step_index is not None:
            windows.setdefault(event.step_index, []).append((event.start_tick, event.end_tick))
    counts = []
    for realized in schedule.step_spans():
        edges, end = 0, None
        for start, stop in sorted(windows.get(realized.span.step_index, ())):
            if end is None or start > end:
                edges += 1
            end = stop if end is None else max(end, stop)
        counts.append(edges)
    return counts


def bound_frames(schedules, binding: DetectorBinding) -> List[int]:
    """The frames ``binding`` takes in each step: ``per_step`` in the steps of
    its phases, none elsewhere."""
    steps = _schedules(schedules)[0].step_spans()
    return [int(binding.per_step) if binding.phases is None or s.span.phase in binding.phases
            else 0 for s in steps]


def frames_per_step(program, schedules, detector: str) -> List[int]:
    """What the recording expects of ``detector`` in each step: its binding's
    frames, or -- without one -- its line's."""
    binding = binding_of(program, detector)
    if binding is not None:
        return bound_frames(schedules, binding)
    return wire_frames(schedules, detector)


def _phases_text(program, binding) -> str:
    if binding.phases is None or program.phase_loop is None:
        return ''
    return f' in phase{"s" if len(binding.phases) > 1 else ""} ' \
           f'{", ".join(str(p) for p in sorted(binding.phases))}'


def binding_problems(program, schedules, *, point_detectors: Sequence[str] = (),
                     known_detectors: Optional[Sequence[str]] = None) -> List[str]:
    """Why the program's bindings cannot be recorded as they say, or ``[]``."""
    problems = []
    seen = set()
    point = set(point_detectors)
    phases = set(range(program.phase_count))
    for binding in program.detectors:
        name = binding.detector
        where = f"{name}'s binding"
        if name in seen:
            problems.append(f'{name} has two bindings; one says what it takes.')
            continue
        seen.add(name)
        if known_detectors is not None and name not in known_detectors:
            problems.append(f'{where}: this setup has no detector {name}.')
            continue
        if program.trajectory is not None:
            problems.append(f'{where}: on a trajectory the point detectors measure window by '
                            'window and cameras take no frames, so a binding is not read; '
                            'remove it.')
            continue
        if name in point:
            if binding.assembly != 'image':
                problems.append(f"{where}: a point detector assembles an image (assembly "
                                "'image'); a stream of its values is not built.")
            if binding.window != 'step':
                problems.append(f"{where}: a point detector integrates every sample of each "
                                "step (window 'step'); integrating only during an event is "
                                "not built.")
            if int(binding.per_step) != 1 or (binding.phases is not None
                                              and set(binding.phases) != phases):
                problems.append(f'{where}: a point detector takes one value per step, in every '
                                'phase; other counts or phases are not built.')
            continue
        if binding.assembly != 'stream':
            problems.append(f"{where}: a camera takes a stream of frames (assembly 'stream'); "
                            "an image assembled from windows is a point detector's.")
        if binding.window != 'event':
            problems.append(f"{where}: a camera takes a frame per pulse on its line (window "
                            "'event'); a window of a whole step is a point detector's.")
        by_id = {e.device for e in program.timeline if e.id == binding.events}
        devices = by_id or {binding.events}
        others = sorted(devices - {name})
        if others:
            problems.append(f'{where}: it takes the events {binding.events!r} of '
                            f'{", ".join(others)}, but a camera takes frames from its own line: '
                            f'bind events on {name}.')
            continue
        expected = bound_frames(schedules, binding)
        fired = wire_frames(schedules, name)
        steps = _schedules(schedules)[0].step_spans()
        for index, (want, got) in enumerate(zip(expected, fired)):
            if want == got:
                continue
            phase = steps[index].span.phase
            at = f'step {index + 1} of {len(steps)}' + (
                f' (phase {phase})' if program.phase_loop is not None else '')
            if want:
                problems.append(f'{where} takes {want} frame{"s" if want > 1 else ""} per step'
                                f'{_phases_text(program, binding)}, but its line fires {got} '
                                f'in {at}: a camera takes a frame on every rising edge of its '
                                'line.')
            else:
                problems.append(f'{where} leaves out phase {phase}, but its line fires {got} in '
                                f'{at}: the camera would take frames the recording does not '
                                'expect.')
            break
    return problems


def binding_lines(program, schedules, detectors: Sequence[str], *,
                  point_detectors: Sequence[str] = ()) -> List[str]:
    """What the report says each taking-part detector records."""
    lines = []
    point = set(point_detectors)
    for name in detectors:
        binding = binding_of(program, name)
        if name in point:
            if binding is not None:
                lines.append(f'{name}: one image pixel per step, integrating the whole step.')
            continue
        fired = wire_frames(schedules, name)
        if not any(fired):
            continue
        if binding is not None:
            lines.append(f'{name}: {binding.per_step} frame{"s" if binding.per_step > 1 else ""}'
                         f' per step{_phases_text(program, binding)}; its trigger line '
                         'agrees.')
        else:
            counts = sorted(set(fired))
            each = (f'{counts[0]} frame{"s" if counts[0] > 1 else ""} per step'
                    if len(counts) == 1 else f'{min(counts)} to {max(counts)} frames per step')
            lines.append(f'{name}: not in Recording; recorded as its trigger line fires '
                         f'({each}).')
    return lines


__all__ = ['binding_lines', 'binding_of', 'binding_problems', 'bound_frames',
           'frames_per_step', 'wire_frames']


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
