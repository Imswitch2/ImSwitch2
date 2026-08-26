from __future__ import annotations

import numpy as np

from slmcore.cgh.targets.lattice import rasterize_spots
from slmcore.cgh.targets.multi_foci import MultiFociTarget
from slmcore.cgh.targets.multi_foci_vector import MultiFociVectorTarget
from slmcore.cgh.execution import session as session_module
from slmcore.engine.section.context import SectionContext
from slmcore.engine.section.geometry import SectionGeometry


def _context():
    return SectionContext(
        geometry=SectionGeometry(
            key="sec_0",x=0,y=0,width=64,height=64,
        ),
        pixel_size_um=1.0,
        wavelength_nm=488,
        pupil_radius_px=32,
        center_offset_x_px=0,
        center_offset_y_px=0,
    )


def _target(target_class,**overrides):
    context = _context()
    params = {
        key:spec.default
        for key,spec in target_class.target_params.items()
    }
    params.update({
        "square":False,
        "period_x_px":32.0,
        "period_y_px":32.0,
        "fov_x_px":32.0,
        "fov_y_px":32.0,
        "n_foci_x":2,
        "n_foci_y":2,
    })
    params.update(overrides)
    canonical,prepared = target_class.canonicalize_params(
        params,tuple(params),context=context,
    )
    return target_class(
        context=context,
        prepared_definition=prepared,
        **canonical,
    )


def test_multi_foci_target_intensity_update_regenerates_raster_array():
    target = _target(MultiFociTarget)
    base = target.resolution
    intensities = np.array([1.0,0.5,0.25,0.75],dtype=np.float64)

    assert target.signature == target.definition_signature_for(
        target.context,target.params,
    )
    assert base.target_signature == target.signature

    updated = target.with_resolution_updates(
        base,
        spot_intensities=intensities,
    )

    assert updated is not base
    np.testing.assert_array_equal(
        base.spot_intensities,
        np.ones_like(base.spot_intensities),
    )
    np.testing.assert_array_equal(updated.spot_intensities,intensities)
    assert updated.target_signature == target.signature
    np.testing.assert_array_equal(
        updated.target_array,
        target.prepared_definition.render_internal(intensities),
    )
    np.testing.assert_array_equal(updated.preview,updated.target_array)
    assert "round_index" not in updated.details
    assert "position_correction_active" not in updated.details
    assert not updated.spot_intensities.flags.writeable


def test_multi_foci_target_position_update_is_rejected():
    target = _target(MultiFociTarget)
    base = target.resolution
    positions = np.array(base.spot_positions_kxy,copy=True)
    positions[0,0] += 1e-3

    try:
        target.with_resolution_updates(base,spot_positions_kxy=positions)
    except RuntimeError as error:
        assert "position" in str(error).lower()
    else:
        raise AssertionError("Expected raster target to reject position update")


def test_multi_foci_vector_target_intensity_update_regenerates_preview():
    target = _target(MultiFociVectorTarget)
    base = target.resolution
    intensities = np.array([1.0,0.4,0.7,0.2],dtype=np.float64)

    updated = target.with_resolution_updates(
        base,
        spot_intensities=intensities,
    )

    assert base.target_signature == target.signature
    assert updated.target_signature == target.signature
    assert updated is not base
    assert updated.target_array is None
    np.testing.assert_array_equal(updated.spot_positions_kxy,base.spot_positions_kxy)
    np.testing.assert_array_equal(updated.spot_intensities,intensities)
    np.testing.assert_array_equal(
        updated.preview,
        rasterize_spots(
            base.spot_positions_kxy,intensities,base.preview.shape,strict=False,
        ),
    )
    assert "round_index" not in updated.details
    assert "position_correction_active" not in updated.details


def test_multi_foci_vector_target_position_update_regenerates_preview():
    target = _target(MultiFociVectorTarget)
    base = target.resolution
    positions = np.array(base.spot_positions_kxy,copy=True)
    positions[0] += 1e-3

    updated = target.with_resolution_updates(
        base,
        spot_positions_kxy=positions,
    )

    assert updated.target_signature == target.signature
    assert updated is not base
    assert updated.target_array is None
    np.testing.assert_array_equal(updated.spot_positions_kxy,positions)
    np.testing.assert_array_equal(updated.spot_intensities,base.spot_intensities)
    np.testing.assert_array_equal(
        updated.preview,
        rasterize_spots(
            positions,base.spot_intensities,base.preview.shape,strict=False,
        ),
    )
    np.testing.assert_array_equal(
        base.spot_positions_kxy,
        target.resolution.spot_positions_kxy,
    )
    assert not updated.spot_positions_kxy.flags.writeable


def test_multi_foci_vector_target_combined_update_regenerates_preview():
    target = _target(MultiFociVectorTarget)
    base = target.resolution
    positions = np.array(base.spot_positions_kxy,copy=True)
    positions[1] -= 1e-3
    intensities = np.array([0.2,1.0,0.5,0.8],dtype=np.float64)

    updated = target.with_resolution_updates(
        base,
        spot_positions_kxy=positions,
        spot_intensities=intensities,
    )

    assert updated.target_signature == target.signature
    np.testing.assert_array_equal(updated.spot_positions_kxy,positions)
    np.testing.assert_array_equal(updated.spot_intensities,intensities)
    np.testing.assert_array_equal(
        updated.preview,
        rasterize_spots(positions,intensities,base.preview.shape,strict=False),
    )


def test_session_no_longer_exposes_target_rasterization_helpers():
    assert not hasattr(session_module,"_rasterize_spots")
    assert not hasattr(session_module.CGHSession,"_effective_positions")


def test_target_update_rejects_resolution_from_different_target_definition():
    target = _target(MultiFociVectorTarget)
    other = _target(
        MultiFociVectorTarget,
        period_x_px=24.0,
        period_y_px=24.0,
        fov_x_px=24.0,
        fov_y_px=24.0,
    )
    assert target.signature != other.signature

    try:
        target.with_resolution_updates(
            other.resolution,
            spot_intensities=np.ones_like(other.resolution.spot_intensities),
        )
    except RuntimeError as error:
        assert "different target definition" in str(error).lower()
    else:
        raise AssertionError("Expected mismatched target resolution rejection")
