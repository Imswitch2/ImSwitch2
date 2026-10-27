"""The Profile widget shared by imcontrol's Line Profile dock and ImProcess.

Driven on napari's headless ``ViewerModel`` rather than a double, because what
is under test here is mostly the wiring to napari: which layer the selection
events leave measured, and whether a new frame reaches the plot.
"""

import numpy as np
import pytest
from napari.components import ViewerModel
from qtpy import QtWidgets

from imswitch.imcommon.model.result_records import series_to_csv
from imswitch.imcommon.view.guitools.ProfileWidget import ProfileWidget

_APP = None


@pytest.fixture(scope="module")
def qapp():
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _ramp(height=32, width=64, gain=1.0):
    """Intensity equal to the column index (times ``gain``)."""
    return np.tile(np.arange(width, dtype=float) * gain, (height, 1))


def _viewer(*layers):
    viewer = ViewerModel()
    for name, data, options in layers:
        viewer.add_image(data, name=name, **options)
    return viewer


@pytest.fixture
def two_detectors(qapp):
    viewer = _viewer(
        ("Live: A", np.full((32, 64), 1.0), {}),
        ("Live: B", np.full((32, 64), 2.0), {}),
    )
    widget = ProfileWidget(viewer, modes=ProfileWidget.MODES + ("timetrace",))
    yield widget, viewer
    widget.close()
    widget.deleteLater()


def _choose_layer(widget, data):
    widget.refreshLayerChoices()
    index = widget.layerCombo.findData(data)
    assert index >= 0, f"{data!r} is not offered"
    widget.layerCombo.setCurrentIndex(index)


# -- which layer is measured ---------------------------------------------------

def test_drawing_does_not_move_the_profile_off_the_selected_detector(two_detectors):
    """Drawing makes the Shapes layer napari's active one.

    Falling back to "the first image layer" from there is how a two-detector
    setup profiled the detector nobody had selected.
    """
    widget, viewer = two_detectors
    detector_b = viewer.layers["Live: B"]
    viewer.layers.selection.active = detector_b

    viewer.add_shapes(name="Viewer Tools")  # what arming the draw tool does
    assert viewer.layers.selection.active is viewer.layers["Viewer Tools"]

    assert widget._activeImageLayer() is detector_b
    assert widget.layerCombo.itemText(0) == "Active: Live: B"


def test_a_layer_chosen_by_name_stays_measured(two_detectors):
    widget, viewer = two_detectors
    _choose_layer(widget, "Live: A")

    viewer.layers.selection.active = viewer.layers["Live: B"]

    assert widget._activeImageLayer() is viewer.layers["Live: A"]


def test_a_chosen_layer_that_goes_away_is_reported_not_swapped(two_detectors):
    widget, viewer = two_detectors
    _choose_layer(widget, "Live: A")

    viewer.layers["Live: A"].visible = False
    widget.refreshLayerChoices()
    widget._plotLineProfile(((4.0, 0.0), (4.0, 20.0)))

    assert widget.layerCombo.currentText() == "Live: A (not shown)"
    assert widget._last_payload == []
    assert "Live: A is not shown" in widget.fitSummary.text()


def test_the_plot_names_the_layer_it_measured(two_detectors):
    widget, viewer = two_detectors
    viewer.layers.selection.active = viewer.layers["Live: B"]

    widget._plotLineProfile(((4.0, 0.0), (4.0, 20.0)))

    title = widget.plot.getPlotItem().titleLabel.text
    assert "Live: B" in title
    assert widget._last_payload[0][2] == pytest.approx(2.0)
    assert widget._buildOutputRecords()[0]["source"] == "Live: B"


def test_all_visible_layers_get_a_curve_each_in_their_own_pixel_size(qapp):
    """Two detectors with different pixel sizes under one drawn line.

    Each is sampled in its own pixels, so both curves span the same physical
    length; one shared conversion would put one of them in the wrong place.
    """
    viewer = _viewer(
        ("Live: A", _ramp(), {}),
        ("Live: B", _ramp(gain=10.0), {"scale": (2.0, 2.0)}),
    )
    widget = ProfileWidget(viewer, defaultUnit="µm")
    try:
        _choose_layer(widget, "__all__")
        widget._plotLineProfile(((4.0, 0.0), (4.0, 20.0)))

        names = [name for name, _x, _y in widget._last_payload]
        assert names == ["Live: A", "Live: B"]
        (_, x_a, y_a), (_, x_b, y_b) = widget._last_payload
        assert x_a[-1] == pytest.approx(20.0)
        assert x_b[-1] == pytest.approx(20.0)
        assert y_a[-1] == pytest.approx(20.0)   # column 20 of A
        assert y_b[-1] == pytest.approx(100.0)  # column 10 of B, gain 10
        assert "2 layers" in widget.plot.getPlotItem().titleLabel.text
        sources = [record["source"] for record in widget._buildOutputRecords()]
        assert sources == ["Live: A", "Live: B"]
    finally:
        widget.deleteLater()


def test_a_translated_layer_is_sampled_where_it_is_drawn(qapp):
    viewer = _viewer(("tile", _ramp(), {"translate": (0.0, 10.0)}))
    widget = ProfileWidget(viewer)
    try:
        widget._plotLineProfile(((4.0, 10.0), (4.0, 20.0)))
        profile = widget._last_payload[0][2]
        assert profile[0] == pytest.approx(0.0)
        assert profile[-1] == pytest.approx(10.0)
    finally:
        widget.deleteLater()


def test_scaled_layers_without_a_unit_use_the_hosts_unit(qapp):
    viewer = _viewer(("Live: cam", _ramp(), {"scale": (0.1, 0.1)}))
    live = ProfileWidget(viewer, defaultUnit="µm")
    plain = ProfileWidget(viewer)
    try:
        assert live._distanceUnit() == "µm"
        assert plain._distanceUnit() == "px"
    finally:
        live.deleteLater()
        plain.deleteLater()


# -- following a live image ----------------------------------------------------

def test_a_new_frame_redraws_the_profile(qapp):
    viewer = _viewer(("Live: cam", _ramp(), {}))
    widget = ProfileWidget(viewer, liveUpdates=True)
    try:
        widget.isVisible = lambda: True
        widget._findFirstShape = (
            lambda kind: ((4.0, 0.0), (4.0, 20.0)) if kind == "line" else None
        )
        widget._plotLineProfile(widget._findFirstShape("line"))
        widget._followTick()  # starts listening to the layer
        assert widget._last_payload[0][2][-1] == pytest.approx(20.0)

        # Assigning the *same* array, mutated in place, is how PMT/APD
        # detectors deliver a frame: identity cannot tell new from old.
        frame = viewer.layers["Live: cam"].data
        frame *= 3.0
        viewer.layers["Live: cam"].data = frame
        widget._followTick()

        assert widget._last_payload[0][2][-1] == pytest.approx(60.0)
    finally:
        widget.close()
        widget.deleteLater()


def test_live_redraws_wait_for_a_drag_and_stop_when_frozen(qapp):
    viewer = _viewer(("Live: cam", _ramp(), {}))
    widget = ProfileWidget(viewer, liveUpdates=True)
    try:
        widget.isVisible = lambda: True
        widget._findFirstShape = (
            lambda kind: ((4.0, 0.0), (4.0, 20.0)) if kind == "line" else None
        )
        widget._plotLineProfile(widget._findFirstShape("line"))
        widget._followTick()
        layer = viewer.layers["Live: cam"]

        widget._isDraggingMeasurement = lambda: True
        layer.data = _ramp(gain=2.0)
        widget._followTick()
        assert widget._last_payload[0][2][-1] == pytest.approx(20.0)  # not yet

        widget._isDraggingMeasurement = lambda: False
        widget._followTick()
        assert widget._last_payload[0][2][-1] == pytest.approx(40.0)  # caught up

        widget.liveCheck.setChecked(False)
        layer.data = _ramp(gain=5.0)
        widget._followTick()
        assert widget._last_payload[0][2][-1] == pytest.approx(40.0)  # frozen
    finally:
        widget.close()
        widget.deleteLater()


# -- Intensity vs T ------------------------------------------------------------

def test_intensity_vs_time_samples_the_frame_until_a_rectangle_is_drawn(two_detectors):
    widget, viewer = two_detectors
    viewer.layers.selection.active = viewer.layers["Live: A"]
    widget.selectMode("timetrace")

    assert widget._traceTimer.isActive()
    assert widget._traceTimer.interval() == 1000
    assert widget.plot.getAxis("bottom").labelText == "Time (s)"
    ((name, times, values),) = widget._last_payload
    assert name == "Live: A"
    assert values == pytest.approx([1.0])

    viewer.layers["Live: A"].data = np.full((32, 64), 4.0)
    widget._traceTick()

    ((_, times, values),) = widget._last_payload
    assert values == pytest.approx([1.0, 4.0])
    assert times[1] >= times[0]
    assert widget._buildOutputRecords()[0]["kind"] == "time-trace"


def test_drawing_a_rectangle_restarts_the_trace_on_that_region(qapp):
    viewer = _viewer(("Live: cam", _ramp(), {}))
    widget = ProfileWidget(viewer, modes=("timetrace",))
    try:
        widget.selectMode("timetrace")
        widget._traceTick()
        assert len(widget._traceTimes) == 2

        widget._findFirstShape = (
            lambda kind: (0.0, 0.0, 4.0, 8.0) if kind == "rectangle" else None
        )
        widget._shapesChanged()
        assert len(widget._traceTimes) == 1
        assert widget._last_payload[0][2] == pytest.approx([3.5])  # columns 0..7
        assert "ROI mean" in widget.plot.getPlotItem().titleLabel.text

        # napari reports one drawn rectangle more than once; that must not
        # throw the trace away.
        widget._traceTick()
        widget._shapesChanged()
        assert len(widget._traceTimes) == 2
    finally:
        widget.close()
        widget.deleteLater()


def test_pausing_and_leaving_the_trace_stop_sampling(two_detectors):
    widget, _viewer = two_detectors
    widget.selectMode("timetrace")

    widget.tracePauseButton.setChecked(True)
    assert not widget._traceTimer.isActive()
    assert widget.tracePauseButton.text() == "Resume"
    widget.tracePauseButton.setChecked(False)
    assert widget._traceTimer.isActive()

    widget.traceIntervalSpin.setValue(2.5)
    assert widget._traceTimer.interval() == 2500

    widget.selectMode("line")
    assert not widget._traceTimer.isActive()
    assert widget.traceRestartButton.isHidden()


def test_a_layer_joining_mid_trace_gets_a_curve_of_its_own(qapp):
    viewer = _viewer(("Live: A", np.full((8, 8), 1.0), {}))
    widget = ProfileWidget(viewer, modes=("timetrace",))
    try:
        _choose_layer(widget, "__all__")
        widget.selectMode("timetrace")
        widget._traceTick()

        viewer.add_image(np.full((8, 8), 5.0), name="Live: B")
        widget._traceTick()

        assert widget._traceSeries["Live: A"] == pytest.approx([1.0, 1.0, 1.0])
        joined = widget._traceSeries["Live: B"]
        assert np.isnan(joined[:2]).all()
        assert joined[2] == pytest.approx(5.0)
    finally:
        widget.close()
        widget.deleteLater()


def test_the_trace_keeps_a_bounded_history(qapp):
    viewer = _viewer(("Live: cam", np.ones((4, 4)), {}))
    widget = ProfileWidget(viewer, modes=("timetrace",))
    try:
        widget.TRACE_MAX_SAMPLES = 3
        widget.selectMode("timetrace")
        for _ in range(5):
            widget._sampleTrace()
        assert len(widget._traceTimes) == 3
        assert len(widget._traceSeries["Live: cam"]) == 3
    finally:
        widget.close()
        widget.deleteLater()


# -- lifetime ------------------------------------------------------------------

def test_a_shape_drawn_after_the_panel_is_destroyed_is_ignored(qapp, monkeypatch):
    """The tool broker calls the panel from a Qt slot for as long as its
    Python object lives; an exception there escapes unhandled."""
    import sys

    from qtpy import sip

    escaped = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc_info: escaped.append(exc_info))
    viewer = _viewer(("Live: A", np.ones((8, 8)), {}))
    widget = ProfileWidget(viewer)
    widget.selectMode("line")  # the panel now owns the drawing tool
    service = widget._toolService
    sip.delete(widget)

    service.manager.sigShapesChanged.emit()  # reaches _shapesChanged
    # napari's own events are safe without help: it disconnects a handler
    # whose Qt object is gone.
    viewer.add_image(np.ones((8, 8)), name="Live: B")
    viewer.layers.clear()

    assert escaped == []


# -- host API and output -------------------------------------------------------

def test_select_mode_is_a_click_and_announces_itself(two_detectors):
    widget, _viewer = two_detectors
    seen = []
    widget.sigModeChanged.connect(seen.append)

    widget.selectMode("rectangle")

    assert seen == ["rectangle"]
    assert widget.rectangleButton.isChecked()
    with pytest.raises(ValueError):
        widget.selectMode("crosshair")


def test_host_options_hide_what_the_host_cannot_use(qapp):
    viewer = _viewer(("img", np.ones((4, 4)), {}))
    widget = ProfileWidget(viewer, pushTargets=False)
    try:
        assert widget.pushButton.isHidden()
        assert widget.pushGraphButton.isHidden()
        assert widget.liveCheck.isHidden()  # liveUpdates is off
        assert widget.timeTraceButton is None
        assert widget.modes() == ProfileWidget.MODES
        # Nothing to choose from until an ROI manager is bound.
        assert widget._sourceBox.isHidden()
    finally:
        widget.deleteLater()


def test_save_data_writes_the_curves_themselves(two_detectors, tmp_path, monkeypatch):
    widget, _viewer = two_detectors
    widget._plotLineProfile(((4.0, 0.0), (4.0, 3.0)))
    target = tmp_path / "profile.csv"
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "CSV (*.csv)")),
    )

    widget.saveDataButton.click()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "line x,line y"
    assert len(lines) == 1 + 4  # header, then one row per sample


def test_series_of_different_lengths_share_one_csv():
    text = series_to_csv([
        ("x", [0.0, 1.0], [5.0, 6.0]),
        ("y", [0.0, 1.0, 2.0], [7.0, 8.0, 9.0]),
    ])
    assert text.splitlines() == [
        "x x,x y,y x,y y",
        "0,5,0,7",
        "1,6,1,8",
        ",,2,9",
    ]
