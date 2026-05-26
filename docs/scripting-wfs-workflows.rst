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
       RecordingWorkflow,
       RecordingParams,
   )

   # 2. Build the facade via the API helper (the controller wraps
   #    build_facade_from_master so you do not need to touch the
   #    internal master controller).
   facade = api.imcontrol.workflowFacade.build(
       laser_aliases={
           "488": "488 (EXC) sn27311",
           "405": "405 (ACT) sn26647",
       },
       detector_name="Kiralux",
       xy_positioner_name="XY",
       z_positioner_name="Z",
       hwp_name="HWP",
       qwp_name="QWP",
   )

   # 3. Configure parameters
   params = RecordingParams(
       pin488=8,
       pin405=9,
       camerapin=10,
       start488=500,
       start405=600,
       start_camera=0,
       width488=50000,
       width405=50000,
       width_camera=60000,
       dwelltime=80000,
       delay_time=10000,
       frame_number=100,
       move_waveplate=True,
       record_h=True,
       record_v=True,
   )

   # 4. Execute
   workflow = RecordingWorkflow(facade, params)
   workflow.run()
   getLogger().info("Recording complete")

**Line-by-line breakdown:**

``api``
    The global scripting API object, available in every script executed
    from the ImSwitch Scripting widget. ``api.imcontrol`` is the main
    microscope API; ``api.imcontrol.workflowFacade.build(...)`` is the
    helper for assembling a workflow facade.

``api.imcontrol.workflowFacade.build``
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

``RecordingParams``
    A dataclass holding all parameters for one recording sequence: Teensy
    pin assignments, pulse timings (microseconds), frame count, and file
    paths. All timing values are integers in microseconds.

``RecordingWorkflow``
    The workflow object. Constructed with the facade and params; calling
    ``.run()`` moves the wave plates, fires pulse sequences via Teensy,
    grabs frames from the camera, saves stacks to TIFF, and computes a
    quick Stokes/anisotropy summary.

The MicroscopeFacade
====================

``api.imcontrol.workflowFacade.build(...)`` returns a ``MicroscopeFacade``
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
     - ``snap_trigger(laser_pin, camera_pin, exposure_us, laser_power_mw)``,
       ``snap_trigger_multi(...)``, ``set_pulse_sequence(...)``

   * - ``stage_con``
     - ``move_to(x, y)``, ``get_position()``,
       ``jog_start(axis, sign)``, ``jog_stop(axis)``

   * - ``z_stage_con``
     - ``read_pos_um()``, ``set_pos_um(value_um)``,
       ``activate_ext_control()``, ``deactivate_ext_control()``

   * - ``rotator_hwp``
     - ``move_deg(angle)``, ``move_preset(preset_name)``

   * - ``rotator_qwp``
     - ``move_deg(angle)``, ``move_preset(preset_name)``

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
       laser_name="488",
       laser_power_mw=5.0,
       exposure_us=50000,
       bidirectional=True,
       save_individual=True,
   )

   wf = ZStackWorkflow(facade, params)
   wf.run(save_folder="~/Data/z_stack_001")

If your acquisition routine requires changing only one or two parameters
between runs, you can mutate the dataclass after construction or use
keyword overrides:

.. code-block:: python

   # Inline override
   params = ZStackParams(
       n_planes=50,
       step_um=1.0,
       laser_name="488",
       laser_power_mw=5.0,
       exposure_us=50000,
   )

   # First run: 50 planes
   wf = ZStackWorkflow(facade, params)
   wf.run(save_folder="~/Data/z_stack_50")

   # Second run: 100 planes (mutate params)
   params.n_planes = 100
   wf_deeper = ZStackWorkflow(facade, params)
   wf_deeper.run(save_folder="~/Data/z_stack_100")

Alternatively, subclass the params dataclass to set site-specific defaults:

.. code-block:: python

   from dataclasses import dataclass
   from imswitch.imcontrol.model.workflows import RecordingParams

   @dataclass
   class MyLabRecordingParams(RecordingParams):
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
   params = MyLabRecordingParams(frame_number=200)

Mapping Laser Names
===================

Workflow code uses short logical names like ``"488"`` and ``"405"`` to
refer to lasers. Your setup JSON, however, may define devices with longer
cosmetic names like ``"488 (EXC) sn27311"`` or ``"Laser 488nm Oxxius"``.

The ``laser_aliases`` dictionary on
``api.imcontrol.workflowFacade.build`` bridges this gap:

.. code-block:: python

   facade = api.imcontrol.workflowFacade.build(
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
       laser_name="488",
       laser_power_mw=5.0,
       exposure_us=50000,
   )

   wf = ZStackWorkflow(facade, params)
   # run() will call mock methods, no hardware involved
   wf.run(save_folder="/tmp/mock_z_stack")

The mock facade records all method calls in internal counters. Tests
then assert that the expected number of frames were acquired, that the
Z stage moved to the correct positions, etc. See the workflow tests in
``imswitch/imcontrol/_test/unit/test_*_workflow.py`` for complete examples.

Composite Workflows
===================

The single-device workflows are:

**RecordingWorkflow**
    Polarisation-resolved H/V acquisition. Drives HWP/QWP to preset
    angles, fires a Teensy pulse scheme, grabs N camera frames, saves
    a TIFF and reports a quick Stokes/anisotropy summary.

**ZStackWorkflow**
    Sweeps the Z piezo through N planes, snapping one frame per plane.
    ``run_autofocus()`` fits a gradient-energy curve to find best focus.

**CWSTARSSWorkflow**
    Photoselection sequence per polarisation: pre-bleach pulse, stream
    488 nm only, then stream 488 + 405 nm together for the configured
    duration.

**CalibrationWorkflow**
    Sweeps QWP/HWP angles and records quad-pixel intensities for
    polarisation calibration; writes a CSV ready for analysis.

The composite workflows reuse the singles:

**TilingWorkflow**
    Acquires a spiral grid of tiles and stitches them into a large mosaic.
    Internally uses ``RecordingWorkflow`` or a custom tile callback to
    acquire individual tiles.

**MultiWellTilingWorkflow**
    Iterates over a rectangular grid of well positions (e.g., 4x6 plate).
    At each well, runs ``ZStackWorkflow.run_autofocus()`` to find the
    focal plane, then calls ``TilingWorkflow.run()`` to tile the well.

**DefocusScanWorkflow**
    Executes a series of Z-stack recordings at multiple defocus offsets
    to calibrate phase retrieval algorithms. Uses ``ZStackWorkflow`` and
    ``RecordingWorkflow`` internally.

**SerialCWSTARSSWorkflow**
    Performs CWSTARSS (polarisation-resolved, high-speed, multi-channel)
    experiments with automated stage positioning. Composes
    ``RecordingWorkflow`` and ``ZStackWorkflow``.

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

   facade = api.imcontrol.workflowFacade.build(
       laser_aliases={"488": "488 (EXC) sn27311"},
       detector_name="Kiralux",
       xy_positioner_name="XY",
       z_positioner_name="Z",
   )

   # Configure sub-workflows
   tiling_params = TilingParams(
       n_tiles=25,
       laser_name="488",
       laser_power_mw=5.0,
       exposure_us=50000,
   )
   tiling_wf = TilingWorkflow(facade, tiling_params)

   zstack_params = ZStackParams(
       n_planes=50,
       step_um=1.0,
       laser_name="488",
       laser_power_mw=5.0,
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

**Symptom: "TrigFacade.snap_trigger called but no pulse generator"**

Cause:
    The workflow tried to fire a hardware-triggered pulse sequence, but
    the setup JSON has no ``teensyPulse`` block at the top level (the
    facade resolves the pulse generator from that key).

Fix:
    Add a ``teensyPulse`` block to your setup JSON; see
    ``example_kiralux_teensy.json`` for a working example. The facade
    builder auto-attaches the pulse generator when present.

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
    Manually disable lasers from the GUI or issue an ``api.imcontrol``
    command:

    .. code-block:: python

       # Turn off all lasers
       for laser_name in api.imcontrol._master._setupInfo.lasers:
           api.imcontrol._master._lasers[laser_name].setEnabled(False)

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
