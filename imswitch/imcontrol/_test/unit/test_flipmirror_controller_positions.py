"""FlipMirrorController with N-position selectors: links need equal position counts, restored states are range-checked."""
import logging
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.FlipMirrorController import FlipMirrorController
from imswitch.imcontrol.view.widgets import FlipMirrorWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory

pytestmark = pytest.mark.nohardware


class FakeDevice:
    def __init__(self, count, state=0):
        self.count = count
        self.state = state
        self.moves = []

    def get_state_names(self):
        return {i: f"p{i}" for i in range(self.count)}

    def get_state(self):
        return self.state

    def move_to(self, state):
        if not 0 <= int(state) < self.count:
            raise ValueError(f"state {state} out of range")
        self.moves.append(int(state))
        self.state = int(state)

    def is_connected(self):
        return True

    def get_last_error(self):
        return None


class FakeManager:
    def __init__(self, devices):
        self.devices = devices

    def hasDevices(self):
        return True

    def __getitem__(self, name):
        return self.devices[name]


@pytest.fixture
def controller(qtbot):
    devices = {"A": FakeDevice(2), "B": FakeDevice(2, 1), "Wheel": FakeDevice(10, 3), "Wheel2": FakeDevice(10)}
    widget = WidgetFactory(SimpleNamespace()).createWidget(FlipMirrorWidget)
    qtbot.addWidget(widget)
    controller = FlipMirrorController.__new__(FlipMirrorController)
    controller._manager = FakeManager(devices)
    controller._widget = widget
    controller._names = list(devices)
    controller._master_by_follower = {}
    controller._followers_by_master = {}
    controller._FlipMirrorController__logger = logging.getLogger("test.flipmirror")
    for name, device in devices.items():
        widget.addFlipMirror(name, device.get_state_names())
    controller._refresh_all_states()
    return controller


def choices(controller, name):
    combo = controller._widget.rows[name]["masterCombo"]
    return [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i) != "—"]


def test_master_choices_list_only_mirrors_with_the_same_number_of_positions(controller):
    assert choices(controller, "A") == ["B"]
    assert choices(controller, "Wheel") == ["Wheel2"]
    assert choices(controller, "Wheel2") == ["Wheel"]


def test_two_state_mirrors_still_link_and_follow(controller):
    assert controller.set_link("A", "B") is True
    assert controller._master_by_follower == {"A": "B"} and controller._manager["A"].state == 1
    controller.move_flip("B", 0)
    assert controller._manager["A"].state == 0


def test_equal_count_wheels_link_and_follow_the_state_number(controller):
    assert controller.set_link("Wheel2", "Wheel") is True
    assert controller._manager["Wheel2"].state == 3
    controller.move_flip("Wheel", 7)
    assert controller._manager["Wheel2"].state == 7


def test_a_link_across_different_counts_is_refused_with_a_clear_message(controller, caplog):
    with caplog.at_level(logging.ERROR, logger="test.flipmirror"):
        assert controller.set_link("A", "Wheel") is False
    assert "A (2 positions) to Wheel (10 positions)" in caplog.text and "same number of positions" in caplog.text
    assert controller._master_by_follower == {} and controller._manager["A"].moves == []
    assert controller.set_link("Wheel", "A") is False


def test_a_saved_state_beyond_the_count_is_a_warning_and_not_applied(controller):
    state = {"mirrors": {"A": {"state": 5, "connected": True}, "Wheel": {"state": 9, "connected": True}}, "links": {}}
    warnings = controller.applyComponentState(state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    assert any('"A" not moved' in w and "2 positions" in w for w in warnings)
    assert controller._manager["A"].moves == [] and controller._manager["Wheel"].moves == [9]
    warnings = controller.applyComponentState(
        {"mirrors": {"A": {"state": 5}}, "links": {}}, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    assert any('"A" not applied' in w for w in warnings) and controller._manager["A"].moves == []


# ------------------------------------------------ a row follows a device that changed its positions
def seen_signals(widget):
    seen = []
    widget.sigStateChanged.connect(lambda n, s: seen.append((n, s)))
    return seen


def test_a_widget_row_is_rebuilt_from_two_positions_to_ten_and_back(qtbot):
    widget = WidgetFactory(SimpleNamespace()).createWidget(FlipMirrorWidget)
    qtbot.addWidget(widget)
    widget.addFlipMirror("W", {0: "0", 1: "1"})
    widget.addFlipMirror("Other", {0: "a", 1: "b"})
    seen = seen_signals(widget)
    widget.rows["W"]["state1Btn"].click()
    assert seen == [("W", 1)]

    ten = {i: f"p{i}" for i in range(10)}
    widget.rebuildFlipMirror("W", ten)
    row = widget.rows["W"]
    assert "stateButtons" not in row and "state0Btn" not in row and "buttonGroup" not in row
    assert row["stateCombo"].count() == 10 and widget.stateNames("W") == ten
    assert [row["stateCombo"].itemText(i) for i in range(10)] == [f"p{i}" for i in range(10)]
    assert row["states"] == list(range(10))
    assert row["stateWidget"].layout().count() == 1                    # the two buttons are gone, not hidden
    row["stateCombo"].activated.emit(7)                                # the user picks the eighth position
    assert seen[-1] == ("W", 7)
    widget.setState("W", 3)
    assert row["stateCombo"].currentIndex() == 3
    assert seen[-1] == ("W", 7)                                         # setState is silent

    widget.rebuildFlipMirror("W", {0: "Out", 1: "In"})
    row = widget.rows["W"]
    assert "stateCombo" not in row and sorted(row["stateButtons"]) == [0, 1]
    assert row["state0Btn"].text() == "Out" and row["state1Btn"].text() == "In"
    assert row["stateWidget"].layout().count() == 2
    row["state1Btn"].click()
    assert seen[-1] == ("W", 1)
    widget.setState("W", 0)
    assert [s for s, b in row["stateButtons"].items() if b.isChecked()] == [0]

    # The other row, and the row's own follow / master / status controls, were not touched.
    assert widget.stateNames("Other") == {0: "a", 1: "b"} and "state1Btn" in widget.rows["Other"]
    assert widget.rows["W"]["masterCombo"] is row["masterCombo"] and widget.rows["W"]["statusLabel"].text() == "—"


def test_rebuilding_an_unknown_row_adds_it_and_a_missing_row_shows_no_names(qtbot):
    widget = WidgetFactory(SimpleNamespace()).createWidget(FlipMirrorWidget)
    qtbot.addWidget(widget)
    assert widget.stateNames("nope") == {}
    widget.rebuildFlipMirror("New", {0: "x", 1: "y", 2: "z"})
    assert widget.stateNames("New") == {0: "x", 1: "y", 2: "z"}


def test_a_lifecycle_refresh_rebuilds_the_row_of_a_device_that_now_has_other_positions(controller):
    """A wheel that was absent at startup has the two placeholder positions; once the lifecycle service
    reports it back it has ten, and its row must follow (the old row offered 0 and 1 only)."""
    devices = controller._manager.devices
    controller._widget.rebuildFlipMirror("Wheel", {0: "0", 1: "1"})     # as built while the device was absent
    assert controller._widget.stateNames("Wheel") == {0: "0", 1: "1"}
    devices["Wheel"].state = 6
    devices["Wheel"].get_cached_state = lambda: 6
    seen = seen_signals(controller._widget)

    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="Wheel")])

    row = controller._widget.rows["Wheel"]
    assert row["stateCombo"].count() == 10 and controller._widget.stateNames("Wheel") == devices["Wheel"].get_state_names()
    assert row["stateCombo"].currentIndex() == 6                        # the device's position is shown
    assert seen == []                                                   # a refresh never moves anything
    assert devices["Wheel"].moves == []
    assert controller._widget.rows["Wheel"]["stateCombo"].isEnabled()    # _refresh_link_ui enabled the new control
    assert choices(controller, "Wheel") == ["Wheel2"]                   # links still list equal-count wheels only

    controller._widget.rows["Wheel"]["stateCombo"].activated.emit(8)
    assert seen == [("Wheel", 8)]


def test_a_lifecycle_refresh_leaves_a_row_alone_when_the_positions_are_the_same(controller):
    row_before = controller._widget.rows["A"]["state0Btn"]
    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="A"), SimpleNamespace(name="unknown")])
    assert controller._widget.rows["A"]["state0Btn"] is row_before


def test_a_lifecycle_refresh_survives_a_device_that_cannot_name_its_positions(controller):
    def broken():
        raise RuntimeError("not there")

    controller._manager.devices["A"].get_state_names = broken
    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="A")])
    assert controller._widget.stateNames("A") == {0: "p0", 1: "p1"}


def test_a_recovered_follower_with_other_positions_loses_its_link_and_the_master_no_longer_drags_it(controller, caplog):
    """Two ten-position wheels linked; the follower is recovered with two positions. The link must not
    survive: commanding position 7 would move the master and fail on the follower."""
    devices = controller._manager.devices
    assert controller.set_link("Wheel2", "Wheel") is True
    assert controller.getComponentState()["links"] == {"Wheel2": "Wheel"}
    devices["Wheel2"].count = 2                                          # recovered with two positions
    caplog.set_level(logging.WARNING, logger="test.flipmirror")

    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="Wheel2")])

    assert controller.getComponentState()["links"] == {}
    assert controller._followers_by_master == {}
    assert any("Removed the link" in r.getMessage() and "Wheel2" in r.getMessage() for r in caplog.records)
    assert controller._widget.stateNames("Wheel2") == {0: "p0", 1: "p1"}
    assert controller._widget.rows["Wheel2"]["masterCombo"].currentText() == "—"      # the widget shows it
    moves_before = list(devices["Wheel2"].moves)
    controller.move_flip("Wheel", 7)
    assert devices["Wheel"].state == 7 and devices["Wheel2"].moves == moves_before   # the follower is not touched
    assert choices(controller, "Wheel") == []                                        # and no longer offered as a partner


def test_a_recovered_master_with_other_positions_loses_the_links_of_its_followers(controller):
    devices = controller._manager.devices
    devices["Wheel2"].count = 10
    assert controller.set_link("Wheel2", "Wheel") is True
    devices["Wheel"].count = 2
    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="Wheel")])
    assert controller.getComponentState()["links"] == {}


def test_a_recovered_mirror_with_the_same_positions_keeps_its_links(controller):
    devices = controller._manager.devices
    assert controller.set_link("Wheel2", "Wheel") is True
    devices["Wheel2"].get_state_names = lambda: {i: f"renamed{i}" for i in range(10)}
    controller._refreshLifecycleAffectedFlipMirrors([SimpleNamespace(name="Wheel2")])
    assert controller._widget.stateNames("Wheel2")[0] == "renamed0"
    assert controller.getComponentState()["links"] == {"Wheel2": "Wheel"}
