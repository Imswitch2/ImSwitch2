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
   (e.g. MoNaLISA).  See `Example: MoNaLISA-only configuration`_ below.

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
* *Duplicate* — create a new array-backed result from the active result.
* *Crop/Substack...* — create a ranged subset with first/last/step controls for
  every result axis.
* *Max projection* — create a max projection using the projection processor's
  default stack-axis choice.
* *Split stack* — split the active stack into one result per plane along the
  selected stack axis.
* *Split channels* — split a ``C``, ``Channel`` or ``Base`` axis into one
  result per channel.
* *Merge channels* — merge selected compatible grayscale results into a new
  ``C``-axis channel stack.
* *Make composite* — create a composite result that renders a channel-like axis
  as independently-scaled colored display layers.
* *Make RGB* — bake a channel-like axis into a channel-last RGB visualization
  result for display/export.
* *Reset view* — restore the reconstruction viewer camera.

The contrast, LUT and channel-visibility operations are display-only: they
update the Napari image layer and the active
:py:class:`~imswitch.improcess.model.result.ProcessingResult` display settings,
including per-display-layer settings for composite outputs, but do not alter
pixel data.  Duplicate, crop/substack, max projection, merge channels, make
composite and make RGB publish new ``ProcessingResult`` objects into the
reconstruction list. Split stack and split channels publish multiple
``ProcessingResult`` objects and make the final split result current.

The *Analysis tools* toolbar keeps Fiji-like panel shortcuts visible for Graph,
Profile, ROI manager, ROI statistics, Projection, Segmentation and Results.
Runtime-backed buttons use the same loading path as the *Load tool* combo, so
opening a panel registers its processor when needed, raises an existing dock
when it already exists and preserves the runtime-loaded panel in the
dock-layout state.  The Results button raises the built-in results-table dock
directly.

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

**Single source of truth: the active napari layer.** Tool panels (Segmentation,
Profile, ROI Manager, FRC, etc.) operate on the *active* (highlighted) napari
layer.  When you select a result from the reconstruction list, the viewer
automatically:

1. Loads the result's data into the main image layer
2. Renames the layer to match the result's name
3. Re-activates the main layer so it becomes the target for tool operations

This keeps "which image am I processing?" unambiguous: it's always the active
napari layer, and the reconstruction list keeps that layer aligned with your
selection.  When a tool *commits* a new layer (e.g. a segmentation result),
napari activates that new layer — at that point tools operate on it until you
click back to a result or select a different layer in napari's layer list.
Ephemeral preview layers (the segmentation preview) are the exception: they
restore the previously active layer and are excluded from tool source
resolution, so a preview can never become its own input.

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
drift-correct           Processor      FFT cross-correlation drift correction with drift trace plots
projection              Processor      Generic max/mean/sum/median/std axis projections
stack-subset            Processor      Crop/substack by labeled axis ranges
stack-split             Processor      Split a stack axis into one result per plane
channel-split           Processor      Split a channel-like C/Channel/Base axis into one result per channel
channel-merge           Processor      Merge compatible grayscale results into a C-axis channel stack
make-composite          Processor      Render a channel-like axis as colored display layers
make-rgb                Processor      Bake a channel-like axis into channel-last RGB visualization data
segmentation            Processor      Threshold + connected-component labels and ROI export
psf-resolution          Processor      2D Gaussian bead/PSF FWHM and sigma measurements
colocalization          Processor      Pearson, Manders and overlap channel colocalization metrics
frc                     Processor      Fourier ring correlation and single-image FRC resolution estimates
multicolor-registration Processor      Three-color X-strip bead calibration and alignment HDF5 export
multicolor-apply        Processor      Apply saved three-color X-strip alignment to deskewed sample volumes
denoise                 Processor      UNet / UNet+RCAN neural-network denoising (requires torch)
======================= ============== ====================================================

Result graph panel
==================

ImProcess can show a generic graph panel below the reconstruction viewer.  The
panel is controlled by the setup JSON and is hidden unless
``"graphPanel": true`` is set.  When enabled, it renders optional plot payloads
exposed by the currently selected ``ProcessingResult``.

Built-in graph producers include ``drift-correct`` and ``widefield-starss``.
Drift-corrected results expose Y and X drift traces over frame number.
WidefieldSTARSS results expose an anisotropy histogram and region
area-vs-anisotropy scatter plot.  ``frc`` results expose the FRC curve,
threshold curve and cutoff marker.  The same graph contract is intended for
future processing units such as batch summaries, FLIM traces and line-profile
tools.

Projection panel
================

Set ``"projectionPanel": true`` in the ``processing`` block to show an
interactive projection panel below the reconstruction viewer.  It runs on the
active image layer and can project along ``T``, ``Z``, ``C`` or another selected
axis using max, mean, sum, median or standard-deviation modes.  The registered
``projection`` processor exposes the same operation for future processor-chain
UI integration.

FRC panel
=========

Set ``"frcPanel": true`` in the ``processing`` block to show an interactive
Fourier ring correlation panel below the reconstruction viewer.  It runs on the
active image layer and supports two-image FRC across an image axis or
single-image FRC with checkerboard / odd-even splitting.  The registered
``frc`` processor exposes the same analysis path for future processor-chain UI
integration.

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
threshold and connected-component segmentation panel.  It runs on the active
2D image plane, supports manual and Otsu thresholding, minimum-area filtering
and optional Gaussian smoothing, adds a label layer to the viewer, exports
region tables as CSV/JSON and can push exact segmented component masks into the
ROI manager.  Enable the **Preview** checkbox to see a live, reused preview
layer that updates when parameters change or when the viewer slice changes,
making it easier to tune segmentation settings without committing multiple
layers.  The preview layer is shown at 50% opacity to distinguish it from
committed segmentations.

PSF resolution panel
====================

Set ``"psfResolutionPanel": true`` in the ``processing`` block to show a
bead/PSF resolution panel.  It fits a non-rotated 2D Gaussian to the active
image plane or to each ROI Manager entry, reports center, sigma, FWHM,
amplitude, background and RMS fit error, and exports the fit table as CSV or
JSON.  The registered ``psf-resolution`` processor exposes the same Gaussian
fit path for full-image processor-chain use.

Colocalization panel
====================

Set ``"colocalizationPanel": true`` in the ``processing`` block to show a
channel colocalization panel.  It compares two planes from a selected stack
axis, supports full-image or ROI Manager batched analysis, reports Pearson
correlation, Manders M1/M2, overlap coefficient and mean intensities, and
exports the table as CSV or JSON.  The registered ``colocalization`` processor
exposes the same metric path for full-image processor-chain use.

Multicolor panel
================

Set ``"multicolorPanel": true`` in the ``processing`` block to show a
three-color strip registration panel.  It is intended for SNOUTY / OPM /
MS-RESOLFT measurements where a deskewed bead volume contains three color
channels as adjacent X-axis strips.  The panel can split the active ``ZYX`` or
``TZYX`` image layer into three X ROIs, estimate transforms in ``maxproj``,
``volume`` or ``descriptor_3d`` mode, save the calibration as an HDF5 alignment
file and add an aligned bead preview layer to the viewer.

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

Only the plugin IDs you list are instantiated.  IDs not in the list
are not registered, even if their code is present.

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

Pending:

* **Phase B.2** — flip the live reconstruction call path through the
  registry.  Plugin code is present and configurable; the legacy
  direct-call path in ``ImProcessMainViewController`` is still used at
  reconstruct time.  Lands when a Windows + ``GPU_acc_recon.dll``
  setup is available for end-to-end verification.
* **Processor-chain UI** — processors such as ``frc`` are registered and
  testable, but the main window does not yet expose a general processor-chain
  runner.  The FRC panel is available as a direct interactive path meanwhile.
* **Phase D follow-up** — richer per-modality UI: WFS table/layer display and
  batch folder mode, STED / confocal averaging, FLIM overlays and SNOUTY
  validation polish.

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

To publish plots in the graph panel, implement ``plot_payloads()`` on the
returned ``ProcessingResult`` and return ``PlotPayload`` objects from
``imswitch.improcess.model``.

See also
========

* ``docs/design/plans/imreconstruct-2-0.md`` — unified Milestone 12 design
  and per-layer audits
* :doc:`gui` — main GUI overview (Imcontrol)
* :doc:`modules` — list of Imswitch2 modules
