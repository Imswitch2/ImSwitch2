"""P-6.3: where ROI sets live between sessions, and what refuses to load.

The two behaviours that are easy to get wrong and expensive when wrong: a
session that never opened the panel must not erase what the last one saved,
and a spill file that no longer matches its marker must be refused rather than
half-loaded.
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_set import MeasurementConfig, ROISet  # noqa: E402
from imswitch.imcommon.algorithms.spatial_frame import (  # noqa: E402
    AxisDescriptor,
    SpatialFrame,
)
from imswitch.improcess.controller.ImProcessMainController import (  # noqa: E402
    _ROIManagerStateAdapter,
)
from imswitch.improcess.model.roi_persistence import (  # noqa: E402
    SPILL_CAP_ROIS,
    SPILL_FILENAME,
    SPILLED_KEY,
    ROIStateError,
    sets_from_payload,
    sets_payload,
    should_spill,
    spill_marker,
    unpack,
    write_spill,
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


def _frame(space="space-1"):
    return SpatialFrame(
        coordinate_space_uid=space,
        result_uid="r",
        dataset_uid="d",
        plane_axes=("Y", "X"),
        axes=(AxisDescriptor("Y", 16, 1.0, "px"), AxisDescriptor("X", 16, 1.0, "px")),
        shape=(16, 16),
    )


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------

def test_sets_round_trip_through_the_payload():
    frame = _frame()
    sets = [
        ROISet(
            name="one",
            rois=(ROIRecord("a", "rectangle", (0, 4, 0, 4), frame_uid=frame.frame_uid),),
            frames=(frame,),
            measurement_config=MeasurementConfig(selected=("mean",)),
        ),
        ROISet(name="two"),
    ]
    restored, active, options, _dropped = sets_from_payload(
        sets_payload(sets, 1, {"labels": True})
    )

    assert [s.name for s in restored] == ["one", "two"]
    assert active == 1
    assert options == {"labels": True}
    assert restored[0].measurement_config.selected == ("mean",)


def test_an_out_of_range_active_index_is_clamped_not_trusted():
    payload = sets_payload([ROISet(name="only")], 7, {})
    _sets, active, _options, _dropped = sets_from_payload(payload)
    assert active == 0


def test_an_empty_payload_yields_one_empty_set():
    _sets, _active, _options, _dropped = sets_from_payload({"sets": []})
    assert _sets and _sets[0].rois == ()


# --------------------------------------------------------------------------
# the cap and the spill file
# --------------------------------------------------------------------------

def test_a_small_set_stays_in_the_state_store():
    payload = sets_payload([ROISet(rois=(ROIRecord("a", "rectangle", (0, 4, 0, 4)),))], 0)
    assert not should_spill(payload)


def test_too_many_rois_spills():
    many = tuple(
        ROIRecord(f"r{i}", "rectangle", (i, i + 1, 0, 1), uid=f"u{i}")
        for i in range(SPILL_CAP_ROIS + 1)
    )
    assert should_spill(sets_payload([ROISet(rois=many)], 0))


def test_a_few_enormous_masks_spill_too():
    """Measured on the serialised size, because that is the cost being guarded.

    A handful of segmentation masks weighs more than the two-thousand-ROI
    count cap ever sees — which is why both limits exist rather than one.
    """
    from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask

    rng = np.random.default_rng(0)
    rois = tuple(
        roi_from_mask(
            rng.random((1500, 1500)) > 0.5,   # incompressible, so genuinely large
            name=f"big{i}",
            offset=(0, 0),
        )
        for i in range(3)
    )
    payload = sets_payload([ROISet(rois=rois)], 0)
    assert len(rois) < SPILL_CAP_ROIS      # the count cap is nowhere near
    assert should_spill(payload)


def test_the_marker_keeps_a_checksum_and_the_names_not_a_copy(tmp_path):
    payload = sets_payload([ROISet(name="cells"), ROISet(name="nuclei")], 0)
    marker = spill_marker(payload)

    assert marker[SPILLED_KEY] is True
    assert marker["file"] == SPILL_FILENAME
    assert marker["names"] == ["cells", "nuclei"]
    assert "sets" not in marker       # never two copies of the same sets
    assert len(marker["checksum"]) == 64


def test_a_spilled_set_round_trips_through_the_file(tmp_path):
    payload = sets_payload([ROISet(name="cells", rois=(
        ROIRecord("a", "rectangle", (0, 4, 0, 4), uid="u1"),
    ))], 0)
    write_spill(payload, tmp_path)
    sets, _active, _options, _dropped = unpack(spill_marker(payload), tmp_path)
    assert [roi.name for roi in sets[0].rois] == ["a"]


def test_a_missing_spill_file_says_which_sets_are_gone(tmp_path):
    marker = spill_marker(sets_payload([ROISet(name="cells")], 0))
    with pytest.raises(ROIStateError, match="cells"):
        unpack(marker, tmp_path)


def test_an_edited_spill_file_is_refused_rather_than_half_loaded(tmp_path):
    """Restoring ROIs that may belong to another session is the failure here."""
    payload = sets_payload([ROISet(name="cells")], 0)
    marker = spill_marker(payload)
    write_spill(payload, tmp_path)

    path = tmp_path / SPILL_FILENAME
    tampered = json.loads(path.read_text())
    tampered["sets"][0]["roi_set"]["name"] = "someone else's"
    path.write_text(json.dumps(tampered))

    with pytest.raises(ROIStateError, match="edited or replaced"):
        unpack(marker, tmp_path)


def test_the_checksum_does_not_depend_on_key_order(tmp_path):
    payload = sets_payload([ROISet(name="cells")], 0)
    marker = spill_marker(payload)
    write_spill(dict(reversed(list(payload.items()))), tmp_path)
    # Reordering a dict does not change what it means, so the load succeeds.
    unpack(marker, tmp_path)


# --------------------------------------------------------------------------
# the panel's own state
# --------------------------------------------------------------------------

def test_panel_state_round_trips(panel):
    frame = panel._current_frame()
    panel._set = panel._set.with_frame(frame)
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4), frame_uid=frame.frame_uid)])
    panel.new_set("second")
    panel.labelsCheck.setChecked(True)

    state = panel.roiState()
    panel._sets = [ROISet(name="wiped")]
    panel._activeIndex = 0
    panel._loadActiveSet()

    panel.setRoiState(state)
    assert [s.name for s in panel._sets] == ["ROIs", "second"]
    assert panel._set.name == "second"
    assert panel.labelsCheck.isChecked()


def test_an_roi_with_no_stored_frame_is_not_restored(panel):
    """A region with no plane to belong to would measure against anything."""
    state = sets_payload(
        [ROISet(name="one", rois=(
            ROIRecord("orphan", "rectangle", (0, 4, 0, 4), frame_uid="frame-gone"),
            ROIRecord("fine", "rectangle", (5, 9, 5, 9)),
        ))],
        0,
    )
    panel.setRoiState(state)

    assert [roi.name for roi in panel._model.rois] == ["fine"]
    assert "no stored frame" in panel.summaryLabel.text()


# --------------------------------------------------------------------------
# the controller-owned adapter (A-09)
# --------------------------------------------------------------------------

class _View:
    def __init__(self, panel=None):
        self.roiManagerWidget = panel


def test_a_session_that_never_opened_the_panel_does_not_erase_its_state():
    """The whole reason the adapter is controller-owned rather than widget-owned."""
    adapter = _ROIManagerStateAdapter(_View(panel=None))
    saved = sets_payload([ROISet(name="cells", rois=(
        ROIRecord("a", "rectangle", (0, 4, 0, 4)),
    ))], 0)

    adapter.setWidgetState(saved)
    written = adapter.getWidgetState()

    assert written["sets"][0]["roi_set"]["name"] == "cells"
    assert written["sets"][0]["roi_set"]["rois"][0]["name"] == "a"


def test_state_restored_before_the_panel_exists_is_applied_when_it_does(panel):
    adapter = _ROIManagerStateAdapter(_View(panel=None))
    adapter.setWidgetState(
        sets_payload([ROISet(name="cells", rois=(
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
        ))], 0)
    )

    adapter.applyStashTo(panel)
    assert [roi.name for roi in panel._model.rois] == ["a"]
    # Applied once: a second call must not re-apply over later edits.
    panel.clear_rois()
    adapter.applyStashTo(panel)
    assert panel._model.rois == []


def test_the_adapter_reads_the_panel_when_there_is_one(panel):
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    adapter = _ROIManagerStateAdapter(_View(panel=panel))
    assert adapter.getWidgetState()["sets"][0]["roi_set"]["rois"][0]["name"] == "a"


def test_an_unreadable_restore_is_reported_not_raised(panel):
    """Startup must not fail because a saved set is corrupt."""
    logged = []
    adapter = _ROIManagerStateAdapter(
        _View(panel=panel),
        logger=type("L", (), {"warning": lambda _s, m, **k: logged.append(m)})(),
    )
    adapter.setWidgetState({SPILLED_KEY: True, "names": ["cells"], "checksum": "x"})
    assert logged and "cells" in logged[0]


# --------------------------------------------------------------------------
# P-U — crash-recovery autosave
# --------------------------------------------------------------------------

def test_autosave_writes_through_the_same_adapter_shutdown_uses(panel, monkeypatch):
    """One payload, one place: a recovery file of its own would raise the
    question of which of the two is newer."""
    from imswitch.improcess.controller import ImProcessMainController as module

    saved = []

    class _Persistence:
        def saveWidgetState(self, key, name="default"):
            saved.append((key, name))
            return True

    monkeypatch.setattr(
        module, "_ROI_MANAGER_STATE_KEY", "ImProcessROIManager", raising=False
    )
    monkeypatch.setattr(
        "imswitch.imcommon.model.getWidgetStatePersistence",
        lambda: _Persistence(),
    )

    controller = module.ImProcessMainController.__new__(
        module.ImProcessMainController
    )
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None
    )
    controller._autosaveROIState()

    assert saved == [("ImProcessROIManager", "default")]


def test_an_autosave_failure_never_reaches_the_user(panel, monkeypatch):
    """Losing an autosave is a shame; a dialog mid-edit is worse."""
    from imswitch.improcess.controller import ImProcessMainController as module

    def explode():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(
        "imswitch.imcommon.model.getWidgetStatePersistence", explode
    )
    logged = []
    controller = module.ImProcessMainController.__new__(
        module.ImProcessMainController
    )
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: logged.append(a)
    )
    controller._autosaveROIState()      # must not raise
    assert logged
