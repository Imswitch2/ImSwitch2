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
