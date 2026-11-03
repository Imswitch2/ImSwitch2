from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.view.LoadedDataFrame import LoadedDataFrame  # noqa: E402


_APP = None


@pytest.fixture(scope="module")
def qapp():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _data(path, name):
    return SimpleNamespace(
        sourceKind="image",
        dataPath=str(path),
        sourceOriginalPath=str(path),
        datasetName="Camera",
        name=name,
    )


def test_loaded_catalog_is_lightweight_and_current_does_not_steal_selection(qapp, tmp_path):
    frame = LoadedDataFrame()
    a = _data(tmp_path / "a.h5", "a.h5")
    b = _data(tmp_path / "b.h5", "b.h5")
    frame.addOrUpdateDataObj(a, acquisition="XY")
    frame.addOrUpdateDataObj(b, acquisition="YZ")

    assert frame.dataList.columnCount() == 2
    frame.dataList.topLevelItem(1).setSelected(True)
    frame.setCurrentDataObj(a)

    assert frame.dataList.topLevelItem(0).font(0).bold()
    assert not frame.dataList.topLevelItem(1).font(0).bold()
    assert frame.selectedDataObjs() == [b]

    frame.deleteLater()


def test_select_sources_selects_whole_quick_load_group(qapp, tmp_path):
    frame = LoadedDataFrame()
    a_path = tmp_path / "a.h5"
    b_path = tmp_path / "b.h5"
    c_path = tmp_path / "c.h5"
    a, b, c = (_data(a_path, "a"), _data(b_path, "b"), _data(c_path, "c"))
    for obj in (a, b, c):
        frame.addOrUpdateDataObj(obj)

    frame.selectSources([a_path, c_path])
    assert set(map(id, frame.selectedDataObjs())) == {id(a), id(c)}

    frame.deleteLater()
