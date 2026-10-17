"""A DVI SLM whose display is not connected must not stop ImSwitch from starting.

The offscreen Qt platform has exactly one screen, so monitorIdx 2 is a display
that does not exist -- the rig case of an SLM meant to be the third monitor on
a computer with only one connected.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.managers.slms.HamamatsuSLMdviManager import (
    HamamatsuSLMdviManager,
)


def _slmInfo(monitorIdx, **managerProperties):
    return SimpleNamespace(width=64, height=48, pixelSize=0.0125,
                           monitorIdx=monitorIdx,
                           managerProperties=managerProperties)


@pytest.fixture
def manager_factory(qapp):
    made = []

    def make(monitorIdx, **managerProperties):
        manager = HamamatsuSLMdviManager(_slmInfo(monitorIdx, **managerProperties), 'SLM1')
        made.append(manager)
        return manager

    yield make
    for manager in made:
        manager.SLMQLabel.close()


def test_missing_display_reports_device_not_found_instead_of_raising(manager_factory, qapp):
    assert len(qapp.screens()) == 1

    manager = manager_factory(monitorIdx=2)

    connection, mode, summary, details, failure = manager.statusSnapshot()
    assert connection is DeviceConnectionState.ERROR
    assert failure is DeviceFailureKind.DEVICE_NOT_FOUND
    assert mode is DeviceRuntimeMode.REAL
    assert summary == 'SLM display not connected'
    assert 'monitorIdx 2' in details and '1 display(s) connected' in details
    assert not manager.SLMQLabel.isVisible()


def test_patterns_for_a_missing_display_are_dropped_quietly(manager_factory):
    manager = manager_factory(monitorIdx=2)

    manager.upload_pattern(np.zeros((64, 48), dtype=np.uint8))

    assert manager.SLMQLabel.pixmap() is None or manager.SLMQLabel.pixmap().isNull()


def test_mock_mode_without_the_display_keeps_the_mock_status(manager_factory):
    manager = manager_factory(monitorIdx=2, mockermode=True)

    connection, mode, *_ = manager.statusSnapshot()
    assert mode is DeviceRuntimeMode.MOCK
    assert connection is DeviceConnectionState.NOT_APPLICABLE


def test_an_existing_display_still_gets_the_full_screen_window(manager_factory):
    manager = manager_factory(monitorIdx=0)

    connection, *_ = manager.statusSnapshot()
    assert connection is DeviceConnectionState.NOT_APPLICABLE
    assert manager.SLMQLabel.isVisible()


@pytest.mark.parametrize('monitorIdx', [-1])
def test_a_negative_index_is_not_wrapped_to_the_last_display(manager_factory, monitorIdx):
    manager = manager_factory(monitorIdx=monitorIdx)

    assert manager.statusSnapshot()[4] is DeviceFailureKind.DEVICE_NOT_FOUND
