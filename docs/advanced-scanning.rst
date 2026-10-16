*****************
Advanced scanning
*****************

The Advanced Scan widget repeats each physical scan line one or more times and
can gate a different set of lasers, detectors and other TTL devices on each
pass. It is intended for interleaved multicolour point scanning and experiments
that need explicit pulse timing within every pixel.

Configure it with ``scanWidgetType: "Advanced"`` and normally pair it with
``AdvancedScanTTLCycleDesigner``. See :ref:`the scan block <setupinfo-scan>` for the complete setup-file
fields and :doc:`scan-lifecycle` for the controller/recording lifecycle.


Geometry and pixel count
========================

For every active scan axis, ImSwitch2 calculates::

    pixels = max(1, round(size / step))

The requested step is the realized centre-to-centre pixel pitch. Consequently,
``N`` pixel centres span ``(N - 1) * step`` rather than the full value displayed
in **Size**. This convention keeps the generated waveform, displayed pixel
count, recording dimensions and OME physical pixel size consistent when size
is not an exact multiple of step.

The scan is centred on the configured centre position. A one-pixel axis visits
that centre once.


Line steps
==========

**#Line repeats** is the number of passes over each physical line before the
slow axis advances. **Line program devices** opens an enable matrix with one
column per line step. For example, a two-colour scan can enable the 488 nm laser
and detector on step 1, then the 561 nm laser and detector on step 2.

Line steps are interleaved line by line, not acquired as complete sequential
frames. ``N`` steps therefore take roughly ``N`` times the line acquisition
time. Point-detector output depends on its manager:

* ``APDManager`` and ``PMTManager`` preserve the line-step dimension and
  record it as channels, normally ``(T, C, Y, X)``. The PMT's live view shows
  the steps summed into one image; its recordings are not summed.
* A camera produces a frame for every rising edge in its TTL program.


Timing windows
==============

Enable **Advanced Line Program** to edit timing within one pixel dwell. In
**Timing windows** mode, each device and line step has comma-separated
**Start(s)** and **End(s)** values in milliseconds. Corresponding entries form
pulse windows; a device with no explicit window is enabled for the whole pixel
when its line-step checkbox is on.

**Lock-Master** and **Lock-with** make devices share timing instead of requiring
the same windows to be entered repeatedly. **Power Level (%)** applies only to
lasers with an analog channel and is constant for the entire line step; TTL
still gates when that level reaches the device.


Sequence Builder
----------------

**Sequence builder** is another editor for the same timing windows. Its rows
are compiled in table order, independently for each line step:

``Device(s)``
    One row may gate several devices over the same interval.

``Start delay (ms)``
    Relative to the **end of the preceding row**, not to the start of the pixel.
    The first row is relative to time zero. A positive value creates a gap; a
    negative value deliberately overlaps the preceding row.

``Duration (ms)``
    The length of the interval. Zero creates no pulse.

``Value``
    Disabled for TTL-only devices. For analog-capable lasers it is a percentage
    clamped to 0–100; for a positioner it is labelled in µm. Laser analog power
    remains one constant value per device and line step, not a different analog
    amplitude per pulse. If the same laser appears in several rows with
    different values, the last compiled row sets that step's power.

Avoid combining positioners and analog-capable lasers in one row: they share
timing, but one ``Value`` cannot represent both µm and percent. Switching between
Timing windows and Sequence builder converts the current program; inspect the
result after conversion, especially when several devices share windows.

The displayed **Dead time** is dwell time minus the end of the latest programmed
event. A negative value means the program extends beyond the dwell and should be
corrected before scanning. Use **Plot** and enable **include TTL** to inspect
the generated analog and digital waveforms before driving hardware.

Intra-pixel positioner moves
----------------------------

With **Intra pixel positioners** on, a scanning positioner can be given
windows in a line step, like a laser, and a **Value** in µm: during each
window the positioner stands that far from its pixel's position, and returns
to it afterwards. On a stepped scan (``BetaScanDesigner``) the moves are
played: each one takes the stage's ``move_time`` and ``settle_time`` on both
sides, inside the dwell, so a window must leave room for them while the
stage holds its pixel. A window that does not is refused, and the message
says which step and how long the move needs. A scan with an intra-pixel move
runs under strict timing (see `Order`_).

.. note::

   On a swept scan (``GalvoScanDesigner``) intra-pixel moves are refused with
   the reason: the fast axis never holds a pixel. Earlier versions saved the
   windows and silently ignored them on every designer; a saved scan with
   intra-pixel moves now does what it says on a stepped scan.


Order
=====

The **Order** column says in which order an axis visits its positions:

**Forward**
    1, 2, 3, ... -- every scan until now.

**Serpentine**
    Every other pass runs back: the fast axis on every other line played in
    a slice (line steps included), the middle axis on every other slice. The
    outermost axis cannot be serpentine.

**Interleave k**
    *k* cycles of every *k*-th position: with *k* = 2 over six positions,
    1, 3, 5, 2, 4, 6. These are the cycles and planes of MS-RESOLFT (*k*
    cycles of *n/k* planes). Only the *k* that divide the axis's pixel count
    are offered.

Each pixel is still placed at its own position: APDs and PMTs assemble the
image in position order, and a camera's recording says in its acquisition
layout where every frame was taken (an interleaved axis as ``cycle`` ×
``plane``, which the SNOUTY reconstructor restacks; a serpentine as a
serpentine traversal). Saved scans keep the order (``axis_order``); a scan
saved without one loads as forward.

Orders are built for stepped scans (``BetaScanDesigner``). On a swept scan
the column is greyed out with the reason.

An order other than forward, or an intra-pixel move, runs the scan under
**strict timing** (``strict-v1``), and the panel says so. Every time -- the
dwell, the moves, the windows of the line program too -- must sit on the
sample clock: a value off it is refused with the two nearest that are on it,
never rounded. A forward scan
runs as before (``advanced-compatible-v1``): when the clock rounds a time,
the panel shows the time it runs at beside the requested one -- the dwell
(*Dwell 1.005 ms runs as 1.01 ms*), and every move, settle, return and
line-step window edge the scan plays differently (*405 (ON) ttl start at
15 µs runs at 20 µs*), each once. The dwell that plays is a whole number of
samples, rounded up, and it is the one dwell of the whole scan: the sweep,
the laser and camera lines and the point detectors' bins all follow it,
and the recording's ``dwell_time`` is that value, with the request beside
it as ``dwell_requested_s`` when they differ. Saved scans record the policy they run under as
``timingPolicy``; a file without it is a scan saved before, and runs as
before. A file naming a policy this version does not know is not applied.
A recording holds what the scan realized (``ScanStage:scan_schedule``): the
policy with its contents, the sample clock and where it came from, and each
time it plays differently, once -- what the timing note says. A swept scan,
which the program path does not describe, records none.

Only the APD and PMT place visits; the Swabian TimeTagger refuses a scan
that is not forward, with the reason.


Hardware requirements and limits
================================

Every scanning axis needs ``forScanning: true`` and a valid ``analogChannel``.
Axes that ``GalvoScanDesigner`` sweeps smoothly additionally require these
``managerProperties``:

``vel_max``
    Maximum velocity in µm/µs.

``acc_max``
    Maximum acceleration in µm/µs².

``jerk_max``
    Optional maximum jerk in µm/µs³.

For example::

    "managerProperties": {
        "vel_max": 0.1,
        "acc_max": 0.0001
    }

Use values measured or specified for the actual scanner. Missing velocity or
acceleration limits stop signal construction with a configuration error.

An axis is swept smoothly unless its ``managerProperties`` set
``"smoothScan": false``; without that key, only an axis whose name contains
``mock`` is stepped. A stepped axis (a piezo or stage on the fast axis, for
example) needs no limits.

The fast axis sweeps one pixel per dwell, so ``step / dwell`` must not exceed
its ``vel_max``. A faster scan is refused before anything moves. The message
names the axis and the limit, and gives a dwell or pixel size that would pass,
for example *Fast axis X would sweep at 0.2 µm/µs, above its vel_max of
0.1 µm/µs. Use a dwell of at least 20 µs for 2 µm pixels, or pixels of at most
1 µm at a 10 µs dwell.*

Before this check, such a scan ran: the galvo was driven past its limit and
overshot each line several times over, and the scan was stopped only if that
overshoot left the scanner's voltage range. A stepped fast axis
(``smoothScan: false``) holds each position for the dwell and is not limited.

Signal construction also refuses more than 10 million spatial positions. The
optional ``scan.maxScanTimeMin`` setup value adds a duration guard; ``null`` or
omission disables that time guard. The guard is an early estimate, not a
replacement for checking the plotted waveform and the GUI duration, especially
when line steps multiply a scan.


Recording an advanced scan
==========================

For a single acquisition:

1. Select the detector or detectors in the Recording widget.
2. Confirm that every triggered camera is enabled in the intended line steps
   and that its exposure plus readout fits inside the trigger interval/dwell.
3. Choose **Scan once** and press **REC**. The Advanced widget is the setup's
   ``Scan`` widget, so this arms the recording *and* starts the Advanced scan;
   do not also press the scan widget's own start button, which would be
   refused while the scan runs. The recording derives its spatial dimensions
   and camera TTL rising-edge counts from the generated scan.
4. Inspect the saved axes: APD and PMT line steps are channels.

**Scan once** always drives the ``Scan`` widget, even if the setup has other
scan widgets as well. A **Timelapse scan** on such a setup needs the required
controller selected in **Scan source** before starting; the run is refused
while that choice remains ambiguous.

General Scan-once recording counts the rising edges in each generated camera
TTL waveform, including multiple pulses in one line step. **BeadRec is
narrower:** its live frames-per-pixel calculation assumes at most one camera
trigger per enabled line step. When using BeadRec with an Advanced scan, give
the camera no more than one pulse per enabled step. Multiple pulses can make
the BeadRec raster consume the wrong number of frames even though the ordinary
recording frame count is correct.

If recording cannot arm, the scan is stopped before hardware starts rather
than running unrecorded. The recording-binding section in
:doc:`scan-lifecycle` describes the underlying source-selection contract.


Saving and loading programs
===========================

**Save Scan** writes the complete Advanced widget state to JSON: geometry,
line-step enable matrix, timing mode and windows, Sequence Builder rows,
device locks, analog power values, dwell time, delays, and the reserved
positioner metadata. **Load Scan** restores the same state. Review device names
and plot the waveform after moving a saved program to another setup; missing or
renamed hardware cannot be made equivalent by the saved file.


Advanced scan troubleshooting
=============================

.. list-table::
   :header-rows: 1
   :widths: 38 62

   * - Symptom
     - Check
   * - Signal is rejected as too long
     - Reduce the ROI/pixel count, increase the step, shorten the dwell or line
       program, reduce line steps, or review ``maxScanTimeMin``.
   * - Galvo signal construction reports missing limits
     - Add realistic ``vel_max`` and ``acc_max`` values to every real scanning
       positioner's ``managerProperties``.
   * - Scan refused: the fast axis would sweep above ``vel_max``
     - Lengthen the dwell or use smaller pixels, as the message says. If the
       scanner is really faster than configured, measure it and raise
       ``vel_max``; do not raise it just to make the scan pass.
   * - Camera records too few or no frames
     - Confirm its line-step enable boxes and TTL windows, then ensure exposure
       plus readout fits between triggers. Plot with TTL included.
   * - BeadRec raster wraps or has the wrong size
     - Use one camera pulse per enabled line step and verify the scan dimensions
       and camera selection.
   * - Programmed intra-pixel offset has no effect
     - Intra-pixel moves are played on stepped scans (``BetaScanDesigner``)
       only; a swept scan refuses them. Plot the scan to see the move.
   * - Scan refused: a time "is not on the 10 µs grid"
     - The scan uses an order other than forward or an intra-pixel move, so
       it runs under strict timing. Use one of the two values the message
       offers.
   * - Line-step image has the wrong number of lines or channels
     - Search the log for ``[LineStepDiag]``. The scan controller, NI-DAQ
       manager and APD manager each log the expanded line count, sample
       budget, line-clock edges and read plan, ending in ``status=OK`` or
       ``status=MISMATCH: ...`` naming what disagrees.


.. seealso::

   :doc:`gui` for the rest of the ImControl interface,
   :doc:`devices/detectors` for detector-specific line-step output, and
   :doc:`scan-lifecycle` for developer-level ownership and completion rules.
