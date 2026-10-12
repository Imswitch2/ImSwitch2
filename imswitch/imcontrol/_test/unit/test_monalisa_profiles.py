import pytest

from imswitch.imcontrol.model.monalisa_profiles import (
    MoNaLISAProfileDefinition,
    MoNaLISAProfileError,
    MoNaLISAProfileStore,
    MoNaLISASequenceNotCompatible,
    compile_profile_sequence,
    parse_profile_sequence,
)


SAMPLE_RATE = 100000
DELAY_S = 0.0001


def _digital_from_compiled(compiled, all_devices):
    starts = {name: [[]] for name in all_devices}
    ends = {name: [[]] for name in all_devices}
    enabled = {name: [False] for name in all_devices}
    for name, windows in compiled.windows_by_device.items():
        starts[name] = [[start for start, _ in windows]]
        ends[name] = [[end for _, end in windows]]
        enabled[name] = [True]
    return {
        'target_device': list(compiled.windows_by_device),
        'linestep_enable': enabled,
        'pulse_starts_s': starts,
        'pulse_ends_s': ends,
    }


def _dual_color_profile():
    return MoNaLISAProfileDefinition.from_config('dual_color', {
        'label': 'Dual colour',
        'defaultMode': 'resolft',
        'steps': [
            {
                'id': 'activation', 'label': 'ON', 'role': 'on',
                'ttlDevices': ['405'], 'defaultDurationMs': 1.0,
                'durationEditable': True,
            },
            {
                'id': 'green_off', 'label': 'Green OFF', 'role': 'off',
                'ttlDevices': ['488'], 'defaultDurationMs': 2.0,
                'durationEditable': True,
            },
            {
                'id': 'green_readout', 'label': 'Green readout', 'role': 'readout',
                'ttlDevices': ['488', 'Camera'], 'defaultDurationMs': 1.0,
                'durationEditable': False,
            },
            {
                'id': 'red_off', 'label': 'Red OFF', 'role': 'off',
                'ttlDevices': ['590'], 'defaultDurationMs': 2.5,
                'durationEditable': True,
            },
            {
                'id': 'red_readout', 'label': 'Red readout', 'role': 'readout',
                'ttlDevices': ['590', 'Camera'], 'defaultDurationMs': 1.0,
                'durationEditable': False,
            },
        ],
        'modes': {
            'resolft': {
                'label': 'RESOLFT',
                'enabledSteps': [
                    'activation', 'green_off', 'green_readout',
                    'red_off', 'red_readout',
                ],
            },
            'confocal': {
                'label': 'Confocal',
                'enabledSteps': ['activation', 'green_readout', 'red_readout'],
            },
            'green': {
                'label': 'Green only',
                'enabledSteps': ['activation', 'green_off', 'green_readout'],
            },
        },
    })


def test_dual_color_profile_compiles_repeated_roles_and_reused_devices():
    profile = _dual_color_profile()
    compiled = compile_profile_sequence(
        profile,
        'resolft',
        {'green_off': 0.003, 'red_off': 0.004},
        inter_pulse_delay_s=DELAY_S,
        sample_rate=SAMPLE_RATE,
    )

    assert compiled.active_step_ids == (
        'activation', 'green_off', 'green_readout', 'red_off', 'red_readout'
    )
    assert compiled.step_windows_s['activation'] == pytest.approx((0.0001, 0.0011))
    assert compiled.step_windows_s['green_off'] == pytest.approx((0.0012, 0.0042))
    assert compiled.step_windows_s['green_readout'] == pytest.approx((0.0043, 0.0053))
    assert compiled.step_windows_s['red_off'] == pytest.approx((0.0054, 0.0094))
    assert compiled.step_windows_s['red_readout'] == pytest.approx((0.0095, 0.0105))
    assert len(compiled.windows_by_device['488']) == 2
    assert len(compiled.windows_by_device['590']) == 2
    assert len(compiled.windows_by_device['Camera']) == 2

    digital = _digital_from_compiled(compiled, ('405', '488', '590', 'Camera'))
    parsed = parse_profile_sequence(
        digital,
        profile,
        'resolft',
        inter_pulse_delay_s=DELAY_S,
        sample_rate=SAMPLE_RATE,
    )
    assert parsed.step_durations_s['green_off'] == pytest.approx(0.003)
    assert parsed.step_durations_s['red_off'] == pytest.approx(0.004)
    assert parsed.end_time_s == pytest.approx(0.0105)


def test_mode_is_subset_but_keeps_profile_step_order():
    profile = _dual_color_profile()
    compiled = compile_profile_sequence(
        profile,
        'confocal',
        {},
        inter_pulse_delay_s=DELAY_S,
        sample_rate=SAMPLE_RATE,
    )
    assert compiled.active_step_ids == ('activation', 'green_readout', 'red_readout')
    assert compiled.step_windows_s['activation'] == pytest.approx((0.0001, 0.0011))
    assert compiled.step_windows_s['green_readout'] == pytest.approx((0.0012, 0.0022))
    assert compiled.step_windows_s['red_readout'] == pytest.approx((0.0023, 0.0033))


def test_one_step_can_drive_two_off_lasers_synchronously():
    profile = MoNaLISAProfileDefinition.from_config('dual_off', {
        'defaultMode': 'resolft',
        'steps': [
            {
                'id': 'on', 'role': 'on', 'ttlDevices': ['405'],
                'defaultDurationMs': 1,
            },
            {
                'id': 'off', 'role': 'off', 'ttlDevices': ['488-A', '488-B'],
                'defaultDurationMs': 2,
            },
            {
                'id': 'readout', 'role': 'readout', 'ttlDevices': ['488-RO', 'Camera'],
                'defaultDurationMs': 1, 'durationEditable': False,
            },
        ],
        'modes': {
            'resolft': {'enabledSteps': ['on', 'off', 'readout']},
        },
    })
    compiled = compile_profile_sequence(
        profile, 'resolft', {'off': 0.003},
        inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    assert compiled.windows_by_device['488-A'] == compiled.windows_by_device['488-B']

    digital = _digital_from_compiled(
        compiled, ('405', '488-A', '488-B', '488-RO', 'Camera')
    )
    parsed = parse_profile_sequence(
        digital, profile, 'resolft',
        inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    assert parsed.step_durations_s['off'] == pytest.approx(0.003)


def test_parser_rejects_extra_window_instead_of_guessing_another_sequence():
    profile = _dual_color_profile()
    compiled = compile_profile_sequence(
        profile, 'green', {}, inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    digital = _digital_from_compiled(compiled, ('405', '488', '590', 'Camera'))
    digital['target_device'].append('590')
    digital['linestep_enable']['590'] = [True]
    digital['pulse_starts_s']['590'] = [[0.006]]
    digital['pulse_ends_s']['590'] = [[0.007]]

    with pytest.raises(MoNaLISASequenceNotCompatible, match='outside the selected profile/mode'):
        parse_profile_sequence(
            digital, profile, 'green',
            inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
        )


def test_profile_roles_include_custom_and_may_repeat():
    custom = MoNaLISAProfileDefinition.from_config('alignment', {
        'steps': [
            {
                'id': 'alignment', 'role': 'custom',
                'ttlDevices': ['405', '488-OFF', '488-RO', 'Camera'],
                'defaultDurationMs': 2,
            },
        ],
        'modes': {'all': {'enabledSteps': ['alignment']}},
    })
    assert custom.step('alignment').role == 'custom'

    with pytest.raises(MoNaLISAProfileError, match='role must be one of'):
        MoNaLISAProfileDefinition.from_config('bad', {
            'steps': [
                {
                    'id': 'thing', 'role': 'unknown', 'ttlDevices': ['405'],
                    'defaultDurationMs': 1,
                },
            ],
            'modes': {'run': {'enabledSteps': ['thing']}},
        })

    # Repeated OFF/READOUT roles are explicitly valid.
    assert len(_dual_color_profile().steps_with_role('off')) == 2
    assert len(_dual_color_profile().steps_with_role('readout')) == 2


def test_mode_can_enable_only_some_devices_inside_one_step():
    profile = MoNaLISAProfileDefinition.from_config('alignment', {
        'label': 'Alignment',
        'defaultMode': 'off_only',
        'steps': [
            {
                'id': 'alignment', 'label': 'Alignment pulse', 'role': 'custom',
                'ttlDevices': ['405', '488-OFF', '488-RO', 'Camera'],
                'defaultDurationMs': 2,
            },
        ],
        'modes': {
            'all': {'enabledSteps': ['alignment']},
            'off_only': {
                'enabledSteps': ['alignment'],
                'enabledDevices': {'alignment': ['488-OFF', 'Camera']},
            },
            'readout_only': {
                'enabledSteps': ['alignment'],
                'enabledDevices': {'alignment': ['488-RO', 'Camera']},
            },
        },
    })

    compiled = compile_profile_sequence(
        profile, 'off_only', {},
        inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    assert set(compiled.windows_by_device) == {'488-OFF', 'Camera'}
    assert compiled.windows_by_device['488-OFF'] == compiled.windows_by_device['Camera']

    digital = _digital_from_compiled(
        compiled, ('405', '488-OFF', '488-RO', 'Camera')
    )
    parsed = parse_profile_sequence(
        digital, profile, 'off_only',
        inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    assert parsed.active_step_ids == ('alignment',)
    assert parsed.step_durations_s['alignment'] == pytest.approx(0.002)

    # The whole-step shorthand remains equivalent to enabling every device.
    all_compiled = compile_profile_sequence(
        profile, 'all', {}, inter_pulse_delay_s=DELAY_S, sample_rate=SAMPLE_RATE,
    )
    assert set(all_compiled.windows_by_device) == {'405', '488-OFF', '488-RO', 'Camera'}


def test_mode_device_override_must_be_subset_of_enabled_step_devices():
    with pytest.raises(MoNaLISAProfileError, match='devices not in the step'):
        MoNaLISAProfileDefinition.from_config('bad_mode', {
            'steps': [
                {
                    'id': 'alignment', 'role': 'custom',
                    'ttlDevices': ['405', 'Camera'], 'defaultDurationMs': 1,
                },
            ],
            'modes': {
                'bad': {
                    'enabledSteps': ['alignment'],
                    'enabledDevices': {'alignment': ['590']},
                },
            },
        })


def test_profile_store_uses_only_explicit_profiles_and_allows_empty_state():
    profile = _dual_color_profile()
    store = MoNaLISAProfileStore.from_config({
        'defaultProfile': 'dual_color',
        'profiles': {'dual_color': profile.to_config()},
    })
    assert store.profile().id == 'dual_color'

    empty = MoNaLISAProfileStore.from_config({})
    assert empty.profiles == {}
    assert empty.default_profile == ''
    with pytest.raises(MoNaLISAProfileError, match='No MoNaLISA profile'):
        empty.profile()


def test_profile_binding_tracks_explicit_profile_and_mode_identity():
    from imswitch.imcontrol.model.monalisa_profiles import (
        MONALISA_BINDING_BOUND,
        MONALISA_BINDING_INCOMPATIBLE,
        MoNaLISAProfileBinding,
    )

    unbound = MoNaLISAProfileBinding.unbound()
    assert not unbound.is_bound
    assert unbound.profile_id is None

    profile = _dual_color_profile()
    binding = MoNaLISAProfileBinding.bound(profile, 'resolft')
    assert binding.status == MONALISA_BINDING_BOUND
    assert binding.profile_id == 'dual_color'
    assert binding.mode_id == 'resolft'
    assert binding.profile_fingerprint

    broken = binding.incompatible('extra TTL window')
    assert broken.status == MONALISA_BINDING_INCOMPATIBLE
    assert broken.profile_id == binding.profile_id
    assert broken.mode_id == binding.mode_id
    assert broken.profile_fingerprint == binding.profile_fingerprint
    assert broken.reason == 'extra TTL window'


def test_profile_store_live_mutations_are_atomic_and_keep_stable_ids():
    from dataclasses import replace
    from imswitch.imcontrol.model.monalisa_profiles import (
        config_with_profile_store,
        profile_id_from_label,
    )

    profile = _dual_color_profile()
    store = MoNaLISAProfileStore.from_config({
        'defaultProfile': 'dual_color',
        'profiles': {'dual_color': profile.to_config()},
    })

    duplicate_id = profile_id_from_label('Dual colour copy', store.profiles)
    duplicate = replace(profile, id=duplicate_id, label='Dual colour copy')
    store = store.with_profile(duplicate)
    assert set(store.profiles) == {'dual_color', 'dual_colour_copy'}

    renamed = store.relabel_profile('dual_color', 'Two-colour RESOLFT')
    assert renamed.profile('dual_color').label == 'Two-colour RESOLFT'
    assert renamed.profile('dual_color').id == 'dual_color'

    deleted = renamed.without_profile('dual_color')
    assert deleted.default_profile == 'dual_colour_copy'
    assert tuple(deleted.profiles) == ('dual_colour_copy',)

    merged = config_with_profile_store(
        {
            'fovUm': [40, 40], 'interPulseDelayMs': 0.1, 'legacyThing': 7,
            'onLaser': '405', 'offLaser': '488', 'readoutLaser': '488',
            'readoutDetector': 'Camera', 'defaultOnTimeMs': 1,
        },
        deleted,
    )
    assert merged['fovUm'] == [40, 40]
    assert merged['legacyThing'] == 7
    assert merged['defaultProfile'] == 'dual_colour_copy'
    assert 'dual_colour_copy' in merged['profiles']
    assert 'onLaser' not in merged
    assert 'offLaser' not in merged
    assert 'readoutLaser' not in merged
    assert 'readoutDetector' not in merged
    assert 'defaultOnTimeMs' not in merged


def test_profile_store_allows_deleting_last_profile():
    profile = _dual_color_profile()
    store = MoNaLISAProfileStore.from_config({
        'defaultProfile': 'dual_color',
        'profiles': {'dual_color': profile.to_config()},
    })
    empty = store.without_profile('dual_color')
    assert empty.profiles == {}
    assert empty.default_profile == ''
    assert empty.to_config() == {'profiles': {}}


def test_new_profile_ids_are_unique_but_rename_does_not_change_identity():
    from imswitch.imcontrol.model.monalisa_profiles import profile_id_from_label

    assert profile_id_from_label('Green RESOLFT', ()) == 'green_resolft'
    assert profile_id_from_label('Green RESOLFT', ('green_resolft',)) == 'green_resolft_2'
