************************
WFS Workflow Cookbook
************************

Overview
========

The ImSwitch model layer includes a collection of **headless workflow
primitives** ported from the Widefield-Starss (WFS) microscopy project.
These workflows encapsulate complex multi-step acquisition routines
that previously lived in GUI controller code — recording polarisation-resolved
image stacks, running Z-stacks with autofocus, tiling large areas, and
scanning multi-well plates.

Unlike the widget-bound ``api.imcontrol`` scripting interface, these
workflows run independently of the GUI. They take hardware facades and
parameter dataclasses as input, execute the routine, and return results
or write to disk. This makes them testable, composable, and suitable for
batch automation from scripts or external processes.

Anatomy of a Workflow Script
=============================

A typical workflow script has four parts: imports, facade construction,
parameter setup, and execution. Here is a minimal example that acquires
a polarisation-resolved image stack:

.. code-block:: python

   # 1. Import workflow classes
   from imswitch.imcontrol.model.workflows import (
       RotatorPresets,
       WidefieldStarssWorkflow,
       WidefieldStarssParams,
   )

   # 2. Build the facade via the API helper (the controller wraps
   #    build_facade_from_master so you do not need to touch the
   #    internal master controller).
   facade = api.imcontrol.buildWorkflowFacade(
       laser_aliases={
           "488": "488 (EXC) sn27311",
           "405": "405 (ACT) sn26647",
       },
       detector_name="Kiralux",
       xy_positioner_name="XY",
       z_positioner_name="Z",
       hwp_name="HWP",
       qwp_name="QWP",
       # Optional script-level H/V angle overrides. If omitted, the facade
       # reads managerProperties.workflowPresets from the setup JSON.
       hwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
       qwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
   )

   # 3. Configure parameters
   params = WidefieldStarssParams(
       pin488=8,
       pin405=6,
       camerapin=11,
       start488=0,
       start405=25000,
       start_camera=0,
       width488=20000,
       width405=20000,
       width_camera=50000,
       dwelltime=50000,
       delay_time=0,
       frame_number=20,
       move_waveplate=True,
       record_h=True,
       record_v=True,
   )

   # 4. Execute
   workflow = WidefieldStarssWorkflow(facade, params)
   workflow.run()
   getLogger().info("WidefieldStarss complete")

**Line-by-line breakdown:**

``api``
    The global scripting API object, available in every script executed
    from the ImSwitch Scripting widget. ``api.imcontrol`` is the main
    microscope API; ``api.imcontrol.buildWorkflowFacade(...)`` is the
    helper for assembling a workflow facade.

``api.imcontrol.buildWorkflowFacade``
    Wraps ImSwitch managers in WFS-shaped sub-facades. Resolves managers
    by name from the running setup and returns a ``MicroscopeFacade``
    object with attributes ``laser_con``, ``cam``, ``trig``,
    ``stage_con``, ``z_stage_con``, ``rotator_hwp``, ``rotator_qwp``.
    Under the hood this calls ``build_facade_from_master`` (also
    importable directly for headless test code).

``laser_aliases``
    Maps short logical names (``"488"``, ``"405"``) to the full device
    names defined in your setup JSON. Decouples script logic from cosmetic
    names — see **Mapping laser names** below.

``WidefieldStarssParams``
    A dataclass holding all parameters for one recording sequence: Teensy
    pin assignments, pulse timings (microseconds), frame count, and file
    paths. All timing values are integers in microseconds.

``WidefieldStarssWorkflow``
    The workflow object. Constructed with the facade and params; calling
    ``.run()`` moves the wave plates, fires pulse sequences via Teensy,
    grabs frames from the camera, saves stacks to TIFF, and computes a
    quick Stokes/anisotropy summary.

Compatibility:
    Older scripts may still import ``RecordingWorkflow`` and
    ``RecordingParams`` from ``imswitch.imcontrol.model.workflows`` or from
    ``imswitch.imcontrol.model.workflows.recording``. Those names are kept as
    aliases, but new scripts should use ``WidefieldStarssWorkflow`` and
    ``WidefieldStarssParams``.

Rotator Presets
===============

``move_to_h()`` and ``move_to_v()`` use calibrated H/V angles from each
``RotatorFacade``. There are two ways to provide those angles:

1. Put persistent defaults in the setup JSON for each rotator manager:

   .. code-block:: json

      "rotators": {
          "HWP": {
              "managerName": "ElliptecRotatorManager",
              "managerProperties": {
                  "workflowPresets": {
                      "h_deg": 0.0,
                      "v_deg": 90.0
                  }
              }
          }
      }

2. Override them from a script when building the facade:

   .. code-block:: python

      from imswitch.imcontrol.model.workflows import RotatorPresets

      facade = api.imcontrol.buildWorkflowFacade(
          # ... laser/camera/stage names ...
          hwp_name="HWP",
          qwp_name="QWP",
          hwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
          qwp_presets=RotatorPresets(h_deg=12.5, v_deg=102.5),
      )

Script-level presets take precedence over setup JSON. If neither is provided,
the fallback is ``h_deg=0.0`` and ``v_deg=90.0``.

The MicroscopeFacade
====================

``api.imcontrol.buildWorkflowFacade(...)`` returns a ``MicroscopeFacade``
with the following sub-facades. Each wraps one or more ImSwitch managers
and presents a WFS-compatible API:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Sub-facade
     - Key methods

   * - ``laser_con``
     - ``laser_on(names)``, ``laser_off(names)``,
       ``set_constant_power(names, powers)``,
       ``set_triggered_mode(names, powers)``,
       ``set_modulation_mode(names)``

   * - ``cam``
     - ``prepare_acquisition(n_frames)``, ``start_acquisition()``,
       ``stop_acquisition()``, ``wait_for_frame(timeout_s)``,
       ``get_data()``

   * - ``trig``
     - ``snap_trigger(laser_pin, camera_pin, exposure_us)``,
       ``command(...)``, ``Sendsignal(...)``

   * - ``stage_con``
     - ``move_to(x, y)``, ``get_position()``,
       ``jog_start(axis, sign)``, ``jog_stop(axis)``

   * - ``z_stage_con``
     - ``read_pos_um()``, ``set_pos_um(value_um)``,
       ``activate_ext_control()``, ``deactivate_ext_control()``

   * - ``rotator_hwp``
     - ``move_abs(angle)``, ``move_rel(angle)``, ``position()``,
       ``move_to_h()``, ``move_to_v()``

   * - ``rotator_qwp``
     - ``move_abs(angle)``, ``move_rel(angle)``, ``position()``,
       ``move_to_h()``, ``move_to_v()``,
       ``chained_move_to_h(callable)``, ``chained_move_to_v(callable)``

Not all workflows require all sub-facades. For example, ``ZStackWorkflow``
only uses ``cam``, ``z_stage_con``, and ``laser_con``; attempting to use
``trig`` when no pulse generator is configured will raise an error (see
**Troubleshooting** below).

Customising Parameters
======================

Every workflow constructor takes a ``*Params`` dataclass. You can
instantiate it inline with all required arguments:

.. code-block:: python

   from imswitch.imcontrol.model.workflows import ZStackParams, ZStackWorkflow

   params = ZStackParams(
       n_planes=50,
       step_um=1.0,
       laser_pin=8,
       camera_pin=11,
       pulsed=True,
       laser_power_488_mw=5.0,
       exposure_us=50000,
   )

   wf = ZStackWorkflow(facade, params)
   stack, z_positions = wf.run(save_stack=True)

If your acquisition routine requires changing only one or two parameters
between runs, you can mutate the dataclass after construction or use
keyword overrides:

.. code-block:: python

   # Inline override
   params = ZStackParams(
       n_planes=50,
       step_um=1.0,
       laser_pin=8,
       camera_pin=11,
       pulsed=True,
       laser_power_488_mw=5.0,
       exposure_us=50000,
   )

   # First run: 50 planes
   wf = ZStackWorkflow(facade, params)
   wf.run(save_stack=True)

   # Second run: 100 planes (mutate params)
   params.n_planes = 100
   wf_deeper = ZStackWorkflow(facade, params)
   wf_deeper.run(save_stack=True)

Alternatively, subclass the params dataclass to set site-specific defaults:

.. code-block:: python

   from dataclasses import dataclass
   from imswitch.imcontrol.model.workflows import WidefieldStarssParams

   @dataclass
   class MyLabWidefieldStarssParams(WidefieldStarssParams):
       pin488: int = 8
       pin405: int = 9
       camerapin: int = 10
       start488: int = 500
       start405: int = 600
       start_camera: int = 0
       width488: int = 50000
       width405: int = 50000
       width_camera: int = 60000
       dwelltime: int = 80000
       delay_time: int = 10000
       frame_number: int = 100

   # Now you only override what changes
   params = MyLabWidefieldStarssParams(frame_number=200)

Mapping Laser Names
===================

Workflow code uses short logical names like ``"488"`` and ``"405"`` to
refer to lasers. Your setup JSON, however, may define devices with longer
cosmetic names like ``"488 (EXC) sn27311"`` or ``"Laser 488nm Oxxius"``.

The ``laser_aliases`` dictionary on
``api.imcontrol.buildWorkflowFacade`` bridges this gap:

.. code-block:: python

   facade = api.imcontrol.buildWorkflowFacade(
       laser_aliases={
           "488": "488 (EXC) sn27311",
           "405": "Laser 405nm Oxxius",
       },
       detector_name="Kiralux",
       # ... other manager names
   )

Now when a workflow calls ``facade.laser_con.set_constant_power(["488"], [5.0])``,
the facade resolves ``"488"`` to ``"488 (EXC) sn27311"`` and calls the
correct laser manager.

If your setup JSON uses simple names that match the logical names, you
can omit ``laser_aliases`` or pass an identity mapping:

.. code-block:: python

   laser_aliases={"488": "488", "405": "405"}

**Why separate logical from physical names?**

Scripts become portable across setups. A colleague with a different laser
brand (different serial numbers, different JSON device names) can run the
same script by updating only the ``laser_aliases`` dictionary, not the
workflow logic.

Bundled Example Scripts
=======================

Representative scripts live under
``imswitch/_data/user_defaults/scripts/wfs/``. They are designed to be copied
into the Scripting widget and edited for the active microscope setup:

``01_WidefieldSTARSS_example.py``
    Basic polarisation-resolved WidefieldStarss acquisition.

``02_zstack.py``
    Z-stack acquisition in hardware-triggered or software snap mode.

``03_cwstarss.py``
    CW-STARSS photoselection sequence for H/V polarisations.

``04_calibration.py``
    HWP/QWP sweep for polarisation calibration.

``05_tiling.py``
    Overview-only spiral tiling. Cell targeting is disabled in this example.

``06_defocus_scan.py``
    Composite defocus scan that runs WidefieldStarss at each Z plane.

``07_serial_cwstarss.py``
    CWSTARSS power sweep across spiral stage positions.

``08_multi_well_tiling.py``
    Multi-well tiling with per-well autofocus.

``09_AutoWidefieldSTARSS_example.py``
    Automated overview tiling, cell segmentation, stage movement to each
    detected cell, and per-cell WidefieldStarss acquisition.

All examples use ``api.imcontrol.buildWorkflowFacade(...)`` and the hardware
names from ``example_kiralux_teensy.json``. Update those names, pin numbers,
powers, output paths, and scan sizes before using the scripts with real
hardware.

Running Headlessly in Tests
============================

Every workflow can run with a ``MockMicroscopeFacade`` instead of the
real hardware facade. This is useful for:

- **Unit tests**: Verify that workflows call the correct facade methods
  with the correct parameters.
- **Integration tests**: Validate that composed workflows (e.g.,
  ``MultiWellTilingWorkflow`` calling ``TilingWorkflow`` and
  ``ZStackWorkflow``) execute the expected sequence of operations.
- **Scripting dry-runs**: Test the logic of a complex multi-step routine
  without hardware access.

Example from the test suite:

.. code-block:: python

   from imswitch.imcontrol.model.workflows import (
       ZStackWorkflow,
       ZStackParams,
       build_mock_facade,
   )

   # Build a mock facade that returns known data
   facade = build_mock_facade()

   params = ZStackParams(
       n_planes=10,
       step_um=2.0,
       laser_pin=8,
       camera_pin=11,
       pulsed=True,
       laser_power_488_mw=5.0,
       exposure_us=50000,
   )

   wf = ZStackWorkflow(facade, params)
   # run() will call mock methods, no hardware involved
   wf.run(save_stack=False)

The mock facade records all method calls in internal counters. Tests
then assert that the expected number of frames were acquired, that the
Z stage moved to the correct positions, etc. See the workflow tests in
``imswitch/imcontrol/_test/unit/test_*_workflow.py`` for complete examples.

Composite Workflows
===================

The single-device workflows are:

**WidefieldStarssWorkflow**
    Polarisation-resolved H/V acquisition. Drives HWP/QWP to preset
    angles, fires a Teensy pulse scheme, grabs N camera frames, saves
    a TIFF and reports a quick Stokes/anisotropy summary.

**ZStackWorkflow**
    Sweeps the Z piezo through N planes, snapping one frame per plane.
    ``run_autofocus()`` fits a gradient-energy curve to find best focus.

**CWSTARSSWorkflow**
    Photoselection sequence per polarisation: pre-bleach pulse, stream
    488 nm only, then stream 488 + 405 nm together for the configured
    duration. Saves per-phase TIFF stacks.

**CalibrationWorkflow**
    Sweeps QWP/HWP angles and records quad-pixel intensities for
    polarisation calibration; writes a CSV ready for analysis.

The composite workflows reuse the singles:

**TilingWorkflow**
    Acquires a spiral grid of tiles and stitches them into a large mosaic.
    Internally uses ``WidefieldStarssWorkflow`` or a custom tile callback to
    acquire individual tiles.

    Cell targeting is split by use case. In the main GUI, the tiling widget's
    ``Detect cells`` action segments the stitched overview and displays target
    markers only; it does not move the stage. Automated scripts can use
    ``TilingWorkflow.run_cell_targeting(...)`` with a ``for_each_feature``
    callback to move to each detected target and run a per-cell sub-workflow:

    .. code-block:: python

       def run_per_cell(idx, props, stage_xy):
           widefield_starss_wf.run(measurement_name_addition=f"_cell_{idx + 1}")

       tiling_wf.run_cell_targeting(
           stitched=stitched_image,
           pixel_size_um=0.5,
           canvas_origin_stage=(origin_x_um, origin_y_um),
           for_each_feature=run_per_cell,
       )

**MultiWellTilingWorkflow**
    Iterates over a rectangular grid of well positions (e.g., 4x6 plate).
    At each well, runs ``ZStackWorkflow.run_autofocus()`` to find the
    focal plane, then calls ``TilingWorkflow.run()`` to tile the well.

**DefocusScanWorkflow**
    Executes a series of Z-stack recordings at multiple defocus offsets
    to calibrate phase retrieval algorithms. Uses ``ZStackWorkflow`` and
    ``WidefieldStarssWorkflow`` internally.

**SerialCWSTARSSWorkflow**
    Performs CWSTARSS power-sweep experiments with automated stage positioning.
    Composes a configured ``CWSTARSSWorkflow`` with a spiral XY stage loop.

To use a composite workflow, you instantiate the sub-workflows first,
then pass them to the parent:

.. code-block:: python

   from imswitch.imcontrol.model.workflows import (
       MultiWellTilingWorkflow,
       MultiWellTilingParams,
       TilingWorkflow,
       TilingParams,
       ZStackWorkflow,
       ZStackParams,
   )

   facade = api.imcontrol.buildWorkflowFacade(
       laser_aliases={"488": "488 (EXC) sn27311"},
       detector_name="Kiralux",
       xy_positioner_name="XY",
       z_positioner_name="Z",
   )

   # Configure sub-workflows
   tiling_params = TilingParams(
       n_tiles=25,
       step_units=1560.0,
       laser_pin=8,
       camera_pin=11,
       pulsed=True,
       laser_power_488_mw=5.0,
       exposure_us=50000,
   )
   tiling_wf = TilingWorkflow(facade, tiling_params)

   zstack_params = ZStackParams(
       n_planes=50,
       step_um=1.0,
       laser_pin=8,
       camera_pin=11,
       pulsed=True,
       laser_power_488_mw=5.0,
       exposure_us=50000,
   )
   zstack_wf = ZStackWorkflow(facade, zstack_params)

   # Configure multi-well parameters
   multi_well_params = MultiWellTilingParams(
       n_rows=4,
       n_cols=6,
       well_pitch_x_units=9000.0,  # 9mm in stage units
       well_pitch_y_units=9000.0,
       autofocus_n_planes=20,
       autofocus_step_um=2.0,
       tiling_n_tiles=16,
       measurements_root="~/Data/plate_scan",
   )

   # Run multi-well scan
   multi_wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, multi_well_params)
   multi_wf.run()
   getLogger().info("Multi-well scan complete")

The parent workflow calls ``.run()`` or ``.run_autofocus()`` on the
sub-workflows at the appropriate points in its execution. Each sub-workflow
maintains its own parameters; ``MultiWellTilingWorkflow`` temporarily
overrides the ``ZStackWorkflow`` params during autofocus calls to use
fewer planes and a coarser step size than the main Z-stack configuration.

Troubleshooting
===============

**Symptom: KeyError: "Laser '488' not in facade"**

Cause:
    The ``laser_aliases`` mapping does not include an entry for ``"488"``,
    or the device name it maps to does not exist in your setup JSON.

Fix:
    Check the ``lasers`` section of your setup JSON. Find the exact device
    name (including any serial numbers or suffixes), and add it to
    ``laser_aliases``:

    .. code-block:: python

       laser_aliases={
           "488": "Exact Name From Setup JSON",
       }

    If your setup JSON uses ``"488"`` directly, pass ``{"488": "488"}``.

**Symptom: "TrigFacade.snap_trigger called but neither WFS Teensy nor pulse generator is configured"**

Cause:
    The workflow tried to fire a hardware-triggered pulse sequence, but
    the setup JSON has no ``teensyPulse`` block at the top level (the
    facade resolves the pulse generator from that key).

Fix:
    Add a ``teensyPulse`` block to your setup JSON, or pass
    ``wfs_teensy_port=...`` to ``api.imcontrol.buildWorkflowFacade(...)`` when
    using the WFS Teensy firmware directly. See ``example_kiralux_teensy.json``
    for a working setup-level example.

    If your microscope has no pulse generator, you can only run workflows
    that do not require triggered acquisition (e.g., ``ZStackWorkflow``
    with constant-power illumination, not pulsed).

**Symptom: Workflow raises an exception, but laser stays on / stage keeps moving**

Cause:
    Every workflow wraps its ``run()`` body in ``try/finally`` to ensure
    hardware is returned to a safe state (lasers off, modulation mode
    restored). However, if you forcibly terminate the script from the
    Scripting widget (e.g., by closing the tab or killing the process),
    the finally clause may not execute.

Fix:
    Manually disable lasers from the GUI, or issue a facade cleanup command
    from the Scripting widget:

    .. code-block:: python

       facade.laser_con.laser_off(["488", "405"])
       facade.laser_con.set_modulation_mode(None)

    Better: wrap the workflow call in your own try/finally:

    .. code-block:: python

       try:
           workflow.run()
       except Exception as e:
           getLogger().error(f"Workflow failed: {e}")
       finally:
           facade.laser_con.laser_off(["488", "405"])
           getLogger().info("Lasers disabled")

    This guarantees cleanup even if the workflow itself fails.
