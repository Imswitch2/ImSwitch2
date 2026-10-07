from imswitch.imcontrol.model.measurement.mocks import MockPAXDriver, MockRotatorControl

from .InstrumentManager import InstrumentManager


class MockPAXManager(InstrumentManager):
    """ A simulated PAX1000 polarimeter behind two fixed waveplates.

    Manager properties:

    - ``plate1Deg`` / ``plate2Deg`` -- simulated waveplate angles (default 0)
    - ``noiseDeg`` -- angular noise per reading (default 0.2)
    - ``serial`` -- reported serial number (default "MOCK-PAX")
    """

    def _createDriver(self):
        plate1 = MockRotatorControl('plate1', start_deg=float(self._managerProperties.get('plate1Deg', 0.0)))
        plate2 = MockRotatorControl('plate2', start_deg=float(self._managerProperties.get('plate2Deg', 0.0)))
        return MockPAXDriver(
            plate1, plate2, revolution_s=0.01,
            noise_deg=float(self._managerProperties.get('noiseDeg', 0.2)),
            serial=str(self._managerProperties.get('serial', 'MOCK-PAX')),
        )
