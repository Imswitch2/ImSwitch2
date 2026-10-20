*****************************************
TriggerScope step firmware
*****************************************

``firmware/triggerscope_step/`` is a firmware for the lab's TriggerScope 16
R4 that speaks the step-engine protocol, :doc:`step-engine-protocol`
(``IMSWITCH_STEP/1``). With it the board is a step engine like any other --
``StepProtocolScanEngine`` asks it what it can do, uploads a scan program
and follows the run -- and the six fixed routines of the old
``TriggerSwitch_0.1`` sketch, their ``PARAMETER`` uploads and the blind 50 ms
pauses between them are no longer needed. The old sketch is not changed and
can be flashed back at any time.

The firmware is in three parts:

``TriggerScopeStep/step_core.{h,cpp}``
    The protocol, the program and the executor, platform independent (C++11,
    no allocation after start-up). It mirrors the reference implementation,
    ``step_executor.py``'s walk, statement by statement, and adds the one
    thing the reference leaves out: time. The reference only logs what it
    would do at which tick; this also does it, when the clock says so.

``TriggerScopeStep/TriggerScopeStep.ino``
    The board: the LTC2668 DAC over SPI, the TTL lines, the trigger input,
    the USB serial link and the clock, ported from the old sketch without
    its scan routines. ``Adafruit_MCP23017.{h,cpp}`` are the expander driver
    the old sketch used, copied so the sketch folder is complete.

``host/host_board.cpp``
    The same core as a fake board on a PC, with a virtual clock, on stdin
    and stdout. The conformance programs run against it in the unit tests
    (``test_step_board_firmware_host.py``), so the executor is verified
    without a board; what the board adds is hardware and real time.


What the board declares
=======================

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - ``*CAPS?``
     - Why
   * - ``grid_ns 1000``
     - The Teensy's microsecond clock (``micros()``, extended to 64 bits).
   * - ``dac 0..15, -10 to 10 V``, ``dac_bits 16``
     - The LTC2668-16 at the ±10 V span the old sketch set on every channel.
       A DAC sets the nearest of its 65536 codes, and the log says the volts
       of that code.
   * - ``ttl 16``
     - Lines 0-8 are Teensy pins and switch within the grid. Lines 9-15 go
       through the MCP23017 expander over I2C and take far longer (a few
       hundred microseconds, unmeasured); keep timed pulses on 0-8.
   * - ``trigger_in 1``
     - Pin 0, released on a rising edge.
   * - ``trigger_latency_ticks``
     - **Not declared until measured.** The host then refuses trigger waits
       as unknown rather than guess the latency. See below.
   * - ``alive_ticks 500000``
     - A message at least every 0.5 s while a program runs (``STEP``,
       ``ALIVE``, ``WAITING``), so the host tells a hung board from a busy
       one within its reply timeout.
   * - ``max_steps 4294967295``
     - The counters are walked, not stored: any count the protocol's
       integers hold.

Not declared: ``clock_ppm`` (unmeasured) and ``trigger_min_ticks``.

The firmware's own limits (``step_core.h``; a program past one is refused
with ``ERR,capacity`` naming it): 8 loops, 16 DAC drives and 8 ``time``
channels, 4096 table entries in all, 32 events with 16 phases each, 16
waits, a program line of 16384 characters (a long ``TABLE``), 512 outputs
timed ahead of the clock (edges past their step). The log holds 8192
entries on a Teensy 4.x (in OCRAM); a run that writes more is run to its
end, and ``LOG?`` then answers ``ERR,capacity`` instead of a truncated log.


Building and flashing
=====================

The sketch is built in the Arduino IDE with Teensyduino, as the old one is.
Open ``firmware/triggerscope_step/TriggerScopeStep/TriggerScopeStep.ino``,
choose the same board as for ``TriggerSwitch_0.1`` (**Tools → Board**; the
old sketch's arrays only fit a Teensy 4.1, and the log is placed in OCRAM
on a 4.x), **USB Type: Serial**, and upload. No library needs installing:
``SPI`` and ``Wire`` come with Teensyduino and the expander driver is in the
folder.

The board sends nothing at start-up (the old banner is gone: the host's
first line from the board must be its answer to ``*IDN?``), and it no longer
reboots when the port is closed.

**Going back:** flash ``TriggerSwitch_0.1`` again. The two firmwares share
no command, so a setup written for one does not run on the other.


Testing it
==========

**On a PC, without the board** (any POSIX system with a C++ compiler):

.. code-block:: bash

    cd firmware/triggerscope_step/host && make
    python tools/step_board_conformance.py --simulated --grid-ns 1000   # the reference
    pytest imswitch/imcontrol/_test/unit/test_step_board_firmware_host.py

The test builds the fake board and runs the conformance programs, trigger
waits with chosen arrivals, a stop during a wait, an injected failure and
the refusals. The fake board takes two hooks outside the protocol, as the
simulated board takes its ``arrivals`` and ``fail_at_step``:
``#ARRIVE,<t1>;<t2>;...`` (ticks since ``RUN``) and ``#FAIL,<step>``.

**On the board**, with the firmware flashed and nothing wired to ``dac0``,
``ttl0``, ``ttl1`` and ``ttl2`` (the programs drive them; ``dac1`` plays a
power level):

.. code-block:: bash

    python tools/step_board_conformance.py --port COM5 --baudrate 115200 --reply-timeout 0.5

It prints ``PASS`` or ``FAIL`` per program with every difference, and exits
0 when all pass. The log it compares is what the board planned, tick by
tick; the outputs follow that plan on the board's clock. A scope on
``ttl0`` and ``dac0`` during the *raster with line steps* program shows the
pulses and the steps, and is the check that the hardware does what the log
says.

**The trigger latency**, before any program with trigger waits: wire a
line to the trigger input (``ttl3`` to ``TRIG 1``, say), upload a program
whose first event starts at tick 0 of the step and waits for the trigger
at every step, with no preparation, and measure on a scope from the
trigger's rising edge to the event's edge. Put the result, in microseconds,
into ``TRIGGER_LATENCY_TICKS`` at the top of the sketch and flash again;
``*CAPS?`` then declares it, and the host accepts trigger waits. With the
latency declared, ``--trigger --trigger-timeout <s>`` adds the trigger
program to the conformance run, with something pulsing the input.


Using it from ImSwitch
======================

The board is a ``scanEngines`` entry, and every device on it names its
channel (:doc:`scan_engines`):

.. code-block:: json

    "scanEngines": {
        "triggerscope": {"managerName": "StepProtocolScanEngine",
                         "managerProperties": {"port": "COM5", "baudrate": 115200,
                                               "replyTimeoutS": 0.5}}
    },
    "positioners": {
        "Galvo": {"managerName": "StepBoardPositionerManager",
                  "managerProperties": {"conversionFactor": 10.0, "minVolt": -5, "maxVolt": 5},
                  "analogChannel": "triggerscope/dac3", "axes": ["X"]}
    },
    "lasers": {
        "405": {"managerName": "StepBoardLaserManager", "ttlActiveLevel": "high",
                "digitalLine": "triggerscope/ttl0"}
    }

``baudrate`` is nominal on USB. The ``triggerScope`` section and
``TriggerScopeManager`` belong to the old firmware and are not used with
this one; a setup keeps one or the other.


Design notes
============

- **The log is the plan.** The executor computes every write and edge at
  its tick exactly as ``step_executor.py`` does and logs it then; a small
  queue plays the outputs on the clock in time order. So the log compares
  with the interpreter whatever the hardware's jitter, and the hardware is
  checked separately, with a scope.
- **A DAC's volts are the host's volts.** The nearest code is computed by
  the same operations in the same order as ``BoardCaps.dac_value``, ties to
  even, and printed with 17 digits, so what ``LOG?`` returns is the double
  the host computes.
- **``STOP`` within a millisecond.** A busy-wait returns every 1 ms to poll
  the link; a ``STOP`` is honoured before the next step, at once in a wait.
- **The outputs after a run** are the safe state the program declared:
  every line at its safe level, a DAC carrying a power level at its off
  volts, every other DAC holding.
