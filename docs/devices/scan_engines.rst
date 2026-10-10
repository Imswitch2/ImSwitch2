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
   * - ``maxSteps``
     - int
     - not declared
     - The steps it can hold; a longer program is refused.
