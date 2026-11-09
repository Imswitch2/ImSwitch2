"""Patch 3: stable Local footprint defaults and standalone width calibration."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.params import (
    FOOTPRINT_FROM_LATTICE, FOOTPRINT_FIXED, PRESET_FULL, apply_preset, default_params,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry
from imswitch.improcess.reconstructors.monalisa.spot_calibration import estimate_shared_width
from imswitch.improcess.reconstructors.monalisa.setup_defaults import (
    save_detector_footprint, read_detector_defaults,
)
from imswitch.improcess._test._monalisa_synthetic import render_spots


def test_default_from_lattice_and_full_does_not_enable_variance_weighting():
    normal = default_params()
    full = apply_preset(normal, PRESET_FULL)
    assert normal['local_footprint_mode'] == FOOTPRINT_FROM_LATTICE
    assert normal['local_footprint_shape'] == 'square'
    assert full['variance_weighting'] is False


@pytest.mark.parametrize('shape,expected', [('square', 9), ('circle', 11)])
def test_lattice_sizing_is_resolved_from_current_geometry(shape, expected):
    loc = PatternLocalization.manual_grid(12, 10, 3, 4, stagger_axis='x', stagger=0.5)
    param = PipelineParams(
        estimator='local', spot_sigma_px=1.6, background='constant',
        variance_weighting=False, reassignment='off', frame_gain='off',
        local_footprint_mode=FOOTPRINT_FROM_LATTICE,
        local_footprint_shape=shape, local_footprint_size=5,
    )
    geo = prepare_geometry(np.zeros((48, 48)), np.eye(2), np.array([[0, 0]]), param, localization=loc)
    assert geo.diagnostics['local_footprint_mode'] == FOOTPRINT_FROM_LATTICE
    assert geo.diagnostics['local_footprint_size'] == expected


def test_fixed_footprint_preserves_explicit_pixel_size():
    loc = PatternLocalization.manual_grid(11, 11, 3, 4)
    param = PipelineParams(
        estimator='local', spot_sigma_px=1.6, variance_weighting=False,
        reassignment='off', frame_gain='off', local_footprint_mode=FOOTPRINT_FIXED,
        local_footprint_size=5,
    )
    geo = prepare_geometry(np.zeros((48, 48)), np.eye(2), np.array([[0, 0]]), param, localization=loc)
    assert geo.diagnostics['local_footprint_size'] == 5


def test_save_footprint_as_one_atomic_setup_preference(tmp_path):
    import json
    path = tmp_path / 'hardware.json'
    path.write_text(json.dumps({'detectors': {'CAM': {'managerProperties': {'keep': True}}}}))
    save_detector_footprint('CAM', 'from_lattice', 'circle', 9, path=path)
    loaded = read_detector_defaults('CAM', path=path)
    assert (loaded.footprint_mode, loaded.footprint_shape, loaded.footprint_size) == (
        'from_lattice', 'circle', 9
    )
    assert json.loads(path.read_text())['detectors']['CAM']['managerProperties']['keep']
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Invalid'):
        save_detector_footprint('CAM', 'from_lattice', 'hexagon', 8, path=path)
    assert path.read_bytes() == before


def test_pre_reconstruction_estimate_uses_selected_localization_without_changing_it():
    loc = PatternLocalization.manual_grid(15, 15, 5, 5)
    x, y = loc.points_in_frame(80, 80)
    spots = render_spots((80, 80), x, y, 1.8, np.full(x.size, 250.0))
    frames = np.stack([30 + factor * spots for factor in (0.2, 0.8, 1.3, 0.5)])
    sigma, status, new_loc = estimate_shared_width(frames, localization=loc)
    assert np.isfinite(sigma)
    assert sigma == pytest.approx(1.8, abs=0.18)
    assert 'selected foci' in status
    assert new_loc is None


def test_pre_reconstruction_estimate_rejects_uninformative_single_frame():
    with pytest.raises(ValueError, match='at least two frames'):
        estimate_shared_width(np.ones((1, 40, 40)))
