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
  pass-through), and future plugins for SNOUTY deskew, STED
  deconvolution, etc.
* :py:class:`~imswitch.improcess.processors.base.Processor`
  Operates on a ``ProcessingResult`` and returns a new one.  Stackable.
  Modality-agnostic by design — a single ``drift-correct`` works for
  any plugin output that has a time axis.  Future processors:
  registration, denoising, projections, lifetime overlays.

Both shapes are registered with a :py:class:`PluginRegistry`.  The
registry is populated at startup, either from a config block or from
standalone defaults.

Built-in plugins
----------------

============= ============== ====================================================
ID            Type           Purpose
============= ============== ====================================================
monalisa      Reconstructor  MoNaLISA point-scanning SIM (Windows + CUDA DLL)
view-only     Reconstructor  Pass-through; raw frames wrapped as a result
drift-correct Processor      FFT cross-correlation drift correction (T-axis req.)
============= ============== ====================================================

Config schema
=============

To configure which plugins ImProcess loads, add a ``processing`` block
to your Imcontrol setup file (the same JSON you select via
``imcontrol_options.json``)::

    {
        "processing": {
            "reconstructors": ["monalisa", "view-only"],
            "processors":     ["drift-correct"]
        }
    }

The block is optional.  When it is absent (or the setup file cannot be
read at all, e.g. in standalone mode) the registry falls back to::

    reconstructors: ["view-only"]
    processors:     ["drift-correct"]

Only the plugin IDs you list are instantiated.  IDs not in the list
are not registered, even if their code is present.

Example: MoNaLISA-only configuration
====================================

A ready-to-use minimal config ships under
``imswitch/_data/user_defaults/imcontrol_setups/monalisa_processor.json``::

    {
        "processing": {
            "reconstructors": ["monalisa", "view-only"],
            "processors":     ["drift-correct"]
        }
    }

To launch Imswitch2 with *only* ImProcess (no Imcontrol GUI) and *only*
the MoNaLISA reconstructor + drift correction:

1. Set ``modules.json`` to::

       {"enabled": ["improcess"]}

2. Set ``imcontrol_options.json`` to::

       {"setupFileName": "monalisa_processor.json"}

3. Launch the app normally.  At startup the registry is populated with
   ``monalisa``, ``view-only`` and ``drift-correct``; the corresponding
   plugin objects are reachable from the controllers and can be
   inspected programmatically.

.. note::

   Until Phase B.2 lands, the *live* reconstruction button still goes
   through the legacy MoNaLISA path inside
   ``ImProcessMainViewController``.  The reconstructor / processor
   picker UI is not yet wired into the main window — the
   ``processing:`` block today controls which plugins are *registered
   and configured*, not which one the *Reconstruct* button executes.
   Phase B.2 replaces the direct-call path with registry dispatch and
   adds the picker controls.

This is the recommended setup for users who treat Imswitch2 as a
post-processing tool only — e.g. opening MoNaLISA acquisitions taken
on a different machine for batch reconstruction.

Status (Milestone 12)
=====================

ImProcess is delivered in phases, tracked in ``ROADMAP.md`` Milestone 12.

Done (as of writing):

* Rename ``imreconstruct`` → ``improcess`` (Phase A)
* Plugin contracts + registry, MoNaLISA / view-only reconstructors,
  drift-correct processor, drag-and-drop ingest, standalone launch,
  config-driven plugin loading (Phase B.1)
* Cleanup: duplicate file removal, shared U-Net helpers extracted,
  ``PatternFinder.findBestPeak`` arithmetic fix

Pending:

* **Phase B.2** — flip the live reconstruction call path through the
  registry.  Plugin code is present and configurable; the legacy
  direct-call path in ``ImProcessMainViewController`` is still used at
  reconstruct time.  Lands when a Windows + ``GPU_acc_recon.dll``
  setup is available for end-to-end verification.
* **Phase D** — per-modality plugins (SNOUTY deskew, STED / confocal
  averaging + drift, WidefieldSTARSS polarization demux).

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

Register it by adding the class to ``available_plugins`` in
``imswitch/improcess/reconstructors/__init__.py``.

A ``Processor`` follows the same pattern but its ``apply(result, params)``
takes a ``ProcessingResult`` and returns a new one — see
:py:mod:`imswitch.improcess.processors.drift_correct` as a reference.

See also
========

* ``docs/design/plans/imreconstruct-2-0.md`` — unified Milestone 12 design
  and per-layer audits
* :doc:`gui` — main GUI overview (Imcontrol)
* :doc:`modules` — list of Imswitch2 modules
