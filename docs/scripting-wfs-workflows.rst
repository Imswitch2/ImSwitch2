*******************************************
Scripting Acquisition Workflows (Cookbook)
*******************************************

Overview
========

This cookbook teaches the **general pattern for scripting complex acquisition
workflows** in ImSwitch2. The pattern is generic and applicable to any
microscope modality — the examples throughout use WidefieldSTARSS (WFS)
as the worked example, but the same structure applies to confocal, STED,
light-sheet, or any custom microscope.

A scripted acquisition workflow is a **headless, testable, composable
unit** that encapsulates a multi-step routine: parameter setup, hardware
orchestration, data acquisition, and optional post-processing. Unlike
widget-bound ``api.imcontrol`` scripting, workflows run independently
of the GUI. They take a hardware facade and parameter dataclass as input,
execute the routine, and return results or write to disk. This makes
them testable with mock facades, composable into higher-level sequences,
and suitable for batch automation from scripts or external processes.

**Why use this pattern?**

* **Decoupling**: Workflows talk to a narrow, testable facade instead of
  directly accessing managers and controllers. Your workflow logic becomes
  portable across setups.
* **Testability**: The same workflow code runs against real hardware via
  ``MicroscopeFacade`` or a mock in unit tests via ``MockMicroscopeFacade``.
* **Composability**: Simple workflows (e.g., Z-stack, single tile) become
  building blocks for complex sequences (multi-well plates, tiled mosaics).
* **Batch-friendly**: Run headless from external scripts, scheduled jobs,
  or continuous integration pipelines.

**Worked example: WidefieldSTARSS (WFS)**

The ImSwitch model layer includes a collection of workflow primitives
ported from the Widefield-Starss microscopy project: polarisation-resolved
image stacks, Z-stacks with autofocus, tiling, multi-well plate scanning,
and photoselection sequences. The rest of this guide uses these workflows
as concrete examples of the general pattern.

Anatomy of a Workflow Script
=============================

**General structure**

Every workflow script follows the same four-part structure:

1. **Import workflow classes**: The parameters dataclass, the workflow
   class itself, and any supporting types (e.g., ``RotatorPresets``,
   ``GateSpec``).
2. **Build a facade**: Call ``api.imcontrol.buildWorkflowFacade(...)``
   to wrap the running setup's managers in a facade with the methods your
   workflow needs. Map device names from your setup JSON to logical names
   your workflow expects.
3. **Configure parameters**: Instantiate the workflow's parameter dataclass
   with your acquisition settings (exposure, laser power, step sizes, file
   paths, etc.).
4. **Execute**: Instantiate the workflow with the facade and parameters,
   then call ``.run()``.

**WFS example: polarisation-resolved image stack**

Here is a minimal example that acquires a polarisation-resolved image stack
with the WidefieldSTARSS workflow:

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

**Understanding the pieces (generic)**

``api``
    The global scripting API object, available in every script executed
    from the ImSwitch Scripting widget. ``api.imcontrol`` is the main
    microscope control API; ``api.imcontrol.buildWorkflowFacade(...)``
    is the helper for assembling a workflow facade from the running setup.

``api.imcontrol.buildWorkflowFacade(...)``
    Wraps ImSwitch managers in a ``MicroscopeFacade`` object. Accepts
    keyword arguments for device names (``detector_name``,
    ``xy_positioner_name``, ``z_positioner_name``, etc.) and optional
    mappings (``laser_aliases``, rotator presets). Resolves managers by
    name from the running setup and returns a facade with sub-facades for
    the devices you requested. Under the hood this calls
    ``build_facade_from_master`` (also importable directly for headless
    test code).
    
    **Extending the facade**: If your workflow needs access to a manager
    not yet exposed by the facade, extend the ``build_facade_from_master``
    function in ``imswitch/imcontrol/model/workflows/facade.py`` to wrap
    the new manager in a sub-facade with the methods your workflow needs.

``laser_aliases``
    A dictionary mapping short logical names (``"488"``, ``"405"``) to
    the full device names defined in your setup JSON. This decouples
    script logic from cosmetic device names — see **Mapping device names
    between script and setup** below. When your workflow calls
    ``facade.laser_con.laser_on(["488"])``, the facade translates ``"488"``
    to the actual manager via the alias map.

``*Params`` dataclass
    Every workflow has a corresponding parameter dataclass (e.g.,
    ``WidefieldStarssParams``, ``ZStackParams``). This holds all
    acquisition settings: exposure times, laser powers, step sizes,
    frame counts, file paths, and any workflow-specific options. All
    fields have type hints and defaults; you override what you need.

``*Workflow`` class
    The workflow object. Constructed with the facade and params; calling
    ``.run()`` executes the multi-step routine and returns a result object.
    Some workflows also expose utility methods like ``.run_autofocus()``
    (Z-stack) or ``.run_cell_targeting()`` (tiling).

**WFS-specific notes**

``WidefieldStarssParams``
    Holds Teensy pin assignments, pulse timings (all in microseconds),
    frame count, and file paths for polarisation-resolved acquisition.

``WidefieldStarssWorkflow``
    Moves wave plates to H/V angles, fires pulse sequences via Teensy,
    grabs frames from the camera, saves stacks to TIFF, and computes a
    quick Stokes/anisotropy summary.

Compatibility:
    Older scripts may still import ``RecordingWorkflow`` and
    ``RecordingParams`` from ``imswitch.imcontrol.model.workflows`` or from
    ``imswitch.imcontrol.model.workflows.recording``. Those names are kept as
    aliases, but new scripts should use ``WidefieldStarssWorkflow`` and
    ``WidefieldStarssParams``.

Device-Specific Configuration
=============================

Some facades expose device-specific configuration options that can be
set either in the setup JSON or overridden from the script. This section
shows the general pattern using WFS rotator presets as an example.

**WFS example: Rotator presets**

The ``RotatorFacade`` exposes ``move_to_h()`` and ``move_to_v()`` methods
that move the rotator to calibrated horizontal and vertical polarization
angles. These angles must be provided through one of two mechanisms:

1. **Persistent defaults in the setup JSON** — stored in each rotator
   manager's ``managerProperties.workflowPresets``:

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

2. **Script-level overrides** — passed when building the facade:

   .. code-block:: python

      from imswitch.imcontrol.model.workflows import RotatorPresets

      facade = api.imcontrol.buildWorkflowFacade(
          # ... laser/camera/stage names ...
          hwp_name="HWP",
          qwp_name="QWP",
          hwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
          qwp_presets=RotatorPresets(h_deg=12.5, v_deg=102.5),
      )

Script-level configuration takes precedence over setup JSON. If neither is
provided, the facade falls back to ``h_deg=0.0`` and ``v_deg=90.0``.

**General pattern**

When building workflows for other microscope types, follow the same pattern:
define sensible defaults in your setup JSON's ``managerProperties``, and
allow script-level overrides via facade constructor arguments. Document
both sources in your workflow's usage instructions.

The Facade Pattern
==================

**Why facades?**

Workflows talk to hardware through a **facade** — a thin abstraction layer
that wraps ImSwitch managers and exposes only the methods the workflow needs.
This design has several benefits:

* **Testability**: Swap the real facade (``MicroscopeFacade``) for a mock
  (``MockMicroscopeFacade``) in unit tests. The workflow code stays identical.
* **Portability**: Your workflow logic is decoupled from manager implementation
  details. If a manager's internal API changes, only the facade wrapper needs
  updating.
* **Clarity**: The facade surface documents exactly which hardware operations
  the workflow requires. This makes it easy to see what a workflow does without
  reading its entire implementation.

``api.imcontrol.buildWorkflowFacade(...)`` returns a ``MicroscopeFacade``
object assembled from the managers in your running setup. The facade has
attributes for each requested sub-facade (``laser_con``, ``cam``, ``trig``,
``stage_con``, ``z_stage_con``, ``rotator_hwp``, ``rotator_qwp``, etc.).
Each sub-facade wraps one or more managers and exposes a focused API.

**Available sub-facades (WFS example)**

The WFS workflows use the following sub-facades. Each wraps one or more
ImSwitch managers:

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

**Extending for your workflow**

If your workflow needs hardware not yet exposed by the facade (e.g., an SLM,
filter wheel, or deformable mirror), extend ``build_facade_from_master`` in
``imswitch/imcontrol/model/workflows/facade.py``. Add a new sub-facade class
that wraps your manager, then add corresponding parameters to
``buildWorkflowFacade`` so scripts can pass the device name. Follow the
existing sub-facades as examples — each is typically 20-50 lines of wrapper
code that translates workflow-level method calls into manager-level calls.

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

Mapping Device Names Between Script and Setup
=============================================

**General pattern**

Workflow code often uses short logical device names (e.g., ``"488"``,
``"main_cam"``, ``"XY"``) to keep the logic readable. Your setup JSON,
however, may define devices with longer cosmetic names that include
manufacturer details, serial numbers, or site-specific labels.

The facade builder accepts mapping dictionaries to bridge this gap. When
building the facade, you provide both the logical name the workflow expects
and the physical device name from your setup JSON.

**Laser aliases (WFS example)**

WFS workflows use ``"488"`` and ``"405"`` as logical laser names. If your
setup JSON defines lasers as ``"488 (EXC) sn27311"`` or ``"Laser 488nm Oxxius"``,
provide the mapping via ``laser_aliases``:

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

If your setup JSON already uses the logical names the workflow expects, you
can omit ``laser_aliases`` or pass an identity mapping:

.. code-block:: python

   laser_aliases={"488": "488", "405": "405"}

**Why separate logical from physical names?**

Scripts become portable across setups. A colleague with a different laser
brand (different serial numbers, different JSON device names) can run the
same script by updating only the ``laser_aliases`` dictionary, not the
workflow logic. The same applies to other devices: detectors, positioners,
rotators, etc.

**Extending for other device types**

The same pattern applies to any device the facade exposes. When writing
workflows for other modalities, consider which device names your workflow
code should use as logical handles, and document how to map those to the
user's setup JSON device names via facade parameters.

Bundled Example Scripts (WFS Specifics)
=======================================

The WFS workflow collection includes representative scripts under
``imswitch/_data/user_defaults/scripts/workflows/wfs/``. These are designed to be
copied into the Scripting widget and edited for your active microscope setup:

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

**For other workflows**

When building workflows for your own modality, follow the same pattern:
create example scripts in ``imswitch/_data/user_defaults/scripts/workflows/<your_modality>/``,
include a README with usage instructions, and provide commented templates
that show all common parameter combinations.

Running Headlessly in Tests
============================

**General pattern**

Every workflow can run with a ``MockMicroscopeFacade`` instead of the
real hardware facade. The workflow code is identical — only the facade
changes. This is useful for:

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

**General pattern**

Simple workflows are building blocks; composite workflows orchestrate them
into higher-level sequences. A composite workflow instantiates one or more
sub-workflows, then calls their ``.run()`` methods at appropriate points
in a loop or decision tree. This composition pattern enables complex
routines like multi-well plate scans, tiled mosaics, or serial parameter
sweeps without duplicating acquisition logic.

**WFS example: Single-device workflows**

The single-device WFS workflows are:

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

**WFS example: Composite workflows**

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

Adapting This to Your Own Workflow
===================================

To build acquisition workflows for your own microscope or modality, follow
this checklist:

1. **Define your parameters dataclass**

   Create a ``@dataclass`` subclass with all acquisition settings. Use type
   hints, sensible defaults, and docstrings. Place it in
   ``imswitch/imcontrol/model/workflows/<your_modality>.py``.

2. **Identify the facade methods you need**

   Review the existing sub-facades in ``MicroscopeFacade`` (laser, camera,
   trigger, stage, rotator, etc.). If your workflow needs hardware not yet
   exposed, extend ``build_facade_from_master`` in
   ``imswitch/imcontrol/model/workflows/facade.py`` following the existing
   sub-facade pattern.

3. **Write the workflow run function**

   Create a workflow class with ``__init__(facade, params)`` and a ``.run()``
   method. Call facade methods to execute the acquisition sequence. Wrap the
   body in ``try/finally`` for hardware cleanup.

4. **Add a mock facade test**

   Write a unit test using ``build_mock_facade()`` to verify your workflow
   calls the correct facade methods in the correct order without hardware.
   Place tests in ``imswitch/imcontrol/_test/unit/test_<your_modality>_workflow.py``.

5. **Register script examples**

   Create example scripts in
   ``imswitch/_data/user_defaults/scripts/workflows/<your_modality>/``. Include a
   README with setup instructions and parameter explanations.

6. **Extend buildWorkflowFacade if needed**

   If your facade extension requires new device-name parameters, add them
   to ``buildWorkflowFacade`` in ``WorkflowFacadeController`` so scripts
   can pass device names via ``api.imcontrol.buildWorkflowFacade(...)``.

See the WFS workflows (``imswitch/imcontrol/model/workflows/recording.py``,
``imswitch/imcontrol/model/workflows/z_stack.py``, etc.) and time-resolved
workflows (``imswitch/imcontrol/model/workflows/time_resolved.py``) as
complete worked examples.

Troubleshooting
===============

**General issues**

**Symptom: KeyError when accessing a device via the facade**

Cause:
    The facade builder could not find a device with the requested name in
    your setup JSON, or the ``laser_aliases`` / other mapping does not
    include an entry for the logical name your workflow uses.

Fix:
    Check the corresponding device section of your setup JSON (``lasers``,
    ``detectors``, ``positioners``, etc.). Find the exact device name
    (including any serial numbers or suffixes), and add the correct mapping:

    .. code-block:: python

       facade = api.imcontrol.buildWorkflowFacade(
           laser_aliases={"488": "Exact Name From Setup JSON"},
           detector_name="Exact Detector Name",
           xy_positioner_name="Exact Positioner Name",
       )

    If your setup JSON already uses the logical name the workflow expects,
    pass it directly without an alias.

**Symptom: Workflow raises an exception, but hardware stays in an unsafe state (laser on, stage moving, etc.)**

Cause:
    Every workflow wraps its ``run()`` body in ``try/finally`` to ensure
    hardware cleanup. However, if you forcibly terminate the script (e.g.,
    by closing the Scripting widget tab or killing the process), the finally
    clause may not execute.

Fix:
    Manually disable hardware from the GUI, or issue cleanup commands from
    the Scripting widget:

    .. code-block:: python

       facade.laser_con.laser_off(["488", "405"])  # or your logical laser names
       # ... other cleanup as needed

    Better: wrap the workflow call in your own try/finally to guarantee
    cleanup even if the workflow itself fails:

    .. code-block:: python

       try:
           workflow.run()
       except Exception as e:
           getLogger().error(f"Workflow failed: {e}")
       finally:
           facade.laser_con.laser_off(["488", "405"])
           getLogger().info("Hardware disabled")

**WFS-specific issues**

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

Related Documentation
=====================

* :doc:`scripting` — General scripting overview and API reference.
* :doc:`scripting-time-resolved-workflows` — Time-resolved detector
  workflows for photon-counting products (TCSPC cubes, gated STED,
  tau-STED), using the same facade pattern with a different detector
  contract.
