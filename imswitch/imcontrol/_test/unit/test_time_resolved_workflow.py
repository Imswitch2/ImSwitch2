from __future__ import annotations

import json

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanProducts,
)
from imswitch.imcontrol.model.workflows import (
    BinnedPhotonArrivalParams,
    BinnedPhotonArrivalWorkflow,
    GatedSTEDParams,
    GatedSTEDWorkflow,
    MicroscopeFacade,
    TauSTEDParams,
    TauSTEDWorkflow,
    TimeResolvedWorkflowParams,
    TimeResolvedScanWorkflow,
    build_mock_facade,
)


def _products(*, cube=True, lifetime=True, gates=True):
    cube_counts = (
        np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
        if cube else None
    )
    t_axis = np.array([0.5, 1.5, 2.5, 3.5], dtype=np.float32)
    intensity = (
        cube_counts.sum(axis=-1).astype(np.float32)
        if cube_counts is not None
        else np.ones((2, 3), dtype=np.float32) * 10
    )
    lifetime_ns = (
        np.ones((2, 3), dtype=np.float32) * 2.5
        if lifetime else None
    )
    gate_images = {
        "late": np.ones((2, 3), dtype=np.float32) * 3
    } if gates else {}
    return TimeResolvedScanProducts(
        cube_counts=cube_counts,
        cube_axes=("y", "x", "tcspc_bin"),
        t_axis_ns=t_axis,
        intensity=intensity,
        lifetime_ns=lifetime_ns,
        gate_images=gate_images,
        decay_counts=np.ones(4, dtype=np.float32),
        global_tau_ns=2.5,
        metadata={"backend": "mock", "scan_info": {"img_dims": [3, 2]}},
        is_final=True,
    )


def test_binned_photon_arrival_workflow_configures_cube_and_saves_npz(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=True))
    acquired = []
    params = BinnedPhotonArrivalParams(
        save_folder=tmp_path,
        save_h5=False,
        save_npz=True,
        save_tiff=True,
        timeout_s=0.1,
    )
    workflow = BinnedPhotonArrivalWorkflow(
        facade,
        params,
        acquisition=lambda: acquired.append("scan"),
    )

    result = workflow.run()

    assert acquired == ["scan"]
    assert result.products.cube_counts is not None
    assert result.output_paths["npz"].exists()
    assert result.output_paths["intensity_tiff"].exists()
    assert result.output_paths["lifetime_tiff"].exists()
    assert facade.call_names()[:3] == [
        "time_resolved.clear",
        "time_resolved.configure",
        "time_resolved.wait_for_final",
    ]
    config = facade.calls[1][1][0]
    assert config.capture_cube is True

    with np.load(result.output_paths["npz"]) as data:
        np.testing.assert_array_equal(data["cube_counts"], result.products.cube_counts)
        metadata = json.loads(str(data["metadata_json"]))
    assert metadata["backend"] == "mock"


def test_gated_sted_workflow_requests_gates_without_retaining_cube(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=False, gates=True))
    gate = GateSpec("late", 2.0, 5.0)
    params = GatedSTEDParams(
        gates=(gate,),
        save_folder=tmp_path,
        save_h5=False,
        save_npz=True,
        save_tiff=False,
        timeout_s=0.1,
    )

    result = GatedSTEDWorkflow(facade, params).run()

    config = facade.calls[1][1][0]
    assert config.capture_cube is False
    assert config.gates == (gate,)
    assert "late" in result.products.gate_images
    assert result.products.cube_counts is None
    assert result.output_paths["npz"].exists()


def test_time_resolved_workflow_saves_h5_when_h5py_available(tmp_path):
    h5py = pytest.importorskip("h5py")
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=True, gates=True))
    params = TimeResolvedWorkflowParams(
        capture_cube=True,
        gates=(GateSpec("late", 2.0, 5.0),),
        save_folder=tmp_path,
        save_h5=True,
        save_npz=False,
        save_tiff=False,
        timeout_s=0.1,
    )

    result = TimeResolvedScanWorkflow(facade, params).run()

    with h5py.File(result.output_paths["h5"], "r") as h5:
        assert h5.attrs["workflow_name"] == "time_resolved"
        assert "time_resolved/cube_counts" in h5
        assert "time_resolved/intensity" in h5
        assert "time_resolved/decay_counts" in h5
        assert "gates/late" in h5
        assert "fit" in h5
        assert "scan" in h5
        assert h5["gates/late"].attrs["start_ns"] == 2.0
        assert h5["gates/late"].attrs["stop_ns"] == 5.0
        assert json.loads(h5.attrs["metadata_json"])["backend"] == "mock"


def test_time_resolved_workflow_uses_facade_scan_when_no_acquisition_given(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=False))
    params = TimeResolvedWorkflowParams(
        save_folder=tmp_path,
        save_h5=False,
        save_tiff=False,
        timeout_s=0.1,
    )

    TimeResolvedScanWorkflow(facade, params).run()

    assert facade.call_names() == [
        "time_resolved.clear",
        "time_resolved.configure",
        "scan.run_once",
        "time_resolved.wait_for_final",
    ]
    assert facade.calls[2][2]["timeout_s"] == 0.1


def test_gated_sted_workflow_requires_at_least_one_gate(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products())
    workflow = GatedSTEDWorkflow(
        facade,
        GatedSTEDParams(save_folder=tmp_path, save_h5=False, save_tiff=False),
    )

    with pytest.raises(ValueError, match="at least one GateSpec"):
        workflow.run()


def test_tau_sted_workflow_requests_fit_and_requires_lifetime(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=False, lifetime=True))
    params = TauSTEDParams(
        fit=LifetimeFitConfig(method="exp1", min_counts_per_pixel=15),
        save_folder=tmp_path,
        save_h5=False,
        save_npz=True,
        save_tiff=False,
        timeout_s=0.1,
    )

    result = TauSTEDWorkflow(facade, params).run()

    config = facade.calls[1][1][0]
    assert config.fit.method == "exp1"
    assert config.fit.min_counts_per_pixel == 15
    np.testing.assert_array_equal(result.products.lifetime_ns, np.ones((2, 3)) * 2.5)


def test_tau_sted_workflow_raises_when_lifetime_missing(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products(cube=False, lifetime=False))
    workflow = TauSTEDWorkflow(
        facade,
        TauSTEDParams(save_folder=tmp_path, save_h5=False, save_tiff=False),
    )

    with pytest.raises(RuntimeError, match="lifetime_ns"):
        workflow.run()


def test_time_resolved_workflow_requires_facade_detector():
    workflow = TimeResolvedScanWorkflow(
        MicroscopeFacade(),
        TimeResolvedWorkflowParams(save_h5=False, save_tiff=False),
    )

    with pytest.raises(RuntimeError, match="facade.time_resolved"):
        workflow.run()
