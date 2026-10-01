**********************
Rotators — reference
**********************

This page documents every ``RotatorManager`` implementation in
ImSwitch2.  For each manager you get the setup-file JSON it expects,
field-by-field, plus any required low-level managers and vendor
libraries.

For the manager-writing-side perspective see
:doc:`/adding-device-support`; to wrap a driver from another project see
:doc:`/how-to/port-from-third-party`.

**Vendor libraries.**  The ``hardware`` extra (``pip install -e
".[hardware]"``, see :doc:`../installation`) installs ``pylablib``, which
the Kinesis and Elliptec rotators use.  The Standa XIMC library comes from
the vendor's installer.


How rotators are configured
===========================

Rotators live under the top-level ``"rotators"`` dict in your setup
JSON.  Each entry uses the generic ``DeviceInfo`` shape
(``imswitch.imcontrol.model.SetupInfo``), but
the only fields actually consumed by rotator managers are
``managerName`` and ``managerProperties``.  ``analogChannel`` and
``digitalLine`` are present in the dataclass but ignored — leave them
out (or ``null``) for rotators.

.. code-block:: json

    "rotators": {
        "<your_rotator_name>": {
            "managerName": "<one of the classes below>",
            "managerProperties": { "...": "..." }
        }
    }


StandaRotatorManager
====================

Standa-branded motorized rotation mounts (e.g. 8SMC5 controller).

**Setup JSON**

.. code-block:: json

    "rotators": {
        "myRotator": {
            "managerName": "StandaRotatorManager",
            "managerProperties": {
                "motorListIndex": 0,
                "ximcLibLocation": "C:/Program Files/XIMC/ximc",
                "stepsPerTurn": 200,
                "microstepsPerStep": 256
            }
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 12 63
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``motorListIndex``
     - int
     - Index of the device in the XIMC-enumerated motor list.  ``0`` for the first detected device.
   * - ``ximcLibLocation``
     - str
     - Filesystem path to the bundled XIMC vendor library.  Often a network drive — if the path becomes unreachable the manager falls back to the mock.
   * - ``stepsPerTurn``
     - int
     - Full motor steps per 360° (e.g. 200 for a 1.8°/step motor).
   * - ``microstepsPerStep``
     - int
     - Microstep subdivision.  Total counts per turn = ``stepsPerTurn × microstepsPerStep``.

All four fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.standamotor.StandaMotor`` (bundled
ImSwitch2 wrapper around XIMC).  Lazy-imported.  If the import or
``StandaMotor`` construction fails (commonly: ``ximcLibLocation``
unreachable network drive), the manager falls back to
``MockStandaMotor`` for headless operation.

**Source**

`StandaRotatorManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/rotators/StandaRotatorManager.py>`_


KinesisRotatorManager
=====================

Thorlabs K10CR1 motorized rotation mounts driven via the Kinesis stack.

**Setup JSON**

.. code-block:: json

    "rotators": {
        "K10CR1": {
            "managerName": "KinesisRotatorManager",
            "managerProperties": {
                "snr": "55000194",
                "unitsPerDegree": 136533.33,
                "homeOnInit": false
            }
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 14 52
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``snr``
     - str
     - **required**
     - Kinesis serial number printed on the device (e.g. ``"55000194"``).
   * - ``unitsPerDegree``
     - float
     - ``136533.33``
     - Encoder counts per degree.  Override only if your firmware revision drifts from the stock K10CR1 value.
   * - ``homeOnInit``
     - bool
     - ``false``
     - If true, home the motor at startup before reading the initial position.

**Low-level dependencies**

None.

**Vendor library**

``pylablib.devices.Thorlabs.KinesisMotor`` (lazy via the
``imswitch.imcontrol.model.interfaces.kinesisrotator`` wrapper).  On
import failure or hardware connect failure, the manager substitutes
``MockKinesisMotor`` — a stateful in-process mock that supports the
same ``move_to`` / ``move_by`` / ``get_position`` API.

**Source**

`KinesisRotatorManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/rotators/KinesisRotatorManager.py>`_


ElliptecRotatorManager
======================

Thorlabs ELL14 / ELL14K Elliptec rotation mounts.

The Elliptec wire protocol is **multidrop** — multiple rotators share
one COM port distinguished by ``address``.  The manager uses a
refcounted, per-port shared-bus singleton so multiple
``ElliptecRotatorManager`` instances on the same ``port`` cooperate
safely.  When the last instance is finalized the bus is closed.

**Setup JSON (two rotators on one port)**

.. code-block:: json

    "rotators": {
        "HWP": {
            "managerName": "ElliptecRotatorManager",
            "managerProperties": {
                "port": "COM20",
                "address": 0,
                "homeOnInit": false
            }
        },
        "QWP": {
            "managerName": "ElliptecRotatorManager",
            "managerProperties": {
                "port": "COM20",
                "address": 1,
                "homeOnInit": false
            }
        }
    }

**managerProperties**

.. list-table::
   :widths: 18 18 14 50
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``port``
     - str
     - **required**
     - Serial port (e.g. ``"COM20"`` on Windows, ``"/dev/ttyUSB0"`` on Linux).
   * - ``address``
     - int
     - **required**
     - Elliptec bus address.  Each device on the same ``port`` MUST have a unique address.
   * - ``scale``
     - str or float
     - ``"stage"``
     - ``pylablib`` scale parameter.  Leave at ``"stage"`` unless you're driving a non-standard device.
   * - ``homeOnInit``
     - bool
     - ``false``
     - If true, home this rotator at startup (uses the bus lock; safe to set on multiple devices).

**Low-level dependencies**

None.  The shared bus is internal to the manager — no
``lowLevelManagers`` entry needed.

**Vendor library**

``pylablib.devices.Thorlabs.ElliptecMotor`` (lazy via the
``imswitch.imcontrol.model.interfaces.elliptecbus`` wrapper).  If the
real bus fails to open, the manager swaps in
``MockElliptecBus`` + ``MockElliptecMotor`` — a stateful per-address
simulator suitable for headless / CI use.

**Gotchas**

* Per-address moves are serialized through the bus lock — concurrent
  ``move_abs`` calls on different addresses will queue, not parallelize.
* Elliptec NAKs under bus contention; the manager retries up to 5 times
  per move before raising.  If you see frequent retries in the logs,
  reduce concurrent traffic on the bus.

**Source**

`ElliptecRotatorManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/rotators/ElliptecRotatorManager.py>`_
