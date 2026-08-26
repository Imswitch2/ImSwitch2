from __future__ import annotations

import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.cgh.targets.lattice import (
    LatticeAxisIntent,
    LatticeDefinition,
    LatticeLockRequest,
    LatticeResolutionIntent,
    reconcile_lattice_params_with_intent,
)
from slmcore.cgh.targets.raster_lattice import (
    RasterResolutionPolicy,
    RasterResolutionPriority,
    resolve_raster_lattice,
)
from slmcore.engine.section import split_slm_geometry


def _runtime() -> SLMRuntime:
    geometry = SLMGeometry(width=512,height=512,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch(
        "sec_0",{("cgh","selected_target"):"multi_foci"},
    )
    return runtime


def _target(runtime: SLMRuntime,target_key="multi_foci"):
    return runtime.get_section_snapshot("sec_0").state.cgh.items[target_key]


def _value(runtime: SLMRuntime,key: str):
    return _target(runtime).params.get_param_value(key)


def _lock(runtime: SLMRuntime):
    return _target(runtime).lock_state


def _set_lock(runtime: SLMRuntime,kind: str,reference):
    return runtime.apply_section_patch(
        "sec_0",{},
        lattice_lock_request=LatticeLockRequest(
            "multi_foci",kind,reference,
        ),
    )


def _patch(runtime: SLMRuntime,**values):
    return runtime.apply_section_patch(
        "sec_0",{
            ("cgh","multi_foci","params",key):value
            for key,value in values.items()
        },
    )


def test_raster_lock_state_is_persisted_but_vector_target_has_none():
    runtime = _runtime()
    _set_lock(runtime,"fov",(180.0,170.0))

    state = runtime.get_section_snapshot("sec_0").state
    data = state.to_dict()
    assert data["cgh"]["items"]["multi_foci"]["lock_state"] == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert "lock_state" not in data["cgh"]["items"]["multi_foci_vector"]

    cloned = state.clone()
    assert cloned.cgh.items["multi_foci"].lock_state.to_dict() == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert cloned.cgh.items["multi_foci_vector"].lock_state is None

    restored = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )
    restored_state = restored.get_section_snapshot("sec_0").state.cgh
    assert restored_state.items["multi_foci"].lock_state.to_dict() == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert restored_state.items["multi_foci_vector"].lock_state is None


def test_fov_lock_prevents_stepwise_drift_from_becoming_next_reference():
    stepped = _runtime()
    direct = _runtime()
    _set_lock(stepped,"fov",(180.0,180.0))
    _set_lock(direct,"fov",(180.0,180.0))

    for period in (6.05,6.10,6.15,6.20,6.25):
        _patch(stepped,period_x_px=period)
    _patch(direct,period_x_px=6.25)

    assert _lock(stepped).reference == (180.0,180.0)
    assert _lock(direct).reference == (180.0,180.0)
    for key in ("period_x_px","fov_x_px","n_foci_x"):
        assert _value(stepped,key) == pytest.approx(_value(direct,key))


def test_n_foci_lock_prevents_realized_count_from_becoming_next_reference():
    stepped = _runtime()
    direct = _runtime()
    _set_lock(stepped,"n_foci",(31,31))
    _set_lock(direct,"n_foci",(31,31))

    for period in (6.05,6.10,6.15,6.20,6.25):
        _patch(stepped,period_x_px=period)
    _patch(direct,period_x_px=6.25)

    assert _lock(stepped).reference == (31,31)
    assert _lock(direct).reference == (31,31)
    for key in ("period_x_px","fov_x_px","n_foci_x"):
        assert _value(stepped,key) == pytest.approx(_value(direct,key))


def test_explicit_batch_overrides_lock_for_transaction_without_replacing_it():
    params = {
        "period_x_px":6.5,"period_y_px":6.0,
        "fov_x_px":180.0,"fov_y_px":180.0,
        "n_foci_x":41,"n_foci_y":31,
    }
    from slmcore.cgh.targets.lattice import LatticeLockState

    resolved,intent = reconcile_lattice_params_with_intent(
        params,{"period_x_px","n_foci_x"},
        lock=LatticeLockState(kind="fov",reference=(180.0,180.0)),
    )

    assert resolved["period_x_px"] == 6.5
    assert resolved["n_foci_x"] == 41
    assert resolved["fov_x_px"] == pytest.approx(260.0)
    assert intent.x.explicit == frozenset({"period","n_foci"})
    assert intent.x.persistent == frozenset()


def test_editing_locked_quantity_updates_only_successful_reference_components():
    runtime = _runtime()
    _set_lock(runtime,"fov",(180.0,180.0))

    _patch(runtime,fov_x_px=190.0)
    assert _lock(runtime).reference == (190.0,180.0)

    with pytest.raises(ValueError):
        _patch(runtime,fov_y_px=-1.0)
    assert _lock(runtime).reference == (190.0,180.0)


def test_fov_resolution_priority_can_choose_a_different_raster_compromise():
    lattice = LatticeDefinition(
        period_x_px=8.1,period_y_px=8.1,
        n_foci_x=20,n_foci_y=20,
    )
    axis_intent = LatticeAxisIntent(
        period_px=8.1,
        n_foci=20,
        fov_px=180.0,
        explicit=frozenset({"period","n_foci","fov"}),
    )
    intent = LatticeResolutionIntent(x=axis_intent,y=axis_intent)

    period = resolve_raster_lattice(
        lattice,(256,256),
        RasterResolutionPolicy(RasterResolutionPriority.PERIOD,-1,1),
        intent=intent,
    )
    fov = resolve_raster_lattice(
        lattice,(256,256),
        RasterResolutionPolicy(RasterResolutionPriority.FOV,-1,1),
        intent=intent,
    )

    assert abs(period.lattice.period_x_px - 8.1) < abs(fov.lattice.period_x_px - 8.1)
    assert abs(fov.lattice.fov_x_px - 180.0) < abs(period.lattice.fov_x_px - 180.0)
    assert fov.lattice.fov_x_px == pytest.approx(180.0)


def test_period_only_intent_keeps_precanonical_fov_as_fov_priority_reference():
    params = {
        "period_x_px":6.25,"period_y_px":6.0,
        "fov_x_px":180.0,"fov_y_px":180.0,
        "n_foci_x":31,"n_foci_y":31,
    }
    resolved,intent = reconcile_lattice_params_with_intent(
        params,{"period_x_px"},lock=None,
    )

    assert resolved["fov_x_px"] != pytest.approx(180.0)
    assert intent.x.fov_px == pytest.approx(180.0)
    assert intent.x.explicit == frozenset({"period"})
