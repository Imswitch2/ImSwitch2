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
                'defaultLateralStepUm': 0.04,
                'defaultZScanLengthUm': 1.0, 'defaultZStepUm': 0.15,
                'interPulseDelayMs': 0.1, 'cameraReadoutTimeMs': 6,
                **_profiles_config(),
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


def test_empty_profile_config_stays_empty_and_requires_explicit_profile():
    setup = _Setup()
    setup.scan.simpleMoNaLISA = {
        'fovUm': [40, 45], 'ulensPeriodUm': 0.72,
        'interPulseDelayMs': 0.1, 'cameraReadoutTimeMs': 6,
    }
    limits = MoNaLISALimits.from_setup(setup)
    assert limits.profile_store.profiles == {}
    assert limits.profile_store.default_profile == ''
    with pytest.raises(Exception, match='No MoNaLISA profile'):
        default_plan(limits)


def test_default_profile_plan_round_trips_and_uses_fixed_setup_lengths():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    analog, digital = plan_to_dicts(plan, limits)

    assert plan.scan_type == 'XY'
    assert plan.profile_id == 'green'
    assert plan.mode_id == 'resolft'
    assert analog['scan_dim_target_device'][:2] == ['Stage X', 'Stage Y']
    assert analog['axis_length'][:2] == pytest.approx([0.72, 0.72])
    assert analog['sequence_time'] == pytest.approx(0.0103)
    assert digital['pulse_starts_s']['405 ON'][0] == pytest.approx([0.0001])
    assert digital['pulse_starts_s']['488 OFF'][0] == pytest.approx([0.0012])
    assert digital['pulse_starts_s']['488 EXC'][0] == pytest.approx([0.0033])
    assert dicts_to_plan(analog, digital, limits, plan) == plan


def test_readout_pulse_duration_is_setup_owned_and_clamped_to_one_ms():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['readoutPulseTimeMs'] = 2.5
    setup.scan.simpleMoNaLISA['profiles']['green']['steps'][2]['defaultDurationMs'] = 7.0
    setup.scan.simpleMoNaLISA['profiles']['green']['steps'][2]['durationEditable'] = True
    limits = MoNaLISALimits.from_setup(setup)

    plan = default_plan(limits)
    assert limits.readout_pulse_time_s == pytest.approx(0.0025)
    assert plan.step_durations_s['readout'] == pytest.approx(0.0025)

    too_short = _Setup()
    too_short.scan.simpleMoNaLISA['readoutPulseTimeMs'] = 0.2
    short_limits = MoNaLISALimits.from_setup(too_short)
    assert short_limits.readout_pulse_time_s == pytest.approx(0.001)
    assert default_plan(short_limits).step_durations_s['readout'] == pytest.approx(0.001)


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
    setup.scan.simpleMoNaLISA['profiles']['green']['steps'][2]['ttlDevices'] = ['488 OFF', 'Camera']
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
    # readout default; READOUT timing is setup-owned and must ignore that
    # profile-local value.
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
    assert adapted.step_durations_s['readout'] == pytest.approx(
        new_limits.readout_pulse_time_s
    )


def test_fixed_ulens_period_is_setup_owned_and_mode_independent():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['ulensPeriodPolicy'] = 'fixed'
    setup.scan.simpleMoNaLISA['ulensPeriodUm'] = 0.81
    # A stray profile value must not override a fixed setup period.
    setup.scan.simpleMoNaLISA['profiles']['green']['ulensPeriodUm'] = 0.63
    limits = MoNaLISALimits.from_setup(setup)

    resolft = default_plan(limits)
    confocal = select_mode(resolft, 'confocal', limits)

    assert limits.ulens_period_um('green') == pytest.approx(0.81)
    assert resolft.lateral_length_um == pytest.approx(0.81)
    assert confocal.lateral_length_um == pytest.approx(0.81)


def test_per_profile_ulens_period_changes_with_profile_not_mode():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['ulensPeriodPolicy'] = 'per_profile'
    setup.scan.simpleMoNaLISA['profiles']['green']['ulensPeriodUm'] = 0.81
    setup.scan.simpleMoNaLISA['profiles']['dual_color']['ulensPeriodUm'] = 0.63
    setup.scan.simpleMoNaLISA['profiles']['dual_off']['ulensPeriodUm'] = 0.72
    limits = MoNaLISALimits.from_setup(setup)

    green = default_plan(limits)
    green_confocal = select_mode(green, 'confocal', limits)
    dual = select_profile(green, 'dual_color', limits)

    assert green.lateral_length_um == pytest.approx(0.81)
    assert green_confocal.lateral_length_um == pytest.approx(0.81)
    assert dual.lateral_length_um == pytest.approx(0.63)


def test_manual_scan_extent_is_independent_of_ulens_and_round_trips_backend():
    setup = _Setup()
    profile = setup.scan.simpleMoNaLISA['profiles']['green']
    profile['scanExtentSource'] = 'manual'
    profile['scanExtentUm'] = 2.5
    limits = MoNaLISALimits.from_setup(setup)
    plan = default_plan(limits)

    assert plan.lateral_length_um == pytest.approx(2.5)
    assert limits.ulens_period_um('green') == pytest.approx(0.72)

    analog, digital = plan_to_dicts(plan, limits)
    analog['axis_length'][0] = 3.2
    analog['axis_length'][1] = 3.2
    parsed = dicts_to_plan(analog, digital, limits, plan)
    assert parsed.lateral_length_um == pytest.approx(3.2)


def test_simple_monalisa_slider_ranges_are_setup_configurable():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['sliderRanges'] = {
        'pixelSizeNm': [12, 420],
        'lateralScanLengthUm': [0.2, 7.5],
        'stepDurationMs': [0.05, 12],
    }
    limits = MoNaLISALimits.from_setup(setup)

    assert limits.slider_ranges['pixelSizeNm'] == pytest.approx((12, 420))
    assert limits.slider_ranges['lateralScanLengthUm'] == pytest.approx((0.2, 7.5))
    assert limits.slider_ranges['stepDurationMs'] == pytest.approx((0.05, 12))
    # Unspecified controls keep their setup defaults.
    assert limits.slider_ranges['zStepNm'][0] > 0
    assert limits.detector_devices == ('Camera',)


def test_camera_readout_inherits_setup_default_and_profile_can_override():
    setup = _Setup()
    setup.scan.simpleMoNaLISA.pop('cameraReadoutTimeMs', None)
    setup.scan.simpleMoNaLISA['defaultCameraReadoutTimeMs'] = 3.0
    limits = MoNaLISALimits.from_setup(setup)
    inherited = default_plan(limits)

    assert limits.camera_readout_time_s('green') == pytest.approx(0.003)
    assert limits.camera_readout_source('green') == 'setup default'
    assert inherited.dwell_s(limits) == pytest.approx(0.0073)

    profile_cfg = setup.scan.simpleMoNaLISA['profiles']['green']
    profile_cfg['cameraReadoutTimeMs'] = 2.4
    overridden_limits = MoNaLISALimits.from_setup(setup)
    overridden = default_plan(overridden_limits)

    assert overridden_limits.camera_readout_time_s('green') == pytest.approx(0.0024)
    assert overridden_limits.camera_readout_source('green') == 'profile'
    assert overridden.dwell_s(overridden_limits) == pytest.approx(0.0067)


def test_legacy_setup_camera_readout_is_default_compatibility_fallback():
    setup = _Setup()
    # _Setup intentionally uses the old setup-level key.
    limits = MoNaLISALimits.from_setup(setup)
    assert limits.default_camera_readout_time_s == pytest.approx(0.006)
    assert limits.camera_readout_time_s('green') == pytest.approx(0.006)


def test_profile_z_geometry_defaults_override_setup_and_round_trip():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['profiles']['green']['defaultZStepUm'] = 0.08
    setup.scan.simpleMoNaLISA['profiles']['green']['defaultZScanLengthUm'] = 2.4
    limits = MoNaLISALimits.from_setup(setup)

    plan = default_plan(limits)
    profile = limits.profile_store.profile('green')

    assert profile.default_z_step_um == pytest.approx(0.08)
    assert profile.default_z_scan_length_um == pytest.approx(2.4)
    assert plan.z_step_um == pytest.approx(0.08)
    assert plan.z_length_um == pytest.approx(2.4)
    saved = profile.to_config()
    assert saved['defaultZStepUm'] == pytest.approx(0.08)
    assert saved['defaultZScanLengthUm'] == pytest.approx(2.4)


def test_profile_without_z_defaults_keeps_setup_fallback():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['defaultZStepUm'] = 0.22
    setup.scan.simpleMoNaLISA['defaultZScanLengthUm'] = 1.7
    limits = MoNaLISALimits.from_setup(setup)

    profile = limits.profile_store.profile('green')
    plan = default_plan(limits)

    assert profile.default_z_step_um is None
    assert profile.default_z_scan_length_um is None
    assert plan.z_step_um == pytest.approx(0.22)
    assert plan.z_length_um == pytest.approx(1.7)


def test_xy_backend_parse_preserves_reference_profile_z_geometry():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['profiles']['green']['defaultZStepUm'] = 0.09
    setup.scan.simpleMoNaLISA['profiles']['green']['defaultZScanLengthUm'] = 2.1
    limits = MoNaLISALimits.from_setup(setup)
    reference = default_plan(limits)
    analog, digital = plan_to_dicts(reference, limits)

    restored = dicts_to_plan(analog, digital, limits, reference)

    assert restored.scan_type == 'XY'
    assert restored.z_step_um == pytest.approx(0.09)
    assert restored.z_length_um == pytest.approx(2.1)


def test_profile_reload_uses_profile_owned_z_defaults():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['profiles']['dual_off']['defaultZStepUm'] = 0.11
    setup.scan.simpleMoNaLISA['profiles']['dual_off']['defaultZScanLengthUm'] = 1.9
    limits = MoNaLISALimits.from_setup(setup)
    current = dataclasses.replace(default_plan(limits), z_step_um=0.3, z_length_um=4.0)

    reloaded = select_profile(current, 'dual_off', limits, preserve_if_same=False)

    assert reloaded.z_step_um == pytest.approx(0.11)
    assert reloaded.z_length_um == pytest.approx(1.9)


def test_a_fixed_readout_step_plays_the_setup_readout_pulse_time():
    """The readout pulse time is setup-owned: a profile's readout step that
    is not editable plays it too, not its own default (the panel showed
    2.5 ms while the camera TTL played 1 ms)."""
    setup = _Setup()
    setup.scan.simpleMoNaLISA['readoutPulseTimeMs'] = 2.5
    readout = setup.scan.simpleMoNaLISA['profiles']['green']['steps'][2]
    assert readout['durationEditable'] is False and readout['defaultDurationMs'] == 1
    limits = MoNaLISALimits.from_setup(setup)

    plan = default_plan(limits)
    assert plan.step_durations_s['readout'] == pytest.approx(0.0025)
    analog, digital = plan_to_dicts(plan, limits)
    (start,), (end,) = digital['pulse_starts_s']['Camera'][0], digital['pulse_ends_s']['Camera'][0]
    assert end - start == pytest.approx(0.0025)
    assert dicts_to_plan(analog, digital, limits, plan) == plan


def test_recording_naming_defaults_include_all_active_steps_including_readout():
    from imswitch.imcontrol.model.simple_monalisa_scan import render_recording_suffix

    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)

    assert render_recording_suffix(plan, limits) == 'RESOLFT_ON1ms_OFF2ms_RO1ms'


def test_recording_naming_can_include_effective_laser_power_and_units():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        MoNaLISARecordingNaming,
        render_recording_suffix,
    )

    limits = MoNaLISALimits.from_setup(_Setup())
    limits = dataclasses.replace(
        limits,
        recording_naming=MoNaLISARecordingNaming(
            enabled=True,
            template='{mode}_{sequence}',
            include_power=True,
        ),
    )
    plan = default_plan(limits)
    laser_states = {
        '405 ON': {
            'active_for_scan': True, 'power_device': '405 ON',
            'value': 50, 'value_units': 'mW',
        },
        '488 OFF': {
            'active_for_scan': True, 'power_device': '488 OFF',
            'value': 200, 'value_units': 'mW',
        },
        '488 EXC': {
            'active_for_scan': True, 'power_device': '488 EXC',
            'value': 25, 'value_units': 'mW',
        },
    }

    assert render_recording_suffix(
        plan, limits, laser_states=laser_states
    ) == 'RESOLFT_ON1ms50mW_OFF2ms200mW_RO1ms25mW'


def test_recording_naming_uses_only_steps_enabled_by_current_mode():
    from imswitch.imcontrol.model.simple_monalisa_scan import render_recording_suffix

    limits = MoNaLISALimits.from_setup(_Setup())
    plan = select_mode(default_plan(limits), 'confocal', limits)

    assert render_recording_suffix(plan, limits) == 'Confocal_ON1ms_RO1ms'


def test_recording_scan_info_uses_slice_for_axial_planes_and_expands_anisotropic_steps():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        MoNaLISARecordingNaming,
        render_recording_suffix,
    )

    limits = MoNaLISALimits.from_setup(_Setup())
    naming = MoNaLISARecordingNaming(
        enabled=True,
        template='{mode}',
        include_scan_info=True,
    )
    limits = dataclasses.replace(limits, recording_naming=naming)
    plan = dataclasses.replace(
        default_plan(limits),
        scan_type='YZ',
        lateral_step_um=0.05,
        x_step_um=0.05,
        y_step_um=0.05,
        z_step_um=0.1,
    )

    assert render_recording_suffix(plan, limits) == 'RESOLFT_slice_y50nm_z100nm'

    plan = dataclasses.replace(plan, z_step_um=0.05)
    assert render_recording_suffix(plan, limits) == 'RESOLFT_slice_yz50nm'


def test_recording_scan_labels_are_configurable():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        MoNaLISARecordingNaming,
        render_recording_suffix,
    )

    limits = MoNaLISALimits.from_setup(_Setup())
    naming = MoNaLISARecordingNaming(
        enabled=True,
        template='{mode}',
        include_scan_info=True,
        scan_labels={'XY': 'lateral', 'XZ': 'axial', 'YZ': 'axial', 'XYZ': 'volume'},
    )
    limits = dataclasses.replace(limits, recording_naming=naming)
    plan = dataclasses.replace(
        default_plan(limits),
        scan_type='YZ',
        lateral_step_um=0.05,
        x_step_um=0.05,
        y_step_um=0.05,
        z_step_um=0.05,
    )

    assert render_recording_suffix(plan, limits) == 'RESOLFT_axial_yz50nm'


def test_recording_naming_rejects_unknown_template_tokens():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        RecordingNamingTemplateError,
        validate_recording_naming_template,
    )

    with pytest.raises(RecordingNamingTemplateError, match='Unknown recording naming token'):
        validate_recording_naming_template('{mode}_{unknown}')


def test_recording_naming_config_is_global_setup_state_and_round_trips():
    from imswitch.imcontrol.model.simple_monalisa_scan import MoNaLISARecordingNaming

    setup = _Setup()
    setup.scan.simpleMoNaLISA['recordingNaming'] = {
        'enabled': True,
        'template': '{profile}_{mode}_{sequence}',
        'includePower': True,
        'includeScanInfo': True,
        'scanLabels': {'XY': 'lateral', 'YZ': 'axial'},
    }
    limits = MoNaLISALimits.from_setup(setup)
    naming = limits.recording_naming

    assert naming.template == '{profile}_{mode}_{sequence}'
    assert naming.include_power is True
    assert naming.include_scan_info is True
    assert naming.scan_labels['XY'] == 'lateral'
    assert naming.scan_labels['YZ'] == 'axial'
    # Missing labels inherit the setup-wide defaults rather than disappearing.
    assert naming.scan_labels['XZ'] == 'slice'
    assert naming.scan_labels['XYZ'] == '3D'
    assert MoNaLISARecordingNaming.from_config(naming.to_config()) == naming


def test_recording_scan_info_covers_xy_xz_yz_xyz_default_conventions():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        MoNaLISARecordingNaming,
        render_recording_suffix,
    )

    limits = MoNaLISALimits.from_setup(_Setup())
    naming = MoNaLISARecordingNaming(
        enabled=True, template='{mode}', include_scan_info=True
    )
    limits = dataclasses.replace(limits, recording_naming=naming)
    base = dataclasses.replace(
        default_plan(limits),
        lateral_step_um=0.05,
        x_step_um=0.05,
        y_step_um=0.05,
        z_step_um=0.05,
    )

    expected = {
        'XY': 'RESOLFT_2D_xy50nm',
        'XZ': 'RESOLFT_slice_xz50nm',
        'YZ': 'RESOLFT_slice_yz50nm',
        'XYZ': 'RESOLFT_3D_xyz50nm',
    }
    for scan_type, suffix in expected.items():
        plan = dataclasses.replace(base, scan_type=scan_type)
        assert render_recording_suffix(plan, limits) == suffix


def test_recording_naming_multiple_laser_powers_are_unambiguous():
    from imswitch.imcontrol.model.simple_monalisa_scan import (
        MoNaLISARecordingNaming,
        render_recording_suffix,
        select_profile,
    )

    limits = MoNaLISALimits.from_setup(_Setup())
    naming = MoNaLISARecordingNaming(
        enabled=True, template='{mode}_{sequence}', include_power=True
    )
    limits = dataclasses.replace(limits, recording_naming=naming)
    plan = select_profile(default_plan(limits), 'dual_off', limits)
    laser_states = {
        '405 ON': {
            'active_for_scan': True, 'power_device': '405 ON',
            'value': 5, 'value_units': 'mW',
        },
        '488 OFF': {
            'active_for_scan': True, 'power_device': '488 OFF',
            'value': 100, 'value_units': 'mW',
        },
        '488 OFF 2': {
            'active_for_scan': True, 'power_device': '488 OFF 2',
            'value': 120, 'value_units': 'mW',
        },
        '488 EXC': {
            'active_for_scan': True, 'power_device': '488 EXC',
            'value': 20, 'value_units': 'mW',
        },
    }

    suffix = render_recording_suffix(plan, limits, laser_states=laser_states)
    assert '_OFF2ms_488_OFF-100mW_488_OFF_2-120mW_' in suffix


def test_recording_naming_sanitizes_filename_unsafe_profile_and_mode_labels():
    from imswitch.imcontrol.model.simple_monalisa_scan import render_recording_suffix

    setup = _Setup()
    setup.scan.simpleMoNaLISA['profiles']['green']['label'] = 'Green / test'
    setup.scan.simpleMoNaLISA['profiles']['green']['modes']['resolft']['label'] = (
        'RESOLFT:fast'
    )
    setup.scan.simpleMoNaLISA['recordingNaming'] = {
        'template': '{profile}_{mode}_{sequence}',
    }
    limits = MoNaLISALimits.from_setup(setup)

    suffix = render_recording_suffix(default_plan(limits), limits)
    assert suffix == 'Green_test_RESOLFT_fast_ON1ms_OFF2ms_RO1ms'
    assert '/' not in suffix
    assert ':' not in suffix
