*********************************************
Wire a Teensy pulse generator into your setup
*********************************************

This guide walks you through adding a Teensy / Arduino pulse generator
(running the ImSwitch v4 firmware) to an existing ImSwitch2 setup file
and driving one or more lasers from it.

The whole pipeline is hardware-free testable: if the Teensy isn't
plugged in (or you don't have one yet), the manager falls back to an
in-process mock so you can develop, configure, and even run scans
end-to-end before any hardware arrives.


Prerequisites
=============

- An ImSwitch2 checkout you can edit.
- (Optional, for real hardware) A Teensy 4.1 with
  ``arduino_code_teensy4p1_v4.txt`` flashed.  The firmware sketch
  lives in the WidefieldStarss repo under ``teensy/``.
- (Optional) ``pyserial`` installed.  Required only when the real
  driver opens a port; the mock fallback works without it.


Step 1 — Flash and verify the firmware (skip if mock-only)
==========================================================

If you have hardware, flash the v4 firmware and walk the smoke-test
checklist from the WidefieldStarss repo
(``teensy/v4_smoke_test_checklist.md``).  At minimum complete steps
0–2 (boot banner, ``*IDN?`` handshake, single-pin control) before
moving on — these confirm the wire protocol works.

If you don't have hardware, skip this step.  The rest of the guide
works against the in-process mock driver.


Step 2 — Add the ``teensyPulse`` block to your setup file
=========================================================

Open your hardware setup JSON (under
``~/ImSwitchConfig/imcontrol_setups/<your_setup>.json`` once installed,
or ``imswitch/_data/user_defaults/imcontrol_setups/`` in the source
tree) and add a top-level ``teensyPulse`` entry.  The shipped
``imswitch/_data/user_defaults/imcontrol_setups/example_kiralux_teensy.json``
already has one and makes a good starting point::

    {
      "detectors": { ... },
      "lasers": { ... },
      "positioners": { ... },
      "teensyPulse": {
          "port": "/dev/ttyACM0",
          "baud": 115200,
          "pinMap": {
              "laser488": 2,
              "laser405": 3,
              "camera":   4
          },
          "useMockOnFailure": true
      }
    }

Field reference (see ``TeensyPulseInfo`` in ``imswitch/imcontrol/model/SetupInfo.py``):

* ``port`` — serial port (``COM7`` on Windows, ``/dev/ttyACM0`` or
  ``/dev/cu.usbmodemNNNNN`` on macOS/Linux).  Leaving it ``null`` or
  empty skips the real driver and uses the mock backend, but only while
  ``useMockOnFailure`` is ``true``; with ``useMockOnFailure: false`` an
  empty port stops start-up with a ``RuntimeError``.  To run without a
  pulse generator at all, remove the whole ``teensyPulse`` block.
* ``baud`` — defaults to 115200, matches the firmware.
* ``pinMap`` — optional name→channel mapping.  Purely a passthrough
  for scripts; not enforced by the manager.
* ``useMockOnFailure`` — when ``true``, falls back to
  ``MockTeensyPulseDriver`` if the port can't be opened.  Set to
  ``false`` to make a missing device a startup error.
* ``mockNChannels`` / ``mockMinPulseUs`` / ``mockMaxSteps`` — only
  used in mock mode (defaults: 16, 1, 256).

That's the entire integration on the hardware side.  Restart ImSwitch2
and check the logs — you should see one of:

.. code-block:: text

    Connected to Teensy on /dev/ttyACM0 (v4.0, n_channels=16)

or, with no hardware/no port:

.. code-block:: text

    No Teensy port configured; using MockTeensyPulseDriver


Step 3 — Drive a laser through the pulse generator
==================================================

Pick ``PulseGeneratorLaserManager`` for the laser.  It drives whichever
``PulseGeneratorManager`` backend the setup provides; today that is
always the Teensy (``TeensyPulseManager``), the only backend built from a
setup file.  ``PulseStreamerManager`` implements the same interface but
is not constructed from a ``pulseStreamer`` block yet.  The older
``PulseStreamerLaserManager`` looks for a ``pulseStreamerManager``
low-level manager that ImSwitch2 no longer provides, so it always runs in
mock mode and drives nothing.

In your setup file, change the laser's ``managerName`` and put its
pulse-generator channel under ``digitalLine``::

    "lasers": {
        "488": {
            "managerName": "PulseGeneratorLaserManager",
            "managerProperties": {},
            "digitalLine": 2,
            "analogChannel": null,
            "valueRangeMin": 0,
            "valueRangeMax": 1,
            "wavelength": 488
        }
    }

* ``digitalLine`` — the pulse-generator channel that drives the laser
  HIGH/LOW.  Must be within ``[0, n_digital_channels)`` for the
  backend.
* ``analogChannel`` — leave ``null`` for binary on/off lasers.  Set to
  an analog channel index if your backend supports analog (Teensy v4
  currently does not; the manager downgrades to binary automatically
  if you set one anyway, with a warning).

Restart ImSwitch2.  The laser widget's enable button now drives the
configured pulse-generator channel.


Step 4 — Sanity-check end-to-end against the mock
=================================================

Without leaving your dev machine:

.. code-block:: python

    from imswitch.imcontrol.model.SetupInfo import TeensyPulseInfo
    from imswitch.imcontrol.model.managers.pulsegen import (
        TeensyPulseManager,
        PulseStep,
    )

    # No port → falls back to MockTeensyPulseDriver
    m = TeensyPulseManager(TeensyPulseInfo(port=None))

    # Constant pin
    m.setDigital(2, True)
    assert m.driver.get_pin_state(2) is True

    # Snap (HIGH then LOW)
    m.snap(channels=[2, 4], width_ns=50_000)
    m.driver.assert_pulse(channel=2, start_ns=0, duration_ns=50_000)

    # Arbitrary sequence
    m.program_sequence([
        PulseStep(duration_ns=100_000, channel_states={2: True, 4: True}),
        PulseStep(duration_ns=100_000, channel_states={3: True}),
    ])
    m.run(n_reps=2, blocking=True)

    # Inspect what the firmware *would* have done:
    for e in m.driver.timeline:
        print(f't={e.virtual_time_ns:>8}ns  ch={e.channel}  {"HI" if e.level else "LO"}')

The mock advances a virtual clock instead of sleeping, so this whole
script runs in milliseconds while accurately modelling the pulse
timeline.


Step 5 — Verify with real hardware
==================================

Plug in the Teensy, set ``port`` to the actual device, restart
ImSwitch2.  Confirm the log line reads
``Connected to Teensy on <port> (v4.0, ...)``.

Then walk the rest of the smoke-test checklist
(``v4_smoke_test_checklist.md``, steps 3–8) with a multimeter or
oscilloscope on the configured pins.


Troubleshooting
===============

**"No Teensy port configured" even though I set ``port``.**
Check the JSON: the value must be a non-empty string (``"COM7"``).
``null``, an empty string or ``0`` take the mock path.  Any other number
is handed to the serial driver and fails with the "Failed to open Teensy"
message below instead.

**"Failed to open Teensy on <port>" with a SerialException.**
Either the port doesn't exist (typo, wrong device), is held by
another process (Arduino IDE Serial Monitor — close it), or
``pyserial`` isn't installed.  With ``useMockOnFailure: true`` the
manager keeps booting in mock mode; check the log for the exact
exception.

**Driver connects but reports v3 capabilities.**
The firmware isn't v4 — most likely the older ``arduino_code_teensy4p1_v3.txt``
is flashed.  v3 mode is intentionally supported for backwards compat,
but lacks ``program_sequence`` / ``run`` / ``stop``.  Flash v4 to get
the full feature set.

**``program_sequence`` raises with "channel X outside [0, N)".**
Your setup's ``digitalLine`` or programmed channel exceeds the
backend's ``n_digital_channels``.  Either pick a channel within range
or — if you're in mock mode — bump ``mockNChannels`` in the setup
JSON.

**STOP latency feels long.**
The v4 firmware checks abort between steps, with long delays chunked
to 16 ms intervals.  STOP latency = at most one step duration, or
16 ms for sub-step granularity on long sequences.  This is by design;
documented in the firmware comments.


See also
========

- :doc:`add-pulse-generator-backend` — write a new backend for a different timing device.
- :doc:`/adding-device-support` — generic guide for adding any device manager.
- `Pulse generator subsystem <https://github.com/Imswitch2/ImSwitch2/blob/main/docs/design/ARCHITECTURE.md#pulse-generator-subsystem>`_
  in the repository's architecture note — design, including the v4 wire protocol.
