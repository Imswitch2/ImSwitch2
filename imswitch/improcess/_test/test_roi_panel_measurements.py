"""P-3.3–3.5: the panel's columns, calibration, precision, debounce and cache.

The registry is tested on its own in test_roi_measurements.py; what is checked
here is that the *panel* asks it the right question — the right measurements,
in the right unit, once per burst, and not twice for an answer it already has.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_set import MeasurementConfig  # noqa: E402
from imswitch.improcess.analysis.roi_frame_adapter import plane_scales  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _panel(qapp, *, scale=None, unit="px", token=None):
    viewer = _Viewer(np.arange(256, dtype=float).reshape(16, 16))
    layer = viewer.layers[0]
    if scale is not None:
        layer.scale = tuple(scale)
        layer.metadata["axes"] = [
            {"label": "Y", "size": 16, "scale": scale[0], "unit": unit},
            {"label": "X", "size": 16, "scale": scale[1], "unit": unit},
        ]
        layer.metadata["scale_unit"] = unit
    if token is not None:
        layer.metadata["mutation_token"] = token
    widget = ROIManagerWidget(viewer)
    return widget


def _cell(panel, label, row=0):
    return panel.table.item(row, panel.column_index(label)).text()


# --------------------------------------------------------------------------
# P-3.3 — columns follow the selection, and the unit lives in the header
# --------------------------------------------------------------------------

def test_columns_come_from_the_measurement_selection(qapp):
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("area_px", "mean", "perimeter_px"))
        )
        headers = [
            panel.table.horizontalHeaderItem(col).text()
            for col in range(panel.table.columnCount())
        ]
        assert headers[:3] == ["Visible", "Name", "Type"]
        assert headers[-1] == "Note"
        assert len(headers) == 3 + 3 + 1
        assert panel.column_index("Mean") > 0
        assert panel.column_index("Median") == -1
    finally:
        panel.deleteLater()


def test_calibrated_columns_carry_the_unit_in_the_header(qapp):
    """The unit appears once, in the header — never inside a cell."""
    panel = _panel(qapp, scale=(0.5, 0.5), unit="um")
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("area_px", "area_cal"))
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])

        headers = [
            panel.table.horizontalHeaderItem(col).text()
            for col in range(panel.table.columnCount())
        ]
        assert any(header.endswith("(um²)") for header in headers)

        # 16 px at 0.5 um/px on both axes = 4 um^2, and the cell is a bare
        # number either way.
        area_cal = panel.table.item(0, headers.index("Area (um²)")).text()
        assert float(area_cal) == pytest.approx(4.0)
        assert "um" not in area_cal
    finally:
        panel.deleteLater()


def test_an_uncalibrated_frame_drops_its_calibrated_columns(qapp):
    """No calibration must not become "1 micrometre per pixel".

    The column is not rendered at all rather than rendered as a copy of the
    pixel one: an "Area" beside an identical "Area" is a claim of calibration.
    """
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("area_px", "area_cal"))
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        headers = [
            panel.table.horizontalHeaderItem(col).text()
            for col in range(panel.table.columnCount())
        ]
        assert headers.count("Area (px)") == 1
        assert "Area" not in headers
        assert float(_cell(panel, "Area (px)")) == pytest.approx(16.0)
    finally:
        panel.deleteLater()


def test_a_calibrated_frame_keeps_both_columns(qapp):
    panel = _panel(qapp, scale=(0.5, 0.5), unit="um")
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("area_px", "area_cal"))
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        headers = [
            panel.table.horizontalHeaderItem(col).text()
            for col in range(panel.table.columnCount())
        ]
        assert "Area (px)" in headers
        assert "Area (um²)" in headers
    finally:
        panel.deleteLater()


def test_an_explicitly_empty_selection_stays_empty(qapp):
    """Unticking everything must not spring back to the defaults."""
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(MeasurementConfig(selected=()))
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        headers = [
            panel.table.horizontalHeaderItem(col).text()
            for col in range(panel.table.columnCount())
        ]
        assert headers == ["Visible", "Name", "Type", "Note"]
    finally:
        panel.deleteLater()


def test_plane_scales_reads_the_displayed_axes_by_label():
    """A YZ view must not be calibrated with the Y and X scales."""
    from imswitch.imcommon.algorithms.spatial_frame import AxisDescriptor, SpatialFrame

    frame = SpatialFrame(
        coordinate_space_uid="space",
        result_uid="result",
        dataset_uid="data",
        plane_axes=("Y", "Z"),
        axes=(
            AxisDescriptor("Z", 8, 2.0, "um"),
            AxisDescriptor("Y", 16, 0.5, "um"),
            AxisDescriptor("X", 16, 0.25, "um"),
        ),
        shape=(16, 8),
        unit="um",
    )
    assert plane_scales(frame) == (0.5, 2.0, "um")


def test_changing_the_selection_rebuilds_the_rows(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        assert panel.table.rowCount() == 1
        panel.set_measurement_config(MeasurementConfig(selected=("mean",)))
        assert panel.table.rowCount() == 1
        assert panel.column_index("Area") == -1
        assert float(_cell(panel, "Mean")) == pytest.approx(
            np.arange(256).reshape(16, 16)[0:4, 0:4].mean()
        )
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-3.4 — the dialog's precision is a display choice only
# --------------------------------------------------------------------------

def test_decimals_and_scientific_notation_are_display_only(qapp):
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("mean",), decimals=1)
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        exact = float(np.arange(256).reshape(16, 16)[0:4, 0:4].mean())

        assert _cell(panel, "Mean") == f"{exact:.1f}"
        # The value behind the cell — what sorts, and what is exported — is
        # never rounded.
        assert panel._stats_rows[0]["mean"] == pytest.approx(exact)

        panel.set_measurement_config(
            MeasurementConfig(selected=("mean",), decimals=2, scientific=True)
        )
        assert _cell(panel, "Mean") == f"{exact:.2e}"
    finally:
        panel.deleteLater()


def test_pixel_counts_are_not_shown_with_decimals(qapp):
    panel = _panel(qapp)
    try:
        panel.set_measurement_config(
            MeasurementConfig(selected=("area_px",), decimals=3)
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        assert _cell(panel, "Area") == "16"
    finally:
        panel.deleteLater()


def test_export_does_not_emit_a_column_twice(qapp, tmp_path):
    """The legacy statistics must not ride along beside the registry's."""
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        row = panel._stats_rows[0]
        assert "area_pixels" not in row  # legacy name, superseded by area_px
        assert row["area_px"] == 16

        target = tmp_path / "rois.csv"
        panel.write_csv(target)
        header = target.read_text(encoding="utf-8").splitlines()[0].split(",")
        assert len(header) == len(set(header))
    finally:
        panel.deleteLater()


def test_threshold_from_the_dialog_reaches_the_measurement(qapp):
    panel = _panel(qapp)
    try:
        image = np.arange(256, dtype=float).reshape(16, 16)
        window = (0.0, 20.0)
        panel.set_measurement_config(
            MeasurementConfig(selected=("mean",), threshold=window)
        )
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])

        patch = image[0:4, 0:4]
        expected = patch[(patch >= window[0]) & (patch <= window[1])].mean()
        assert float(_cell(panel, "Mean")) == pytest.approx(expected)
        assert expected != pytest.approx(patch.mean())
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-3.5 — one measurement pass per burst, and no re-measuring
# --------------------------------------------------------------------------

def test_a_burst_of_viewer_events_costs_one_measurement_pass(qapp):
    panel = _panel(qapp)
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        calls = []
        original = panel._model.compute_stats

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        panel._model.compute_stats = counting

        for _ in range(10):
            panel._viewer.dims.events.current_step.emit(SimpleNamespace())
        assert calls == []  # nothing yet: the burst is still coalescing

        panel.refresh_stats()  # what the timer would fire
        assert len(calls) == 1
    finally:
        panel.deleteLater()


def test_a_measured_plane_is_not_measured_again(qapp):
    panel = _panel(qapp, token="render:result:1")
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        assert len(panel._cache) > 0

        calls = []
        import imswitch.improcess.analysis.roi_manager as roi_manager

        original = roi_manager.measure_roi

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        roi_manager.measure_roi = counting
        try:
            panel.refresh_stats()
            assert calls == []
            assert float(_cell(panel, "Mean")) == pytest.approx(
                np.arange(256).reshape(16, 16)[0:4, 0:4].mean()
            )
        finally:
            roi_manager.measure_roi = original
    finally:
        panel.deleteLater()


def test_an_image_without_a_token_is_never_cached(qapp):
    """Caching an image that can change under us is the one unsafe answer."""
    panel = _panel(qapp)  # no mutation_token in the layer's metadata
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        assert len(panel._cache) == 0
    finally:
        panel.deleteLater()


def test_changing_the_configuration_invalidates_the_cache(qapp):
    """A cached row measured under one selection must not answer another."""
    panel = _panel(qapp, token="render:result:1")
    try:
        panel.set_measurement_config(MeasurementConfig(selected=("mean",)))
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        assert len(panel._cache) == 1

        panel.set_measurement_config(MeasurementConfig(selected=("max",)))
        patch = np.arange(256).reshape(16, 16)[0:4, 0:4]
        # Answering from the cached row would leave this column empty, since
        # the cached row only ever held "mean".
        assert float(_cell(panel, "Max")) == pytest.approx(patch.max())
        assert panel.column_index("Mean") == -1
    finally:
        panel.deleteLater()


def test_a_moved_roi_is_measured_again(qapp):
    """The cache keys on the ROI's revision, so an edit is never answered stale."""
    panel = _panel(qapp, token="render:result:1")
    try:
        panel.add_rois([ROIRecord("cell", "rectangle", (0, 4, 0, 4))])
        before = float(_cell(panel, "Mean"))

        roi = panel._model.rois[0]
        panel._model.update(roi.name, bounds=(8, 12, 8, 12))
        panel.refresh_stats()

        assert float(_cell(panel, "Mean")) != pytest.approx(before)
    finally:
        panel.deleteLater()


# --------------------------------------------------------------------------
# P-3.4 — the dialog is a view onto the registry
# --------------------------------------------------------------------------

def test_the_dialog_offers_every_registered_measurement(qapp):
    from imswitch.improcess.analysis.roi_measurements import MEASUREMENTS
    from imswitch.improcess.view.ROIMeasurementsDialog import ROIMeasurementsDialog

    dialog = ROIMeasurementsDialog(MeasurementConfig(selected=("mean", "area_px")))
    try:
        # Adding a measurement to the registry must add a checkbox here
        # without anyone editing the dialog.
        assert set(dialog._checks) == set(MEASUREMENTS)
        assert dialog._checks["mean"].isChecked()
        assert not dialog._checks["median"].isChecked()

        dialog._checks["median"].setChecked(True)
        dialog.decimalsSpin.setValue(5)
        config = dialog.config()
        assert "median" in config.selected
        assert config.decimals == 5
        assert config.threshold is None
    finally:
        dialog.deleteLater()


def test_the_dialog_orders_a_threshold_low_to_high(qapp):
    from imswitch.improcess.view.ROIMeasurementsDialog import ROIMeasurementsDialog

    dialog = ROIMeasurementsDialog(MeasurementConfig())
    try:
        dialog.thresholdCheck.setChecked(True)
        dialog.lowSpin.setValue(80.0)
        dialog.highSpin.setValue(20.0)
        assert dialog.config().threshold == (20.0, 80.0)
    finally:
        dialog.deleteLater()


def test_the_configuration_revision_only_ever_advances(qapp):
    """The dialog builds a fresh config each time; the revision is the set's."""
    panel = _panel(qapp)
    try:
        revisions = []
        for selection in (("mean",), ("max",), ("mean",)):
            panel.set_measurement_config(MeasurementConfig(selected=selection))
            revisions.append(panel._set.measurement_config.revision)
        assert revisions == sorted(set(revisions)) == revisions
        assert len(set(revisions)) == 3
    finally:
        panel.deleteLater()
