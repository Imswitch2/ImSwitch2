"""Apply on the ImProcess section writes the block as it was received.

Before the ``as_written`` policy one Edit -> Apply dropped every key the
template did not list (``liveStallTimeoutS``, ``napariEndpoints``, anything a
newer ImSwitch wrote) and expanded a four-key block into every template field.
These tests drive the real dialog and compare the result with the input, kind
for kind.
"""

import copy
import json

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QCheckBox

from imswitch.imcontrol.view.configeditor import editor
from imswitch.imcontrol.view.configeditor.editor import SectionEditorDialog

from imswitch.imcontrol._test.unit.configeditor_testing import SHIPPED_SETUPS, assert_json_identical

PROCESSING = editor.SECTION_SCHEMAS["processing"]

#: The shipped live setup's block plus one value of every kind the old dialog lost.
LIVE_BLOCK = {
    "reconstructors": ["monalisa", "view-only"],
    "processors": ["resize", "drift-correct", "convert-type"],
    "fileWatcherPanel": True,
    "reconstructionPanel": True,
}


def _rich_block() -> dict:
    block = copy.deepcopy(LIVE_BLOCK)
    block.update({
        "liveStallTimeoutS": 0,
        "liveViewerUpdateIntervalS": 0.5,
        "napariEndpoints": [{"id": "x", "plugin": "p"}],
        "someFutureKey": 1,
    })
    return block


def _apply(schema, current):
    dlg = SectionEditorDialog("processing", schema, current, {})
    dlg._try_apply()
    return dlg


def test_the_template_asks_for_as_written(qapp):
    assert PROCESSING["write_policy"] == "as_written"
    assert editor._writes_as_written(PROCESSING)


def test_apply_untouched_returns_exactly_the_input(qapp):
    block = _rich_block()
    dlg = _apply(PROCESSING, copy.deepcopy(block))
    assert dlg.result_data, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, block)
    assert dlg.result_data["liveStallTimeoutS"] == 0
    assert isinstance(dlg.result_data["liveStallTimeoutS"], int)


def test_apply_untouched_keeps_the_files_key_order(qapp):
    block = _rich_block()
    dlg = _apply(PROCESSING, copy.deepcopy(block))
    assert list(dlg.result_data) == list(block)


def test_a_four_key_block_is_not_expanded(qapp):
    dlg = _apply(PROCESSING, copy.deepcopy(LIVE_BLOCK))
    assert_json_identical(dlg.result_data, LIVE_BLOCK)


def test_touching_one_checkbox_changes_only_that_key(qapp):
    block = copy.deepcopy(LIVE_BLOCK)
    dlg = SectionEditorDialog("processing", PROCESSING, copy.deepcopy(block), {})
    box = dlg._field_widgets["graphPanel"]._w
    assert isinstance(box, QCheckBox) and not box.isChecked()
    box.click()
    dlg._try_apply()
    assert_json_identical(dlg.result_data, {**block, "graphPanel": True})


def test_touching_a_present_checkbox_back_to_default_still_writes_it(qapp):
    """The file said True; the operator unticked it: the file now says False."""
    block = copy.deepcopy(LIVE_BLOCK)
    dlg = SectionEditorDialog("processing", PROCESSING, copy.deepcopy(block), {})
    dlg._field_widgets["fileWatcherPanel"]._w.click()
    dlg._try_apply()
    assert dlg.result_data["fileWatcherPanel"] is False


def test_an_absent_number_shows_empty_and_stays_absent_untouched(qapp):
    dlg = SectionEditorDialog("processing", PROCESSING, copy.deepcopy(LIVE_BLOCK), {})
    assert dlg._field_widgets["liveStallTimeoutS"]._w.text() == ""
    dlg._try_apply()
    assert "liveStallTimeoutS" not in dlg.result_data


def test_typing_a_number_into_the_empty_box_writes_a_number(qapp):
    from PyQt5.QtTest import QTest

    dlg = SectionEditorDialog("processing", PROCESSING, copy.deepcopy(LIVE_BLOCK), {})
    box = dlg._field_widgets["liveStallTimeoutS"]._w
    QTest.keyClicks(box, "90")
    dlg._try_apply()
    assert dlg.result_data["liveStallTimeoutS"] == 90
    assert isinstance(dlg.result_data["liveStallTimeoutS"], int)


def test_clearing_a_present_number_writes_null(qapp):
    """Emptying the box is how the operator says 'automatic' to a file that pinned it."""
    from PyQt5.QtTest import QTest

    dlg = SectionEditorDialog("processing", PROCESSING, _rich_block(), {})
    box = dlg._field_widgets["liveStallTimeoutS"]._w
    box.clear()
    QTest.keyClicks(box, " ")
    dlg._try_apply()
    assert dlg.result_data["liveStallTimeoutS"] is None


def test_unknown_keys_land_on_the_custom_tab(qapp):
    dlg = SectionEditorDialog("processing", PROCESSING, _rich_block(), {})
    assert "someFutureKey" in dlg._custom_widgets
    assert "liveStallTimeoutS" not in dlg._custom_widgets


def test_a_new_section_seeds_only_the_required_fields(qapp):
    seed = editor._new_section_seed(PROCESSING)
    assert set(seed) == {"reconstructors"}
    assert seed["reconstructors"] == ["view-only"]
    dlg = _apply(PROCESSING, seed)
    assert_json_identical(dlg.result_data, seed)


def test_a_new_section_does_not_gain_keys_on_apply(qapp):
    dlg = _apply(PROCESSING, editor._new_section_seed(PROCESSING))
    assert list(dlg.result_data) == ["reconstructors"]


def test_other_schemas_still_seed_every_field():
    scan = editor.SECTION_SCHEMAS["scan"]
    assert not editor._writes_as_written(scan)
    assert editor._new_section_seed(scan) == editor._build_default_section(scan)


def test_a_schema_without_the_policy_still_writes_every_field(qapp):
    """Regression guard: the policy is opt-in, the old behaviour stays the default."""
    scan = editor.SECTION_SCHEMAS["scan"]
    dlg = SectionEditorDialog("scan", scan, {}, {})
    dlg._try_apply()
    keys = {f["key"] for f in scan["fields"]}
    assert dlg.result_data, dlg._err_lbl.text()
    assert set(dlg.result_data) == keys
    assert not dlg._allow_custom


def _shipped_processing_blocks():
    out = []
    for path in sorted(SHIPPED_SETUPS.glob("*.json")):
        block = json.loads(path.read_text(encoding="utf-8")).get("processing")
        if isinstance(block, dict) and block:
            out.append(pytest.param(block, id=path.stem))
    return out


@pytest.mark.parametrize("block", _shipped_processing_blocks())
def test_every_shipped_processing_block_applies_untouched_unchanged(qapp, block):
    dlg = _apply(PROCESSING, copy.deepcopy(block))
    assert dlg.result_data, dlg._err_lbl.text()
    assert_json_identical(dlg.result_data, block)


def test_there_are_shipped_blocks_to_check():
    assert len(_shipped_processing_blocks()) >= 5
