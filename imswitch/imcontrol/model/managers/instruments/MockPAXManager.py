from imswitch.imcontrol.model.measurement.mocks import MockPAXDriver, MockRotatorControl

from .InstrumentManager import InstrumentManager


class _RotatorPlate:
    """A waveplate held by a rotator entry of the setup: its angle is the
    rotator's position (degrees)."""

    def __init__(self, rotatorsManager, name: str) -> None:
        try:
            self._manager = rotatorsManager[name]
        except (KeyError, TypeError):
            known = (', '.join(sorted(rotatorsManager.getAllDeviceNames()))
                     if rotatorsManager is not None else 'none')
            raise ValueError(f'MockPAXManager: no rotator {name!r} (rotators: {known})') from None

    def angle_at(self, t: float) -> float:
        return float(self._manager.position)


class MockPAXManager(InstrumentManager):
    """ A simulated PAX1000 polarimeter behind two waveplates.

    The waveplates are fixed angles, or -- with ``plate1Rotator`` /
    ``plate2Rotator`` -- follow rotator entries of the setup, so moving
    those rotators (a script, a measurement run) changes the polarisation.

    Manager properties:

    - ``plate1Deg`` / ``plate2Deg`` -- fixed waveplate angles (default 0)
    - ``plate1Rotator`` / ``plate2Rotator`` -- rotator names whose position
      is the waveplate angle, instead of the fixed one
    - ``noiseDeg`` -- angular noise per reading (default 0.2)
    - ``serial`` -- reported serial number (default "MOCK-PAX")
    """

    def _createDriver(self):
        # Literal keys, so the config editor can list them.
        plate1 = self._plate('plate1', self._managerProperties.get('plate1Rotator'),
                             float(self._managerProperties.get('plate1Deg', 0.0)))
        plate2 = self._plate('plate2', self._managerProperties.get('plate2Rotator'),
                             float(self._managerProperties.get('plate2Deg', 0.0)))
        return MockPAXDriver(
            plate1, plate2, revolution_s=0.01,
            noise_deg=float(self._managerProperties.get('noiseDeg', 0.2)),
            serial=str(self._managerProperties.get('serial', 'MOCK-PAX')),
        )

    def _plate(self, label, rotator, angle_deg):
        if rotator:
            return _RotatorPlate(self._lowLevelManagers.get('rotatorsManager'), str(rotator))
        return MockRotatorControl(label, start_deg=angle_deg)
