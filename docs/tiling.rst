*******************
Tiling acquisitions
*******************

Tiling covers an area larger than one field of view by driving the stage
through a square spiral, acquiring one tile at each stop, and stitching them
into a single overview as the run proceeds. The overview is live: it can be
clicked to drive the stage or to mark a target to come back to.

The spiral starts at the current position and grows outward, so the run can be
stopped at any point and still leaves a filled, centred mosaic rather than a
half-finished raster.


The widget
==========

Controls are grouped by the question they answer.

Scan
    ``Tiles X x Y`` is the grid, and the total is the product — the run visits
    exactly that many positions. ``Square`` ties the two counts together;
    unlock it for a rectangular area. Unlocking never changes the area, it only
    stops the spinboxes following each other.

    ``Step (µm)`` is the stage travel between tile centres — it must be
    **smaller than the field of view**, or the tiles will not overlap and
    cannot be aligned to each other.

    ``Settle (ms)`` is the wait after each stage move before the tile is
    acquired. This is the first knob to reach for when a mosaic does not line
    up: too short and tiles are captured while the stage is still ringing.

    ``Pattern`` is the order tiles are visited in, and it decides where the
    grid sits relative to the stage:

    **Spiral** grows outward from the current position, so the mosaic is
    *centred* on where you framed it and a run stopped part-way still leaves a
    filled, centred area. Choose it when the extent is open-ended — keep going
    until something interesting appears. A rectangular spiral is the same walk
    with the positions outside the grid skipped; the stage does not stop on a
    skipped position, it makes one longer move, so the number of settles and
    focus reacquisitions is exactly the number of tiles either way. What the
    skips cost is travel, and that grows with the aspect ratio.

    **Serpentine** rasters from the current position in +X and +Y, so the stage
    is at one *corner* of the area rather than its middle. No move in the run
    is longer than a single tile step — rows reverse direction instead of
    returning across — which means the settle you tuned for one step is the
    right settle for every move. Choose it when you know the area and intend to
    finish it, and frame the stage at the corner where it begins. The trade is
    that a stopped run leaves a partial band rather than a centred area.

    The scripted
    :class:`~imswitch.imcontrol.model.workflows.tiling.TilingWorkflow` takes a
    tile count and rounds it up to a complete square instead.

Acquisition
    ``Mode`` chooses the *timing model*. **Free-running** plucks a frame from a
    continuously running camera. **Triggered** runs one scan per tile, which
    covers both a scanned detector (APD/PMT) building its image as the scan
    runs and a camera wired to the scan's trigger output — they are the same
    event, so they are the same code path.

    Which mechanism implements the chosen model follows from the detector, so a
    scan-driven detector cannot be tiled free-running and says so rather than
    silently producing nothing.

    Triggered tiling also requires the scan and the tile motion to use different
    actuators. A scan that drives the same XY positioner that tiling steps
    between tiles is refused before acquisition: use a galvo/beam scan to form
    each tile, or move between tiles with a different positioner. Letting both
    operations command one stage would corrupt the scan trajectory part-way
    through the sample.

    ``Align on`` and ``Scan`` appear only on rigs that offer a choice.
    ``Align on`` names the detector the mosaic is built and aligned from, not
    the only one saved — see :ref:`tiling-multiple-detectors`. Changing it
    discards the current overview: a different detector means a different pixel
    size, so the existing mosaic no longer maps to stage coordinates.

    ``Save tiles`` writes each tile as it is acquired — see `Saved output`_.

Alignment
    ``Align tiles`` measures where each tile really belongs instead of trusting
    the commanded stage position — see `How alignment works`_.

    ``Orientation`` decides which way the mosaic grows. There are exactly eight
    ways a camera can sit relative to the stage axes (the symmetry group of the
    square), and ``Flip X``/``Flip Y``/``Swap X/Y`` enumerate all eight. Swap is
    applied before the flips. Getting this wrong is obvious in the result: the
    mosaic fragments instead of tiling, because tiles are laid down transposed
    or mirrored relative to where their content actually continues.

Stitching
    ``Mean overlaps`` averages overlapping pixels instead of letting the newer
    tile win. ``Intensity correction`` matches each tile's brightness to its
    neighbours where they overlap.

    What it corrects is what changes from tile to tile: a lamp that flickered,
    a frame taken before the light had settled, illumination that faded during
    the run. What the detector does is left out of the comparison. Vignetting
    is fixed to the detector, so wherever two tiles overlap, one sees the
    sample through the bright side of the field and the other through the dim
    side; matched as if that were sample, every tile came out a little darker
    than the last, and the mosaic ran from bright to dark across its width.
    The correction divides out the illumination profile estimated from the
    tiles themselves (as ``Correct vignetting`` does offline, from four tiles
    on), then takes out whatever every overlap of the same direction still
    agrees on. A real change belongs to one tile, and so to only a few of its
    direction's overlaps, and is still corrected.

    Two limits follow from this. A brightness trend that runs evenly across
    the whole mosaic looks exactly like vignetting from the overlaps, so it is
    left as captured, as with the correction off; strong tile-to-tile flicker
    can leave a gentle slope of a few percent for the same reason. And with
    only one overlap in a direction there is nothing to compare against, so
    two tiles alone are left as they are. Gains are solved again at 4 and 9
    tiles, each time the count doubles after that, and once more when the run
    ends.

    Each tile keeps its own vignetting in the live overview; ``Correct
    vignetting`` in the offline reconstructor removes it.

Contrast
    The levels and colour map sit beside the overview. ``Auto contrast``
    (beside the run buttons, on by default) fits the levels to every update
    with ImageJ's *Auto* rule, so a few hot pixels saturate instead of setting
    the range and the empty canvas around the first tiles is ignored. Drag the
    levels to set your own: ``Auto contrast`` turns off and they hold while
    tiles arrive -- also across the timepoints of a tiling timelapse -- until
    it is ticked again. Right-click the colour bar for other colour maps.
    Contrast is display only; the saved tiles and mosaic are unchanged.

Offline, the **Tiling mosaic** reconstructor additionally offers ``Correct
vignetting``, which is described under `Correcting uneven illumination`_.


Navigating and marking targets
==============================

The stitched overview maps image pixels back to sample positions. The
``Click`` chooser next to the run buttons decides what a click on it does:

* **moves the stage** drives the XY positioner to the clicked spot;
* **adds a target** remembers the clicked spot in the **Targets** list under
  the overview, without moving anything.

Clicks are ignored while a tiling run owns the stage.

Targets
-------

A target is a stage position, not a pixel: the list survives a new run, a
stopped run and a change of detector, and its markers are drawn again on
whatever overview is current. A target that lies outside the overview is
listed but not drawn.

Each row shows the target's id, its stage position in µm, and where it came
from (``manual`` for a click, ``segmentation``, ``imported``). Untick ``On`` to
keep a target in the list but skip it. Select a row to ring its marker on the
overview; ``Go to`` drives the stage there, ``Remove`` and ``Clear`` edit the
list, and ``Save…`` / ``Load…`` write and read it as JSON. Scripts work with
the same list through the controller API below, and anything that finds
positions — the Segmentation processor in ImProcess, a script of your own —
feeds it with ``setTargets`` or ``addTarget`` rather than through a button
here.

Detecting cells is no longer a tiling-widget action. Segmenting a large
mosaic on the GUI thread hung the window, and its result was an overlay that
nothing in the GUI could act on. Segment the saved run in ImProcess, where
the Segmentation processor runs on a worker and can be cancelled, or in a
script with ``detect_cell_targets`` and ``TargetList.from_segmentation``
(see :doc:`scripting-wfs-workflows`), and hand the positions to the list.


How alignment works
===================

The commanded stage position is only as good as the stage's repeatability, the
configured sample-plane pixel size, and the assumption that the camera axes
line up with the stage axes. When the mosaic does not overlap cleanly, any of
the three could be at fault.

With ``Align tiles`` on, each incoming tile is cross-correlated against **every
already-placed tile it overlaps** — a spiral usually offers two to four — and
placed at the confidence-weighted consensus of what those neighbours imply.
Every measurement is kept, and when the run finishes the whole layout is solved
again by weighted least squares, with measurements that contradict the
consensus rejected on their residual.

That end-of-run solve is the part that matters most. Aligning each tile only to
what came before it accumulates error along the acquisition order, and lets a
single false correlation displace every tile placed after it — the
characteristic failure being a mosaic in two internally consistent halves with
a discontinuity between them. Fitting all the measurements at once has no such
ordering, so one bad match is outvoted rather than obeyed.

The solved layout is what reaches the overview, the saved mosaic and the
sidecar files.

Reading the report
------------------

After a run the widget reports what alignment measured::

    Tile registration: 100/100 tiles registered, RMS correction 4.2 px,
    worst 11.8 px at grid (-3, 4). Whole-run solve: 100 tiles from 337
    measurement(s), 4 rejected as inconsistent, 12 still disagreed with the
    live placement.

The *pattern* of the corrections identifies the cause:

* Corrections scattered randomly, growing with speed → stage settling or
  repeatability. Increase ``Settle (ms)``.
* Corrections proportional to the expected separation, a constant ratio away
  from 1.0 → the geometry itself is wrong. Either the sample-plane pixel size
  (magnification, or binning not accounted for) or the stage's µm calibration.
  The report says so explicitly when it sees this. A ratio near the binning
  factor is the classic signature of the latter.

A run that reports **groups nothing could be measured across** has fragmented:
some tiles share no measurable overlap with the rest, usually because that
overlap is empty background. Each group is aligned within itself but positioned
by its stage coordinates, so the groups are only as well placed relative to
each other as the stage was.


Correcting uneven illumination
==============================

``Correct vignetting`` in the offline reconstructor estimates the illumination
profile from the run itself and divides it out of every tile. It is off by
default and costs one extra read pass.

It works because shading is fixed to the *detector* — whatever the stage does,
the same corner is dim in every tile — while the sample is not: the spiral
visits a different piece of specimen at each stop. Averaging every tile in
detector coordinates therefore lets the specimen cancel and leaves the
illumination envelope, which heavy smoothing then separates from whatever
structure is left. Nothing assumes the profile is centred or radial, so a
one-sided falloff is handled as readily as a vignette.

Each tile is divided by its own mean before it is averaged in, so tiles
contribute their *shape* and not their brightness; one very bright field cannot
set the profile for the run. The profile is normalised to its median, so
correcting flattens the falloff without rescaling the mosaic.

The estimate is **skipped, with the reason in the log, rather than applied when
it cannot be trusted**:

* fewer than four usable tiles — the sample has not averaged out, so the
  "profile" would largely be the specimen. Between four and nine it is applied
  but the log says how few contributed.
* tiles that are not all the same shape, which means there is no common
  detector frame to estimate in.
* a result spanning more than 0.2–5× its median. An illumination envelope is
  gentle; something that steep is a specimen gradient, and dividing by it would
  burn that gradient into the mosaic inverted.

This is a different correction from the acquisition widget's ``Intensity
correction``, which matches whole tiles to their neighbours by a single scale
factor. That fixes tile-to-tile steps; this fixes the gradient *within* each
tile, and they can be used together.


.. _tiling-multiple-detectors:

Saving more than one detector
=============================

A run saves the detector it aligns on, plus whatever the **Recording** widget is
set to capture. That selection is the operator's existing answer to "what is my
data", and tiling reads it rather than keeping a second one — the same
arrangement as the output folder. The **Acquire with** checkboxes in **Image
Controls** choose which detectors run in live view and take part in scans;
they do not select files for recording.

Each extra detector is captured at the same stage position as the tile, before
the stage is allowed to move again, and gets the same fresh-frame proof the
aligned-on detector gets. Without that a second camera would hand back whatever
was last in its buffer: a frame from the previous tile, or one exposed during
the move. The per-tile cost is therefore set by the **slowest** camera in the
set, which is worth knowing before pairing a 5 fps camera with a 100 fps one.

Every extra detector must declare how its pixels relate to the aligned-on one,
via ``tiling.detectorTransforms``:

.. code-block:: json

   "tiling": {
       "camera": "APDred",
       "detectorTransforms": {"Camera": "identity"}
   }

If either of two registered APDs may be selected as the alignment detector for
a run, declare both. The entry for the detector currently used for alignment is
ignored, while the other one authorizes that detector to join the saved set:

.. code-block:: json

   "detectorTransforms": {
       "APD1": "identity",
       "APD2": "identity"
   }

**A detector with no declaration is still saved, and recorded as unknown.**
Writing pixels needs a stage position and nothing more, so not knowing how two
detectors relate is no reason to discard one of them — that question only
arises when something tries to overlay them, which happens offline where there
is freedom to measure or declare the relationship. Of "saved it without
knowing" and "did not save it", only the second cannot be undone later.

What the manifest records is the difference between the two: ``identity`` is an
assertion about the rig that whoever wrote it owns, and ``unknown`` is the
absence of one. A reader can then default to identity *and say so* rather than
silently. Sharing a stage position establishes the *tile grid*; it says nothing
about whether two detectors agree pixel for pixel, since sensor origin, ROI,
orientation, rotation and optical-path offsets all differ independently.

A detector that declares ``identity`` while reporting a different pixel size or
frame shape is contradicting itself; that is logged as a warning and the data
is still saved, because the contradiction affects how the tiles may be combined
rather than whether they are worth keeping.

Detectors that cannot participate are dropped with a reason in the log rather
than silently included — a scan-driven detector in free-running mode, for
instance, since nothing would clock it.

All of them share one solved layout, because they were all acquired at the same
positions. Offline, the **Tiling mosaic** reconstructor's **Output source:**
picker chooses which to assemble (*Aligned on*, the default, is the detector
the run aligned on); the geometry is identical whichever you pick.


Saved output
============

``Save tiles`` writes one folder per run, ``tiling_<YYYYMMDD_HHMMSS>``, into the
Recording widget's output folder, or into ``tiling.measurementsRoot`` if the
setup file sets it. It contains:

Individual tiles
    Files in the configured ``saveFormat``: OME-TIFF (the default), HDF5 or
    Zarr. OME-TIFF carries each tile's stage position in the standard
    ``Plane/@PositionX|Y`` fields, so an OME-aware reader can place it directly.
    HDF5 and Zarr are useful inside ImSwitch2 but are not interchangeable with
    OME-TIFF in external stitching programs.

``TileConfiguration.txt``
    The layout format used by Fiji's Grid/Collection Stitching plugin and by
    BigStitcher. Not an OME standard, but the de-facto one for handing a tile
    set to a stitcher.

``tiles.json``
    Everything the other two cannot express: each tile's grid index, its
    commanded stage position, the placement it ended up at, and the acquisition
    settings the mosaic geometry depends on. Each tile can also carry ``axes``
    (the logical array axes), ``stored_axes`` (the axes physically present in
    the file), and ``shape``. These distinguish, for example, a line-step
    channel stack ``CYX`` from a Z stack ``ZYX`` instead of interpreting every
    leading dimension as depth. Older manifests without these descriptors are
    still accepted using the legacy assumptions.

    For a run that records a payload per tile, a payload's descriptors come
    from its own recording: the writer names the axes from the recording's
    acquisition layout wherever the layout gives every stored axis one OME
    letter, and falls back to the frame-count guess only where it does not
    (a camera frame stream over a raster, say). Snapshot tiles still use the
    manifest's own guess from the Z step.

Per-tile payload recordings
    Each payload recorded for a tile identifies itself without the manifest:
    its acquisition layout carries a ``tile`` partition (the tile's ordinal
    and the run's tile count), and its shared attributes carry the same
    ``Tiling:grid_x``/``grid_y``/``stage_x_um``/``stage_y_um`` a snapshot tile
    carries. Such a run is the Recording widget's scan lapse with tiling
    placing the stage, and the lapse's legacy ``recording:num_timepoints``
    attribute still counts its items; the partition kind is what says they
    are tiles, not timepoints.

The stitched mosaic is written alongside them. All of it is written even for a
stopped or partial run — whatever reached disk should still be a usable dataset
rather than orphaned files.


Reassembling a saved run
========================

The **Tiling mosaic** reconstructor in ImProcess rebuilds a saved run offline,
where there is no latency budget and every tile is in hand at once. Open any
file from the run's folder; it finds the manifest beside it. The reconstructor
picker still offers what can read the file you selected (**View only**, for
instance), and choosing one reopens that file.

It lays the tiles out from the **commanded stage coordinates**, not the pixel
positions saved in the manifest. Those are recorded after the live alignment
pass, so they carry whatever that pass did — and a bad live correction cannot
be undone offline, because the correlation windows are cropped from those very
positions. Untick **Start from stage positions** to reassemble exactly the
layout that was saved.

Options:

``Refine alignment``
    Correlate every overlapping pair and solve the layout globally, as above.
    Slower than trusting the recorded positions, and better than anything the
    live pass can do.

``Mean overlaps``
    Average overlapping pixels. Unchecked, later tiles overwrite earlier ones,
    which leaves visible seams but no ghosting.

``Max shift (px)``
    Reject corrections larger than this, which are usually false matches on
    repeating structure. ``auto`` derives a limit from the tile size.

``Project volumes to 2D``
    Maximum-project a volumetric mosaic instead of keeping every plane.


A timelapse of tilings
----------------------

The Timelapse panel's *tiling* kind runs a tiling at every timepoint. It
writes one folder, ``timelapse_<YYYYMMDD_HHMMSS>``, holding ``timelapse.json``
(the plan, the Tiling panel's settings, and one record per timepoint run so
far: its folder, its scheduled start in seconds, whether it completed) and one
tiling run per timepoint, ``t0/ t1/ ...`` (zero-padded to the width of the
timepoint count), each exactly as above. The manifest is rewritten after every
timepoint, so an interrupted run lists fewer records than planned: the others
are *missing*; a timepoint whose tiling was stopped or failed is *incomplete*.

The **Tiling mosaic** reconstructor recognises it. Open a tile of any
timepoint, a timepoint folder, ``timelapse.json`` or the timelapse folder, and
it reports how many timepoints there are, which are complete, incomplete or
missing, their time stamps, and whether their grids agree (detector,
alignment-tile and payload shape, grid, pixel size, orientation, stage
positions); a timepoint whose grid differs is named and left out. The
**Timepoints** box under the parameters chooses *This timepoint only*, *All
timepoints* (the default for a timelapse) or a *Range*, with every n-th, and
what to do about timepoints that are missing or incomplete: leave them out, or
keep ``T`` regular with a plane of zeros or NaN. Either way they are listed.

All timepoints give **one** result, ``T, [C, Z,] Y, X``. ``T`` is scaled from
the recorded start times (their typical step, in seconds; the real time of every
plane is in the metadata and, when saved, in each plane's ``DeltaT``). Every
plane is drawn on the **same canvas** -- one origin, one shape, the union of
what every timepoint occupies -- so nothing has to be cropped or shifted
afterwards.

``Align across time`` decides how the tiles are placed from one timepoint to
the next (**Refine alignment** and **Max shift** still apply inside a mode):

``Stage positions only``
    The positions the manifests record, no registration. Identical geometry.
``One shared layout`` (default)
    One tile layout for every timepoint. For each overlapping pair of tiles the
    cross-correlation surfaces of the overlaps are *added over the
    timepoints*, and the peak of the sum is the pair's offset; the offsets go
    through the same robust global solve as a single tiling. The sum is the
    maximum-likelihood estimate of one common offset under independent noise
    -- its peak grows with the number of timepoints while the noise floor grows
    with its square root -- so a sample too faint for any single timepoint to
    align is aligned by the series, where a median of per-timepoint offsets
    would still need most of them to be right. A rigid drift moves both tiles
    of a pair together and does not blur the sum. ``Layout from: Reference
    timepoint only`` applies one timepoint's own registration to all.
``Shared layout + drift``
    The shared layout plus one rigid XY shift per timepoint against the
    **reference timepoint** (the sample or stage moving between timepoints).
    A few tiles spread over the grid are each correlated with the same tile at
    the reference and combined by a confidence-weighted median; ``Smooth
    drift`` takes a running median over timepoints. The canvas is padded once
    for the whole drift range. The correction applied to every timepoint is a
    column in the Results table, a trace in the Graph panel and in the
    metadata.
``Each timepoint on its own``
    Today's registration, per timepoint. Tiles may jitter from one timepoint
    to the next in this mode; they are still placed on the common canvas.

The result is one array of ``T`` float32 planes, held in memory: its size is
the ceiling. The memory estimate counts every plane; a run above the
reconstruction budget is refused with the planes and bytes named (reconstruct a
range, every n-th timepoint, one channel, or max-project Z) until the estimate
is confirmed. Planes are assembled one timepoint at a time straight into the
output, so only one timepoint's tiles are read at once, and Cancel stops
between tiles. Correlation accumulators are held in batches of 256 MiB; more
pairs than that cost one more read pass over the alignment images.

The result's provenance (in the OME description, and the history of the result)
names ``timelapse.json`` and every tiling manifest used with their
fingerprints, the alignment mode, the layout that was applied (tile positions;
the timepoints it was estimated from), the drift per timepoint, the canvas, and
which timepoints were left out or filled and why, so the run can be replayed.
It saves as OME-TIFF, HDF5 or Zarr. As a workflow step, bind the source to
``timelapse.json`` (``source_kind: tiling-manifest``); the options are the
step's ``time_alignment``, ``layout_source``, ``reference_timepoint``,
``drift_smoothing``, ``max_drift_px`` and the series options ``_series``,
``_series_first``, ``_series_last``, ``_series_step``, ``_series_missing``
(see :doc:`improcess`). ``examples/improcess_workflows/tiling_timelapse_mosaic.yaml``
is a worked example.

Volumetric runs keep their Z planes; tiles are aligned on their projections and
the whole stack moves together, since the tiles share a Z range.

From a script, :func:`~imswitch.imcommon.algorithms.tile_mosaic.assemble_dataset`
does the same thing in one call:

.. code-block:: python

   from imswitch.imcommon.algorithms.tile_mosaic import assemble_dataset

   mosaic, dataset, tiles_moved = assemble_dataset(
       '/data/tiling_20260804_151204', refine=True
   )

Progress and diagnostics are logged at each stage — tiles read, pairs
correlated, links rejected, and the mosaic's dimensions and memory cost before
that memory is requested. A hundred 2000×2000 tiles make a ~15000×15000 mosaic
and take roughly fifteen seconds.


Controller API
==============

The Tiling controller exports these methods through ImSwitch2's generated API:

``startTiling()`` / ``stopTiling()``
    Start with the values currently shown in the widget, or request cancellation
    of the active tiling run.

``setTileLabel(label)``
    Change the widget's status label.

``getStitchedImage()``
    Return the current stitched overview as a NumPy array, or ``None`` before an
    overview exists.

``getRegistrationSummary()``
    Return the last alignment report as text, or an empty string when no report
    exists.

``isTilingRunning()`` / ``getLastTilingFolder()``
    Whether a run is in progress (true from ``startTiling()`` until its files are
    written and the stage is back), and the folder the last saved run wrote its
    ``tiles.json`` to. Together they let a script take an overview and wait for it:
    ``startTiling()`` then ``waitUntil(lambda: not api.imcontrol.isTilingRunning())``.
    The manifest also names the stage (``xy_positioner``, ``xy_axes``), so its
    ``stage_um`` positions can be driven back to.

``getTargets()``
    The target list as dicts: ``id``, ``stage_xy`` (x, y in µm), ``enabled``,
    ``source``, ``row_col``, ``z_um``, ``props``. For a target workflow, build a
    :class:`~imswitch.imcontrol.model.workflows.target.TargetList` with
    ``TargetList([Target.from_dict(t) for t in api.imcontrol.getTargets()])``.

``setTargets(targets)`` / ``addTarget(x_um, y_um, enabled=True, source='imported')``
    Replace the list (each entry needs ``stage_xy``; ids are assigned afresh)
    or append one target and get its id back.

``removeTarget(targetId)`` / ``setTargetEnabled(targetId, enabled)`` / ``clearTargets()``
    Edit the list; the widget follows.

``goToTarget(targetId)``
    Drive the XY stage to a target. Refused while a tiling run owns the stage.

``saveTargets(path)`` / ``loadTargets(path)``
    The same JSON file the widget's ``Save…`` and ``Load…`` use.


Spots per tile
--------------

The ImProcess processor ``tile-spots`` runs on a mosaic from the
``tiling-mosaic`` reconstructor and counts diffraction-limited spots on the
tiles behind it (the SMLM net-gradient detection ``spot-count`` uses), ranking
the tiles by count with the stage position to revisit each. The workflow script
``scripts/workflows/event_trigger/01_spot_tiles_confocal.py`` does the whole loop
on the microscope: overview, spots per tile, a confocal scan (the Scan panel's
scan) on every tile with spots, most spots first.

Tiling troubleshooting
======================

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Symptom
     - Likely cause
   * - Mosaic grows in the wrong direction, or tiles do not continue into each
       other at all
     - Wrong ``Orientation``. Try ``Swap X/Y`` first, then the flips.
   * - Tiles overlap by the wrong amount, consistently
     - Sample-plane pixel size is wrong, or camera binning is not accounted
       for. The registration report names this when it sees it.
   * - Tiles smeared, or showing the previous position
     - ``Settle (ms)`` too short; in free-running mode, also check the camera
       frame rate against the tile rate — the log warns when tiles fell back to
       a buffered frame.
   * - Alignment refuses to run
     - The step is at least a full field of view, so tiles do not overlap.
       Reduce ``Step (µm)``.
   * - One region is fine, another is fine, the join between them is not
     - The layout fragmented. Check the report for *groups nothing could be
       measured across*; increase overlap so neighbouring tiles share more
       structure.


Stage tilt Apply: safety limits
-------------------------------

**Apply** commands the compensation Z from a recorded plane.  These checks
always run, before anything moves, whatever **Pre-check** says:

* the calibration's X and Y slopes are within **Max slope** (default
  15 µm/mm), the predicted Z never leaves the run's start Z by more than
  **Max ΔZ** (default 30 µm) and never steps more than **Max step**
  (default 5 µm) in one move -- the panel shows the calibration's date, RMS and
  slopes and the predicted maximum excursion before **Start**;
* every tile lies inside the calibrated XY area (no extrapolation), and the
  run's span fits inside the calibrated span;
* a Z positioner that needs referencing is referenced, and every target is
  inside its output (DAC / NI-DAQ voltage) range.

A calibration is only selectable when its fit passes the quality gate: at
least four points, RMS ≤ 1 µm and worst residual ≤ 3 µm.  Recording rejects
outliers (a residual above 3.5 robust sigmas and 0.2 µm, at most a quarter of
the points) and reports them in ``tilt_fit.json``.  An older file that lacks
the residuals the gate needs is refused with a request to re-record it.  A
calibration recorded on another setup, without a setup name, or older than
30 days needs an explicit confirmation when it is chosen; Apply never starts
by itself.

On the way back to the origin (end, Stop or error) XY returns in short
segments with Z following the plane, so the objective tracks the sample
surface the whole way: restoring Z first, or XY first, would each push the
objective toward the sample by the full predicted ΔZ on one sign of tilt.  If
the XY return fails, Z stays matched to the XY that was reached; the popup
and the run result say where Z was left.  A Timelapse (workflow) run reports
in its run result instead of a popup.  The XY stage and the compensation Z
are reserved for the run, so a script or another action trying to move them
is refused cleanly instead of colliding.  A run with neither **Build mosaic
preview** nor **Save tiles** is refused (it would keep nothing), except a
calibration-only recording.

Focus lock during stage moves
-----------------------------

**Pause focus lock during moves** (Acquisition group, off by default) is for
free-running tiling with an engaged focus lock.  Before each XY move the lock
is paused (Z held, frames ignored); after the move and the settle time it is
relocked to the kept setpoint in bounded steps (:ref:`focuslock-hold-relock`)
before the tile is taken.  Each tile's status -- ``locked``, ``held``,
``relock-failed`` or ``relock-cancelled`` -- goes into ``tiles.json`` under
``focus_lock_tiles`` and into the run result.  A failed relock leaves Z at the
last good position, the tile is taken there and marked, and the next tile
tries again; the run message names the tiles that were not locked.  The return
to the origin is paused the same way and relocked at the end; after a Stop the
lock is left paused (Z held) and **Re-lock** in the Focus Lock panel brings it
back.  **Stop** cancels a relock in flight.  Triggered tiling ignores the
option: its scans already suspend and reacquire the lock.

With tilt **Record**, a point is sampled after the relock, so it records the Z
the lock holds only when the relock succeeded (otherwise the sample is marked
by the lock state).  **Apply** on the focus lock's own actuator still requires
the lock to be unlocked, so the option has nothing to pause there; Apply on a
different actuator and the pause option work together.

.. seealso::

   :doc:`setupinfo-reference` for the ``tiling`` configuration section, and
   :ref:`focuslock-scan-arbitration` for how the focus lock behaves across a
   tiling run.
