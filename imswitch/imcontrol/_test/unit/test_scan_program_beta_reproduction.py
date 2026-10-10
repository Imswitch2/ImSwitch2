"""M0 acceptance: the scan-program interpreter plays what today's designers
play (docs/design/plans/scan-engines-and-expert-scan.md §2.7, M0).

Every Beta golden is rebuilt from an Advanced-shaped program -- adapter,
interpreter, realization under ``advanced-compatible-v1``, densification --
and compared with the baseline captured before any change. So are the
Advanced TTL designer's goldens on the same scans, clocks included. The
compatibility realization reports every rounding it makes; ``strict-v1``
refuses the same request instead. Saving gives back the request unchanged.
"""

import copy

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts, rising_edge_count, validate_detector_edge_counts,
)

from imswitch.imcontrol.model.scan_program import (
    PolicyError, ScheduleRefused, expand, realize,
)
from imswitch.imcontrol.model.scan_program.advanced import (
    COMPATIBLE_POLICY, advanced_from_program, is_legacy_advanced_state, layout_inputs,
    program_from_advanced, timing_policy_for_state,
)
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner
from imswitch.imcontrol.model.scan_program.densify import densify
from . import test_beta_signal_goldens as goldens
from .test_galvo_signal_goldens import _assert_matches_golden

SETUP = goldens._setup()
RATE = SETUP.scan.sampleRate
CONVERSION = {name: info.managerProperties['conversionFactor']
              for name, info in SETUP.positioners.items()}


def _schedule(stage, digital=None, policy=None):
    program, context = program_from_advanced(stage, digital, SETUP)
    return realize(expand(program, context), RATE, policy)


def _stored(name):
    return np.load(goldens.GOLDEN_DIR / f'{name}.npz', allow_pickle=False)


@pytest.mark.parametrize('case', sorted(goldens.CASES))
def test_the_interpreter_plays_every_beta_golden(case):
    stored = _stored(case)
    analog = densify(_schedule(goldens.CASES[case]))['analog']
    assert sorted(analog) == sorted(stored['__devices__'].tolist())
    for device, signal in analog.items():
        _assert_matches_golden(f'{case}/{device}', signal / CONVERSION[device],
                               stored[f'sig::{device}'])


@pytest.mark.parametrize('case', sorted(goldens.TTL_CASES))
def test_the_interpreter_plays_every_advanced_ttl_golden(case):
    scan, digital = goldens.TTL_CASES[case]
    stage = goldens.CASES[scan]
    digital = dict(digital, sequence_time=stage['sequence_time'])
    stored = _stored(case)
    lines = densify(_schedule(stage, digital))['digital']
    assert sorted(lines) == sorted(stored['__devices__'].tolist())
    for device, line in lines.items():
        assert np.array_equal(line, stored[f'ttl::{device}']), device


@pytest.mark.parametrize('case', sorted(goldens.CASES))
def test_the_sample_structure_is_the_designers(case):
    """Per pixel, per line (with its return) and in total, the schedule has
    the samples the Beta designer's ScanInfoContract reports."""
    stored = _stored(case)
    schedule = _schedule(goldens.CASES[case])
    steps = schedule.step_spans()
    returns = [s for s in schedule.spans if s.span.kind == 'return']
    assert {s.ticks for s in steps} == {int(stored['info::samples_per_pixel'])}
    assert steps[0].ticks == int(stored['info::scan_samples'][0])
    assert sum(s.ticks for s in schedule.spans) == int(stored['info::scan_samples_total'])
    per_line = int(stored['info::n_pixels_fast']) * steps[0].ticks + returns[0].ticks
    assert per_line == int(stored['info::scan_samples_d2_period'])


def test_a_dwell_off_the_grid_runs_as_the_designer_rounds_it_and_says_so():
    """4.025 ms at 100 kHz is 402.5 samples; Beta plays 403 (rounded up), and
    so does the compatible schedule -- which reports every rounding and the
    run-wide timing error instead of hiding it."""
    schedule = _schedule(goldens.CASES['xy_fractional_samples'])
    steps = [c for c in schedule.changes if c.quantity == 'step']
    assert len(steps) == 8 and all(c.realized_s == pytest.approx(4.03e-3) for c in steps)
    assert all(c.requested_s == pytest.approx(4.025e-3) for c in steps)
    returns = [c for c in schedule.changes if c.quantity == 'return']
    assert len(returns) == 2 and returns[0].realized_s == pytest.approx(10.01e-3)
    # 8 pixels 5 µs longer, 2 returns 5 µs longer
    assert schedule.error.end_deviation_s == pytest.approx(10 * 5e-6)


def test_strict_timing_refuses_what_compatibility_rounds_with_its_neighbours():
    with pytest.raises(ScheduleRefused) as refused:
        _schedule(goldens.CASES['xy_fractional_samples'], policy='strict-v1')
    text = str(refused.value)
    assert '4.025 ms is not on the 10 µs grid' in text and '4.02 ms or 4.03 ms' in text


def test_a_whole_sample_scan_has_no_changes_under_either_policy():
    compatible = _schedule(goldens.CASES['xyz_centred'])
    strict = _schedule(goldens.CASES['xyz_centred'], policy='strict-v1')
    assert compatible.changes == [] and strict.changes == []
    assert np.array_equal(densify(compatible)['analog']['StageZ'],
                          densify(strict)['analog']['StageZ'])


def test_saving_gives_back_the_request_unchanged():
    """The program keeps the request; the realized dwell is never written
    back (plan §2.6)."""
    stage = goldens.CASES['xy_fractional_samples']
    digital = dict(goldens.TTL_CASES['ttl_whole_pixel'][1], sequence_time=stage['sequence_time'])
    program, _ = program_from_advanced(stage, digital, SETUP)
    again_stage, again_digital = advanced_from_program(program)
    assert again_stage == stage and again_digital == digital
    assert again_stage['sequence_time'] == 4.025e-3
    assert program.timing_policy == COMPATIBLE_POLICY


def test_the_schedule_records_its_policy_and_sources():
    summary = _schedule(goldens.CASES['xy_fractional_samples']).summary()
    assert summary['policy']['id'] == COMPATIBLE_POLICY
    assert summary['policy']['contents']['rounding']['step'] == 'ceil'
    assert summary['grid_s'] == pytest.approx(1e-5)
    assert any(s['quantity'] == 'timing grid' for s in summary['sources'])
    assert summary['changes'] and summary['error']['end_deviation_s'] > 0


# ---------------------------------------------------------------------------
# Layouts come from the program (plan §2.5)
# ---------------------------------------------------------------------------

def _controller_inputs(stage, digital, detectors):
    """What ScanControllerAdvanced hands build_advanced_scan_layouts today:
    the Beta designer's scanInfo, the line-step masks, and the pulses per
    condition counted on a single generated pixel with every condition on."""
    _, _, info = BetaScanDesigner().make_signal(stage, SETUP)
    conditions = max(1, int(digital['n_linesteps']))
    forced = copy.deepcopy(digital)
    for detector in detectors:
        forced['linestep_enable'][detector] = [True] * conditions
    pixel = AdvancedScanTTLCycleDesigner().make_single_pixel_signal(forced, SETUP)
    samples = max(1, int(round(float(stage['sequence_time']) * RATE)))
    counts = {d: tuple(rising_edge_count(pixel[d][c * samples:(c + 1) * samples])
                       for c in range(conditions)) for d in detectors}
    devices = {label: stage['target_device'][i]
               for i, label in enumerate(info['img_axes_phys'])}
    return info, digital['linestep_enable'], counts, devices


def _camera_off_in_phase_1():
    scan, digital = goldens.TTL_CASES['ttl_linesteps']
    digital = copy.deepcopy(digital)
    digital['linestep_enable']['Camera'] = [True, False, True]
    return scan, digital


LAYOUT_CASES = {name: goldens.TTL_CASES[name]
                for name in ('ttl_whole_pixel', 'ttl_windows', 'ttl_linesteps')}
LAYOUT_CASES['camera_off_in_phase_1'] = _camera_off_in_phase_1()


@pytest.mark.parametrize('case', sorted(LAYOUT_CASES))
def test_the_programs_layouts_are_the_controllers(case):
    scan, digital = LAYOUT_CASES[case]
    stage = goldens.CASES[scan]
    digital = dict(copy.deepcopy(digital), sequence_time=stage['sequence_time'])
    info, masks, counts, devices = _controller_inputs(stage, digital, ['Camera'])
    expected = build_advanced_scan_layouts(
        info, ['Camera', 'APD'], scan_source='Test', detector_masks=masks,
        pulse_counts_by_condition=counts, scan_driven_detectors=['APD'], devices=devices)

    schedule = _schedule(stage, digital)
    p_info, p_devices, p_masks, p_counts = layout_inputs(schedule, ['Camera'])
    got = build_advanced_scan_layouts(
        p_info, ['Camera', 'APD'], scan_source='Test', detector_masks=p_masks,
        pulse_counts_by_condition=p_counts, scan_driven_detectors=['APD'], devices=p_devices)
    assert got == expected

    # The frame-count check still runs, and gives the interpreter's TTL the
    # verdict it gives the designer's. Whole-pixel gating holds a camera's
    # line high through a line -- one trigger per line for one frame per
    # pixel -- and is refused on both.
    _, _, info = BetaScanDesigner().make_signal(stage, SETUP)
    designer, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, SETUP, info)

    def verdict(lines):
        try:
            validate_detector_edge_counts(got, lines)
        except ValueError as error:
            return str(error)
        return 'ok'

    assert verdict(densify(schedule)['digital']) == verdict(designer)
    assert (verdict(designer) == 'ok') == (case != 'ttl_whole_pixel')


def test_a_disabled_phase_does_not_size_the_repeat_loop():
    """Open point for M2 (plan §9): the controller counts pulses with every
    line step forced on, so a phase the camera is OFF in still sizes its
    repeat loop. A program knows only the phases the camera takes part in.
    They differ only when a disabled phase has more pulses than every
    enabled one; pinned here until it is decided."""
    scan, digital = goldens.TTL_CASES['ttl_linesteps']
    stage = goldens.CASES[scan]
    digital = dict(copy.deepcopy(digital), sequence_time=stage['sequence_time'])
    digital['linestep_enable']['Camera'] = [True, False, True]
    digital['pulse_starts_s']['Camera'][1] = [0.0, 2.0e-3]
    digital['pulse_ends_s']['Camera'][1] = [1.0e-3, 3.0e-3]
    info, masks, counts, devices = _controller_inputs(stage, digital, ['Camera'])
    assert counts['Camera'] == (1, 2, 1)
    _, _, p_masks, p_counts = layout_inputs(_schedule(stage, digital), ['Camera'])
    assert p_counts['Camera'] == (1, 0, 1) and p_masks['Camera'] == [True, False, True]


# ---------------------------------------------------------------------------
# Which policy a saved scan runs under
# ---------------------------------------------------------------------------

def _legacy_state():
    return {'analog': dict(goldens.CASES['xy_centred']),
            'digital': dict(goldens.TTL_CASES['ttl_whole_pixel'][1])}


def test_a_recognized_legacy_advanced_file_runs_compatibly():
    state = _legacy_state()
    assert is_legacy_advanced_state(state)
    assert timing_policy_for_state(state).id == COMPATIBLE_POLICY


def test_a_saved_policy_is_used_and_an_unknown_one_refused():
    state = dict(_legacy_state(), timingPolicy='strict-v1')
    assert timing_policy_for_state(state).id == 'strict-v1'
    with pytest.raises(PolicyError, match='Unknown timing policy'):
        timing_policy_for_state(dict(state, timingPolicy='strict-v2'))


def test_a_new_format_program_without_a_policy_gains_no_exemption():
    with pytest.raises(PolicyError, match='not a recognized legacy'):
        timing_policy_for_state({'loops': [], 'drives': []})
    broken = _legacy_state()
    del broken['analog']['axis_step_size']
    with pytest.raises(PolicyError, match='not a recognized legacy'):
        timing_policy_for_state(broken)


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
