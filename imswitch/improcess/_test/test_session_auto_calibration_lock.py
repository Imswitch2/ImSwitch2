"""Completed Session Auto reconstruction exposes calibration without re-localizing."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

pytest.importorskip("qtpy")
from qtpy import QtWidgets

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.params_widget import MonalisaParamsWidget
from imswitch.improcess.reconstructors.monalisa.session_registry import MonalisaSessionRegistry


def test_session_auto_result_enables_lock_without_loading_source(qtbot, monkeypatch, tmp_path):
    # Hardware setup detection is machine-dependent; this test starts with
    # an explicitly unknown detector to exercise adoption from the result.
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.monalisa.params_widget.startup_detector_id",
        lambda: "unknown",
    )
    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    qtbot.addWidget(widget)
    assert widget._current_data_obj is None
    assert widget._detector_id == "unknown"
    original_params = widget.get_values()
    assert not original_params["auto_localization"]

    resolved = PatternLocalization.manual_grid(12, 14, 1, 2)
    widget.remember_auto_reconstruction({
        "detector": "CAM",
        "localization_source": "Auto",
        "resolved_localization": resolved.to_dict(),
        "orientation_source": "Auto",
        "resolved_orientation": "+x+y",
    }, source_label="first acquisition")

    assert widget._detector_id == "CAM"
    assert widget.lockLocalizationButton.isEnabled()
    assert widget.lockOrientationButton.isEnabled()
    assert registry.current_localization("CAM").source_dataset == "first acquisition"
    # A result must NOT seed a subsequent Auto reconstruction silently.
    assert widget.get_values()["auto_localization"] == original_params["auto_localization"]
    assert widget.get_values()["localization_mode"] == "Auto"

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: ("first good", True))
    widget._lock_current_localization()
    assert registry.localization_profiles("CAM")[0].name == "first good"
    assert widget.get_values()["localization_mode"] == "Locked"
    registry.complete_session()


def test_single_startup_detector_is_used_before_loading_data(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.monalisa.params_widget.startup_detector_id",
        lambda: "CAM",
    )
    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    qtbot.addWidget(widget)
    assert widget._current_data_obj is None
    assert widget._detector_id == "CAM"
    registry.complete_session()


def test_auto_result_for_other_detector_does_not_change_active_detector(qtbot, tmp_path):
    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    qtbot.addWidget(widget)
    widget._detector_id = "CAM-A"
    widget._current_data_obj = object()  # an active, separately selected source
    localization = PatternLocalization.manual_grid(10, 10)
    widget.remember_auto_reconstruction({
        "detector": "CAM-B", "localization_source": "Auto",
        "resolved_localization": localization.to_dict(),
    }, source_label="other camera")
    assert widget._detector_id == "CAM-A"
    assert registry.current_localization("CAM-B") is not None
    assert registry.current_localization("CAM-A") is None
    registry.complete_session()


def test_only_completed_nonprovisional_published_calibration_is_lockable():
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    remembered = []
    dataset = SimpleNamespace(name="first recording")
    session = SimpleNamespace(dataset_for_result=lambda result: dataset)
    fake = SimpleNamespace(
        _session=session, _autoCalibrationSeenJobs=set(),
        _main=SimpleNamespace(reconstructorManager=SimpleNamespace(
            rememberMonalisaAutoCalibration=lambda result, **kwargs:
                (remembered.append((result, kwargs)) or True)
        )),
        _logger=SimpleNamespace(warning=lambda *args: None),
    )
    result = SimpleNamespace(
        name="result", _improcess_queue_job_id="job-1",
        metadata={"monalisa_streaming": {"provisional_preview": True},
                  "result_assembly": {"published_steps": 0}},
    )
    ProcessingSessionController._autoCalibrationPreviewPublished(fake, result)
    assert not remembered

    result.metadata["monalisa_streaming"]["provisional_preview"] = False
    ProcessingSessionController._autoCalibrationPreviewPublished(fake, result)
    assert not remembered  # T0 is still incomplete

    result.metadata["result_assembly"]["published_steps"] = 1
    ProcessingSessionController._autoCalibrationPreviewPublished(fake, result)
    ProcessingSessionController._autoCalibrationPreviewPublished(fake, result)
    assert len(remembered) == 1
    assert remembered[0][1]["source_label"] == "first recording"
