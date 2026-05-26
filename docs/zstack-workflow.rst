Z-Stack Workflow Usage
======================

The Z-stack workflow provides automated Z-plane acquisition with optional autofocus capabilities.

Basic Usage
-----------

.. code-block:: python

    from imswitch.imcontrol.model.workflows import (
        ZStackWorkflow,
        ZStackParams,
        build_facade_from_master,
    )

    # Build facade from ImControl master controller
    facade = build_facade_from_master(master)

    # Configure Z-stack parameters
    params = ZStackParams(
        n_planes=20,
        step_um=0.5,
        pulsed=False,  # Use software live mode
        laser_power_488_mw=50.0,
        exposure_us=50000.0,
        measurements_root="~/Measurements",
    )

    # Create and run workflow
    workflow = ZStackWorkflow(facade, params)
    stack, z_positions = workflow.run(save_stack=True)

    # stack: numpy array of shape (n_planes, H, W)
    # z_positions: list of Z coordinates in micrometers

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

Autofocus
---------

The workflow includes a gradient-energy based autofocus method:

.. code-block:: python

    # Run Z-stack and automatically move to computed focus position
    stack, z_positions = workflow.run_autofocus(z_start=50.0)

    # The stage will be moved to the computed best-focus position
    # after the Z-stack completes

The autofocus algorithm:

1. Acquires the Z-stack
2. Computes a gradient-energy profile for each plane (Sobel-like metric)
3. Fits a quadratic to the profile
4. Moves the stage to the quadratic vertex (peak focus)
5. Raises ``RuntimeError`` if:

   - Gradient profile is too flat/linear
   - Computed focus falls outside the scanned range

Output
------

TIFF files are saved to::

    ~/Measurements/{YYYY_MM_DD}/zstack_{HHMMSS}.tif

Example::

    ~/Measurements/2026_05_26/zstack_143052.tif

Parameters Reference
--------------------

**ZStackParams fields:**

- **n_planes**: Number of Z planes to acquire
- **step_um**: Step size between planes in micrometers
- **laser_pin**: Teensy digital pin for laser trigger (required if ``pulsed=True``)
- **camera_pin**: Teensy digital pin for camera trigger (required if ``pulsed=True``)
- **pulsed**: If True, use hardware-triggered acquisition. If False, use software live-mode snaps
- **laser_power_488_mw**: Laser power in milliwatts for 488 nm laser
- **exposure_us**: Camera exposure time in microseconds
- **measurements_root**: Root directory for saving measurements

Safety Features
---------------

- Z positions are automatically clamped to the piezo's allowed range
- Laser is returned to modulation mode after acquisition
- Stage is returned to starting position after acquisition
- Cleanup is attempted even if acquisition fails

Implementation Notes
--------------------

- Ported from WFS ``ZStackWorkflow``
- No GUI dependencies (uses facade pattern)
- Compatible with both software and hardware-triggered acquisition modes
- Gradient-energy autofocus matches WFS implementation
