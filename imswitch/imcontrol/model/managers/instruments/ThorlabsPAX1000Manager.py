from imswitch.imcontrol.model.measurement.thorlabs import ThorlabsPAX1000Driver

from .InstrumentManager import InstrumentManager


class ThorlabsPAX1000Manager(InstrumentManager):
    """ A Thorlabs PAX1000 polarimeter over VISA, in measurement mode 9.

    Needs ``pyvisa`` (``pip install "imswitch2[hardware]"``) and the PAX1000
    software's NI-VISA runtime (backend ``""``) or pyvisa-py (``"@py"``).
    The waveplate starts rotating on connect and stops on disconnect.

    Manager properties:

    - ``serial`` -- serial number, as in the VISA resource string (required)
    - ``visaBackend`` -- pyvisa backend (default ``""``)
    - ``timeoutMs`` -- VISA timeout (default 5000)
    - ``wavelengthNm`` -- wavelength set on connect (optional)
    - ``powerUnit`` -- unit of the packet's power field, ``"W"`` (default)
      or ``"mW"``; check against the Thorlabs software
    - ``updateBoundS`` -- a sample returned this long after a window opened
      was measured entirely after it (default 0.5)
    - ``updatePeriodS`` -- minimum spacing of distinct samples (default 0.1)

    Timing is unverified until checked on hardware: quantitative measurement
    runs need ``allowUnverifiedTiming`` until then.
    """

    def _createDriver(self):
        wavelength = self._managerProperties.get('wavelengthNm')
        return ThorlabsPAX1000Driver(
            str(self._managerProperties['serial']),
            backend=str(self._managerProperties.get('visaBackend', '')),
            timeout_ms=int(self._managerProperties.get('timeoutMs', 5000)),
            wavelength_nm=None if wavelength is None else float(wavelength),
            power_unit=str(self._managerProperties.get('powerUnit', 'W')),
            update_bound_s=float(self._managerProperties.get('updateBoundS', 0.5)),
            update_period_s=float(self._managerProperties.get('updatePeriodS', 0.1)),
        )
