***********************************
Write a new pulse-generator backend
***********************************

This guide shows you how to implement a new backend under the
``PulseGeneratorManager`` abstraction — for an NI digital-out card,
an FPGA, a different microcontroller, etc.

The contract is small enough to fit in your head; most of the work is
choosing the right driver shape and writing a mock that lets the
manager be tested without hardware.

Before you start, skim ``docs/design/ARCHITECTURE.md`` for the
"Pulse Generator Subsystem" section so the abstraction-level decisions
already made are clear.


When to add a new backend
=========================

You want a new ``PulseGeneratorManager`` if the hardware can:

* drive a small-to-medium number of digital channels HIGH/LOW with
  microsecond-or-better timing, and
* either step through a pre-loaded sequence or be commanded one
  transition at a time fast enough for your use case.

You probably do **not** want one for general-purpose analog waveform
output, image-sensor exposure control, or anything where the host
needs per-sample synchronous feedback.  Those belong to NI-DAQ,
detector, or scan managers respectively.


The interface
=============

The abstract base lives at
``imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager``.
You implement:

============================  =======================================================
Capability properties         Honest, runtime values consumers can inspect
``jitter_ns``                 Worst-case transition jitter in ns
``min_pulse_width_ns``        Shortest step duration the backend can resolve
``n_digital_channels``        Logical channel count (0..n-1)
``supports_hw_trigger_in``    Can ``run()`` wait for an external trigger?
``supports_analog``           Does ``setAnalog()`` work?
============================  =======================================================

============================  ================================================
Required methods              Contract
``setDigital(ch, enable)``    Constant pin level.  Raise
                              ``PulseGeneratorError`` if a sequence is running.
``program_sequence(steps)``   Upload a ``list[PulseStep]``.  Validate channel
                              ranges and durations against capabilities;
                              raise ``ValueError`` on contract violation.
``run(n_reps, blocking)``     Execute the buffered sequence.  Emit
                              ``sigSequenceStarted`` immediately, then
                              ``sigSequenceDone`` on completion.  Honour
                              ``blocking=False`` via a worker thread.
``stop()``                    Abort.  Safe to call when idle.  Emit
                              ``sigSequenceDone``.
============================  ================================================

You also get a default ``snap()`` for free (program a 2-step sequence,
run once blocking) — override only if your backend has a faster
one-shot path.


Worked example: skeleton of a new backend
=========================================

Say you're adding an NI digital-out backend.  Create
``imswitch/imcontrol/model/managers/pulsegen/NiDigitalPulseManager.py``::

    from typing import List

    from imswitch.imcommon.model import initLogger

    from .PulseGeneratorManager import (
        PulseGeneratorError,
        PulseGeneratorManager,
        PulseStep,
    )


    class NiDigitalPulseManager(PulseGeneratorManager):
        """PulseGeneratorManager backed by an NI digital-out card."""

        def __init__(self, info):
            super().__init__()
            self._logger = initLogger(self)
            self._info = info
            self._driver = self._open_driver(info)   # your I/O layer
            self._running = False
            # ...

        # --- capabilities ----------------------------------------------

        @property
        def jitter_ns(self) -> int:
            return 10       # ns — depends on the card

        @property
        def min_pulse_width_ns(self) -> int:
            return 100

        @property
        def n_digital_channels(self) -> int:
            return self._driver.n_lines

        @property
        def supports_hw_trigger_in(self) -> bool:
            return True

        @property
        def supports_analog(self) -> bool:
            return False

        # --- required methods ------------------------------------------

        def setDigital(self, channel, enable):
            if self._running:
                raise PulseGeneratorError('sequence running; call stop() first')
            self._driver.write_line(channel, enable)

        def program_sequence(self, steps: List[PulseStep]):
            if self._running:
                raise PulseGeneratorError('sequence running')
            if not steps:
                raise ValueError('empty sequence')
            # Validate, transform into card-native format, upload.
            ...

        def run(self, n_reps=1, blocking=False):
            ...                                       # see TeensyPulseManager.run()

        def stop(self):
            ...

For a complete reference implementation see
``TeensyPulseManager.py``.  Its threading model (worker-thread for
non-blocking ``run``, sigSequenceDone emission) is the canonical
pattern.


Step-by-step
============

1. **Pick a driver shape.**

   The Teensy backend separates a thin I/O wrapper
   (``TeensyPulseDriver`` in ``imswitch/imcontrol/model/interfaces/teensypulse.py``)
   from the manager.  If your backend involves any external library
   (NI-DAQmx, vendor SDK, …) put it behind a driver class so the
   manager can stay pure logic.

2. **Write an in-process mock with the same public API.**

   ``MockTeensyPulseDriver`` mirrors the real driver's public surface
   and exposes a virtual-time ``timeline`` for tests.  Tests assert
   against the timeline; no real sleeps, no hardware.  Worth the
   investment — it lets you ship the manager green even before the
   hardware arrives.

3. **Implement the abstracts.**  Use ``TeensyPulseManager`` as a
   template.  Pay particular attention to:

   * **Capability properties must reflect the *connected* backend**,
     not just hardcoded values.  A v3 Teensy reports 3 channels and
     1 µs floor; a v4 Teensy reports whatever ``*IDN?`` says.  Your
     backend should similarly query the device once at startup.

   * **``run(blocking=False)`` requires a worker thread.**  Backends
     whose underlying API is synchronous (like Teensy's serial
     wait-for-DONE) must spawn a thread; the caller must never block
     on a non-blocking run.  Emit ``sigSequenceDone`` from the worker
     when the sequence completes — sequences that complete via
     ``stop()`` should also emit Done (the ABC contract doesn't
     distinguish; both terminal events fire Done).

   * **``setDigital`` while a sequence is running must raise.**
     Document the lock convention; the manager-level ``_running_lock``
     pattern in TeensyPulseManager is good enough for most backends.

4. **Add a config dataclass to** ``SetupInfo.py``.

   Frozen dataclass with ``port``/``connection`` fields plus any
   mock-mode knobs.  Mirror ``TeensyPulseInfo``.  Add an
   ``Optional[...]`` field on ``SetupInfo`` itself (default
   ``None``) so setups without your backend continue to load
   unchanged.

5. **Wire** ``MasterController``.

   Construct the manager iff ``setupInfo.<your_field>`` is set, expose
   via ``lowLevelManagers['pulseGeneratorManager']``.  Only one
   pulse generator can be active per setup at a time — this is by
   design and matches how consumers (laser managers, snap actions)
   look up the dependency.

6. **Add contract tests.**

   The fastest path is to subclass the helper in
   ``test_pulsegenerator_base.py``: it has a ``NullPulseGenerator``
   you can take inspiration from, plus 18 contract tests that every
   backend should pass.  Replace ``NullPulseGenerator`` with your
   mock and re-run.


Common pitfalls
===============

**Hardcoding capabilities.**  The whole point of the ABC is honest
capability introspection.  If your card has a 1 ns clock, say
``jitter_ns = 1``.  If the timing budget varies (e.g. you're a
microcontroller doing USB serial framing), report the worst case.

**Treating** ``stop()`` **as a "wait for completion" hook.**  It must
be safe to call when idle, and it aborts when running.  Don't make
it the only way to wait for ``sigSequenceDone``; consumers can also
just connect to the signal.

**Sharing state between** ``wait_done`` **and** ``stop`` **without
locks.**  This bit the Teensy driver during development — concurrent
read and write on the serial buffer raced.  If your driver has any
similar shared state (a USB read thread, a card-level command queue),
serialize at the driver level and let ``stop()`` interleave safely.


Cross-references
================

- :doc:`wire-teensy` — concrete recipe for the existing Teensy backend.
- :doc:`/adding-device-support` — generic guide for adding any device manager.
- ``docs/design/ARCHITECTURE.md`` — full subsystem context.
