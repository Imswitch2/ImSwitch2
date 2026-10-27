"""The Fiji import chooser: rows, origins, default selection, refresh."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore  # noqa: E402

from imswitch.improcess.fiji.convert import SentRecord  # noqa: E402
from imswitch.improcess.fiji.protocol import FijiImageDescription  # noqa: E402
from imswitch.improcess.view.FijiImportDialog import (  # noqa: E402
    COLUMNS,
    RGB_TOOLTIP,
    FijiImportDialog,
    format_size,
)


def _desc(image_id, *, title="img", dims=(5, 4, 1, 1, 1), bit_depth=16):
    return FijiImageDescription.from_dict({
        "image_id": image_id, "title": title, "dims_xyczt": list(dims), "bit_depth": bit_depth,
        "pixel_width": 1.0, "pixel_height": 1.0, "pixel_depth": 1.0,
        "x_unit": "pixel", "y_unit": "pixel", "z_unit": "pixel",
        "x_origin": 0.0, "y_origin": 0.0, "z_origin": 0.0,
        "frame_interval": 0.0, "time_unit": "sec", "properties": {}, "channel_labels": [], "changes": False,
    })


def _record(image_id):
    return SentRecord(image_id, "uid", ("Y", "X"), (1, 1, 1, 4, 5))


def _cell(dialog, row, column):
    return dialog.table.item(row, COLUMNS.index(column)).text()


@pytest.fixture
def dialog(qapp):
    descriptions = [
        _desc(-1, title="sent one"),
        _desc(-2, title="derived", dims=(512, 256, 3, 10, 4), bit_depth=8),
        _desc(-3, title="foreign", bit_depth=32),
        _desc(-4, title="already", bit_depth=16),
        _desc(-5, title="color", bit_depth=24),
    ]
    return FijiImportDialog(
        descriptions, sent={-1: _record(-1)}, imported={-4},
        source_names={-1: "src", -2: "src", -3: None, -4: None, -5: None},
    )


def test_rows_and_columns(dialog):
    assert dialog.table.rowCount() == 5
    assert [dialog.table.horizontalHeaderItem(i).text() for i in range(6)] == list(COLUMNS)
    assert _cell(dialog, 1, "Size") == "512×256, 3 c, 10 z, 4 t"
    assert _cell(dialog, 0, "Size") == "5×4"
    assert [_cell(dialog, r, "Type") for r in range(5)] == ["16-bit", "8-bit", "32-bit", "16-bit", "RGB"]
    assert _cell(dialog, 0, "ImageJ id") == "-1"


def test_origins_and_imported_mark(dialog):
    assert _cell(dialog, 0, "Origin") == "sent from src"
    assert _cell(dialog, 1, "Origin") == "derived from src (lineage hint)"
    assert _cell(dialog, 2, "Origin") == "unknown"
    assert [_cell(dialog, r, "Imported") for r in range(5)] == ["", "", "", "✓", ""]


def test_default_selection_is_unsent_and_unimported(dialog):
    # -1 was sent, -4 was imported, -5 cannot be fetched
    assert dialog.selectedIds() == [-2, -3]


def test_rgb_rows_are_disabled_with_a_tooltip(dialog):
    item = dialog.table.item(4, 0)
    assert not item.flags() & QtCore.Qt.ItemIsEnabled and not item.flags() & QtCore.Qt.ItemIsSelectable
    assert item.toolTip() == RGB_TOOLTIP
    dialog.table.selectAll()
    assert -5 not in dialog.selectedIds()


def test_selected_ids_follow_table_order(dialog):
    dialog.table.clearSelection()
    for row in (3, 0, 2):
        dialog.table.selectRow(row)
    assert dialog.selectedIds() == [-1, -3, -4]


def test_refresh_emits_and_set_descriptions_keeps_the_user_selection(dialog, qtbot):
    dialog.table.clearSelection()
    dialog.table.selectRow(0)                                   # the user picks the sent image only
    with qtbot.waitSignal(dialog.sigRefreshRequested, timeout=1000):
        dialog.refreshButton.click()
    dialog.setDescriptions(
        [_desc(-1, title="sent one"), _desc(-6, title="newcomer"), _desc(-2, title="derived", bit_depth=8)],
        sent={-1: _record(-1)}, imported=set(), source_names={},
    )
    assert dialog.table.rowCount() == 3
    # -1 stays selected (the user's choice), -2 was seen and not chosen, -6 is new: default selected
    assert dialog.selectedIds() == [-1, -6]


def test_size_text_leaves_out_single_planes():
    assert format_size((5, 4, 1, 1, 1)) == "5×4"
    assert format_size((5, 4, 2, 1, 1)) == "5×4, 2 c"
    assert format_size((5, 4, 1, 7, 1)) == "5×4, 7 z"
