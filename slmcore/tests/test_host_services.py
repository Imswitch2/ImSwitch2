import numpy as np
import pytest

from slmcore.host import (
    DeviceConnectionResult,MockSLMDeviceProvider,SLMDeviceProvider,SLMHostServices,
)


def test_host_services_device_upload_is_an_optional_capability():
    frame = np.zeros((4,5),dtype=np.uint8)
    received = []
    device = SLMDeviceProvider(
        upload_frame=lambda value:received.append(value),
    )
    services = SLMHostServices(device=device)

    services.upload(frame)

    assert received == [frame]
    SLMHostServices().upload(frame)


def test_host_services_rejects_invalid_device_capability():
    with pytest.raises(TypeError,match="device"):
        SLMHostServices(device=object())


def test_device_provider_requires_connection_callbacks_when_requested():
    with pytest.raises(ValueError,match="connect and disconnect"):
        SLMDeviceProvider(
            upload_frame=lambda _frame:None,
            requires_explicit_connection=True,
        )


def test_device_provider_normalizes_legacy_connection_tuples():
    provider = SLMDeviceProvider(
        upload_frame=lambda _frame:None,
        connect=lambda:(True,"SER123"),
        disconnect=lambda:(True,"closed"),
        requires_explicit_connection=True,
    )

    connected = provider.connect()
    disconnected = provider.disconnect()

    assert connected == DeviceConnectionResult(
        connected=True,device_info="SER123",
    )
    assert disconnected == DeviceConnectionResult(
        connected=False,message="closed",
    )


def test_mock_device_provider_retains_uploaded_frame():
    provider = MockSLMDeviceProvider(requires_explicit_connection=True)
    frame = np.arange(12,dtype=np.uint8).reshape(3,4)

    assert provider.is_mock
    assert provider.requires_explicit_connection
    assert not provider.connected
    assert provider.connect().connected
    provider.upload_frame(frame)
    assert provider.upload_count == 1
    np.testing.assert_array_equal(provider.last_frame,frame)
    assert not provider.disconnect().connected


class _MeasurementProvider:
    def available_sources(self,section_key):
        return ("camera",)

    def preferred_source(self,section_key,available):
        return available[0]

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        return None


def test_host_services_accepts_measurement_provider_capability():
    provider = _MeasurementProvider()
    services = SLMHostServices(measurement_provider=provider)

    assert services.measurement_provider is provider
    assert services.can_upload_frame is False


def test_host_services_rejects_incomplete_measurement_provider():
    class Incomplete:
        def available_sources(self,section_key):
            return ()

    with pytest.raises(TypeError,match="measurement_provider"):
        SLMHostServices(measurement_provider=Incomplete())
