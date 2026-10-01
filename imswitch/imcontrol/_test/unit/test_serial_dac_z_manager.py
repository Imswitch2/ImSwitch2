"""No-hardware tests for SerialDacZManager robustness."""

import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.SerialDacZManager import (
    SerialDacZManager,
)


class FakeSerial:
    def __init__(self, port, baudrate, timeout):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.writes = []
        self.close_count = 0
        self.fail_writes = False
        self._rx = b""

    @property
    def in_waiting(self):
        return len(self._rx)

    def write(self, data):
        if self.fail_writes:
            raise OSError("serial link lost")

        self.writes.append(data)
        if data == b"\r\n" or data.endswith(b"\r\n"):
            self._rx += b"\r\n>>> "
        return len(data)

    def flush(self):
        if self.fail_writes:
            raise OSError("serial link lost")

    def read(self, size):
        chunk = self._rx[:size]
        self._rx = self._rx[size:]
        return chunk

    def close(self):
        self.close_count += 1
        self.is_open = False


def _make_info(**overrides):
    props = {
        "port": "COM13",
        "baudrate": 115200,
        "timeout": 0.1,
        "command_timeout": 0.1,
        "initial_position": 0.0,
        "offset_voltage": 0.0,
        "volts_per_um": 0.005,
        "min_voltage": -5.0,
        "max_voltage": 5.0,
        "clamp_voltage": True,
    }
    props.update(overrides)
    return PositionerInfo(
        managerName="SerialDacZManager",
        managerProperties=props,
        axes=["Z"],
        forPositioning=True,
    )


def _install_fake_serial(monkeypatch):
    created = []

    def factory(port, baudrate, timeout):
        ser = FakeSerial(port, baudrate, timeout)
        created.append(ser)
        return ser

    monkeypatch.setattr(
        "imswitch.imcontrol.model.managers.positioners.SerialDacZManager.serial.Serial",
        factory,
    )
    monkeypatch.setattr(
        "imswitch.imcontrol.model.managers.positioners.SerialDacZManager.time.sleep",
        lambda _seconds: None,
    )
    return created


@pytest.mark.nohardware
def test_successful_startup_marks_manager_available(monkeypatch):
    created = _install_fake_serial(monkeypatch)

    manager = SerialDacZManager(_make_info(initial_position=10.0), "FocusPiezoDAC")

    assert manager.isAvailable is True
    assert manager.connectionError is None
    assert manager.position["Z"] == 10.0
    assert len(created) == 1
    assert b"dac.SetDac(0.05)\r\n" in created[0].writes


@pytest.mark.nohardware
def test_serial_open_failure_keeps_manager_constructed_but_unavailable(monkeypatch):
    def fail_open(*_args, **_kwargs):
        raise OSError("port unavailable")

    monkeypatch.setattr(
        "imswitch.imcontrol.model.managers.positioners.SerialDacZManager.serial.Serial",
        fail_open,
    )

    manager = SerialDacZManager(_make_info(), "FocusPiezoDAC")

    assert manager.isAvailable is False
    assert manager.connectionError == "port unavailable"
    assert manager.position["Z"] == 0.0


@pytest.mark.nohardware
def test_repl_failure_closes_open_serial(monkeypatch):
    created = _install_fake_serial(monkeypatch)
    monkeypatch.setattr(
        SerialDacZManager,
        "_enter_repl",
        lambda self: (_ for _ in ()).throw(TimeoutError("no prompt")),
    )

    manager = SerialDacZManager(_make_info(), "FocusPiezoDAC")

    assert manager.isAvailable is False
    assert manager.connectionError == "no prompt"
    assert created[0].is_open is False
    assert created[0].close_count == 1


@pytest.mark.nohardware
def test_successful_set_position_updates_cache_after_command(monkeypatch):
    created = _install_fake_serial(monkeypatch)
    manager = SerialDacZManager(_make_info(), "FocusPiezoDAC")

    result = manager.setPosition(20.0, "Z")

    assert result == 20.0
    assert manager.position["Z"] == 20.0
    assert b"dac.SetDac(0.1)\r\n" in created[0].writes


@pytest.mark.nohardware
def test_runtime_communication_failure_marks_unavailable_and_preserves_position(
    monkeypatch,
):
    created = _install_fake_serial(monkeypatch)
    manager = SerialDacZManager(_make_info(), "FocusPiezoDAC")
    manager.setPosition(20.0, "Z")
    serial_port = created[0]
    serial_port.fail_writes = True

    with pytest.raises(RuntimeError, match="Serial DAC Z communication failed"):
        manager.setPosition(40.0, "Z")

    assert manager.isAvailable is False
    assert manager.connectionError == "serial link lost"
    assert manager.position["Z"] == 20.0
    assert serial_port.is_open is False


@pytest.mark.nohardware
def test_explicit_move_while_unavailable_raises(monkeypatch):
    created = _install_fake_serial(monkeypatch)
    manager = SerialDacZManager(_make_info(), "FocusPiezoDAC")
    created[0].fail_writes = True

    with pytest.raises(RuntimeError):
        manager.setPosition(10.0, "Z")

    with pytest.raises(RuntimeError, match="is unavailable"):
        manager.move(1.0, "Z")


@pytest.mark.nohardware
def test_invalid_unclamped_initial_voltage_remains_configuration_error(monkeypatch):
    serial_called = False

    def factory(*_args, **_kwargs):
        nonlocal serial_called
        serial_called = True
        return FakeSerial("COM13", 115200, 0.1)

    monkeypatch.setattr(
        "imswitch.imcontrol.model.managers.positioners.SerialDacZManager.serial.Serial",
        factory,
    )

    with pytest.raises(ValueError, match="outside allowed range"):
        SerialDacZManager(
            _make_info(
                initial_position=2000.0,
                volts_per_um=0.01,
                min_voltage=-5.0,
                max_voltage=5.0,
                clamp_voltage=False,
            ),
            "FocusPiezoDAC",
        )

    assert serial_called is False


@pytest.mark.nohardware
def test_finalize_sends_safe_voltage_and_closes(monkeypatch):
    created = _install_fake_serial(monkeypatch)
    manager = SerialDacZManager(
        _make_info(safe_voltage_on_close=0.25),
        "FocusPiezoDAC",
    )
    serial_port = created[0]

    manager.finalize()

    assert b"dac.SetDac(0.25)\r\n" in serial_port.writes
    assert serial_port.is_open is False
    assert manager.isAvailable is False

    # Cleanup remains harmless if called again.
    manager.finalize()
    assert serial_port.close_count == 1


@pytest.mark.nohardware
def test_finalize_closes_even_if_safe_voltage_command_fails(monkeypatch):
    created = _install_fake_serial(monkeypatch)
    manager = SerialDacZManager(
        _make_info(safe_voltage_on_close=0.25),
        "FocusPiezoDAC",
    )
    serial_port = created[0]
    serial_port.fail_writes = True

    manager.finalize()

    assert serial_port.is_open is False
    assert manager.isAvailable is False
