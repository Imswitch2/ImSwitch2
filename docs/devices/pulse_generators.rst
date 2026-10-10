*******************************
Pulse generators - reference
*******************************

A pulse generator plays a table of steps -- a duration and the state of every
digital channel -- repeated a number of times: a Teensy or Arduino running
the ImSwitch firmware, a Swabian Pulse Streamer, or a pulse-generator plugin.
Lasers switch through one (``PulseGeneratorLaserManager``, :doc:`lasers`),
and a scan program's digital part runs on one (``PulseGeneratorScanEngine``,
:doc:`scan_engines`).

The contract every generator implements is
``imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager``; see
:doc:`/how-to/add-pulse-generator-backend` to write one.


How pulse generators are configured
===================================

``pulseGenerators`` maps a generator name to a ``PulseGeneratorInfo``
(``imswitch.imcontrol.model.SetupInfo``): a ``managerName`` and the
generator's ``managerProperties``. ``managerName`` is resolved through the
device plugin registry (see :doc:`plugins`), kind ``pulse_generator``, so a
setup can have several generators and a generator joins by its plugin.

.. code-block:: json

    "pulseGenerators": {
        "widefield": {
            "managerName": "TeensyPulseManager",
            "managerProperties": {"port": "COM7"}
        },
        "streamer": {
            "managerName": "PulseStreamerManager",
            "managerProperties": {"ipAddress": "192.168.1.100"}
        }
    },
    "scanEngines": {
        "sequence": {
            "managerName": "PulseGeneratorScanEngine",
            "managerProperties": {"pulseGenerator": "widefield"}
        }
    }

* A consumer names the generator it drives: a ``PulseGeneratorScanEngine``'s
  or a ``PulseGeneratorLaserManager``'s ``managerProperties.pulseGenerator``.
  A name the setup does not have stops the setup from loading, with the
  names it does have.
* The ``teensyPulse`` section still builds the one Teensy it always did,
  named ``pulseGeneratorManager``: a consumer that names no generator drives
  it, and ``pulseGenerators`` cannot reuse that name.
* Each generator is closed when ImSwitch shuts down.


TeensyPulseManager
==================

A Teensy or Arduino running the ImSwitch v4 pulse-generator firmware (with a
v3 fallback); :doc:`/how-to/wire-teensy` wires one. It is registered as
``TeensyPulseManager`` and ``builtin.teensy-pulse``. Its ``managerProperties``
are the keys of the ``teensyPulse`` section, with the same defaults; a key it
does not take is refused by name.

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``port``
     - str or null
     - Serial port, e.g. ``COM7`` or ``/dev/ttyACM0``.
   * - ``baud``
     - int
     - Baud rate.
   * - ``pinMap``
     - dict
     - Name-to-channel mapping, a convenience for scripts.
   * - ``useMockOnFailure``
     - bool
     - Fall back to an in-process mock when the port cannot be opened.
   * - ``mockNChannels``
     - int
     - Channels the mock declares when the device is unavailable.
   * - ``mockMinPulseUs``
     - int
     - Shortest pulse (µs) the mock declares when the device is unavailable.
   * - ``mockMaxSteps``
     - int
     - Steps the mock holds when the device is unavailable.


PulseStreamerManager
====================

A Swabian Instruments Pulse Streamer 8/2 on the network, through the
``pulsestreamer`` library (``pip install pulsestreamer``); without it the
manager refuses every operation. It is registered as ``PulseStreamerManager``
and ``builtin.pulse-streamer``. Before ``pulseGenerators`` it was not built at
all: the ``pulseStreamer`` section has no effect.

**managerProperties**

.. list-table::
   :widths: 25 15 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``ipAddress``
     - str or null
     - IP address of the Pulse Streamer on the network.
