"""One storage story for every calibration method: ``transforms/<id>.json``
(the record) plus ``<id>.json`` (the smart pipeline's entry) in
``<ImSwitchConfig>/calibrations/``, next to the laser store's
``*.calib.json``. Records written before the panel took over -- the bead
window's ``cam_to_scan`` and the Transform Calibration panel's own
file-dialog saves -- still load and, where they did, still drive the
pipeline."""
import json

import numpy as np
import pytest

from imswitch.imcommon.algorithms.transforms import AcquisitionContext, AffineTransform, ResidualStats, SpatialTransform
from imswitch.imcommon.algorithms.transforms import io as transform_io
from imswitch.imcommon.model.smartpipeline import Points, apply_transform
from imswitch.imcontrol.model import transform_store
from imswitch.imcontrol.model.calibrations import CalibrationStore, parse_legacy_csv
from imswitch.imcontrol.model.smartpipeline.calibrations import CalibrationError, load_calibrations

from .test_calibration_records import MEASURED_W

#: What the bead window on dev wrote for ``cam_to_scan`` (record and entry), trimmed.
LEGACY_RECORD = {
    "schema": "imswitch.transform/1", "created_at": "2026-11-26T15:02:11Z",
    "source_frame": "detector:Camera", "target_frame": "scanner:galvo", "units": "um", "ndim": 2,
    "kind": "affine",
    "params": {"matrix": [[0.084, 0.0485, -53.94], [0.0484, -0.0838, 16.11], [0.0, 0.0, 1.0]]},
    "source_context": {"detector": "Camera", "image_shape": [800, 800], "roi": [0, 0, 800, 800], "binning": 1,
                       "pixel_size_um": [0.1, 0.1]},
    "target_context": {"detector": "APD", "image_shape": [200, 240], "axes": ["Y", "X"], "units": "um",
                       "scan_geometry": {"axes": [{"device": "X", "step_um": 0.1, "first_um": -8.95},
                                                  {"device": "Y", "step_um": 0.1, "first_um": -11.95}]}},
    "residuals": {"n_points": 13, "n_inliers": 13, "mean": 0.004, "median": 0.004, "max": 0.009, "rms": 0.005,
                  "unit": "um", "threshold": 0.6},
    "provenance": {"estimator": "affine", "beads_used": 13, "residual_rms_um": 0.005, "residual_max_um": 0.009},
}
LEGACY_ENTRY = {"type": "spatial-transform", "id": "cam_to_scan", "source_frame": "detector:Camera",
                "target_frame": "scanner:galvo", "path": "transforms/cam_to_scan.json", "target_axes": ["Y", "X"],
                "residual_rms_um": 0.005, "residual_max_um": 0.009, "n_beads": 13}


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "calibrations"
    (root / "transforms").mkdir(parents=True)
    (root / "transforms" / "cam_to_scan.json").write_text(json.dumps(LEGACY_RECORD))
    (root / "cam_to_scan.json").write_text(json.dumps(LEGACY_ENTRY))
    CalibrationStore(root).add(parse_legacy_csv(MEASURED_W))
    return root


def _registration(source_detector=None, target_detector=None):
    """A record as the Transform Calibration panel's Save… wrote it (frames
    moving -> reference, pixel units)."""
    model = AffineTransform.from_linear_offset([[1.01, 0.02], [-0.02, 1.01]], [3.0, -4.0])
    return SpatialTransform(
        model, "moving", "reference", units="px",
        source_context=AcquisitionContext(detector=source_detector, image_shape=(512, 512)),
        target_context=AcquisitionContext(detector=target_detector, image_shape=(512, 512)),
        residuals=ResidualStats.from_residuals([0.1, 0.2, 0.15]),
        provenance={"strategy": "foci_grid", "estimator": "affine"})


def test_the_legacy_bead_record_still_loads_and_drives_the_pipeline(folder):
    registry = load_calibrations(folder)
    assert list(registry.ids()) == ["cam_to_scan"]          # the laser record is not a transform
    ref = registry.get("cam_to_scan")
    record = transform_store.load_record("cam_to_scan", folder)
    points = np.array([[100.0, 200.0]])
    moved = apply_transform(Points(points, frame="detector:Camera"), ref)
    np.testing.assert_allclose(moved.coordinates, record.map_points(points))
    listed = transform_store.list_records(folder)
    assert [(item["id"], item["pipeline"]) for item in listed] == [("cam_to_scan", True)]


def test_a_panel_file_save_loads_and_is_published_under_an_id(folder):
    # Saved through the panel's file dialog, into its default folder: a record only.
    path = transform_io.save_json(folder / "transforms" / "2026-11-20_101500_image_registration.json",
                                  _registration("Camera", "Camera2"))
    assert "2026-11-20_101500_image_registration" not in load_calibrations(folder)
    loaded = transform_store.load_record(path)
    assert loaded.source_frame == "moving"
    # Saved under an id: published with detector frames from its contexts.
    paths = transform_store.save_record(loaded, "cam1_to_cam2", folder)
    assert paths["pipeline"].name == "cam1_to_cam2.json" and not paths["notes"]
    ref = load_calibrations(folder).get("cam1_to_cam2")
    assert (ref.source_frame, ref.target_frame) == ("detector:Camera", "detector:Camera2")
    np.testing.assert_allclose(ref.apply(np.array([[1.0, 2.0]])), loaded.map_points(np.array([[1.0, 2.0]])))


def test_a_record_with_unknown_frames_is_saved_but_not_offered_to_the_pipeline(folder):
    paths = transform_store.save_record(_registration(), "from_files", folder)
    assert paths["record"].exists() and "pipeline" not in paths
    assert "frames are unknown" in paths["notes"][0]
    assert "from_files" not in load_calibrations(folder)
    # The frames can be given.
    paths = transform_store.save_record(_registration(), "from_files", folder, overwrite=True,
                                        source_frame="detector:Camera", target_frame="detector:Camera2")
    assert "from_files" in load_calibrations(folder)


def test_ids_are_protected_and_archived(folder):
    with pytest.raises(CalibrationError, match="exists"):
        transform_store.save_record(_registration("A", "B"), "cam_to_scan", folder)
    new_id = transform_store.archive("cam_to_scan", folder)
    assert new_id.startswith("cam_to_scan_")
    assert (folder / "transforms" / f"{new_id}.json").exists() and (folder / f"{new_id}.json").exists()
    assert not transform_store.exists("cam_to_scan", folder)
    assert new_id in load_calibrations(folder)


def test_the_laser_store_is_untouched_by_transform_saves(folder):
    before, unreadable = CalibrationStore(folder).scan()
    transform_store.save_record(_registration("A", "B"), "a_to_b", folder)
    after, unreadable_after = CalibrationStore(folder).scan()
    assert [r.record.id for r in after] == [r.record.id for r in before] and unreadable_after == unreadable == []
    assert sorted(load_calibrations(folder).ids()) == ["a_to_b", "cam_to_scan"]


def test_the_panel_loads_both_kinds_of_record(folder, qtbot):
    from imswitch.imcommon.view.widgets.TransformCalibrationWidget import TransformCalibrationWidget

    widget = TransformCalibrationWidget()
    qtbot.addWidget(widget)
    assert widget.loadTransformFromPath(folder / "transforms" / "cam_to_scan.json")
    assert widget.transform.units == "um" and "MIRRORED" in widget.matrixView.toPlainText()
    widget.predict(400, 400)
    assert "Y = " in widget.predictResultLabel.text()
    # Saved by hand into the calibrations folder itself: the pipeline loader skips it.
    path = transform_io.save_json(folder / "old.json", _registration())
    assert "old" not in load_calibrations(folder)
    assert widget.loadTransformFromPath(path)
    assert widget.transform.source_frame == "moving"
    widget.setCalibrationStore(transform_store.TransformStore(folder))
    widget.idEdit.setText("registered")
    paths = widget.saveToStore()
    assert paths["record"].exists() and "frames are unknown" in widget.matrixView.toPlainText()
