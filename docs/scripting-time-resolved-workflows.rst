**********************************************
Time-Resolved Detector Workflows (Cookbook)
**********************************************

Overview
========

This cookbook describes the **general pattern for detector-side
time-resolved products** from photon-counting scans in ImSwitch2, using
the Swabian TimeTagger backend as the worked example throughout. The
pattern splits into three layers, each usable on its own:

* a **generic detector contract** any TCSPC/time-gating backend can
  implement (``imswitch/imcontrol/model/timeresolved/``);
* **opt-in, headless workflows** built only on that contract
  (``imswitch/imcontrol/model/workflows/time_resolved.py``):

  - binned photon-arrival histograms per scan pixel,
  - software time-gated STED images,
  - tau-STED / FLIM-STED lifetime products;

* a **workflow facade entry** that scripts use to reach both
  (``api.imcontrol.buildWorkflowFacade(...)``).

Because the workflows talk to the contract rather than to a
Swabian-specific API, another time tagger (or any photon-counting
detector that can bin arrival times) plugs into the same workflows by
implementing the same contract — see `Adding a new backend`_.

The normal FLIM live view and recording path stay unchanged. The
advanced products are written through an explicit side channel only when
a script or workflow asks for them.

The detector contract
=====================

The contract lives in
``imswitch/imcontrol/model/timeresolved/detector_contract.py`` as
``TimeResolvedDetectorMixin``. A backend detector manager implements
five methods:

``timeResolvedCapabilities() -> dict``
    Advertises what the backend can do (which products, axis support).

``configureTimeResolvedProducts(config: TimeResolvedScanConfig) -> None``
    Arms product capture for the next scan (gates, optional cube
    capture, optional lifetime fit).

``waitForFinalTimeResolvedProducts(timeout_s) -> TimeResolvedScanProducts``
    Blocks until the products for the completed scan are ready.

``getLastTimeResolvedProducts(copy=True) -> TimeResolvedScanProducts | None``
    Returns the most recent products without waiting.

``clearTimeResolvedProducts() -> None``
    Drops any held products.

The shared types come from
``imswitch/imcontrol/model/timeresolved/types.py``:

* ``TimeResolvedScanConfig`` — what to capture (gates, cube, fit);
* ``TimeResolvedScanProducts`` — what came back (time axis, decay,
  intensity, optional per-pixel cube, gate images, lifetime products);
* ``GateSpec`` — a named ``start_ns <= t < stop_ns`` gate interval;
* ``LifetimeFitConfig`` — fit method and thresholds for lifetime
  estimation.

Backend-agnostic math (decay aggregation, intensity from cube, gate
image computation) is shared in
``imswitch/imcontrol/model/timeresolved/processing.py``, so a new
backend only supplies raw binned data.

The facade entry
================

Time-resolved workflows need a facade with a ``time_resolved`` detector.
When scripts are run from the ImSwitch2 Scripting widget,
``api.imcontrol.buildWorkflowFacade(...)`` also attaches ``facade.scan``;
the workflow can then trigger the currently configured ScanWidget scan
and wait for completion.

.. code-block:: python

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

``time_resolved_detector_name``
    Name of a detector entry whose manager implements the time-resolved
    contract. For Swabian setups this is often ``"FLIM"``. The facade
    wraps the manager in a ``TimeResolvedDetectorFacade`` exposing
    ``configure(config)``, ``wait_for_final(timeout_s)``,
    ``get_last(copy=True)``, ``clear()`` and ``capabilities()``; a
    detector that misses part of the contract is rejected with a clear
    ``TypeError`` naming the missing methods.

``facade.scan.run_once()``
    Triggers one scan with the existing ScanWidget configuration and, by
    default, waits until that scan has ended: a coordinated Scan
    controller reports the end of exactly this request, and a refused or
    failed scan raises. A blocking ``run_once()`` refuses to run on the
    GUI thread, and needs exactly one scan source that can report such an
    exact completion; otherwise pass ``wait=False``. The time-resolved
    workflows call this automatically when no explicit acquisition
    callable is passed.

The workflows
=============

All three workflows share one base (``TimeResolvedScanWorkflow``) whose
``run()`` does: configure the detector → run the acquisition → wait for
final products → save. The acquisition step is deliberately just a
no-argument callable: by default the workflow uses
``facade.scan.run_once()``, but a script can pass any callable (its own
scan sequence, an external trigger) without coupling the workflow to one
scanner implementation.

Each workflow takes a parameters dataclass and returns a
``TimeResolvedWorkflowResult`` with three fields: ``products`` (the
``TimeResolvedScanProducts``), ``save_folder`` and ``output_paths``
(one path per written artifact, keyed by label).

Worked example: binned photon arrivals
======================================

Use ``BinnedPhotonArrivalWorkflow`` when you need the full per-pixel
TCSPC cube. The saved cube has axes ``("y", "x", "tcspc_bin")`` for
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

Worked example: time-gated STED
===============================

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

Worked example: tau-STED / FLIM-STED
====================================

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

Output files
============

Workflow output is written under ``measurements_root/YYYY_MM_DD`` unless
``save_folder`` is provided. HDF5 output uses this schema:

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
lifetime, and each gate image. ``save_npz=True`` writes a compressed
NumPy archive useful for quick script-side inspection.

Adding a new backend
====================

To make another photon-counting detector drive these workflows
unchanged:

1. Implement the five ``TimeResolvedDetectorMixin`` methods on your
   detector manager, returning the shared
   ``TimeResolvedScanProducts`` type.
2. Report honest ``timeResolvedCapabilities()`` and raise a clear error
   from ``configureTimeResolvedProducts`` for scan shapes you do not
   support, rather than mislabeling axes.
3. Reuse the helpers in
   ``imswitch/imcontrol/model/timeresolved/processing.py`` for decay
   aggregation, intensity and gate images — your backend only needs to
   produce binned arrival data.
4. Add a mock mode so the workflows are testable without hardware, then
   run the workflow unit tests under ``imswitch/imcontrol/_test/unit/``.

See ``SwabianTimeTaggerManager`` in
``imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py``
as the complete worked example of the contract implementation.

Backend limits (worked example)
===============================

The Swabian implementation currently supports explicit time-resolved
workflow products for 2D ``x/y`` scans. If product capture is enabled
and the scan has an active outer axis such as ``z`` or time, the manager
raises a clear error instead of silently mislabeling dimensions.

Example scripts
===============

Default scripts are installed under
``imswitch/_data/user_defaults/scripts/workflows/timeresolved/``:

* ``01_binned_photon_arrivals.py``
* ``02_gated_sted.py``
* ``03_tau_sted.py``

Related documentation
=====================

* :doc:`scripting-wfs-workflows` — the general workflow-scripting
  cookbook (parameters dataclasses, facade pattern, composition,
  headless testing) with WidefieldSTARSS as the worked example.
* :doc:`scripting` — the scripting module and ``api.imcontrol`` surface.
* Design plan:
  ``docs/design/plans/time-resolved-detector-workflows.md``.
