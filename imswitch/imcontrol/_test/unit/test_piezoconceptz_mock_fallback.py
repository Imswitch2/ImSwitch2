import threading
import time

import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
from imswitch.imcontrol.model.managers.positioners.PiezoconceptZManager import (
    PiezoconceptZManager,
)
from imswitch.imcontrol.model.managers.positioners.PiezoconceptZManager2 import (
    PiezoconceptZManager2,
)

pytestmark = pytest.mark.nohardware


def _positioner_info(manager_properties=None):
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='PiezoconceptZManager',
        managerProperties=manager_properties or {'rs232device': 'missing-device'},
        axes=['Z'],
        forPositioning=True,
        resetOnClose=False,
        liveUpdate=True,
    )


class _MissingRS232Manager(dict):
    """Stands in for lowLevelManagers['rs232sManager'] when no RS232 device
    is configured, matching how MHXYStageManager's fallback is exercised."""

    def __getitem__(self, key):
        raise KeyError(key)


class _ScriptedRS232:
    def __init__(self, queryReplies, readReplies=()):
        self.queryReplies = list(queryReplies)
        self.readReplies = list(readReplies)
        self.commands = []

    def query(self, command):
        self.commands.append(command)
        return self.queryReplies.pop(0)

    def read(self):
        if not self.readReplies:
            return None
        return self.readReplies.pop(0)


class _ConcurrentRS232:
    def __init__(self):
        self._stateLock = threading.Lock()
        self.activeQueries = 0
        self.maxActiveQueries = 0

    def query(self, command):
        with self._stateLock:
            self.activeQueries += 1
            self.maxActiveQueries = max(
                self.maxActiveQueries, self.activeQueries
            )
        try:
            # Release the GIL long enough for an unprotected second caller to
            # enter query(). A manager-level lock must keep this at one.
            time.sleep(0.02)
            return '12.5 um' if command == 'GET_Z' else 'Ok'
        finally:
            with self._stateLock:
                self.activeQueries -= 1

    def read(self):
        return None


def _manager_with_rs232(managerClass, rs232):
    return managerClass(
        _positioner_info({'rs232device': 'piezo'}),
        'Z-piezo',
        rs232sManager={'piezo': rs232},
    )


def test_piezoconceptz_manager_falls_back_to_mock_rs232(caplog):
    manager = PiezoconceptZManager(
        _positioner_info(),
        'Z-piezo',
        rs232sManager=_MissingRS232Manager(),
    )

    assert isinstance(manager._rs232Manager, MockRS232Driver)

    manager.setPosition(5.0, 'Z')
    manager.move(1.5, 'Z')

    assert manager.position['Z'] == pytest.approx(6.5)


def test_piezoconceptz_manager2_falls_back_to_mock_rs232():
    manager = PiezoconceptZManager2(
        _positioner_info(),
        'Z-piezo-v2',
        rs232sManager=_MissingRS232Manager(),
    )

    assert isinstance(manager._rs232Manager, MockRS232Driver)

    manager.setPosition(5.0, 'Z')
    manager.move(1.5, 'Z')

    assert manager.position['Z'] == pytest.approx(6.5)

    # get_abs() must tolerate the mock driver's None reply instead of raising.
    assert manager.get_abs() == pytest.approx(6.5)


@pytest.mark.parametrize(
    'managerClass', [PiezoconceptZManager, PiezoconceptZManager2]
)
def test_piezoconcept_get_abs_skips_null_padded_move_ack(managerClass):
    rs232 = _ScriptedRS232(
        queryReplies=['Ok\x00'],
        readReplies=['12.345 um\x00'],
    )
    manager = _manager_with_rs232(managerClass, rs232)

    assert manager.get_abs() == pytest.approx(12.345)
    assert manager._position['Z'] == pytest.approx(12.345)
    assert rs232.commands == ['GET_Z']


@pytest.mark.parametrize(
    'managerClass', [PiezoconceptZManager, PiezoconceptZManager2]
)
def test_piezoconcept_move_drains_reply_until_ack(managerClass):
    rs232 = _ScriptedRS232(
        queryReplies=['9.5 um'],
        readReplies=['Ok\x00'],
    )
    manager = _manager_with_rs232(managerClass, rs232)

    manager.move(1.0, 'Z')

    assert manager._position['Z'] == pytest.approx(1.0)
    assert rs232.commands == ['MOVRZ +1.0u']


@pytest.mark.parametrize(
    'managerClass', [PiezoconceptZManager, PiezoconceptZManager2]
)
def test_piezoconcept_serializes_move_and_position_queries(managerClass):
    rs232 = _ConcurrentRS232()
    manager = _manager_with_rs232(managerClass, rs232)
    start = threading.Barrier(3)

    def move():
        start.wait()
        manager.move(1.0, 'Z')

    def readPosition():
        start.wait()
        manager.get_abs()

    threads = [
        threading.Thread(target=move),
        threading.Thread(target=readPosition),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert rs232.maxActiveQueries == 1
