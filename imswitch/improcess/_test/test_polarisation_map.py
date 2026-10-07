"""Polarisation-map reconstructor on synthetic measurement runs."""
import json
import math

import numpy as np
import pytest

from imswitch.imcommon.algorithms.polarisation import TwoPlateModel, angles_from_direction
from imswitch.imcommon.model.measurement_run import (
    AcquisitionOutcome,
    ControlResult,
    MeasurementRunFile,
    PointStatus,
    QuantitySpec,
    RunJournalWriter,
    Sample,
    Verification,
    WindowResult,
    finalize_journal,
)
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.dataset_sources import MEASUREMENT_RUN_SOURCE_KIND
from imswitch.improcess.reconstructors.polarisation_map import (
    PolarisationMapError,
    PolarisationMapReconstructor,
    analyse_run,
)

PAX = (
    QuantitySpec('azimuth', 'rad', 'polarisation.azimuth', -math.pi / 2, math.pi / 2),
    QuantitySpec('ellipticity', 'rad', 'polarisation.ellipticity', -math.pi / 4, math.pi / 4),
    QuantitySpec('dop', '', 'polarisation.dop', 0, 1.05),
    QuantitySpec('power', 'W', 'optical.power', 0),
)
MODEL = TwoPlateModel()  # QWP then HWP, horizontal input: reaches every state


def _sample(seq, direction, dop=0.99, verified=True):
    psi, chi = angles_from_direction(direction)
    return Sample(
        values={'azimuth': float(psi), 'ellipticity': float(chi), 'dop': dop, 'power': 1e-3},
        t_host=float(seq), generation=1, sequence=seq, device_id=seq,
        verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
        profile_id='mock')


def write_run(tmp_path, points, *, name='run', instruments=('pax1',), quantities=PAX,
              verified=True, dop=None, grid_axes=None, illumination=633.0,
              wavelength_setting=633.0, finish=True, status_for=None, to_units=False):
    """``points``: list of (angle1, angle2); samples from MODEL (2 per point)."""
    meta = {
        'run_id': name,
        'controls': [{'name': 'qwp', 'unit': 'deg', 'zero_reference': {'state': 'unknown'}},
                     {'name': 'hwp', 'unit': 'deg', 'zero_reference': {'state': 'unknown'}}],
        'instruments': {i: {'identity': {'model': 'PAX1000', 'serial': f'S-{i}'},
                            'settings': {'wavelength_nm': wavelength_setting}}
                        for i in instruments},
        'illumination': None if illumination is None else
        {'source': '633', 'wavelength_nm': illumination},
        'plane_label': 'sample',
        'created': '2026-10-07T00:00:00+00:00',
        'grid': grid_axes,
    }
    writer = RunJournalWriter(tmp_path / f'{name}.journal', metadata=meta,
                              instruments={i: quantities for i in instruments},
                              controls=['qwp', 'hwp'], fsync=False)
    seq = 0
    for index, (a1, a2) in enumerate(points):
        out = MODEL.output(a1, a2)
        direction = out[1:] / np.linalg.norm(out[1:])
        point_dop = 0.99 if dop is None else dop[index]
        windows = {}
        for inst in instruments:
            samples = []
            for _ in range(2):
                seq += 1
                samples.append(_sample(seq, direction, point_dop, verified))
            if to_units:
                factor = {'azimuth': 180 / math.pi, 'ellipticity': 180 / math.pi,
                          'dop': 100.0, 'power': 1.0}
                samples = [Sample(values={k: v * factor[k] for k, v in s.values.items()},
                                  t_host=s.t_host, generation=1, sequence=s.sequence,
                                  verification=s.verification, profile_id='mock')
                           for s in samples]
            elif quantities is not PAX:
                samples = [Sample(values={k: v for k, v in s.values.items()
                                          if k in {q.name for q in quantities}},
                                  t_host=s.t_host, generation=1, sequence=s.sequence,
                                  verification=s.verification, profile_id='mock')
                           for s in samples]
            windows[inst] = WindowResult(
                samples=tuple(samples), invalid=(), discarded=0, complete=True, cause=None,
                verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
                profile_id='mock', boundary_t=0.0, requested=2)
        status = PointStatus.COMMITTED if status_for is None else status_for(index)
        grid_index = None
        if grid_axes:
            n_inner = len(grid_axes['axes'][1]['values'])
            grid_index = (index // n_inner, index % n_inner)
        writer.commit_point(
            point=index, status=status, t_start=index, t_end=index + 0.1,
            controls=[ControlResult('qwp', a1, a1, a1), ControlResult('hwp', a2, a2, a2)],
            windows=windows, grid_index=grid_index)
    if finish:
        writer.finish(AcquisitionOutcome.COMPLETE)
    else:
        writer.close()
    return finalize_journal(tmp_path / f'{name}.journal')


def _grid(step):
    a = list(np.arange(0.0, 180.0, step))
    pts = [(q, h) for q in a for h in a]
    axes = {'axes': [{'name': 'qwp', 'values': a}, {'name': 'hwp', 'values': a}],
            'traversal': 'raster'}
    return pts, axes


def _params(**kw):
    p = PolarisationMapReconstructor.default_params()
    p.update(kw)
    return p


def test_dense_grid_reaches_every_target(tmp_path):
    pts, axes = _grid(3.0)
    run = MeasurementRunFile.load(write_run(tmp_path, pts, grid_axes=axes))
    result = analyse_run(run, _params())
    assert list(result.properties['status']) == ['pass'] * 20
    assert max(result.properties['distance (°)']) <= 5.0
    assert result.metadata['summary']['pass'] == 20
    # the reported angles really produce the target state
    for i, target in enumerate(result.properties['target']):
        q = result.properties['qwp (deg)'][i]
        h = result.properties['hwp (deg)'][i]
        out = MODEL.output(q, h)
        got = out[1:] / np.linalg.norm(out[1:])
        assert np.degrees(np.arccos(np.clip(got @ result.coordinates[i], -1, 1))) <= 5.0


def test_coarse_grid_fails_targets_it_does_not_reach(tmp_path):
    run = MeasurementRunFile.load(write_run(tmp_path, [(0, 0), (0, 22.5)]))
    result = analyse_run(run, _params())
    status = dict(zip(result.properties['target'], result.properties['status']))
    assert status['linear 0°'] == 'pass'
    assert status['linear 50°'] == 'failed' or status['linear 40°'] == 'failed'
    assert status['RCP'] == 'failed'


def test_unverified_runs_are_unqualified_never_pass(tmp_path):
    pts, axes = _grid(10.0)
    run = MeasurementRunFile.load(write_run(tmp_path, pts, grid_axes=axes, verified=False))
    result = analyse_run(run, _params())
    assert set(result.properties['status']) == {'unqualified'}
    assert result.metadata['summary']['unverified_points'] == len(pts)


def test_low_dop_points_are_not_eligible(tmp_path):
    """Nearest point with DOP 0.5 loses to a farther one with DOP 0.99."""
    pts = [(0.0, 0.0), (0.0, 0.5)]   # 0° and 2° from horizontal on the sphere
    run = MeasurementRunFile.load(write_run(tmp_path, pts, dop=[0.5, 0.99]))
    result = analyse_run(run, _params())
    i = list(result.properties['target']).index('linear 0°')
    assert result.properties['status'][i] == 'pass'
    assert result.properties['hwp (deg)'][i] == pytest.approx(0.5)
    assert result.properties['distance (°)'][i] == pytest.approx(2.0, abs=1e-6)


def test_only_committed_points_are_used(tmp_path):
    pts = [(0.0, 0.0), (45.0, 0.0)]
    path = write_run(tmp_path, pts, finish=False,
                     status_for=lambda i: PointStatus.COMMITTED if i == 0 else PointStatus.FAILED_PARTIAL)
    run = MeasurementRunFile.load(path)
    assert run.acquisition is AcquisitionOutcome.INTERRUPTED
    result = analyse_run(run, _params())
    status = dict(zip(result.properties['target'], result.properties['status']))
    assert status['RCP'] == 'failed'          # the circular point was not committed
    assert result.metadata['summary']['committed'] == 1


def test_two_polarimeters_must_be_chosen_explicitly(tmp_path):
    run = MeasurementRunFile.load(write_run(tmp_path, [(0, 0)], instruments=('paxA', 'paxB')))
    with pytest.raises(PolarisationMapError, match='several polarimeters'):
        analyse_run(run, _params())
    result = analyse_run(run, _params(instrument='paxB'))
    assert result.instrument == 'paxB'


def test_dop_is_required(tmp_path):
    no_dop = tuple(q for q in PAX if q.name != 'dop')
    run = MeasurementRunFile.load(write_run(tmp_path, [(0, 0)], quantities=no_dop))
    with pytest.raises(PolarisationMapError, match='DOP'):
        analyse_run(run, _params())


def test_handedness_convention_swaps_circular_targets(tmp_path):
    run = MeasurementRunFile.load(write_run(tmp_path, [(45.0, 0.0)]))
    positive = analyse_run(run, _params())
    negative = analyse_run(run, _params(convention='rcp = -s3'))
    s_pos = dict(zip(positive.properties['target'], positive.properties['status']))
    s_neg = dict(zip(negative.properties['target'], negative.properties['status']))
    assert {s_pos['RCP'], s_pos['LCP']} == {'pass', 'failed'}
    assert s_pos['RCP'] == s_neg['LCP']


def test_export_document_and_its_refusals(tmp_path):
    pts, axes = _grid(10.0)
    run = MeasurementRunFile.load(write_run(tmp_path, pts, grid_axes=axes))
    export = analyse_run(run, _params()).export_document()
    assert export['format'] == 'imswitch-polarisation-states'
    assert export['illumination']['wavelength_nm'] == 633.0
    assert export['instrument']['identity']['serial'] == 'S-pax1'
    assert {c['name'] for c in export['controls']} == {'qwp', 'hwp'}
    assert export['controls'][0]['zero_reference']['state'] == 'unknown'
    rcp = next(t for t in export['targets'] if t['target'] == 'RCP')
    assert set(rcp['settings']) == {'qwp', 'hwp'}
    json.dumps(export)  # serialisable

    no_light = MeasurementRunFile.load(write_run(tmp_path, pts, name='b', illumination=None))
    with pytest.raises(PolarisationMapError, match='no illumination'):
        analyse_run(no_light, _params()).export_document()
    mismatch = MeasurementRunFile.load(write_run(tmp_path, pts, name='c', wavelength_setting=532.0))
    with pytest.raises(PolarisationMapError, match='532'):
        analyse_run(mismatch, _params()).export_document()


def test_display_layers(tmp_path):
    pts, axes = _grid(30.0)   # 6 x 6 grid
    run = MeasurementRunFile.load(write_run(tmp_path, pts, grid_axes=axes))
    layers = {spec.component: spec for spec in analyse_run(run, _params()).display_layers()}
    assert set(layers) == {'sphere', 'axes', 'measured', 'lines', 'targets'}
    assert layers['measured'].kind == 'points' and layers['measured'].role == 'primary'
    assert np.asarray(layers['measured'].data).shape == (36, 3)
    assert np.asarray(layers['lines'].data).shape == (6, 6, 3)
    assert np.asarray(layers['targets'].data).shape == (20, 3)
    assert all((spec.metadata or {}).get('ndisplay') == 3 for spec in layers.values())
    sphere = np.asarray(layers['sphere'].data)
    np.testing.assert_allclose(np.linalg.norm(sphere, axis=-1), 1.0, atol=1e-12)


def test_reconstructor_on_a_metadata_source(tmp_path):
    path = write_run(tmp_path, [(0, 0), (45, 0)])
    run = MeasurementRunFile.load(path)
    data_obj = DataObj.fromMetadataSource(path.name, path, MEASUREMENT_RUN_SOURCE_KIND, run)
    assert data_obj.sourceKind in PolarisationMapReconstructor.accepted_source_kinds
    result = PolarisationMapReconstructor().process(data_obj, {})
    assert result.kind == 'table'
    assert len(result.table_records()) == 20


def test_workflow_source_opens_run_files(tmp_path):
    from imswitch.improcess.workflows.sources import SourceSpec, open_source

    path = write_run(tmp_path, [(0, 0)])
    data_obj = open_source(SourceSpec(path=str(path)))
    assert data_obj.sourceKind == MEASUREMENT_RUN_SOURCE_KIND
    assert isinstance(data_obj.sourceMetadata, MeasurementRunFile)


def test_registered_as_a_built_in():
    from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES

    assert _AVAILABLE_RECONSTRUCTOR_CLASSES['polarisation-map'] is PolarisationMapReconstructor


def test_json_save_writes_the_export(tmp_path):
    from imswitch.improcess.model.save_protocol import SavePlan

    pts, axes = _grid(30.0)
    result = analyse_run(MeasurementRunFile.load(write_run(tmp_path, pts, grid_axes=axes)), _params())
    plan = result.plan_save(tmp_path / 'states.json', 'json')
    result.write_files(plan, {})
    data = json.loads((tmp_path / 'states.json').read_text())
    assert data['run_id'] == 'run' and len(data['targets']) == 20


def test_angles_in_degrees_are_converted_and_unknown_units_refused(tmp_path):
    """Review: units were ignored; degrees would have been read as radians."""
    pts, axes = _grid(30.0)
    reference = analyse_run(MeasurementRunFile.load(
        write_run(tmp_path, pts, grid_axes=axes, name='rad')), _params())

    degrees = (
        QuantitySpec('azimuth', 'deg', 'polarisation.azimuth', -90, 90),
        QuantitySpec('ellipticity', 'deg', 'polarisation.ellipticity', -45, 45),
        QuantitySpec('dop', '%', 'polarisation.dop', 0, 105),
        QuantitySpec('power', 'W', 'optical.power', 0),
    )
    path = write_run(tmp_path, pts, grid_axes=axes, name='deg', quantities=degrees,
                     to_units=True)
    converted = analyse_run(MeasurementRunFile.load(path), _params())
    np.testing.assert_allclose(converted.properties['distance (°)'],
                               reference.properties['distance (°)'], atol=1e-9)
    assert list(converted.properties['status']) == list(reference.properties['status'])

    grads = tuple(QuantitySpec(q.name, 'grad', q.quantity) if q.name == 'azimuth' else q
                  for q in degrees)
    bad = MeasurementRunFile.load(write_run(tmp_path, pts, name='grad', quantities=grads))
    with pytest.raises(PolarisationMapError, match="'grad'"):
        analyse_run(bad, _params())
