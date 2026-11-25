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

Power calibrations
------------------

A power calibration maps a laser's raw drive (volts, AOTF amplitude, ...)
to the optical power it gives.  With one **in force** the laser's control
is a 0–100 % setpoint; without one, the control shows the manager's own
unit and range (``valueRangeMin``–``valueRangeMax``) — running on raw units
is a normal state, not an error.  The control shows percent *only* while a
calibration is actually in force: a calibration that is named but does not
load leaves the laser on raw units, flags the row with *calibration not
loaded* and says why in its tooltip (and in the log).

**Which managers apply one.**  ``NidaqLaserManager`` and
``AAAOTFLaserManager`` convert the percent setpoint into their drive.  Every
other laser manager runs on its own units; a ``calibCsvPath`` its setup
names is reported as *not applied* instead of turning the control into
percent over raw units, as it used to.

**Where calibrations live.**  Calibrations are global, one file per
calibration in ``ImSwitchConfig/calibrations`` (created on the first
import), shared by every setup on the computer.  Several can exist for one
laser — measured at the back focal plane and at the sample, with and
without an ND filter — and each records where it was measured and a short
comment.  Which one a laser uses is a fact of the setup:

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Key (``managerProperties``)
     - Meaning
   * - ``calibrationId``
     - The id of a calibration in the global store.  Takes precedence over
       ``calibCsvPath``.  ``"raw"`` runs the laser on raw units even when
       ``calibCsvPath`` is set.  Written by *Save selection to setup file*
       in :ref:`Hardware → Calibrations… <calibrations-window>`.
   * - ``calibCsvPath``
     - A calibration file used as it is, exactly as before: a relative
       path resolves against ImSwitch's working directory.  The
       Calibrations window shows the resolved path, and can import the
       file into the store (the file and the setup stay as they are).

A calibration file is two or more whitespace-separated columns — raw drive,
then measured power (further columns are kept, not used) — with ``#``
comment lines; a comma-separated file does not load.  The measured power is
rescaled so its minimum is 0 % and its maximum 100 %, and a percentage maps
onto the drive through the curve — the same arithmetic as always, so saved
percentages keep their meaning.  A request outside 0–100 % (or the measured
drive range) is refused, never clamped.  A file is refused, and the laser
runs on raw units, when it has fewer than two rows, a constant power, a
power that falls as the drive rises, a repeated drive value, non-numbers,
or a drive unit different from the laser's.

The header fields that ``measureLaserPowerLut`` and
``utility_scripts/aa_aotf_calibration.py`` write are read (laser,
wavelength, setting unit, power unit — W for the former, mW for the latter
— plane, meter, creation time, source run, notes).  When a file states no
power unit, none is assumed: the laser still works in percent, but no
absolute power is shown.  When it states no drive unit, it is assumed to be
in the laser's own unit, and that assumption is recorded.

With a power unit, the setpoint's tooltip shows the power the curve
predicts at the current value ("12.4 mW at back focal plane"), the drive
behind it, the comment and the calibration's id.  Recordings and saved
states record which calibration a value was set under
(``Laser:<name>:Calibration`` = ``{id, contentHash, driveValue}``); a saved
value applied under a different calibration is never reinterpreted: the
recorded drive is applied (converted into the current scale) or, when that
is impossible, the value is skipped — with a warning either way.  Loading the
settings of a recording follows the same rule (see *Loading settings from a
recording* in :doc:`/gui`).

.. code-block:: json

    "managerProperties": { "calibCsvPath": "C:/calib/561_aotf.csv" }

.. code-block:: json

    "managerProperties": { "calibrationId": "561aotf-20261120-3fa9c2" }

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
     - Optional path to a 2-column CSV (raw, measured).  When it loads,
       ``valueUnits`` switches from ``"arb"`` to ``"%"``; see *Power
       calibrations* above.
   * - ``calibrationId``
     - str
     - *(unset)*
     - Optional id of a calibration in the global store; takes precedence
       over ``calibCsvPath``.  ``"raw"`` runs the channel on amplitude units.
       See *Power calibrations* above.
   * - ``useMockOnFailure``
     - bool
     - ``false``
     - When the controller does not answer the startup commands (a pyvisa
       timeout, say), a best-effort channel OFF is sent and the channel is
       left **not connected**: the error shows in Hardware status, commands
       are refused until the channel is reconnected there. ``true`` installs
       a mock instead (the channel then silently accepts commands). Configuration
       errors abort startup either way.

**LaserInfo fields used**

* ``wavelength``, ``valueRangeMin``, ``valueRangeMax``, ``valueRangeStep``
  (via base class).

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — RS232 channel used for the
  ``L<ch>O<0|1>`` / ``L<ch>P<v>`` commands. The optional frequency-startup
  profile additionally sends ``I0`` followed by ``L<ch>F<frequencyMHz>``.

**Vendor library**

None — commands are sent as plain ASCII over the shared RS232 manager.
When the port cannot be opened at all the RS232 sub-manager substitutes a
mock port; when the port opens but the controller does not answer, the
channel is not connected until reconnected from Hardware status (a mock
only with ``useMockOnFailure``). A reconnect repeats the startup exchange
(frequency, control mode) and ends with the channel OFF.

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
                "rampDownDwellS": 0.0
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
     - Defaults to ``false``: after an initialization failure (a best-effort
       immediate OFF is attempted first) the laser is left **not connected**,
       with the error in Hardware status; commands are refused until it is
       reconnected there, which repeats the startup exchange (darken, APC
       mode, limits). ``true`` installs a mock instead.

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
     - Optional path to a 2-column calibration CSV.  When it loads,
       ``valueUnits`` switches from ``"V"`` to ``"%"``; see *Power
       calibrations* above.
   * - ``calibrationId``
     - str
     - *(unset)*
     - Optional id of a calibration in the global store; takes precedence
       over ``calibCsvPath``.  ``"raw"`` runs the laser on volts.  See
       *Power calibrations* above.

Otherwise the manager takes **no managerProperties**.  Wiring comes
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
