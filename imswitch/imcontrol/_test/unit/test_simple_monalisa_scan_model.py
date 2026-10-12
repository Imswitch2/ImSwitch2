import dataclasses
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.scan_cloak import PlanNotRepresentable
from imswitch.imcontrol.model.simple_monalisa_scan import (
    MoNaLISALimits,
    default_plan,
    dicts_to_plan,
    plan_to_dicts,
    select_mode,
    select_profile,
)


def _device(*, axes=None, scanning=False, acquisition=False, digital=None):
    return SimpleNamespace(
        axes=axes or [], forScanning=scanning, forAcquisition=acquisition,
        digitalLine=digital,
    )


class _Setup:
    def __init__(self):
        self.positioners = {
            'Stage X': _device(axes=['X'], scanning=True),
            'Stage Y': _device(axes=['Y'], scanning=True),
            'Stage Z': _device(axes=['Z'], scanning=True),
        }
        self.lasers = {
            '488 OFF': _device(digital='line0'),
            '405 ON': _device(digital='line1'),
            '488 EXC': _device(digital='line2'),
            '488 OFF 2': _device(digital='line3'),
            '590': _device(digital='line4'),
        }
        self.detectors = {'Camera': _device(acquisition=True, digital='line5')}
        self.scan = SimpleNamespace(
            sampleRate=100000,
            scanDesignerParams={'return_time': 0.001},
            simpleMoNaLISA={
                'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
                'offLaser': '488 OFF', 'onLaser': '405 ON',
                'readoutLaser': '488 EXC', 'readoutDetector': 'Camera',
                'defaultLateralStepUm': 0.04,
                'defaultZScanLengthUm': 1.0, 'defaultZStepUm': 0.15,
                'defaultOffTimeMs': 2, 'defaultOnTimeMs': 1,
                'readoutTimeMs': 1, 'interPulseDelayMs': 0.1,
                'cameraReadoutTimeMs': 6,
            },
        )

    def getTTLDevices(self):
        return {**self.lasers, **self.detectors}


def _profiles_config():
    return {
        'defaultProfile': 'green',
        'profiles': {
            'green': {
                'label': 'Green', 'defaultMode': 'resolft',
                'steps': [
                    {'id': 'on', 'label': 'ON', 'role': 'on', 'ttlDevices': ['405 ON'], 'defaultDurationMs': 1},
                    {'id': 'off', 'label': 'OFF', 'role': 'off', 'ttlDevices': ['488 OFF'], 'defaultDurationMs': 2},
                    {'id': 'readout', 'label': 'Readout', 'role': 'readout', 'ttlDevices': ['488 EXC', 'Camera'], 'defaultDurationMs': 1, 'durationEditable': False},
                ],
                'modes': {
                    'resolft': {'label': 'RESOLFT', 'enabledSteps': ['on', 'off', 'readout']},
                    'confocal': {'label': 'Confocal', 'enabledSteps': ['on', 'readout']},
                },
            },
            'dual_off': {
                'label': 'Dual OFF', 'defaultMode': 'resolft',
                'steps': [
                    {'id': 'on', 'role': 'on', 'ttlDevices': ['405 ON'], 'defaultDurationMs': 1},
                    {'id': 'off', 'role': 'off', 'ttlDevices': ['488 OFF', '488 OFF 2'], 'defaultDurationMs': 2},
                    {'id': 'readout', 'role': 'readout', 'ttlDevices': ['488 EXC', 'Camera'], 'defaultDurationMs': 1, 'durationEditable': False},
                ],
                'modes': {'resolft': {'enabledSteps': ['on', 'off', 'readout']}},
            },
            'dual_color': {
                'label': 'Dual colour', 'defaultMode': 'resolft',
                'steps': [
                    {'id': 'on', 'role': 'on', 'ttlDevices': ['405 ON'], 'defaultDurationMs': 1},
                    {'id': 'green_off', 'role': 'off', 'ttlDevices': ['488 OFF'], 'defaultDurationMs': 2},
                    {'id': 'green_ro', 'role': 'readout', 'ttlDevices': ['488 EXC', 'Camera'], 'defaultDurationMs': 1, 'durationEditable': False},
                    {'id': 'red_off', 'role': 'off', 'ttlDevices': ['590'], 'defaultDurationMs': 2.5},
                    {'id': 'red_ro', 'role': 'readout', 'ttlDevices': ['590', 'Camera'], 'defaultDurationMs': 1, 'durationEditable': False},
                ],
                'modes': {
                    'resolft': {'label': 'RESOLFT', 'enabledSteps': ['on', 'green_off', 'green_ro', 'red_off', 'red_ro']},
                    'confocal': {'label': 'Confocal', 'enabledSteps': ['on', 'green_ro', 'red_ro']},
                    'green': {'label': 'Green only', 'enabledSteps': ['on', 'green_off', 'green_ro']},
                },
            },
        },
    }


def test_legacy_resolft_plan_round_trips_and_uses_fixed_setup_lengths():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    analog, digital = plan_to_dicts(plan, limits)

    assert plan.scan_type == 'XY'
    assert plan.profile_id == 'default'
    assert plan.mode_id == 'resolft'
    assert analog['scan_dim_target_device'][:2] == ['Stage X', 'Stage Y']
    assert analog['axis_length'][:2] == pytest.approx([0.72, 0.72])
    assert analog['sequence_time'] == pytest.approx(0.0103)
    assert digital['pulse_starts_s']['405 ON'][0] == pytest.approx([0.0001])
    assert digital['pulse_starts_s']['488 OFF'][0] == pytest.approx([0.0012])
    assert digital['pulse_starts_s']['488 EXC'][0] == pytest.approx([0.0033])
    assert dicts_to_plan(analog, digital, limits, plan) == plan


def test_mode_switch_preserves_hidden_step_timing():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    plan = dataclasses.replace(plan, step_durations_s={**plan.step_durations_s, 'off': 0.003})
    plan = select_mode(plan, 'confocal', limits)
    analog, digital = plan_to_dicts(plan, limits)

    assert '488 OFF' not in digital['target_device']
    assert analog['sequence_time'] == pytest.approx(0.0082)
    restored = dicts_to_plan(analog, digital, limits, plan)
    assert restored.mode_id == 'confocal'
    assert restored.step_durations_s['off'] == pytest.approx(0.003)
    assert select_mode(restored, 'resolft', limits).step_durations_s['off'] == pytest.approx(0.003)


def test_xyz_uses_lateral_step_for_xy_and_separate_z_step():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = dataclasses.replace(default_plan(limits), scan_type='XYZ', z_length_um=1.2)
    analog, digital = plan_to_dicts(plan, limits)
    assert analog['axis_step_size'] == pytest.approx([0.04, 0.04, 0.15])
    assert analog['axis_length'] == pytest.approx([0.72, 0.72, 1.2])
    assert plan.pixels(limits) == (18, 18, 8)
    assert dicts_to_plan(analog, digital, limits, plan).z_length_um == pytest.approx(1.2)


def test_same_physical_device_can_appear_in_multiple_steps():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['readoutLaser'] = '488 OFF'
    limits = MoNaLISALimits.from_setup(setup)
    plan = default_plan(limits)
    analog, digital = plan_to_dicts(plan, limits)
    assert len(digital['pulse_starts_s']['488 OFF'][0]) == 2
    assert dicts_to_plan(analog, digital, limits, plan) == plan


def test_profile_selection_is_explicit_and_dual_color_round_trips():
    setup = _Setup()
    setup.scan.simpleMoNaLISA = {
        'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
        'defaultZScanLengthUm': 1.0, 'defaultLateralStepUm': 0.04,
        'defaultZStepUm': 0.15, 'interPulseDelayMs': 0.1,
        'cameraReadoutTimeMs': 6, **_profiles_config(),
    }
    limits = MoNaLISALimits.from_setup(setup)
    plan = select_profile(default_plan(limits), 'dual_color', limits)
    analog, digital = plan_to_dicts(plan, limits)

    assert len(digital['pulse_starts_s']['590'][0]) == 2
    assert len(digital['pulse_starts_s']['Camera'][0]) == 2
    assert dicts_to_plan(analog, digital, limits, plan) == plan

    # Parsing the same backend against another explicitly selected profile is
    # an incompatibility; no profile discovery is attempted.
    wrong = select_profile(plan, 'green', limits, preserve_if_same=False)
    with pytest.raises(PlanNotRepresentable):
        dicts_to_plan(analog, digital, limits, wrong)


def test_advanced_confocal_pattern_does_not_auto_switch_resolft_binding():
    setup = _Setup()
    setup.scan.simpleMoNaLISA = {
        'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
        'interPulseDelayMs': 0.1, 'cameraReadoutTimeMs': 6,
        **_profiles_config(),
    }
    limits = MoNaLISALimits.from_setup(setup)
    resolft = default_plan(limits)
    confocal = select_mode(resolft, 'confocal', limits)
    analog, digital = plan_to_dicts(confocal, limits)

    with pytest.raises(PlanNotRepresentable):
        dicts_to_plan(analog, digital, limits, resolft)
    assert dicts_to_plan(analog, digital, limits, confocal).mode_id == 'confocal'


def test_profile_switch_preserves_geometry_and_uses_new_defaults():
    setup = _Setup()
    setup.scan.simpleMoNaLISA = {
        'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
        'interPulseDelayMs': 0.1, 'cameraReadoutTimeMs': 6,
        **_profiles_config(),
    }
    limits = MoNaLISALimits.from_setup(setup)
    plan = dataclasses.replace(default_plan(limits), scan_type='XYZ', z_length_um=1.7)
    switched = select_profile(plan, 'dual_color', limits)
    assert switched.scan_type == 'XYZ'
    assert switched.z_length_um == pytest.approx(1.7)
    assert switched.profile_id == 'dual_color'
    assert switched.step_durations_s['red_off'] == pytest.approx(0.0025)


def test_active_profile_edit_preserves_live_geometry_and_unchanged_editable_durations():
    from imswitch.imcontrol.model.monalisa_profiles import MoNaLISAProfileDefinition
    from imswitch.imcontrol.model.simple_monalisa_scan import adapt_plan_to_profile_definition

    setup = _Setup()
    setup.scan.simpleMoNaLISA = {
        'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
        'interPulseDelayMs': 0.1, 'cameraReadoutTimeMs': 6,
        **_profiles_config(),
    }
    limits = MoNaLISALimits.from_setup(setup)
    plan = dataclasses.replace(
        default_plan(limits),
        scan_type='XYZ', z_length_um=1.6,
        step_durations_s={'on': 0.0014, 'off': 0.0031, 'readout': 0.001},
    )

    old = limits.profile_store.profile('green')
    config = old.to_config()
    # Keep OFF's stable id but change its profile default; the current live
    # editable value must win. Add a new editable OFF step and change fixed
    # readout default; those should use profile defaults.
    for step in config['steps']:
        if step['id'] == 'off':
            step['defaultDurationMs'] = 8.0
        if step['id'] == 'readout':
            step['defaultDurationMs'] = 1.5
    config['steps'].insert(2, {
        'id': 'off_2', 'label': 'Second OFF', 'role': 'off',
        'ttlDevices': ['488 OFF 2'], 'defaultDurationMs': 2.7,
        'durationEditable': True,
    })
    config['modes']['resolft']['enabledSteps'].insert(2, 'off_2')
    edited = MoNaLISAProfileDefinition.from_config('green', config)
    new_store = limits.profile_store.with_profile(edited)
    new_limits = dataclasses.replace(limits, profile_store=new_store)

    adapted = adapt_plan_to_profile_definition(plan, edited, new_limits)
    assert adapted.scan_type == 'XYZ'
    assert adapted.z_length_um == pytest.approx(1.6)
    assert adapted.step_durations_s['off'] == pytest.approx(0.0031)
    assert adapted.step_durations_s['off_2'] == pytest.approx(0.0027)
    assert adapted.step_durations_s['readout'] == pytest.approx(0.0015)
