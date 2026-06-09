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
  Operates on a ``ProcessingResult`` and returns a new one.  Stackable.
  Modality-agnostic by design — a single ``drift-correct`` works for
  any plugin output that has a time axis, and ``frc`` works on 2D image
  planes.  Future processors include denoising, projections and lifetime
  overlays.

Both shapes are registered with a :py:class:`PluginRegistry`.  The
registry is populated at startup, either from a config block or from
standalone defaults.

Built-in plugins
----------------

================== ============== ====================================================
ID                 Type           Purpose
================== ============== ====================================================
monalisa           Reconstructor  MoNaLISA point-scanning SIM (Windows + CUDA DLL)
view-only          Reconstructor  Pass-through; raw frames wrapped as a result
widefield-starss   Reconstructor  H/V WidefieldSTARSS anisotropy maps and region metrics
snouty             Reconstructor  SNOUTY / OPM / MS-RESOLFT lightsheet deskew
snouty-projections Reconstructor  Fast SNOUTY projection-preview stack
drift-correct      Processor      FFT cross-correlation drift correction with drift trace plots
frc                Processor      Fourier ring correlation and single-image FRC resolution estimates
================== ============== ====================================================

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

WidefieldSTARSS pairing
=======================

The ``widefield-starss`` reconstructor analyzes one H/V TIFF pair.  When the
current file name ends in ``_h.tif`` or ``_v.tif`` it auto-loads the matching
counterpart next to it.  Otherwise, set the current file role and select the
counterpart path in the parameter panel.  The parameter panel includes presets
for standard widefield-cell analysis and line-PSF split-detection analysis.

Config schema
=============

To configure which plugins ImProcess loads, add a ``processing`` block
to your Imcontrol setup file (the same JSON you select via
``imcontrol_options.json``)::

    {
        "processing": {
            "graphPanel": true,
            "frcPanel": true,
            "roiStatsPanel": true,
            "reconstructors": ["monalisa", "view-only"],
            "processors":     ["drift-correct", "frc"]
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

Processing-only setup presets
=============================

Ready-to-use minimal configs ship under
``imswitch/_data/user_defaults/imcontrol_setups/``:

.. list-table::
   :header-rows: 1

   * - Setup file
     - Intended use
   * - ``monalisa_processor.json``
     - MoNaLISA reconstruction plus view-only fallback
   * - ``snouty_processor.json``
     - SNOUTY deskew and SNOUTY projection previews
   * - ``widefieldstarss_processor.json``
     - WidefieldSTARSS H/V-pair analysis
   * - ``general_image_processing.json``
     - View-only display, drift correction, FRC, ROI

The MoNaLISA preset has the same shape as the others::

    {
        "processing": {
            "graphPanel": true,
            "reconstructors": ["monalisa", "view-only"],
            "processors":     ["drift-correct"]
        }
    }

To launch Imswitch2 with *only* ImProcess (no Imcontrol GUI) and *only*
the plugins from one of these setup presets:

1. Set ``modules.json`` to::

       {"enabled": ["improcess"]}

2. Set ``imcontrol_options.json`` to::

       {"setupFileName": "general_image_processing.json"}

3. Launch the app normally.  At startup the registry is populated with
   the reconstructors and processors listed in the selected setup file;
   the corresponding plugin objects are reachable from the controllers
   and can be inspected programmatically.

.. note::

   Until Phase B.2 lands, the *live* reconstruction button still goes
   through the legacy MoNaLISA path inside
   ``ImProcessMainViewController``.  The reconstructor / processor
   picker UI is not yet wired into the main window — the
   ``processing:`` block today controls which plugins are *registered
   and configured*, not which one the *Reconstruct* button executes.
   Phase B.2 replaces the direct-call path with registry dispatch and
   adds the picker controls.

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
* Optional ROI statistics panel for full-image or rectangle-ROI area, mean,
  median, standard deviation, min, max and sum
* Cleanup: duplicate file removal, shared U-Net helpers extracted,
  ``PatternFinder.findBestPeak`` arithmetic fix

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
