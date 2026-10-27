"""ImageWindow with real pyqtgraph widgets (offscreen): one result, one window."""

from __future__ import annotations

import h5py
import numpy as np
import pytest
from qtpy import QtGui, QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import ViewMode
from imswitch.improcess.processors.make_composite.result import CompositeResult
from imswitch.improcess.view.image_window import (
    ImageWindow,
    lut_table,
    nice_length,
)


def _result(data, labels, **kwargs):
    return ArrayProcessingResult("res", data, list(labels), **kwargs)


def _window(qtbot, result):
    window = ImageWindow(result)
    qtbot.addWidget(window)
    return window


def _items(window):
    return [layer.item for layer in window._layers]


def test_a_2d_result_shows_one_item_with_the_persisted_levels(qtbot):
    data = np.arange(64 * 48, dtype=np.uint16).reshape(48, 64)
    result = _result(data, "YX")
    result.setDispLevels((100.0, 900.0))
    window = _window(qtbot, result)

    assert len(_items(window)) == 1
    assert tuple(_items(window)[0].getLevels()) == (100.0, 900.0)
    assert np.array_equal(_items(window)[0].image, data)
    assert window.windowTitle() == "res"
    assert window._sliders == []


def test_a_tzyx_result_gets_two_sliders_that_pick_the_plane(qtbot):
    data = np.random.default_rng(0).integers(0, 1000, (3, 4, 16, 20)).astype(np.uint16)
    window = _window(qtbot, _result(data, "TZYX"))

    assert len(window._sliders) == 2
    assert window.axis_labels == ["T", "Z", "Y", "X"]
    window._sliders[0][0].setValue(2)
    window._sliders[1][0].setValue(3)
    assert np.array_equal(_items(window)[0].image, data[2, 3])
    assert window.current_index() == (2, 3, None, None)

    window.set_current_index((1, 0, None, None))
    assert np.array_equal(_items(window)[0].image, data[1, 0])
    steps = []
    window.sigAxisStepChanged.connect(steps.append)
    window.moveDimStep(0, 99)  # clamped
    assert window.current_index()[0] == 2
    assert steps


def test_a_composite_draws_channels_additively_with_their_own_luts(qtbot):
    data = np.random.default_rng(1).integers(0, 255, (3, 12, 12)).astype(np.uint16)
    result = CompositeResult(
        "comp", data, ["C", "Y", "X"], channel_axis=0,
        channel_colormaps=["red", "green", "blue"],
    )
    window = _window(qtbot, result)

    items = _items(window)
    assert len(items) == 3
    modes = [item.paintMode for item in items]
    assert modes[0] != QtGui.QPainter.CompositionMode_Plus
    assert modes[1:] == [QtGui.QPainter.CompositionMode_Plus] * 2
    for item, name in zip(items, ["red", "green", "blue"]):
        assert np.array_equal(item.lut, lut_table(name))
    assert window._sliders == []  # the channel axis is not a slider


def test_a_plain_result_with_a_c_axis_gets_a_slider(qtbot):
    window = _window(qtbot, _result(np.zeros((3, 8, 8), np.uint8), "CYX"))
    assert len(_items(window)) == 1
    assert len(window._sliders) == 1


def test_toolbar_api_persists_onto_the_result(qtbot):
    data = np.random.default_rng(2).integers(0, 255, (2, 10, 10)).astype(np.uint16)
    result = CompositeResult(
        "comp", data, ["C", "Y", "X"], channel_axis=0, channel_colormaps=["red", "green"],
    )
    window = _window(qtbot, result)

    window.setActiveImageDisplayLevels(5, 50)
    window.setActiveImageColormap("cyan")
    states = window.getImageLayerStates()
    assert [s["id"] for s in states] == ["C_0", "C_1"]
    assert window.getActiveImageDisplayLevels() == (5.0, 50.0)
    assert window.getActiveImageColormap() == "cyan"
    assert result.getDisplayLayerLevels("C_0") == (5.0, 50.0)
    assert result.getDisplayLayerColormap("C_0") == "cyan"

    window.setImageLayerColormap("C_1", "magenta")
    window.setImageLayerVisible("C_1", False)
    assert result.getDisplayLayerColormap("C_1") == "magenta"
    assert result.getDisplayLayerVisible("C_1") is False
    assert _items(window)[1].isVisible() is False
    assert [s["visible"] for s in window.getImageLayerStates()] == [True, False]

    # A new window on the same result starts from what was persisted.
    again = _window(qtbot, result)
    assert again.getActiveImageColormap() == "cyan"
    assert tuple(_items(again)[0].getLevels()) == (5.0, 50.0)
    assert [s["visible"] for s in again.getImageLayerStates()] == [True, False]


def test_levels_of_a_plain_result_land_on_the_whole_result(qtbot):
    result = _result(np.arange(100, dtype=np.uint16).reshape(10, 10), "YX")
    window = _window(qtbot, result)
    seen = []
    window.sigImageLevelsChanged.connect(lambda meta, levels: seen.append(levels))
    window.setActiveImageDisplayLevels(10, 90)
    window.setActiveImageColormap("hot")
    assert result.getDispLevels() == (10.0, 90.0)
    assert result.getDisplayColormap() == "hot"
    assert seen == [(10.0, 90.0)]
    assert window.getActiveImageDisplayLevelsRange() is not None


class _CountingDataset:
    """Wraps an h5py dataset and records the size of every read."""

    def __init__(self, dataset):
        self._dataset = dataset
        self.shape = dataset.shape
        self.dtype = dataset.dtype
        self.ndim = dataset.ndim
        self.reads: list[int] = []

    def __getitem__(self, key):
        out = np.asarray(self._dataset[key])
        self.reads.append(int(out.size))
        return out


def test_a_lazy_h5py_result_reads_one_plane_at_a_time(qtbot, tmp_path):
    data = np.random.default_rng(3).integers(0, 1000, (6, 40, 50)).astype(np.uint16)
    with h5py.File(tmp_path / "lazy.h5", "w") as handle:
        handle.create_dataset("d", data=data)
        counted = _CountingDataset(handle["d"])
        result = _result(counted, "TYX")
        result.setDispLevels((0.0, 1000.0))  # remembered, so no scan to scale it
        window = _window(qtbot, result)

        plane = 40 * 50
        assert counted.reads == [plane]
        window._sliders[0][0].setValue(4)
        assert np.array_equal(_items(window)[0].image, data[4])
        assert counted.reads == [plane, plane]
        assert max(counted.reads) < data.size


def test_a_lazy_subset_array_result_works(qtbot):
    from imswitch.improcess.model.lazy_array import identity_lazy_view

    data = np.random.default_rng(4).integers(0, 1000, (3, 20, 30)).astype(np.uint16)
    lazy = identity_lazy_view(data, source_shape=data.shape)
    window = _window(qtbot, _result(lazy, "TYX"))
    window._sliders[0][0].setValue(2)
    assert np.array_equal(_items(window)[0].image, data[2])


def test_cursor_readout_for_a_calibrated_result(qtbot):
    data = np.arange(10 * 20, dtype=np.float32).reshape(10, 20)
    result = _result(data, "YX", axis_scales=[0.5, 0.25], scale_unit="um")
    window = _window(qtbot, result)

    # x = 2.0 um is column 8, y = 1.0 um is row 2.
    text = window.readoutText(2.0, 1.0)
    assert "x=2 µm" in text and "y=1 µm" in text
    assert f"value={data[2, 8]:g}" in text


def test_cursor_readout_in_pixels_and_per_channel(qtbot):
    data = np.arange(2 * 6 * 6, dtype=np.uint16).reshape(2, 6, 6)
    result = CompositeResult(
        "comp", data, ["C", "Y", "X"], channel_axis=0, channel_colormaps=["red", "green"],
    )
    window = _window(qtbot, result)
    text = window.readoutText(3.5, 1.5)
    assert "x=3  y=1" in text
    assert f"comp C_0={data[0, 1, 3]:g}" in text and f"comp C_1={data[1, 1, 3]:g}" in text
    # Outside the image only the position is given.
    assert "value" not in window.readoutText(99, 99)


def test_view_modes_switch_the_displayed_axes(qtbot):
    data = np.random.default_rng(5).integers(0, 255, (4, 6, 8)).astype(np.uint16)
    modes = [ViewMode("XY", (0, 1, 2)), ViewMode("XZ", (1, 0, 2))]
    window = _window(qtbot, _result(data, "ZYX", view_modes=modes))

    assert window._modeCombo.isVisibleTo(window)
    assert np.array_equal(_items(window)[0].image, data[0])
    changed = []
    window.sigViewChanged.connect(lambda: changed.append(1))
    window._modeCombo.setCurrentIndex(1)
    assert window.getViewName() == "XZ"
    assert changed
    assert window.axis_labels == ["Y", "Z", "X"]
    # Displayed (Y, Z, X) with a slider on Y: the plane is data[:, y, :] as (Z, X).
    assert np.array_equal(_items(window)[0].image, data[:, 0, :])
    window.moveDimStep(0, 3)
    assert np.array_equal(_items(window)[0].image, data[:, 3, :])


def test_a_single_view_mode_shows_no_combo(qtbot):
    window = _window(qtbot, _result(np.zeros((4, 4), np.uint8), "YX",
                                    view_modes=[ViewMode("Standard", (0, 1))]))
    assert not window._modeCombo.isVisibleTo(window)


def test_transposed_view_mode_transposes_the_plane(qtbot):
    data = np.arange(6 * 8, dtype=np.uint16).reshape(6, 8)
    window = _window(qtbot, _result(data, "YX", view_modes=[
        ViewMode("A", (0, 1)), ViewMode("B", (1, 0))]))
    window._modeCombo.setCurrentIndex(1)
    assert np.array_equal(_items(window)[0].image, data.T)


def test_non_image_layers_are_noted_not_drawn(qtbot):
    from imswitch.improcess.model.result import DisplayLayerSpec

    image = DisplayLayerSpec("img", np.zeros((8, 8), np.uint8), ["Y", "X"], role="context")
    labels = DisplayLayerSpec("lab", np.ones((8, 8), np.uint8), ["Y", "X"], kind="labels")
    window = _window(qtbot, _result(np.zeros((8, 8), np.uint8), "YX"))
    window.setDisplayLayers([image, labels])
    assert len(_items(window)) == 1
    assert "labels layers are shown in window mode in a later version" in window.statusText()


def test_the_window_closes_by_hiding_and_reports_it(qtbot):
    window = _window(qtbot, _result(np.zeros((8, 8), np.uint8), "YX"))
    window.show()
    closed = []
    window.sigClosed.connect(lambda: closed.append(1))
    window.close()
    assert not window.isVisible()
    assert closed == [1]


def test_activate_emits_sigActivated(qtbot):
    window = _window(qtbot, _result(np.zeros((8, 8), np.uint8), "YX"))
    with qtbot.waitSignal(window.sigActivated, timeout=1000):
        window.activate()


def test_scale_bar_only_for_calibrated_units(qtbot):
    calibrated = _window(qtbot, _result(np.zeros((20, 20), np.uint8), "YX",
                                        axis_scales=[0.1, 0.1], scale_unit="um"))
    calibrated.show()
    calibrated._updateScaleBar()
    assert calibrated.scaleBarVisible()
    plain = _window(qtbot, _result(np.zeros((20, 20), np.uint8), "YX"))
    plain.show()
    plain._updateScaleBar()
    assert not plain.scaleBarVisible()


@pytest.mark.parametrize("maximum, expected", [
    (7.3, 5.0), (2.0, 2.0), (1.0, 1.0), (0.26, 0.2), (130.0, 100.0), (0.0, 0.0), (-3.0, 0.0),
])
def test_nice_length(maximum, expected):
    assert nice_length(maximum) == pytest.approx(expected)


def test_lut_tables():
    assert lut_table("grayclip")[-1].tolist() == [255, 0, 0]
    assert lut_table("gray")[-1].tolist() == [255, 255, 255]
    assert lut_table("green")[-1].tolist() == [0, 255, 0]
    assert lut_table("viridis").shape == (256, 3)
    assert lut_table("no-such-map").tolist() == lut_table("gray").tolist()
