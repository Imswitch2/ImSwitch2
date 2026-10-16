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

The panel describes the scan from top to bottom, in the order it happens,
in the words of the microscope rather than of the model: a loop of the
program is a **dimension** on the panel (timepoints, cycles, planes, tiles,
positions, phases), a drive is a **positioner**, the length of a step its
**dwell**, and the events are **pulses**, **exposures**, **power levels**
and **moves**. The saved file and the API keep the model's names (below).
When the dock is wide enough, **Can it run?** and **Preview** stand beside
the editor; when it is narrow, below it.

**The header** stays in view while the rest scrolls. It names the scan (its
template, or *edited* once changed) with **Change template values…** beside
it to reopen the template's form with the values the scan was made from,
and its dimensions (*2 timepoints › 2 cycles › 3 planes*); says whether it
can run and on which engine (*Ready to run on triggerscope*; red, with the
first reason on the line under it, when it cannot -- click it for the whole
check); and shows its steps, duration, frames per camera and the engine's
clock. **Run**, **New from template** (the templates and trajectories,
below), **Open…**, **Save…**, **Undo** and **Redo** (Ctrl+Z, Ctrl+Shift+Z),
**Repeat** and **Tables** are next to it. An empty panel shows only **Start
from a template**: the templates as buttons, a **Trajectory** menu, and **+
Dimension** to build the scan by hand. A trajectory is shown as its path,
with **Edit the path…** to change it.

**Scan order** shows the dimensions as blocks, slowest to fastest, each with
its count, its name and its order (*forward*, *backward*, *serpentine*). A
pause or a trigger before a dimension's iterations shows on its block. A
serpentine alternates with the dimension right outside it (the slowest one
cannot be one). The **⋯** of a block renames it, makes it slower or faster
(moves it outwards or inwards), changes its kind, says how many dimensions
outside it a serpentine alternates with, or removes it -- with the pauses
before it and the anchors on it, which the line under the header says
(**Undo** brings them back); **+ Dimension** adds one inside the others.
Scrolling over a field you have not clicked into scrolls the panel; it does
not change the field. Under the blocks, a line through points is the
position the first positioner visits at each step, in order, over the two
fastest dimensions that move it, the lowest and highest positions at its
right edge: an interleaved order is seen at a glance.

**Positions** writes each positioner as a sentence -- *Z starts at 1 µm and
moves 0.25 µm per cycle and 0.5 µm per plane*; a dimension that does not
move it is behind **+ also per…** -- with where that takes it (*6 positions
from 1 to 2.25 µm, 0.25 µm apart*), and how it moves: *jumps, at the end of
each step, taking 0.5 ms, then settles 0.5 ms; return to the line start 1
ms; positions are absolute; after the scan back to its first position*.

**Within one step** draws one step: its **dwell**, and each event a bar on
its device's row, in the colour of the laser's light. Drag a bar to move
it, an edge to resize it (a narrow bar: just outside its edges), or select
it and nudge it with the arrow keys -- one tick of the engine's clock, ten
with Shift; Delete removes it. Times snap to the engine's clock and stay
inside the step, and the times show while you drag. The time a positioner
moves and settles at the end of the step is hatched, so an exposure that
must fall while the stage stands still is placed outside it. With line
steps, the phase box shows the step as one phase plays it. **+ Pulse**, **+
Exposure**, **+ Power level** and **+ Move** add an event of a device, from
the end of the last event to the end of the step, to be resized. Events
that end after the step offer to lengthen the dwell. Under the drawing, the
selected event's device, kind and times (and value, for a power level or a
move); **More** shows the rest -- its phases, whether it waits for the
stage to stand still and for which positioners, every step or only the
first or last of each pass, its id.

**Pauses and triggers** and **Recording** are sentences: *Before each
timepoint, pause 100 ms; meanwhile the lasers are off*; *Camera records 1
frame per step, triggered by exposure*; *APD integrates each whole step
into one pixel of an image*. A camera records frames, a point detector an
image: those are the only combinations the model accepts, so the panel
says them rather than offering them, and choosing a detector of the other
kind rewrites them. Under the recording, what the check says each detector
records.

**Can it run?** is the engine's verdict (the report, below): yes or no, on
which engine; what stops the run first, in red; each feature that is played
differently (in grey: a fact, such as the NI-DAQ playing stepped positions
as sampled waveforms) or impossible or not known, and the notes. **Full
report** shows all of it.

**Preview** draws every device on its own lane, like a logic analyser --
the positioners' positions scaled into their lanes, the lines high or low
-- for **One step**, **One pass** or **All**. **Show preview** compiles it
the way the run will; after an edit the preview says it is out of date, and
**Auto-update** draws it again after every edit the engine accepts. A scan
that cannot be drawn says why where the drawing would be. A scan that waits
for a trigger is drawn up to that wait.

**Tables** shows the same scan as tables, every field of it, as the parts
above edit them: one program, two views.


The program
===========

**Dimensions** (outermost first; the model's loops)
    An id, a kind -- ``scan_x``, ``scan_y``, ``scan_z``, ``cycle``,
    ``plane``, ``time``, ``condition`` (line steps, at any level), ... -- and a
    count. **Order** is forward, reverse or serpentine; a serpentine runs
    back on every other pass of the loops right outside it, which
    **Alternates with** names.

**Positioners** (the model's drives)
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

**Pauses** (the model's waits)
    A pause or an external trigger before the run, before every step, or
    before every iteration of a loop. Every drive holds where it is through
    a wait.

**Detectors**
    What each detector records. A camera takes a frame on every rising edge
    of its line -- the line is one wire, so a window to the end of a step
    and one from the start of the next are one edge, and a line held
    through a ``hold`` wait stays up -- so its binding -- the **Events** it takes (an event id, or
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

**Dwell** (``step_s``) is the length of every step. **Timing** is the program's
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

**Where the scan ends.** The last position the schedule plays is where
the run leaves a positioner, and what it is told afterwards. The last
return takes every positioner to where it goes after the scan; requested
with no duration (``return_s`` 0), it is given the time the positioner
declares for a move and a settle (one tick of the clock when it declares
none) and listed among the times played differently.

**A move that outlasts the return.** A drive of an outer loop moves at the
end of a pass, during the return. When its move and settle are longer than
the return, it is still moving at the start of the next step, and an event
there that must be placed while it stands still is refused with the time it
moves for: lengthen the return, or let the event run while it moves
(**During** ``any``).


Presets
=======

**New from template** builds a scan from a few values, in a form in sections
(Positions, Pulses, Camera, Time lapse, Motion); the scan remembers them, so
**Change template values…** in the header opens the form again with them
(once the scan is edited by hand, the form holds the template's values, not
the edits). A template opened afresh offers the values last used for it. An
empty field is filled in from the rig where the rig knows the value and
never guessed: positioner fields with distinct positioners, a first
position with that positioner's current position (it follows the positioner
chosen until the field is typed in), and the move, settle and line return
times with what the setup declares in ``scanDesignerParams`` (``move_time``,
``settle_time``, ``return_time``); what the rig does not know stays empty and
says *required*.

**MS-RESOLFT (cycles x planes)**
    One readout positioner visits ``first + spacing * (cycle + cycles *
    plane)``, cycle by cycle, *timepoints* times, with a pulse sequence at
    every position and the camera exposing during one of its pulses. The camera's
    recording carries the ``cycle`` and ``plane`` loops, which the SNOUTY
    reconstructor reads and restacks.

**Pulse sequence (widefield RESOLFT)**
    Lasers and a camera only, *frames* times: a pulse generator can play it
    alone.

**Stepped raster**
    X and Y stepped, X fast (forward or serpentine), a pulse sequence at
    every pixel. APDs and PMTs assemble it as an image.

Every value is a parameter of the form; a preset defaults no time or
distance (the form fills in only what the rig knows, above).


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
brings is shown when the file is loaded with **Open…**, and again at **Show
preview** or **Run** until it is approved; declined, it does not run, and the report
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
on it. The panel shows the laser's timing on the path; **Edit the path…**
edits the path itself.

**Your own patterns.** A ``.py`` file in the ``scan_patterns`` folder of the
user files (``ImSwitchConfig/scan_patterns``) adds patterns to the
**Trajectory** menu without touching ImSwitch: it defines ``PATTERNS``, a list of ``Pattern`` --
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

**Show preview** draws every device's trace, each on its lane: positions in
µm, scaled into their lane, lines high or low.


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
(beside **Run**, enabled while a run is in progress) stops it at its next
step; on the NI-DAQ, the pass in progress finishes and nothing more starts. A program whose devices are on ``scan.engine``
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

**Save…** writes the program (``scan-program/1``) with its timing policy.
The file declares that it needs the Expert panel, so the Advanced panel
refuses it, saying why. A file is read strictly: an unknown key, a malformed
value or a missing timing policy refuses it, with every problem.

**Open…** also opens an **Advanced scan file**, as the program the
Advanced panel would run on this setup -- the same waveforms and lines. While
it is unchanged it is saved in the Advanced format too, so it still opens on
the Advanced panel; once edited, it is an Expert program.

A file that names a device the setup does not have is not applied; the
message names the device.


.. seealso::

   :doc:`advanced-scanning` for the raster panel, :doc:`devices/scan_engines`
   for the engines, and ``docs/design/plans/scan-engines-and-expert-scan.md``
   for the design.
