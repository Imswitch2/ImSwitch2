from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.simple_monalisa_scan import (
    MoNaLISALimits,
    default_plan,
    dicts_to_plan,
    plan_to_dicts,
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
        }
        self.detectors = {'Camera': _device(acquisition=True, digital='line3')}
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


def test_resolft_plan_round_trips_and_uses_fixed_setup_lengths():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    analog, digital = plan_to_dicts(plan, limits)

    assert plan.scan_type == 'XY'
    assert analog['scan_dim_target_device'][:2] == ['Stage X', 'Stage Y']
    assert analog['axis_length'][:2] == pytest.approx([0.72, 0.72])
    assert analog['sequence_time'] == pytest.approx(0.0103)
    assert '488 OFF' in digital['target_device']
    assert digital['pulse_starts_s']['405 ON'][0] == pytest.approx([0.0001])
    assert digital['pulse_starts_s']['488 OFF'][0] == pytest.approx([0.0012])
    assert digital['pulse_starts_s']['488 EXC'][0] == pytest.approx([0.0033])
    assert dicts_to_plan(analog, digital, limits) == plan


def test_confocal_omits_only_the_off_pulse_and_shortens_dwell():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    plan = plan.__class__(**{**plan.__dict__, 'mode': 'confocal'})
    analog, digital = plan_to_dicts(plan, limits)

    assert '488 OFF' not in digital['target_device']
    assert '405 ON' in digital['target_device']
    assert digital['pulse_starts_s']['405 ON'][0] == pytest.approx([0.0001])
    assert digital['pulse_starts_s']['488 EXC'][0] == pytest.approx([0.0012])
    assert analog['sequence_time'] == pytest.approx(0.0082)
    assert dicts_to_plan(analog, digital, limits).mode == 'confocal'


def test_xyz_uses_lateral_step_for_xy_and_separate_z_step():
    limits = MoNaLISALimits.from_setup(_Setup())
    plan = default_plan(limits)
    plan = plan.__class__(**{
        **plan.__dict__, 'scan_type': 'XYZ', 'z_length_um': 1.2,
    })
    analog, _ = plan_to_dicts(plan, limits)
    assert analog['axis_step_size'] == pytest.approx([0.04, 0.04, 0.15])
    assert analog['axis_length'] == pytest.approx([0.72, 0.72, 1.2])
    assert plan.pixels(limits) == (18, 18, 8)
    assert dicts_to_plan(analog, plan_to_dicts(plan, limits)[1], limits).z_length_um == pytest.approx(1.2)


def test_one_laser_can_be_both_off_and_readout_and_round_trip_both_modes():
    setup = _Setup()
    setup.scan.simpleMoNaLISA['readoutLaser'] = '488 OFF'
    limits = MoNaLISALimits.from_setup(setup)

    resolft = default_plan(limits)
    analog, digital = plan_to_dicts(resolft, limits)
    assert len(digital['pulse_starts_s']['488 OFF'][0]) == 2
    assert dicts_to_plan(analog, digital, limits) == resolft

    confocal = resolft.__class__(**{**resolft.__dict__, 'mode': 'confocal'})
    analog, digital = plan_to_dicts(confocal, limits)
    assert len(digital['pulse_starts_s']['488 OFF'][0]) == 1
    restored = dicts_to_plan(analog, digital, limits)
    assert restored.mode == 'confocal'
    assert restored == confocal
