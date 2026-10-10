***************
Expert scanning
***************

The Expert scan panel edits a whole **scan program**: nested loops that
drive devices, and what happens inside every step. Interleaved and composite
axes, line steps at any level, pulse sequences, waits and detector bindings
are all written directly, instead of through the fixed geometry of the
Advanced panel. The program runs on the scan engine its devices are on (see
:doc:`devices/scan_engines`): the NI-DAQ plays it as sampled waveforms, a
pulse generator or step board plays it itself. A program with no device on
an engine runs on ``scan.engine``.

Configure it with ``scanWidgetType: "Expert"``. The panel builds no waveforms
with the signal designers, so ``scanDesigner`` and ``TTLCycleDesigner`` can be
``null``. ``expert_mock_scan_setup.json`` runs it without hardware: two APDs
image a synthetic sample, and a mock camera takes a frame per trigger.


The panel
=========

The panel describes the scan from top to bottom, in the order it happens.
When the dock is wide enough, **Check** and **Preview** stand beside the
editor; when it is narrow, below it.

**The header** stays in view while the rest scrolls. It names the program
(its template, or *edited* once changed) and its loops (*2 timepoints × 3
cycles × 4 planes*), says whether it can run and on which engine (*Ready on
triggerscope*; red, with the first reason on the line under it, when it
cannot -- click it for the whole check), and shows its steps, duration,
frames per camera and the engine's clock. **Run**, **Templates** (the
presets and trajectories, below), **Open…**, **Save…**, **Undo** and **Redo**
(Ctrl+Z, Ctrl+Shift+Z), **Repeat** and **Tables** are next to it. An empty
program starts with the templates as buttons; a trajectory is shown as its
path, with **Edit the path…** to change it.

**Scan order** shows the loops as blocks, outermost first, each with its
count and its order (the arrow: forward, backward, serpentine; click for the
next). A pause or a trigger before a loop's iterations shows on its block.
A serpentine alternates with the loop right outside it (the outermost loop
cannot be one). The **⋯** of a block renames it, moves it outwards or
inwards, changes its kind, says how many loops outside it a serpentine
alternates with, or removes it -- with the waits before it and the anchors on
it, which the line under the header says (**Undo** brings them back); **+
Loop** adds one inside the others. Scrolling over a field you have not
clicked into scrolls the panel; it does not change the field. The bars under the blocks are the positions the first
drive visits, in order, over the innermost two loops: an interleaved order
is seen at a glance.

**Positions** writes each drive as a formula -- *Z = 1 µm + 0.15 µm × cycle
+ 0.45 µm × plane* (a blank weight: that loop does not move it), with where
that takes it (*12 positions, from 1 to 2.65 µm*) -- and how it moves, as a
sentence: the move shape, the move and settle times, at the
end of each step or between steps, the return after each pass, absolute or
from where it starts, and where it goes after the scan.

**Inside every step** draws one step: each event a bar on its device's row,
in the colour of the laser's light. Drag a bar to move it, an edge to resize
it (a narrow bar: just outside its edges), or select it and nudge it with
the arrow keys -- one tick of the engine's clock, ten with Shift; Delete
removes it. Times snap to the engine's clock and stay inside the step, and
the times show while you drag. The time a drive moves at the end of the step
is hatched, so an exposure that must fall while the stage stands still is
placed outside it. With line steps, the phase box shows the step as one
phase plays it. **+ Event** adds a pulse, a camera exposure, a power level or
a move, from the end of the last event to the end of the step, to be
resized. Events that end after the step offer to lengthen it. Under the
drawing, the selected event's device, kind and times (and value, for a
level or a move); **More** shows the rest -- its phases, whether it needs
the drives still, every step or only the first or last of each pass, its
id.

**Waits** and **Recording** are sentences: *Before each timepoint, pause
100 ms; outputs off*; *Camera takes 1 per step of exposure*. Under the
recording, what the check says each detector records.

**Check** is the engine's verdict (the report, below): what stops the run
first, in red; each feature that is not native, and the notes. **Full
report** shows all of it.

**Preview** draws every device on its own lane, like a logic analyser --
the drives' positions scaled into their lanes, the lines high or low -- for
**One step**, **One pass** or **All**. **Draw** compiles it the way the run
will; after an edit the preview says it changed since it was drawn, and
**Keep drawn** draws it again after every edit the engine accepts. A program
that cannot be drawn says why where the drawing would be. A program that
waits for a trigger is drawn up to that wait.

**Tables** shows the same program as tables, every field of it, as the
parts above edit them: one program, two views.


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
    still unless **During** says ``any``; **Still** names the devices that
    must stand still under it (blank: every drive).

**Waits**
    A pause or an external trigger before the run, before every step, or
    before every iteration of a loop. Every drive holds where it is through
    a wait.

**Detectors**
    What each detector records. A camera takes a frame on every rising edge
    of its line, so its binding -- the **Events** it takes (an event id, or
    the camera itself), the frames **Per step** and the **Phases** it takes
    part in (blank: all) -- is checked against its line before anything
    runs: a step where the line fires other than the binding says refuses
    the program, with the step and its phase. The recording's layout and
    frame counts then come from the binding. A camera takes ``event``
    windows assembled as a ``stream``; a point detector (APD, PMT)
    assembles an ``image``, one value per step that integrates every sample
    of the step (``step``), in every phase. Other combinations are refused,
    not approximated. A detector without a binding is recorded as its line
    fires; the report says which detector is recorded which way.

**Step** is the length of every innermost step. **Timing** is the program's
timing policy: ``strict-v1`` -- every time must sit on the engine's clock,
and one that does not is refused with the two nearest that do. Nothing is
rounded silently. A time is taken as the decimal it is typed as (2.3 ms is
2.3 ms, not the nearest binary fraction of a division), and a preset adds
its times exactly, so a time on the clock is never refused as off it.

**The approach.** A run starts with the drives moving to their first
positions and settling -- for the longest move and settle of a drive that
is not there yet (where it was captured; a drive whose position is not
known is given its move and settle too) -- before anything else is timed.
The report says how long (*Starts with 1 ms for the drives to reach their
first positions and settle*). An Advanced scan opened here starts as its
designer plays it, without one, until it is edited.

**A move that outlasts the return.** A drive of an outer loop moves at the
end of a pass, during the return. When its move and settle are longer than
the return, it is still moving at the start of the next step, and an event
there that must be placed while it stands still is refused with the time it
moves for: lengthen the return, or let the event run while it moves
(**During** ``any``).


Presets
=======

**Templates** builds a program from a few parameters, in a form; the program
remembers them, so its template opens the form again with them.

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


Trajectories
============

A trajectory program moves two scanners along a free path instead of through
loops: **Trajectory: Circle**, **Spiral**, **Lissajous**, **Points** (a curve
given as a list of points, visited at equal times) or **Python** (a function
``path(t, duration)`` returning x and y in µm for an array of times; it runs
like a Python step, unsandboxed). The form asks for the time on the path, the
**measurement window**, the **approach** (the move onto the path from where
the scanners are, and back off it at the end) and the **grid pixel**; a laser
can be on along the path.

**A Python path runs only once it is approved on this computer.** A scan file
can carry one, and its code runs whenever the program is checked, plotted or
run. Code written in a template's form counts as approved. Code a file
brings is shown when the file is loaded with **Open…**, and again at **Draw**
or **Run** until it is approved; declined, it does not run, and the report
says so. The startup restore and scripts that load or set a program never
ask: the program opens with its path held until someone approves it at the
panel. Approved code is remembered by its SHA-256 (line endings ignored) in
``ImSwitchConfig/config/approved_scan_scripts.json``, so the same code does
not ask again; changed code asks again. Drop-in pattern files (below) are
installed by hand and not asked about.

**What is measured.** APDs and PMTs measure window after window along the
path. Each window is one measurement over its realized integration time,
placed at the commanded position of its centre, and binned onto a grid that
covers the windows:

.. list-table::
   :header-rows: 1
   :widths: 20 40 15

   * - Detector
     - Bin value
     - Unit
   * - APD (photon counter)
     - Σ counts / Σ integration time
     - counts/s
   * - PMT (analog)
     - Σ (voltage · integration time) / Σ integration time
     - V

Weighting by integration time keeps a bin right when windows differ in
length -- the last one usually does. A bin no window reached is **empty
(NaN)**; a measured zero is 0. The image assumes a linear detector, no
dead-time correction, and the path followed as commanded.

**The coverage** -- each bin's total integration time, in seconds -- is
recorded beside the image, so sparse can be told from dark: in HDF5 and Zarr
as a ``coverage`` array in the detector's group, in TIFF as a
``…_coverage.tif`` beside the recording. The image carries its unit
(``measurement:unit``) and the reduction (``measurement:reduction``: that it
was gridded from a trajectory, the grid, the weighting, the assumptions, and
whether the measurements themselves were stored).

**Every measurement, when asked.** The gridded image is a reduction: windows
in one bin are averaged, and from the image a reconstructor knows every
window's position but not its value, so it cannot re-grid finer or correct
the path. Tick **Record every measurement** in the form, and the recording
also holds what the image was reduced from:

.. list-table::
   :header-rows: 1
   :widths: 28 52 20

   * - Array
     - What
     - Unit
   * - ``measurements``
     - each window's measurement, in the order it was taken -- the counts
       (APD) or the mean voltage, offset removed (PMT); one row per
       recorded frame
     - counts or V
   * - ``measurement_seconds``
     - each window's realized integration time
     - s
   * - ``measurement_centre_um``
     - each window's commanded position at its centre, one column per
       scanner
     - µm

In HDF5 and Zarr they are arrays in the detector's group, beside the
coverage; in TIFF, ``…_measurements.tif`` and so on beside the recording.
Each frame's row travels with that frame from the detector to the file, so a
lapse of several scans keeps every scan's own. A frame whose measurements did
not arrive has a row of NaN. Off by default: a 1 s path in 20 µs windows is
50 000 numbers per frame.

**Limits.** The approach joins the path with its speed, so the scanners never
jump. The report shows each scanner's peak speed and acceleration against its
``vel_max`` and ``acc_max``; a path above them is refused, and so is a
scanner that does not declare one of them -- a path is not run on limits
nobody gave.
A spiral at constant angular speed is gentle at its centre; one at constant
linear speed starts with an unbounded acceleration there and is refused on a
galvo.

A trajectory runs on a streaming engine (the NI-DAQ). Cameras take no frames
on it. The panel shows the laser's timing on the path; **Templates** edits the
path itself.

**Your own patterns.** A ``.py`` file in the ``scan_patterns`` folder of the
user files (``ImSwitchConfig/scan_patterns``) adds patterns to **Templates**
without touching ImSwitch: it defines ``PATTERNS``, a list of ``Pattern`` --
an id (dotted and your own, such as ``user.rosette``), a title, the devices
it moves (two: the X and Y device), its parameters as ``(name, label,
kind)``, and ``path(params, t, duration)`` returning x and y in µm for an
array of times. A parameter's kind decides its field in the form:
``micrometres``, ``number``, ``pair-micrometres``, ``pair-hertz``,
``points``, ``code`` or ``choice:a,b``. The folder holds an example,
``_example_pattern.py``; files starting with an underscore are not loaded.

The patterns are loaded when the Expert panel starts. A file that does not
load, a pattern that would replace a built-in or reuse another file's id,
moves other than two devices, or asks for a kind the form has no field for,
is skipped and the reason logged (**View → Log**); the rest still load. A
saved program that uses a pattern opens wherever its file is loaded, and is
refused with that reason where it is not. The gridded image records the
pattern's source with its id (``measurement:reduction``: ``pattern_source``,
the file's name and SHA-256). The files run as Python, unsandboxed: keep only
code you trust.


The report
==========

**Check** is the scan engine's verdict on the program,
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

**Draw**, in the preview, draws every device's trace, each on its lane:
positions in µm, scaled into their lane, lines high or low.


Running and recording
=====================

**Run** runs the program on the engine, through the same lifecycle as
the other scan panels: **Repeat**, recording, the progress of the run.
Recording asks the program for its positions, frames per position and
layout: a camera records the program's loops (with a ``repeat`` loop when it
takes several frames per step, and the steps it takes them in when not
every step); an APD or PMT records its assembled image.

A recording holds what ran beside what was requested: the program as
``ScanStage:scan_program`` and its realized timing as
``ScanStage:scan_schedule`` -- the policy with its contents, the grid and
where it came from, every time that runs differently, the approach and the
duration. A timing policy that declares tolerances is refused: nothing
applies them yet, so it would claim a tolerance the realization does not
honour (a separation carries its own ``tolerance_s``).

When a run ends, each positioner it moved is told where the run left it --
the position the program restores it to, a trajectory's last sample; for a
stopped or failed run on a step board, the board's last write -- so the
next run's captured origin, and a move by hand, start from there. Where that
is not known, the log says so and the tracked position is left as it was.

On a step engine the engine runs the compiled program itself, and **Stop**
stops it at its next step. A program whose devices are on ``scan.engine``
and the engines wired to follow it runs on all of them as one: ``scan.engine``
leads, and every follower is stepped by its pulses
(:doc:`devices/scan_engines`); devices on engines that do not run together
are refused, with which is where.
The report then lists each engine's verdicts, the pulse, and each
follower's drift between two pulses. A step board that speaks the step-engine protocol
(:doc:`devices/step-engine-protocol`) is uploaded the program and runs it
on its own; ``"port": "simulated"`` runs a simulated one. See
:doc:`devices/scan_engines` for what each engine can play, and what it
refuses.


Started by an event
===================

The EtSnouty widget's **Slow scan** set to ``Scan`` runs the Expert program
once for every event its pipeline detects, instead of the TriggerScope
routine. The run carries the event (``ScanEvent:triggeredBy``). See
:ref:`the EtSnouty widget <etsnouty-widget>`.


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
