from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.mockscan import SimulatedScanPlan
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.basesignaldesigners import (
    ScanInfoContract,
    validate_scan_info_contract,
)


def _scan_setup(sample_rate=1000):
    positioners = {
        axis: SimpleNamespace(
            forScanning=True,
            managerProperties={"conversionFactor": 1},
        )
        for axis in ("X", "Y", "Z")
    }
    return SimpleNamespace(
        scan=SimpleNamespace(sampleRate=sample_rate),
        positioners=positioners,
    )


def _beta_scan_parameters(*, n_linesteps=1):
    return {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [3, 2, 0],
        "axis_step_size": [1, 1, 1],
        "axis_startpos": [[0], [0], [0]],
        "axis_centerpos": [0, 0, 0],
        "return_time": 0.001,
        "sequence_time": 0.002,
        "n_linesteps": n_linesteps,
    }


def _valid_contract_kwargs(**overrides):
    kwargs = {
        "img_dims": [3, 2],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [1.0, 1.0],
        "scan_samples": [2, 6, 42],
        "scan_samples_total": 42,
        "scan_samples_d2_period": 7,
        "n_pixels_fast": 3,
        "samples_per_pixel": 2,
        "dwell_time": 0.002,
        "scan_time_step": 0.001,
        "n_linesteps": 3,
    }
    kwargs.update(overrides)
    return kwargs


def test_scan_info_contract_derives_and_validates_linestep_axis():
    contract = ScanInfoContract(**_valid_contract_kwargs())

    assert contract.img_dims == [3, 2]
    assert contract.img_axes_with_linesteps == ["x", "y", "linestep"]
    assert validate_scan_info_contract(contract.to_dict()) is True


def test_scan_info_contract_rejects_missing_sample_level():
    with pytest.raises(ValueError, match="scan_samples length"):
        ScanInfoContract(
            **_valid_contract_kwargs(scan_samples=[2, 6])
        )


def test_beta_scan_designer_keeps_linesteps_out_of_physical_dimensions():
    setup_info = _scan_setup()
    signals, positions, scan_info = BetaScanDesigner().make_signal(
        _beta_scan_parameters(n_linesteps=3),
        setup_info,
    )

    assert positions == [3, 2]
    assert scan_info["img_dims"] == [3, 2]
    assert scan_info["n_linesteps"] == 3
    assert scan_info["img_axes_with_linesteps"] == ["x", "y", "linestep"]
    assert len(scan_info["scan_samples"]) == len(scan_info["img_dims"]) + 1
    assert scan_info["scan_samples_total"] == 42
    assert {len(signal) for signal in signals.values()} == {42}
    assert validate_scan_info_contract(scan_info) is True


def test_beta_scan_designer_validates_3d_sample_levels():
    setup_info = _scan_setup()
    parameters = _beta_scan_parameters(n_linesteps=2)
    parameters["axis_length"] = [3, 2, 2]
    signals, positions, scan_info = BetaScanDesigner().make_signal(
        parameters,
        setup_info,
    )

    assert positions == [3, 2, 2]
    assert scan_info["img_dims"] == [3, 2, 2]
    assert scan_info["img_axes_with_linesteps"] == ["x", "y", "z", "linestep"]
    assert len(scan_info["scan_samples"]) == len(scan_info["img_dims"]) + 1
    assert scan_info["scan_samples_total"] == 56
    assert {len(signal) for signal in signals.values()} == {56}
    assert validate_scan_info_contract(scan_info) is True


def test_advanced_ttl_linestep_edges_drive_simulated_camera_frame_count():
    setup_info = _scan_setup()
    _signals, _positions, scan_info = BetaScanDesigner().make_signal(
        _beta_scan_parameters(n_linesteps=3),
        setup_info,
    )
    ttl_signals, returned_scan_info = AdvancedScanTTLCycleDesigner().make_signal(
        {
            "target_device": ["Camera"],
            "n_linesteps": 3,
            "linestep_enable": {"Camera": [True, False, True]},
            "pulse_starts_s": {"Camera": [[], [], []]},
            "pulse_ends_s": {"Camera": [[], [], []]},
            "sequence_time": 0.002,
            "advanced_mode": False,
        },
        setup_info,
        scan_info,
    )

    assert returned_scan_info is scan_info
    assert ttl_signals["Camera"].dtype == np.dtype("bool")
    assert len(ttl_signals["Camera"]) == scan_info["scan_samples_total"]
    assert len(ttl_signals["line_clock"]) == scan_info["scan_samples_total"]

    plan = SimulatedScanPlan.fromScan(
        SimpleNamespace(
            scan=setup_info.scan,
            detectors={"Camera": SimpleNamespace(forAcquisition=True)},
        ),
        {"TTLCycleSignalsDict": ttl_signals},
        scan_info,
    )

    assert plan.nPositions == 6
    assert plan.frameCounts["Camera"] == 4
