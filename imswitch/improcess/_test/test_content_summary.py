"""The results / data lists say what each entry holds (icon + one line)."""

from __future__ import annotations

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.content_summary import (
    ICONS,
    ContentSummary,
    describe_data_obj,
    describe_result,
)
from imswitch.improcess.model.curve_result import CurveResult
from imswitch.improcess.model.labels_result import LabelsResult


def _image(shape, labels, dtype=np.uint16):
    return ArrayProcessingResult("r", np.zeros(shape, dtype=dtype), list(labels))


@pytest.mark.parametrize(
    "shape, labels, icon, label, detail",
    [
        ((512, 256), "YX", "image", "image", "512 × 256 · uint16"),
        ((21, 64, 64), "ZYX", "stack", "Z-stack", "Z 21 · 64 × 64 · uint16"),
        ((100, 64, 64), ["T", "Y", "X"], "time_series", "time series", "T 100 · 64 × 64 · uint16"),
        ((100, 64, 64), ["Frame", "Y", "X"], "time_series", "time series", "Frame 100 · 64 × 64 · uint16"),
        ((3, 64, 64), "CYX", "channels", "channels", "C 3 · 64 × 64 · uint16"),
        ((5, 21, 64, 64), ["T", "Z", "Y", "X"], "stack", "Z-stack", "T 5 · Z 21 · 64 × 64 · uint16"),
        ((7, 64, 64), ["", "Y", "X"], "stack", "stack", "? 7 · 64 × 64 · uint16"),
    ],
)
def test_images_by_axes(shape, labels, icon, label, detail):
    summary = describe_result(_image(shape, labels))
    assert (summary.icon, summary.label, summary.detail) == (icon, label, detail)
    assert summary.icon_name == ICONS[icon]


def test_curve_counts_its_points():
    curve = CurveResult("frc", np.linspace(0, 1, 128), ["frequency"])
    summary = describe_result(curve)
    assert (summary.icon, summary.label) == ("curve", "curve")
    assert summary.detail == "128 points along frequency"


def test_labels_are_not_images():
    labels = LabelsResult("mask", np.zeros((8, 8), dtype=np.int32), ["Y", "X"])
    summary = describe_result(labels)
    assert (summary.icon, summary.label) == ("labels", "labels")
    assert summary.detail == "8 × 8 · int32"


def test_a_single_value_is_a_number():
    value = ArrayProcessingResult("mean", np.array(3.25), [])
    summary = describe_result(value)
    assert (summary.icon, summary.label, summary.detail) == ("number", "value", "3.25")


class _Table:
    kind = "table"
    data = np.zeros((42, 5))
    axis_labels = ["row", "column"]


class _Points(_Table):
    kind = "localization"

    def __len__(self):
        return 12345


def test_tables_and_points_count_rows():
    assert describe_result(_Table()).line == "table · 42 rows"
    summary = describe_result(_Points())
    assert (summary.icon, summary.detail) == ("points", "12,345 localizations")


class _Measurement(_Table):
    list_icon = "mdi6.chart-bell-curve"

    def list_summary(self):
        return "PSF · 12/15 beads · lateral 231 nm"


def test_a_result_may_say_what_it_is_itself():
    summary = describe_result(_Measurement())
    assert summary.icon_name == "mdi6.chart-bell-curve"
    assert (summary.label, summary.detail) == ("PSF", "12/15 beads · lateral 231 nm")


class _Unopened:
    sourceKind = "image"
    _dataSource = None
    _data = None


class _Opened(_Unopened):
    _dataSource = object()
    dataMaterialized = False
    axis_labels = ["T", "Y", "X"]
    data_handle = np.zeros((10, 32, 32), dtype=np.float32)


class _Loaded(_Opened):
    dataMaterialized = True


def test_data_files_describe_only_what_is_known():
    assert describe_data_obj(_Unopened()) == ContentSummary("file", "file", "not opened")
    assert describe_data_obj(_Opened()).line == "time series · T 10 · 32 × 32 · float32 · on disk"
    assert describe_data_obj(_Loaded()).detail.endswith("· in memory")


def test_a_time_lapse_folder_is_a_folder():
    class Folder:
        sourceKind = "time_lapse"

    assert describe_data_obj(Folder()).icon == "folder"


# --------------------------------------------------------------------------- #
@pytest.fixture
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_delegate_draws_two_lines_without_touching_the_object_role(qapp):
    from imswitch.improcess.view.ContentDelegate import ContentDelegate

    view = QtWidgets.QListWidget()
    view.setItemDelegate(ContentDelegate(describe_result, view))
    result = _image((21, 64, 64), "ZYX")
    item = QtWidgets.QListWidgetItem("stack.0")
    item.setData(1, result)
    view.addItem(item)
    view.resize(220, 120)
    index = view.indexFromItem(item)
    option = view.viewOptions() if hasattr(view, "viewOptions") else QtWidgets.QStyleOptionViewItem()
    hint = view.itemDelegate().sizeHint(option, index)
    assert hint.height() >= 2 * view.fontMetrics().height()
    view.grab()  # paints every entry; must not raise
    assert item.data(1) is result
    assert view.itemDelegate().summaryFor(index).label == "Z-stack"
