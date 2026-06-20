*********************************
Time-Resolved Workflow Cookbook
*********************************

Overview
========

ImSwitch includes opt-in, headless workflows for detector-side
time-resolved products from photon-counting scans:

* binned photon-arrival histograms per scan pixel;
* software time-gated STED images;
* tau-STED / FLIM-STED lifetime products.

These workflows use the generic time-resolved detector contract rather
than a Swabian-specific API.  The first implementation is
``SwabianTimeTaggerManager``, but another time tagger can use the same
workflows by exposing the same contract through the workflow facade.

The normal FLIM live view and recording path stay unchanged.  The
advanced products are written through an explicit side channel only when
a script or workflow asks for them.

Build A Facade
==============

Time-resolved workflows need a facade with a ``time_resolved`` detector.
When scripts are run from the ImSwitch Scripting widget,
``api.imcontrol.buildWorkflowFacade(...)`` also attaches ``facade.scan``;
the workflow can then trigger the currently configured ScanWidget scan
and wait for completion.

.. code-block:: python

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

``time_resolved_detector_name``
    Name of a detector entry whose manager implements the time-resolved
    contract.  For Swabian setups this is often ``"FLIM"``.

``facade.scan.run_once()``
    Triggers the current scan workflow using the existing ScanWidget
    configuration and waits for ``sigScanDone``.  The time-resolved
    workflows call this automatically when no explicit acquisition
    callable is passed.

Binned Photon Arrivals
======================

Use ``BinnedPhotonArrivalWorkflow`` when you need the full per-pixel
TCSPC cube.  The saved cube has axes ``("y", "x", "tcspc_bin")`` for
the current Swabian implementation.

.. code-block:: python

   from imswitch.imcontrol.model.workflows import (
       BinnedPhotonArrivalParams,
       BinnedPhotonArrivalWorkflow,
       GateSpec,
   )

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

   params = BinnedPhotonArrivalParams(
       gates=(
           GateSpec("early", 0.5, 2.5),
           GateSpec("late", 2.5, 8.0),
       ),
       timeout_s=120.0,
       measurements_root="D:/Measurements",
       save_h5=True,
       save_tiff=True,
   )

   result = BinnedPhotonArrivalWorkflow(facade, params).run()
   print(result.output_paths["h5"])

Time-Gated STED
===============

Use ``GatedSTEDWorkflow`` to compute one image per software time gate.
The STED excitation/depletion timing remains controlled by the current
scan and TTL setup; the workflow only gates photon arrival times after
acquisition.

Gate intervals are interpreted as ``start_ns <= t < stop_ns``.

.. code-block:: python

   from imswitch.imcontrol.model.workflows import (
       GateSpec,
       GatedSTEDParams,
       GatedSTEDWorkflow,
   )

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

   params = GatedSTEDParams(
       gates=(
           GateSpec("early", 0.5, 2.5),
           GateSpec("late", 2.5, 8.0),
       ),
       capture_cube=False,
       timeout_s=120.0,
       measurements_root="D:/Measurements",
   )

   result = GatedSTEDWorkflow(facade, params).run()
   late_image = result.products.gate_images["late"]

Tau-STED / FLIM-STED
====================

Use ``TauSTEDWorkflow`` when the desired product is a lifetime image,
global decay, and global tau fit for a STED scan.

.. code-block:: python

   from imswitch.imcontrol.model.workflows import (
       LifetimeFitConfig,
       TauSTEDParams,
       TauSTEDWorkflow,
   )

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

   params = TauSTEDParams(
       fit=LifetimeFitConfig(
           method="moment",
           min_counts_per_pixel=20,
           laser_rep_rate_mhz=80.0,
       ),
       capture_cube=False,
       timeout_s=120.0,
       measurements_root="D:/Measurements",
   )

   result = TauSTEDWorkflow(facade, params).run()
   tau_ns = result.products.global_tau_ns

Output Files
============

Workflow output is written under ``measurements_root/YYYY_MM_DD`` unless
``save_folder`` is provided.  HDF5 output uses this schema:

.. code-block:: text

   <measurement>.h5
     attrs/
       created_unix_s
       workflow_name
       backend
       detector_name
       metadata_json
     scan/
       attrs/metadata_json
     time_resolved/
       t_axis_ns
       decay_counts
       intensity
       lifetime_ns        optional
       cube_counts        optional
     gates/
       <gate_name>
         attrs/start_ns
         attrs/stop_ns
     fit/
       attrs/method
       attrs/min_counts_per_pixel
       attrs/laser_rep_rate_mhz
       attrs/peak_bin
       attrs/peak_time_ns
       attrs/global_tau_ns

``save_tiff=True`` additionally writes preview TIFFs for intensity,
lifetime, and each gate image.  ``save_npz=True`` writes a compressed
NumPy archive useful for quick script-side inspection.

Current Limits
==============

The Swabian implementation currently supports explicit time-resolved
workflow products for 2D ``x/y`` scans.  If product capture is enabled
and the scan has an active outer axis such as ``z`` or time, the manager
raises a clear error instead of silently mislabeling dimensions.

Example Scripts
===============

Default scripts are installed under
``imswitch/_data/user_defaults/scripts/timeresolved/``:

* ``01_binned_photon_arrivals.py``
* ``02_gated_sted.py``
* ``03_tau_sted.py``
