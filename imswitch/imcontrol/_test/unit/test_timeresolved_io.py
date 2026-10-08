"""timeresolved.io: products v2 round trips (HDF5 and NPZ), gate bounds
saved relative and resolved, version-1 files still loading, and the gate
presets the Lifetime widget and tutorial 12 share."""

from __future__ import annotations

import json

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved import (
    FORMAT_VERSION,
    GateSpec,
    TimeResolvedScanProducts,
    load_gate_preset,
    load_products,
    save_gate_preset,
    save_h5,
    save_npz,
    save_tiffs,
)

pytestmark = pytest.mark.nohardware


def _products():
    t = (np.arange(16) + 0.5) * 0.5
    cube = np.random.default_rng(0).poisson(20, size=(3, 4, 16)).astype(np.uint32)
    return TimeResolvedScanProducts(
        cube_counts=cube, cube_axes=('y', 'x', 'tcspc_bin'), t_axis_ns=t,
        intensity=cube.sum(-1).astype(np.float32),
        lifetime_ns=np.full((3, 4), 2.5, np.float32),
        gate_images={'early': cube[..., :4].sum(-1), 'late': cube[..., 4:].sum(-1)},
        decay_counts=cube.sum((0, 1)).astype(np.float32), global_tau_ns=2.5,
        metadata={'backend': 'test', 'detector_name': 'FLIM', 'peak_time_ns': 1.0,
                  'fit_method': 'moment', 'background_per_bin': 0.02,
                  'time_tagger': {'model': 'Time Tagger X (mock)', 'serial': 'MOCK-1',
                                  'is_mock': True, 'roles': {'photons': {'channel': -1}}}},
        is_final=True, tcspc_direction='reverse', background_rate_hz=2000.0,
        pileup_max=0.03, overflows=1, frames_accumulated=2,
        irf={'t_axis_ns': t, 'counts': np.exp(-t), 'peak_ns': 0.25, 'fwhm_ns': 0.35},
    )


GATES = (GateSpec('early', 0.5, 2.5, reference='peak'), GateSpec('late', 2.5, 8.0))


def test_h5_round_trip_keeps_version_2_fields_and_resolved_gates(tmp_path):
    h5py = pytest.importorskip('h5py')
    path = save_h5(_products(), tmp_path / 'p.h5', workflow_name='gated', gates=GATES)
    with h5py.File(path, 'r') as h5:
        assert h5.attrs['format_version'] == FORMAT_VERSION
        assert h5.attrs['tcspc_direction'] == 'reverse'
        assert h5.attrs['frames_accumulated'] == 2 and h5.attrs['overflows'] == 1
        early = h5['gates/early']
        assert early.attrs['reference'] == 'peak'
        assert (early.attrs['start_ns'], early.attrs['stop_ns']) == (0.5, 2.5)
        assert (early.attrs['resolved_start_ns'], early.attrs['resolved_stop_ns']) == (1.5, 3.5)
        late = h5['gates/late']
        assert late.attrs['resolved_start_ns'] == 2.5, 'absolute: resolved is itself'
        assert h5['time_resolved/cube_counts'].dtype == np.uint32
        assert h5['time_tagger'].attrs['model'] == 'Time Tagger X (mock)'
        assert h5['background'].attrs['rate_hz'] == 2000.0
        assert h5['irf'].attrs['fwhm_ns'] == 0.35

    loaded = load_products(path)
    assert loaded.format_version == FORMAT_VERSION
    assert loaded.tcspc_direction == 'reverse' and loaded.frames_accumulated == 2
    assert loaded.pileup_max == pytest.approx(0.03) and loaded.background_rate_hz == 2000.0
    np.testing.assert_array_equal(loaded.cube_counts, _products().cube_counts)
    assert set(loaded.gate_images) == {'early', 'late'}
    assert loaded.metadata['gates']['early']['resolved_stop_ns'] == 3.5
    assert loaded.metadata['time_tagger']['serial'] == 'MOCK-1'
    assert loaded.irf['peak_ns'] == 0.25 and loaded.lifetime_ns.shape == (3, 4)


def test_a_float_cube_is_stored_as_integer_counts(tmp_path):
    pytest.importorskip('h5py')
    products = _products()
    products.cube_counts = products.cube_counts.astype(np.float32) + 0.2
    loaded = load_products(save_h5(products, tmp_path / 'f.h5', workflow_name='x'))
    assert np.issubdtype(loaded.cube_counts.dtype, np.integer)


def test_npz_round_trip_and_tiffs(tmp_path):
    path = save_npz(_products(), tmp_path / 'p.npz')
    loaded = load_products(path)
    assert loaded.format_version == FORMAT_VERSION
    assert loaded.tcspc_direction == 'reverse' and loaded.overflows == 1
    assert set(loaded.gate_images) == {'early', 'late'}
    assert loaded.irf is None, 'the archive carries no IRF'
    paths = save_tiffs(_products(), tmp_path, 'p')
    assert {'intensity_tiff', 'lifetime_tiff', 'gate_early_tiff', 'gate_late_tiff'} <= set(paths)
    assert all(p.exists() for p in paths.values())


def test_a_version_1_file_loads_with_defaults(tmp_path):
    h5py = pytest.importorskip('h5py')
    path = tmp_path / 'v1.h5'
    with h5py.File(path, 'w') as h5:
        h5.attrs['metadata_json'] = json.dumps({'backend': 'old'})
        tr = h5.create_group('time_resolved')
        tr.create_dataset('t_axis_ns', data=np.arange(4.0))
        tr.create_dataset('intensity', data=np.ones((2, 2)))
        tr.create_dataset('decay_counts', data=np.ones(4))
        tr.attrs['global_tau_ns'] = 1.5
    loaded = load_products(path)
    assert loaded.format_version == 1 and loaded.tcspc_direction == 'forward'
    assert loaded.frames_accumulated == 1 and loaded.gate_images == {}
    assert loaded.cube_counts is None and loaded.global_tau_ns == 1.5


def test_gate_presets_round_trip_and_validate(tmp_path):
    path = save_gate_preset(tmp_path / 'sted.json', GATES, name='sted', ratio=('late', 'early'))
    preset = load_gate_preset(path)
    assert preset['name'] == 'sted' and preset['gates'] == GATES
    assert preset['ratio'] == ('late', 'early')
    (tmp_path / 'top.json').write_text(json.dumps({
        'reference': 'peak', 'gates': [{'name': 'g', 'start_ns': 1, 'stop_ns': 2}]}))
    assert load_gate_preset(tmp_path / 'top.json')['gates'][0].reference == 'peak'
    (tmp_path / 'bad.json').write_text(json.dumps({
        'gates': [{'name': 'g', 'start_ns': 1, 'stop_ns': 2}], 'ratio': ['g', 'nope']}))
    with pytest.raises(ValueError, match='ratio names'):
        load_gate_preset(tmp_path / 'bad.json')


def test_the_shipped_presets_load():
    from pathlib import Path
    import imswitch
    folder = Path(imswitch.__file__).parent / '_data' / 'user_defaults' / 'scripts' / 'tutorial' / 'timetagger' / 'gate_presets'
    names = sorted(p.stem for p in folder.glob('*.json'))
    assert names == ['detection_window', 'sted_early_late']
    sted = load_gate_preset(folder / 'sted_early_late.json')
    assert [g.name for g in sted['gates']] == ['early', 'late']
    assert all(g.reference == 'peak' for g in sted['gates'])
