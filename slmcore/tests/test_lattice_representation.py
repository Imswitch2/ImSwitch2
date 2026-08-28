import numpy as np

from slmcore.core.cgh.lattice_geometry import LatticeRepresentation
from slmcore.core.cgh.localization import (
    infer_lattice_shape,
    localize_lattice,
    make_lattice_model,
    register_lattice,
)
from slmcore.core.cgh.localization.model import DetectedSpots,LatticeRegistrationOptions
from slmcore.core.cgh.targets.lattice import LatticeDefinition


def _indices(nx,ny):
    i,j = np.meshgrid(np.arange(nx),np.arange(ny),indexing="xy")
    return np.vstack([i.ravel(),j.ravel()])


def _detections_for_stagger(stagger,nx=17,ny=15):
    indices = _indices(nx,ny)
    representation = LatticeRepresentation.alternating_rows(indices,stagger)
    logical = representation.centered_logical_positions
    linear = np.array([[10.7,1.8],[-0.9,11.4]],dtype=np.float64)
    translation = np.array([130.0,120.0],dtype=np.float64)
    points = linear.dot(logical) + translation[:,None]
    rng = np.random.default_rng(2)
    points = points + rng.normal(scale=0.05,size=points.shape)
    count = points.shape[1]
    image = np.zeros((32,32),dtype=np.float64)
    return indices,DetectedSpots(
        positions_px=points,
        intensities=np.ones(count,dtype=np.float64),
        scores=np.ones(count,dtype=np.float64),
        cropped_image=image,
        processed_image=image,
        crop_coord=(0,32,0,32),
    )


def test_canonical_representation_preserves_public_stagger_positions():
    indices = _indices(7,6)
    for stagger in (0.0,0.2,0.5,0.9,1.0):
        representation = LatticeRepresentation.alternating_rows(indices,stagger)
        expected = indices.astype(np.float64)
        expected[0] += (indices[1] % 2) * stagger
        assert np.allclose(representation.logical_positions,expected)
        canonical = float(representation.diagnostics["canonical_stagger"])
        assert -0.5 <= canonical <= 0.5


def test_lattice_definition_positions_are_unchanged_by_representation():
    lattice = LatticeDefinition(
        period_x_px=6.0,period_y_px=8.0,
        n_foci_x=7,n_foci_y=6,
        rotation_deg=13.0,skew_deg=7.0,stagger=0.9,
    )
    indices = lattice.lattice_indices()
    centered_i = indices[0] - (lattice.n_foci_x-1)/2.0
    centered_j = indices[1] - (lattice.n_foci_y-1)/2.0
    period_x,period_y = lattice.period_kxy
    skew = np.deg2rad(lattice.skew_deg)
    x = centered_i*period_x + centered_j*period_y*np.sin(skew)
    x += (indices[1] % 2) * lattice.stagger * period_x
    y = centered_j*period_y*np.cos(skew)
    theta = np.deg2rad(lattice.rotation_deg)
    expected = np.vstack([
        np.cos(theta)*x - np.sin(theta)*y,
        np.sin(theta)*x + np.cos(theta)*y,
    ])
    assert np.allclose(lattice.spot_positions_kxy(),expected,atol=1e-14)


def test_unknown_stagger_is_inferred_before_registration():
    indices,detections = _detections_for_stagger(0.27)
    unresolved = make_lattice_model(indices,stagger=None)
    result = register_lattice(
        detections,unresolved,
        LatticeRegistrationOptions(lattice_candidate_search_mode="fast"),
    )
    assert result.matched_count == indices.shape[1]
    assert result.diagnostics["search_path"] == "image_inferred_lattice"
    assert result.diagnostics["stagger_source"] == "image"
    assert abs(float(result.diagnostics["resolved_stagger"])-0.27) < 0.03
    assert result.rms_residual_px < 0.2


def test_lattice_size_is_inferred_from_image_rows():
    _indices_value,detections = _detections_for_stagger(0.27,nx=17,ny=15)
    shape,diagnostics = infer_lattice_shape(detections)
    assert shape == (17,15)
    assert diagnostics["source"] == "image"


def test_auto_size_and_auto_stagger_localize_without_target_geometry():
    nx,ny = 13,11
    indices = _indices(nx,ny)
    representation = LatticeRepresentation.alternating_rows(indices,0.5)
    logical = representation.centered_logical_positions
    linear = np.array([[9.2,1.1],[0.5,10.0]],dtype=np.float64)
    points = linear.dot(logical) + np.array([[80.0],[75.0]])
    image = np.zeros((160,170),dtype=np.float64)
    from scipy.ndimage import gaussian_filter
    for x,y in points.T:
        image[int(round(y)),int(round(x))] += 1.0
    image = gaussian_filter(image,0.9)

    result = localize_lattice(
        image,
        None,
        stagger=None,
        registration_options=LatticeRegistrationOptions(
            lattice_candidate_search_mode="fast",
        ),
    )
    assert result.model.lattice_indices.shape[1] == nx*ny
    assert int(np.unique(result.model.lattice_indices[0]).size) == nx
    assert int(np.unique(result.model.lattice_indices[1]).size) == ny
    assert result.matched_count == nx*ny
    assert abs(float(result.diagnostics["resolved_stagger"])-0.5) < 0.05
