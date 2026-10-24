"""FlipMirrorWidget rows with N named positions (buttons up to MAX_BUTTON_POSITIONS, a drop-down beyond).

Imports go through the package, never ``...view.widgets.FlipMirrorWidget`` as a module name for the class
(see the note in test_dock_panel_sizing.py); the module constants are read from ``sys.modules``.
"""
import sys
from types import SimpleNamespace

import pytest
from qtpy import QtWidgets

from imswitch.imcontrol.view.widgets import FlipMirrorWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory

pytestmark = pytest.mark.nohardware


def widget_module():
    return sys.modules[FlipMirrorWidget.__module__]


@pytest.fixture
def widget(qtbot):
    w = WidgetFactory(SimpleNamespace()).createWidget(FlipMirrorWidget)
    qtbot.addWidget(w)
    return w


def checked(widget, name):
    return [s for s, b in widget.rows[name]["stateButtons"].items() if b.isChecked()]


def test_the_default_is_the_classic_two_state_row(widget):
    widget.addFlipMirror("M")
    row = widget.rows["M"]
    assert [b.text() for b in row["stateButtons"].values()] == ["0", "1"]
    assert row["state0Btn"].objectName() == "state0Btn" and row["state1Btn"].objectName() == "state1Btn"
    assert "stateCombo" not in row


def test_two_named_states_keep_their_texts_and_object_names(widget):
    widget.addFlipMirror("M", {0: "Out", "1": "In"})
    row = widget.rows["M"]
    assert row["state0Btn"].text() == "Out" and row["state1Btn"].text() == "In"
    assert row["state0Btn"].property("edge") == "first" and row["state1Btn"].property("edge") == "last"


def test_signals_carry_the_state_number(widget, qtbot):
    widget.addFlipMirror("M", {0: "Out", 1: "In"})
    with qtbot.waitSignal(widget.sigStateChanged) as blocker:
        widget.rows["M"]["state1Btn"].click()
    assert blocker.args == ["M", 1]


def test_set_state_checks_exactly_one_button_and_is_silent(widget, qtbot):
    widget.addFlipMirror("M", {0: "Out", 1: "In"})
    seen = []
    widget.sigStateChanged.connect(lambda n, s: seen.append((n, s)))
    widget.setState("M", 1)
    assert checked(widget, "M") == [1]
    widget.setState("M", "0")
    assert checked(widget, "M") == [0]
    widget.setState("M", 7)                                   # a state the row does not have: none shown
    assert checked(widget, "M") == []
    assert seen == []


def test_three_and_four_positions_are_buttons_sorted_numerically(widget):
    widget.addFlipMirror("T", {"2": "c", "0": "a", "1": "b"})
    row = widget.rows["T"]
    assert [b.text() for b in row["stateButtons"].values()] == ["a", "b", "c"]
    assert [row[f"state{i}Btn"].property("edge") for i in range(3)] == ["first", "middle", "last"]
    widget.setState("T", 2)
    assert checked(widget, "T") == [2]
    widget.addFlipMirror("F", {i: f"p{i}" for i in range(widget_module().MAX_BUTTON_POSITIONS)})
    assert "stateCombo" not in widget.rows["F"]
    assert len(widget.rows["F"]["stateButtons"]) == widget_module().MAX_BUTTON_POSITIONS


def test_numeric_sort_is_not_alphabetic(widget):
    widget.addFlipMirror("W", {str(i): f"p{i}" for i in range(12)})      # "10" < "2" as text
    combo = widget.rows["W"]["stateCombo"]
    assert [combo.itemText(i) for i in range(12)] == [f"p{i}" for i in range(12)]
    assert [combo.itemData(i) for i in range(12)] == list(range(12))


def test_a_ten_position_wheel_is_a_drop_down_with_the_same_signal_and_set_state(widget, qtbot):
    names = {i: f"pos{i}" for i in range(10)}
    widget.addFlipMirror("Wheel", names)
    row = widget.rows["Wheel"]
    combo = row["stateCombo"]
    assert isinstance(combo, QtWidgets.QComboBox) and "stateButtons" not in row
    seen = []
    widget.sigStateChanged.connect(lambda n, s: seen.append((n, s)))
    widget.setState("Wheel", 3)
    assert combo.currentText() == "pos3" and seen == []        # silent, like the buttons
    with qtbot.waitSignal(widget.sigStateChanged) as blocker:
        combo.activated.emit(7)                                # what a user's pick emits
    assert blocker.args == ["Wheel", 7]
    widget.setState("Wheel", 99)
    assert combo.currentIndex() == -1


def test_a_state_numbering_with_gaps_keeps_the_real_state_numbers(widget, qtbot):
    widget.addFlipMirror("G", {0: "a", 2: "b"})
    with qtbot.waitSignal(widget.sigStateChanged) as blocker:
        widget.rows["G"]["state1Btn"].click()                  # second button is state 2
    assert blocker.args == ["G", 2]
    widget.setState("G", 2)
    assert checked(widget, "G") == [2]


def test_row_state_enables_and_disables_both_renderings(widget):
    widget.addFlipMirror("B", {0: "a", 1: "b"})
    widget.addFlipMirror("C", {i: str(i) for i in range(10)})
    widget.setRowState("B", connected=False)
    widget.setRowState("C", connected=False)
    assert not any(b.isEnabled() for b in widget.rows["B"]["stateButtons"].values())
    assert not widget.rows["C"]["stateCombo"].isEnabled()
    widget.setAllEnabled(True)
    assert all(b.isEnabled() for b in widget.rows["B"]["stateButtons"].values())
    assert widget.rows["C"]["stateCombo"].isEnabled()


def test_a_state_name_key_that_is_not_a_number_is_refused(widget):
    with pytest.raises(ValueError, match="keyed by state numbers"):
        widget.addFlipMirror("X", {"left": "a", "right": "b"})
    with pytest.raises(ValueError, match="keyed by state numbers"):
        widget.addFlipMirror("X", {-1: "a", 0: "b"})
    assert "X" not in widget.rows
