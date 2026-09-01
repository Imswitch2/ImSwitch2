from types import SimpleNamespace

from imswitch.imcontrol.model.devices import (
    DeviceLifecycleAction,
    HardwareDeviceId,
    SLMSessionLifecycle,
)


class _Session:
    def __init__(self, connected=True, message=None):
        self.result = SimpleNamespace(connected=connected, message=message)
        self.calls = 0

    def reconnect_device(self):
        self.calls += 1
        return self.result


def test_slm_session_lifecycle_delegates_reconnect_to_slmcore_session():
    session = _Session(connected=True)
    lifecycle = SLMSessionLifecycle(slm_key="slm_usb", session=session)

    result = lifecycle.reconnect()

    assert lifecycle.hardware_id == HardwareDeviceId("slm", "slm:slm_usb")
    assert lifecycle.capabilities.reconnect is True
    assert session.calls == 1
    assert result.action is DeviceLifecycleAction.RECONNECT
    assert result.success is True
    assert result.affected_device_ids[0].kind == "slm"
    assert result.affected_device_ids[0].name == "slm_usb"


def test_slm_session_lifecycle_reports_failed_connection_result():
    session = _Session(connected=False, message="SLM not found")
    lifecycle = SLMSessionLifecycle(slm_key="slm_usb", session=session)

    result = lifecycle.reconnect()

    assert result.success is False
    assert result.details == "SLM not found"
