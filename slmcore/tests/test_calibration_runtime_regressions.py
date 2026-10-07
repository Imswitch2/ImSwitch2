from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def _runtime_and_session(tmp_path: Path, *, with_fov_store: bool=False):
    from slmcore import (
        DEFAULT_REGISTRIES, ImageMeasurement, ObservationPlaneDefinition,
        SLMGeometry, SLMIdentity, SLMRuntime, SLMSession, SLMWorkspace,
    )
    from slmcore.core.cgh import CGHResult
    from slmcore.core.engine.section import split_slm_geometry

    workspace = SLMWorkspace(tmp_path)
    geometry = SLMGeometry(width=64, height=64, pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm", "SER"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry, 1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch(
        "sec_0",
        {
            ("cgh", "active"): True,
            ("cgh", "selected_target"): "multi_foci_vector",
            ("cgh", "multi_foci_vector", "params", "n_foci_x"): 3,
            ("cgh", "multi_foci_vector", "params", "n_foci_y"): 3,
        },
    )
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape, dtype=np.complex128),
        ),
    )
    workspace.mapping_store.add({"name": "100x"})
    workspace.observation_plane_store.add(
        ObservationPlaneDefinition(name="Main", default_pixel_size_um=0.2)
    )
    kwargs = dict(
        runtime=runtime,
        calibration_store=workspace.calibration_store,
        mapping_store=workspace.mapping_store,
        observation_plane_store=workspace.observation_plane_store,
    )
    if with_fov_store:
        kwargs["fov_mapping_correction_store"] = workspace.fov_mapping_correction_store
        kwargs["geometry_orientation_calibration_store"] = (
            workspace.geometry_orientation_calibration_store
        )
    session = SLMSession(**kwargs)
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0", "100x")
    )
    return runtime, session, ImageMeasurement


def test_base_calibration_explicit_pixel_size_overrides_plane_fallback(tmp_path):
    _runtime, session, ImageMeasurement = _runtime_and_session(tmp_path)
    session.calibration.ensure_target_reference("sec_0")

    state = session.calibration.commit_target_measurement(
        "sec_0",
        ImageMeasurement(np.zeros((64, 64)), source="file"),
        pixel_size_um=0.75,
    )

    assert state.measurement.pixel_size_um == pytest.approx(0.75)


def test_fov_calibration_explicit_pixel_size_overrides_plane_fallback(tmp_path):
    _runtime, session, ImageMeasurement = _runtime_and_session(
        tmp_path, with_fov_store=True,
    )
    session.fov_calibration.ensure_reference("sec_0", "100x")

    state = session.fov_calibration.commit_measurement(
        "sec_0",
        "100x",
        ImageMeasurement(np.zeros((64, 64)), source="file"),
        pixel_size_um=0.75,
    )

    assert state.measurement.pixel_size_um == pytest.approx(0.75)


def test_geometry_dialog_close_finishes_active_calibration_frame():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtCore, QtWidgets
        from slmcore.qt.application.geometry_calibration import GeometryCalibrationCoordinator
    except Exception as error:
        pytest.skip(f"Geometry calibration Qt dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class FakeService:
        def __init__(self):
            self.observation_planes = SimpleNamespace(get=lambda _name: object())
            self.finish_count = 0
            self.active = True

        def observation_plane_names(self):
            return ("Main",)

        def available_sources(self, _section):
            return ()

        def preferred_source(self, _section, _plane):
            return None

        def context(self, _section, plane):
            return {
                "observation_plane_name": plane,
                "calibrated": False,
                "calibration": None,
                "grid_x": 5,
                "grid_y": 4,
                "period_x_px": 6.0,
                "period_y_px": 6.0,
                "metric_periods_available": False,
                "computed": self.active,
                "frame_active": self.active,
                "any_frame_active": self.active,
                "active_frame_observation_plane": plane if self.active else None,
                "measurement": None,
                "localization": None,
                "localization_parameters": {},
                "localization_context": {},
                "analysis": None,
                "analyze_enabled": False,
                "save_enabled": False,
                "detector_name": None,
            }

        def finish(self, _section):
            self.finish_count += 1
            self.active = False

    class FakeQtSession(QtCore.QObject):
        sigError = QtCore.Signal(str, object)

        def __init__(self):
            super().__init__()
            self.geometry_calibration_service = FakeService()

    qt_session = FakeQtSession()
    coordinator = GeometryCalibrationCoordinator(qt_session)
    coordinator.open("sec_0", "Main")
    dialog = coordinator.dialog("sec_0")
    assert dialog is not None

    dialog.reject()
    app.processEvents()

    assert qt_session.geometry_calibration_service.finish_count == 1
    coordinator.close_all()
