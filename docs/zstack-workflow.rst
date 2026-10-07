Z-Stack Workflow Usage
======================

The Z-stack workflow provides automated Z-plane acquisition with optional autofocus capabilities.

Basic Usage
-----------

Run this from an ImScripting script, where ``api`` is available. The facade
only contains the devices you name; the Z-stack needs a laser, a camera and a
Z positioner, and ``run()`` raises ``RuntimeError`` if any of them is
missing.

.. code-block:: python

    from imswitch.imcontrol.model.workflows import ZStackWorkflow, ZStackParams

    # Map the workflow's laser alias ("488") to the laser name in your setup
    facade = api.imcontrol.buildWorkflowFacade(
        laser_aliases={"488": "<laser name in the setup>"},
        detector_name="<camera name>",
        z_positioner_name="<Z positioner name>",
    )

    # Configure Z-stack parameters
    params = ZStackParams(
        n_planes=20,
        step_um=0.5,
        pulsed=False,  # Use software live mode
        laser_power_488_mw=50.0,
        measurements_root="~/Measurements",
    )

    # Create and run workflow
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=True)

    # stack: numpy array of shape (n_planes, H, W)
    # z_positions: list of Z coordinates in micrometers

    # The workflow leaves the laser on; switch it off yourself
    facade.laser_con.laser_off(["488"])

The stack is centred on ``z_start`` (an optional argument of ``run()``) or,
if it is not given, on the current Z position. The stage first moves to that
centre, then steps from ``centre - (n_planes - 1) * step_um / 2`` upwards.

Hardware-Triggered Mode
------------------------

.. code-block:: python

    params = ZStackParams(
        n_planes=20,
        step_um=0.5,
        pulsed=True,  # Enable hardware triggering
        laser_pin=3,  # Teensy digital pin for laser
        camera_pin=4,  # Teensy digital pin for camera
        laser_power_488_mw=100.0,
        exposure_us=100000.0,
        measurements_root="~/Measurements",
    )

    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=True)

Hardware-triggered mode needs ``facade.trig``. ``buildWorkflowFacade`` creates
it when you pass ``wfs_teensy_port="<serial port>"``, or from the setup's pulse
generator if it has one. Each plane is one laser and camera pulse of
``exposure_us``.

Autofocus
---------

The workflow includes a gradient-energy based autofocus method:

.. code-block:: python

    # Run Z-stack and automatically move to computed focus position
    stack, z_positions = workflow.run_autofocus(z_start=50.0)

    # The stage will be moved to the computed best-focus position
    # after the Z-stack completes

The autofocus algorithm:

1. Acquires the Z-stack (not saved to disk)
2. Computes a gradient-energy profile for each plane (Sobel-like metric)
3. Fits a quadratic to the profile
4. Moves the stage to the quadratic vertex
5. Raises ``RuntimeError`` if:

   - Gradient profile is too flat/linear
   - Computed focus falls outside the scanned range

The sign of the fit is not checked: if the profile curves upwards, the vertex
is its minimum (the least sharp plane) and the stage still moves there. Check
the result when the sample has little structure.

Output
------

TIFF files are saved to::

    {measurements_root}/{YYYY_MM_DD}/zstack_{HHMMSS}.tif

Example::

    ~/Measurements/2026_05_26/zstack_143052.tif

The same date folder receives ``acquisition_metadata.json`` with the
parameters, the ImSwitch2 version, a timestamp and whether the run completed.
The default ``measurements_root`` (also used when it is ``None``) is
``$IMSWITCH_WORKFLOW_MEASUREMENTS_ROOT`` if that variable is set, otherwise
``D:/Measurements`` on Windows and ``~/ImSwitchMeasurements`` elsewhere.

Parameters Reference
--------------------

**ZStackParams fields:**

- **n_planes**: Number of Z planes to acquire
- **step_um**: Step size between planes in micrometers
- **laser_pin**: Teensy digital pin for laser trigger (required if ``pulsed=True``)
- **camera_pin**: Teensy digital pin for camera trigger (required if ``pulsed=True``)
- **pulsed**: If True, use hardware-triggered acquisition. If False, use software live-mode snaps
- **laser_power_488_mw**: Laser power in milliwatts
- **laser_name**: Facade alias of the laser to use (default ``"488"``); it must
  be a key of ``laser_aliases``
- **exposure_us**: Length of the trigger pulse in microseconds, used only when
  ``pulsed=True``. In software mode the camera keeps its current exposure time
- **measurements_root**: Root directory for saving measurements

Safety Features
---------------

- Z positions are clamped to the positioner's range (``_posRangeUm``, or
  0–100 µm for a positioner that does not declare one). Planes beyond the
  range are clamped to its edge, so the stack can contain repeated planes
- Stage is returned to the centre position after acquisition
- Cleanup is attempted even if acquisition fails
- If the camera returns no frame, a warning is logged and a 512 × 512 plane of
  zeros is stored in its place

.. warning::

   The workflow does **not** switch the laser off. In software mode it enables
   the laser at ``laser_power_488_mw`` and leaves it on after the stack, also
   after a failure. In hardware-triggered mode digital modulation stays
   enabled. The clean-up only takes the laser out of scan mode. Call
   ``facade.laser_con.laser_off([params.laser_name])`` when you are done.

Implementation Notes
--------------------

- Ported from WFS ``ZStackWorkflow``
- No GUI dependencies (uses facade pattern)
- Compatible with both software and hardware-triggered acquisition modes
- Gradient-energy autofocus matches WFS implementation
