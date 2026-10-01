"""P-S: more than one named ROI set — merging, comparing, and keeping frames apart.

The acceptance criteria: two sets captured on different results coexist without
frame confusion, merging reports and resolves conflicts rather than silently
overwriting, and frames stay stored once per set.
"""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_set import (  # noqa: E402
    ROISet,
    compare_sets,
    merge_sets,
)
from imswitch.imcommon.algorithms.roi_style import ROIStyle  # noqa: E402
from imswitch.imcommon.algorithms.spatial_frame import (  # noqa: E402
    AxisDescriptor,
    SpatialFrame,
)
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.ones((16, 16), dtype=float)))
    yield widget
    widget.deleteLater()


def _roi(name, uid, bounds=(0, 4, 0, 4), **kwargs):
    return ROIRecord(name, "rectangle", bounds, uid=uid, **kwargs)


def _frame(space="space-1", shape=(16, 16)):
    return SpatialFrame(
        coordinate_space_uid=space,
        result_uid=f"result-{space}",
        dataset_uid=f"data-{space}",
        plane_axes=("Y", "X"),
        axes=(
            AxisDescriptor("Y", shape[0], 1.0, "px"),
            AxisDescriptor("X", shape[1], 1.0, "px"),
        ),
        shape=shape,
    )


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def test_a_clean_merge_adds_what_was_missing():
    target = ROISet(name="A", rois=(_roi("a", "u1"),))
    source = ROISet(name="B", rois=(_roi("a", "u1"), _roi("b", "u2", (8, 12, 8, 12))))

    merged, report = merge_sets(target, source)
    assert [roi.name for roi in merged.rois] == ["a", "b"]
    assert report.added == ("u2",)
    assert report.identical == ("u1",)
    assert report.conflicts == ()


def test_the_same_uid_with_different_geometry_is_a_conflict():
    """The same ROI, edited two ways — the only case that can lose work."""
    target = ROISet(name="A", rois=(_roi("a", "u1", (0, 4, 0, 4)),))
    source = ROISet(name="B", rois=(_roi("a", "u1", (0, 8, 0, 8)),))

    _merged, report = merge_sets(target, source)
    assert report.conflicts == ("u1",)


def test_skip_keeps_this_set_s_version():
    target = ROISet(name="A", rois=(_roi("a", "u1", (0, 4, 0, 4)),))
    source = ROISet(name="B", rois=(_roi("a", "u1", (0, 8, 0, 8)),))

    merged, report = merge_sets(target, source, on_conflict="skip")
    assert merged.rois[0].bounds == (0, 4, 0, 4)
    assert report.skipped == ("u1",)


def test_replace_takes_the_other_set_s_version():
    target = ROISet(name="A", rois=(_roi("a", "u1", (0, 4, 0, 4)),))
    source = ROISet(name="B", rois=(_roi("a", "u1", (0, 8, 0, 8)),))

    merged, report = merge_sets(target, source, on_conflict="replace")
    assert merged.rois[0].bounds == (0, 8, 0, 8)
    assert report.replaced == ("u1",)


def test_keep_both_mints_a_new_identity_for_the_incoming_one():
    """Two different regions cannot share a uid without one becoming unreachable."""
    target = ROISet(name="A", rois=(_roi("a", "u1", (0, 4, 0, 4)),))
    source = ROISet(name="B", rois=(_roi("a", "u1", (0, 8, 0, 8)),))

    merged, _report = merge_sets(target, source, on_conflict="keep-both")
    assert len(merged.rois) == 2
    assert len({roi.uid for roi in merged.rois}) == 2
    assert [roi.name for roi in merged.rois] == ["a", "a_1"]


def test_a_shared_name_under_different_uids_is_only_a_rename():
    """A name is display text and was never the identity."""
    target = ROISet(name="A", rois=(_roi("cell", "u1"),))
    source = ROISet(name="B", rois=(_roi("cell", "u2", (8, 12, 8, 12)),))

    merged, report = merge_sets(target, source)
    assert [roi.name for roi in merged.rois] == ["cell", "cell_1"]
    assert report.renamed == (("cell", "cell_1"),)
    assert report.conflicts == ()


def test_a_display_only_difference_is_not_a_conflict():
    """A changed colour is not a changed region."""
    styled = _roi("a", "u1", style=ROIStyle(stroke_color="#ff0000"))
    target = ROISet(name="A", rois=(_roi("a", "u1"),))
    source = ROISet(name="B", rois=(styled,))

    _merged, report = merge_sets(target, source)
    assert report.conflicts == ()
    assert report.identical == ("u1",)


def test_a_merge_brings_the_frames_the_incoming_rois_point_at():
    """An unresolvable frame_uid reads as "no provenance", not as a bug."""
    other = _frame("space-2")
    target = ROISet(name="A", rois=(_roi("a", "u1"),), frames=(_frame(),))
    source = ROISet(
        name="B",
        rois=(_roi("b", "u2", frame_uid=other.frame_uid),),
        frames=(other,),
    )

    merged, _report = merge_sets(target, source)
    assert merged.frame(other.frame_uid) is not None
    assert len(merged.frames) == 2


def test_frames_are_stored_once_however_many_rois_point_at_them():
    frame = _frame()
    rois = tuple(
        _roi(f"r{i}", f"u{i}", frame_uid=frame.frame_uid) for i in range(20)
    )
    target = ROISet(name="A", frames=(frame,))
    source = ROISet(name="B", rois=rois, frames=(frame,))

    merged, _report = merge_sets(target, source)
    assert len(merged.rois) == 20
    assert len(merged.frames) == 1


def test_an_unknown_conflict_policy_is_refused():
    with pytest.raises(ValueError, match="conflict policy"):
        merge_sets(ROISet(), ROISet(), on_conflict="whatever")


def test_the_report_summarises_itself_even_when_nothing_happened():
    _merged, report = merge_sets(ROISet(), ROISet())
    assert report.summary == "nothing to merge"


# --------------------------------------------------------------------------
# comparing
# --------------------------------------------------------------------------

def test_compare_keys_on_identity_not_on_name():
    """Comparing by name would report a rename as two unrelated ROIs."""
    left = ROISet(rois=(_roi("before", "u1"),))
    right = ROISet(rois=(_roi("after", "u1"),))

    report = compare_sets(left, right)
    assert report.identical == ("u1",)
    assert report.only_in_left == ()


def test_compare_separates_missing_from_changed():
    left = ROISet(rois=(_roi("a", "u1"), _roi("b", "u2")))
    right = ROISet(rois=(_roi("a", "u1", (0, 9, 0, 9)), _roi("c", "u3")))

    report = compare_sets(left, right)
    assert report.different == ("u1",)
    assert report.only_in_left == ("u2",)
    assert report.only_in_right == ("u3",)
    assert "1 differing" in report.summary


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------

def test_a_new_set_starts_empty_and_leaves_the_old_one_intact(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")

    assert panel.table.rowCount() == 0
    assert panel._set.name == "second"
    assert [roi.name for roi in panel._sets[0].rois] == ["a"]


def test_switching_back_restores_the_first_set(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")
    panel.add_rois([ROIRecord("b", "rectangle", (8, 12, 8, 12))])

    panel.setCombo.setCurrentIndex(0)
    assert [roi.name for roi in panel._model.rois] == ["a"]
    panel.setCombo.setCurrentIndex(1)
    assert [roi.name for roi in panel._model.rois] == ["b"]


def test_two_sets_keep_their_own_frames(panel):
    """The acceptance criterion: no frame confusion between results."""
    first = _frame("space-1")
    second = _frame("space-2")
    panel._set = panel._set.with_frame(first)
    panel.new_set("second")
    panel._set = panel._set.with_frame(second)

    assert [f.coordinate_space_uid for f in panel._sets[0].frames] == ["space-1"]
    assert [f.coordinate_space_uid for f in panel._sets[1].frames] == ["space-2"]


def test_two_sets_keep_their_own_measurement_configuration(panel):
    from imswitch.imcommon.algorithms.roi_set import MeasurementConfig

    panel.set_measurement_config(MeasurementConfig(selected=("mean",)))
    panel.new_set("second")
    panel.set_measurement_config(MeasurementConfig(selected=("area_px",)))

    assert panel._sets[0].measurement_config.selected == ("mean",)
    assert panel._sets[1].measurement_config.selected == ("area_px",)


def test_duplicating_a_set_keeps_every_roi_s_identity(panel):
    """Otherwise comparing the copy with the original says nothing."""
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    original_uid = panel._model.rois[0].uid
    panel.duplicate_set()

    assert panel._set.name == "ROIs_copy"
    assert panel._model.rois[0].uid == original_uid
    assert panel._set.uid != panel._sets[0].uid   # the *set* is a new one


def test_deleting_the_only_set_is_refused(panel):
    panel.delete_set()
    assert len(panel._sets) == 1
    assert "only set" in panel.summaryLabel.text()


def test_deleting_a_set_moves_to_a_remaining_one(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")
    panel.delete_set()

    assert len(panel._sets) == 1
    assert [roi.name for roi in panel._model.rois] == ["a"]


def test_merging_through_the_panel_reports_what_it_did(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")
    panel.add_rois([ROIRecord("b", "rectangle", (8, 12, 8, 12))])

    panel.merge_set(0)
    assert [roi.name for roi in panel._model.rois] == ["b", "a"]
    assert "1 added" in panel.summaryLabel.text()


def test_a_merge_that_renames_says_which_roi_it_renamed(panel):
    panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")
    panel.add_rois([ROIRecord("cell", "rectangle", (8, 12, 8, 12))])

    panel.merge_set(0)
    assert "Renamed 'cell' to 'cell_1'" in panel.summaryLabel.text()


def test_comparing_through_the_panel_reports_both_sides(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.duplicate_set()
    panel.add_rois([ROIRecord("b", "rectangle", (8, 12, 8, 12))])

    panel.compare_set(0)
    text = panel.summaryLabel.text()
    assert "1 identical" in text
    assert "1 only in the first" in text


def test_switching_sets_does_not_leave_an_undo_that_targets_the_other_one(panel):
    """Commands hold names, which mean different things in different sets."""
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")

    panel._commands.undo()   # must be a no-op, not a delete in the other set
    assert [roi.name for roi in panel._sets[0].rois] == ["a"]
    assert panel._model.rois == []


def test_the_combo_shows_each_set_and_its_size(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.new_set("second")

    assert panel.setCombo.count() == 2
    assert panel.setCombo.itemText(0) == "ROIs (1)"
    assert panel.setCombo.itemText(1) == "second (0)"


def test_renaming_a_set_onto_a_taken_name_uniquifies_it(panel, monkeypatch):
    panel.new_set("second")
    panel.setCombo.setCurrentIndex(0)
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText", staticmethod(lambda *a, **k: ("second", True))
    )
    panel.rename_set()
    assert panel._set.name == "second_1"


def test_renaming_a_set_to_the_name_it_already_has_changes_nothing(panel, monkeypatch):
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText", staticmethod(lambda *a, **k: ("ROIs", True))
    )
    panel.rename_set()
    assert panel._set.name == "ROIs"
