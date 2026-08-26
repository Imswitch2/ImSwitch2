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

File metadata panel
===================

Set ``"metadataPanel": true`` in the ``processing`` block to show the file
metadata panel, or open it at any time from the **Tools** toolbar / menu
(``panel.metadata``, unbound by default).  It shows the complete metadata
hierarchy of a measurement file as a collapsible tree with *Name*, *Value* and
*Type* columns.

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

The panel follows the current data item, so loading a file shows its metadata.
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
enabled, raw frames are rescaled by the linear frame-energy ratio
``E_0 / E_i`` before reconstruction, so every frame carries the first frame's
total energy. (A legacy version of the offline path raised the ratio to the
4th power, overcorrecting bleaching by the cube of the energy loss, while the
live path was already linear; both now agree.) The option is off by default.

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

``Fast Gauss options -> Sampling`` chooses how the footprint samples are
read. ``Bilinear (legacy)`` interpolates the frame at integer offsets from
each focus' fractional center — the historical behavior, kept as the default.
Interpolation low-passes the focus peak, which biases the fitted amplitudes a
few percent low by an amount that depends on each focus' subpixel position.
``Exact pixel`` instead fits the true integer pixels around each focus with
per-focus weights evaluated at the real offsets: no interpolation bias, edge
foci fit only the pixels that exist, and extraction is slightly faster.
Rig output is byte-identical until the mode is switched.

``Fast Gauss options -> Parameter sweep`` is an optional advanced mode for
offline fast-Gauss reconstruction: instead of one reconstruction, the run is
repeated once per value of the chosen parameter (``Pinhole radius (×σ)`` or
``Gaussian sigma (px)``) and the results are stacked along a leading Sweep
axis, which the viewer exposes as a slider — slide through the stack to find
the best setting empirically. Values are entered as a comma-separated list or
an inclusive ``start:step:stop`` range. The pinhole optimum is a genuine
data-dependent tradeoff (smaller buys sectioning and resolution at the price
of noise; beyond ~2–2.5 sigma at the standard period-to-sigma ratio,
neighbor-focus crosstalk bleeds in), which is exactly what the sweep makes
visible. Sweeping the pinhole radius forces ``Circular pinhole`` footprint
mode. Saving a sweep with a single timepoint writes one ImageJ hyperstack
with the sweep on the T axis and each plane labeled by its value; with
multiple timepoints, one file per value is written. Sweep results cannot be
consolidated across datasets.

Pattern localization no longer needs a good period guess: a 2D spectral
detection first recovers the illumination lattice (period, offset and any
rotation) and seeds the precise 1D refinement with it, so patterns coarser
or finer than the widget values localize correctly in both the live and
offline paths. The detection searches a period band derived from the rough
pattern scale (the widget's period values), because real summed stacks carry
low-frequency *sample* structure whose spectral peaks would otherwise
out-compete the pattern's, and only accepts peaks that stand far above the
band's noise floor — patternless data fails with a readable error instead of
returning a junk basis.

``Fast Gauss options -> Pattern geometry`` chooses the offline reassignment
for non-rectangular illumination patterns. The rectangular pipeline places
one amplitude per output pixel by integer arithmetic, which only works when
an axis-aligned grid is scanned in steps subdividing its periods; any other
Bravais lattice — a rotated square ("diamond") pattern, hexagonal — lands
its samples off the square raster. The **general-lattice** path detects the
lattice, extracts per-focus amplitudes at the detected centers (always with
exact-pixel per-focus weights), assigns each amplitude its sample-space
position ``focus + scan offset``, resolves the scan orientation by total
variation exactly as the rectangular path does, and grids the scattered
samples onto a square output raster of pitch equal to the scan step by
bilinear splatting with weight normalization. Output pixels the scan never
covered are NaN rather than silently zero, and the diagnostics record the
detected lattice, chosen orientation, coverage, and how many lattice unit
cells the scan area spans (1.0 = every sample position visited once — a
useful cross-check of the scan geometry and pixel size). The default
``Auto`` detects the pattern and keeps axis-aligned grids on the exact
legacy pipeline — rectangular data reconstructs bit for bit as before —
while measurably non-rectangular patterns reroute automatically. The general
path needs a correct ``Pixel size`` (scan offsets are placed in camera
pixels), currently supports a single line step, and is offline-only; live
reconstruction remains rectangular.

The general path also keeps the **pre-gridding result**: every sample's
position and extracted intensity (the spot cloud, before any interpolation)
is retained on the result and written as ``<name>_spots.csv`` next to the
saved TIFF (columns ``timepoint, frame, focus, x_px, y_px, x_nm, y_nm,
intensity``) — so custom gridding or artifact analysis can start from the
raw localized samples.

The fast-Gauss offline mode intentionally has the same geometry scope as the
live path: a 2D Right-Left / Up-Down scan, one Z slice, and optional
timepoints. Use the default ``MoNaLISA`` method for the full coefficient-based
pipeline.

Enhanced confocal (ISM)
=======================

The third ``Reconstruction method`` treats a MoNaLISA scan as what it also
is: a massively parallel image-scanning-microscopy measurement. Each focus's
emission spot is *imaged* rather than integrated, and the camera pixel at
offset ``d`` from a focus predominantly sees the specimen at ``factor × d``
beside the excitation spot, where the ideal factor is
``σ_exc² / (σ_exc² + σ_det²)`` — 0.5 for equal-width PSFs, slightly less
with a Stokes-shifted detection PSF. Instead of fitting anything, every
footprint pixel's raw value is deposited at
``focus + scan offset + factor × (pixel − focus)`` and gridded: open-pinhole
photon collection with up to √2 resolution gain over the confocal
equivalent. ``ISM options → Reassignment factor`` is the single parameter
(0 degenerates to a binned open-pinhole confocal), and it can be swept like
the fast-Gauss parameters to find the sharpest setting empirically. The
footprint/pinhole options are shared with Fast Gauss, and the method works
identically for rectangular and non-rectangular lattices (the reassigned
positions never form a square raster anyway; the pattern is detected
automatically, falling back to the widget's rectangular fields).

One geometric difference from co-scanned detector-array ISM is handled
internally: the parallel scan covers each specimen point once, so an output
point only receives a partial, position-dependent subset of detector
offsets, and a plain sum or mean would imprint a tile-scale collection
ripple. Each pixel is therefore treated as measuring the specimen with gain
``g = envelope(d)`` and combined inverse-variance weighted
(``Σ g·value / Σ g²``), which makes the signal gain exactly uniform.
Background is not modeled — a constant camera offset becomes a smooth
tile-scale offset pattern — so this method shows haze the fast-Gauss
background fit would remove; in exchange it uses every collected photon and
has no per-focus fit to destabilize near very bright structures.

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
            "reconstructors": ["monalisa", "view-only"],
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
            "reconstructors": ["monalisa", "view-only"],
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
    from imswitch.improcess.model.result import ProcessingResult, ViewMode

    class MyResult(ProcessingResult):
        def save(self, path, fmt="tiff"):
            ...  # write self.data to path

    class MyReconstructor(Reconstructor):
        name = "My modality"
        id = "my-modality"
        file_extensions = ["hdf5", "tiff"]

        def make_param_widget(self, parent):
            ...  # return a QWidget exposing get_values() -> dict

        def make_metadata_dialog(self, parent):
            return None  # or a QDialog for acquisition metadata

        def process(self, data_obj, params):
            data_obj.checkAndLoadData()
            ...
            return MyResult(name=data_obj.name, data=..., axis_labels=[...])

Register it by adding the class to ``_AVAILABLE_RECONSTRUCTOR_CLASSES`` in
``imswitch/improcess/reconstructors/__init__.py``.  Processors use the
analogous ``_AVAILABLE_PROCESSOR_CLASSES`` map in
``imswitch/improcess/processors/__init__.py``.

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

Adding a processor by editing the ImProcess source tree is fine for built-ins,
but ImProcess also supports **Picasso-style drop-in plugins**: a single ``.py``
file dropped into a user folder is discovered at startup and becomes a fully
integrated analysis tool — parameter panel, result-kind gating and
results-table / graph integration — with no packaging and no UI code.

This is deliberately separate from the *device* plugin system
(:doc:`devices/plugins`), which uses pip-installed packages and entry points for
hardware.  Analysis processors are the low-friction case and get a low-friction
path.

Using a plugin
--------------

#. In any ImProcess window, choose **Analyze → Drop-in plugins → Open plugins
   folder…**.  The folder is ``~/.imswitch/improcess_plugins/`` and is created
   on first use with an inert ``_example_plugin.py`` template (underscore-
   prefixed files are ignored by discovery).
#. Drop a ``.py`` file that defines one or more ``Processor`` subclasses into
   that folder.  Ready-to-copy examples live in
   ``examples/improcess_plugins/`` (``invert.py``, ``gaussian_blur.py``).
#. Choose **Analyze → Drop-in plugins → Reload plugins** (or restart ImProcess).
   The processor appears in the **Load tool** dropdown in the analysis toolbar;
   load it, select a compatible result and run it.

Reloading re-scans the folder, so newly added or removed plugins take effect
immediately.  An edited plugin's new code is used the next time its panel is
opened (an already-open panel keeps the version it was built with until it is
closed and reopened).

Installing from the online store
--------------------------------

**Analyze → Drop-in plugins → Browse online plugins…** opens a store that lists
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

        @property
        def applies_to(self):
            return lambda result: getattr(result.data, "ndim", 0) >= 2

        def make_param_widget(self, parent):
            from qtpy import QtWidgets

            widget = QtWidgets.QWidget(parent)
            widget.get_values = lambda: {}       # -> params passed to apply()
            return widget

        def apply(self, result, params):
            data = result.data
            return ArrayProcessingResult(
                name=f"{result.name} (inverted)",
                data=data.max() - data,
                axis_labels=list(result.axis_labels),
            )

The contract is the same as a built-in processor: a unique dotted ``id``,
``kinds`` (one or more of ``image``, ``labels``, ``table``, ``curve``,
``localization``, ``rgb``, ``composite``), an ``applies_to`` shape/axis gate,
``make_param_widget`` returning a widget with ``get_values() -> dict``, and a
pure ``apply(result, params)`` returning a new ``ProcessingResult``.  Built-in
ids always win a collision, so a stray file cannot shadow a core processor.

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
* :doc:`gui` — main GUI overview (Imcontrol)
* :doc:`modules` — list of Imswitch2 modules
