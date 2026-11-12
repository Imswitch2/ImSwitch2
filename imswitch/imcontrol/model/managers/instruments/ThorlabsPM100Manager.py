from imswitch.imcontrol.model.measurement.thorlabs import ThorlabsPM100Driver

from .InstrumentManager import InstrumentManager


class ThorlabsPM100Manager(InstrumentManager):
    """ A Thorlabs PM100D / PM100USB / PM100A power meter over VISA.

    Needs ``pyvisa`` (``pip install "imswitch2[hardware]"``) and a VISA
    library: the Thorlabs software's NI-VISA runtime (backend ``""``) or
    pyvisa-py (backend ``"@py"``).

    Manager properties:

    - ``serial`` -- serial number, as in the VISA resource
      ``USB0::0x1313::0x8078::<serial>::INSTR`` (required)
    - ``visaBackend`` -- pyvisa backend (default ``""``)
    - ``timeoutMs`` -- VISA timeout (default 2000)
    - ``wavelengthNm`` -- correction wavelength set on connect (optional;
      otherwise the meter keeps its own)

    Timing is unverified until checked on hardware: quantitative measurement
    runs need ``allowUnverifiedTiming`` until then.
    """

    def _createDriver(self):
        wavelength = self._managerProperties.get('wavelengthNm')
        return ThorlabsPM100Driver(
            str(self._managerProperties['serial']),
            backend=str(self._managerProperties.get('visaBackend', '')),
            timeout_ms=int(self._managerProperties.get('timeoutMs', 2000)),
            wavelength_nm=None if wavelength is None else float(wavelength),
        )
