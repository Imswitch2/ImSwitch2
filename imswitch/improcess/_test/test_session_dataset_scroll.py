"""Regression: Session row navigation must not jump the dataset viewport."""

from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")
from qtpy import QtCore, QtWidgets

from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController
from imswitch.improcess.view.ProcessingSessionFrame import ProcessingSessionFrame


def _entries(count=50):
    return [
        {
            "id": f"d{index:02}",
            "label": f"dataset-{index:02}",
            "search": f"dataset-{index:02}",
            "timestamp": float(count - index),
            "status": "— No result",
            "state": "raw",
            "info": "XY",
            "dimension": "XY",
            "dimension_label": "XY",
        }
        for index in range(count)
    ]


def _scrollable_session(qtbot):
    frame = ProcessingSessionFrame()
    qtbot.addWidget(frame)
    frame.resize(430, 340)
    frame.setSessionActive(True, "scroll regression")
    frame.show()
    frame.setDatasets(_entries())
    QtWidgets.QApplication.processEvents()
    assert frame.datasetList.verticalScrollBar().maximum() > 0
    return frame


def _row(frame, dataset_id):
    for index in range(frame.datasetList.topLevelItemCount()):
        item = frame.datasetList.topLevelItem(index)
        if item.data(0, QtCore.Qt.UserRole) == dataset_id:
            return item
    raise AssertionError(f"Dataset {dataset_id} is not visible")


def test_following_visible_dataset_keeps_scroll_but_offscreen_navigation_reveals(qtbot):
    frame = _scrollable_session(qtbot)
    tree = frame.datasetList
    target = _row(frame, "d30")
    tree.setCurrentItem(target)  # What Qt has already done when the user clicked.
    tree.scrollToItem(target, QtWidgets.QAbstractItemView.PositionAtTop)
    QtWidgets.QApplication.processEvents()
    scroll = tree.verticalScrollBar()
    before = scroll.value()
    assert before > 0

    # The selected result follows back into Session; no re-centering is allowed.
    assert frame.selectDataset("d30")
    assert scroll.value() == before
    assert frame.selectDataset("d30")
    assert scroll.value() == before

    # Following a result from elsewhere still reveals an off-screen source.
    assert frame.selectDataset("d49")
    QtWidgets.QApplication.processEvents()
    assert tree.viewport().rect().intersects(tree.visualItemRect(_row(frame, "d49")))


def test_double_click_load_updates_only_one_row_without_scrolling(qtbot):
    frame = _scrollable_session(qtbot)
    tree = frame.datasetList
    item = _row(frame, "d30")
    frame.selectDataset("d30")
    tree.scrollToItem(item, QtWidgets.QAbstractItemView.PositionAtTop)
    QtWidgets.QApplication.processEvents()
    scroll = tree.verticalScrollBar()
    before = scroll.value()
    dataset = SimpleNamespace(id="d30", label="dataset-30", primary_path="/tmp/d30.h5", kind="image", raw_deleted=False)
    status_calls = []
    render_calls = []
    fake = SimpleNamespace(
        _session=SimpleNamespace(dataset_by_id=lambda dataset_id: dataset if dataset_id == "d30" else None),
        _selectedDatasetId="d30",
        _fileIO=SimpleNamespace(_loadFromPath=lambda *_args, **_kwargs: "current"),
        _loadingDatasetId=None,
        _widget=SimpleNamespace(setStatus=status_calls.append),
    )

    def render_one(dataset_id):
        render_calls.append(dataset_id)
        entry = dict(item.data(0, QtCore.Qt.UserRole + 1))
        entry["status"] = "Loading…" if fake._loadingDatasetId else "— No result"
        entry["state"] = "loading" if fake._loadingDatasetId else "raw"
        frame.updateDataset(entry)

    fake._renderDataset = render_one
    fake._render = lambda: pytest.fail("Raw load must not rebuild the dataset catalog")
    ProcessingSessionController.loadSelectedRaw(fake)
    assert render_calls == ["d30", "d30"]
    assert status_calls == ["Loading dataset-30…", "Loaded dataset-30"]
    assert _row(frame, "d30") is item
    assert scroll.value() == before


def test_current_raw_change_updates_old_and_new_rows_only():
    calls = []
    fake = SimpleNamespace(
        _currentDatasetId="d01",
        _session=object(),
        _renderDataset=calls.append,
        _render=lambda: pytest.fail("Current-data change must not rebuild the catalog"),
    )
    fake._sync_current_dataset = lambda _obj: setattr(fake, "_currentDatasetId", "d02")
    ProcessingSessionController._currentDataChanged(fake, object())
    assert calls == ["d01", "d02"]
    calls.clear()
    ProcessingSessionController._currentDataChanged(fake, object())
    assert calls == ["d02"]  # The same active dataset need not render twice.


def test_catalog_refresh_keeps_existing_scroll_offset(qtbot):
    frame = _scrollable_session(qtbot)
    tree = frame.datasetList
    assert frame.selectDataset("d30")
    tree.scrollToItem(_row(frame, "d30"), QtWidgets.QAbstractItemView.PositionAtTop)
    QtWidgets.QApplication.processEvents()
    previous = tree.verticalScrollBar().value()
    assert previous > 0
    refreshed = _entries()
    refreshed[30]["status"] = "✓ Result (1)"
    refreshed[30]["state"] = "reconstructed"
    frame.setDatasets(refreshed)
    QtWidgets.QApplication.processEvents()
    assert tree.verticalScrollBar().value() == previous
    assert frame.currentDatasetId() == "d30"
