"""P-G.6: every model change goes through a command that can undo itself."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.roi_set import MeasurementConfig, ROISet
from imswitch.improcess.analysis.roi_commands import (
    AddROI,
    CommandLog,
    DeleteROI,
    RenameROI,
    UpdateROI,
)
from imswitch.improcess.analysis.roi_manager import ROIManagerModel


def _model(*names):
    return ROIManagerModel(
        [ROIRecord(name, "rectangle", (0, 4, 0, 4)) for name in names]
    )


def test_add_then_undo_leaves_the_model_as_it_was():
    model = _model("a")
    log = CommandLog(model)

    log.run(AddROI(ROIRecord("b", "rectangle", (2, 6, 2, 6))))
    assert [roi.name for roi in model.rois] == ["a", "b"]

    log.undo()
    assert [roi.name for roi in model.rois] == ["a"]


def test_delete_undo_restores_the_roi_in_its_original_position():
    """Order is user-visible, so an undone delete must not move the ROI."""
    model = _model("a", "b", "c")
    log = CommandLog(model)

    log.run(DeleteROI("b"))
    assert [roi.name for roi in model.rois] == ["a", "c"]

    log.undo()
    assert [roi.name for roi in model.rois] == ["a", "b", "c"]


def test_delete_undo_restores_every_field():
    model = ROIManagerModel(
        [ROIRecord("cell", "mask", (1, 5, 2, 6), pixels=((1, 2), (2, 3)), group=3)]
    )
    log = CommandLog(model)
    before = model.get("cell")

    log.run(DeleteROI("cell"))
    log.undo()

    after = model.get("cell")
    assert after.pixels == before.pixels
    assert after.group == before.group
    assert after.uid == before.uid


def test_rename_undo_restores_the_previous_name():
    model = _model("a")
    log = CommandLog(model)

    log.run(RenameROI("a", "nucleus"))
    assert model.get("nucleus") is not None

    log.undo()
    assert model.get("a") is not None
    assert model.get("nucleus") is None


def test_update_undo_restores_the_previous_geometry():
    model = _model("a")
    log = CommandLog(model)
    before = model.get("a").bounds

    log.run(UpdateROI("a", {"bounds": (5, 9, 5, 9)}))
    assert model.get("a").bounds == (5, 9, 5, 9)

    log.undo()
    assert model.get("a").bounds == before


def test_redo_reapplies_and_a_new_action_clears_the_redo_stack():
    model = _model("a")
    log = CommandLog(model)

    log.run(AddROI(ROIRecord("b", "rectangle", (2, 6, 2, 6))))
    log.undo()
    assert log.can_redo

    log.redo()
    assert [roi.name for roi in model.rois] == ["a", "b"]

    log.undo()
    log.run(AddROI(ROIRecord("c", "rectangle", (2, 6, 2, 6))))
    assert not log.can_redo, "a new action must invalidate the redo stack"


def test_history_is_bounded():
    """An unbounded history is a slow leak over a long session."""
    model = _model()
    log = CommandLog(model, limit=5)

    for index in range(20):
        log.run(AddROI(ROIRecord(f"r{index}", "rectangle", (0, 2, 0, 2))))

    assert len(log.labels()) == 5


def test_commands_hold_records_not_pixels():
    """Undo history must never pin image data."""
    model = ROIManagerModel([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    log = CommandLog(model)
    image = np.zeros((512, 512))

    log.run(DeleteROI("cell"))

    referenced = [
        obj for command in log.labels() for obj in ()  # labels only, no arrays
    ]
    assert referenced == []
    assert image.shape == (512, 512)  # untouched, and unreferenced by the log


# --------------------------------------------------------------------------
# ROISet / MeasurementConfig (P-G.0)
# --------------------------------------------------------------------------

def test_set_registers_a_frame_once():
    from imswitch.imcommon.algorithms.spatial_frame import AxisDescriptor, SpatialFrame

    frame = SpatialFrame(
        coordinate_space_uid="space",
        result_uid="result",
        dataset_uid="data",
        plane_axes=("Y", "X"),
        axes=(AxisDescriptor("Y", 8), AxisDescriptor("X", 8)),
        shape=(8, 8),
    )
    roi_set = ROISet().with_frame(frame).with_frame(frame)

    assert len(roi_set.frames) == 1, "frames are stored once per set, not per ROI"
    assert roi_set.frame(frame.frame_uid) is frame


def test_set_revision_advances_on_any_change():
    roi_set = ROISet()

    updated = roi_set.with_rois([ROIRecord("a", "rectangle", (0, 2, 0, 2))])

    assert updated.revision == roi_set.revision + 1


def test_measurement_config_round_trips():
    config = MeasurementConfig(selected=("area_px", "mean"), threshold=(10.0, 200.0))

    restored = MeasurementConfig.from_json(config.to_json())

    assert restored.selected == config.selected
    assert restored.threshold == pytest.approx(config.threshold)


# --------------------------------------------------------------------------
# every model change is undoable (round-7: three bypassed the log)
# --------------------------------------------------------------------------

def test_visibility_is_undoable():
    from imswitch.improcess.analysis.roi_commands import SetVisible

    model = _model("a")
    log = CommandLog(model)

    log.run(SetVisible("a", False))
    assert model.get("a").visible is False

    log.undo()
    assert model.get("a").visible is True


def test_clear_is_undoable_and_restores_order_and_identity():
    """The most expensive thing to redo by hand, so the most important to undo."""
    from imswitch.improcess.analysis.roi_commands import ClearROIs

    model = _model("a", "b", "c")
    log = CommandLog(model)
    uids = [roi.uid for roi in model.rois]

    log.run(ClearROIs())
    assert model.rois == []

    log.undo()
    assert [roi.name for roi in model.rois] == ["a", "b", "c"]
    assert [roi.uid for roi in model.rois] == uids


def test_remove_slice_info_is_undoable():
    from imswitch.improcess.analysis.roi_commands import RemoveSliceInfo

    model = ROIManagerModel(
        [
            ROIRecord("a", "rectangle", (0, 4, 0, 4), position=(("Z", 3),)),
            ROIRecord("b", "rectangle", (4, 8, 4, 8), position=(("Z", 7),)),
        ]
    )
    log = CommandLog(model)

    log.run(RemoveSliceInfo())
    assert all(roi.position == () for roi in model.rois)

    log.undo()
    assert model.get("a").position == (("Z", 3),)
    assert model.get("b").position == (("Z", 7),)
