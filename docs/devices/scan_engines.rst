*************************
Scan engines - reference
*************************

A scan engine is what runs a scan: the board that plays the waveforms, steps
the positions and switches the lines. This page documents the engines
configured through the top-level ``"scanEngines"`` setup section, and the
NI-DAQ engine every setup has. The design behind them is
``docs/design/plans/scan-engines-and-expert-scan.md``.

For the complete top-level setup shape, see :doc:`/setupinfo-reference`.


How scan engines are configured
===============================

``scanEngines`` maps an engine name to a ``ScanEngineInfo``
(``imswitch.imcontrol.model.SetupInfo``): a ``managerName`` and the engine's
``managerProperties``. ``managerName`` is resolved through the device plugin
registry (see :doc:`plugins`), kind ``scan_engine``, so a board joins by its
plugin. A setup without the section runs on the NI-DAQ alone, built from its
``nidaq`` section.

.. code-block:: json

    "scanEngines": {
        "board": {
            "managerName": "SimulatedStepEngine",
            "managerProperties": {
                "timingGridS": 1e-05,
                "digitalLines": ["Sim/TTL0", "Sim/TTL1"]
            }
        }
    },
    "scan": { "engine": "board", "...": "..." }

* ``scan.engine`` names the engine a scan panel runs on; omitted, the NI-DAQ.
  A name the setup does not have stops the setup from loading, with the
  names it does have.
* **Channel ownership.** Which engine drives a device follows from its
  ``analogChannel`` and ``digitalLine``: each engine says which channels it
  owns. A channel two engines claim stops the setup from loading, naming
  both engines and the channel.
* **What an engine cannot drive is refused, not dropped.** The Advanced
  panel greys out a laser on a line its engine does not drive, with the
  reason, and refuses a scan that fires it. SimplePointScan does not offer
  it. On a scanner with no digital line at all (analog only), both panels
  grey out their TTL parts and say why.
* **Polarity.** A device's ``ttlActiveLevel`` (``"high"`` by default,
  ``"low"`` for an inverted input) maps logical on/off to the line's level on
  every path that drives the line: a scan, manual control, and cleanup.

Engines declare their capabilities -- timing grid, what they stream or step,
the waits they support, their trigger latency, how they stop. A capability an
engine does not declare is unknown, and a scan that needs it is refused as
unknown rather than run on a guess.


Several engines in one scan
===========================

A program can drive devices on more than one engine. One engine leads -- the
one ``scan.engine`` names -- and the others **follow it step by step**: a
follower's trigger input is wired to a line the leader drives, and the setup
says which with ``triggerInput`` on the follower's entry:

.. code-block:: json

    "scanEngines": {
        "lead":   {"managerName": "StepProtocolScanEngine",
                   "managerProperties": {"port": "COM5", "baudrate": 115200,
                                         "replyTimeoutS": 0.5}},
        "piezo":  {"managerName": "StepProtocolScanEngine", "triggerInput": "lead/ttl3",
                   "managerProperties": {"port": "COM6", "baudrate": 115200,
                                         "replyTimeoutS": 0.5}}
    },
    "scan": {"engine": "lead", "...": "..."}

* The leader runs the whole program -- the followers' devices as timing only:
  it waits for their moves and returns and writes nothing for them -- and
  **pulses the wired line at the start of every step**. A follower waits for
  the pulse before every step and runs its devices' part, shifted by its
  declared trigger latency so that it happens when the program says.
* **Why every step:** a shared start trigger is not a shared clock. Two
  clocks that agree once drift apart by their combined accuracy over the
  whole run; agreeing at every pulse, they drift only between two pulses.
  The report states that bound and refuses a program whose drift would move
  an edge off its tick (half the finer tick).
* **Waits** are the leader's: while it waits it sends no pulse, and every
  follower is held. A follower has no waits of its own.
* **One run:** **Stop** stops every engine; the first to fail stops the
  others, and the run fails with its reason.
* **What it needs, from the follower:** a trigger input it can wait on before
  every step, its trigger latency, the shortest pulse its input takes, and
  its clock's accuracy; **from the leader:** its clock's accuracy. Undeclared,
  each is unknown and the program is refused as such. An event earlier in
  the step than the follower's latency cannot happen on it and is refused,
  naming both times.
* **Who can lead:** step engines (the simulated step board, protocol boards).
  The NI-DAQ cannot lead yet -- its scan drives only devices' lines, and the
  pulse line is no device's -- nor can it, or a pulse generator, follow. A
  simulated engine only leads or follows simulated ones.
* A program that uses only the leader's devices is the leader's alone,
  exactly as without followers. A follower wired to any engine but the one
  ``scan.engine`` names is a setup error.


NidaqScanEngine
===============

The NI-DAQ seen as a scan engine; every setup has it, named ``nidaq``. Its
signals are the NI-DAQ manager's own, and the scans the Advanced and
SimplePointScan panels build run through it exactly as before. It reads no
``managerProperties``; what it drives is today's behaviour, written down:

* digital lines whose name contains ``Dev`` (the lines ``NidaqManager.runScan``
  drives);
* analog channels in NI-DAQmx form, ``<device>/ao<n>``.

It streams sampled waveforms on the board's 100 kHz timebase, emulates
stepped scans by sampling them, cannot wait for an external trigger, and
cannot stop a running scan iteration (the run stops after it). It is
registered as ``NidaqScanEngine`` and ``builtin.nidaq-scan-engine``.


SimulatedStepEngine
===================

A simulated step board: it executes scan programs the way step firmware
will, independently of the scan-program interpreter, and logs every DAC
write, line edge and trigger release. The conformance tests check its log
against the interpreter's timeline, and its trigger handling against arrival
times the tests choose. It is registered as ``SimulatedStepEngine`` and
``builtin.simulated-step-board``.

**managerProperties**

.. list-table::
   :widths: 25 12 18 45
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``timingGridS``
     - float
     - required
     - The board's timing grid, in seconds. Every time a program uses must
       be a whole number of it.
   * - ``analogChannels``
     - dict
     - ``{}``
     - The analog channels it drives, ``{channel: [minV, maxV]}``.
   * - ``digitalLines``
     - list
     - ``[]``
     - The digital lines it drives.
   * - ``triggerLatencyS``
     - float
     - not declared
     - From a trigger's arrival to the release, in seconds. Not declared,
       trigger waits are unknown and refused.
   * - ``triggerMinPulseS``
     - float
     - not declared
     - The shortest pulse its trigger input takes, in seconds (to follow
       another engine).
   * - ``clockAccuracyPpm``
     - float
     - not declared
     - Its clock's accuracy, in parts per million (to lead or follow).
   * - ``maxSteps``
     - int
     - not declared
     - The steps it can hold; a longer program is refused.


StepProtocolScanEngine
======================

A board that speaks the step-engine protocol, IMSWITCH_STEP/1
(:doc:`step-engine-protocol`): the TriggerScope once its firmware is
rewritten to it, or any microcontroller with DACs and digital lines. Such a
board joins without Python: the engine asks it ``*IDN?`` and ``*CAPS?``, and
what the board answers -- its timing grid, DACs and their ranges, lines,
capacity, trigger input and latency -- is what the engine can do. It is
registered as ``StepProtocolScanEngine`` and
``builtin.step-protocol-scan-engine``.

A scan program is compiled on the host into the board's terms and uploaded;
the board walks the loops, writes the DACs, switches the lines and waits for
its trigger input on its own, and reports its progress, each release and the
end. **Stop** stops it at its next step, and at once while it waits. ``LOG?``
returns what it executed, which the conformance tests compare with the
interpreter.

* **Channels** are named ``<engine>/dac<i>`` and ``<engine>/ttl<j>`` (the
  engine's name in ``scanEngines``); a device is wired to one by its
  ``analogChannel`` or ``digitalLine``. A channel the board does not declare
  is not the engine's, so a device on it is refused with the channel named.
  While the board does not answer, every channel of that form stays the
  engine's, and the next compile asks the board again.
* **Positions** go to the board in volts, from the positioner's
  ``conversionFactor`` (µm per volt). A positioner that declares none is
  refused as unknown; positions outside the board's DAC range, or the
  positioner's ``minVolt``/``maxVolt``, are refused with both ranges.
* **Polarity** stays on the host: each line's on and off levels come from the
  device's ``ttlActiveLevel``, and its off level is sent as its **safe**
  level, where a stop, a failure and the end of a run leave it. A DAC holds
  its position.
* **Refused, with the reason:** a sweep, a smooth transition (a board jumps),
  a power level inside a step (``level``), an intra-step move (``offset``),
  two devices on one line, a wait that does not idle, a trigger wait on a
  board without a trigger input, more steps than the board holds, a time off
  its grid, and a number the protocol's integers cannot hold. A board that
  answers another protocol version, or does not answer, is unknown, and so
  is everything it would run.

The **simulated board** (``"port": "simulated"``) is the board end of the
protocol in process: it runs uploaded programs on the step executor the
simulated step board uses, independently of the interpreter, and answers as
firmware must. Until the TriggerScope firmware exists, it is the board the
Expert panel and the conformance tests run against.

.. code-block:: json

    "scanEngines": {
        "board": {
            "managerName": "StepProtocolScanEngine",
            "managerProperties": {"port": "COM5", "baudrate": 115200,
                                  "replyTimeoutS": 0.5}
        }
    },
    "positioners": {"Z": {"analogChannel": "board/dac0", "...": "..."}},
    "scan": {"engine": "board", "scanWidgetType": "Expert", "...": "..."}

**managerProperties**

.. list-table::
   :widths: 25 12 18 45
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``port``
     - str
     - required
     - The board's serial port, or ``"simulated"`` for the simulated board.
   * - ``baudrate``
     - int
     - required on a serial port
     - The link's rate (a USB board ignores it; the port still needs one).
   * - ``replyTimeoutS``
     - float
     - required on a serial port
     - How long a command waits for the board's answer, in seconds.
   * - ``simulatedBoard``
     - dict
     - required on ``"simulated"``
     - What the simulated board declares: ``gridNs`` (required, its timing
       grid in ns), ``dacs`` (``{index: [minV, maxV]}``), ``ttlLines``,
       ``maxSteps``, ``triggerIn``, ``triggerLatencyTicks``,
       ``triggerMinTicks``, ``clockPpm``, and ``actualTriggerLatencyTicks``
       for a trigger input whose latency it does not declare.


PulseGeneratorScanEngine
========================

Any pulse generator -- the Teensy, the PulseStreamer, a pulse-generator
plugin -- as a scan engine, for the digital part of a scan program: laser and
camera switching, with fixed pauses between, such as a widefield RESOLFT
sequence. It is registered as ``PulseGeneratorScanEngine`` and
``builtin.pulse-generator-scan-engine``.

It compiles the program into the generator's step table: it plays the
program on the generator's own timing grid, finds the shortest stretch whose
line states repeat through the whole run, uploads that stretch and repeats
it. A device is wired to it by its ``digitalLine``, the generator's channel
index (as ``PulseGeneratorLaserManager`` reads it).

It refuses, naming the feature and the limit, what a table of digital states
cannot play:

* a position (a drive or an intra-step move): "Stage needs an analog output;
  Teensy has none.";
* a power level: "The L488 power level is an analog event; pulse generators
  play digital states only.";
* a wait for a trigger before every step, and a start trigger the generator
  does not take;
* more distinct steps than the generator holds ("This timeline needs 412
  distinct steps; Teensy holds 256."), more repeats than it counts;
* a time off the generator's grid, with the two nearest times on it (the
  Teensy's grid is 1 µs);
* a line active when low, on a generator whose stop drives every channel low
  (the Teensy's does): a stop would switch that device on.

After a run, and after a stop, it sets every line it drives to its off level:
the Teensy keeps its last step's state on the wire when a sequence ends.

What the generator does not declare is unknown, and a scan that needs it is
refused as such. The pulse-generator contract
(``imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager``) has
optional properties for it: ``max_sequence_steps``, ``timing_resolution_ns``,
``max_repeats``, ``run_triggers``, ``stop_drives_low`` and
``stop_granularity``. The Teensy declares them all from its v4 firmware (256
steps, whole microseconds, a 32-bit repeat count, no trigger input, a stop
that drives every channel low, checked between steps); a v3 firmware plays no
uploaded sequences. The PulseStreamer declares its 1 ns resolution only.

**managerProperties**

.. list-table::
   :widths: 25 12 18 45
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``pulseGenerator``
     - str
     - ``pulseGeneratorManager``
     - The low-level manager it drives. A ``teensyPulse`` section builds
       ``pulseGeneratorManager``.
   * - ``channels``
     - list
     - every channel
     - The generator's channels it owns, by index.
