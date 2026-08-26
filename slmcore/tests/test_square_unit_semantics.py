from __future__ import annotations

import numpy as np

from slmcore.calibration import SLMSectionCalibration
from slmcore.cgh.targets.multi_foci import MultiFociTarget
from slmcore.cgh.targets.multi_foci_vector import MultiFociVectorTarget
from slmcore.cgh.targets.raster_lattice import ResolvedRasterLattice
from slmcore.engine.parameters import METRIC_UNIT,SLM_UNIT
from slmcore.engine.section.context import SectionContext
from slmcore.engine.section.geometry import SectionGeometry
from slmcore.engine.state import ParameterSetState


def _context():
    return SectionContext(
        geometry=SectionGeometry(
            key="sec_0",x=0,y=0,width=512,height=512,
        ),
        pixel_size_um=1.0,
        wavelength_nm=488,
        pupil_radius_px=256,
        center_offset_x_px=0,
        center_offset_y_px=0,
        calibration=SLMSectionCalibration(
            kx_per_um=0.01,
            ky_per_um=0.012,
        ),
    )


def _params(target_class,**updates):
    params = {
        key:spec.default
        for key,spec in target_class.target_params.items()
    }
    params.update(updates)
    return params


def _metric_value(target_class,key,value,context):
    return target_class.target_params[key].to_unit(
        value,METRIC_UNIT,context.calibration,
    )


def test_vector_square_in_slm_units_keeps_canonical_xy_equal():
    context = _context()
    params = _params(
        MultiFociVectorTarget,
        square=True,
        square_unit=SLM_UNIT,
    )

    canonical,_ = MultiFociVectorTarget.canonicalize_params(
        params,("square","square_unit"),context=context,
    )

    assert canonical["period_x_px"] == canonical["period_y_px"]
    assert canonical["fov_x_px"] == canonical["fov_y_px"]
    assert canonical["n_foci_x"] == canonical["n_foci_y"]
    assert not np.isclose(
        _metric_value(
            MultiFociVectorTarget,"period_x_px",
            canonical["period_x_px"],context,
        ),
        _metric_value(
            MultiFociVectorTarget,"period_y_px",
            canonical["period_y_px"],context,
        ),
        rtol=0.0,
        atol=1e-12,
    )


def test_vector_square_in_metric_units_keeps_physical_xy_equal():
    context = _context()
    params = _params(
        MultiFociVectorTarget,
        square=True,
        square_unit=METRIC_UNIT,
    )

    canonical,_ = MultiFociVectorTarget.canonicalize_params(
        params,("square","square_unit"),context=context,
    )

    assert canonical["period_x_px"] != canonical["period_y_px"]
    assert canonical["fov_x_px"] != canonical["fov_y_px"]
    assert canonical["n_foci_x"] == canonical["n_foci_y"]
    assert np.isclose(
        _metric_value(
            MultiFociVectorTarget,"period_x_px",
            canonical["period_x_px"],context,
        ),
        _metric_value(
            MultiFociVectorTarget,"period_y_px",
            canonical["period_y_px"],context,
        ),
        rtol=0.0,
        atol=1e-12,
    )
    assert np.isclose(
        _metric_value(
            MultiFociVectorTarget,"fov_x_px",
            canonical["fov_x_px"],context,
        ),
        _metric_value(
            MultiFociVectorTarget,"fov_y_px",
            canonical["fov_y_px"],context,
        ),
        rtol=0.0,
        atol=1e-12,
    )


def test_raster_square_in_metric_units_uses_normal_raster_approximation():
    # Use a rectangular section and a realistic non-rational calibration ratio:
    # an exactly square physical lattice is generally not exactly rasterizable.
    context = SectionContext(
        geometry=SectionGeometry(
            key="sec_0",x=0,y=0,width=636,height=1024,
        ),
        pixel_size_um=1.0,
        wavelength_nm=488,
        pupil_radius_px=256,
        center_offset_x_px=0,
        center_offset_y_px=0,
        calibration=SLMSectionCalibration(
            kx_per_um=0.010137123,
            ky_per_um=0.01189373,
        ),
    )
    params = _params(
        MultiFociTarget,
        square=True,
        square_unit=METRIC_UNIT,
    )

    requested_metric_period = _metric_value(
        MultiFociTarget,"period_x_px",params["period_x_px"],context,
    )
    canonical,prepared = MultiFociTarget.canonicalize_params(
        params,("square","square_unit"),context=context,
    )

    assert isinstance(prepared,ResolvedRasterLattice)
    assert canonical["period_x_px"] != canonical["period_y_px"]

    metric_x = _metric_value(
        MultiFociTarget,"period_x_px",
        canonical["period_x_px"],context,
    )
    metric_y = _metric_value(
        MultiFociTarget,"period_y_px",
        canonical["period_y_px"],context,
    )

    # Square is exact in the requested semantic geometry, then each axis is
    # rasterized by the normal resolver. The realized physical periods are
    # therefore close, but are not required to be mathematically identical.
    assert np.isclose(metric_x,requested_metric_period,rtol=1e-3,atol=0.0)
    assert np.isclose(metric_y,requested_metric_period,rtol=1e-3,atol=0.0)
    assert np.isclose(metric_x,metric_y,rtol=1e-3,atol=0.0)
    assert not np.isclose(metric_x,metric_y,rtol=0.0,atol=1e-12)

    assert np.isclose(
        prepared.x_grid.period_px,canonical["period_x_px"],
        rtol=0.0,atol=1e-12,
    )
    assert np.isclose(
        prepared.y_grid.period_px,canonical["period_y_px"],
        rtol=0.0,atol=1e-12,
    )


def test_missing_square_unit_from_old_target_state_defaults_to_slm():
    specs = MultiFociVectorTarget.target_params
    state = ParameterSetState.from_specs(specs)
    old_values = {
        key:spec.default
        for key,spec in specs.items()
        if key != "square_unit"
    }
    old_values["square"] = True

    state.load_dict(old_values)

    assert state.values["square"] is True
    assert state.values["square_unit"] == SLM_UNIT
