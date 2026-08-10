*****************
Advanced scanning
*****************

The Advanced Scan widget repeats each physical scan line one or more times and
can gate a different set of lasers, detectors and other TTL devices on each
pass. It is intended for interleaved multicolour point scanning and experiments
that need explicit pulse timing within every pixel.

Configure it with ``scanWidgetType: "Advanced"`` and normally pair it with
``AdvancedScanTTLCycleDesigner``. See :ref:`scan` for the complete setup-file
fields and :doc:`scan-lifecycle` for the controller/recording lifecycle.


Geometry and pixel count
========================

For every active scan axis, Imswitch2 calculates::

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

* ``APDManager`` preserves the line-step dimension and records it as channels,
  normally ``(T, C, Y, X)``.
* ``PMTManager`` sums the line steps into one 2D image before publishing it.
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
corrected before scanning. Use **Plot scan** and enable **include TTL** to inspect
the generated analog and digital waveforms before driving hardware.

.. warning::

   **Intra-pixel positioner movement is not executed by the built-in scan
   designers.** The widget can edit, save and reload positioner windows and
   offsets, but the current ``GalvoScanDesigner`` and ``BetaScanDesigner`` do
   not apply them to the generated positioner waveform. Treat these fields as
   reserved for a site-specific designer; do not rely on them to move a stage
   or galvo.


Hardware requirements and limits
================================

Every scanning axis needs ``forScanning: true`` and a valid ``analogChannel``.
Real, non-mock axes used by ``GalvoScanDesigner`` additionally require these
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
3. Choose **Scan once** and press **REC**. This arms the recording; it does not
   choose or start a scan prematurely.
4. Start the Advanced scan from its own widget. The recording binds to that
   exact controller and derives its spatial dimensions and camera TTL rising-
   edge counts from the generated scan.
5. Inspect the saved axes: APD line steps are channels, while PMT output is
   summed as described above.

If the setup contains several scan widgets, **Scan once** still binds to the
one the operator starts. A recording-driven **Timelapse** cannot infer that
choice, so select the required controller in **Scan source** before starting;
the run is refused while that choice remains ambiguous.

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
   * - Camera records too few or no frames
     - Confirm its line-step enable boxes and TTL windows, then ensure exposure
       plus readout fits between triggers. Plot with TTL included.
   * - BeadRec raster wraps or has the wrong size
     - Use one camera pulse per enabled line step and verify the scan dimensions
       and camera selection.
   * - Programmed intra-pixel offset has no effect
     - Expected with the built-in designers; the metadata is retained but the
       positioner waveform is not generated.


.. seealso::

   :doc:`gui` for the rest of the Imcontrol interface,
   :doc:`devices/detectors` for detector-specific line-step output, and
   :doc:`scan-lifecycle` for developer-level ownership and completion rules.
