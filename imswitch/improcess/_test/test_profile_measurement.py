"""Manual horizontal-distance measurements in the Profile panel."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.mark.usefixtures("qapp")
def test_profile_delta_x_is_pushed_with_profile_record():
    from imswitch.improcess.view.ProfileWidget import ProfileWidget

    widget = ProfileWidget(SimpleNamespace(layers=[]))
    x = np.linspace(0.0, 10.0, 11)
    y = np.sin(x) + 2.0
    widget._last_kind = "line"
    widget._last_payload = [("line", x, y)]
    widget._current_record_inputs = [
        ("line", x, y, 10.0, 10.0, "µm", None)
    ]
    widget.plot.clear()
    widget.plot.plot(x, y)
    widget.measureButton.setEnabled(True)
    widget.measureButton.setChecked(True)
    widget._measurementRegion.setRegion((2.25, 7.75))

    pushed = []
    widget.sigResultPushed.connect(
        lambda columns, records: pushed.append((list(columns), list(records)))
    )
    widget.pushButton.click()

    assert len(pushed) == 1
    columns, records = pushed[0]
    assert len(records) == 2
    assert records[0]["kind"] == "line"
    assert records[1] == {
        "kind": "profile-delta-x",
        "plot": "Line Profile",
        "x_axis": "Distance (µm)",
        "x_1": 2.25,
        "x_2": 7.75,
        "delta_x": 5.5,
    }
    assert all(key in columns for key in records[1])
    assert "Δx=5.5" in widget.measurementSummary.text()


@pytest.mark.usefixtures("qapp")
def test_profile_save_csv_includes_delta_x(tmp_path, monkeypatch):
    from qtpy import QtWidgets

    from imswitch.improcess.view.ProfileWidget import ProfileWidget
    from imswitch.improcess.view.ResultsTableWidget import records_from_csv

    widget = ProfileWidget(SimpleNamespace(layers=[]))
    x = np.linspace(0.0, 4.0, 5)
    y = np.array([0.0, 3.0, 1.0, 4.0, 0.0])
    widget._last_kind = "line"
    widget._last_payload = [("line", x, y)]
    widget._current_record_inputs = [
        ("line", x, y, 4.0, 4.0, "µm", None)
    ]
    widget.measureButton.setEnabled(True)
    widget.measureButton.setChecked(True)
    widget._measurementRegion.setRegion((1.0, 3.0))

    path = tmp_path / "profile.csv"
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(path), "CSV (*.csv)"),
    )
    widget.saveButton.click()

    columns, records = records_from_csv(path.read_text(encoding="utf-8"))
    assert len(records) == 2
    assert records[1]["kind"] == "profile-delta-x"
    assert records[1]["delta_x"] == 2.0
    assert "delta_x" in columns
