"""Walking the stack axis: Z Project over a range, and the Z-axis profile.

The in-plane profiles answer "how does intensity vary across the field". The
other question a stack raises — how it varies *through* the stack — had no
answer: the projection processor collapsed the whole axis with no way to
restrict it, and no panel plotted along it at all.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.projection import ProjectionProcessor
from imswitch.improcess.view.ProfileWidget import ProfileWidget


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# -- Z Project slice range ---------------------------------------------------

def _ramp_stack(n_z=6, shape=(8, 10)):
    """Stack whose plane z is filled with the value z."""
    data = np.zeros((n_z, *shape), dtype=np.float32)
    for z in range(n_z):
        data[z] = z
    return ArrayProcessingResult(
        name="rec", data=data, axis_labels=["Z", "Y", "X"]
    )


def test_projection_defaults_to_the_whole_axis():
    out = ProjectionProcessor().apply(_ramp_stack(), {"axis": "Z", "mode": "max"})

    assert out.data.max() == pytest.approx(5.0)
    assert out.name == "rec (max Z-projection)"


def test_projection_honours_a_slice_range():
    out = ProjectionProcessor().apply(
        _ramp_stack(), {"axis": "Z", "mode": "max", "start": 1, "stop": 4}
    )

    assert out.data.max() == pytest.approx(3.0)  # planes 1..3, not 5
    assert out.name == "rec (max Z-projection 2-4)"  # reported 1-based, as ImageJ


def test_projection_range_applies_to_the_statistic_too():
    out = ProjectionProcessor().apply(
        _ramp_stack(), {"axis": "Z", "mode": "mean", "start": 1, "stop": 4}
    )

    assert out.data.mean() == pytest.approx(2.0)  # mean of 1, 2, 3


def test_projection_range_clamps_instead_of_raising():
    """A bound left over from a longer stack is a stale number, not an error."""
    out = ProjectionProcessor().apply(
        _ramp_stack(), {"axis": "Z", "mode": "max", "start": 2, "stop": 99}
    )

    assert out.data.max() == pytest.approx(5.0)
    assert out.params["stop"] == 6


def test_projection_keeps_the_other_axes_of_a_hyperstack():
    data = np.zeros((5, 6, 8, 10), dtype=np.float32)
    data[:, 3] = 1.0
    result = ArrayProcessingResult(
        name="rec", data=data, axis_labels=["T", "Z", "Y", "X"]
    )

    out = ProjectionProcessor().apply(result, {"axis": "Z", "mode": "max"})

    assert out.axis_labels == ["T", "Y", "X"]
    assert out.data.shape == (5, 8, 10)


# -- Z-axis profile ----------------------------------------------------------

class _Shapes:
    def __init__(self, **_kwargs):
        self.data, self.shape_type, self.mode = [], [], "pan"
        self.edge_width = 1
        event = lambda: SimpleNamespace(connect=lambda _fn: None)  # noqa: E731
        self.events = SimpleNamespace(data=event(), mode=event())


class _Layer:
    def __init__(self, name, data, scale, labels, unit="um"):
        self.name, self.data, self.scale = name, data, scale
        self.visible, self.ndim = True, data.ndim
        self.metadata = {"scale_unit": unit, "axis_labels": list(labels)}


class _Layers(list):
    def __init__(self):
        super().__init__()
        self.selection = SimpleNamespace(active=None)


class _Viewer:
    def __init__(self, step):
        self.layers = _Layers()
        self.camera = SimpleNamespace(zoom=1.0)
        self.add_shapes = lambda **kwargs: _Shapes(**kwargs)
        self.dims = SimpleNamespace(
            events=SimpleNamespace(
                current_step=SimpleNamespace(connect=lambda _fn: None)
            ),
            ndisplay=2,
            current_step=step,
        )


def _gaussian_stack(n_z=10, centre=4.0, peak=100.0):
    z = np.arange(n_z, dtype=float)[:, None, None]
    return (np.exp(-((z - centre) ** 2) / 2.0) * peak).astype(np.float32) * np.ones(
        (n_z, 8, 12), dtype=np.float32
    )


def _widget(data, labels, scale, step):
    viewer = _Viewer(step)
    layer = _Layer("recZ", data, scale, labels)
    viewer.layers.append(layer)
    viewer.layers.selection.active = layer
    return ProfileWidget(viewer), viewer


def test_z_profile_walks_the_stack_axis_in_its_own_units(qapp):
    widget, _viewer = _widget(
        _gaussian_stack(), ["Z", "Y", "X"], (0.3, 0.065, 0.065), (0, 0, 0)
    )

    widget.zProfileButton.click()

    _name, z, means = widget._last_payload[0]
    assert means.size == 10
    assert z[1] == pytest.approx(0.3)  # the Z scale, not the in-plane one
    assert z[int(np.argmax(means))] == pytest.approx(1.2)  # plane 4 * 0.3
    assert widget.plot.getAxis("bottom").labelText == "Z (µm)"


def test_z_profile_measures_the_whole_frame_without_an_roi(qapp):
    """ImageJ plots the whole image when no ROI is set; requiring one here
    would make the obvious first click do nothing."""
    widget, _viewer = _widget(
        _gaussian_stack(), ["Z", "Y", "X"], (1.0, 1.0, 1.0), (0, 0, 0)
    )

    widget.zProfileButton.click()

    assert widget._last_kind == "zprofile"
    assert widget._last_payload


def test_z_profile_restricts_to_a_drawn_rectangle(qapp):
    data = _gaussian_stack()
    data[:, :4, :] = 0.0  # top half dark
    widget, _viewer = _widget(data, ["Z", "Y", "X"], (1.0, 1.0, 1.0), (0, 0, 0))

    widget._plotZProfile(None)
    whole_frame = widget._last_payload[0][2].max()
    widget._plotZProfile((4.0, 0.0, 8.0, 12.0))  # bright half only
    roi = widget._last_payload[0][2].max()

    assert roi > whole_frame  # the dark half no longer drags the mean down


def test_z_profile_of_a_hyperstack_uses_the_displayed_timepoint(qapp):
    data = np.stack([_gaussian_stack() * (t + 1) for t in range(3)])
    widget, _viewer = _widget(
        data, ["T", "Z", "Y", "X"], (1.0, 0.3, 0.065, 0.065), (2, 0, 0, 0)
    )

    widget.zProfileButton.click()

    _name, _z, means = widget._last_payload[0]
    assert means.size == 10  # walked Z, not T
    assert means.max() == pytest.approx(300.0, rel=1e-3)  # timepoint 2, scaled x3


def test_z_profile_needs_a_stack(qapp):
    widget, _viewer = _widget(
        np.zeros((8, 12), np.float32), ["Y", "X"], (1.0, 1.0), (0, 0)
    )

    widget.zProfileButton.click()

    assert widget._last_payload == []
    assert "stack" in widget.fitSummary.text()


def test_z_profile_pushes_with_the_right_axis(qapp):
    """A pushed payload has to describe the axis it walked; relabelling it
    'Distance' would put a wrong unit on the compared curve."""
    widget, _viewer = _widget(
        _gaussian_stack(), ["Z", "Y", "X"], (0.3, 0.065, 0.065), (0, 0, 0)
    )
    widget.zProfileButton.click()

    payload = widget.buildPlotPayload()

    assert payload.title == "recZ — Z profile"
    assert payload.x_label == "Z (µm)"


def test_z_profile_rows_reach_the_table(qapp):
    widget, _viewer = _widget(
        _gaussian_stack(), ["Z", "Y", "X"], (0.3, 0.065, 0.065), (0, 0, 0)
    )
    widget.zProfileButton.click()
    pushed = []
    widget.sigResultPushed.connect(lambda _columns, records: pushed.extend(records))

    widget.pushButton.click()

    assert pushed[0]["kind"] == "z-profile"
    assert pushed[0]["source"] == "recZ"
    assert pushed[0]["max"] == pytest.approx(100.0)
