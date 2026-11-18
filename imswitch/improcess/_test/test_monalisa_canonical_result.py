"""M2D-B canonical MoNaLISA result/compatibility contracts.

Runs in the normal ImSwitch Qt test environment. No GPU, camera or SDK needed.
"""

from __future__ import annotations

import pickle

import numpy as np
import pytest

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.reconstructors.monalisa.canonical_result import (
    CLASSIC, FLEXIBLE, MonalisaResult, is_flexible_monalisa_result,
)
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult
from imswitch.improcess.reconstructors.monalisa_lattice.result import MonalisaLatticeResult


SCAN = {
    'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
    'directions': ['pos', 'pos', 'pos'],
    'steps': ['2', '3', '1', '1'],
    'step_sizes': ['35', '40', '100', '1'],
    'unidirectional': True,
}


def classic(*, bases=2):
    return MonalisaProcessingResult(
        name='classic', data=np.zeros((1, bases, 1, 1, 3, 2), np.float32),
        scan_params=dict(SCAN), output_pixel_size_nm=(40.0, 35.0),
    )


def flexible(**changes):
    params = {
        'name': 'full', 'image': np.ones((1, 2, 3, 4), np.float32),
        'output_pixel_nm': (40.0, 35.0), 'axis_labels': ['T', 'Z', 'Y', 'X'],
        'params': {'corrections': 'on'},
        'diagnostics': {'foci': 15},
        'sharpened': np.ones((1, 2, 3, 4), np.float32) * 2,
        'background': np.ones((1, 2, 3, 4), np.float32) * .5,
    }
    params.update(changes)
    return MonalisaLatticeResult(**params)


def test_both_existing_constructors_produce_one_canonical_result_class():
    a, b = classic(), flexible()
    assert type(a) is type(b) is MonalisaResult
    assert isinstance(a, ProcessingResult) and isinstance(b, ProcessingResult)
    assert MonalisaProcessingResult is MonalisaLatticeResult is MonalisaResult
    assert a.monalisa_representation == CLASSIC
    assert b.monalisa_representation == FLEXIBLE
    assert not is_flexible_monalisa_result(a)
    assert is_flexible_monalisa_result(b)
    assert a.kind == b.kind == 'image'
    assert a.metadata['monalisa_result']['representation'] == CLASSIC
    assert b.metadata['monalisa_result']['representation'] == FLEXIBLE


def test_classic_keeps_six_axes_and_independently_displayed_bases():
    result = classic()
    assert result.axis_labels == ['Dataset', 'Base', 'T', 'Z', 'Y', 'X']
    assert len(result.display_layers()) == 2
    assert [part.shape for part in result.display_layer_data()] == [(1, 1, 1, 3, 2)] * 2
    view = result.serialization_view()
    assert view.axis_labels == ['T', 'Z', 'C', 'Y', 'X']
    assert view.data.shape == (1, 1, 2, 3, 2)
    assert result.supported_formats == ('tiff', 'hdf5', 'zarr', 'imagej')
    assert result.getCoeffs() is None
    assert result.getScanParams() == SCAN


def test_flexible_keeps_named_axes_layers_metrics_and_scales():
    result = flexible()
    assert result.axis_labels == ['T', 'Z', 'Y', 'X']
    assert [layer.metadata['component'] for layer in result.display_layers()] == [
        'reconstruction', 'sharpened', 'background',
    ]
    assert result.table_records() == [{'result': 'full', 'quantity': 'foci', 'value': 15}]
    assert result.initial_view_mode_name() == 'Standard'
    assert result.serialization_view().axis_labels == ['T', 'Z', 'Y', 'X']
    assert result.publishes_table_rows
    result.updateScanParams({'ignored': True})  # Legacy-only action is a no-op.
    result.updateImages()
    assert result.params == {'corrections': 'on'}


def test_flexible_pinhole_layer_keeps_its_pinhole_axis():
    result = flexible(pinholes=np.ones((1, 2, 7, 3, 4), np.float32),
                      pinhole_offsets=(np.arange(7), np.zeros(7, int)))
    assert result.display_layers()[-1].axis_labels == ['T', 'Z', 'Pinhole', 'Y', 'X']
    assert result.display_layer_data()[-1].shape == (1, 2, 7, 3, 4)


def test_scientific_axis_metadata_does_not_infer_time_from_shape():
    result = classic()
    result.acquisition_projection = {
        'axis': 'T', 'components': ('time', 'condition'),
        'n_time': 1, 'n_conditions': 2,
    }
    assert 'condition' in result.scientific_axis_metadata['acquisition_projection']['components']
    flex = flexible(condition_labels=('A', 'B'))
    assert flex.scientific_axis_metadata['condition_labels'] == ('A', 'B')


def test_old_import_paths_are_valid_pickle_lookup_aliases():
    import imswitch.improcess.reconstructors.monalisa.result as old_classic
    import imswitch.improcess.reconstructors.monalisa_lattice.result as old_flex
    assert old_classic.MonalisaProcessingResult is MonalisaResult
    assert old_flex.MonalisaLatticeResult is MonalisaResult
    result = pickle.loads(pickle.dumps(classic()))
    assert type(result) is MonalisaResult
    assert result.serialization_view().data.shape == (1, 1, 2, 3, 2)


def test_saved_old_state_without_marker_is_still_interpretable():
    result = classic()
    del result._monalisa_representation
    assert result.monalisa_representation == CLASSIC
    assert len(result.display_layers()) == 2
    result = flexible()
    del result._monalisa_representation
    assert result.monalisa_representation == FLEXIBLE
    assert len(result.display_layers()) == 3


def test_classic_coefficients_rerender_through_adapter(monkeypatch):
    from imswitch.improcess.reconstructors.monalisa import result as old
    calls = []
    monkeypatch.setattr(old, 'reconstruct_images_from_coeffs', lambda coeffs, scan, labels, placement=None: (
        calls.append(placement) or np.ones((1, 1, 1, 1, 3, 2), np.float32)
    ))
    monkeypatch.setattr(old, 'output_pixel_size_nm', lambda scan, labels: (40., 35.))
    result = MonalisaResult.from_coeffs('classic', np.ones((1, 1, 6, 1, 1)),
                                        dict(SCAN), dict(old.DEFAULT_AXIS_LABEL_MAP))
    assert len(calls) == 1
    result.updateImages()
    assert len(calls) == 2
    np.testing.assert_array_equal(result.data, 1.)
    assert result.getCoeffs().shape == (1, 1, 6, 1, 1)
    assert result.axis_scales[-2:] == [40., 35.]

@pytest.mark.parametrize('old_module, public_name, private_name, kind', [
    ('imswitch.improcess.reconstructors.monalisa.result',
     'MonalisaProcessingResult', '_ClassicMonalisaResultAdapter', CLASSIC),
    ('imswitch.improcess.reconstructors.monalisa_lattice.result',
     'MonalisaLatticeResult', '_FlexibleMonalisaResultAdapter', FLEXIBLE),
])
def test_actual_old_class_pickles_resolve_through_compatibility_alias(
        monkeypatch, old_module, public_name, private_name, kind):
    """Simulate bytes from before M2D-B, not just a current-class roundtrip."""
    from importlib import import_module
    module = import_module(old_module)
    old_class = getattr(module, private_name)
    with monkeypatch.context() as patch:
        patch.setattr(module, public_name, old_class)
        if kind == CLASSIC:
            old = old_class('classic', np.zeros((1, 1, 1, 1, 3, 2), np.float32), dict(SCAN))
        else:
            old = old_class('flexible', np.ones((3, 4), np.float32),
                            output_pixel_nm=(40., 35.), params={}, diagnostics={'foci': 5})
        payload = pickle.dumps(old)
    # The historical public symbol now points to the *canonical* class.
    result = pickle.loads(payload)
    assert type(result) is MonalisaResult
    assert result.monalisa_representation == kind
    assert result.display_layers() is not None
    if kind == CLASSIC:
        assert 'imagej' in result.supported_formats
        assert result.publishes_table_rows is False
    else:
        assert 'imagej' not in result.supported_formats
        assert result.publishes_table_rows is True
