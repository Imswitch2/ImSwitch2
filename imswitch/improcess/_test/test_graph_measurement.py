"""Interactive horizontal-distance measurements in the shared Graph panel."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model import PlotPayload, PlotSeries
from imswitch.improcess.model.plotting import build_graph_delta_x_record


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _payload(title="Peaks", x=None):
    if x is None:
        x = np.array([10.0, 20.0, 30.0, 40.0])
    return PlotPayload(
        title=title,
        x_label="Distance (µm)",
        y_label="Intensity",
        series=[
            PlotSeries(
                name="profile",
                x=np.asarray(x, dtype=float),
                y=np.array([0.0, 4.0, 1.0, 3.0]),
                kind="line",
            )
        ],
    )


def test_graph_delta_x_record_sorts_endpoints():
    record = build_graph_delta_x_record(_payload(), 8.5, 2.0)

    assert record == {
        "kind": "graph-delta-x",
        "plot": "Peaks",
        "x_axis": "Distance (µm)",
        "x_1": 2.0,
        "x_2": 8.5,
        "delta_x": 6.5,
    }


@pytest.mark.usefixtures("qapp")
def test_measurement_region_updates_readout_and_pushes_table_record():
    from imswitch.improcess.view.GraphWidget import GraphWidget

    widget = GraphWidget()
    pushed = []
    widget.sigResultPushed.connect(
        lambda columns, records: pushed.append((list(columns), list(records)))
    )
    widget.setPlotPayloads([_payload()])

    widget.measureButton.setChecked(True)
    assert widget._measurementRegion is not None
    widget._measurementRegion.setRegion((12.5, 37.5))

    assert "Δx=25" in widget.measurementLabel.text()
    assert widget.pushMeasurementButton.isEnabled()

    widget.pushMeasurementButton.click()
    assert len(pushed) == 1
    columns, records = pushed[0]
    assert columns == ["kind", "plot", "x_axis", "x_1", "x_2", "delta_x"]
    assert records == [{
        "kind": "graph-delta-x",
        "plot": "Peaks",
        "x_axis": "Distance (µm)",
        "x_1": 12.5,
        "x_2": 37.5,
        "delta_x": 25.0,
    }]


@pytest.mark.usefixtures("qapp")
def test_selecting_another_plot_recreates_measurement_for_its_x_range():
    from imswitch.improcess.view.GraphWidget import GraphWidget

    widget = GraphWidget()
    widget.setPlotPayloads([
        _payload("First"),
        _payload("Second", x=np.array([100.0, 200.0, 300.0, 400.0])),
    ])
    widget.measureButton.setChecked(True)
    first_region = widget._measurementRegion

    widget.plotSelector.setCurrentIndex(1)

    assert widget._measurementRegion is not None
    assert widget._measurementRegion is not first_region
    assert widget._measurementRegion.getRegion() == pytest.approx((200.0, 300.0))

    widget.clear()
    assert not widget.measureButton.isEnabled()
    assert not widget.measureButton.isChecked()
    assert widget._measurementRegion is None
    assert not widget.pushMeasurementButton.isEnabled()
