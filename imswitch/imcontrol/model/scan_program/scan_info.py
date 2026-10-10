"""What a realized program tells the detectors (docs/design/plans/
scan-engines-and-expert-scan.md §2.5, M3).

Point detectors (APD, PMT) read a streamed scan line by line through the
``ScanInfoContract``: samples per step, per line with its return, per frame.
A program whose realized schedule is a *regular raster* -- every step the
same length, a return of the same length after every pass of the innermost
loop, nothing else (no waits, no preparation), the phase loop, if any, right
outside the innermost -- is exactly that, and its contract is derived here.

Any other program still gets a contract describing the run (its dimensions,
its length), so the NI-DAQ can play it and cameras count their frames; the
reason it is not a raster comes with it, and a point detector is refused
with that reason.

Qt-free.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..signaldesigners.basesignaldesigners import ScanInfoContract

#: Image-axis labels for the spatial loop kinds; any other kind (cycle,
#: plane, time, ...) is labelled by its kind.
_AXIS_LABELS = {'scan_x': 'x', 'scan_y': 'y', 'scan_z': 'z'}

#: The program's role in a ``scanInfoDict``: whether the point detectors can
#: assemble it, and why not.
RASTER_KEY = 'program_raster'
RASTER_REASON_KEY = 'program_raster_refusal'


def _drive_step(program, loop) -> Optional[Tuple[str, float]]:
    """``(device, µm per count)`` when exactly one drive weights ``loop``."""
    drives = [(d.device, float(d.weights_um[loop.id])) for d in program.drives
              if loop.id in d.weights_um]
    return drives[0] if len(drives) == 1 else None


def _visit_orders(program, image_loops) -> Tuple[List[object], str]:
    """``axis_visit_order`` entries per image axis, or the reason a point
    detector cannot place this program's visits."""
    orders = []
    ids = [loop.id for loop in program.loops]
    tables = {d.table_loop: d for d in program.drives if d.table_loop is not None}
    for axis, loop in enumerate(image_loops):
        if loop.id in tables:
            values = list(tables[loop.id].table_um)
            ranks = sorted(range(len(values)), key=lambda k: values[k])
            image_index = [0] * len(values)
            for rank, visit in enumerate(ranks):
                image_index[visit] = rank
            orders.append({'interleave': image_index})
        elif loop.traversal == 'serpentine':
            position = ids.index(loop.id)
            if axis == 0:
                outside = [lp.id for lp in program.loops[:position]
                           if lp is image_loops[1] or lp.kind == 'condition'] \
                    if len(image_loops) > 1 else []
                if tuple(loop.parity_loops) != tuple(outside):
                    return [], (f'{loop.id} runs back with {", ".join(loop.parity_loops)}; '
                                'a point detector places a serpentine on every other line '
                                'only.')
                orders.append({'serpentine': 'line'})
            elif axis == 1 and len(image_loops) > 2 and \
                    tuple(loop.parity_loops) == (image_loops[2].id,):
                orders.append({'serpentine': 'slice'})
            else:
                return [], (f'{loop.id} is serpentine at a level a point detector cannot '
                            'place.')
        elif loop.traversal == 'reverse':
            return [], f'{loop.id} runs in reverse, which a point detector cannot place yet.'
        else:
            orders.append(None)
    return orders, ''


def _raster_refusal(program, schedule) -> str:
    kinds = {realized.span.kind for realized in schedule.spans if realized.ticks}
    other = sorted(kinds - {'step', 'return'})
    if other:
        return (f'The scan has {", ".join(other)} intervals between its steps; a point '
                'detector reads a regular raster only.')
    steps = schedule.step_spans()
    if len({realized.ticks for realized in steps}) > 1:
        return 'Its steps are not all the same length on the sample clock.'
    phase = program.phase_loop
    if phase is not None and (len(program.loops) < 2 or program.loops[-2] is not phase):
        return (f'The phase loop {phase.id} is not right outside the innermost loop, where '
                'a point detector repeats lines.')
    if any(loop.mode != 'stepped' for loop in program.loops):
        return 'A swept loop is not compiled here.'
    return ''


def scan_info_for(program, schedule, rate_hz) -> Dict[str, object]:
    """The ``scanInfoDict`` for a realized program on a streaming engine at
    ``rate_hz``. ``scanInfoDict[RASTER_KEY]`` says whether point detectors
    can assemble it, ``scanInfoDict[RASTER_REASON_KEY]`` why not."""
    phase = program.phase_loop
    linesteps = phase.count if phase is not None else 1
    image_loops = [loop for loop in reversed(program.loops) if loop.kind != 'condition']
    dims = [int(loop.count) for loop in image_loops]
    steps = schedule.step_spans()
    step_ticks = steps[0].ticks if steps else 0
    returns = [r.ticks for r in schedule.spans if r.span.kind == 'return' and r.ticks]
    pass_ticks = dims[0] * step_ticks + (returns[0] if returns else 0)
    total = sum(segment.ticks for segment in schedule.segments)

    levels = [step_ticks, dims[0] * step_ticks]
    frame = pass_ticks * linesteps * (dims[1] if len(dims) > 1 else 1)
    if len(dims) > 1:
        levels.append(frame)
    for count in dims[2:]:
        levels.append(levels[-1] * count)
    levels = levels[:len(dims) + 1]

    sizes, labels = [], []
    for loop in image_loops:
        step = _drive_step(program, loop)
        # Index units for an axis no single drive steps (time, a composite).
        sizes.append(abs(step[1]) if step else 1.0)
        labels.append(_AXIS_LABELS.get(loop.kind, loop.kind))

    refusal = _raster_refusal(program, schedule)
    orders, order_refusal = _visit_orders(program, image_loops)
    refusal = refusal or order_refusal
    contract = ScanInfoContract(
        img_dims=dims, img_axes_phys=labels, pixel_sizes=sizes,
        scan_samples=[max(1, int(v)) for v in levels], scan_samples_total=int(total),
        scan_samples_d2_period=max(1, int(pass_ticks)), n_pixels_fast=dims[0],
        samples_per_pixel=max(1, int(step_ticks)), dwell_time=float(program.step_s),
        scan_time_step=1.0 / float(rate_hz), n_linesteps=linesteps,
        positions=list(dims),
        return_time=(returns[0] / float(rate_hz)) if returns else 0.0,
    )
    info = contract.to_dict()
    if any(order is not None for order in orders):
        info['axis_visit_order'] = orders
    info[RASTER_KEY] = not refusal
    info[RASTER_REASON_KEY] = refusal
    info['timing_policy'] = schedule.policy.id
    return info


__all__ = ['RASTER_KEY', 'RASTER_REASON_KEY', 'scan_info_for']


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
