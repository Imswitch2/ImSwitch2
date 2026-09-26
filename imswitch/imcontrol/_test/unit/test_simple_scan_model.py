"""The SimplePointScan plan model (docs/simple-point-scan-plan.md §5.1, D4-D6).

Pure tests: limits derived from the shipped galvo/APD mock setup, the plan's
conversion to and from Advanced-style dicts within the representable subset,
refusals with reasons, channel-power validation, snapping and slider
mappings, and the overview planner with injected estimate/fit functions.
"""
import copy

import pytest

from imswitch.imcontrol.model.simple_scan import (
    AxisRegion,
    PlanNotRepresentable,
    ScanLimits,
    SimpleScanPlan,
    dicts_to_plan,
    log_position,
    log_value,
    plan_overview,
    plan_to_dicts,
    power_refusal,
    snap_dwell_s,
    snap_length_um,
)

from .test_mock_scan_simulation_coordinator import _setup_from_user_default


@pytest.fixture(scope='module')
def setup():
    return _setup_from_user_default('galvo_apd_mock_scan_setup.json')


@pytest.fixture(scope='module')
def limits(setup):
    return ScanLimits.from_setup(setup)


def _plan(**overrides):
    values = dict(
        dims=('X', 'Y'),
        regions={'X': AxisRegion(1.0, 10.0, 0.5), 'Y': AxisRegion(-2.0, 6.0, 0.5)},
        dwell_s=40e-6,
        channels=(('405 (ON)',), ('488 (EXC)',)),
        park={'Z': 5.0},
    )
    values.update(overrides)
    return SimpleScanPlan(**values)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def test_limits_come_from_the_setup_file(limits):
    x, y, z = limits.axes
    assert (x.name, y.name, z.name) == ('X', 'Y', 'Z')
    assert x.smooth and y.smooth and not z.smooth
    assert x.range_um == pytest.approx((-17.5, 17.5))     # ±10 V x 1.75 µm/V
    assert z.range_um == pytest.approx((0.0, 10.0))
    assert x.speed_limit == 0.1 and z.speed_limit is None
    assert [g.name for g in limits.gates] == ['405 (ON)', '488 (EXC)']
    assert limits.sample_rate == 100000
    assert limits.overview_axes == ('X', 'Y')
    assert limits.overview_field_um() == pytest.approx(35.0)


def test_the_shortest_dwell_is_samples_or_scanner_speed_whichever_is_longer(limits):
    # two samples at 100 kHz
    assert limits.min_dwell_s('X', 0.5) == pytest.approx(20e-6)
    # 5 µm pixels at vel_max 0.1 µm/µs need 50 µs
    assert limits.min_dwell_s('X', 5.0) == pytest.approx(50e-6)
    # a stepped fast axis has no speed limit
    assert limits.min_dwell_s('Z', 5.0) == pytest.approx(20e-6)


def test_the_speed_rule_agrees_with_the_designer(setup, limits):
    """The panel's shortest dwell is exactly what the designer accepts."""
    from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
        GalvoScanDesigner,
    )
    plan = _plan(regions={'X': AxisRegion(0.0, 20.0, 4.0), 'Y': AxisRegion(0.0, 8.0, 4.0)},
                 dwell_s=limits.min_dwell_s('X', 4.0))
    analog, _ = plan_to_dicts(plan, limits)
    designer = GalvoScanDesigner()
    assert designer.scanSpeedRefusal(analog, setup) == ''
    faster = dict(analog, sequence_time=plan.dwell_s - 1 / limits.sample_rate)
    assert designer.scanSpeedRefusal(faster, setup) != ''


def test_nyquist_from_config_or_from_na_and_wavelength(setup):
    configured = ScanLimits.from_setup(setup)
    assert configured.nyquist_um([488])[0] is None

    object.__setattr__(configured, 'config', dict(configured.config, objectiveNA=1.4))
    value, source = configured.nyquist_um([561, 488])
    assert value == pytest.approx(0.488 / (8 * 1.4))
    assert '488' in source

    object.__setattr__(configured, 'config', dict(configured.config, nyquistPixelSizeUm=0.02))
    assert configured.nyquist_um([488])[0] == 0.02


# ---------------------------------------------------------------------------
# Plan <-> dicts (D4)
# ---------------------------------------------------------------------------

def test_a_plan_round_trips_through_the_dicts(limits):
    plan = _plan()
    analog, digital = plan_to_dicts(plan, limits)

    assert analog['scan_dim_target_device'] == ['X', 'Y', 'None']
    assert analog['target_device'] == ['X', 'Y', 'Z']
    assert analog['axis_centerpos'] == [1.0, -2.0, 5.0]
    assert digital['n_linesteps'] == 2
    assert digital['linestep_enable'] == {'405 (ON)': [True, False],
                                          '488 (EXC)': [False, True]}
    assert (digital['Nx'], digital['Ny']) == (20, 12)
    assert digital['advanced_mode'] is False

    assert dicts_to_plan(analog, digital, limits) == plan


def test_lasers_fired_together_share_a_line_pass(limits):
    plan = _plan(channels=(('405 (ON)', '488 (EXC)'),))
    _, digital = plan_to_dicts(plan, limits)
    assert digital['n_linesteps'] == 1
    assert digital['linestep_enable'] == {'405 (ON)': [True], '488 (EXC)': [True]}


def test_an_imported_non_integral_length_is_kept_verbatim(limits):
    """10 µm at 0.3 µm is 33 pixels; rewriting it to 9.9 µm would move the
    sweep and its first pixel (plan D4, review 2 point 4)."""
    analog, digital = plan_to_dicts(_plan(), limits)
    analog['axis_length'][0] = 10.0
    analog['axis_step_size'][0] = 0.3

    plan = dicts_to_plan(analog, digital, limits)

    assert plan.regions['X'] == AxisRegion(1.0, 10.0, 0.3)
    assert plan_to_dicts(plan, limits)[0]['axis_length'][0] == 10.0


def test_inactive_power_stays_inactive(setup):
    """An Advanced file with stored percentages but advanced_mode off must not
    switch power on when imported (plan D5)."""
    import dataclasses
    limits = ScanLimits.from_setup(setup)
    gates = tuple(dataclasses.replace(g, power_capable=True) for g in limits.gates)
    limits = dataclasses.replace(limits, gates=gates)
    analog, digital = plan_to_dicts(_plan(), limits)
    digital['linestep_power_percent'] = {'488 (EXC)': [100.0, 40.0]}
    digital['linestep_power_enabled'] = {'488 (EXC)': False}

    plan = dicts_to_plan(analog, digital, limits)

    assert plan.channel_power_on is False
    assert plan.channel_power == {'488 (EXC)': (100.0, 40.0)}
    assert plan.channel_power_enabled == {'488 (EXC)': False}
    _, again = plan_to_dicts(plan, limits)
    assert again['advanced_mode'] is False
    assert again['linestep_power_percent']['488 (EXC)'] == [100.0, 40.0]
    assert again['linestep_power_enabled']['488 (EXC)'] is False


@pytest.mark.parametrize('change, reason', [
    (lambda a, d: d.update(advanced_mode=True,
                           pulse_starts_s={'405 (ON)': [[1e-5], []]},
                           pulse_ends_s={'405 (ON)': [[2e-5], []]}),
     'timing windows'),
    (lambda a, d: d.update(advanced_program_mode='sequence',
                           advanced_sequence_rows={0: [{'devices': ['405 (ON)']}]}),
     'Sequence Builder'),
    (lambda a, d: d.update(intra_pixel_positioner_movement=True), 'within the pixel'),
    (lambda a, d: d.update(advanced_device_lock_master={'405 (ON)': True}), 'locks devices'),
    (lambda a, d: (d['target_device'].append('APD'),
                   d['linestep_enable'].update(APD=[True, True])), 'only lasers'),
    (lambda a, d: d['linestep_enable'].update({'488 (EXC)': [False, False]}), 'Line pass 2'),
    (lambda a, d: (a['target_device'].__setitem__(2, 'Galvo9')), 'Galvo9'),
])
def test_what_the_panel_cannot_show_is_refused_with_the_reason(limits, change, reason):
    analog, digital = plan_to_dicts(_plan(), limits)
    change(analog, digital)
    with pytest.raises(PlanNotRepresentable, match=reason):
        dicts_to_plan(analog, digital, limits)


def test_advanced_mode_with_empty_windows_is_channel_power(limits):
    analog, digital = plan_to_dicts(_plan(channel_power_on=True), limits)
    assert digital['advanced_mode'] is True
    assert all(step == [] for steps in digital['pulse_starts_s'].values() for step in steps)
    assert dicts_to_plan(analog, digital, limits).channel_power_on is True


def test_the_plan_survives_json():
    import json
    plan = _plan(channel_power_on=True, channel_power={'488 (EXC)': (80.0, 20.0)},
                 channel_power_enabled={'488 (EXC)': True}, phase_delay_us=12.5,
                 d3step_delay_us=500.0)
    assert SimpleScanPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan


# ---------------------------------------------------------------------------
# Channel power validation (D5)
# ---------------------------------------------------------------------------

def test_power_on_a_gate_without_an_analog_channel_is_refused(limits):
    plan = _plan(channel_power_on=True, channel_power={'488 (EXC)': (100.0, 50.0)})
    assert 'no analog channel' in power_refusal(plan, limits)


def test_power_outside_0_to_100_is_refused(setup):
    import dataclasses
    limits = ScanLimits.from_setup(setup)
    limits = dataclasses.replace(
        limits, gates=tuple(dataclasses.replace(g, power_capable=True) for g in limits.gates)
    )
    plan = _plan(channel_power_on=True, channel_power={'488 (EXC)': (100.0, 150.0)})
    assert '0-100 %' in power_refusal(plan, limits)


def test_a_laser_without_a_digital_line_is_refused(limits):
    plan = _plan(channels=(('405 (ON)', 'SomeAOTF'),))
    assert 'no digital line' in power_refusal(plan, limits)


def test_power_off_needs_no_analog_channel(limits):
    plan = _plan(channel_power={'488 (EXC)': (100.0, 50.0)})
    assert power_refusal(plan, limits) == ''


# ---------------------------------------------------------------------------
# Snapping and sliders
# ---------------------------------------------------------------------------

def test_dwell_is_a_whole_number_of_samples_and_never_below_the_minimum():
    assert snap_dwell_s(33e-6, 100000) == pytest.approx(30e-6)
    assert snap_dwell_s(33e-6, 100000, minimum=35e-6) == pytest.approx(40e-6)
    assert snap_dwell_s(1e-9, 100000) == pytest.approx(10e-6)


def test_lengths_simple_creates_are_whole_numbers_of_steps():
    assert snap_length_um(10.0, 0.3) == pytest.approx(9.9)
    assert snap_length_um(0.1, 0.3) == pytest.approx(0.3)


def test_a_plan_is_kept_on_the_grid_advanced_stores():
    """1 nm positions and steps, whole-µs delays: finer values would build a
    different scan once loaded into the Advanced panel (plan D4)."""
    from imswitch.imcontrol.model.simple_scan import normalize_plan
    plan = normalize_plan(_plan(
        regions={'X': AxisRegion(1.23456, 7.0, 0.0720577), 'Y': AxisRegion(0.0, 10.0, 0.3)},
        park={'Z': 3.00049}, phase_delay_us=12.6,
    ))
    assert plan.regions['X'] == AxisRegion(1.235, snap_length_um(7.0, 0.072), 0.072)
    assert plan.regions['Y'] == AxisRegion(0.0, 10.0, 0.3)   # on the grid: kept
    assert plan.park == {'Z': 3.0}
    assert plan.phase_delay_us == 13.0


def test_the_log_sliders_round_trip():
    for position in (0.0, 0.25, 0.5, 1.0):
        value = log_value(position, 0.04, 2.0)
        assert log_position(value, 0.04, 2.0) == pytest.approx(position)
    assert log_value(0.5, 0.01, 1.0) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Overview planning (D6), with injected estimate and fit
# ---------------------------------------------------------------------------

def _timeModel(turnaround_s=4e-3):
    def estimate(field, pixels, step, dwell):
        return pixels * (pixels * dwell + turnaround_s)
    return estimate


def test_the_overview_takes_the_most_pixels_within_budget(limits):
    estimate = _timeModel()
    overview = plan_overview(limits, estimate=estimate,
                             fits=lambda *args: True, budget_s=1.0)

    assert overview.met is True
    # steps are kept to 1 nm (Advanced's precision), so the field is N whole
    # nanometre steps: within N nm of the one asked for
    assert overview.field_um == pytest.approx(35.0, abs=overview.pixels * 1e-3)
    assert overview.estimate_s <= 1.0
    step = round(35.0 / (overview.pixels + 1), 3)
    bigger = estimate(35.0, overview.pixels + 1, step,
                      limits.min_dwell_s('X', step))
    assert bigger > 1.0
    assert overview.dwell_s == pytest.approx(limits.min_dwell_s('X', overview.step_um))
    assert overview.step_um * overview.pixels == pytest.approx(overview.field_um)


def test_the_overview_shrinks_its_field_when_the_voltage_does_not_fit(limits):
    overview = plan_overview(
        limits, estimate=_timeModel(), budget_s=1.0,
        fits=lambda field, pixels, step, dwell: field <= 25.0,
    )
    assert overview.met is True
    assert overview.field_um <= 25.0
    assert overview.field_um == pytest.approx(35.0 * 0.8 ** 2, abs=overview.pixels * 1e-3)


def test_an_unreachable_budget_gives_the_fastest_overview_and_says_so(limits):
    overview = plan_overview(limits, estimate=lambda *a: 5.0,
                             fits=lambda *args: True, budget_s=1.0)
    assert overview.met is False
    assert overview.pixels == 64
    assert 'not reachable' in overview.note
    assert '5' in overview.note


def test_nothing_fitting_is_reported(limits):
    overview = plan_overview(limits, estimate=_timeModel(),
                             fits=lambda *args: False, budget_s=1.0)
    assert overview.met is False
    assert "voltage range" in overview.note


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
