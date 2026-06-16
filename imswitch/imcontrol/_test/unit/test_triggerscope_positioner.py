import pytest

from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.TriggerScopePositionerManager import (
    TriggerScopePositionerManager,
)


class FakeTriggerScopeManager:
    """Records every DAC command so tests can assert what was actually sent."""

    def __init__(self):
        self.calls = []  # list of (target, voltage)

    def setAnalog(self, target, voltage):
        self.calls.append((target, voltage))


@pytest.fixture(autouse=True)
def _isolated_persistence(tmp_path, monkeypatch):
    """Point position persistence at a temp dir so tests never touch the real
    ImSwitchConfig and don't leak state between cases."""
    monkeypatch.setattr(dirtools.UserFileDirs, 'Config', str(tmp_path))
    return tmp_path


def _make_info(minVolt=0.0, maxVolt=10.0, conversionFactor=2.0):
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='TriggerScopePositionerManager',
        managerProperties={
            'conversionFactor': conversionFactor,
            'minVolt': minVolt,
            'maxVolt': maxVolt,
        },
        axes=['Z'],
        forScanning=True,
    )


def _make_manager(name='TSZ', **kwargs):
    ts = FakeTriggerScopeManager()
    mgr = TriggerScopePositionerManager(_make_info(**kwargs), name,
                                        triggerScopeManager=ts)
    return mgr, ts


def test_no_movement_on_startup():
    """Construction must not move the stage: no homing, no DAC command. This is
    what preserves a manually centred focus across restarts and crashes."""
    mgr, ts = _make_manager()
    assert ts.calls == []
    assert mgr.position['Z'] == 0.0


def test_set_position_commands_expected_voltage_and_tracks_it():
    mgr, ts = _make_manager(conversionFactor=2.0)  # position = 2 * volt

    mgr.setPosition(6.0, 'Z')  # 6 / 2 = 3 V, within [0, 10]
    assert ts.calls == [('TSZ', 3.0)]
    assert mgr.position['Z'] == 6.0


def test_negative_request_clamps_to_floor_and_tracks_clamped_position():
    """A negative move on a minVolt=0 axis parks at 0 and reports 0 (no drift)."""
    mgr, ts = _make_manager(minVolt=0.0, conversionFactor=2.0)

    mgr.setPosition(-4.0, 'Z')  # -2 V clamped to 0 V
    assert ts.calls == [('TSZ', 0.0)]
    # The tracked position reflects what the hardware actually did, not -4.
    assert mgr.position['Z'] == 0.0


def test_over_range_request_clamps_to_ceiling():
    mgr, ts = _make_manager(minVolt=0.0, maxVolt=10.0, conversionFactor=2.0)

    mgr.setPosition(40.0, 'Z')  # 20 V clamped to 10 V -> position 20
    assert ts.calls == [('TSZ', 10.0)]
    assert mgr.position['Z'] == 20.0


def test_move_uses_clamped_tracked_position():
    """Relative moves build on the real (clamped) position, not the request."""
    mgr, ts = _make_manager(minVolt=0.0, conversionFactor=2.0)

    mgr.move(-100.0, 'Z')   # clamps to 0 V / position 0
    mgr.move(6.0, 'Z')      # 0 + 6 -> 3 V
    assert ts.calls == [('TSZ', 0.0), ('TSZ', 3.0)]
    assert mgr.position['Z'] == 6.0


def test_position_persists_across_restart_without_moving():
    """A new manager (simulating an ImSwitch restart) re-adopts the last
    commanded position and issues no DAC command to do so — the board is still
    holding that voltage."""
    mgr, ts = _make_manager(conversionFactor=2.0)
    mgr.setPosition(6.0, 'Z')  # commands 3 V, persists position 6

    mgr2, ts2 = _make_manager(conversionFactor=2.0)  # "restart"
    assert mgr2.position['Z'] == 6.0      # focus survived the restart
    assert ts2.calls == []                # ...and nothing moved


def test_restore_clamps_to_current_range_without_moving():
    """If the range shrank since the position was written, the restored value is
    clamped to the new range — still without commanding the DAC."""
    mgr, _ = _make_manager(maxVolt=10.0, conversionFactor=2.0)
    mgr.setPosition(6.0, 'Z')  # 3 V persisted

    # Reopen with a lower ceiling: 3 V no longer reachable, clamp to 1 V.
    mgr2, ts2 = _make_manager(maxVolt=1.0, conversionFactor=2.0)
    assert mgr2.position['Z'] == 2.0      # 1 V * conversionFactor
    assert ts2.calls == []


def test_persistence_is_per_positioner_name():
    """Two axes with different names don't clobber each other's stored position."""
    mgr_a, _ = _make_manager(name='TSZ', conversionFactor=2.0)
    mgr_b, _ = _make_manager(name='TSX', conversionFactor=2.0)
    mgr_a.setPosition(6.0, 'Z')   # 3 V
    mgr_b.setPosition(4.0, 'Z')   # 2 V

    mgr_a2, _ = _make_manager(name='TSZ', conversionFactor=2.0)
    mgr_b2, _ = _make_manager(name='TSX', conversionFactor=2.0)
    assert mgr_a2.position['Z'] == 6.0
    assert mgr_b2.position['Z'] == 4.0
