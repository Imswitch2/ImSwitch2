from types import SimpleNamespace

from imswitch.imcontrol.model.measurement.mocks import MockPowerMeterDriver

from .InstrumentManager import InstrumentManager


class MockPowerMeterManager(InstrumentManager):
    """ A simulated PM100-like power meter for hardware-free setups.

    Manager properties:

    - ``powerW`` -- simulated optical power at the sensor (default 1e-3 W)
    - ``noiseW`` -- RMS noise per reading (default 2e-7 W)
    - ``serial`` -- reported serial number (default "MOCK-PM")
    """

    def _createDriver(self, properties):
        power = float(properties.get('powerW', 1e-3))
        source = SimpleNamespace(emitted_w=lambda: power)
        return MockPowerMeterDriver(
            [source], transmission=1.0,
            noise_w=float(properties.get('noiseW', 2e-7)),
            serial=str(properties.get('serial', 'MOCK-PM')),
        )
