*************************
Instruments — reference
*************************

Instruments are measurement devices that are not part of an image
acquisition: power meters and polarimeters. They are plugged in when needed,
so they are usually **transient**: ImSwitch does not touch them at startup,
the Hardware status window shows them as *Not connected*, and you connect
them when you need them.

Instruments are used by scripts (reads, settings, actions) and by
measurement runs — a laser power LUT, a waveplate polarisation map. They are
never part of an ordinary recording.

**Vendor libraries.**  The Thorlabs instruments need ``pyvisa`` and a VISA
library. The ``hardware`` extra (``pip install -e ".[hardware]"``, see
:doc:`../installation`) installs ``pyvisa`` and ``pyvisa-py``; the Thorlabs
software (Optical Power Monitor, PAX1000 software) installs the NI-VISA
runtime, which the default backend ``""`` uses. ImSwitch starts without
``pyvisa``; only connecting the instrument fails.


How instruments are configured
==============================

Instruments live under the top-level ``"instruments"`` dict of the setup
JSON, one entry per physical instrument:

.. code-block:: json

    "instruments": {
        "pm1": {
            "managerName": "ThorlabsPM100Manager",
            "transient": true,
            "managerProperties": {"serial": "P0011748", "wavelengthNm": 775}
        },
        "pax1": {
            "managerName": "ThorlabsPAX1000Manager",
            "transient": true,
            "managerProperties": {"serial": "M01012314", "wavelengthNm": 633}
        }
    }

``transient`` (default ``false``)
    ``true``: not connected at startup and shown as *Not connected* — not an
    error. ``false``: connected at startup like any other device.

``connectOnStartup`` (default ``false``)
    A transient instrument that is still connected at startup when it is
    plugged in. If that connect fails, the instrument shows the error and
    stays disconnected; ImSwitch carries on.

An instrument that fails to connect, or faults while connected (a read
error, the USB cable pulled), is shown with the error in the Hardware status
window. It is never replaced by a simulation.


Connecting and disconnecting
============================

**Hardware → Hardware status** lists instruments under *Instruments*. Select
one and use **Connect**, **Disconnect** or **Reconnect**. Instruments can be
connected while a scan or recording runs — they take no part in it — but
not while a measurement run or a script holds them: the refusal names the
holder.

From a script:

.. code-block:: python

    api.imcontrol.getInstruments()            # name, connected, identity, quantities, ...
    api.imcontrol.connectInstrument('pm1')
    api.imcontrol.readInstrument('pm1', n=5)  # a quick look: list of {quantity: value}
    api.imcontrol.disconnectInstrument('pm1')

To make several reads, settings and actions without anybody else changing the
instrument in between, reserve it. Every call through the handle carries the
reservation; when the ``with`` block ends, the handle is dead:

.. code-block:: python

    with api.imcontrol.reserve(instruments=['pm1']) as r:
        pm1 = r.instrument('pm1')
        pm1.set('wavelength_nm', 775)          # returns the value the meter applied
        pm1.action('zero', confirm_dark=True)  # only with the beam blocked
        window = pm1.read(10, allow_unverified=True)
        powers = [s.values['power'] for s in window.samples]

``read`` returns only samples acquired after the call. Until an instrument's
timing has been checked on hardware (see the cards below) a read needs
``allow_unverified=True``, and measurement runs need
``allow_unverified_timing``; their results are then kept but never exported as
a qualified result (no LUT file).


The Instruments panel
=====================

A setup with instruments gets an **Instruments** panel in Hardware Control
(added even if ``availableWidgets`` does not list it). Each instrument shows
its state, its live readings -- power with an SI prefix, azimuth and
ellipticity in degrees, the degree of polarisation and the normalised Stokes
vector s1 / s2 / s3 for a polarimeter -- its settings (the wavelength) and
actions (Zero, after a "beam blocked" confirmation), and Connect /
Disconnect.

With **Live** on, a connected instrument is read continuously. While a
measurement run or a script holds it, the panel makes no reads of its own:
it shows the holder's samples and says who holds it, and its settings and
actions are disabled. Live readings are a look, not a measurement: they do
not need verified timing.


Measuring on a grid of rotator positions
========================================

``api.imcontrol.measureRotatorGrid`` steps rotators over a grid and samples
instruments after every move, into one ``*.run.h5`` file:

.. code-block:: python

    report = api.imcontrol.measureRotatorGrid(
        [('qwp', [i * 5.0 for i in range(37)]),      # outer axis, 0 ... 180°
         ('hwp', [i * 2.5 for i in range(37)])],     # inner axis, 0 ... 90°
        ['pax1'], samples_per_point=5, allow_unverified_timing=True,
    )
    print(report.run_file)

The rotators, the instruments and the waveform outputs are reserved for the
whole run; the rotators return to where they started. **Stop** in
ImScripting ends the run after the current point and keeps the data. Only
rotators audited for measurement runs are accepted -- Standa, Kinesis and
Elliptec mounts -- and never one that runs as a simulation (an unplugged
Elliptec bus, a Standa without its controller), unless the run passes
``allow_simulated=True`` (mock setups; recorded in the run file).

**Viewing a polarisation map.** In ImProcess: *Tools → Load reconstructor
→ Polarisation map*, open the ``*.run.h5`` file, and run *Polarisation map*
from the Parameters panel. It shows the measured states on a 3D Poincaré
sphere and the best angle pair for each target polarisation. The scripting
tutorial ``measurement/01_polarisation_map.py`` does all of this on
``example_no_hardware.json``.

A half-wave plate turned by θ turns the polarisation by 4θ on the Poincaré
sphere, a quarter-wave plate by about 2θ: give the HWP the finer step.


Laser power LUT
===============

``api.imcontrol.measureLaserPowerLut`` measures a laser's power against its
**raw** drive (volts, AOTF amplitude — any loaded LUT is bypassed) and writes
the two-column file that ``calibCsvPath`` loads, if the measurement passes
acceptance:

.. code-block:: python

    report = api.imcontrol.measureLaserPowerLut(
        '775', 'pm1', drive_values=[i * 0.1 for i in range(51)],
        confirm_dark=lambda: True,   # return True only once the beam is blocked
    )
    print(report.lut_file or report.refused)

The laser, the meter and the waveform outputs are reserved for the whole
run. The laser's value and emission are recorded first and restored on every
exit path; the meter is set to the laser's wavelength and zeroed with
emission off, after ``confirm_dark()`` returned ``True``. Runs are written to
``ImSwitchConfig/measurement_runs`` unless ``folder`` is given. Supported
lasers: ``NidaqLaserManager`` and ``AAAOTFLaserManager``.


ThorlabsPM100Manager
====================

Thorlabs PM100D / PM100USB / PM100A power meters over VISA. Reports
``power`` in W. Setting ``wavelength_nm`` (the meter clamps it to its
sensor's range; the readback is what applies). Action ``zero`` (needs the
beam blocked; waits until the meter reports it done).

Timing is not yet verified on hardware: whether ``READ?`` starts a fresh
measurement.

**Setup JSON**

.. code-block:: json

    "instruments": {
        "pm1": {
            "managerName": "ThorlabsPM100Manager",
            "transient": true,
            "managerProperties": {"serial": "P0011748", "wavelengthNm": 775}
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 12 63
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``serial``
     - str
     - Serial number, as in the VISA resource ``USB0::0x1313::0x8078::<serial>::INSTR``.  Required.
   * - ``visaBackend``
     - str
     - pyvisa backend: ``""`` (NI-VISA, default) or ``"@py"`` (pyvisa-py).
   * - ``timeoutMs``
     - int
     - VISA timeout in ms.  Default 2000.
   * - ``wavelengthNm``
     - float
     - Correction wavelength set on connect.  Optional; otherwise the meter keeps its own.


ThorlabsPAX1000Manager
======================

Thorlabs PAX1000 rotating-waveplate polarimeters over VISA, in measurement
mode 9. Reports ``azimuth`` and ``ellipticity`` (rad), ``dop`` and
``power`` (W). The waveplate starts rotating on connect and stops on
disconnect. Settings ``wavelength_nm`` and ``mode``.

Not yet verified on hardware: the timing (what fields 0-8 of a reading
are), the unit of the power field (``powerUnit``), and whether the firmware
answers the settings readbacks — a setting it does not read back keeps the
value ImSwitch sent.

**Setup JSON**

.. code-block:: json

    "instruments": {
        "pax1": {
            "managerName": "ThorlabsPAX1000Manager",
            "transient": true,
            "managerProperties": {"serial": "M01012314", "wavelengthNm": 633}
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 12 63
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``serial``
     - str
     - Serial number, as in the VISA resource string.  Required.
   * - ``visaBackend``
     - str
     - pyvisa backend: ``""`` (NI-VISA, default) or ``"@py"`` (pyvisa-py).
   * - ``timeoutMs``
     - int
     - VISA timeout in ms.  Default 5000.
   * - ``wavelengthNm``
     - float
     - Wavelength set on connect.  Optional.
   * - ``powerUnit``
     - str
     - Unit of the power field of a reading: ``"W"`` (default) or ``"mW"``.  Check against the Thorlabs software.
   * - ``updateBoundS``
     - float
     - A reading returned this long after a read started was measured entirely after it.  Default 0.5.
   * - ``updatePeriodS``
     - float
     - Minimum spacing of distinct readings.  Default 0.1.


MockPowerMeterManager
=====================

A simulated power meter for hardware-free setups (``example_no_hardware``
has one). Same quantities, setting and ``zero`` action as the PM100.

**managerProperties**

.. list-table::
   :widths: 25 12 63
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``powerW``
     - float
     - Simulated optical power at the sensor.  Default 0.001.
   * - ``noiseW``
     - float
     - RMS noise per reading.  Default 2e-7.
   * - ``serial``
     - str
     - Reported serial number.  Default ``"MOCK-PM"``.


MockPAXManager
==============

A simulated PAX1000 behind two waveplates, for hardware-free setups. Same
quantities as the PAX1000. The waveplates are fixed angles, or follow two
rotator entries of the setup (``plate1Rotator`` / ``plate2Rotator``), so
moving those rotators changes the polarisation it reads --
``example_no_hardware`` couples it to its mock QWP and HWP.

**managerProperties**

.. list-table::
   :widths: 25 12 63
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``plate1Deg``
     - float
     - Simulated angle of the first waveplate (quarter-wave).  Default 0.
   * - ``plate2Deg``
     - float
     - Simulated angle of the second waveplate (half-wave).  Default 0.
   * - ``plate1Rotator``
     - str
     - Rotator whose position is the first waveplate's angle, instead of ``plate1Deg``.  Optional.
   * - ``plate2Rotator``
     - str
     - Rotator whose position is the second waveplate's angle, instead of ``plate2Deg``.  Optional.
   * - ``noiseDeg``
     - float
     - Angular noise per reading.  Default 0.2.
   * - ``serial``
     - str
     - Reported serial number.  Default ``"MOCK-PAX"``.
