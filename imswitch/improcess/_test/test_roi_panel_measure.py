"""P-4: Measure, Multi Measure, Multi Plot and the across-results gate.

The row shapes are covered in test_roi_report.py; what is checked here is that
the *panel* pushes them at the right moment, refuses what A-13 says it must,
and never touches a widget from the measurement thread.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_set import MeasurementConfig  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class _Result:
    """The parts of a ProcessingResult that a frame is read from."""

    def __init__(self, name, data, *, labels=None, scales=None, unit="px", space=""):
        self.name = name
        self.data = np.asarray(data)
        self.axis_labels = labels or (["Y", "X"] if self.data.ndim == 2 else ["Z", "Y", "X"])
        self.axis_scales = scales or [1.0] * self.data.ndim
        self.scale_unit = unit
        self.coordinate_space_uid = space
        self.result_uid = f"result-{name}"
        self.dataset_uid = f"data-{name}"
        self.identity_kind = "minted" if space else "derived"


def _panel(qapp, data=None, *, labels=None):
    image = np.arange(256, dtype=float).reshape(16, 16) if data is None else data
    viewer = _Viewer(image)
    layer = viewer.layers[0]
    if labels is not None:
        layer.metadata["axis_labels"] = list(labels)
        layer.metadata["plane_axes"] = tuple(labels[-2:])
        layer.metadata["axes"] = [
            {"label": label, "size": int(size), "scale": 1.0, "unit": "px"}
            for label, size in zip(labels, np.asarray(image).shape)
        ]
        viewer.dims.axis_labels = tuple(labels)
        viewer.dims.current_step = tuple(0 for _ in labels)
    return ROIManagerWidget(viewer)


def _pushes(panel):
    captured = []
    panel.sigResultPushed.connect(lambda columns, rows: captured.append((columns, rows)))
    return captured


# --------------------------------------------------------------------------
# P-4.1 — Measure
# --------------------------------------------------------------------------

def test_measure_with_no_selection_measures_every_roi(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        captured = _pushes(panel)
        panel.measure()

        assert len(captured) == 1
        columns, rows = captured[0]
        assert [row["roi"] for row in rows] == ["a", "b"]
        assert columns[:4] == ["source", "kind", "roi", "roi_uid"]
    finally:
        panel.deleteLater()


def test_measure_with_a_selection_measures_only_that_roi(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        panel.select_roi_by_uid(panel._model.rois[1].uid)
        captured = _pushes(panel)
        panel.measure()

        assert [row["roi"] for row in captured[0][1]] == ["b"]
    finally:
        panel.deleteLater()


def test_measure_skips_hidden_rois(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("shown", "rectangle", (0, 4, 0, 4)),
            ROIRecord("hidden", "rectangle", (8, 12, 8, 12), visible=False),
        ])
        captured = _pushes(panel)
        panel.measure()
        assert [row["roi"] for row in captured[0][1]] == ["shown"]
    finally:
        panel.deleteLater()


def test_one_unmeasurable_roi_does_not_cost_the_batch(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("good", "rectangle", (0, 4, 0, 4)),
            ROIRecord("outside", "rectangle", (100, 120, 100, 120)),
        ])
        captured = _pushes(panel)
        panel.measure()

        assert [row["roi"] for row in captured[0][1]] == ["good"]
        assert "could not be measured" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_nothing_is_pushed_when_there_is_nothing_to_measure(qapp):
    panel = _panel(qapp)
    try:
        captured = _pushes(panel)
        panel.measure()
        assert captured == []
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-4.2 — Multi Measure
# --------------------------------------------------------------------------

def test_multi_measure_produces_one_row_per_roi_per_plane(qapp):
    stack = np.arange(3 * 16 * 16, dtype=float).reshape(3, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        captured = _pushes(panel)
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()

        assert captured, "no rows reached the Results dock"
        _, rows = captured[-1]
        assert len(rows) == 6  # 3 planes x 2 ROIs
        assert [(row["Z"], row["roi"]) for row in rows] == [
            (0, "a"), (0, "b"), (1, "a"), (1, "b"), (2, "a"), (2, "b"),
        ]
        # Each plane really was measured on its own pixels.
        means = [row["mean"] for row in rows if row["roi"] == "a"]
        assert means[0] < means[1] < means[2]
    finally:
        panel.deleteLater()


def test_multi_measure_refuses_an_image_with_no_stack_axis(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        captured = _pushes(panel)
        panel.multi_measure()
        assert captured == []
        assert "no stack axis" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_the_progress_bar_is_hidden_again_when_a_run_ends(qapp):
    stack = np.zeros((3, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()
        assert not panel.progressBar.isVisible()
        assert panel.measureButton.isEnabled()
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-4.3 — Multi Plot
# --------------------------------------------------------------------------

def test_multi_plot_pushes_one_curve_per_roi(qapp, monkeypatch):
    stack = np.arange(3 * 16 * 16, dtype=float).reshape(3, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()
        assert panel.multiPlotButton.isEnabled()

        monkeypatch.setattr(
            QtWidgets.QInputDialog, "getItem",
            staticmethod(lambda *a, **k: ("mean", True)),
        )
        plots = []
        panel.sigPlotPushed.connect(plots.append)
        panel.multi_plot()

        assert len(plots) == 1
        assert [series.name for series in plots[0].series] == ["a", "b"]
        assert plots[0].x_label == "Z"
    finally:
        panel.deleteLater()


def test_multi_plot_before_multi_measure_says_so(qapp):
    panel = _panel(qapp)
    try:
        plots = []
        panel.sigPlotPushed.connect(plots.append)
        panel.multi_plot()
        assert plots == []
        assert "Multi Measure" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-4.4 — across results, gated by A-13 and the Q-08 preflight
# --------------------------------------------------------------------------

def test_a_matching_result_is_measured_without_asking(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        # Same pixel grid as the layer the ROI was captured on.
        frame = panel._current_frame()
        results = [
            _Result("one", np.ones((16, 16)), space=frame.coordinate_space_uid),
            _Result("two", np.full((16, 16), 3.0), space=frame.coordinate_space_uid),
        ]
        panel.setAvailableResults(results)
        captured = _pushes(panel)

        def refuse(entries):
            raise AssertionError("a compatible batch must not ask")

        panel.measure_across_results(confirm=refuse)

        _, rows = captured[-1]
        assert sorted(row["source"] for row in rows) == ["one", "two"]
        assert {row["mean"] for row in rows} == {1.0, 3.0}
    finally:
        panel.deleteLater()


def test_a_clippable_batch_requires_the_preflight_optin(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        frame = panel._current_frame()
        # Same grid, different extent -> clippable.
        results = [_Result("wide", np.ones((32, 32)), space=frame.coordinate_space_uid)]
        panel.setAvailableResults(results)
        captured = _pushes(panel)

        asked = []

        def decline(entries):
            asked.append(list(entries))
            return None

        panel.measure_across_results(confirm=decline)
        assert asked and asked[0][0][2] == "clippable"
        assert captured == []

        panel.measure_across_results(confirm=lambda entries: list(entries))
        _, rows = captured[-1]
        assert rows[0]["geometry_match"] == "clippable"
    finally:
        panel.deleteLater()


def test_an_incompatible_result_is_never_measured_even_if_ticked(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        # A different coordinate space: unrelated data that happens to be the
        # same size, which is exactly what the ladder exists to reject.
        results = [_Result("other", np.ones((16, 16)), space="space-unrelated")]
        panel.setAvailableResults(results)
        captured = _pushes(panel)

        panel.measure_across_results(confirm=lambda entries: list(entries))
        assert captured == []
        assert "incompatible" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_the_preflight_lists_a_verdict_per_roi_and_result(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        frame = panel._current_frame()
        panel.setAvailableResults([
            _Result("same", np.ones((16, 16)), space=frame.coordinate_space_uid),
            _Result("wide", np.ones((32, 32)), space=frame.coordinate_space_uid),
        ])
        entries = panel.preflight_entries()
        assert len(entries) == 4
        assert {(name, verdict) for name, _, verdict in entries} == {
            ("same", "pixel-compatible"),
            ("wide", "clippable"),
        }
    finally:
        panel.deleteLater()


def test_the_across_results_button_follows_the_result_list(qapp):
    panel = _panel(qapp)
    try:
        assert not panel.acrossResultsButton.isEnabled()
        panel.setAvailableResults([_Result("one", np.ones((4, 4)))])
        assert not panel.acrossResultsButton.isEnabled()
        panel.setAvailableResults([
            _Result("one", np.ones((4, 4))),
            _Result("two", np.ones((4, 4))),
        ])
        assert panel.acrossResultsButton.isEnabled()
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-4.7 — wide-form export
# --------------------------------------------------------------------------

def test_export_writes_the_multi_measure_in_wide_form(qapp, tmp_path, monkeypatch):
    stack = np.arange(3 * 16 * 16, dtype=float).reshape(3, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()

        monkeypatch.setattr(
            QtWidgets.QInputDialog, "getItem",
            staticmethod(lambda *a, **k: ("mean", True)),
        )
        target = tmp_path / "multi.csv"
        panel.write_csv(target)

        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "Z,a,b"
        assert len(lines) == 4  # header + one row per plane
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# thread safety
# --------------------------------------------------------------------------

def test_the_worker_never_touches_a_widget(qapp):
    """The runner's callbacks fire on the worker; only the slots touch Qt."""
    import threading

    stack = np.zeros((4, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        main = threading.get_ident()
        emitted = []
        panel.sigJobFinished.connect(
            lambda _result: emitted.append(threading.get_ident())
        )
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()

        assert emitted, "the finished signal never arrived"
        assert emitted[0] == main
    finally:
        panel.deleteLater()


def test_closing_the_panel_stops_its_measurement_thread(qapp):
    stack = np.zeros((8, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    panel.multi_measure("Z")
    panel.close()
    assert not panel._runner.busy
    panel.deleteLater()


def test_a_second_run_replaces_the_rows_rather_than_appending(qapp):
    """Supersession itself is the runner's contract (test_roi_jobs); what the
    panel owes is that its state is the *last* run, not an accumulation."""
    stack = np.arange(4 * 16 * 16, dtype=float).reshape(4, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        for _ in range(2):
            panel.multi_measure("Z")
            assert panel._runner.wait(5.0)
            qapp.processEvents()
        assert len(panel._multiRows) == 4  # 4 planes x 1 ROI, not 8
    finally:
        panel.deleteLater()


def test_a_discarded_run_clears_the_progress_but_keeps_the_numbers(qapp):
    stack = np.zeros((4, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()
        rows = list(panel._multiRows)
        assert rows

        panel._setBusy(True, 10)
        panel._onJobDiscarded(SimpleNamespace(cancelled=True, rows=(), stale=False))
        assert not panel.progressBar.isVisible()
        assert panel._multiRows == rows
        assert "cancelled" in panel.summaryLabel.text().lower()
    finally:
        panel.deleteLater()


def test_a_dims_change_does_not_measure_a_wrong_plane(qapp):
    """The plane a row records is the plane it was measured on."""
    stack = np.arange(3 * 16 * 16, dtype=float).reshape(3, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel._viewer.dims.current_step = (2, 0, 0)
        panel.refresh_stats()
        captured = _pushes(panel)
        panel.measure()

        row = captured[0][1][0]
        assert row["Z"] == 2 if "Z" in row else True
        assert row["mean"] == pytest.approx(stack[2, 0:4, 0:4].mean())
    finally:
        panel.deleteLater()


def test_a_result_pair_may_arrive_as_a_name_result_tuple(qapp):
    """The comm channel hands out (name, result); a bare result also works."""
    panel = _panel(qapp)
    try:
        result = _Result("named", np.ones((16, 16)))
        assert panel._resultPairs([("display", result)]) == [("display", result)]
        assert panel._resultPairs([result]) == [("named", result)]
    finally:
        panel.deleteLater()


def test_measurement_config_reaches_the_pushed_rows(qapp):
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(MeasurementConfig(selected=("mean", "circularity")))
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        captured = _pushes(panel)
        panel.measure()

        row = captured[0][1][0]
        assert "mean" in row and "circularity" in row
        assert "area_px" not in row
    finally:
        panel.deleteLater()


def test_rows_say_which_result_they_came_from(qapp):
    panel = _panel(qapp)
    try:
        panel._viewer.layers[0].metadata["source_result"] = "recon-42"
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        captured = _pushes(panel)
        panel.measure()
        assert captured[0][1][0]["source"] == "recon-42"
    finally:
        panel.deleteLater()


def test_a_frame_can_be_built_from_a_result_that_was_never_displayed():
    from imswitch.improcess.analysis.roi_frame_adapter import frame_from_result

    result = SimpleNamespace(
        data=np.zeros((3, 8, 8)),
        axis_labels=["Z", "Y", "X"],
        axis_scales=[2.0, 0.5, 0.5],
        scale_unit="um",
        coordinate_space_uid="space-1",
        result_uid="result-1",
        dataset_uid="data-1",
        identity_kind="minted",
    )
    frame = frame_from_result(result)
    assert frame.plane_axes == ("Y", "X")
    assert frame.shape == (8, 8)
    assert frame.axis("Z").scale == 2.0
    assert frame.identity_kind == "minted"


def test_an_roi_is_never_measured_on_a_different_plane(qapp):
    """F-15: an XY region says nothing about an XZ slice."""
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        frame = panel._current_frame()
        # Same coordinate space, same size — but the displayed pair is X/Z.
        sideways = _Result(
            "xz",
            np.ones((16, 16, 16)),
            labels=["Y", "Z", "X"],
            space=frame.coordinate_space_uid,
        )
        panel.setAvailableResults([sideways])

        assert [verdict for _n, _r, verdict in panel.preflight_entries()] == [
            "incompatible"
        ]
        captured = _pushes(panel)
        panel.measure_across_results(confirm=lambda entries: list(entries))
        assert captured == []
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# review round 8 — live sources, verdicts, plane binding, row identity
# --------------------------------------------------------------------------

def test_an_image_with_no_token_is_snapshotted_before_a_run(qapp):
    """A-22: never measure a moving image half-and-half."""
    stack = np.zeros((3, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])  # no mutation_token
    try:
        source = panel._plane_source(panel._current_frame())
        assert source.live
        assert source.mutation_token.startswith("snapshot:")
        # The copy is ours: writing to the layer cannot reach it.
        panel._viewer.layers[0].data[0, 0, 0] = 99.0
        assert source.read_plane((("Z", 0),))[0, 0] == 0.0
    finally:
        panel.deleteLater()


def test_a_snapshot_too_large_to_copy_is_refused_not_attempted(qapp):
    stack = np.zeros((3, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.MAX_SNAPSHOT_BYTES = 8  # smaller than the array
        captured = _pushes(panel)
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.multi_measure("Z")
        assert captured == []
        assert "too large" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_a_rendered_image_keeps_its_token_and_is_not_copied(qapp):
    stack = np.zeros((3, 16, 16), dtype=float)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel._viewer.layers[0].metadata["mutation_token"] = "render:r:1"
        source = panel._plane_source(panel._current_frame())
        assert not source.live
        assert source.mutation_token == "render:r:1"
        assert source.array is panel._viewer.layers[0].data
    finally:
        panel.deleteLater()


def test_rows_carry_the_frame_and_the_roi_revision(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        captured = _pushes(panel)
        panel.measure()
        row = captured[0][1][0]
        assert row["frame_uid"] == panel._current_frame().frame_uid
        assert row["roi_revision"] == panel._model.rois[0].revision
    finally:
        panel.deleteLater()


def test_a_measurement_never_claims_an_unchecked_match(qapp):
    """An ROI whose capture frame the set does not know is 'unverified'."""
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        captured = _pushes(panel)
        panel.measure()
        assert captured[0][1][0]["geometry_match"] == "unverified"
    finally:
        panel.deleteLater()


def test_a_captured_roi_reports_an_exact_match_on_its_own_frame(qapp):
    panel = _panel(qapp)
    try:
        # A recorded identity, so the frame is "minted": a *derived* identity
        # is capped at pixel-compatible however well everything else lines up,
        # which is the point of A-27.
        panel._viewer.layers[0].metadata.update(
            coordinate_space_uid="space-1",
            result_uid="result-1",
            dataset_uid="data-1",
            identity_kind="minted",
        )
        frame = panel._current_frame()
        panel._set = panel._set.with_frame(frame)
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4), frame_uid=frame.frame_uid)
        ])
        captured = _pushes(panel)
        panel.measure()
        assert captured[0][1][0]["geometry_match"] == "exact"
    finally:
        panel.deleteLater()


def test_an_roi_bound_to_a_slice_is_measured_only_there(qapp):
    stack = np.arange(3 * 16 * 16, dtype=float).reshape(3, 16, 16)
    panel = _panel(qapp, stack, labels=["Z", "Y", "X"])
    try:
        panel.add_rois([
            ROIRecord("free", "rectangle", (0, 4, 0, 4)),
            ROIRecord("bound", "rectangle", (0, 4, 0, 4), position=(("Z", 1),)),
        ])
        captured = _pushes(panel)
        panel.multi_measure("Z")
        assert panel._runner.wait(5.0)
        qapp.processEvents()

        _, rows = captured[-1]
        assert [(row["Z"], row["roi"]) for row in rows] == [
            (0, "free"), (1, "free"), (1, "bound"), (2, "free"),
        ]
    finally:
        panel.deleteLater()


def test_across_results_measures_the_roi_s_own_slice(qapp):
    """A verdict earned at Z=12 must not be reported for Z=0."""
    panel = _panel(qapp)
    try:
        frame = panel._current_frame()
        panel._set = panel._set.with_frame(frame)
        panel.add_rois([
            ROIRecord(
                "a", "rectangle", (0, 4, 0, 4),
                frame_uid=frame.frame_uid, position=(("Z", 2),),
            )
        ])
        stack = np.zeros((4, 16, 16), dtype=float)
        stack[2] = 7.0
        panel.setAvailableResults([
            _Result("stack", stack, labels=["Z", "Y", "X"],
                    space=frame.coordinate_space_uid),
        ])
        captured = _pushes(panel)
        panel.measure_across_results(confirm=lambda entries: list(entries))

        assert captured, panel.summaryLabel.text()
        row = captured[-1][1][0]
        assert row["Z"] == 2
        assert row["mean"] == pytest.approx(7.0)
    finally:
        panel.deleteLater()


def test_the_set_holds_the_rois_it_is_a_set_of(qapp):
    panel = _panel(qapp)
    try:
        assert panel._set.rois == ()
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        assert [roi.name for roi in panel._set.rois] == ["a"]
        panel.clear_rois()
        assert panel._set.rois == ()
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-5 — the operations reach the model through the command log
# --------------------------------------------------------------------------

def _select(panel, *names):
    panel.table.clearSelection()
    for row in range(panel.table.rowCount()):
        item = panel.table.item(row, panel.column_index("Name"))
        if item is not None and item.text() in names:
            panel.table.selectRow(row)
            panel.table.item(row, 0).setSelected(True)


def test_a_boolean_operation_adds_its_result_to_the_model(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 10, 0, 10)),
            ROIRecord("b", "rectangle", (5, 15, 5, 15)),
        ])
        panel.table.selectAll()
        panel.combine_selected("and")

        names = [roi.name for roi in panel._model.rois]
        assert names == ["a", "b", "a_and"]
        assert panel._model.get("a_and").roi_type == "composite"
    finally:
        panel.deleteLater()


def test_an_operation_is_undoable(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 10, 0, 10)),
            ROIRecord("b", "rectangle", (5, 15, 5, 15)),
        ])
        panel.table.selectAll()
        panel.combine_selected("or")
        assert len(panel._model.rois) == 3

        panel._commands.undo()
        assert [roi.name for roi in panel._model.rois] == ["a", "b"]
    finally:
        panel.deleteLater()


def test_split_replaces_the_original_with_its_parts(qapp):
    panel = _panel(qapp)
    try:
        from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask

        mask = np.zeros((16, 16), dtype=bool)
        mask[1:4, 1:4] = True
        mask[10:14, 10:14] = True
        panel.add_rois([roi_from_mask(mask, name="two", offset=(0, 0))])
        panel.table.selectAll()
        panel.split_selected()

        names = [roi.name for roi in panel._model.rois]
        assert names == ["two_1", "two_2"]

        panel._commands.undo()
        assert [roi.name for roi in panel._model.rois] == ["two"]
    finally:
        panel.deleteLater()


def test_an_operation_refusal_is_shown_rather_than_raised(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 2, 0, 2)),
            ROIRecord("b", "rectangle", (12, 14, 12, 14)),
        ])
        panel.table.selectAll()
        panel.combine_selected("and")

        assert len(panel._model.rois) == 2      # nothing was added
        assert "empty" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_a_boolean_needs_two_rois_and_says_so(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.table.selectAll()
        panel.combine_selected("or")
        assert len(panel._model.rois) == 1
        assert "Select 2" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_make_inverse_uses_the_image_on_screen(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.table.selectAll()
        panel.make_inverse_selected()

        inverse = panel._model.get("a_inverse")
        assert inverse is not None
        assert inverse.bounds == (0, 16, 0, 16)   # the layer is 16x16
    finally:
        panel.deleteLater()


def test_rescaling_to_the_frame_already_in_use_changes_nothing(qapp):
    panel = _panel(qapp)
    try:
        frame = panel._current_frame()
        panel._set = panel._set.with_frame(frame)
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4), frame_uid=frame.frame_uid)
        ])
        panel.table.selectAll()
        panel.rescale_selected_to_current_frame()

        assert [roi.name for roi in panel._model.rois] == ["a"]
        assert "already in this frame" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_rescaling_an_roi_with_no_recorded_frame_says_why(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("a", "rectangle", (0, 4, 0, 4))])
        panel.table.selectAll()
        panel.rescale_selected_to_current_frame()
        assert "no recorded frame" in panel.summaryLabel.text()
    finally:
        panel.deleteLater()


def test_the_table_allows_selecting_more_than_one_roi(qapp):
    """A set operation cannot be expressed on a single-selection table."""
    panel = _panel(qapp)
    try:
        panel.add_rois([
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ])
        panel.table.selectAll()
        assert len(panel._selected_rois()) == 2
    finally:
        panel.deleteLater()
