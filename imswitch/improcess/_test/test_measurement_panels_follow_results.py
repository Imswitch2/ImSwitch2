"""Measurement panels must answer for the result that is on screen.

Profile, ROI stats and the ROI manager read their pixels from the napari
viewer, which switching reconstruction changes underneath them. Nothing told
them a result changed, so they kept showing numbers measured on the previous
one — indistinguishable, on screen, from fresh ones.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets

from imswitch.improcess.view.GraphWidget import GraphWidget
from imswitch.improcess.view.ProfileWidget import ProfileWidget


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class _Layer:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.visible = True
        self.scale = (1.0, 1.0)
        self.metadata = {"scale_unit": "px"}
        self.ndim = data.ndim


class _Layers(list):
    def __init__(self):
        super().__init__()
        self.selection = SimpleNamespace(active=None)


class _Viewer:
    """Just enough napari for the widgets' layer/dims lookups."""

    def __init__(self):
        self.layers = _Layers()
        self.dims = SimpleNamespace(
            events=SimpleNamespace(
                current_step=SimpleNamespace(connect=lambda _fn: None)
            ),
            ndisplay=2,
            current_step=(0,),
        )


def _ramp(name, peak):
    return _Layer(name, np.tile(np.linspace(0.0, peak, 64), (64, 1)).astype(np.float32))


def _profile_widget(layer):
    viewer = _Viewer()
    viewer.layers.append(layer)
    viewer.layers.selection.active = layer
    widget = ProfileWidget(viewer)
    # The ROI itself lives in the viewer's shapes layer; stand in for it so
    # the test exercises the recompute path rather than napari's shape stack.
    widget._findFirstShape = (
        lambda kind: ((32.0, 0.0), (32.0, 63.0)) if kind == "line" else None
    )
    return widget, viewer


def test_profile_follows_the_selected_result(qapp):
    first, second = _ramp("recA", 10.0), _ramp("recB", 100.0)
    widget, viewer = _profile_widget(first)
    widget._plotLineProfile(widget._findFirstShape("line"))
    assert float(widget._last_payload[0][2].max()) == pytest.approx(10.0)

    viewer.layers[0] = second
    viewer.layers.selection.active = second
    widget.setCurrentResult(object())

    assert float(widget._last_payload[0][2].max()) == pytest.approx(100.0)


def test_profile_pushes_a_named_plot_to_the_graph(qapp):
    widget, _viewer = _profile_widget(_ramp("recA", 10.0))
    widget._plotLineProfile(widget._findFirstShape("line"))
    pushed = []
    widget.sigPlotPushed.connect(pushed.append)

    widget._onPushToGraph()

    assert len(pushed) == 1
    payload = pushed[0]
    assert payload.title == "recA — line profile"
    assert payload.metadata["source_layer"] == "recA"
    assert [series.name for series in payload.series] == ["line"]


def test_pushed_plot_carries_the_fit_curve(qapp):
    widget, _viewer = _profile_widget(_ramp("recA", 10.0))
    widget.fitCombo.setCurrentIndex(widget.fitCombo.findData("gaussian"))
    widget._plotLineProfile(widget._findFirstShape("line"))

    payload = widget.buildPlotPayload()

    assert len(payload.series) == len(widget._last_payload) + len(
        widget._last_fit_curves
    )


def test_two_profiles_from_two_results_can_be_compared(qapp):
    """The point of pushing: a curve from result A survives selecting B."""
    first, second = _ramp("recA", 10.0), _ramp("recB", 100.0)
    widget, viewer = _profile_widget(first)
    graph = GraphWidget()
    widget.sigPlotPushed.connect(graph.addPlotPayload)

    widget._plotLineProfile(widget._findFirstShape("line"))
    widget._onPushToGraph()

    viewer.layers[0] = second
    viewer.layers.selection.active = second
    widget.setCurrentResult(object())
    widget._onPushToGraph()

    # Selecting a result with no plots of its own must not drop the pushed ones.
    graph.setPlotPayloads([])

    titles = [payload.title for payload in graph.pinnedPayloads()]
    assert titles == ["recA — line profile", "recB — line profile"]
    assert graph.plotSelector.count() == 2


def test_pushing_the_same_profile_twice_replaces_it(qapp):
    widget, _viewer = _profile_widget(_ramp("recA", 10.0))
    graph = GraphWidget()
    widget.sigPlotPushed.connect(graph.addPlotPayload)
    widget._plotLineProfile(widget._findFirstShape("line"))

    widget._onPushToGraph()
    widget._onPushToGraph()

    assert len(graph.pinnedPayloads()) == 1


def test_graph_overlays_every_curve_for_comparison(qapp):
    widget, viewer = _profile_widget(_ramp("recA", 10.0))
    graph = GraphWidget()
    widget.sigPlotPushed.connect(graph.addPlotPayload)
    widget._plotLineProfile(widget._findFirstShape("line"))
    widget._onPushToGraph()
    second = _ramp("recB", 100.0)
    viewer.layers[0] = second
    viewer.layers.selection.active = second
    widget.setCurrentResult(object())
    widget._onPushToGraph()

    graph.overlayButton.setChecked(True)

    drawn = [item for item in graph.plot.plotItem.items if hasattr(item, "xData")]
    assert len(drawn) == 2


def test_pushed_rows_name_the_result_they_were_measured_on(qapp):
    """Two profiles pushed from two reconstructions land in one accumulating
    table; without a source column the rows cannot be told apart."""
    first, second = _ramp("recA", 10.0), _ramp("recB", 100.0)
    widget, viewer = _profile_widget(first)
    pushed = []
    widget.sigResultPushed.connect(lambda _columns, records: pushed.extend(records))

    widget._plotLineProfile(widget._findFirstShape("line"))
    widget.pushButton.click()

    viewer.layers[0] = second
    viewer.layers.selection.active = second
    widget.setCurrentResult(object())
    widget.pushButton.click()

    assert [record["source"] for record in pushed] == ["recA", "recB"]
    assert pushed[0]["max"] != pushed[1]["max"]


def test_clearing_pushed_plots_keeps_the_result_plots(qapp):
    from imswitch.improcess.model.plotting import PlotPayload, PlotSeries

    graph = GraphWidget()
    own = PlotPayload(
        title="result plot",
        series=[PlotSeries(name="s", y=np.arange(4, dtype=float))],
    )
    graph.setPlotPayloads([own])
    graph.addPlotPayload(
        PlotPayload(
            title="pushed", series=[PlotSeries(name="p", y=np.arange(4, dtype=float))]
        )
    )
    assert graph.plotSelector.count() == 2

    graph.clearPinnedPayloads()

    assert graph.pinnedPayloads() == []
    assert graph.plotSelector.count() == 1
