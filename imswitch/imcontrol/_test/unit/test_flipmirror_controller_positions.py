"""FlipMirrorController with N-position selectors: links need equal position counts, restored states are range-checked."""
import logging
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers import FlipMirrorController
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
