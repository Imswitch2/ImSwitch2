"""P-U: the undo stack, transactional import, batch edits and autosave.

The acceptance criteria: undo restores geometry, style and position exactly; a
failed import leaves the set untouched; the stack is bounded and retains no
image data.
"""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_style import ROIStyle  # noqa: E402
from imswitch.improcess.analysis.roi_commands import (  # noqa: E402
    AddROI,
    CommandLog,
    DeleteROI,
    ImportROIs,
    SetProperties,
)
from imswitch.improcess.analysis.roi_manager import ROIManagerModel  # noqa: E402
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


# --------------------------------------------------------------------------
# the stack
# --------------------------------------------------------------------------

def test_undo_restores_geometry_style_and_position_exactly():
    """The acceptance criterion, field by field."""
    original = ROIRecord(
        "cell", "polygon", (1, 9, 2, 8), uid="u1",
        vertices=((1.5, 2.5), (9.0, 2.5), (5.0, 8.0)),
        position=(("Z", 3),),
        style=ROIStyle(stroke_color="#ff0000", stroke_width=2.5),
        group=4,
        properties=(("stain", "DAPI"),),
    )
    model = ROIManagerModel([original])
    log = CommandLog(model)

    log.run(DeleteROI("cell"))
    assert model.rois == []

    log.undo()
    restored = model.rois[0]
    assert restored == original


def test_the_stack_is_bounded():
    """An unbounded history of a long session is a slow memory leak."""
    model = ROIManagerModel()
    log = CommandLog(model, limit=5)
    for index in range(20):
        log.run(AddROI(ROIRecord(f"r{index}", "rectangle", (0, 2, 0, 2))))

    assert len(log.labels()) == 5


def test_a_new_action_discards_what_was_undone():
    model = ROIManagerModel()
    log = CommandLog(model)
    log.run(AddROI(ROIRecord("a", "rectangle", (0, 2, 0, 2))))
    log.undo()
    assert log.can_redo

    log.run(AddROI(ROIRecord("b", "rectangle", (0, 2, 0, 2))))
    assert not log.can_redo


def test_the_log_says_what_would_be_undone():
    model = ROIManagerModel()
    log = CommandLog(model)
    assert log.undo_label == ""

    log.run(DeleteROI("nothing"))
    assert log.undo_label == "Delete ROI"
    log.undo()
    assert log.redo_label == "Delete ROI"


def test_the_stack_holds_records_never_pixels():
    """A history that pinned image data would keep whole frames alive."""
    model = ROIManagerModel([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    log = CommandLog(model)
    log.run(DeleteROI("a"))

    for command in log._done:
        for value in vars(command).values():
            assert not isinstance(value, np.ndarray), (
                f"{type(command).__name__} holds a numpy array"
            )


# --------------------------------------------------------------------------
# transactional import
# --------------------------------------------------------------------------

def test_an_import_is_one_step_of_undo_not_two_hundred():
    model = ROIManagerModel()
    log = CommandLog(model)
    rois = [ROIRecord(f"r{i}", "rectangle", (i, i + 2, 0, 2)) for i in range(20)]

    log.run(ImportROIs(rois=tuple(rois)))
    assert len(model.rois) == 20

    log.undo()
    assert model.rois == []


def test_a_failed_import_leaves_the_set_untouched():
    """Half an import is the worst outcome: unfindable and un-undoable."""
    model = ROIManagerModel([ROIRecord("kept", "rectangle", (0, 4, 0, 4))])
    log = CommandLog(model)

    class _Exploding(ROIRecord):
        pass

    good = ROIRecord("good", "rectangle", (5, 9, 5, 9))
    bad = object()   # not a record at all

    with pytest.raises(Exception):
        log.run(ImportROIs(rois=(good, bad)))

    assert [roi.name for roi in model.rois] == ["kept"]


def test_import_renames_a_collision_by_default():
    model = ROIManagerModel([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    command = ImportROIs(rois=(ROIRecord("cell", "rectangle", (5, 9, 5, 9)),))
    command.do(model)

    assert [roi.name for roi in model.rois] == ["cell", "cell_1"]
    assert command.added == 1


def test_import_can_skip_a_collision():
    model = ROIManagerModel([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    command = ImportROIs(
        rois=(ROIRecord("cell", "rectangle", (5, 9, 5, 9)),), on_conflict="skip"
    )
    command.do(model)

    assert len(model.rois) == 1
    assert model.rois[0].bounds == (0, 4, 0, 4)
    assert command.skipped == 1


def test_import_can_replace_a_collision():
    model = ROIManagerModel([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
    command = ImportROIs(
        rois=(ROIRecord("cell", "rectangle", (5, 9, 5, 9)),), on_conflict="replace"
    )
    command.do(model)

    assert len(model.rois) == 1
    assert model.rois[0].bounds == (5, 9, 5, 9)
    assert command.replaced == 1


def test_an_unknown_conflict_policy_is_refused_before_anything_is_touched():
    model = ROIManagerModel([ROIRecord("kept", "rectangle", (0, 4, 0, 4))])
    with pytest.raises(ValueError, match="conflict policy"):
        ImportROIs(rois=(), on_conflict="whatever").do(model)
    assert [roi.name for roi in model.rois] == ["kept"]


# --------------------------------------------------------------------------
# batch property changes
# --------------------------------------------------------------------------

def test_a_batch_restyle_is_one_step_of_undo():
    rois = [ROIRecord(f"r{i}", "rectangle", (i, i + 2, 0, 2)) for i in range(5)]
    model = ROIManagerModel(rois)
    log = CommandLog(model)

    log.run(SetProperties(
        names=tuple(roi.name for roi in rois),
        changes={"style": ROIStyle(stroke_color="#00ff00"), "group": 2},
    ))
    assert all(roi.group == 2 for roi in model.rois)

    log.undo()
    assert all(roi.group == 0 for roi in model.rois)
    assert all(roi.style is None for roi in model.rois)


def test_a_batch_change_may_not_touch_geometry():
    """A batch that could move geometry is one that can silently ruin a set."""
    with pytest.raises(ValueError, match="move geometry"):
        SetProperties(names=("a",), changes={"bounds": (0, 1, 0, 1)})


def test_a_batch_change_does_not_bump_a_measurement_revision():
    """Restyling must not invalidate every cached measurement."""
    model = ROIManagerModel([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    before = model.rois[0].revision

    SetProperties(names=("a",), changes={"group": 3}).do(model)
    assert model.rois[0].revision == before


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------

def test_the_buttons_say_what_they_would_undo(panel):
    assert not panel.undoButton.isEnabled()
    assert "Nothing to undo" in panel.undoButton.toolTip()

    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    assert panel.undoButton.isEnabled()
    assert "Import ROIs" in panel.undoButton.toolTip()

    panel.undo()
    assert not panel.undoButton.isEnabled()
    assert panel.redoButton.isEnabled()
    assert "Import ROIs" in panel.redoButton.toolTip()


def test_undo_and_redo_through_the_panel(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.table.selectAll()
    panel.delete_selected()
    assert panel._model.rois == []

    panel.undo()
    assert [roi.name for roi in panel._model.rois] == ["a"]
    assert "Undone: Delete" in panel.summaryLabel.text()

    panel.redo()
    assert panel._model.rois == []
    assert "Redone: Delete" in panel.summaryLabel.text()


def test_a_failed_import_through_the_panel_says_nothing_was_added(panel):
    panel.add_rois([ROIRecord("kept", "rectangle", (0, 4, 0, 4))])
    added = panel.add_rois([ROIRecord("good", "rectangle", (5, 9, 5, 9)), object()])

    assert added == 0
    assert [roi.name for roi in panel._model.rois] == ["kept"]
    assert "nothing was added" in panel.summaryLabel.text()


def test_an_edit_asks_for_an_autosave(panel):
    saves = []
    panel.sigStateChanged.connect(lambda: saves.append(True))

    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    assert panel._autosaveTimer.isActive()   # debounced, not immediate
    panel._autosave()
    assert saves == [True]


def test_measuring_without_editing_does_not_ask_for_an_autosave(panel):
    """Otherwise scrolling a stack would rewrite the state store all day."""
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel._autosaveTimer.stop()

    panel.refresh_stats()
    assert not panel._autosaveTimer.isActive()


def test_undo_redo_are_catalogued_shortcuts():
    from imswitch.improcess.controller.shortcuts import improcess_shortcut_defaults

    defaults = improcess_shortcut_defaults()
    assert defaults["roi.undo"] == "Ctrl+Z"
    # Ctrl+Shift+Z was already this catalog's "Channels"; taking it would have
    # made one of the two silently unreachable.
    assert defaults["roi.redo"] == "Ctrl+Y"
    bound = [key for key in defaults.values() if key]
    assert len(bound) == len(set(bound))


def test_the_shortcuts_bind_to_the_panel_that_is_open(panel, qapp):
    from imswitch.imcommon.controller.ShortcutManager import ShortcutManager
    from imswitch.improcess.controller.shortcuts import (
        register_roi_manager_shortcuts,
    )

    manager = ShortcutManager()
    register_roi_manager_shortcuts(manager, panel)
    actions = manager.getAllActions()

    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    actions["roi.undo"].callback()
    assert panel._model.rois == []
    actions["roi.redo"].callback()
    assert [roi.name for roi in panel._model.rois] == ["a"]
