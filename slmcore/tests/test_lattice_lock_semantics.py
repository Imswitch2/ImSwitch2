from __future__ import annotations

import numpy as np
import pytest

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.cgh.targets.lattice import (
    LatticeAxisIntent,
    LatticeDefinition,
    LatticeLockRequest,
    LatticeResolutionIntent,
    reconcile_lattice_params_with_intent,
)
from slmcore.core.cgh.targets.raster_lattice import (
    RasterResolutionPolicy,
    RasterResolutionPriority,
    resolve_raster_lattice,
)
from slmcore.core.engine.section import split_slm_geometry


def _runtime(target_key="multi_foci") -> SLMRuntime:
    geometry = SLMGeometry(width=512,height=512,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch(
        "sec_0",{("cgh","selected_target"):target_key},
    )
    return runtime


def _target(runtime: SLMRuntime,target_key="multi_foci"):
    return runtime.get_section_snapshot("sec_0").state.cgh.items[target_key]


def _value(runtime: SLMRuntime,key: str,target_key="multi_foci"):
    return _target(runtime,target_key).params.get_param_value(key)


def _lock(runtime: SLMRuntime,target_key="multi_foci"):
    return _target(runtime,target_key).lock_state


def _set_lock(runtime: SLMRuntime,kind: str,reference,target_key="multi_foci"):
    return runtime.apply_section_patch(
        "sec_0",{},
        lattice_lock_request=LatticeLockRequest(
            target_key,kind,reference,
        ),
    )


def _patch(runtime: SLMRuntime,target_key="multi_foci",**values):
    return runtime.apply_section_patch(
        "sec_0",{
            ("cgh",target_key,"params",key):value
            for key,value in values.items()
        },
    )


def test_lattice_lock_state_is_persisted_for_raster_and_vector_targets():
    runtime = _runtime()
    _set_lock(runtime,"fov",(180.0,170.0))
    _set_lock(
        runtime,"n_foci",(31,29),target_key="multi_foci_vector",
    )

    state = runtime.get_section_snapshot("sec_0").state
    data = state.to_dict()
    assert data["cgh"]["items"]["multi_foci"]["lock_state"] == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert data["cgh"]["items"]["multi_foci_vector"]["lock_state"] == {
        "kind":"n_foci","reference":[31,29],
    }

    cloned = state.clone()
    assert cloned.cgh.items["multi_foci"].lock_state.to_dict() == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert cloned.cgh.items["multi_foci_vector"].lock_state.to_dict() == {
        "kind":"n_foci","reference":[31,29],
    }

    restored = SLMRuntime.from_config(
        runtime.create_config(),registries=DEFAULT_REGISTRIES,
    )
    restored_state = restored.get_section_snapshot("sec_0").state.cgh
    assert restored_state.items["multi_foci"].lock_state.to_dict() == {
        "kind":"fov","reference":[180.0,170.0],
    }
    assert restored_state.items["multi_foci_vector"].lock_state.to_dict() == {
        "kind":"n_foci","reference":[31,29],
    }


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


@pytest.mark.parametrize("kind,reference",[
    ("fov",(180.0,180.0)),
    ("n_foci",(31,31)),
])
def test_vector_lock_prevents_stepwise_period_drift(kind,reference):
    stepped = _runtime("multi_foci_vector")
    direct = _runtime("multi_foci_vector")
    _set_lock(stepped,kind,reference,target_key="multi_foci_vector")
    _set_lock(direct,kind,reference,target_key="multi_foci_vector")

    for period in (6.05,6.10,6.15,6.20,6.25):
        _patch(stepped,"multi_foci_vector",period_x_px=period)
    _patch(direct,"multi_foci_vector",period_x_px=6.25)

    assert _lock(stepped,"multi_foci_vector").reference == reference
    assert _lock(direct,"multi_foci_vector").reference == reference
    for key in ("period_x_px","fov_x_px","n_foci_x"):
        assert _value(stepped,key,"multi_foci_vector") == pytest.approx(
            _value(direct,key,"multi_foci_vector")
        )


def test_vector_editing_locked_quantity_updates_reference():
    runtime = _runtime("multi_foci_vector")
    _set_lock(
        runtime,"fov",(180.0,180.0),target_key="multi_foci_vector",
    )

    _patch(runtime,"multi_foci_vector",fov_x_px=190.0)

    assert _lock(runtime,"multi_foci_vector").reference == (190.0,180.0)


def test_explicit_batch_overrides_lock_for_transaction_without_replacing_it():
    params = {
        "period_x_px":6.5,"period_y_px":6.0,
        "fov_x_px":180.0,"fov_y_px":180.0,
        "n_foci_x":41,"n_foci_y":31,
    }
    from slmcore.core.cgh.targets.lattice import LatticeLockState

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


@pytest.mark.parametrize("target_key",["multi_foci","multi_foci_vector"])
def test_unlocked_period_edits_reuse_soft_fov_reference(target_key):
    stepped = _runtime(target_key)
    direct = _runtime(target_key)

    for period in (6.05,6.10,6.15,6.20,6.25):
        _patch(stepped,target_key,period_x_px=period)
    _patch(direct,target_key,period_x_px=6.25)

    assert _lock(stepped,target_key).kind is None
    assert _lock(direct,target_key).kind is None
    assert _lock(stepped,target_key).period_fov_reference == (180.0,180.0)
    assert _lock(direct,target_key).period_fov_reference == (180.0,180.0)
    for key in ("period_x_px","fov_x_px","n_foci_x"):
        assert _value(stepped,key,target_key) == pytest.approx(
            _value(direct,key,target_key)
        )


def test_vector_unlocked_soft_fov_reference_avoids_monotonic_count_ratchet():
    runtime = _runtime("multi_foci_vector")

    counts = []
    for period in (6.05,6.10,6.15,6.20,6.25,6.30,6.35,6.40):
        _patch(runtime,"multi_foci_vector",period_x_px=period)
        counts.append(_value(runtime,"n_foci_x","multi_foci_vector"))

    assert counts == [30,30,30,30,29,29,29,29]
    assert _lock(runtime,"multi_foci_vector").period_fov_reference == (180.0,180.0)


def test_unlocked_explicit_geometry_edit_refreshes_soft_fov_reference():
    runtime = _runtime("multi_foci_vector")

    _patch(runtime,"multi_foci_vector",period_x_px=6.10)
    assert _lock(runtime,"multi_foci_vector").period_fov_reference == (180.0,180.0)

    _patch(runtime,"multi_foci_vector",fov_x_px=200.0)
    assert _lock(runtime,"multi_foci_vector").period_fov_reference == (200.0,180.0)

    _patch(runtime,"multi_foci_vector",n_foci_x=21)
    assert _lock(runtime,"multi_foci_vector").period_fov_reference == pytest.approx(
        (122.0,180.0)
    )


def test_unlock_reseeds_soft_reference_from_current_realized_fov():
    runtime = _runtime("multi_foci_vector")
    _set_lock(
        runtime,"n_foci",(31,31),target_key="multi_foci_vector",
    )
    _patch(runtime,"multi_foci_vector",period_x_px=6.25)
    assert _value(runtime,"fov_x_px","multi_foci_vector") == pytest.approx(187.5)

    _set_lock(runtime,None,None,target_key="multi_foci_vector")

    lock = _lock(runtime,"multi_foci_vector")
    assert lock.kind is None
    assert lock.reference is None
    assert lock.period_fov_reference == pytest.approx((187.5,180.0))
