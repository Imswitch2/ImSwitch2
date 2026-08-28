from __future__ import annotations

import numpy as np

from slmcore.core.cgh.targets.lattice import LatticeDefinition
from slmcore.core.cgh.targets.raster_lattice import (
    RasterResolutionPolicy,
    RasterResolutionPriority,
    materialize_exact_raster_lattice,
    resolve_raster_lattice,
)


def _period_policy():
    return RasterResolutionPolicy(
        priority=RasterResolutionPriority.PERIOD,
        min_foci_delta=-1,
        max_foci_delta=1,
    )


def _square_lattice(period_px=6.0,n_foci=61,stagger=0.0):
    return LatticeDefinition(
        period_x_px=period_px,
        period_y_px=period_px,
        n_foci_x=n_foci,
        n_foci_y=n_foci,
        stagger=stagger,
    )


def test_coupled_resolver_uses_small_axis_as_common_feasible_scale():
    resolved = resolve_raster_lattice(
        _square_lattice(),
        section_shape=(256,512),
        policy=_period_policy(),
    )

    assert resolved.target_shape == (256,256)
    assert resolved.x_grid.raster_spacing == 3
    assert resolved.y_grid.raster_spacing == 3
    assert resolved.lattice.period_x_px == 6.0
    assert resolved.lattice.period_y_px == 6.0
    assert np.isclose(
        np.ptp(resolved.spot_positions_kxy[0]),
        np.ptp(resolved.spot_positions_kxy[1]),
        rtol=0.0,
        atol=1e-12,
    )


def test_coupled_resolver_ignores_extra_height_when_common_scale_is_512():
    resolved = resolve_raster_lattice(
        _square_lattice(),
        section_shape=(900,600),
        policy=_period_policy(),
    )

    assert resolved.target_shape == (512,512)
    assert resolved.x_grid.raster_spacing == 6
    assert resolved.y_grid.raster_spacing == 6
    assert resolved.lattice.period_x_px == 6.0
    assert resolved.lattice.period_y_px == 6.0


def test_exact_materialization_uses_same_coupled_scale_policy():
    resolved = materialize_exact_raster_lattice(
        _square_lattice(),
        section_shape=(900,600),
    )

    assert resolved.target_shape == (512,512)
    assert resolved.x_grid.raster_spacing == 6
    assert resolved.y_grid.raster_spacing == 6


def test_coupling_does_not_change_non_equivalent_scientific_compromise():
    resolved = resolve_raster_lattice(
        LatticeDefinition(
            period_x_px=7.3,
            period_y_px=8.1,
            n_foci_x=41,
            n_foci_y=37,
        ),
        section_shape=(333,577),
        policy=_period_policy(),
    )

    # These are the pre-coupling period-priority choices.  The 2-D coupling
    # may change only raster scale among scientifically tied candidates.
    assert np.isclose(
        resolved.lattice.period_x_px,7.29938900203666,
        rtol=0.0,atol=1e-12,
    )
    assert np.isclose(
        resolved.lattice.period_y_px,8.10126582278481,
        rtol=0.0,atol=1e-12,
    )
    assert resolved.lattice.n_foci_x == 40
    assert resolved.lattice.n_foci_y == 37
