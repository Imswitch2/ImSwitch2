**********************
Timelapse acquisitions
**********************

The Timelapse widget repeats one acquisition N times with a delay between
the repetitions, and keeps the chosen lasers on **only while a timepoint is
being acquired**. Between timepoints the sample is dark, whatever the Laser
widget was left at.

A timepoint is one of three things:

Camera frames (no scan)
    One or more frames from free-running cameras. The ticked lasers are
    switched on for those frames only, so a long-interval timelapse pulses
    the light for a single frame per timepoint.

Scan
    One scan of a scan panel (the Advanced scan, a Simple panel, a
    TriggerScope scan...). The scan program decides which lasers fire, as it
    does for any scan; the widget shows them read-only, and every other laser
    stays off for the whole timelapse.

Tiling
    One complete run of the :doc:`Tiling <tiling>` widget, with the Tiling
    widget's settings. Offered only when the setup has Tiling. In
    free-running tiling the ticked lasers are switched on around each tile's
    frames only, not while the stage moves or the mosaic is stitched; in
    triggered tiling the scan arms its lasers per tile, as for a scan.


The widget
==========

Acquisition
    ``Each timepoint`` is the kind above. ``Scan source`` picks the scan panel
    when the setup has several. ``Frames per timepoint`` (camera timelapses)
    is how many consecutive frames each timepoint records.

Timing
    ``Timepoints`` and ``Interval (s)``. ``Measured`` says what the interval
    is:

    **Start to start** is a fixed cadence: timepoint *i* starts one interval
    after timepoint *i-1* started. A timepoint that takes longer than the
    interval starts the next one at once and the cadence continues from
    there -- there is never a burst of catch-up timepoints, and the delay is
    recorded (``timelapse:slip_s``).

    **Delay after each point** is a pause: the interval runs from the end of
    one timepoint to the start of the next. Use it when a timepoint's
    duration varies, as a tiling's does.

Illumination
    One row per laser. Tick the lasers that light each timepoint and set
    their power; binary lasers have no power box. A TTL gate whose power is
    set by a partner device (``powerDevice`` in the setup) switches that
    partner too.

    ``Extra settle after switching on`` is for light sources slower than one
    camera frame -- a mechanical shutter, an AOTF's rise. Left unticked,
    no settle is declared, and the data says ``none declared`` rather than a
    guessed value.

Detectors
    The detectors each timepoint records. Scan-driven detectors (APDs, PMTs)
    produce an image only while a scan runs, so they are greyed out for
    camera timelapses. A timelapse of tilings records what the Tiling and
    Recording panels choose.

Saving
    ``One file for all timepoints`` groups the timepoints in one HDF5 or Zarr
    file; unticked, each timepoint is its own file. Folder, file name and
    format are the Recording panel's.

``Stop`` lets the current timepoint finish and then ends the timelapse.


How the light is timed
======================

At the start every laser's emission is switched off. For each camera
timepoint the controller then waits until the timepoint is due, switches the
ticked lasers on, waits the declared settle, and waits for **two new frames**
from every recorded camera. Of two frames that arrive after the lasers came
on, the second provably started exposing after it (the same proof tiling and
autofocus use for stage moves). Only then does the recording begin, and it
opens its own frame boundary, so every saved frame was exposed with the light
on. When the timepoint's last frame is written the lasers go off again. A
camera that delivers no new frame within the fresh-frame timeout fails the
timepoint instead of saving a frame that may be dark.

At the end the lasers stay **off** -- an unattended experiment must not end
with the light on -- and the powers the timelapse changed are restored.


What is written
===============

Camera and scan timelapses are written by the Recording controller in its
ordinary timelapse format: one file per timepoint (``..._time007`` for
camera, ``..._scan007`` for scans) or one grouped file, carrying the usual
``recLapseTotal``, ``recLapseIndex`` and ``recLapseIntervalS`` attributes.
Each timepoint additionally records:

``timelapse:kind``, ``timelapse:index``, ``timelapse:total``
    What the timepoint is and where it sits.
``timelapse:interval_s``, ``timelapse:interval_mode``
    The interval as the operator set it, and how it is measured.
``timelapse:scheduled_offset_s``, ``timelapse:slip_s``
    When the timepoint was due, from the start of the timelapse, and how
    late it actually started.
``timelapse:lasers``
    The lasers that lit it, with their powers, as JSON.
``timelapse:illumination_settle_ms``
    The declared settle, or ``none declared``.

``recLapseScheduledTime`` is the UTC time the timepoint was released.

A **timelapse of tilings** is the exception: each timepoint is a tiling
dataset in its own folder, under one timelapse folder in the Recording
panel's folder::

    timelapse_20261118_142501/
        timelapse.json      # the settings, and per timepoint: folder,
                            # schedule, slip, completed, tiles, message
        t0/                 # a complete tiling dataset (manifest, tiles,
        t1/                 #   mosaic), as the Tiling widget writes it
        ...

The Tiling widget's settings are read once, when the timelapse starts, so
every timepoint covers the same grid even if the panel is edited meanwhile.


Setup file
==========

The widget needs no setup section. To change what it opens with, add
``"Timelapse"`` to ``availableWidgets`` and an optional ``timelapse``
section:

.. code-block:: json

    "timelapse": {
        "defaultKind": "camera",
        "defaultTimepoints": 10,
        "defaultIntervalS": 60.0,
        "defaultIntervalMode": "start_to_start",
        "defaultFramesPerPoint": 1,
        "defaultSingleFile": true,
        "illuminationSettleMs": null
    }

Every value except ``illuminationSettleMs`` is only the widget's initial
value; each run uses what the operator entered.


Limits
======

- The light is switched by software around the frames, not by the camera's
  trigger output, so it is on for at least two frame periods plus the
  recorded frames. Gating it with the camera's fire signal would be tighter
  but is rig-specific wiring.
- One illumination state per timepoint: the ticked lasers are on together.
  A channel sequence (488 then 640 at each timepoint) is not supported.
- Camera timelapses need free-running cameras; an externally triggered
  camera is refused, as it is for the Recording widget's camera timelapse.
