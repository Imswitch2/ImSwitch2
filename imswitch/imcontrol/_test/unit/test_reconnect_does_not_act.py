"""A reconnect never acts for the user.

After a device comes back nothing may move a stage, switch a laser on,
restore a non-zero laser power, flip a mirror or push an SLM pattern unless
the user does it. A device that lost its position says so and refuses to move
until the user references it.
"""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import DeviceNotConnectedError
from imswitch.imcontrol.model.SetupInfo import PositionerInfo


class _Board:
    """A GRBL board that records everything it is asked to do."""

    instances = []

    def __init__(self, address=None):
        self.calls = []
        _Board.instances.append(self)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append(name)
        return record

    def _write(self, command):
        self.calls.append("_write")
        return ""


@pytest.fixture
def grbl(monkeypatch):
    import imswitch.imcontrol.model.interfaces.grbldriver as driver

    _Board.instances = []
    monkeypatch.setattr(driver, "GrblDriver", _Board)

    def build(is_home):
        from imswitch.imcontrol.model.managers.positioners.GRBLStageManager import GRBLStageManager
        from imswitch.imcontrol.model.managers.rs232.GRBLManager import GRBLManager

        board = GRBLManager(
            SimpleNamespace(managerProperties={"port": "COM1", "is_home": is_home}), "board")
        info = PositionerInfo(managerName="GRBLStageManager", axes=["X", "Y", "Z"],
                              forPositioning=True, managerProperties={"rs232device": "board"})
        stage = GRBLStageManager(info, "stage", rs232sManager={"board": board})
        return board, stage

    return build


def test_startup_homes_when_the_setup_says_so_and_a_reconnect_does_not(grbl):
    board, stage = grbl(is_home=True)
    first = _Board.instances[0]
    assert first.calls.count("home") == 1            # the setup asked for it at startup
    assert board.reconnectTransport() is True
    second = _Board.instances[1]
    assert "home" not in second.calls                # a reconnect never homes
    assert "write_global_config" in second.calls     # the board is programmed again
    assert not any(call in second.calls for call in ("move_rel", "xmove", "set_laser_intensity"))


def test_stage_refuses_to_move_after_a_reconnect_until_the_user_references_it(grbl):
    board, stage = grbl(is_home=True)
    stage.move(100, "X")                              # known position: moves
    assert "move_rel" in _Board.instances[0].calls
    assert not stage.requiresReference
    board.reconnectTransport()
    assert stage._onTransportReconnected(True) == []
    assert stage.requiresReference and stage.isReferenceActionable
    assert stage.isAxisReferenced("X") is False
    with pytest.raises(DeviceNotConnectedError, match="position is unknown.*reference"):
        stage.move(100, "X")
    with pytest.raises(DeviceNotConnectedError):
        stage.setPosition(5, "X")
    assert "move_rel" not in _Board.instances[1].calls   # nothing reached the new board

    stage.reference(axis="X")                         # the user's explicit action homes it
    assert _Board.instances[1].calls.count("home") == 1
    assert stage.isReferenced and not stage.requiresReference
    stage.move(100, "X")
    assert "move_rel" in _Board.instances[1].calls


def test_stage_follows_the_boards_current_driver(grbl):
    board, stage = grbl(is_home=False)
    old = stage.board
    board.reconnectTransport()
    assert stage.board is not old and stage.board is board._board
