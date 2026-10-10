************************************
Step-engine protocol IMSWITCH_STEP/1
************************************

This is the serial protocol a board implements to run ImSwitch scan programs
on its own: a TriggerScope with new firmware, a Teensy, an Arduino, an ESP32,
any microcontroller with DACs and digital lines. A board that speaks it needs
no Python: ImSwitch's ``StepProtocolScanEngine`` (:doc:`scan_engines`) asks
it what it can do, compiles each scan program into its terms, uploads it and
follows the run.

The protocol is derived from the scan model, not from any firmware's
routines. A board does not know about scan types; it runs **loops**,
**drives**, **events** and **waits**, and nothing else. Everything that needs
judgement -- units, polarity, timing policies, what may be refused -- stays on
the host.

The reference implementation is the simulated board,
``imswitch/imcontrol/model/managers/scan_engines/step_board.py``, running
``step_executor.py``; the codec both ends share is ``step_protocol.py``. A
firmware is conformant when it does what they do. The design is
``docs/design/plans/scan-engines-and-expert-scan.md`` §5.2.


Framing and numbers
===================

- Lines are ASCII, ended by ``\n`` (a ``\r`` before it is ignored); fields
  are separated by commas.
- **Times** are whole **ticks** of the board's timing grid (``grid_ns``).
- **Values** are decimal numbers in the board's units: volts for a DAC; the
  electrical level ``0`` or ``1`` for a line. The host may send any number
  ``strtod`` reads (``2.5``, ``-0.125``, ``1e-05``).
- **Integers:** counts, durations and indices are unsigned 32-bit; event
  times, relative to their anchor, are signed 32-bit; the run clock in
  messages and logs is unsigned 64-bit. A number that does not fit is
  refused (``ERR,range``), never wrapped.
- ``-`` is "none"; ``;`` separates the items of a list; loops are numbered
  from 0, outermost first.
- A **text** (a reason) is always the last field and may contain commas.
- **Channels** are ``dac<i>`` and ``ttl<j>``, as the board declares them, and
  ``time<k>``: a channel that drives nothing. A drive on it is **timing
  only** -- the board moves it in its head, waits for its move, settle and
  return, and writes nothing. A leader runs the devices of the boards that
  follow it that way (see :doc:`scan_engines`).


Commands
========

The host sends one command at a time and reads its answer, except while a
program runs, when the board reports on its own (`While it runs`_).

``*IDN?``
    ``IMSWITCH_STEP/1,<board>,<firmware>``. A host refuses a board that
    answers another protocol or version, and says so.

``*CAPS?``
    One ``CAPS`` line per capability, then ``CAPS,END``:

    .. list-table::
       :widths: 40 60
       :header-rows: 1

       * - Line
         - Meaning
       * - ``CAPS,grid_ns,<n>``
         - The timing grid: one tick is ``n`` ns. Without it the board runs
           nothing.
       * - ``CAPS,dac,<i>,<minV>,<maxV>``
         - DAC ``i`` exists and outputs ``minV`` to ``maxV``.
       * - ``CAPS,ttl,<n>``
         - Lines ``ttl0`` .. ``ttl<n-1>`` exist.
       * - ``CAPS,max_steps,<n>``
         - The most steps a program may have.
       * - ``CAPS,trigger_in,<0|1>``
         - Whether it has a trigger input to wait on.
       * - ``CAPS,trigger_latency_ticks,<n>``
         - From a trigger's arrival to the release. Not declared, the host
           refuses trigger waits as unknown rather than guess it.
       * - ``CAPS,trigger_min_ticks,<n>``
         - The shortest pulse the trigger input takes (to follow another
           engine step by step).
       * - ``CAPS,clock_ppm,<x>``
         - The clock's accuracy, in parts per million (to lead or follow).

    A capability the board does not declare is unknown, and a program that
    needs it is refused as such. A host ignores ``CAPS`` keys it does not
    know.

``PROGRAM,BEGIN`` ... ``PROGRAM,END``
    Uploads a program (`The program`_); the board answers once, after
    ``PROGRAM,END``: ``OK,PROGRAM,<steps>``, or ``ERR`` with the reason. It
    replaces the previous program. Refused while a program runs.

``RUN``
    ``OK,RUN``, and the program starts; or ``ERR,state,...`` (no program,
    already running).

``STOP``
    ``OK,STOP``. A running program stops at its next step, and at once while
    it waits (`Stop, failure and the safe state`_). Idle, it does nothing.

``LOG?``
    What the last run executed (`The log`_), then ``LOG,END``. A board that
    keeps no log answers ``ERR,capacity,...``.

Anything else is ``ERR,syntax,...``.


The program
===========

One line per item. Order matters only as stated: a loop is declared before a
line names it, and a ``DRIVE`` before its ``WEIGHT`` and ``TABLE`` lines.

.. list-table::
   :widths: 45 55
   :header-rows: 1

   * - Line
     - Meaning
   * - ``LOOP,<count>,<order>,<parity>``
     - A loop, outermost first. ``order`` is ``forward``, ``reverse`` or
       ``serpentine``; ``parity`` lists the loops a serpentine alternates
       with (``0;1``), or ``-``.
   * - ``PHASE,<loop>``
     - The loop whose counter is the **phase** that events may be limited
       to (``-``: none).
   * - ``STEP,<step>,<return>``
     - Every innermost step lasts ``step`` ticks; after the last step of
       every pass of the innermost loop the drives move for ``return``
       ticks.
   * - ``DRIVE,<dac>,<offset>,<move>,<settle>,<placement>,<origin>,<restore>``
     - A DAC that positions a device. ``move`` and ``settle`` are ticks;
       ``placement`` is ``in-step`` or ``between-steps``; ``origin`` is
       ``absolute`` or ``captured``; ``restore`` (where it goes after the
       run) is ``captured``, ``first``, ``park`` or ``hold``.
   * - ``WEIGHT,<dac>,<loop>,<volts>``
     - Volts per count of that loop.
   * - ``TABLE,<dac>,<loop>,<v0;v1;...>``
     - Volts per count of that loop, given explicitly.
   * - ``EVENT,<ttl>,<start>,<end>,<anchor>,<loop>,<phases>,<on>,<off>``
     - The line is at ``on`` from ``start`` to ``end`` ticks after its
       anchor, and at ``off`` otherwise. ``anchor`` is ``step`` (every
       step), ``pass-start`` (the first step of every pass of ``loop``) or
       ``pass-end`` (the end of the last step of every pass of ``loop``;
       times are then <= 0). ``phases`` (``0;2``, or ``-`` for all) limits
       it to those phase counters.
   * - ``WAIT,<at>,<trigger>,<interval>,<timeout>,<on_timeout>``
     - A wait before the run (``at`` = ``start``), before every step
       (``step``), or before every iteration of a loop (its index).
       ``trigger`` is ``time`` (wait ``interval`` ticks) or ``trigger`` (the
       trigger input, at most ``timeout`` ticks, ``-`` for none);
       ``on_timeout`` is ``fail`` or ``continue``.
   * - ``PREPARE,<channel>,<ticks>``
     - After every trigger release, the device on that channel needs this
       long before the segment starts.
   * - ``CAPTURED,<dac>,<volts>``
     - Where the device stood before the run (for ``captured``).
   * - ``PARK,<dac>,<volts>``
     - Where it parks after the run (for ``park``).
   * - ``SAFE,<ttl>,<0|1>``
     - The line's safe level: where a stop, a failure and the end of a run
       leave it.

A board refuses (``ERR,channel``) a channel it does not have, an event or a
safe level on a DAC or a ``time`` channel, a drive, captured or park position
on a line;
(``ERR,capacity``) more steps than ``max_steps``, or a trigger wait without a
trigger input; (``ERR,range``) a number that does not fit; (``ERR,syntax``)
anything else that is not this table.


What the board does
===================

**The order of the steps.** The board visits every combination of the loop
counters, the innermost changing fastest. A ``reverse`` loop counts down. A
``serpentine`` loop counts down on every other pass of its parity loops: on
the passes where their **flat index** is odd -- the index of their counters
taken outermost first (``flat = flat × count + ordinal``), not their sum.

**Positions.** A drive's value at a step is ``offset + Σ weight × counter``
over its ``WEIGHT`` loops, plus its ``TABLE`` entry at the counter of the
table's loop, plus its ``CAPTURED`` value if its origin is ``captured``.
Before the first step (and before any wait at the start) every drive is set
to its first value.

**A step** starts at the current tick. Its events go on and off at their
times. Then:

- inside a pass, an ``in-step`` drive that changes is written at
  ``step - move - settle`` ticks into the step, and the step ends after
  ``step`` ticks; a ``between-steps`` drive that changes is written when the
  step ends, and the board waits the longest ``move + settle`` of those
  drives;
- at the end of a pass of the innermost loop, after ``step`` ticks, every
  drive is written its value for the next step (after the last step, its
  ``restore`` position), and the board waits ``return`` ticks.

**Waits** come before a step, in this order: the start wait (first step
only), the loop waits outermost first (a loop's wait comes before every
iteration of that loop, the first included), then the step wait. A ``time``
wait advances the clock by its interval. A ``trigger`` wait releases on the
first arrival at or after the wait began, plus the trigger latency; with a
timeout and no arrival, it fails the run or continues at the timeout, as
``on_timeout`` says; with neither, it waits until it is stopped.

**A release** starts a new **segment**: its ticks count from 0 again. Then
comes the preparation: every drive whose value is not the coming step's is
written, and the board waits the longest of their ``move + settle`` and every
``PREPARE`` time. Nothing timed happens during it.

**After the run** every line goes to its safe level; DACs hold.


While it runs
=============

.. list-table::
   :widths: 40 60
   :header-rows: 1

   * - Message
     - When
   * - ``STARTED``
     - The run has begun.
   * - ``STEP,<i>,<n>``
     - Before step ``i`` (from 0) of ``n``. A board may leave some out.
   * - ``RELEASE,<segment>,<clock>``
     - A trigger wait released; ``clock`` is ticks since ``RUN``.
   * - ``PREPARED,<segment>,<clock>``
     - The preparation after it is over; the segment starts.
   * - ``DONE,<clock>``
     - The run is over.
   * - ``STOPPED,<step>,<clock>``
     - It was stopped before step ``step``.
   * - ``FAILED,<step>,<reason>``
     - It failed at step ``step``.

``DONE``, ``STOPPED`` and ``FAILED`` end a run; nothing is sent after them
until the next command.


Stop, failure and the safe state
================================

``STOP`` is honoured before the next step begins, and at once during a wait.
On a stop and on a failure the board goes to its **safe state**: every line
at its ``SAFE`` level, every DAC holding where it is -- illumination off, no
window open, positions held. The host sends the electrical levels, so a
line's polarity lives on the host: the board never assumes that low is off.


The log
=======

``LOG?`` answers one ``LOG`` line per thing the last run did, in order, then
``LOG,END``. Ticks are within the segment; the clock is ticks since ``RUN``.

.. list-table::
   :widths: 50 50
   :header-rows: 1

   * - Line
     - Meaning
   * - ``LOG,DAC,<segment>,<tick>,<dac>,<volts>``
     - A DAC was written (only when its value changes).
   * - ``LOG,ON,<segment>,<tick>,<ttl>`` and ``LOG,OFF,...``
     - A line went to its on, or off, level.
   * - ``LOG,RELEASE,<segment>,<clock>,<arrival>``
     - A wait released: the trigger's arrival, or ``-`` on a timeout.
   * - ``LOG,FINISH,<segment>,<tick>,<clock>``
     - The run finished.
   * - ``LOG,SAFE,<clock>``
     - It went to the safe state (a stop).
   * - ``LOG,FAILED,<clock>,<reason>``
     - It failed.

The host compares this log with what the program means -- the
interpreter's realized timeline -- every DAC value and every line interval,
segment by segment; and it checks the releases against the trigger arrivals
it chose: none before its arrival, each within the declared latency.


Testing a firmware
==================

``imswitch/imcontrol/_test/unit/test_scan_engine_conformance.py`` runs its
programs through ``StepProtocolScanEngine`` on the simulated board and
compares the logs with the interpreter; ``test_step_protocol.py`` checks the
answers to what a board must refuse. A firmware passes when, run in place of
the simulated board, its logs match the same way. Trigger tests need the
arrivals on the board's trigger input; the simulated board takes them from
the test.


An example
==========

Two cycles of three planes on one piezo (``dac0``, 0.5 V per plane, 0.25 V
per cycle), a laser pulse and an active-low camera exposure every step:

.. code-block:: text

    > *IDN?
    < IMSWITCH_STEP/1,TriggerScope,4.0
    > *CAPS?
    < CAPS,grid_ns,1000
    < CAPS,dac,0,0.0,10.0
    < CAPS,ttl,16
    < CAPS,max_steps,65536
    < CAPS,trigger_in,1
    < CAPS,trigger_latency_ticks,2
    < CAPS,END
    > PROGRAM,BEGIN
    > LOOP,2,forward,-
    > LOOP,3,forward,-
    > PHASE,-
    > STEP,20000,0
    > DRIVE,dac0,0.0,1000,1000,in-step,absolute,first
    > WEIGHT,dac0,0,0.25
    > WEIGHT,dac0,1,0.5
    > EVENT,ttl0,0,2000,step,-,-,1,0
    > EVENT,ttl2,9000,15000,step,-,-,0,1
    > SAFE,ttl0,0
    > SAFE,ttl2,1
    > PROGRAM,END
    < OK,PROGRAM,6
    > RUN
    < OK,RUN
    < STARTED
    < STEP,0,6
    ...
    < DONE,120000
    > LOG?
    < LOG,DAC,0,0,dac0,0.0
    < LOG,ON,0,0,ttl0
    < LOG,OFF,0,2000,ttl0
    < LOG,ON,0,9000,ttl2
    < LOG,OFF,0,15000,ttl2
    < LOG,DAC,0,18000,dac0,0.5
    ...
    < LOG,DAC,0,60000,dac0,0.25
    ...
    < LOG,FINISH,0,120000,120000
    < LOG,END

The plane's move is written 18000 ticks into the first step (20000 − 1000 −
1000: ``in-step``, before its move and settle); the cycle's, at the end of the
third step, the end of a pass, after which ``return`` is 0 ticks.
