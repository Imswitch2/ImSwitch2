***************
Expert scanning
***************

The Expert scan panel edits a whole **scan program**: nested loops that
drive devices, and what happens inside every step. Interleaved and composite
axes, line steps at any level, pulse sequences, waits and detector bindings
are all written directly, instead of through the fixed geometry of the
Advanced panel. The program runs on the setup's scan engine (see
:doc:`devices/scan_engines`): the NI-DAQ plays it as sampled waveforms, a
pulse generator or step board plays it itself.

Configure it with ``scanWidgetType: "Expert"``. The panel builds no waveforms
with the signal designers, so ``scanDesigner`` and ``TTLCycleDesigner`` can be
``null``. ``expert_mock_scan_setup.json`` runs it without hardware: two APDs
image a synthetic sample, and a mock camera takes a frame per trigger.


The program
===========

**Loops** (outermost first)
    An id, a kind -- ``scan_x``, ``scan_y``, ``scan_z``, ``cycle``,
    ``plane``, ``time``, ``condition`` (line steps, at any level), ... -- and a
    count. **Order** is forward, reverse or serpentine; a serpentine runs
    back on every other pass of the loops right outside it, which
    **Alternates with** names.

**Drives**
    A device's position, in µm, is its **Offset** plus a weight per loop
    (µm per count): ``{cycle: 0.25, plane: 0.5}`` on one device visits
    0, 0.5, 1.0, 0.25, 0.75, 1.25 -- two cycles of three planes. One loop can
    drive several devices (a composite axis), and a **Table** gives a
    position per count of one loop instead. How the device moves: the
    **Move shape**, the **Move** and **Settle** times (inside the step,
    at its end, or between steps), the **Return** time after every pass of
    the innermost loop, whether the offset is **absolute** or relative to
    the position **captured** before the scan, and where the device goes
    **after the scan**.

**Timeline**
    What devices do inside every step, in ms from its start: a TTL window
    (``ttl``), a camera exposure (``detector``), a laser power (``level``),
    a positioner moved for part of the step (``offset``). **Phases** limits
    an event to some line steps. An event is placed while the devices stand
    still unless **During** says ``any``.

**Waits**
    A pause or an external trigger before the run, before every step, or
    before every iteration of a loop.

**Detectors**
    Which events a detector takes. A camera takes a frame on every rising
    edge of its line.

**Step** is the length of every innermost step. **Timing** is the program's
timing policy: ``strict-v1`` -- every time must sit on the engine's clock,
and one that does not is refused with the two nearest that do. Nothing is
rounded silently.


Presets
=======

**Make…** builds a program from a few parameters, in a form; the program
remembers them, so **Make…** opens the form again with them.

**MS-RESOLFT (cycles x planes)**
    One readout device visits ``first + dy * (cycle + cycles * plane)``,
    cycle by cycle, *timepoints* times, with a pulse sequence at every
    position and the camera exposing during one of its pulses. The camera's
    recording carries the ``cycle`` and ``plane`` loops, which the SNOUTY
    reconstructor reads and restacks.

**Pulse sequence (widefield RESOLFT)**
    Lasers and a camera only, *frames* times: a pulse generator can play it
    alone.

**Stepped raster**
    X and Y stepped, X fast (forward or serpentine), a pulse sequence at
    every pixel. APDs and PMTs assemble it as an image.

Every value is a parameter of the form; a preset defaults no time or
distance.


The report
==========

The text under the tables is the scan engine's verdict on the program,
compiled the same way the run compiles it, so it refuses exactly what the run
would refuse:

- every feature the program uses, as ``native``, ``emulated`` (with its
  cost), ``impossible`` or ``unknown`` (with the reason);
- the timing policy and the engine's clock, the duration and the number of
  steps, and every time that runs differently from the request;
- whether point detectors can assemble the scan. They read a **regular
  raster**: steps of one length, a return of one length after every pass,
  no waits, the line-step loop right outside the innermost loop. A program
  that is not one runs for cameras; with an APD or PMT taking part it is
  refused, naming them.

**Plot** draws every device's trace: positions in µm, lines as 0 and 1.


Running and recording
=====================

**Run Scan** runs the program on the engine, through the same lifecycle as
the other scan panels: **Repeat**, recording, the progress of the run.
Recording asks the program for its positions, frames per position and
layout: a camera records the program's loops (with a ``repeat`` loop when it
takes several frames per step, and the steps it takes them in when not
every step); an APD or PMT records its assembled image.

On a step engine the engine runs the compiled program itself, and **Stop**
stops it at its next step. See :doc:`devices/scan_engines` for what each
engine can play, and what it refuses.


Saving and opening
==================

**Save Scan** writes the program (``scan-program/1``) with its timing policy.
The file declares that it needs the Expert panel, so the Advanced panel
refuses it, saying why. A file is read strictly: an unknown key, a malformed
value or a missing timing policy refuses it, with every problem.

**Load Scan** also opens an **Advanced scan file**, as the program the
Advanced panel would run on this setup -- the same waveforms and lines. While
it is unchanged it is saved in the Advanced format too, so it still opens on
the Advanced panel; once edited, it is an Expert program.

A file that names a device the setup does not have is not applied; the
message names the device.


.. seealso::

   :doc:`advanced-scanning` for the raster panel, :doc:`devices/scan_engines`
   for the engines, and ``docs/design/plans/scan-engines-and-expert-scan.md``
   for the design.
