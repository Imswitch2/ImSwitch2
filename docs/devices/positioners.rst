************************
Positioners — reference
************************

This page documents every ``PositionerManager`` implementation in
ImSwitch.  For each manager you get the setup-file JSON it expects,
field-by-field, plus any required low-level managers and vendor
libraries.

For the manager-writing-side perspective see
:doc:`/adding-device-support`; for end-to-end recipes see the
:doc:`how-to guides </how-to/wire-teensy>`.


How positioners are configured
==============================

Positioners live under the top-level ``"positioners"`` dict in your
setup JSON.  Each entry extends the generic
:class:`~imswitch.imcontrol.model.SetupInfo.DeviceInfo` shape with the
:class:`~imswitch.imcontrol.model.SetupInfo.PositionerInfo` fields
``axes`` (required), ``isPositiveDirection``, ``forPositioning``,
``forScanning``, ``resetOnClose``, ``joystick`` and ``liveUpdate``.
The abstract base requires that at least one of ``forPositioning`` or
``forScanning`` be ``true``; otherwise construction raises
``ValueError``.

.. code-block:: json

    "positioners": {
        "<your_positioner_name>": {
            "managerName": "<one of the classes below>",
            "managerProperties": { "...": "..." },
            "axes": ["X", "Y"],
            "forPositioning": true,
            "forScanning": false,
            "resetOnClose": false
        }
    }

The abstract base class
:class:`~imswitch.imcontrol.model.managers.positioners.PositionerManager.PositionerManager`
defines ``move`` and ``setPosition`` as abstract, plus a no-op
``finalize``.  It does **not** define ``jog_start`` / ``jog_stop`` —
those are an optional extension implemented by individual managers.

Choosing between Piezoconcept variants
--------------------------------------

``PiezoconceptZManager`` and ``PiezoconceptZManager2`` drive the same
Piezoconcept Z-piezo over RS-232 using the identical command set
(``MOVRZ`` / ``MOVEZ`` / ``GET_Z``) and identical reply parsing.  The
only behavioural differences in the source are at construction time:
``PiezoconceptZManager2`` reads an additional ``range_um``
``managerProperty`` and centers the stage to ``range_um // 2`` on
startup; ``PiezoconceptZManager`` does not.  Its docstring describes
it as "Adapted to new Firmware as of 2026."


BSC203StageManager
==================

Thorlabs BSC203 three-channel benchtop stepper controller (driving a
3-axis stage via APT).

**Setup JSON**

.. code-block:: json

    "positioners": {
        "BSC203": {
            "managerName": "BSC203StageManager",
            "managerProperties": {
                "port": "COM9",
                "home": false,
                "travelRangeUm": 8000
            },
            "axes": ["X", "Y", "Z"],
            "forPositioning": true,
            "forScanning": false
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
   * - ``port``
     - str
     - ``"COM9"``
     - Serial port of the BSC203 controller.
   * - ``home``
     - bool
     - ``false``
     - When ``true``, perform an APT homing operation on startup.  Homing parks each axis at its end-stop (position 0).
   * - ``travelRangeUm``
     - int or float
     - ``8000``
     - Full travel per axis in µm (``8000`` for DRV208 8 mm actuators).  Absolute coordinates run ``0..travelRangeUm`` and every move is clamped to that range.
   * - ``invertJogAxes``
     - list[str]
     - ``[]``
     - Axis labels whose jog direction should be flipped; see **Movement model** below.

**Movement model**

Both absolute (:meth:`setPosition`) and relative (:meth:`move`) moves are
issued as a bounded **jog**: a positive step size of ``|target - current|``
encoder counts plus a direction flag.  ``move_absolute`` / ``move_relative``
are **never** used.

This is essential, not stylistic.  The BSC203 firmware mishandles a move whose
*displacement* is negative — i.e. any move to a position **below** the current
one — reading the signed displacement as unsigned and driving the motor to the
end-stop at full speed ("the negative direction runs away").  This fires for
*any* downward move, even to a perfectly valid positive target, so clamping the
target alone never fixed it.  The jog command takes a positive size and an
explicit direction, which the firmware handles correctly both ways.

The target is pre-clamped to ``[0, travelRangeUm]`` so a jog can never drive
past an end-stop, and the displacement is taken from the *live* encoder so
repeated moves self-correct.  A forward jog is assumed to increase the encoder
(so ``+target`` increases the displayed position); if an axis moves the wrong
way on your rig, list it in ``invertJogAxes``.

* ``invertJogAxes`` — axis labels whose jog direction should be flipped
  (default ``[]``).  Use ``utility_scripts/bsc203_diag.py calibrate`` to confirm
  direction empirically.

**PositionerInfo fields used**

* ``axes`` — used to seed the initial-position dict; axis labels
  ``"X"`` / ``"Y"`` / ``"Z"`` are mapped to BSC bays 0 / 1 / 2 inside
  ``move`` and ``setPosition``.

**Axes**

Multi-axis (three channels).  No ``len(axes)`` validation; axis labels
are matched by string against ``"X"``, ``"Y"``, ``"Z"``.

**Jog API**

Not supported by the abstract base.  The class does expose a
non-standard ``jog(sign, axis)`` helper used internally to implement
relative moves, but no ``jog_start`` / ``jog_stop`` methods.

**Low-level dependencies**

None.

**Vendor library**

``thorlabs_apt_device.devices.bsc.BSC`` (and ``serial.serialutil``),
lazy-imported inside ``__init__``.  On ``ImportError`` the manager
logs an install hint (``pip install thorlabs_apt_device``) and sets
``self.dev = None``; any other exception during construction is
caught and also leaves ``self.dev = None``.  There is no functional
mock fallback — calls into the manager will fail if ``self.dev`` is
``None``.

**Source**

`BSC203StageManager.py <../../imswitch/imcontrol/model/managers/positioners/BSC203StageManager.py>`_


JenaPiezoZManager
=================

Jena piezo Z-stage driven over RS-232.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "JenaZ": {
            "managerName": "JenaPiezoZManager",
            "managerProperties": {
                "rs232device": "JenaCOM",
                "posRangeUm": [0, 100],
                "waitForSettle": true,
                "settleToleranceUm": 0.1,
                "settleTimeoutS": 1.0
            },
            "axes": ["Z"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 18 46
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``rs232device``
     - str
     - **required**
     - Name of the RS-232 channel registered in ``rs232sManager``.
   * - ``posRangeUm``
     - list[float]
     - ``[0, 100]``
     - Allowed position range in µm, ``[min, max]``.  ``setPosition``
       refuses values outside this range.
   * - ``waitForSettle``
     - bool
     - ``true``
     - If true, poll the device after each move until the readback is
       within ``settleToleranceUm`` of target.
   * - ``settleToleranceUm``
     - float
     - ``0.1``
     - Maximum deviation in µm to consider the stage settled.
   * - ``settleTimeoutS``
     - float
     - ``1.0``
     - Maximum time in seconds to wait for settling before raising
       ``TimeoutError``.  A retry of the write command is issued at
       roughly half this timeout.

**PositionerInfo fields used**

* ``axes`` — must have length 1 (otherwise ``RuntimeError``).
* ``managerProperties`` — for all settings listed above.

**Axes**

Single-axis only.  The constructor raises ``RuntimeError`` if
``len(positionerInfo.axes) != 1``.

**Jog API**

Not supported (inherits abstract base; the base does not define
jog methods).

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — provides the ``query()`` channel
  used for all device I/O.

**Vendor library**

None directly.  All hardware access goes through the configured
RS-232 manager.  No mock fallback in this manager.

**Source**

`JenaPiezoZManager.py <../../imswitch/imcontrol/model/managers/positioners/JenaPiezoZManager.py>`_


KDC101PositionerManager
=======================

Thorlabs KDC101 single-axis motor controller exposed through the
generic Positioner widget.  Use this for KDC-driven linear stages,
rotation stages, sliders, or other single-axis actuators where the
KDC is the general-purpose motion controller rather than a semantic
rotator.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "Rotation stage": {
            "managerName": "KDC101PositionerManager",
            "managerProperties": {
                "port": "COM15",
                "posConvFac": 1919.6418578623391,
                "velConvFac": 1.0,
                "accConvFac": 1.0,
                "positionUnit": "deg",
                "homeOnInit": false
            },
            "axes": ["R"],
            "forPositioning": true,
            "forScanning": false,
            "resetOnClose": false,
            "liveUpdate": true
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
   * - ``port``
     - str
     - **required**
     - Serial port for the KDC101 controller.
   * - ``posConvFac``
     - float
     - **required**
     - Encoder counts per ImSwitch position unit.
   * - ``velConvFac``
     - float
     - **required**
     - Encoder velocity conversion factor.
   * - ``accConvFac``
     - float
     - **required**
     - Encoder acceleration conversion factor.
   * - ``positionUnit``
     - str
     - ``"um"``
     - Unit label passed to the Positioner widget, for example ``"deg"`` for
       a KDC-driven rotation stage.
   * - ``homeOnInit``
     - bool
     - ``false``
     - If true, home the device during construction.

**PositionerInfo fields used**

* ``axes`` — must contain exactly one axis label.  The label is used
  by the Positioner widget and API calls.
* ``liveUpdate`` — useful for a KDC because moves may be asynchronous;
  when true, the Positioner controller periodically refreshes the
  displayed position.

**Vendor library**

``thorlabs_apt_device.devices.kdc101.KDC101`` from
``thorlabs-apt-device``.  If the package or device is unavailable,
the manager logs an error and leaves the device disabled.

**Source**

`KDC101PositionerManager.py <../../imswitch/imcontrol/model/managers/positioners/KDC101PositionerManager.py>`_


KinesisStageManager
===================

Thorlabs MLS203 two-axis motorized stage driven via the Kinesis stack.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "MLS203": {
            "managerName": "KinesisStageManager",
            "managerProperties": {
                "snr": "12345678",
                "scale": "MLS203",
                "isRackSystem": true,
                "homeOnInit": false,
                "driverUnitsPerPositionUnit": 1.0
            },
            "axes": ["X", "Y"],
            "forPositioning": true,
            "forScanning": false
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
   * - ``snr``
     - str
     - **required**
     - Kinesis serial number of the device.
   * - ``scale``
     - str
     - ``"MLS203"``
     - Stage scale identifier passed to the pylablib driver.
   * - ``isRackSystem``
     - bool
     - ``true``
     - Whether the device is rack-mounted.
   * - ``homeOnInit``
     - bool
     - ``false``
     - If true, home both axes during construction.
   * - ``driverUnitsPerPositionUnit``
     - float
     - ``1.0``
     - Conversion factor between ImSwitch position units and the raw values
       accepted/reported by the pylablib driver. Keep at ``1.0`` when pylablib
       recognizes the stage scale. Set only when pylablib falls back to raw
       internal units.
   * - ``unitsPerUm``
     - float
     - ``1.0``
     - **Deprecated** spelling of ``driverUnitsPerPositionUnit``; still read
       when the new key is absent, with a warning at startup.

**PositionerInfo fields used**

* ``axes`` — used to seed initial positions and to iterate at homing
  time.  Axis labels must be ``"X"`` or ``"Y"``; mapped to channels
  1 and 2 by ``_axis_to_channel`` (otherwise ``ValueError``).

**Axes**

Two-axis (X/Y).  Axis labels are validated at use time by
``_axis_to_channel``; ``len(axes)`` is not validated explicitly.

**Jog API**

Supported.  Defines ``jog_start(axis, sign)`` and ``jog_stop(axis)``;
both delegate to the pylablib continuous-jog API.

**Shutdown behavior**

Set ``resetOnClose`` to ``false`` for Kinesis stages unless the setup
explicitly requires returning both axes to zero during shutdown. The generic
positioner controller resets all positioners with ``resetOnClose=true`` by
calling ``setPosition(0, axis)`` for every axis before the manager connection
is closed.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.kinesisstage.KinesisStage``
(wrapper around ``pylablib``), lazy-imported inside ``_getStageObj``.
On any exception the manager logs a warning and substitutes
``MockKinesisStage`` from the same module for headless operation.

**Source**

`KinesisStageManager.py <../../imswitch/imcontrol/model/managers/positioners/KinesisStageManager.py>`_


LeicaDMIManager
===============

Leica DMI microscope stand (Z focus drive and accessory control) over
RS-232.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "LeicaDMI": {
            "managerName": "LeicaDMIManager",
            "managerProperties": {
                "rs232device": "LeicaCOM",
                "calibCsvPath": "C:/calib/leica_z.csv"
            },
            "axes": ["Z"],
            "forPositioning": true,
            "forScanning": false
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
     - Name of the RS-232 channel registered in ``rs232sManager``.
       Subscripted via ``managerProperties['rs232device']``.
   * - ``calibCsvPath``
     - str
     - *(optional)*
     - Path to a calibration CSV used to build look-up tables between
       device units and nanometres.  If missing or malformed, the
       LUTs remain ``None`` and the manager reports positions in
       arbitrary device units.

**PositionerInfo fields used**

* ``managerProperties['rs232device']`` and (optionally)
  ``managerProperties['calibCsvPath']``.  This manager does **not**
  call ``super().__init__`` and therefore does not initialise the
  standard ``axes``/``forPositioning``/``forScanning`` properties on
  ``PositionerManager``; in particular, the abstract base's
  "at least one of forPositioning/forScanning" check is bypassed.

**Axes**

Effectively single-axis (Z drive).  No validation of
``positionerInfo.axes`` is performed.

**Jog API**

Not supported.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — used for all stand
  communication.  On ``KeyError`` the manager falls back to
  ``MockRS232Driver`` and logs an error.

**Vendor library**

None — communication is plain text over RS-232.  ``numpy`` and
``scipy.interpolate.interp1d`` are used for the calibration LUT.

**Gotchas**

The class skips ``super().__init__``; properties such as
``self.axes`` and ``self.position`` from the abstract base are
therefore not populated in the usual way.  ``setPosition`` assigns a
scalar to ``self._position`` rather than the ``{axis: value}`` dict
used by other managers.

**Source**

`LeicaDMIManager.py <../../imswitch/imcontrol/model/managers/positioners/LeicaDMIManager.py>`_


MHXYStageManager
================

Marzhauser XY stage driven over RS-232.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "MHXY": {
            "managerName": "MHXYStageManager",
            "managerProperties": {
                "rs232device": "MHCOM"
            },
            "axes": ["X", "Y"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - Name of the RS-232 channel registered in ``rs232sManager``.
       Required (subscripted, no default).

**PositionerInfo fields used**

* ``axes`` — must be exactly ``["X", "Y"]`` (or any 2-element list
  containing both labels); otherwise ``RuntimeError``.

**Axes**

Two-axis only.  Validated in the constructor:
``len(positionerInfo.axes) != 2`` or missing ``"X"``/``"Y"`` raises
``RuntimeError``.

**Jog API**

Not supported.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — used for all stage I/O.  On
  any exception during lookup the manager falls back to
  ``MockRS232Driver`` and logs a warning.

**Vendor library**

None — plain-text protocol over RS-232.

**Source**

`MHXYStageManager.py <../../imswitch/imcontrol/model/managers/positioners/MHXYStageManager.py>`_


MockPositionerManager
=====================

In-process mock positioner with no hardware backing.  Useful for
repeating measurements, timelapses, and headless tests.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "MockPos": {
            "managerName": "MockPositionerManager",
            "managerProperties": {},
            "axes": ["X"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

This manager reads no entries from ``managerProperties``.

**PositionerInfo fields used**

* ``axes`` — must have length 1 (otherwise ``RuntimeError``).

**Axes**

Single-axis only.

**Jog API**

Not supported.

**Low-level dependencies**

None.

**Vendor library**

None.

**Source**

`MockPositionerManager.py <../../imswitch/imcontrol/model/managers/positioners/MockPositionerManager.py>`_


NidaqPositionerManager
======================

Analog-controlled positioner driven via NI-DAQ analog out.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "NidaqZ": {
            "managerName": "NidaqPositionerManager",
            "managerProperties": {
                "conversionFactor": 10.0,
                "minVolt": -10.0,
                "maxVolt": 10.0
            },
            "analogChannel": 0,
            "axes": ["Z"],
            "forPositioning": true,
            "forScanning": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``conversionFactor``
     - float
     - µm-per-volt scaling.  Voltage written =
       ``position / conversionFactor``.
   * - ``minVolt``
     - float
     - Minimum allowed analog voltage.
   * - ``maxVolt``
     - float
     - Maximum allowed analog voltage.

All three are **required** (subscripted, no defaults).

**PositionerInfo fields used**

* ``axes`` — must have length 1 (otherwise ``RuntimeError``).
* ``analogChannel`` is consumed indirectly via the NI-DAQ manager
  (this manager itself only reads ``managerProperties`` and ``axes``).

**Axes**

Single-axis only.

**Jog API**

Not supported.

**Low-level dependencies**

* ``nidaqManager`` — used via ``setAnalog`` to drive the analog
  output for this positioner (channel target identified by
  ``self.name``).

**Vendor library**

None directly; all hardware access goes through the NI-DAQ manager.

**Source**

`NidaqPositionerManager.py <../../imswitch/imcontrol/model/managers/positioners/NidaqPositionerManager.py>`_


PIStageManager
==============

Physik Instrumente C-663 XY-stage over USB (daisy-chained), with
optional analog joystick (e.g. C-819.20).

**Setup JSON**

.. code-block:: json

    "positioners": {
        "PIStage": {
            "managerName": "PIStageManager",
            "managerProperties": {
                "device": "C-663.11",
                "usb_description": null
            },
            "axes": ["X", "Y"],
            "forPositioning": true,
            "forScanning": false,
            "joystick": true
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
   * - ``device``
     - str
     - **required**
     - PI controller model identifier (e.g. ``"C-663.11"``).  If
       missing the manager raises ``ValueError``.
   * - ``usb_description``
     - str or null
     - *(none — auto-detected)*
     - Optional explicit USB description string.  If omitted the
       manager calls ``EnumerateUSB`` and picks the first device
       whose description matches ``device`` (or its prefix before
       the dot).

**PositionerInfo fields used**

* ``axes`` — must be exactly two axes named ``"X"`` and ``"Y"``
  (otherwise ``RuntimeError``).
* ``managerProperties`` — for the fields above.

**Axes**

Two-axis only.  Validated in the constructor.

**Jog API**

Not supported (no ``jog_start`` / ``jog_stop``).  The class exposes
its own ``activate_joystick`` / ``deactivate_joystick`` methods plus
a Qt timer that polls joystick buttons for fast/slow speed toggling
and joystick enable/disable.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.pipython.pidevice.GCSDevice``
and ``pidevice.gcs2.gcs2pitools`` (ImSwitch-bundled PI Python
wrapper), imported at module top.  No mock fallback — if USB
enumeration or connection fails the manager logs a warning and sets
``self.device = None``; subsequent ``move`` / ``setPosition`` calls
return without doing anything.

**Source**

`PIStageManager.py <../../imswitch/imcontrol/model/managers/positioners/PIStageManager.py>`_


PiezoconceptZManager
====================

Piezoconcept Z-piezo over RS-232 (original firmware).

**Setup JSON**

.. code-block:: json

    "positioners": {
        "PiezoZ": {
            "managerName": "PiezoconceptZManager",
            "managerProperties": {
                "rs232device": "PiezoCOM"
            },
            "axes": ["Z"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - Name of the RS-232 channel registered in ``rs232sManager``.
       Required.

**PositionerInfo fields used**

* ``axes`` — must have length 1 (otherwise ``RuntimeError``).
* ``managerProperties['rs232device']``.

**Axes**

Single-axis only.

**Jog API**

Not supported.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — used for all device I/O via
  ``query()``.

**Vendor library**

None — plain-text protocol (``MOVRZ``, ``MOVEZ``, ``GET_Z``) over
RS-232.

**Source**

`PiezoconceptZManager.py <../../imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager.py>`_


PiezoconceptZManager2
=====================

Piezoconcept Z-piezo over RS-232 (variant the docstring describes as
"Adapted to new Firmware as of 2026").  Same command set as
``PiezoconceptZManager``; differs only in init-time behaviour.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "PiezoZ": {
            "managerName": "PiezoconceptZManager2",
            "managerProperties": {
                "rs232device": "PiezoCOM",
                "range_um": 300
            },
            "axes": ["Z"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - Name of the RS-232 channel registered in ``rs232sManager``.
       Required.
   * - ``range_um``
     - number
     - Total travel range in µm.  Required for the auto-centering
       step on init; if missing or if the centering write fails the
       manager logs a warning and continues without centering.

**PositionerInfo fields used**

* ``axes`` — must have length 1 (otherwise ``RuntimeError``).
* ``managerProperties['rs232device']`` and
  ``managerProperties['range_um']``.

**Axes**

Single-axis only.

**Jog API**

Not supported.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — used for all device I/O via
  ``query()``.

**Vendor library**

None — plain-text protocol over RS-232.

**Source**

`PiezoconceptZManager2.py <../../imswitch/imcontrol/model/managers/positioners/PiezoconceptZManager2.py>`_


SQUIDStageManager
=================

SQUID microscope XYZ stage, driven via a SQUID-aware RS-232 manager
that exposes a ``_squid`` attribute with ``move_x_usteps`` /
``move_y_usteps`` / ``move_z_usteps`` methods.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "SQUID": {
            "managerName": "SQUIDStageManager",
            "managerProperties": {
                "rs232device": "SquidCOM"
            },
            "axes": ["X", "Y", "Z"],
            "forPositioning": true,
            "forScanning": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - Name of the RS-232 channel registered in ``rs232sManager``.
       Required.

**PositionerInfo fields used**

* ``axes`` — used to seed the initial-position dict; axis labels
  must be ``"X"``, ``"Y"`` or ``"Z"`` to dispatch in ``move``.

**Axes**

Three-axis (X/Y/Z).  Not validated by length; axis labels are
checked at use time.

**Jog API**

Not supported.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` — must expose a ``_squid``
  attribute providing the microstep-move API.  On any exception the
  manager falls back to ``MockRS232Driver`` and logs a warning (note
  that the mock does not provide ``_squid``, so moves on the
  fallback will fail).

**Vendor library**

None directly — the SQUID driver lives behind the configured RS-232
manager.

**Source**

`SQUIDStageManager.py <../../imswitch/imcontrol/model/managers/positioners/SQUIDStageManager.py>`_


SmarACTPositionerManager
========================

SmarACT MCS / MCS2 piezo XYZ stage driven via the bundled ``SmarACT``
ctypes wrapper.

**Setup JSON**

.. code-block:: json

    "positioners": {
        "SmarACT": {
            "managerName": "SmarACTPositionerManager",
            "managerProperties": {
                "holdTime": 60000,
                "axis_lookup_table": { "X": 0, "Y": 2, "Z": 1 }
            },
            "axes": ["X", "Y", "Z"],
            "forPositioning": true,
            "forScanning": false,
            "resetOnClose": false
        }
    }

**managerProperties**

.. list-table::
   :widths: 24 14 18 44
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``holdTime``
     - int
     - ``60000``
     - Closed-loop hold time in milliseconds after each move, passed
       as the ``holdTime`` argument to ``SA_GotoPositionAbsolute_S``.
   * - ``axis_lookup_table``
     - dict[str, int]
     - ``{"X": 0, "Y": 2, "Z": 1}``
     - Mapping from axis label to MCS channel index.

Both are sourced via ``in`` checks on ``managerProperties`` rather
than ``.get(...)``, but they behave as optional with the class-level
defaults shown above.

**PositionerInfo fields used**

* ``managerProperties`` — for ``holdTime`` and ``axis_lookup_table``.
* The class hard-codes initial positions to ``{'X': 0, 'Y': 0, 'Z': 0}``
  rather than deriving them from ``positionerInfo.axes``.

**Axes**

Three-axis (X/Y/Z), fixed by the class.

**Jog API**

Not supported.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.SmarACT`` (ctypes wrapper
around the vendor MCS DLL), imported at module top inside
``try/except ImportError: raise`` — meaning the manager module
itself fails to import if the wrapper is unavailable.  No mock
fallback.

**Gotchas**

The class docstring notes: *"The stage will not gracefully exit
when resetOnClose is set to True."*

**Source**

`SmarACTPositionerManager.py <../../imswitch/imcontrol/model/managers/positioners/SmarACTPositionerManager.py>`_
