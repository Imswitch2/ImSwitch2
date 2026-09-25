*********
ImProcess
*********

``ImProcess`` is Imswitch2's post-acquisition processing module.  It is the
generalized successor of the older ``ImReconstruct`` module: where
``ImReconstruct`` was hard-wired to MoNaLISA SIM reconstruction,
``ImProcess`` exposes a small plugin system so that every acquisition
Imswitch2 can produce — MoNaLISA, STED, FLIM, confocal, widefield,
WidefieldSTARSS, SNOUTY lightsheet — can be opened, viewed and
post-processed with the same shell.

Why the rename
==============

The audit that opened Milestone 12 (see
``docs/design/plans/imreconstruct-2-0.md`` and the per-layer audit
appendices alongside it) showed that ~70 % of the old ``imreconstruct``
code was already modality-agnostic.  The MoNaLISA-specific bits were concentrated in a
handful of files.  The rename signals the new scope: this module is
about *processing in general*, not just SIM reconstruction.  The
MoNaLISA pipeline lives on as one plugin
(:py:mod:`imswitch.improcess.reconstructors.monalisa`).

Launching ImProcess
===================

ImProcess can run in three modes:

1. **As an Imswitch2 module** — alongside ``imcontrol``, started by the
   main Imswitch2 launcher.  This is the default when ``modules.json``
   lists ``"improcess"`` in the ``enabled`` array.
2. **Stand-alone with no setup** — direct entry point, useful when you
   only want a viewer / post-processor on your laptop with no
   microscope attached::

       python -m imswitch.improcess

   In this mode the registry falls back to the ``view-only``
   reconstructor + the ``drift-correct`` processor.
3. **Stand-alone with a minimal setup** — same launcher, but a
   processing-only setup file selects the plugins you want enabled
   (e.g. MoNaLISA). See :ref:`improcess-processing-presets` below.

Data ingest
===========

Any file Imswitch2 / Imcontrol can write is native to ImProcess:

* **HDF5** (``.h5``, ``.hdf5``) — the default Imswitch2 recording format
* **Zarr** (``.zarr`` directories)
* **TIFF** (``.tif``, ``.tiff``)

There are three ways to open data:

* **Drag and drop.** Drag one or more files onto the main window.
  Files with multiple datasets prompt a dataset picker.
* **Folder watcher.** Point the watcher pane at a directory and tick
  *"Watch and run"* — newly arrived files are loaded automatically and
  optionally reconstructed.
* **Manual load.** Use the *Load data* button or *File* menu.

Plugin architecture
===================

ImProcess defines two plugin shapes:

* :py:class:`~imswitch.improcess.reconstructors.base.Reconstructor`
  Turns a raw :py:class:`~imswitch.improcess.model.DataObj` into a
  :py:class:`~imswitch.improcess.model.result.ProcessingResult`.  One per
  dataset.  Examples: ``monalisa``, ``view-only`` (the no-op
  pass-through), ``widefield-starss`` and the SNOUTY reconstructors.
* :py:class:`~imswitch.improcess.processors.base.Processor`
  Operates on a ``ProcessingResult`` and returns one or more new results.
  Stackable.
  Modality-agnostic by design — a single ``drift-correct`` works for
  any plugin output that has a time axis, and ``frc`` works on 2D image
  planes.  ``projection`` collapses any selected axis with max / mean /
  sum / median / standard-deviation modes.  ``stack-split`` and
  ``channel-split`` can publish several results from one source result.
  ``segmentation`` thresholds a
  2D plane and extracts connected regions.  Future processors include lifetime
  overlays.

Both shapes are registered with a :py:class:`PluginRegistry`.  The
registry is populated at startup, either from a config block or from
standalone defaults.

File and image toolbars
=======================

The main window includes always-visible *File*, *Image* and *Analysis tools*
toolbars for common operations that should be available regardless of the
active reconstructor.  The *File* toolbar exposes quick data loading, virtual
data loading and saving the active reconstruction.  *Virtual load data* opens
the selected TIFF/OME-TIFF, HDF5 or Zarr dataset as a lazy current-data source
so the raw-data panel can show the mean image and individual frames without
materializing the whole stack first.  Normal *Quick load data* keeps the legacy
eager behavior.

The *Image* toolbar and matching *Image* menu provide viewer-level operations:

* *Auto contrast* — percentile-based display-level stretch on the active image
  layer.
* *Brightness/Contrast...* — a modeless histogram dialog with numeric minimum
  and maximum controls, sliders, saturated-pixel auto contrast and whole-stack
  versus current-view histogram scope.
* *Reset contrast* — reset the active layer to the finite data range.
* *LUT* — set the colormap, including ``hot``, for the active image layer and
  persist it on the active result or display-layer component.
* *Channels...* — show display-layer channel controls with per-layer
  visibility and LUT settings.
* *Duplicate* — create a new array-backed result from each selected result.
* *Crop/Substack...* — create a ranged subset with first/last/step controls for
  every result axis.
* *Max projection* — create a max projection using the projection processor's
  default stack-axis choice.  The **Projection** panel exposes the rest of
  ImageJ's *Z Project*: the axis, the statistic (max / mean / sum / median /
  standard deviation) and a *First slice* / *Last slice* range, 1-based and
  inclusive, defaulting to the whole axis.  Projecting one axis of a
  hyperstack keeps the others, so a ``TZYX`` stack projected on ``Z`` stays a
  ``T`` series.
* *Split stack* — split each selected stack into one result per plane along the
  selected stack axis.
* *Split channels* — split a ``C``, ``Channel`` or ``Base`` axis into one
  result per channel.
* *Merge channels...* — merge loaded results into a new ``C``-axis channel
  stack. The dialog lists every loaded result (and the components of
  multi-layer results), pre-checks the reconstruction-list selection, and
  merges in the listed order, which is the channel order. *Create composite*
  publishes the merge as coloured display layers instead of a greyscale stack
  with a channel slider. Inputs are whole results or named components, so the
  channels of one multi-layer result can be merged among themselves; the
  planes of a plain stack are not listed individually — split it first with
  *Split stack*, which the dialog says when a single stack is checked.
* *Stack/Combine…* — stack same-shaped selected results along a new axis, or
  concatenate compatible results along an existing axis. Axis labels, pixel
  scales and scale units must agree.
* *Image calculator…* — combine exactly two compatible selected results with
  pixel-wise add, subtract, multiply, divide, minimum, maximum, average or
  absolute difference operations.
* *Make composite* — create a composite result that renders a channel-like axis
  as independently-scaled colored display layers.
* *Make RGB* — bake a channel-like axis into a channel-last RGB visualization
  result for display/export.
* *Reset view* — restore the reconstruction viewer camera.

The contrast, LUT and channel-visibility operations are display-only: they
update the Napari image layer and the active
:py:class:`~imswitch.improcess.model.result.ProcessingResult` display settings,
including per-display-layer settings for composite outputs, but do not alter
pixel data.  Duplicate, crop/substack, max projection, merge channels,
stack/combine, image calculator, make composite and make RGB publish new
``ProcessingResult`` objects into the reconstruction list. Split stack and
split channels publish multiple ``ProcessingResult`` objects and make the final
split result current.

.. _improcess-multi-result-operations:

Operating on several reconstructions
------------------------------------

Two different things are meant by "several results", and ImProcess offers
both:

* **One operation consuming several results.** Merge channels, Stack/Combine
  and the Image calculator take two or more results and produce one output.
  They are enabled whenever at least two results are loaded — not only when a
  compatible multi-selection exists — and open a picker over everything
  loaded, with the reconstruction-list selection pre-checked. Inputs that
  cannot be combined disable OK and name the reason (differing shape, axis
  labels, pixel scales or scale unit) instead of leaving a silently dead
  button. Two-image FRC works the same way and can compare two separate
  reconstructions.
* **One operation applied to each of several results.** Duplicate, max
  projection, split stack, split channels and make composite run over every
  selected result, skipping the ones the operation does not apply to.
  Processor panels offer the same thing through their *Apply to* selector
  (*Current result* / *Selected results* / *All results*), so a denoise or a
  drift correction can sweep a whole session's reconstructions in one run.
  One input failing does not discard the outputs of the others; the panel
  reports how many failed and why. Crop/Substack and Make RGB stay
  single-result, because their dialogs are parameterised by one result's axes.

Both paths publish through the usual result pipeline, so the bulk-publish
confirmation still guards against flooding napari with layers.

The *Analysis tools* toolbar keeps Fiji-like panel shortcuts visible for Graph,
Profile, ROI manager, ROI statistics, Projection, Segmentation, Metadata and
Results.
Runtime-backed buttons use the same loading path as the *Load tool* combo, so
opening a panel registers its processor when needed, raises an existing dock
when it already exists and preserves the runtime-loaded panel in the
dock-layout state.  The Results button raises the built-in results-table dock
directly.

Every registered built-in or drop-in processor is also available through
*Load tool*. Processors with a dedicated panel open that panel; the rest open a
generic parameter panel for the active result. This runs one processor at a
time and publishes its output back to the reconstruction list. It is not yet a
saved, automatically executed multi-step processing chain.

Reconstructors have the same runtime path.  The reconstructor picker at the
top of the Parameters dock offers only the reconstructors that are registered,
and the setup file's ``processing.reconstructors`` list decides which
built-ins those are — so a setup that names only MoNaLISA never shows the
others.  **Tools → Load reconstructor** lists every reconstructor ImProcess
knows about but has not loaded (the built-ins the setup file did not name,
plus any drop-in that is discovered but not registered) with a one-line
description; picking one registers it for the session, adds it to the picker
and makes it active.  Nothing is written to the setup file: to keep a
reconstructor across sessions, add it to ``processing.reconstructors`` (the
config editor offers every known id, drop-ins included).

Toolbar icons are selected through ImProcess semantic action IDs and rendered
with QtAwesome when available, with Qt standard icons as a fallback.  This
keeps icon choices centralized while allowing each action to retain its
existing text, tooltip and menu entry.

Results list vs. napari layers
===============================

The reconstruction viewer has two visible lists — the **reconstruction list** (right
pane) and the **napari layer list** (napari's built-in layer controls, shown by
default and hidden by setting ``"napariLayerControls": false`` in the
``processing:`` config block).  They serve complementary purposes:

* The **reconstruction list** is the *registry* of processing results.  Each
  entry holds:
  
  - The result data and metadata (axis labels, scales, units)
  - Per-result display settings (contrast levels, colormap)
  - The result's view modes (standard / bottom side / left side / custom XY/XZ/YZ)
  
  These settings are persisted when you adjust contrast or LUT, and restored
  when you click back to that result.

* The **napari layer list** is the *presentation* of the currently selected
  result.  It contains:
  
  - The main image layer, now named after the selected result (not the generic
    ``Reconstruction``)
  - Derived overlays: per-component display layers (composite results with
    independent channel scaling), segmentation mask layers, multicolor preview
    layers, and so on

**Single source of truth: the active napari layer.** Tool panels (Profile, ROI
Manager, FRC, etc.) operate on the *active* (highlighted) napari layer.  When
you select a result from the reconstruction list, the viewer automatically:

1. Loads the result's data into the main image layer
2. Renames the layer to match the result's name
3. Re-activates the main layer so it becomes the target for tool operations

This keeps "which image am I processing?" unambiguous: it's always the active
napari layer, and the reconstruction list keeps that layer aligned with your
selection.

**The producer/tool rule.** Every panel is one of two things:

* *Producing panels* create new ``ProcessingResult`` entries in the
  reconstruction list, never floating napari layers.  Projection, FRC,
  Segmentation, PSF resolution and Colocalization run through the generic
  run→publish pipeline (``sigRunRequested`` →
  ``ResultProcessorController`` → ``sigResultProduced``), operating on the
  *selected result*; Multicolor's Register/Apply publish
  registration/aligned results through its own producing-panel bridge.
* *Interacting/measuring tools* (ROI Manager, ROI stats, Profile) operate on
  the *active napari layer* and display their measurements in place; they do
  not create results.  All of them resolve their source layer through the
  shared ``imswitch.improcess.layer_selection`` helper, so what counts as an
  image source cannot drift between tools.  They also re-measure when the
  selected result changes: the ROI stays where it is and the numbers follow
  the result now under it, rather than lingering from the previous one.

The Profile tool can draw line and rectangle ROIs, plot the sampled profile,
and optionally overlay fitted curves.  Available profile fits are no fit,
single Gaussian, two independent Gaussians with center-distance reporting,
and a single exponential decay/rise model.  Fit metrics are included when the
profile is pushed to the results table or saved as CSV.

*Z profile* answers the other question a stack raises — how intensity varies
*through* it rather than across the field, ImageJ's *Plot Z-axis Profile*.  It
plots mean intensity over a drawn rectangle against the stack axis, or over
the whole frame when no rectangle is drawn.  The axis is chosen from the
result's own labels (``Z``, else ``T``, else the first non-spatial axis with
more than one plane) and is plotted in that axis' physical units when it has a
scale, falling back to slice number.  On a hyperstack the other axes stay
where the viewer is, so profiling ``Z`` on a ``TZYX`` result profiles the
timepoint on screen.  Fits, *Measure Δx*, *Push to table* and *Push to graph*
all work on it exactly as they do on the in-plane profiles — an exponential
fit over a ``T`` profile is a bleaching curve.

*Push to graph* sends the profile and its fit to the Graph panel, opening it
if needed.  Pushed plots are **pinned**: they stay when the selected result
changes, where the Graph's own content is replaced by each result's plots.
That is what makes two profiles comparable — measure one reconstruction, push,
select the next, measure, push — and the Graph's *Overlay* button then draws
every plot it holds in one set of axes.  *Clear pushed* forgets them again.
Rows pushed to the Results table carry a ``source`` column naming the result
they were measured on, since that table accumulates across results.

Ephemeral preview layers (the segmentation preview, multicolor's
split-boundary and detected-bead overlays) are the exception: they are
tuning/diagnostic overlays, restore the previously active layer and are
excluded from tool source resolution, so a preview can never become its own
input.

To see which result produced the current image, check the main layer's name or
the highlighted item in the reconstruction list — after clicking a result, those
two always agree.

Built-in plugins
----------------

======================= ============== ====================================================
ID                      Type           Purpose
======================= ============== ====================================================
monalisa                Reconstructor  MoNaLISA point-scanning SIM (Windows + CUDA DLL)
view-only               Reconstructor  Pass-through; raw frames wrapped as a result
widefield-starss        Reconstructor  H/V WidefieldSTARSS anisotropy maps and region metrics
snouty                  Reconstructor  SNOUTY / OPM / MS-RESOLFT lightsheet deskew
snouty-projections      Reconstructor  Fast SNOUTY projection-preview stack
smlm-localizer          Reconstructor  SMLM localization table from camera frame stacks
beadrec                 Reconstructor  Raster bead reconstruction from a camera frame stream
tiling-mosaic           Reconstructor  Offline assembly and refinement of saved tiling datasets
time-lapse              Reconstructor  A saved camera or scan time lapse as one lazy T-stack
drift-correct           Processor      FFT cross-correlation drift correction with drift trace plots
projection              Processor      Generic max/mean/sum/median/std axis projections
stack-subset            Processor      Crop/substack by labeled axis ranges
stack-split             Processor      Split a stack axis into one result per plane
stack-combine           Processor      Stack results on a new axis or concatenate an existing axis
channel-split           Processor      Split a channel-like C/Channel/Base axis into one result per channel
channel-merge           Processor      Merge compatible grayscale results into a C-axis channel stack
make-composite          Processor      Render a channel-like axis as colored display layers
make-rgb                Processor      Bake a channel-like axis into channel-last RGB visualization data
resize                  Processor      Resize Y/X while preserving calibrated physical extent
convert-type            Processor      Convert to 8-bit, 16-bit or 32-bit float, optionally rescaled
transform               Processor      Rotate or flip every Y/X plane
filter                  Processor      Gaussian, median, mean and unsharp spatial filtering
subtract-background     Processor      Rolling-ball background removal, optionally returning the background
math                    Processor      Constant arithmetic and unary transforms on one image
image-calculator        Processor      Pixel-wise arithmetic between two compatible results
segmentation            Processor      Threshold + connected-component labels and ROI export
label-morphology        Processor      Fill, erode, dilate, open, close or watershed 2D label masks
psf-resolution          Processor      2D Gaussian bead/PSF FWHM and sigma measurements
colocalization          Processor      Pearson, Manders and overlap channel colocalization metrics
frc                     Processor      Fourier ring correlation and single-image FRC resolution estimates
multicolor-registration Processor      Three-color X-strip bead calibration and alignment HDF5 export
multicolor-apply        Processor      Apply saved three-color X-strip alignment to deskewed sample volumes
denoise                 Processor      UNet / UNet+RCAN neural-network denoising (requires torch)
smlm-render             Processor      Render a localization table into a super-resolved image/volume
smlm-filter             Processor      Filter localizations by photons, lateral sigma and frame range
smlm-drift              Processor      Segment cross-correlation drift correction with drift trace plots
smlm-group              Processor      Link blinking repeats into photon-weighted merged localizations
table-to-localizations  Processor      Promote a points table to localizations with an explicit column mapping
======================= ============== ====================================================

Processor categories and compatibility
======================================

Built-in processors carry a category label used by the runtime tool loader,
and compatibility is decided by ``Processor.accepts(result)``, which checks
two things in order:

1. **Semantic kind.** Every ``ProcessingResult`` declares a ``kind`` — one of
   ``image`` (the default), ``labels``, ``table``, ``curve``,
   ``localization``, ``rgb`` or ``composite`` — and every processor declares
   the ``kinds`` it accepts (default ``("image",)``).  This is what keeps a
   colocalization *table* (a 2D array of metric values) from being offered to
   the segmentation processor just because its shape fits.  Component inputs
   derived from display layers inherit the layer's semantics: a
   segmentation's labels layer is a ``labels`` input, a composite's channel
   is an ``image`` input.
2. **Shape/axis contract**, encoded in each processor's ``applies_to()``.

UI code (the image toolbar, the generic result-processor panels) must gate on
``accepts()``; ``applies_to()`` alone cannot tell a metrics table from an
image.  The table below documents the shape/axis contracts as implemented by
``applies_to()`` and ``apply()``.  "Image-like" means a
``ProcessingResult.data`` array whose last two axes are treated as spatial
``Y, X`` unless the processor documents a stricter axis contract.  Processors
accept kind ``image`` unless noted; ``stack-subset``, ``projection``,
``stack-split``, ``channel-split``, ``make-rgb``, ``drift-correct`` and
``colocalization`` also accept ``composite`` (composite data is the source
intensity stack), and the SMLM processors (``smlm-render``, ``smlm-filter``,
``smlm-drift``, ``smlm-group``) accept only ``localization``.
``label-morphology`` accepts only ``labels``.

.. list-table::
   :header-rows: 1
   :widths: 18 18 30 34

   * - Processor
     - Category
     - Accepts
     - Output and chaining notes
   * - ``stack-subset``
     - Dimensions and channels
     - Any rank >= 2 result; ranges are addressed by axis label or index.
     - ``ArrayProcessingResult`` with the same rank and sliced data.  Lazy
       sources stay lazy when possible; stepped axes get scaled axis spacing.
   * - ``projection``
     - Dimensions and channels
     - Any rank >= 2 result.  Auto prefers ``Z``, then ``T``, then ``C``.
     - ``ProjectionResult`` with the projected axis removed and a histogram
       plot payload.
   * - ``stack-split``
     - Dimensions and channels
     - Rank > 2 image-like stacks; auto prefers ``Z``, then ``T``, then a
       remaining non-spatial axis.
     - Multiple ``ArrayProcessingResult`` objects, one per plane, with the
       split axis removed.
   * - ``channel-split``
     - Dimensions and channels
     - Rank >= 3 result with a ``C``, ``Channel``, ``Channels`` or ``Base``
       axis containing more than one plane.
     - Multiple ``ArrayProcessingResult`` objects, one per channel/base, with
       the channel-like axis removed.
   * - ``channel-merge``
     - Dimensions and channels
     - Two or more selected rank >= 2 results with identical shape, axis
       labels and compatible axis scales.
     - ``ArrayProcessingResult`` with a new leading channel axis, default
       label ``C``.
   * - ``stack-combine``
     - Dimensions and channels
     - Two or more rank >= 2 results. Stacking requires identical shape;
       concatenation permits different lengths only on the chosen join axis.
       Axis labels, pixel scales and scale units must agree.
     - ``ArrayProcessingResult`` with either a new leading axis or an enlarged
       existing axis. A new axis label may not duplicate an existing label.
   * - ``make-composite``
     - Visualization
     - Rank >= 3 result with a multi-plane ``C``, ``Channel``, ``Channels``
       or ``Base`` axis.
     - ``CompositeResult`` retaining source data but exposing one independently
       scaled display layer per channel.  Component layers can be selected as
       explicit processor inputs.
   * - ``make-rgb``
     - Visualization
     - Rank >= 3 result with a multi-plane channel-like axis.  More than three
       channels require explicit channel selection.
     - ``RGBResult`` with channel-last ``uint8`` RGB visualization data.  This
       is a display/export product, not a quantitative intensity result.
   * - ``resize``
     - Transform
     - Any rank >= 2 image-like result.
     - ``ArrayProcessingResult`` with every Y/X plane resized by nearest,
       bilinear or cubic interpolation. Pixel scales change inversely so the
       calibrated physical extent remains constant.
   * - ``convert-type``
     - Transform
     - Any rank >= 2 image-like result.
     - ``ArrayProcessingResult`` stored as 8-bit, 16-bit or 32-bit float.
       Integer conversion can rescale the finite data range or clip it.
   * - ``transform``
     - Transform
     - Any rank >= 2 image-like result.
     - ``ArrayProcessingResult`` rotated 90 degrees or flipped in Y/X. A
       rotation also swaps the Y/X pixel scales.
   * - ``filter``
     - Filters
     - Any rank >= 2 image-like result.
     - ``ArrayProcessingResult`` after Gaussian, median, mean or unsharp
       filtering in each Y/X plane; leading stack axes are not blurred.
   * - ``subtract-background``
     - Restoration
     - Any rank >= 2 image-like result.
     - One background-subtracted ``ArrayProcessingResult`` and, optionally, a
       second result containing the rolling-ball background estimate.
   * - ``math``
     - Math
     - Any rank >= 2 image-like result.
     - Float32 ``ArrayProcessingResult`` after constant add/subtract/multiply/
       divide/gamma or invert/log/exp/square-root.
   * - ``image-calculator``
     - Math
     - Exactly two results with matching shape, axes, pixel scales and unit.
     - ``ArrayProcessingResult`` from pixel-wise arithmetic; division by zero
       yields zero. The toolbar dialog is the normal two-input entry point.
   * - ``drift-correct``
     - Restoration
     - Any result whose ``axis_labels`` contain ``T``.
     - ``DriftCorrectedResult`` with the same shape and axes plus drift trace
       plots.  Shift estimation uses a 2D ``Y, X`` plane and first index of
       intermediate axes, then applies the same shift to every slice.
   * - ``denoise``
     - Restoration
     - Any rank >= 2 image-like result; requires an installed denoising model
       and torch/torchvision.
     - ``DenoisedResult``.  With padding enabled the original shape is
       preserved; without padding the output can become a cropped plane or
       frame stack depending on the selected frame-like axis.
   * - ``segmentation``
     - Segmentation
     - Any rank >= 2 image-like result.  Extra axes are collapsed to selected
       indices or ``0``.
     - ``SegmentationResult`` with ``Y, X`` integer labels, a context source
       image layer, and region-area plot payload.
   * - ``label-morphology``
     - Segmentation
     - A 2D ``labels``-kind result.
     - Relabelled ``LabelsResult`` after fill holes, erosion, dilation,
       opening, closing or watershed splitting.
   * - ``psf-resolution``
     - Measurement
     - Any rank >= 2 image-like result.  Extra axes collapse to index ``0``.
     - ``PSFResolutionResult`` table with axes ``ROI, Metric`` and FWHM plot
       payload.  Intended as an analysis output, not an image-stack input.
   * - ``colocalization``
     - Measurement
     - Rank >= 3 image-like result with a compare axis containing at least two
       planes; auto prefers ``C``, then ``T``, then ``Z``.
     - ``ColocalizationResult`` table with axes ``ROI, Metric`` and intensity
       scatter plot payload.
   * - ``frc``
     - Measurement
     - Rank >= 2 image-like result for single-image FRC; two-image mode also
       needs a compare axis with at least two planes.
     - ``FRCResult`` curve data with axes ``C, Frequency`` and graph payload.
       This is a curve/table result, not an image-stack input.
   * - ``multicolor-registration``
     - Registration
     - Strictly ``["Z", "Y", "X"]`` or ``["T", "Z", "Y", "X"]`` deskewed
       volumes containing color strips along the configured split axis.
     - ``MulticolorRegistrationResult`` with aligned ``C, Z, Y, X`` preview
       data plus a reusable alignment transform, optionally saved to HDF5.
   * - ``multicolor-apply``
     - Registration
     - Strictly ``["Z", "Y", "X"]`` or ``["T", "Z", "Y", "X"]`` deskewed
       sample volumes and a saved multicolor alignment HDF5.
     - ``MulticolorApplyResult`` with ``C, Z, Y, X`` or ``T, C, Z, Y, X`` data.
   * - ``smlm-render``
     - Localization
     - ``LocalizationResult`` only.
     - ``ArrayProcessingResult`` render with ``Y, X`` or ``Z, Y, X`` axes,
       nanometer axis scales and image display levels.
   * - ``smlm-filter``
     - Localization
     - ``LocalizationResult`` only.  Bounds set to 0 are disabled; the
       results-table histograms are the intended way to choose cutoffs.
     - ``LocalizationResult`` with rows outside the photon/sigma/frame ranges
       removed; kept/total counts recorded in name and metadata.  Chains with
       ``smlm-drift``/``smlm-group``/``smlm-render``.
   * - ``smlm-drift``
     - Localization
     - ``LocalizationResult`` only; needs a frame span of at least the
       segment count and 2+ non-empty temporal segments.
     - ``DriftCorrectedLocalizationResult`` with positions minus the
       estimated per-frame drift, plus an X/Y drift-trace graph payload.
   * - ``smlm-group``
     - Localization
     - ``LocalizationResult`` only.
     - ``LocalizationResult`` with blinking repeats within the link radius
       merged: photon-weighted mean position/sigmas, summed photons, first
       frame; optional dark-frame gap tolerance.
   * - ``table-to-localizations``
     - SMLM
     - Any ``table`` result with named columns (a ``PointsTableResult``
       imported from a napari Points layer, a CSV). The x/y (optionally
       z, frame, photons, sigma) columns and their unit are given
       explicitly; a missing column is an error, never a guess.
     - ``LocalizationResult`` in nanometres, per-axis converted (``px``:
       lateral pixel size and Z step; ``table``: the table's own per-axis
       scale and unit). The only path from a points table to emitters.

Remaining follow-ups
--------------------

Semantic result kinds are implemented: results declare ``kind``, processors
declare ``kinds``, ``accepts()`` combines the kind and shape gates, and
``test_result_kind_matrix.py`` pins every built-in processor against
representative image, labels, table, curve, localization, RGB and composite
results.  Still open:

* Populate parameter widgets from the active result's real axis labels instead
  of hard-coded choices such as ``T``, ``Z``, ``C`` and ``D0``.
* Kind propagation through generic axis processors: cropping or splitting a
  ``labels`` or ``rgb`` result would currently come out as a plain ``image``
  ``ArrayProcessingResult``, so those kinds are simply not offered to the
  axis processors yet.  Propagating the input kind to the output would let
  label stacks be cropped/split without mislabeling.
* Keep multicolor registration/apply deliberately strict until their input
  assumptions are generalized beyond deskewed ``ZYX``/``TZYX`` strip data.
* RGB outputs are visualization/export artifacts (autoscaled ``uint8`` display
  values, not calibrated intensities); the ``rgb`` kind now enforces this by
  matching no analysis processor.

Result graph panel
==================

ImProcess can show a generic graph panel below the reconstruction viewer.  The
panel is controlled by the setup JSON and is hidden unless
``"graphPanel": true`` is set.  When enabled, it renders optional plot payloads
exposed by the currently selected ``ProcessingResult``.

Use *Measure Δx* to place a draggable horizontal interval between two graph
features.  The live readout reports both marker positions and their distance.
*Push to table* appends what the plot says to the shared Results dock: the
payload's own parameters (a fit's coefficients travel in
``PlotPayload.metadata``) with one row per curve, plus the Δx row when markers
are shown.  It no longer requires a measurement to exist — pressing it on a
plot of fitted data reports the fit.  The Profile panel offers the same
*Measure Δx* interaction; its *Push to table* and *Save CSV...* actions include
the manual distance together with the profile statistics and optional fit
results.

.. _improcess-result-parameters:

Curves and their parameters
---------------------------

An analysis that fits something produces two outputs — the curve, and the
parameters of the fit — and the parameters are usually the answer.  A
``ProcessingResult`` can therefore render into more than one panel at once:
``plot_payloads()`` draws the curve in this panel while
``table_columns()``/``table_records()`` contribute rows to the Results dock.

Rows are published automatically for ``kind == "table"`` results.  Any other
kind opts in by setting ``publishes_table_rows = True``, which keeps bulk rows
out of the dock by default — a ``localization`` result's ``table_records()``
can run to six figures.  ``frc`` results publish their resolution and cutoff
frequency this way, and the photophysics drop-in publishes its fitted
amplitudes, time constants and R².  Without the opt-in the numbers exist only
inside the result, drawn but unreadable.

Built-in graph producers include ``drift-correct`` and ``widefield-starss``.
Drift-corrected results expose Y and X drift traces over frame number.
WidefieldSTARSS results expose an anisotropy histogram and region
area-vs-anisotropy scatter plot.  ``frc`` results expose the FRC curve,
threshold curve and cutoff marker.  The same graph contract is intended for
future processing units such as batch summaries, FLIM traces and line-profile
tools.

Metadata panel
==============

Set ``"metadataPanel": true`` in the ``processing`` block to show the
metadata panel, or open it at any time from the **Tools** toolbar / menu
(``panel.metadata``, unbound by default).  It shows the complete metadata
hierarchy of a measurement file — or of the result selected in the
reconstruction list, provenance included — as a collapsible tree with
*Name*, *Value* and *Type* columns.

The reader is deliberately **layout-agnostic**: it walks whatever hierarchy the
container actually has and reports every attribute it finds on the way down.
Nothing in it encodes the ImSwitch recording layout, so files written by a
future storer — or by another program entirely — display without any code
change:

* **HDF5** — every group and dataset is descended recursively; group and
  dataset attributes appear as leaves, arrays additionally show shape and
  dtype.
* **Zarr / OME-NGFF** — the same walk over Zarr groups and arrays, including
  ``.zattrs`` content such as ``multiscales``.
* **TIFF / OME-TIFF** — the format flags tifffile detected, every
  ``*_metadata`` block it exposes (OME, ImageJ, ScanImage, and any vendor
  block a newer tifffile learns about), plus per-series axes/shape/dtype and
  the TIFF tags of each series' first page.

Structured values are opened up rather than shown as one unreadable line:
nested dicts and lists become subtrees, and string attributes that actually
carry a JSON document or an XML document (OME-XML, for example) are parsed and
expanded, with the raw string kept on the parent node.

The panel follows the current data item, so loading a file shows its metadata,
and it follows the selection in the reconstruction list: a result made in the
session (a duplicate, a crop, a processor's output) shows its identity, its
metadata and its provenance -- every step done to it so far, and the source
file it came from -- in the same tree.  *Reload* rebuilds that view after
further processing.
*Open file...* reads the metadata of any supported file without loading its
pixels, and *Reload* re-reads the current one — useful while a recording is
still being written.  The filter box matches names, values and types, keeping
the ancestors of each match visible so a hit deep in the hierarchy stays
readable in context.  *Copy* and the right-click menu copy a row, a value, a
path or a whole subtree as JSON; *Export...* writes the tree as JSON or as a
flat ``path``/``value``/``type`` CSV; *To Results* sends the currently visible
entries to the shared Results dock, where they can be accumulated across files.

Pathological containers cannot freeze the GUI: depth, per-node child count and
total node count are bounded, and every limit that trips inserts a visible
"… not shown" marker instead of silently dropping content.  Attribute values
are materialized during the walk, so the tree stays readable after the file is
closed.

Projection panel
================

Set ``"projectionPanel": true`` in the ``processing`` block to show an
interactive projection panel below the reconstruction viewer.  It operates on the
currently selected reconstruction result and produces a new projection result in
the reconstruction list.  The panel supports projection along ``T``, ``Z``, ``C``
or another selected axis using max, mean, sum, median or standard-deviation modes.
The projection panel uses the generic result-processor infrastructure — committed
projections are published as ``ProcessingResult`` entries in the reconstruction
list, not floating napari layers.  If the panel is enabled at startup, ImProcess
auto-registers the ``projection`` processor when it is not already listed under
``processing.processors``.

FRC panel
=========

Set ``"frcPanel": true`` in the ``processing`` block to show a Fourier ring
correlation panel below the reconstruction viewer.  The panel is the generic
result-processor panel for the ``frc`` processor: it runs on the currently
selected reconstruction result and publishes an ``FRCResult`` into the
reconstruction list.  Two-image FRC across an image axis and single-image FRC
with checkerboard / odd-even splitting are both supported; selecting the
result shows the FRC curve, threshold curve and cutoff marker in the graph
panel.  (The former custom FRC panel duplicated the processor's parameters
and plotted only locally; it was retired in the result-unification tool
audit.)  If the panel is enabled at startup, ImProcess auto-registers the
``frc`` processor when it is not already listed under ``processing.processors``.

ROI statistics panel
====================

Set ``"roiStatsPanel": true`` in the ``processing`` block to show a compact
ROI statistics panel.  It reports area, finite-pixel count, mean, median,
standard deviation, min, max and sum for either the full active image layer or
a rectangle ROI drawn in the reconstruction viewer.

ROI manager panel
=================

Set ``"roiManagerPanel": true`` in the ``processing`` block to show an
ImageJ-like ROI manager.  It stores multiple rectangular ROIs, supports
rename/duplicate/delete/show-hide operations, computes per-ROI statistics on
the active image plane and exports ROI/statistics tables as CSV or JSON.  The
simple ``roiStatsPanel`` remains available as a single-ROI quick view.

Segmentation panel
==================

Set ``"segmentationPanel": true`` in the ``processing`` block to show a
threshold and connected-component segmentation panel.  It operates on the
currently selected reconstruction result and produces a new ``SegmentationResult``
in the reconstruction list when you click **Segment**.  The panel supports manual
and Otsu thresholding, minimum-area filtering, optional Gaussian smoothing,
morphological operations, and watershed segmentation.  Region tables can be
exported as CSV/JSON, and exact segmented component masks can be pushed into the
ROI manager.

Enable the **Preview** checkbox to see a live, ephemeral preview layer that
updates when parameters change or when the viewer slice changes, making it easier
to tune segmentation settings before committing.  The preview layer is shown at
50% opacity and remains a transient tuning overlay.  Clicking **Segment** commits
the final segmentation as a reconstruction-list result (not a floating napari
layer), allowing you to save, reload, and reprocess segmentations alongside other
processing results.

PSF resolution panel
====================

Set ``"psfResolutionPanel": true`` in the ``processing`` block to show a
bead/PSF resolution panel.  It fits a non-rotated 2D Gaussian to the
currently selected reconstruction result — full-frame or per ROI Manager
entry — and publishes a ``PSFResolutionResult`` into the reconstruction list.
Selecting the result shows center, sigma, FWHM, amplitude, background and RMS
fit error in the results table (with CSV export) and the FWHM plot in the
graph panel.  The panel's ROI Manager sourcing is forwarded to the
``psf-resolution`` processor via the ``rois`` parameter.

Colocalization panel
====================

Set ``"colocalizationPanel": true`` in the ``processing`` block to show a
channel colocalization panel.  It compares two planes from a selected stack
axis of the currently selected reconstruction result — full-image or ROI
Manager batched — and publishes a ``ColocalizationResult`` into the
reconstruction list.  Selecting the result shows Pearson correlation, Manders
M1/M2, overlap coefficient and mean intensities in the results table (with
CSV export) and the intensity scatter in the graph panel.  The panel's ROI
Manager sourcing is forwarded to the ``colocalization`` processor via the
``rois`` parameter.

Multicolor panel
================

Set ``"multicolorPanel": true`` in the ``processing`` block to show a
three-color strip registration panel.  It is intended for SNOUTY / OPM /
MS-RESOLFT measurements where a deskewed bead volume contains three color
channels as adjacent X-axis strips.  The panel operates on the currently
selected ``ZYX`` or ``TZYX`` reconstruction result (falling back to the active
image layer in standalone use), splits it into channel ROIs, estimates
transforms in ``maxproj``, ``volume`` or ``descriptor_3d`` mode, and saves the
calibration as an HDF5 alignment file.  **Register** publishes the aligned
calibration preview and **Apply** publishes the aligned sample stack as
results in the reconstruction list (the same result types the
``multicolor-registration`` / ``multicolor-apply`` processors return).  The
split-boundary rectangles and detected-bead markers are ephemeral
tuning/diagnostic overlays, like the segmentation preview.

The usual workflow is:

1. Deskew a bead sample that contains signal in all three color strips.
2. Run ``multicolor-registration`` from the panel and save the alignment HDF5.
3. Deskew a sample acquisition with the same optical/channel layout.
4. Load the alignment HDF5 and run ``multicolor-apply`` to produce a
   ``CZYX`` or ``TCZYX`` aligned color result.

The same functionality is available as registered processors:
``multicolor-registration`` extracts and optionally saves the transform, while
``multicolor-apply`` applies a saved HDF5 transform to later data.

Active reconstructor
====================

A combo box at the top of the Parameters dock lists every registered
reconstructor and shows which one will run when *Reconstruct current* fires.
Picking a different entry swaps the parameter widget below the picker (with
the dock title following along: ``Parameters — WidefieldSTARSS analysis``),
re-wires the watcher's output subfolder (see below), and — for pass-through
plugins — re-renders the currently loaded ``DataObj`` in the viewer
immediately.

The picker's choices come from the ``processing:`` config block at startup;
if you list ``["view-only", "widefield-starss"]`` under ``reconstructors``,
those are the two entries the combo offers.  Plugins not in the list are
not registered and therefore not pickable.

Bead-scan reconstruction
========================

The ``beadrec`` reconstructor turns a recorded camera frame stream into a 2D
raster image. Each input frame represents one scan position; the reconstructed
pixel value is the mean intensity inside the selected camera ROI. It accepts
HDF5, TIFF/OME-TIFF and Zarr input.

Set **Scan X pixels** and **Scan Y pixels** to the real raster dimensions. A
value of ``0`` asks the reconstructor to infer the missing dimension from the
frame count; when both are ``0`` it assumes a square scan. Enter explicit
dimensions for non-square scans. **Full frame** uses the entire camera image as
the detection ROI; untick it to enter ``x0, y0, x1, y1`` bounds. **Step X** and
**Step Y** correct anisotropic scan sampling by rescaling the reconstructed
image to equal pixel spacing.

**Fit model** can be left at ``none`` or set to one of the available Gaussian/
donut bead models. A successful fit is stored in the result metadata with the
model, centre, fitted parameters and :math:`R^2`; a fit failure is reported in
the metadata without discarding the reconstructed image.

For saved tiling folders, use the separate ``tiling-mosaic`` reconstructor
described in :doc:`tiling`.

.. _improcess-time-lapse:

Time lapses
===========

A camera time lapse or scan time lapse is recorded one item per timepoint:
a file each (``<name>_time07_Camera.hdf5``, ``<name>_scan0007_APD.zarr``), or
with *single file* storage a group each inside one HDF5 or Zarr file
(``scan0/Camera``, ``scan1/Camera``, ...). The ``time-lapse`` reconstructor
puts them back together as one ``(T, ...)`` stack. Load it for the session from **Tools → Load
reconstructor**, or add it to ``processing.reconstructors`` in the setup file
to keep it.

With **Time lapse** active, open any one item of the lapse -- the first, the
last, or any in between -- and the whole lapse becomes the current source. A
single-file lapse opens without the dataset picker, unless the file holds more
than one lapse: then the picker asks for an item, and the lapse of the item
picked is opened. A path picked inside a Zarr lapse (``lapse.zarr/scan4``)
names its item directly. *Reconstruct current*
then returns the stack. It is lazy: a timepoint is read when the viewer shows
it, so a 2000-point lapse appears at once instead of after loading every point,
and the viewer holds only a few of its files open at a time.

The lapse is found from what every item records, not from its file name
alone: ``recording:lapse_index`` (which point it is),
``recording:num_timepoints`` (how many were planned) and
``recording:single_lapse_file``. A file that belongs to another lapse but
happens to share the name pattern is left out, and the log says why. In a
single file, the ``scanN`` group number is only the next free slot, so a file
that received two lapses is split where the recorded index starts again.

Only real time is stacked:

* a scan lapse whose points were positioned by a workflow records them as
  ``tile`` or ``position`` points, and is refused -- a tiling run belongs to
  :doc:`tiling`;
* an older tiling run, whose payloads were still labelled as time, is
  recognised by the ``tiles.json`` beside it;
* a single recording (``recording:num_timepoints`` of 1) is not a lapse.

A file that is not a lapse opens as the image it is, with the reason in the
Parameters dock.

**Detector** chooses which detector's lapse to stack when several were
recorded.

**Time axis** chooses how T is spaced. *Planned interval* spaces it evenly at
the ``recording:lapse_interval_s`` the lapse was set up with. *Actual start
times* follows each point's recorded ``acquisition:start_time``; the viewer
spaces planes by the typical step between them, and an export keeps every
plane's own time. Both times are kept for every point in either case. A point
that started more than 10% of the interval after its scheduled time is listed
in the Results table with its delay. A scan lapse records no interval -- it
waits a set delay after each scan finishes rather than keeping a schedule --
so it always uses the actual times.

**Incomplete points** decides what happens to a point that was stopped early
(``recording:completion_outcome`` is ``stopped_early``), never finalized, or
holds no frames. *Mark in place* keeps it: frames it never recorded are blank,
and it is listed in the Results table. *Skip at the end* leaves incomplete
points off the end of the stack. A point missing or incomplete in the middle
of the lapse is always kept and marked, because every point sits at its
recorded index: leaving one out would move every later point onto the wrong
time. The stack ends at the last point recorded; points planned after a lapse
was stopped are not in it.

Points that disagree on shape or data type -- an ROI or binning changed
part-way -- are refused with a message naming the point, rather than stacked.
An incomplete point may be shorter along its frame axis and nothing else.

**Save** streams the stack one timepoint at a time into OME-TIFF, HDF5 or
OME-Zarr. OME-TIFF gets the T spacing as ``TimeIncrement`` and each plane's
time as ``DeltaT``. HDF5 and Zarr add a ``t_seconds`` list, and every format
records each point's state, planned and actual time in the result
annotations.

The discovery, the header pass and the lazy stack live in the data layer
(:py:mod:`imswitch.improcess.model.lapse_source`), not in the reconstructor.
A reconstructor that wants to process a lapse one point at a time --
MoNaLISA or BeadRec per timepoint -- can accept the ``time-lapse`` source kind
and read ``open_time_lapse(...).timepoint(t)`` without redoing any of it.

MoNaLISA fast-Gauss mode
========================

The public config-editor and setup-file ID for MoNaLISA is still
``monalisa``. There is no separate ``gauss-monalisa`` plugin ID to list in
``processing.reconstructors``. The same registered
:py:class:`~imswitch.improcess.reconstructors.monalisa.reconstructor.MonalisaReconstructor`
handles both paths:

* *Reconstruct current* uses the parameter widget's ``Reconstruction method``
  selector. ``MoNaLISA`` is the default full post-acquisition pipeline;
  ``Fast Gauss MoNaLISA`` runs the same low-latency Gaussian reassignment
  algorithm on the loaded stack.
* Live reconstruction always calls ``MonalisaReconstructor.make_session()``
  and uses the fast-Gauss streaming session under
  ``imswitch/improcess/reconstructors/monalisa/live_session.py`` regardless
  of the offline selector.

The MoNaLISA parameter widget's ``Bleaching correction`` checkbox applies to
the full offline path, the fast-Gauss offline path, and live fast-Gauss. When
enabled, raw frames are normalized with the same 4th-power frame-energy
correction, ``(E_0 / E_i) ** 4``, before reconstruction. The option is off by
default.

Fast-Gauss uses the Mini_Recon-style Gaussian footprint: concentric
rectangular shells around each localized focus, followed by a least-squares
fit of Gaussian amplitude plus optional constant background. The parameter
widget exposes the fit and footprint options that used to be hard-coded:
``Fast Gauss options -> Footprint rectangles`` defaults to ``3`` shells,
``Fast Gauss options -> Gaussian sigma`` defaults to ``2.0`` pixels, and
``Fast Gauss options -> Pinhole radius`` defaults to ``1.5`` times sigma.
``Fast Gauss options -> Footprint mode`` controls which footprint is active:
``Rectangular shells`` keeps the Mini_Recon footprint, while
``Circular pinhole`` replaces it with a circular detection footprint using the
pinhole radius.
The shared ``BG modelling`` option controls whether the fast path fits a
constant background term; ``No background`` uses a pure Gaussian matched
filter.
Automatic scan-orientation detection is also used for fast-Gauss by trying
the eight possible fast/slow-axis and direction combinations and choosing the
one with the lowest total variation.

The fast-Gauss offline mode intentionally has the same geometry scope as the
live path: a 2D Right-Left / Up-Down scan, one Z slice, and optional
timepoints. Use the default ``MoNaLISA`` method for the full coefficient-based
pipeline.

Pass-through reconstructors
===========================

Some reconstructors don't actually run any signal processing — they wrap raw
frames as a :py:class:`~imswitch.improcess.model.result.ProcessingResult` so
the viewer and the analysis panels can work with the data uniformly.  These
plugins set ``is_pass_through = True`` on the class (``view-only`` is the
canonical example).

When a pass-through plugin is active:

* dropping a single file with one dataset goes **straight to the napari
  viewer** — no need to click *Set as current data* and then *Reconstruct
  current*;
* loading via *Quick load* or *Set as current data* in the Multidata dock
  routes through the auto-render path the moment ``sigCurrentDataChanged``
  fires;
* the *Reconstruct current* button is hidden, since the result is the data
  itself.

To opt a plugin into pass-through routing, just set the class attribute::

    class MyPassThroughReconstructor(Reconstructor):
        id = "my-pass-through"
        name = "My pass-through"
        is_pass_through = True

A contract test (``test_pass_through_contract.py``) keeps every modality
reconstructor in the built-in registry at ``is_pass_through = False`` so a
real-compute plugin can't silently trigger the auto-route on every dataset
load.

SMLM localization
=================

The SMLM localizer reconstructor (id ``smlm-localizer``) performs
single-molecule localization microscopy (SMLM) on raw data. It detects and
fits bright spots in each frame, outputting a table of localizations with
sub-pixel coordinates, photon counts, and fit widths.

The SMLM parameter widget exposes:

* **Detection** threshold, smoothing sigma, and ROI size for the net-gradient
  spot detection algorithm.
* **Fitting** method — ``gausslq`` (centroid + Gaussian moments) or ``mle``
  (Poisson Gaussian maximum-likelihood).
* **Pixel size** in nanometers for converting pixel coordinates to physical
  units.
* **Preview detection** toggle — when enabled, candidate spots from the
  detection step (before fitting) appear as a live scatter overlay on the
  raw-data frame viewer. The overlay updates automatically whenever detection
  parameters change or when the displayed frame changes (via the frame slider
  or "show mean"). This preview is essential for tuning the detection threshold
  before running the full reconstruction stack, since an incorrect threshold
  often results in zero localizations.

The preview uses only the detection step (no fitting) to stay responsive, and
recomputes with a short debounce (~250 ms). The scatter markers appear as open
red circles on the pyqtgraph frame viewer, distinct from the MoNaLISA pattern
overlay.

If the reconstructor returns zero localizations, it logs a warning suggesting
that the user tune the detection threshold using the preview toggle. The
warning message directs users to enable the preview checkbox to visualize
which pixels meet the detection criteria before running the full processing
pipeline.

The SMLM localizer is **streaming-capable**: during a live recording, 
localizations accumulate incrementally as frames arrive, and the preview 
histogram in the viewer updates in real time to show the growing point cloud. 
The output from a live reconstruction is identical to running the batch 
reconstruction on the saved data afterwards, ensuring reproducibility.

Importing localization tables
=============================

Coordinate tables written by other SMLM software open straight into the
reconstruction list.  A localization table is a *result*, not raw data to
reconstruct, so it bypasses the data loader and is published the same way a
reconstructor publishes its output: filter, drift-correct, group, render and
view it exactly as if the ``smlm-localizer`` had produced it.

Two formats are recognised by content and need no interaction:

* **ThunderSTORM CSV** — units are read from the ``name [unit]`` header
  rather than assumed, and the 1-based frame numbers become 0-based to match
  Picasso and ImProcess.  A file whose positions are in pixels cannot be
  converted without a pixel size and says so instead of guessing.
* **Picasso HDF5** — recognised by its ``locs`` dataset, so it opens even
  though it shares the ``.hdf5`` suffix with image stacks.  The pixel size is
  taken from the ``.yaml`` sidecar Picasso writes next to it.

Any other ``.csv``/``.tsv`` is offered as a localization table only while the
``smlm-localizer`` reconstructor is active (its accepted sources include
``localizations``), because a generic table could be anything.  A column
mapping dialog then asks what each column means, with the mapping pre-filled
from the header names, plus the position unit, the camera pixel size and the
first frame number.  X and Y are mandatory; everything else is optional and
left empty when the file has nothing to put there.

Files that declare no pixel size fall back to an assumed 100 nm, which is
recorded in ``metadata["pixel_size_assumed"]`` and logged.  The coordinates
themselves are exact either way — the pixel size only sets the preview
histogram's bin floor and the scale of a later Picasso export — but nothing
downstream should report the assumed value as measured.

Localization precision versus PSF width
---------------------------------------

The localization table carries two different widths on purpose, because they
are different physical quantities:

* ``sigma_x_nm`` / ``sigma_y_nm`` / ``sigma_z_nm`` — the **PSF width**, how
  broad the spot was on the camera.  Useful for rejecting bad fits.  Picasso
  calls these ``sx``/``sy``, ThunderSTORM ``sigma``.
* ``lp_x_nm`` / ``lp_y_nm`` / ``lp_z_nm`` — the **localization precision**,
  how well the emitter's position is known.  Picasso calls these
  ``lpx``/``lpy``, ThunderSTORM ``uncertainty``.

Precision is what an SMLM reconstruction should be rendered with; drawing
every molecule at its PSF width merely reproduces a diffraction-limited image.
Import keeps both columns apart, and the ``smlm-localizer`` fills the precision
columns itself from the Thompson/Larson/Webb closed form with Mortensen's
correction, using the fitted width, photons, background and pixel size.  Note
that ImProcess does not yet apply camera gain and offset, so for a camera
without unit gain that estimate is correct in shape but scaled: good for
filtering localizations against each other, not for quoting an absolute
nanometre precision.

.. _improcess-napari-storm:

Localization point clouds (napari-storm)
========================================

A ``LocalizationResult`` displays by default as a low-resolution histogram
preview.  Set ``"napariStormViewer": true`` in the ``processing`` block to draw
localization results instead as GPU-rendered summed Gaussians through the
optional `napari-storm <https://pypi.org/project/napari-storm/>`_ package,
which is installed by the ``storm`` extra::

    pip install "imswitch2[storm]"

Everything about this backend is best-effort.  Without the package, on a GL
session without instancing support, or for a table napari-storm refuses, the
flag does nothing and the result keeps its preview; nothing else in the viewer
changes.  napari-storm pins ``zarr<3`` for its own MINFLUX reader, so
resolving the extra moves an environment onto zarr 2.x.  ImSwitch runs on
either zarr major; to stay on zarr 3, install the package itself with
``pip install --no-deps napari-storm`` instead of the extra.

The viewer reads the result's nanometre table in place, without a copy or unit
conversion, and declares the columns to napari-storm rather than renaming
them.  Each molecule is drawn as a Gaussian of its localization precision
where the table has one (variable-width mode); a table without usable
precision falls back to the fitted PSF width, and one with neither to a fixed
20 nm width.  A 2D result is drawn on the 2D canvas and a 3D one switches the
canvas to 3D, framed on the cloud.

Point clouds are *retained*: a result is opened in the renderer once, updated
in place when its settings change, hidden while another kind of result is
selected, and closed only when it leaves the reconstruction list.  This is
deliberately not the stateless rebuild-on-click path the other display layers
take, because opening and closing GPU datasets on every selection is the churn
napari-storm's architecture exists to avoid.  The remaining napari layers work
as before; the contrast and layer-control widgets skip point-cloud layers,
which hold geometry rather than an intensity array.

Render controls
---------------

Set ``"smlmRenderPanel": true`` in the ``processing`` block to show a panel
driving the point-cloud renderer for the selected localization result.  It is
only useful together with ``napariStormViewer``.

* **Gaussian** — *Width mode* chooses between the precision of each
  localization (variable, available only when the table has one) and a fixed
  sigma; the fixed XY and Z sigmas are entered in nanometres with the
  corresponding FWHM shown alongside, since papers quote FWHM while the
  renderer takes sigma.  *Colour by depth* encodes Z on a hue-sweeping
  colormap and is available for 3D results in fixed-width mode.
* **Render range** — per-axis lower and upper bounds, as fractions of the
  dataset extent, restricting the drawn sub-volume without copying the data.
* **Appearance** — colormap and opacity, which recolour the cloud without
  rebuilding any geometry.

Until a control is touched the renderer decides from the data, so an untouched
panel never overrides what a result would draw on its own.

File watcher save folder
========================

When the *Watch and run* checkbox is on, the watcher writes each
reconstructed output under ``{watched_dir}/{default_save_subdir}/``.  The
subdirectory name comes from the active reconstructor's class attribute::

    class SnoutyReconstructor(Reconstructor):
        id = "snouty"
        default_save_subdir = "deskew"

The default is ``"rec"``.  Switching reconstructors in the active-reconstructor
picker also retargets the watcher, so two watchers running back-to-back on the
same folder with different plugins won't overwrite each other's outputs.

Result flow
===========

Producers of processing results — the legacy MoNaLISA reconstruct path, the
plugin reconstruct path, and any future processor-chain runner — publish
their output via the ``sigResultProduced(result, displayName)`` signal on
:py:class:`~imswitch.improcess.controller.CommunicationChannel`.  The
canonical listener
(:py:meth:`~imswitch.improcess.controller.ReconstructionViewController.resultProduced`)
folds the result into the reconstruction list and updates the napari layer.

Producers therefore don't need to know which widget holds the napari list;
emit the signal and forget.  Writing a CLI batch processor or a future
processor-chain UI is as simple as instantiating the comm channel and
emitting the signal whenever a new result is ready.

WidefieldSTARSS pairing
=======================

The ``widefield-starss`` reconstructor analyzes one H/V TIFF pair.  When the
current file name ends in ``_h.tif`` or ``_v.tif`` it auto-loads the matching
counterpart next to it.  Otherwise, set the current file role and select the
counterpart path in the parameter panel.  The parameter panel includes presets
for standard widefield-cell analysis and line-PSF split-detection analysis.

In standard four-pixel polarization-mosaic mode, anisotropy can be computed
two ways.  The default ``stokes`` mode uses all four analyzer pixels:
``S0`` is estimated from both ``I0 + I90`` and ``I45 + I135``, ``S1`` is
``I0 - I90``, and virtual H/V detection intensities are constructed as
``IH = (S0 + S1) / 2`` and ``IV = (S0 - S1) / 2``.  The alternative
``direct_0_90`` mode uses only the raw ``I0`` and ``I90`` analyzer pixels for
H/V detection.  ``stokes`` is the default because it keeps the current behavior
and uses the full four-pixel measurement; ``direct_0_90`` is available as an
explicit comparison path.

For segmentation, WFS ``otsu`` uses the same shared ImProcess segmentation
kernel as the ``segmentation`` processor.  WFS-specific controls such as
threshold scaling and bounded hole filling are passed into that shared helper
instead of being implemented separately.  Older saved parameters that contain
``generic_otsu`` are still accepted as a compatibility alias for ``otsu``, but
new UI presets expose only the single ``otsu`` mode.

WidefieldSTARSS batch helpers
=============================

The WFS analysis package also includes backend helpers for folder-scale
processing.  ``discover_widefield_starss_pairs_in_folder`` matches
``*_h.tif[f]`` and ``*_v.tif[f]`` files, and
``run_widefield_starss_batch_from_folder`` runs the same single-pair analysis
on every matched pair with one shared parameter set.  The batch result exposes
two consolidated tables:

* ``regions`` — one row per segmented nucleus/region, including sample ID,
  source H/V paths, anisotropy values, area, centroid, bounding box,
  ellipticity, channel means and signal variances.
* ``summary`` — one row per H/V pair, including region count, total area,
  mean/median anisotropy and mean intensity summaries.

The backend can export ``batch_regions.csv``, ``batch_summary.csv`` and a
combined HDF5 file.  The WidefieldSTARSS parameter widget exposes a first
batch UI pass with input/output folder fields, a *Run WFS batch* button,
progress reporting and cancellation between file pairs.  Batch execution runs
in a Qt worker thread so the ImProcess UI remains responsive while the
consolidated CSV/HDF5 exports are written.  Completed runs populate in-panel
summary and capped region-preview tables, plus an unmatched-file list.  A
shared filter field searches the visible batch tables and unmatched paths, and
the widget can open the selected output folder directly.  When the graph panel
is enabled, completed batches also publish aggregate plots for region
anisotropy, area-vs-anisotropy and per-sample summaries.

.. _improcess-memory-limits:

Memory limits
=============

How much RAM ImSwitch may spend on buffering and on automatic work is a
property of the computer, so it lives in the per-machine options file
``imcontrol_options.json`` (under the user config directory) rather than in
the setup file, which travels between machines. The ``memory`` group holds
three limits in MiB, each named for the one thing it bounds; the values
shown are the defaults. Edit them in ImControl under **Tools → Memory
limits…**, which saves this file and applies the new limits at once, or edit
the file by hand and restart::

    {
        "setupFileName": "example_sted.json",
        "memory": {
            "writerQueueMB": 512,
            "perDetectorQueueMB": 256,
            "processingWorkingSetMB": 1024
        }
    }

``writerQueueMB``
    The backlog the recording writer may hold before the acquisition loop
    blocks. Blocking is graceful backpressure: frames wait in the detector's
    chunk queue meanwhile, and the log says the moment it starts.

``perDetectorQueueMB``
    The backlog any one *(detector, consumer)* chunk queue may hold before
    that consumer's stream is declared incomplete -- for a recording, the
    point at which it fails. It is per queue: a rig with several detectors
    and several consumers (the recording, BeadRec, a workflow) can hold this
    much in each. A recording's stall tolerance is roughly the two numbers
    added: a smaller writer queue backed by a larger detector queue absorbs a
    short stall as well, but only the writer's share is graceful.

    One delivery larger than this -- a scan-driven detector's whole volume,
    or a burst of camera frames after a late poll -- is still admitted when
    the queue is empty, and warned about once, because refusing it would
    refuse the measurement rather than bound a backlog; nothing further fits
    behind it until it is read.

``processingWorkingSetMB``
    The working set ImProcess may spend on work nobody asked for: contrast
    sampling (the sample size follows it) and the mean preview computed when
    data is loaded. It defaults to 1 GiB, a transient working set sized for
    a typical workstation; lower it on a small machine. Above it, the current-data panel shows the first plane
    instead of the mean and says so; *Show mean* still computes the mean on
    request. Opening a dataset whose decoded size exceeds it is announced in
    the status bar before decoding starts, naming the size and whether
    *Open virtual* offers a lazy path for that source. Nothing is refused.

None of these is a process limit and ImSwitch claims none: camera drivers
allocate their own buffers, datasets and results are as large as the data.
A value that is not a positive whole number is reported at startup and the
default stands; the dialog shows such a value as the default in force and
replaces it when saved. Every message that quotes a limit names the setting
that moves it, so the line in the log is the line to act on. Saving is
refused while a recording runs: every queue check reads the limit in force,
so a smaller queue would fail the recording in progress.

Config schema
=============

To configure which plugins ImProcess loads, add a ``processing`` block
to your Imcontrol setup file (the same JSON you select via
``imcontrol_options.json``)::

    {
        "processing": {
            "parameterPanel": true,
            "napariLayerControls": true,
            "reconstructionPanel": true,
            "actionsPanel": true,
            "fileWatcherPanel": true,
            "multiDataPanel": true,
            "currentDataPanel": true,
            "resultsPanel": true,
            "graphPanel": true,
            "metadataPanel": true,
            "projectionPanel": true,
            "segmentationPanel": true,
            "psfResolutionPanel": true,
            "colocalizationPanel": true,
            "multicolorPanel": true,
            "frcPanel": true,
            "roiManagerPanel": true,
            "roiStatsPanel": true,
            "smlmRenderPanel": true,
            "napariStormViewer": true,
            "reconstructors": ["monalisa", "smlm-localizer", "view-only"],
            "processors":     ["drift-correct", "projection", "segmentation", "psf-resolution", "colocalization", "frc", "multicolor-registration", "multicolor-apply"]
        }
    }

The block is optional.  When it is absent (or the setup file cannot be
read at all, e.g. in standalone mode) the registry falls back to::

    reconstructors: ["view-only"]
    processors:     ["drift-correct"]

The defaults differ when a setup starts an explicit plugin list but supplies
only one side of it. If either ``reconstructors`` or ``processors`` is present,
an omitted ``reconstructors`` key defaults to ``["monalisa"]`` and an omitted
``processors`` key defaults to ``["drift-correct"]``. Use an explicit empty
list when that side should load nothing.

Only the plugin IDs you list are instantiated during initial registry setup.
Runtime-loaded tools, and registry-backed startup panels such as
``projectionPanel`` and ``frcPanel``, may register their required processors
later.  List processor IDs explicitly when you want them preloaded without
opening the corresponding panel.

Set ``"graphPanel": true`` in the ``processing`` block to show the optional
graph panel.  When the key is absent, ImProcess keeps the panel hidden.

The core GUI layout can also be made more minimal from the same block.
``"parameterPanel": false``, ``"actionsPanel": false``,
``"fileWatcherPanel": false``, ``"multiDataPanel": false``,
``"currentDataPanel": false`` and ``"resultsPanel": false`` hide the
corresponding startup docks.  ``"napariLayerControls": false`` hides napari's
built-in layer-controls dock, and ``"reconstructionPanel": false`` hides the
Reconstruction dock until the first result is displayed.  These keys default to
``true`` so existing setups keep the full ImProcess window.

.. _improcess-processing-presets:

Processing-only setup presets
=============================

Ready-to-use minimal configs ship under
``imswitch/_data/user_defaults/imcontrol_setups/``:

.. list-table::
   :header-rows: 1

   * - Setup file
     - Intended use
   * - ``fiji_processor.json``
     - Ultimate minimal Fiji/ImageJ-like processor: view-only loading, no startup docks, no napari layer controls and no Reconstruction dock until data is loaded
   * - ``monalisa_processor.json``
     - MoNaLISA reconstruction plus view-only fallback
   * - ``snouty_processor.json``
     - SNOUTY deskew, SNOUTY projection previews and multicolor strip alignment
   * - ``widefieldstarss_processor.json``
     - WidefieldSTARSS H/V-pair analysis
   * - ``general_image_processing.json``
     - View-only display, drift correction, projections, segmentation, PSF, colocalization, FRC, ROI and multicolor tools

The Fiji preset is intentionally sparse: it starts with ``view-only`` and no
registered processors, then lets the Image and Analysis toolbars register
processor-backed tools only when the user opens them.

The MoNaLISA preset has the same shape as the others::

    {
        "processing": {
            "parameterPanel": true,
            "napariLayerControls": true,
            "reconstructionPanel": true,
            "actionsPanel": true,
            "fileWatcherPanel": true,
            "multiDataPanel": true,
            "currentDataPanel": true,
            "resultsPanel": true,
            "graphPanel": true,
            "profilePanel": true,
            "metadataPanel": true,
            "projectionPanel": true,
            "segmentationPanel": true,
            "psfResolutionPanel": true,
            "colocalizationPanel": true,
            "frcPanel": true,
            "roiManagerPanel": true,
            "roiStatsPanel": true,
            "smlmRenderPanel": true,
            "napariStormViewer": true,
            "reconstructors": ["monalisa", "smlm-localizer", "view-only"],
            "processors":     ["drift-correct", "projection", "segmentation", "psf-resolution", "colocalization", "frc"],
            "liveStallTimeoutS": 300
        }
    }

The ``processing:`` block also accepts:

* **liveStallTimeoutS** (numeric, optional) — timeout in seconds for detecting
  crashed recording writers during live reconstruction. When a live-reconstruction
  reader waits this long without receiving new frames and without seeing a
  completion marker, it assumes the writer has crashed and finalizes the session
  with the partial data received so far. Defaults to 300 seconds (5 minutes).
  Set to ``0``, ``null`` or ``false`` to disable the watchdog. Timelapse
  sources — both single-file (``scan{N}`` groups in one store) and per-file
  (one store per timepoint) — legitimately idle between timepoints while the
  recorder prepares the next one; the reader automatically disables the
  watchdog for these lapse sources unless you explicitly set a value in this
  config key (an explicit value then applies everywhere).
* **napariStormViewer** (bool, default ``false``) — draw localization results
  as GPU point clouds through the optional napari-storm package instead of
  their histogram preview.  Needs the ``storm`` extra; without it the key has
  no effect.  See :ref:`improcess-napari-storm`.
* **smlmRenderPanel** (bool, default ``false``) — show the panel controlling
  the point-cloud renderer (Gaussian width, colour by depth, render range,
  appearance).  Only useful together with ``napariStormViewer``.

To launch Imswitch2 with *only* ImProcess (no Imcontrol GUI) and *only*
the plugins from one of these setup presets:

1. Set ``modules.json`` to::

       {"enabled": ["improcess"]}

2. Set ``imcontrol_options.json`` to::

       {"setupFileName": "fiji_processor.json"}

3. Launch the app normally.  At startup the registry is populated with
   the reconstructors and processors listed in the selected setup file;
   the corresponding plugin objects are reachable from the controllers
   and can be inspected programmatically.

.. note::

   The reconstructor picker UI is now wired into the Parameters dock and
   the live reconstruction button dispatches through the registry. For
   ``monalisa``, live mode uses the fast-Gauss streaming session described
   above. The ``processing:`` block continues to control which plugins are
   *registered and offered in the picker*.

These are the recommended setups for users who treat Imswitch2 as a
post-processing tool only — e.g. opening acquisitions taken on a different
machine for reconstruction, preview, or quantitative analysis.

Status (Milestone 12)
=====================

ImProcess is delivered in phases, tracked in ``ROADMAP.md`` Milestone 12.

Done:

* Rename ``imreconstruct`` → ``improcess`` (Phase A)
* Plugin contracts + registry, MoNaLISA / view-only reconstructors,
  drift-correct processor, drag-and-drop ingest, standalone launch,
  config-driven plugin loading (Phase B.1)
* Optional result graph panel + ``PlotPayload`` contract, with drift-correct
  publishing Y/X drift traces
* ``widefield-starss`` reconstructor first slice: H/V TIFF pairing, standard
  mosaic and split-detection analysis kernels, anisotropy maps, region table,
  HDF5/TIFF save, and graph payloads
* ``frc`` processor and optional FRC panel: two-image FRC, single-image FRC,
  checkerboard / odd-even splitting, 1/7 threshold and resolution estimates
* ``projection`` processor and optional projection panel: max, mean, sum,
  median and standard-deviation projections along selected axes
* ``stack-subset``, ``stack-split``, ``channel-split``, ``channel-merge``,
  ``make-composite`` and ``make-rgb`` processors plus image-toolbar actions:
  one source result can emit cropped substacks, multiple per-plane/per-channel
  results, composite display-layer results, or explicit RGB visualization
  results through the shared processor contracts
* ``segmentation`` processor and optional segmentation panel: manual/Otsu
  thresholding, connected components, label-layer display and ROI Manager
  mask export, plus region-table export
* ``psf-resolution`` processor and optional PSF resolution panel: 2D Gaussian
  bead/PSF fits on full images or ROI Manager entries, FWHM/sigma table export
* ``colocalization`` processor and optional colocalization panel: Pearson,
  Manders M1/M2, overlap coefficient, ROI Manager batching and CSV/JSON export
* ``multicolor-registration`` and ``multicolor-apply`` processors plus the
  optional multicolor panel: SNOUTY-style three-color X-strip calibration,
  HDF5 transform persistence and aligned ``CZYX`` / ``TCZYX`` output
* Optional ROI manager panel for multiple rectangular ROIs, per-ROI
  statistics, visibility toggles, duplication and CSV/JSON export
* Optional ROI statistics panel for full-image or rectangle-ROI area, mean,
  median, standard deviation, min, max and sum
* Cleanup: duplicate file removal, shared U-Net helpers extracted,
  ``PatternFinder.findBestPeak`` arithmetic fix
* ``denoise`` processor wrapping the existing UNet / UNet+RCAN model with
  lazy torch import, auto model-type selection and DenoisedResult save
* Pass-through reconstructor contract (``is_pass_through``): drag-drop or
  *Set as current data* on ``view-only`` renders to the viewer in one
  action instead of three
* Active-reconstructor picker in the Parameters dock header — flip
  between registered plugins on the fly, with the param widget and dock
  title following the choice
* File watcher's output subfolder driven by the active reconstructor's
  ``default_save_subdir`` instead of a hardcoded ``rec/``
* ``sigResultProduced`` decouples producers from the napari list widget,
  so future processor-chain runners and CLI batch processors can publish
  results without reaching into the main view
* Registry-backed offline reconstruction for every reconstructor except the
  legacy full MoNaLISA method; Fast Gauss MoNaLISA uses the plugin path too
* Registry-backed live reconstruction through ``make_session()`` for streaming
  reconstructors, with a batch ``process()`` fallback for other plugins
* Runtime *Load tool* panels for every registered processor, including generic
  parameter panels for processors without a dedicated dock
* SMLM localization, BeadRec and tiling-mosaic reconstructors, plus the full
  built-in processor inventory listed above
* WidefieldSTARSS batch folder processing, consolidated table display and
  CSV/HDF5 export

Pending:

* **Saved processor chains** — individual processors are available now through
  dedicated or generic runtime panels, but the application cannot yet define,
  save and automatically execute a multi-step chain.
* **Legacy full MoNaLISA path** — the coefficient-based offline method still
  delegates to ``MoNaLISAController``. Fast Gauss offline and all live
  reconstruction already use the registry-backed plugin path.
* **Per-modality follow-up** — STED/confocal averaging, FLIM overlays, richer
  WidefieldSTARSS layer presentation, deconvolution and physical SNOUTY setup
  validation.

Writing a new plugin
====================

A minimal ``Reconstructor`` looks like this::

    from imswitch.improcess.reconstructors.base import Reconstructor
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    class MyReconstructor(Reconstructor):
        name = "My modality"
        id = "my-modality"
        file_extensions = ["hdf5", "tiff"]

        @classmethod
        def default_params(cls):
            return {"iterations": 10}   # what a fresh widget hands process()

        def make_param_widget(self, parent):
            ...  # return a QWidget exposing get_values() -> dict (same keys/defaults)

        def make_metadata_dialog(self, parent):
            return None  # or a QDialog for acquisition metadata

        def process(self, data_obj, params, context=None):
            data_obj.checkAndLoadData()
            ...
            return ArrayProcessingResult(name=data_obj.name, data=..., axis_labels=[...])

Return one of the existing result classes (``ArrayProcessingResult``,
``LabelsResult``, ``LocalizationResult``, …) unless the modality really
needs its own on-disk layout; the existing classes come with saving,
provenance embedding and napari conversion already done.  Register the
reconstructor by adding the class to ``_AVAILABLE_RECONSTRUCTOR_CLASSES``
in ``imswitch/improcess/reconstructors/__init__.py``.  Processors use the
analogous ``_AVAILABLE_PROCESSOR_CLASSES`` map in
``imswitch/improcess/processors/__init__.py``.  Or skip the source tree
altogether: a ``.py`` file in the plugins folder defining the class is a
drop-in plugin (below), for reconstructors and processors alike.

.. _improcess-headless-contract:

What a plugin gets for free, and what it must declare
-----------------------------------------------------

Every plugin that goes through the shared run path — the GUI, a workflow, a
batch, live streaming — gets without any hook of its own: a provenance graph
on each result it produces, a version stamp (the ImSwitch version for
built-ins, a digest of the file for drop-ins), the staged save protocol with
the provenance carried in the file, napari endpoints for every layerable
result kind, batch runs, the command line, and replay.  Those live in the
run path and the result classes, not in the plugin.

What the run path cannot invent is the plugin's **parameter contract**:

``default_params()`` (required)
    A class method returning exactly what a freshly opened parameter widget
    hands ``apply``/``process``: the same keys, the same defaults.  It is the
    root of everything headless — the keys a workflow may set, the values a
    headless run starts from, and what the provenance records as the
    effective parameters.  Declare it even when it returns ``{}``: a plugin
    that only *inherits* the empty framework default has declared nothing
    and is treated as **GUI-only** — workflow validation refuses it,
    ``python -m imswitch.improcess.workflows list`` marks it, and the
    provenance of results made with it in the GUI says the step cannot be
    replayed and why.  Declaring it on an intermediate base class of your
    own is fine; any override above the framework base counts.
    A fallback ``apply`` reads (``params.get("tail", 500)``) *is* a
    parameter: put it in the defaults (and return it from ``get_values``),
    or the recorded parameters are not the effective ones.

Widget agreement (checked)
    When the GUI builds the widget it compares ``get_values()`` with
    ``default_params()`` — identical key sets, identical non-volatile
    values, and a lossless codec round trip.  A mismatch is logged as a
    warning and remembered on the class: results made with the plugin from
    then on are recorded non-replayable with the reason, and workflows
    refuse it.  Built-ins and the shipped examples are pinned by a test; for
    your own plugin's tests use the helper::

        from imswitch.improcess.model.plugin_contract import check_plugin_contract

        def test_contract(qapp):
            assert check_plugin_contract(MyProcessor) == []

    Machine-dependent widget defaults (a model path found at import time)
    go in ``default_params_volatile`` so the value comparison skips them.

``extra_param_keys`` (optional)
    Keys a workflow may set beyond the defaults, for a setting no widget
    default names (MoNaLISA's ``scan_params``).  It only *permits* a key; it
    injects and records nothing.

``output_spec()`` (optional)
    One port ``out`` unless you say otherwise: named ports when ``apply``
    returns a ``ProcessorOutput`` with ``keys``, a pattern when the ports
    depend on the data.

``params_version`` / ``migrate_params`` / ``encode_params`` / ``decode_params`` (optional)
    Only when the parameter schema changes over time, or parameters carry
    objects rather than JSON scalars.

``prepare_params(data_obj, params)`` (reconstructors, optional)
    Complete parameters from the file before a headless run, as the GUI does
    behind the user's back.

Beyond parameters, the checklist an author should walk through:

* **Inputs**: ``min_inputs``/``max_inputs`` and ``check_inputs`` for a
  processor that consumes several results (see the next section).
* **ROIs are opt-in**: ``accepts_roi = True`` (and ``roi_modes``) lets the
  region chooser restrict a run; ``preserves_grid = True`` says the output is
  pixel-aligned with its input, which is what lets ROIs and imported napari
  layers share its coordinate space.
* **Custom result types**: never override ``save()`` — that is the protocol
  itself, and overriding it skips staging, the provenance companion and the
  receipt.  Set ``kind``, ``supported_formats``, and implement ``plan_save``
  (name every file, companions included) and ``write_files`` (write them
  into the staging paths given, embedding the document where the container
  allows).  The photophysics example does this for a CSV curve with a
  ``.provenance.json`` companion.  napari conversion is automatic only for
  the known layerable kinds (``image``, ``composite``, ``rgb``, ``labels``,
  ``localization``); a new kind needs its own adapter.
* **Registration**: built-ins go in the class maps above; drop-ins
  (processors and reconstructors) are discovered from the plugins folder.

Two optional class attributes refine the UX without any extra method:

* ``is_pass_through = True`` opts the plugin into the auto-render path
  (see *Pass-through reconstructors* above).  Use only when
  ``process()`` performs no real signal processing — otherwise every
  dataset load would silently trigger your compute step.
* ``default_save_subdir = "deskew"`` (or another short folder name)
  changes the subdirectory under which the file watcher writes outputs
  for this plugin.  Defaults to ``"rec"``.

A ``Processor`` follows the same pattern but its ``apply(result, params)``
takes a ``ProcessingResult`` and returns a new one — see
:py:mod:`imswitch.improcess.processors.drift_correct` as a reference.
Declare the semantic result kinds your processor handles with the ``kinds``
class attribute (default ``("image",)``), and keep ``applies_to()`` about
shapes and axis labels — UI compatibility is decided by ``accepts()``, which
checks both.  Likewise, if your result subclass is not a calibrated intensity
image, set its ``kind`` class attribute (``labels``, ``table``, ``curve``,
``localization``, ``rgb`` or ``composite``) so image processors are not
offered on it.

To publish plots in the graph panel, implement ``plot_payloads()`` on the
returned ``ProcessingResult`` and return ``PlotPayload`` objects from
``imswitch.improcess.model``.

Drop-in analysis plugins
========================

Adding a plugin by editing the ImProcess source tree is fine for built-ins,
but ImProcess also supports **Picasso-style drop-in plugins**: a single ``.py``
file dropped into a user folder is discovered at startup and becomes a fully
integrated plugin with no packaging and no UI code.  A ``Processor`` in the
file becomes an analysis tool — parameter panel, result-kind gating and
results-table / graph integration; a ``Reconstructor`` appears in the
reconstructor picker of the Parameters dock, with its parameter widget, the
file watcher, multidata runs and workflows behind it.

This is deliberately separate from the *device* plugin system
(:doc:`devices/plugins`), which uses pip-installed packages and entry points for
hardware.  Analysis processors are the low-friction case and get a low-friction
path.

Using a plugin
--------------

#. In any ImProcess window, choose **Plugins → Add plugin file…** and pick
   the ``.py`` file: it is copied into the plugins folder and the plugins are
   reloaded in one step.  Or choose **Plugins → Open plugins folder…** and
   drop the file in yourself.  The folder is ``~/.imswitch/improcess_plugins/``
   and is created on first use with an inert ``_example_plugin.py`` template
   (underscore-prefixed files are ignored by discovery).  Ready-to-copy
   examples live in ``examples/improcess_plugins/`` (``invert.py``,
   ``gaussian_blur.py``, and the reconstructor ``frame_average.py``).
#. If you copied the file by hand, choose **Plugins → Reload plugins** (or
   restart ImProcess).
#. A processor appears in the **Load plugin** dropdown in the Plugins toolbar;
   load it, select a compatible result and run it.  A reconstructor appears in
   the reconstructor picker at the top of the Parameters dock; pick it and use
   *Reconstruct current* (or the multidata actions, or the file watcher) as
   with any built-in.

Reloading re-scans the folder, so newly added or removed plugins take effect
immediately.  An edited processor's new code is used the next time its panel
is opened (an already-open panel keeps the version it was built with until it
is closed and reopened).  An edited reconstructor takes effect at once: if it
is the active one it is swapped in and its parameter widget rebuilt; a plugin
whose file did not change keeps its instance and its widget state; a removed
active reconstructor hands over to the first registered one.  While a
reconstruction is running the reconstructors are left untouched and the
status bar says so — reload again when it has finished — so a running job
never straddles two versions of one plugin.

Installing from the online store
--------------------------------

**Plugins → Browse online plugins…** opens a store that lists
plugins from the `Improcess-plugins
<https://github.com/Imswitch2/Improcess-plugins>`_ registry.  Each entry can be
installed, updated (when the registry offers a newer version) or uninstalled;
installing downloads the plugin's ``.py`` into the plugins folder and records
the version in a hidden ``.installed.json`` sidecar.  A one-time confirmation
precedes the first install, because installed plugins run arbitrary Python at
ImProcess startup — install only plugins you trust.  Plugins that require a
newer ImProcess than you are running are shown but not installable.  The store
re-scans the folder after any change, so an installed plugin is available
immediately.

Writing a plugin
----------------

A plugin file defines an ordinary ``Processor`` subclass:

.. code-block:: python

    from imswitch.improcess.processors.base import Processor
    from imswitch.improcess.model.array_result import ArrayProcessingResult


    class InvertProcessor(Processor):
        name = "Invert"
        id = "example.invert"        # unique, dotted, user-namespaced
        category = "User"
        kinds = ("image",)           # result kinds this accepts

        @classmethod
        def default_params(cls):
            return {}                # what a fresh widget hands apply(); see below

        @property
        def applies_to(self):
            return lambda result: getattr(result.data, "ndim", 0) >= 2

        def make_param_widget(self, parent):
            from qtpy import QtWidgets

            widget = QtWidgets.QWidget(parent)
            widget.get_values = lambda: {}       # -> params passed to apply()
            return widget

        def apply(self, result, params):
            data = np.asarray(result.data)       # may be a lazy view over the file
            return ArrayProcessingResult(
                name=f"{result.name} (inverted)",
                data=data.max() - data,
                axis_labels=list(result.axis_labels),
            )

The contract is the same as a built-in processor: a unique dotted ``id``,
``kinds`` (one or more of ``image``, ``labels``, ``table``, ``curve``,
``localization``, ``rgb``, ``composite``), an ``applies_to`` shape/axis gate,
``make_param_widget`` returning a widget with ``get_values() -> dict``, a
pure ``apply(result, params)`` returning a new ``ProcessingResult``, and
``default_params()`` declaring the parameters (see
:ref:`improcess-headless-contract`).  Built-in ids always win a collision,
so a stray file cannot shadow a core processor.

A drop-in reconstructor is the same file shape around a ``Reconstructor``
subclass, with the contract of *Writing a new plugin* above: ``name``, ``id``,
``file_extensions``, ``default_params()``, ``make_param_widget``,
``make_metadata_dialog`` (``None`` when there is no acquisition metadata to
ask for) and ``process(data_obj, params, context=None)`` turning the raw
``DataObj`` into a result.  ``examples/improcess_plugins/frame_average.py``
is a complete one, with a parameter.  One file may define both kinds.

Processors that consume several results
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``Processor`` declares how many results one run consumes:

.. code-block:: python

    class BlendProcessor(Processor):
        min_inputs = 2
        max_inputs = None            # None = unbounded; the default is 1..1

        def check_inputs(self, results):
            """(ok, reason) — the reason is shown to the user."""
            ok, reason = super().check_inputs(results)   # count + accepts()
            if not ok:
                return ok, reason
            return combine_compatibility(results, mode="stack")

        def apply(self, result, params):
            inputs = params["results"]   # ordered, as picked in the UI
            ...

When ``max_inputs != 1`` the UI hands the ordered inputs to ``apply`` as
``params["results"]`` (with ``result`` being ``results[0]``), and the generic
processor panel shows the multi-result picker instead of a single input combo.
``check_inputs`` is what the picker calls to decide whether *Run* is enabled;
returning a reason is how the panel explains a refusal, so return one instead
of a bare ``False``.  Keep the check metadata-only — it runs on every
selection change, and materializing a lazily-backed result would read it from
disk.

Single-input processors need no opt-in to run over several reconstructions:
because ``apply`` is a pure function of one result, the panel's *Apply to*
scope simply calls it once per result.

.. warning::

   Drop-in plugins run arbitrary Python at ImProcess startup.  Only add files
   from sources you trust.  Loading is tolerant — a broken plugin is logged and
   skipped, never blocking startup — but a malicious plugin has full access to
   your machine, exactly as any Python you run.

See also
========

* ``docs/design/plans/imreconstruct-2-0.md`` — unified Milestone 12 design
  and per-layer audits
* :doc:`improcess-napari-plugins` — sending results to installed napari
  plugins (dock widgets and readers), and taking layers back
* :doc:`improcess-workflows` — reconstructing and processing without the
  GUI: batch workflows, ports, saves, binding, replay
* :doc:`gui` — main GUI overview (Imcontrol)
* :doc:`modules` — list of Imswitch2 modules
