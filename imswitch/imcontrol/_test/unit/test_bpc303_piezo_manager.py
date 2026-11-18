"""The BPC303 piezo manager: µm through the channels' output voltage, as the
polarization rig's Microscope_GUI drove its stage, on the mock controller
and on a fake of the Kinesis driver."""

import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.interfaces import bpc303
from imswitch.imcontrol.model.managers.positioners.BPC303PiezoManager import (
    BPC303PiezoManager,
)


def _info(axes=('X', 'Y', 'Z'), **properties):
    return PositionerInfo(analogChannel=None, digitalLine=None,
                          managerName='BPC303PiezoManager',
                          managerProperties={'serialNumber': 'MOCK-71824990', **properties},
                          axes=list(axes), forPositioning=True)


def test_a_position_is_the_channels_share_of_its_largest_voltage():
    manager = BPC303PiezoManager(_info(), 'Piezo')
    controller = manager.backendHolder.backend
    manager.setPosition(10.0, 'Y')
    # 75 V over 20 µm: 10 µm is 37.5 V on channel 2
    assert controller.output_voltage(2) == pytest.approx(37.5)
    assert manager.position == pytest.approx({'X': 0.0, 'Y': 10.0, 'Z': 0.0})
    manager.move(2.5, 'Y')
    assert manager.position['Y'] == pytest.approx(12.5)
    manager.finalize()


def test_the_travel_is_configurable_and_a_target_outside_it_is_clamped():
    manager = BPC303PiezoManager(_info(travelUm=30.0), 'Piezo')
    manager.setPosition(45.0, 'X')
    assert manager.position['X'] == pytest.approx(30.0)
    assert manager.backendHolder.backend.output_voltage(1) == pytest.approx(75.0)
    manager.setPosition(-1.0, 'X')
    assert manager.position['X'] == pytest.approx(0.0)


def test_axes_take_the_channels_they_are_given():
    manager = BPC303PiezoManager(_info(axes=('Z',), channels={'Z': 3}), 'Piezo')
    manager.setPosition(4.0, 'Z')
    assert manager.backendHolder.backend.output_voltage(3) == pytest.approx(15.0)
    with pytest.raises(ValueError, match='no channel for Y'):
        BPC303PiezoManager(_info(axes=('X', 'Y'), channels={'X': 1}), 'Piezo')
    with pytest.raises(ValueError, match='channels 1-3'):
        BPC303PiezoManager(_info(axes=('X',), channels={'X': 4}), 'Piezo')
    with pytest.raises(ValueError, match='three channels'):
        BPC303PiezoManager(_info(axes=('A', 'B', 'C', 'D')), 'Piezo')


def test_a_decimal_comma_reads_as_a_number():
    """Kinesis returns .NET Decimals, written in the computer's locale."""
    assert bpc303._number('37,5') == 37.5
    assert bpc303._number('75.0') == 75.0


class _FakeController:
    def __init__(self, serial, *, channels=3, kinesis_dir=bpc303.KINESIS_DIR):
        self.serial, self.channels, self.kinesis_dir = serial, channels, kinesis_dir
        self.volts = {c: 0.0 for c in range(1, channels + 1)}

    def max_voltage(self, channel):
        return 150.0

    def output_voltage(self, channel):
        return self.volts[channel]

    def set_output_voltage(self, channel, volts):
        self.volts[channel] = volts

    def close(self):
        pass


def test_a_real_serial_opens_the_kinesis_driver(monkeypatch):
    monkeypatch.setattr(bpc303, 'BPC303', _FakeController)
    manager = BPC303PiezoManager(_info(axes=('X', 'Y'), serialNumber='71824990',
                                       kinesisPath=r'D:\Kinesis'), 'Piezo')
    controller = manager.backendHolder.backend
    assert isinstance(controller, _FakeController)
    assert (controller.serial, controller.channels, controller.kinesis_dir) == \
        ('71824990', 2, r'D:\Kinesis')
    manager.setPosition(5.0, 'X')
    assert controller.volts[1] == pytest.approx(37.5)        # 150 V over 20 µm


def test_a_controller_that_does_not_open_is_not_connected_or_a_mock_on_request(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError('no BPC303 on this computer')

    monkeypatch.setattr(bpc303, 'BPC303', refuse)
    absent = BPC303PiezoManager(_info(serialNumber='71824990'), 'Piezo')
    assert not absent.backendHolder.backend
    standing_in = BPC303PiezoManager(_info(serialNumber='71824990', useMockOnFailure=True),
                                     'Piezo')
    assert isinstance(standing_in.backendHolder.backend, bpc303.MockBPC303)
