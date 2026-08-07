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
    # The plot's own curves are reported alongside the manual measurement:
    # pushing used to send the Δx row and nothing else, so a plot carrying fit
    # parameters put none of them in the table.
    kinds = [record["kind"] for record in records]
    assert kinds.count("graph-series") == len(_payload().series)
    assert kinds[-1] == "graph-delta-x"
    assert records[-1] == {
        "kind": "graph-delta-x",
        "plot": "Peaks",
        "x_axis": "Distance (µm)",
        "x_1": 12.5,
        "x_2": 37.5,
        "delta_x": 25.0,
    }
    assert all(key in columns for key in records[-1])
    assert all(key in columns for key in records[0])


def test_push_reports_the_plot_without_any_measurement():
    """The button used to require Δx markers and silently do nothing without
    them — which is what made pressing it on a fit plot look broken."""
    from imswitch.improcess.view.GraphWidget import GraphWidget

    widget = GraphWidget()
    pushed = []
    widget.sigResultPushed.connect(
        lambda columns, records: pushed.append((list(columns), list(records)))
    )
    widget.setPlotPayloads([_payload()])

    assert widget.pushMeasurementButton.isEnabled()
    widget.pushMeasurementButton.click()

    assert len(pushed) == 1
    _columns, records = pushed[0]
    assert records and all(record["kind"] == "graph-series" for record in records)


def test_push_carries_the_payload_parameters():
    """A fit's coefficients ride in PlotPayload.metadata, which nothing else
    renders; every pushed row has to carry them or they stay invisible."""
    from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
    from imswitch.improcess.view.GraphWidget import GraphWidget

    widget = GraphWidget()
    pushed = []
    widget.sigResultPushed.connect(
        lambda columns, records: pushed.append((list(columns), list(records)))
    )
    widget.setPlotPayloads([
        PlotPayload(
            title="Off-switching kinetics",
            x_label="time (ms)",
            series=[PlotSeries(name="decay", y=np.array([1.0, 0.5, 0.25]))],
            metadata={"t_half_ms": 12.5, "fit1_r2": 0.998},
        )
    ])

    widget.pushMeasurementButton.click()

    _columns, records = pushed[0]
    assert records[0]["t_half_ms"] == 12.5
    assert records[0]["fit1_r2"] == 0.998


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
