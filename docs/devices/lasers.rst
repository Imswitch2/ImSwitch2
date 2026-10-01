********************
Lasers — reference
********************

This page documents the ``LaserManager`` implementations in ImSwitch2.
For each manager you get the setup-file JSON it expects, field-by-field,
plus any required low-level managers and vendor libraries.  Six managers
have no card here yet: ``ESP32LEDMatrixManager``,
``ESP32LightSheetManager``, ``GRBLLaserManager``, ``OxxiusLaserManager``,
``OxxiusCombinerLaserManager`` and ``TriggerScopeLaserManager``.

For the manager-writing-side perspective see
:doc:`/adding-device-support`.

**Vendor libraries.**  The ``hardware`` extra (``pip install -e
".[hardware]"``, see :doc:`../installation`) installs ``pyvisa`` /
``pyvisa-py``, which the RS-232 devices and the bundled Cobolt 06-01 driver
use, plus ``nidaqmx`` and ``microscope`` (for
``PyMicroscopeLaserManager``).  ``pyserial`` is a core dependency.


How lasers are configured
=========================

Lasers live under the top-level ``"lasers"`` dict in your setup JSON.
Each entry deserialises into a
``LaserInfo`` (``imswitch.imcontrol.model.SetupInfo``), which extends
the generic ``DeviceInfo`` with laser-specific fields:

* ``analogChannel`` — analog output identifier (NI-DAQ string or
  pulse-generator channel index).
* ``digitalLine`` — digital output identifier (NI-DAQ line string or
  pulse-generator channel index).
* ``wavelength`` — laser wavelength in nm.
* ``valueRangeMin`` / ``valueRangeMax`` — power/voltage range.
* ``valueRangeStep`` — UI step size (default ``1.0``).
* ``freqRangeMin`` / ``freqRangeMax`` / ``freqRangeInit`` — optional
  frequency-modulation range (defaults ``0``).
* ``powerDevice`` — name of another laser entry that sets this one's
  power, for a beam path where the gate and the power are separate
  hardware (a TTL-gated line whose power an AOTF channel sets, for
  example).  A scan that gates this laser switches that device on for its
  duration.  Default ``null``: the entry owns both gate and power.

Which of these a manager actually consumes depends on the manager —
each section below lists its "LaserInfo fields used".

Power calibration file
----------------------

Every laser manager accepts ``calibCsvPath`` in its ``managerProperties``
(the base class reads it through ``hasProperty``).  When the key is
present the laser widget becomes a 0–100 % setpoint instead of the raw
``valueRangeMin``–``valueRangeMax`` range: ``LaserController`` asks
``usesCalibrationLookup()``, which is true whenever the key is set.
``AAAOTFLaserManager`` and ``NidaqLaserManager`` read the file itself
with ``numpy.loadtxt``: two columns, raw value then measured power, one
pair per line, separated by **whitespace** (a comma-separated file does not
load; lines starting with ``#`` are ignored).  The measured column is
rescaled so that its minimum is 0 % and its maximum 100 %, and the
percentage is mapped onto the raw range through it.  A manager that does
not read the file still switches its widget to percent when the key is
set, so only set it for a manager that implements the lookup.

.. code-block:: json

    "managerProperties": { "calibCsvPath": "C:/calib/561_aotf.csv" }

.. code-block:: json

    "lasers": {
        "<your_laser_name>": {
            "managerName": "<one of the classes below>",
            "managerProperties": { "...": "..." },
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 100,
            "analogChannel": null,
            "digitalLine": null
        }
    }


Choosing between Cobolt variants
================================

Four Cobolt-family managers coexist for historical reasons; pick one:

* ``Cobolt0601LaserManager`` — uses the bundled Cobolt 06-01 driver
  ``cobolt.cobolt0601.Cobolt0601_f2`` from
  ``imswitch.imcontrol.model.lantzdrivers`` via ``LantzLaserManager``.
  Despite the package name the driver has no Lantz dependency; it talks
  over ``pyvisa`` through ``RS232Driver``.  Drives APC and modulation
  modes through raw SCPI strings (``cp``, ``em``, ``slmp``, ``sdmes``).
* ``Cobolt0601NewLaserManager`` — pyserial-based.  Talks to the
  in-tree ``PyCoboltManager`` (``Cobolt06`` class) which wraps
  ``pyserial`` directly.  Has an explicit mock fallback to
  ``imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601.MockCobolt06``.
* ``CoboltLaserManager`` — empty subclass alias for
  ``Cobolt0601LaserManager``, preserved for backwards compatibility.
* ``PyCoboltManager`` — **not a LaserManager**.  It is the bundled
  pyserial driver module (``CoboltLaser`` / ``Cobolt06`` / ``Cobolt06MLD``
  / ``Cobolt06DPL`` classes) consumed by ``Cobolt0601NewLaserManager``.


AAAOTFLaserManager
==================

One channel of an AA Opto-Electronic acousto-optic modulator / tunable
filter, controlled over RS232.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "AOTF488": {
            "managerName": "AAAOTFLaserManager",
            "managerProperties": {
                "rs232device": "aotfRS232",
                "channel": 1,
                "protocolProfile": "aa.compatibility",
                "toggleTrueExternal": false,
                "ttlToggling": false
            },
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 1023
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 18 48
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``rs232device``
     - str
     - **required**
     - Name of the RS232 channel (key into ``rs232sManager``).
   * - ``channel``
     - int
     - **required**
     - AOTF channel index (1-based) this manager drives.
   * - ``protocolProfile``
     - str
     - ``"aa.compatibility"``
     - Command profile. Use ``"aa.frequency-startup"`` only for a controller
       whose RF frequency resets at power-up.
   * - ``frequencyMHz``
     - float
     - ``0`` (disabled)
     - Optional fixed RF frequency to restore at startup. Requires
       ``protocolProfile: "aa.frequency-startup"``. When omitted, no global
       ``I0`` or frequency command is sent. ``0`` is also treated as omitted
       so setup-editor defaults preserve the legacy startup behavior.
   * - ``toggleTrueExternal``
     - bool
     - ``false``
     - Whether the channel should default to external (``true``) or
       internal (``false``) control after power changes.
   * - ``ttlToggling``
     - bool
     - ``false``
     - If ``true``, flip to the opposite control mode briefly during
       enable/value writes so an external TTL source can gate the channel.
   * - ``calibCsvPath``
     - str
     - *(unset)*
     - Optional path to a 2-column CSV (raw, measured).  If present, a
       LUT is built and ``valueUnits`` switches from ``"arb"`` to ``"%"``.
       The columns are whitespace-separated; see *Power calibration file*
       above.
   * - ``useMockOnFailure``
     - bool
     - ``true``
     - When the controller does not answer the startup commands (a pyvisa
       timeout, say), send a best-effort channel OFF and continue with this
       channel in mock mode instead of aborting ImSwitch2. Set to ``false``
       when a missing AOTF must be a startup error. Configuration errors
       abort startup either way.

**LaserInfo fields used**

* ``wavelength``, ``valueRangeMin``, ``valueRangeMax``, ``valueRangeStep``
  (via base class).

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — RS232 channel used for the
  ``L<ch>O<0|1>`` / ``L<ch>P<v>`` commands. The optional frequency-startup
  profile additionally sends ``I0`` followed by ``L<ch>F<frequencyMHz>``.

**Vendor library**

None — commands are sent as plain ASCII over the shared RS232 manager.
Two mock paths: the RS232 sub-manager substitutes a mock port when the port
cannot be opened at all, and this manager enters mock mode (see
``useMockOnFailure``) when the port opens but the controller does not answer.

**Source**

`AAAOTFLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/AAAOTFLaserManager.py>`_


Cobolt0601LaserManager
======================

Cobolt 06-01 series lasers via the bundled driver
``cobolt.cobolt0601.Cobolt0601_f2`` (in
``imswitch.imcontrol.model.lantzdrivers``; no Lantz dependency).  Inherits
from ``LantzLaserManager``.  Uses digital modulation mode for scans.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "Cobolt488": {
            "managerName": "Cobolt0601LaserManager",
            "managerProperties": {
                "digitalPorts": ["COM4"]
            },
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 200
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``digitalPorts``
     - list[str]
     - COM ports to connect to (e.g. ``["COM4"]``).  Multiple ports gang
       multiple lasers behind one manager; ``setValue`` divides power
       by the port count.  **Required** (inherited from
       ``LantzLaserManager``).

**LaserInfo fields used**

* Standard base-class fields: ``wavelength``, ``valueRangeMin``,
  ``valueRangeMax``, ``valueRangeStep``.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.lantzlasers.LantzLaser`` (imported
at module top level in ``LantzLaserManager``) wrapping the bundled
``Cobolt0601_f2`` driver from
``imswitch.imcontrol.model.lantzdrivers.cobolt.cobolt0601``, which uses
``pyvisa`` through ``RS232Driver``.  If the driver cannot be imported or
the laser does not initialise, ``getLaser`` logs a warning and loads the
mock ``Cobolt0601_f2`` from ``imswitch.imcontrol.model.lantzdrivers_mock``
instead; the error propagates only if the mock fails too.

**Source**

`Cobolt0601LaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/Cobolt0601LaserManager.py>`_


Cobolt0601NewLaserManager
=========================

Cobolt 06-01 series lasers via the in-tree pyserial driver
(``PyCoboltManager.Cobolt06``).  Independent of the ``lantzdrivers``
package.  Has an explicit mock fallback.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "Cobolt561": {
            "managerName": "Cobolt0601NewLaserManager",
            "managerProperties": {
                "digitalPorts": ["COM7"],
                "protocolProfile": "cobolt.scpi-compatible",
                "emissionControl": "master",
                "startupControl": "external",
                "scpiPowerUnit": "mW"
            },
            "wavelength": 561,
            "valueRangeMin": 0,
            "valueRangeMax": 100
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``digitalPorts``
     - list[str]
     - COM ports to connect to.  Only the first port is used (the
       manager indexes ``[0]`` after normalisation).  **Required**.
   * - ``useMockOnFailure``
     - bool
     - Defaults to ``true``. When the configured port cannot be opened, start
       with ``MockCobolt06`` instead of aborting ImSwitch2 startup. Set it to
       ``false`` for a hardware-required setup where a missing laser must be
       reported as an error.
   * - ``simulation``
     - bool
     - Defaults to ``false``. When ``true`` the real serial transport is never
       opened and ``MockCobolt06`` is used from the start, whether or not the
       port exists -- for a deliberately simulated laser, as opposed to the
       ``useMockOnFailure`` fallback.
   * - ``emissionControl``
     - str
     - ``"master"`` uses ``l0``/``l1`` and is the default fail-safe off path.
       ``"pause"`` uses ``las:paus 1``/``0`` and is only for OEM/interlock
       firmware that must not be stopped with ``l0``; it assumes the laser is
       otherwise externally started/armed. ``"auto"`` is diagnostic-only: it
       logs the detected command family and still resolves to ``"master"``.
   * - ``protocolProfile``
     - str
     - ``"auto"`` (the default) selects a command profile using read-only
       probes. A known controller can be pinned to ``"cobolt.legacy"`` or
       ``"cobolt.scpi-compatible"``.
   * - ``startupControl``
     - str
     - ``"external"`` (the default) never sends a software-start command.
       ``"software"`` sends ``@cob1`` once after establishing the
       modulation gate and requires ``emissionControl: "pause"`` so normal
       off transitions do not undo the start with ``l0``. This is an explicit
       per-laser setting and is never auto-detected.
   * - ``modulationPowerMw``
     - float
     - Digital-modulation setpoint used for the idle safe state.  Defaults to
       5 mW.
   * - ``scpiPowerUnit``
     - str
     - Unit used by SCPI power setpoint commands.  Defaults to ``"mW"``, which
       matches Cobolt's current ``pycobolt`` ``Cobolt06`` wrapper.  Use ``"W"``
       only for firmware/configurations that expose SCPI setpoints in watts.
   * - ``scanResumeSettleMs``
     - float
     - Extra delay, in milliseconds, after a successful pause-mode scan resume
       before returning control to the scan.  Defaults to ``0`` and is only
       relevant with ``emissionControl: "pause"``.

**LaserInfo fields used**

* Standard base-class fields only (``wavelength``, ``valueRangeMin``,
  ``valueRangeMax``, ``valueRangeStep``).

**Low-level dependencies**

None.

**Vendor library**

``PyCoboltManager.Cobolt06`` (lazy-imported from the sibling module).
Built on ``pyserial`` (also lazy-loaded inside ``CoboltLaser.connect``).
If construction fails, the manager swaps in
``imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601.MockCobolt06``
for headless operation.

**Gotchas**

* On startup the manager queries ``gfv?`` (firmware), ``sn?``/``gsn?``
  (serial), and ``glm?`` (model) and logs the selected command profile.
  Keep this log line when reporting a Cobolt that behaves differently from
  the existing units.
* If an older manager controlled the laser but this manager cannot, try
  ``"emissionControl": "pause"`` only when the controller is known to be
  externally started/armed and must not receive ``l0``.  ``"auto"`` is useful
  for logging/diagnosis, but intentionally keeps the master-off path.
* A controller that requires ``@cob1`` should use the explicit combination
  ``"protocolProfile": "cobolt.scpi-compatible"``,
  ``"emissionControl": "pause"``, and
  ``"startupControl": "software"``. Existing OEM pause-controlled units
  that must not receive ``@cob1`` keep the default ``"external"`` value.

**Source**

`Cobolt0601NewLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/Cobolt0601NewLaserManager.py>`_


CoboltLaserManager
==================

Empty subclass alias for ``Cobolt0601LaserManager``, preserved for
backwards compatibility with older setup files.

**Setup JSON**

Identical to ``Cobolt0601LaserManager`` (see above) — just substitute
``"managerName": "CoboltLaserManager"``.

**managerProperties**

Inherited verbatim from ``Cobolt0601LaserManager``:

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``digitalPorts``
     - list[str]
     - COM ports to connect to.  Several ports gang several lasers behind one manager, as for ``Cobolt0601LaserManager``.  **Required**.

**LaserInfo fields used**

Same as ``Cobolt0601LaserManager``.

**Low-level dependencies**

None.

**Vendor library**

Same as ``Cobolt0601LaserManager`` (bundled ``cobolt.cobolt0601.Cobolt0601_f2``
driver, with the mock driver as fallback).

**Source**

`CoboltLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/CoboltLaserManager.py>`_


CoolLEDLaserManager
===================

CoolLED illumination system; each manager instance controls one LED
channel (A–H) over RS232.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "CoolLED_A": {
            "managerName": "CoolLEDLaserManager",
            "managerProperties": {
                "rs232device": "coolLED",
                "channel_index": "A"
            },
            "wavelength": 470,
            "valueRangeMin": 0,
            "valueRangeMax": 100
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 18 48
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``rs232device``
     - str
     - **required**
     - Name of the RS232 channel (looked up in ``rs232sManager``).
   * - ``channel_index``
     - str
     - ``"A"``
     - LED channel letter A–H.  The default ``"A"`` is only used when
       hardware init fails and the manager falls into mock mode.

**LaserInfo fields used**

* ``freqRangeMin``, ``freqRangeMax``, ``freqRangeInit`` — if all three
  are non-``null``, the manager declares itself ``isModulated=True``
  (the modulation getters/setters are otherwise inherited base no-ops).
  They default to ``0``, not ``null``, so an entry that leaves them out is
  modulated; set one of them to ``null`` explicitly to turn modulation off.
* Standard base-class fields.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — sends ``C<ch><N|F>`` and
  ``C<ch>IX<nnn>`` commands.

**Vendor library**

None — plain ASCII over RS232.  If the RS232 lookup or
``managerProperties['channel_index']`` access raises, the manager
enters its own mock mode and silently drops commands.

**Source**

`CoolLEDLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/CoolLEDLaserManager.py>`_


ESP32LEDLaserManager
====================

LEDs / lasers connected to an ESP32 that exposes a REST API.  Each
manager instance drives one channel through the ESP32's RS232 (squid)
interface.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "ESP32_0": {
            "managerName": "ESP32LEDLaserManager",
            "managerProperties": {
                "rs232device": "esp32",
                "channel_index": 0
            },
            "wavelength": 470,
            "valueRangeMin": 0,
            "valueRangeMax": 1023
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 18 48
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``rs232device``
     - str
     - **required**
     - Name of the RS232 channel into ``rs232sManager``.
   * - ``channel_index``
     - int
     - ``0``
     - ESP32 laser channel index.  The default ``0`` only applies when
       hardware init fails and the manager runs as a mock.

**LaserInfo fields used**

* Standard base-class fields only.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — the manager calls
  ``_rs232manager._squid.set_laser(channel, power)`` on this object.
  Only a ``SQUIDManager`` RS-232 device has a ``_squid`` attribute; an
  ``ESP32Manager`` device exposes ``_esp32`` instead, so with one of those
  the first ``setEnabled`` / ``setValue`` fails with ``AttributeError``.

**Vendor library**

None directly — communication is via the ESP32 RS232 sub-manager.  If
the RS232 lookup fails, the manager enters mock mode and drops
``setEnabled`` / ``setValue`` calls.

**Source**

`ESP32LEDLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/ESP32LEDLaserManager.py>`_


LEDMatrixManager
================

**Unused legacy abstract base class.**  ``LEDMatrixManager`` mirrors the
``LaserManager`` shape but is not a ``LaserManager`` subclass, and nothing
in the tree subclasses it.  There is no ``LEDMatrixInfo`` dataclass and no
``"leds"`` setup section: ``LEDMatrixInfo`` is only the name of its
constructor argument.  The bundled ``ESP32LEDMatrixManager`` subclasses
``LaserManager``, not this class.  Naming ``LEDMatrixManager`` as a
``managerName`` fails, because it is abstract.

**Setup JSON**

Not applicable.

**managerProperties**

None.

**LaserInfo fields used**

Not applicable.

**Low-level dependencies**

None at the abstract level.

**Vendor library**

None.

**Source**

`LEDMatrixManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/LEDMatrixManager.py>`_


LantzLaserManager
=================

**Base class** for fully-digital lasers driven through a driver from
``imswitch.imcontrol.model.lantzdrivers`` — today only the bundled Cobolt
06-01 driver ``cobolt.cobolt0601.Cobolt0601_f2``.  The package name is
historical: there is no Lantz dependency.  Concrete managers
(``Cobolt0601LaserManager``) supply ``isBinary``, ``valueUnits``,
``valueDecimals`` and the ``driver`` argument, and implement
``setEnabled`` / ``setValue``.

**Setup JSON**

Not applicable: ``LantzLaserManager`` cannot be named as a
``managerName``.  The setup loader passes only the device info, the name
and the low-level managers, and the class leaves the base class's abstract
methods unimplemented.
Use a concrete subclass such as ``Cobolt0601LaserManager``.

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``digitalPorts``
     - list[str]
     - One or more COM ports.  Accepted forms are normalised by
       ``normalise_ports``: a JSON array, a Python-repr list, a
       comma-separated string, or a bare string.  **Required**.

Note: the ``driver`` argument is supplied by subclasses in code, not by
JSON.

**LaserInfo fields used**

* Standard base-class fields (``wavelength``, ``valueRangeMin``,
  ``valueRangeMax``, ``valueRangeStep``).

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.lantzlasers.LantzLaser``
(top-level import).  Driver module names are passed as strings (e.g.
``'cobolt.cobolt0601.Cobolt0601_f2'``).  ``getLaser`` in that module loads
the named driver from ``lantzdrivers`` and, when the driver is missing or
fails to initialise, logs a warning and loads the matching mock from
``lantzdrivers_mock``.

**Source**

`LantzLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/LantzLaserManager.py>`_


MPBLaserManager
===============

MPB Communications lasers (e.g. the 2RU-VFL series at 775 nm) driven
over RS232.  Manager has no class-level docstring documenting the
schema — fields below are derived from the constructor.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "MPB775": {
            "managerName": "MPBLaserManager",
            "managerProperties": {
                "rs232device": "mpb",
                "rampDownEnabled": true,
                "rampDownDurationS": 2.0,
                "rampDownSteps": 20,
                "rampDownDwellS": 0.0,
                "useMockOnFailure": true
            },
            "wavelength": 775,
            "valueRangeMin": 0,
            "valueRangeMax": 3050
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - RS232 channel name resolved via
       ``kwargs['rs232sManager']._subManagers[...]``.  **Required**.
   * - ``rampDownEnabled``
     - bool
     - Ramp a live APC output to the device-reported minimum before normal
       OFF, zero-power, and finalization commands. Defaults to ``true``.
   * - ``rampDownDurationS``
     - float
     - Approximate duration of the descending software ramp. Defaults to
       ``2.0`` seconds; confirm the correct profile for the exact laser and
       firmware.
   * - ``rampDownSteps``
     - int
     - Number of descending ``SETPOWER`` commands. Defaults to ``20``.
   * - ``rampDownDwellS``
     - float
     - Optional dwell at minimum power before diode disable. Defaults to zero.
   * - ``useMockOnFailure``
     - bool
     - Continue in mock mode after an initialization failure. A best-effort
       immediate OFF is attempted first. Set to ``false`` when an unavailable
       or unconfirmed MPB laser must abort startup.

**LaserInfo fields used**

* Standard base-class fields only.  The power widget's range comes from
  ``valueRangeMin`` / ``valueRangeMax`` as usual; the laser's own setpoint
  limits, read at startup with ``GETPOWERSETPTLIM 0``, apply on top of it:
  every setpoint is clipped to them before it is sent.

**Low-level dependencies**

* ``rs232sManager`` (accessed as ``kwargs['rs232sManager']._subManagers[<rs232device>]``).

**Vendor library**

None — commands are MPB SCPI strings (``GETSN``, ``GETPOWERENABLE``,
``SETLDENABLE``, ``SETPOWER``, ``POWER``) sent over RS232. Startup never
re-enables emission while correcting APC mode. If an APC laser was left
emitting by a crashed process, startup uses the configured ramp before
disabling it. Initialization failure triggers an independent best-effort
immediate OFF before the optional mock fallback.

While emission is disabled, positive power changes are cached rather than
written. Enabling flushes the cached user setpoint before ``SETLDENABLE 1``.
This keeps the desired power separate from the temporary minimum-power
setpoint reached by a graceful ramp.

**Source**

`MPBLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/MPBLaserManager.py>`_


NidaqLaserManager
=================

Lasers controlled by an NI-DAQ board's analog and/or digital outputs.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "Laser488": {
            "managerName": "NidaqLaserManager",
            "managerProperties": {},
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 5,
            "analogChannel": "Dev1/ao0",
            "digitalLine": "Dev1/port0/line0"
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 15 18 42
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``calibCsvPath``
     - str
     - *(unset)*
     - Optional path to a 2-column calibration CSV.  When present, a
       LUT is built and ``valueUnits`` switches from ``"V"`` to ``"%"``.
       The columns are whitespace-separated; see *Power calibration file*
       above.

Otherwise the manager takes **no managerProperties** — the class
docstring explicitly says "Manager properties: None".  Wiring comes
from ``analogChannel`` / ``digitalLine`` on the ``LaserInfo``.

**LaserInfo fields used**

* ``analogChannel`` — via ``laserInfo.getAnalogChannel()`` to decide
  ``isBinary``.
* ``valueRangeMin`` / ``valueRangeMax`` — passed to
  ``nidaqManager.setAnalog`` as voltage clamps.
* Plus the standard base-class fields.

**Low-level dependencies**

* ``nidaqManager`` — used for ``setDigital(name, enabled)`` and
  ``setAnalog(target, voltage, min_val, max_val)``.

**Vendor library**

None directly.  All NI-DAQmx traffic goes through the
``NidaqManager`` low-level manager.

**Source**

`NidaqLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/NidaqLaserManager.py>`_


PulseGeneratorLaserManager
==========================

Backend-agnostic laser driven by a ``PulseGeneratorManager``
(Teensy today; PulseStreamer/NI in the future).  v4-era replacement
for ``PulseStreamerLaserManager``.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "PG488": {
            "managerName": "PulseGeneratorLaserManager",
            "managerProperties": {},
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 5,
            "digitalLine": 3,
            "analogChannel": 0
        }
    }

**managerProperties**

None.  The class docstring lists ``digitalChannel`` /
``analogChannel`` as "managerProperties", but the implementation reads
them from the top-level ``LaserInfo`` fields ``digitalLine`` and
``analogChannel`` via ``getattr(laserInfo, ...)``.

**LaserInfo fields used**

* ``digitalLine`` — pulse-generator channel index for on/off gating.
* ``analogChannel`` — pulse-generator channel index for analog power
  (ignored if the backend reports ``supports_analog == False``).

**Low-level dependencies**

* ``pulseGeneratorManager`` — required; accessed via
  ``lowLevelManagers.get('pulseGeneratorManager')``.  If ``None`` (e.g.
  ``setupInfo.teensyPulse`` was left unset), the manager enters mock
  mode (``_isMock=True``).

**Vendor library**

None directly — all hardware traffic goes through the pulse-generator
manager.  Mock fallback triggers when no ``pulseGeneratorManager`` is
wired.

**Gotchas**

* If you configure ``analogChannel`` but the backend reports it does
  not support analog (``supports_analog`` ``False``), the manager logs
  a warning and treats the laser as binary.

**Source**

`PulseGeneratorLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/PulseGeneratorLaserManager.py>`_


PulseStreamerLaserManager
=========================

Swabian Pulse Streamer 8/2.  **Legacy.**  This manager expects a
``pulseStreamerManager`` low-level manager, which ``MasterController`` no
longer constructs, so it always starts in mock mode (with a warning) and
drops every call.  Setup validation also flags a ``pulseStreamer`` section
as legacy.  Use ``PulseGeneratorLaserManager`` with a pulse-generator
backend instead.

**Setup JSON**

.. code-block:: json

    "lasers": {
        "PS488": {
            "managerName": "PulseStreamerLaserManager",
            "managerProperties": {},
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 5,
            "digitalLine": 0,
            "analogChannel": 0
        }
    }

**managerProperties**

None.  The class docstring lists ``digitalChannel`` /
``analogChannel`` but the implementation reads them from
``laserInfo.digitalLine`` / ``laserInfo.analogChannel`` directly.

**LaserInfo fields used**

* ``digitalLine`` — PulseStreamer digital output (0–7).
* ``analogChannel`` — PulseStreamer analog output (0–1); ``None`` →
  manager declares itself binary.
* ``valueRangeMin`` / ``valueRangeMax`` — passed to ``setAnalog`` as
  voltage clamps.

**Low-level dependencies**

* ``pulseStreamerManager`` — accessed as
  ``lowLevelManagers["pulseStreamerManager"]``.  It is never provided, so
  the lookup fails and the manager enters mock mode and silently drops
  calls.

**Vendor library**

None directly — it would use the PulseStreamer low-level manager, which
is not constructed (see above).

**Source**

`PulseStreamerLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/PulseStreamerLaserManager.py>`_


PyCoboltManager
===============

**Not a LaserManager.**  Despite living in
``model/managers/lasers/``, this module contains pure pyserial driver
classes (``CoboltLaser``, ``Cobolt06``, ``Cobolt06MLD``,
``Cobolt06DPL``) plus a ``list_lasers()`` helper.  These are consumed
by ``Cobolt0601NewLaserManager``.

**Setup JSON**

Not applicable — ``PyCoboltManager`` cannot be used as ``managerName``.
Use ``Cobolt0601NewLaserManager`` instead.

**managerProperties**

N/A.

**LaserInfo fields used**

N/A.

**Low-level dependencies**

None.

**Vendor library**

``pyserial`` (a core dependency of ImSwitch2) — imported lazily inside
``CoboltLaser.connect`` and ``send_cmd``.  No mock fallback within this
module.

**Source**

`PyCoboltManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/PyCoboltManager.py>`_


PyMicroscopeLaserManager
========================

Generic adapter for lasers supported by the
`Python Microscope <https://python-microscope.org>`_ library
(``microscope.lights.*``).

**Setup JSON**

.. code-block:: json

    "lasers": {
        "Toptica": {
            "managerName": "PyMicroscopeLaserManager",
            "managerProperties": {
                "pyMicroscopeDriver": "toptica.TopticaiBeam",
                "digitalPorts": "COM4"
            },
            "wavelength": 488,
            "valueRangeMin": 0,
            "valueRangeMax": 200
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 12 25 38
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``pyMicroscopeDriver``
     - str
     - ``"Unknown"``
     - Dotted ``module.Class`` to import from ``microscope.lights``
       (e.g. ``"toptica.TopticaiBeam"``).  If malformed or missing the
       manager enters mock mode.
   * - ``digitalPorts``
     - str
     - ``"Unknown"``
     - Connection string forwarded to the driver constructor (e.g. a
       COM port).  Note: documented as a list in the docstring but
       passed through as-is to the driver.

**LaserInfo fields used**

* ``valueRangeMax`` — read as ``self.__maxPower``; ``setValue`` divides
  the user value by it to derive a fraction (0–1) handed to the
  Python-Microscope ``power`` setter.
* Standard base-class fields.

**Low-level dependencies**

None.

**Vendor library**

``microscope.lights.<module>`` — lazy-imported via ``importlib``.  On
any import or construction failure the manager enters mock mode
(``_isMock=True``) and silently drops calls.

**Gotchas**

* ``maxPower`` (``valueRangeMax``) must be non-zero; ``setValue``
  short-circuits with an error log if it is zero.

**Source**

`PyMicroscopeLaserManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/lasers/PyMicroscopeLaserManager.py>`_
