"""Point detectors read every line of an Expert raster, whatever its fast
axis (review 2026-10-17, P1).

The APD and PMT workers expanded the line steps on the axis NAMED y. The
designers name their axes by position (x, y, z), an Expert program names
them physically, so a fast-Y raster ('y', 'x') expanded its fast axis:
three lines of four pixels were read instead of six, half the samples were
never consumed, and the worker reported completion. Every program here is
compiled by the NI-DAQ engine and read by the real workers from a stream in
which each sample carries the ordinal of the line it belongs to, so a line
skipped, read twice or placed on the wrong position shows.
"""
import itertools

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors.APDManager import ScanWorker as APDScanWorker
from imswitch.imcontrol.model.managers.detectors.PMTManager import ScanWorker as PMTScanWorker
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.scan_program import DeviceDrive, Loop, Motion, ScanProgram
from imswitch.imcontrol.model.scan_program.engine import setup_channels

from .test_frame_publication import _pmt
from .test_point_detector_single_axis import _apd
from .test_scan_one_schedule import _expert_setup

MOVE = Motion(transition='jump', move_s=1e-4, restore='hold')
AXES = {'x': ('X', 'scan_x'), 'y': ('Y', 'scan_y'), 'z': ('Z', 'scan_z')}


def _program(order, counts, phases):
    """Loops outer to inner along ``order``; ``phases`` conditions right
    outside the innermost loop, where a point detector repeats lines."""
    loops = [Loop(axis, AXES[axis][1], n) for axis, n in zip(order[:-1], counts[:-1])]
    if phases > 1:
        loops.append(Loop('phase', 'condition', phases))
    loops.append(Loop(order[-1], AXES[order[-1]][1], counts[-1]))
    drives = tuple(DeviceDrive(AXES[axis][0], weights_um={axis: 0.01}, motion=MOVE)
                   for axis in order)
    return ScanProgram(loops=tuple(loops), drives=drives, step_s=1e-3)


def _scan_info(program):
    setup = _expert_setup()
    compiled = NidaqScanEngine(None, nidaqManager=None, setupInfo=setup).compile(
        program, setup_channels(setup), captured_um={'X': 0.0, 'Y': 0.0, 'Z': 0.0})
    info = compiled.scanInfoDict
    assert info['program_raster'] is True, info['program_raster_reason']
    return info


def _read(kind, info):
    """Run ``kind``'s worker over the scan; the lines it emitted, in order."""
    detector = _apd() if kind == 'APD' else _pmt()
    worker = (APDScanWorker if kind == 'APD' else PMTScanWorker)(
        detector, info, {'TTLCycleSignalsDict': {}})
    assert worker._throw_init_smooth == 0       # nothing else before the first line
    first = worker._phase_delay + worker._throw_startzero
    period = worker._samples_d2_period
    fetched = {'n': 0, 'last': 0}

    def fetch(count, _kind):
        index = np.arange(fetched['n'], fetched['n'] + int(count))
        fetched['n'] += int(count)
        ordinal = np.where(index < first, 0, (index - first) // period + 1)
        if kind == 'PMT':
            return ordinal.astype(float)
        counts = np.cumsum(ordinal, dtype=np.int64) + fetched['last']   # a counter
        if counts.size:
            fetched['last'] = int(counts[-1])
        return counts
    worker._fetchSamples = fetch
    if kind == 'APD':
        detector._mock_photon_count_max = 10 ** 12      # the mock's clip
    lines = []
    worker.d2Step.connect(lambda pixels, pos, _gen: lines.append(
        (np.asarray(pixels, dtype=float).copy(), tuple(int(p) for p in pos))))
    worker.scanning = True
    worker.run()
    return detector, worker, lines, first, period


#: (outer to inner axes, their counts, conditions): fast X, Y and Z, a
#: singleton axis, and reordered outer axes.
CASES = {
    'fast x': ('yx', (3, 4), 2),
    'fast y': ('xy', (3, 4), 2),
    'fast z': ('yz', (3, 4), 3),
    'fast y, singleton x': ('xy', (1, 4), 2),
    'fast y, outer z then x': ('zxy', (2, 3, 4), 2),
    'fast x, no conditions': ('zyx', (2, 3, 4), 1),
}


@pytest.mark.parametrize('kind', ['APD', 'PMT'])
@pytest.mark.parametrize('case', list(CASES))
def test_every_line_is_read_once_in_order_at_its_place(kind, case):
    order, counts, phases = CASES[case]
    info = _scan_info(_program(order, counts, phases))
    fast, outer = counts[-1], counts[:-1][::-1]           # img_dims order: fast first
    assert info['img_dims'] == [fast, *outer]
    assert info['n_linesteps'] == phases
    _, worker, lines, first, period = _read(kind, info)

    n_lines = int(np.prod(outer)) * phases
    assert len(lines) == n_lines
    # one line per (outer..., line axis x line steps) index, in acquisition order
    expected = list(itertools.product(*(range(d) for d in reversed(worker._loop_dims[1:]))))
    assert [pos for _, pos in lines] == expected
    spp = int(worker._frac_det_dwell)
    for k, (pixels, _) in enumerate(lines):
        assert len(pixels) == fast
        assert np.all(pixels == (k + 1) * (spp if kind == 'APD' else 1)), (k, pixels)
    # every line's samples were consumed, none past the stream
    assert worker._samples_read >= first + (n_lines - 1) * period + fast * spp
    assert worker._samples_read <= worker._samples_total


@pytest.mark.parametrize('case', ['fast x', 'fast y'])
def test_a_line_lands_where_its_line_step_and_line_are(case):
    """The manager splits the expanded line index into (line, line step):
    each pixel holds the ordinal of the line acquired there."""
    order, counts, phases = CASES[case]
    info = _scan_info(_program(order, counts, phases))
    detector, worker, lines, _, _ = _read('APD', info)
    detector._linestep = worker._linestep         # as the manager takes it at the start
    for pixels, pos in lines:
        detector.updateImage(pixels, pos)
    spp = int(worker._frac_det_dwell)
    image = np.asarray(detector._image).reshape(phases, counts[0], counts[1])
    for line in range(counts[0]):
        for step in range(phases):
            assert np.all(image[step, line] == (line * phases + step + 1) * spp)


def test_the_time_tagger_takes_its_lines_by_position():
    """The same rule where the FLIM image is built from line-clock edges."""
    from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
        SwabianTimeTaggerManager,
    )
    infer = SwabianTimeTaggerManager._infer_dims_from_scanInfo
    fast_y = _scan_info(_program('zxy', (2, 3, 4), 2))
    assert fast_y['img_axes_phys'] == ['y', 'x', 'z']
    assert infer(None, fast_y) == (4, 3, 2, ['z'], [2])
    designer = {'img_dims': [4, 3, 2], 'img_axes_phys': ['x', 'y', 'z'], 'n_linesteps': 2}
    assert infer(None, designer) == (4, 3, 2, ['z'], [2])     # as before
    assert infer(None, {'img_dims': [5], 'img_axes_phys': ['x']}) == (5, 1, 1, [], [])
