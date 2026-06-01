# WFS Workflow Example Scripts

This directory contains runnable example scripts for the WFS (Widefield
STARSS) workflows ported to ImSwitch2. They are intended as starting points:
edit the hardware names, output folder, pin numbers, powers, and scan geometry
before running them on a real microscope.

## Prerequisites

- **Setup configuration**: `example_kiralux_teensy.json` (or equivalent)
- **Hardware**: Lasers (488 nm, 405 nm), Kiralux camera, XY/Z positioners, HWP/QWP rotators, Teensy pulse generator
- **Running environment**: ImSwitch Scripting widget (provides global `api` object)

All scripts assume the hardware names match those in
`example_kiralux_teensy.json`. If your setup uses different names, edit the
`laser_aliases`, `detector_name`, `xy_positioner_name`, `z_positioner_name`,
`hwp_name`, and `qwp_name` arguments in the
`api.imcontrol.buildWorkflowFacade(...)` calls.

Rotator H/V angles are read from each rotator's setup JSON
`managerProperties.workflowPresets` block. For a one-off script override, pass
`hwp_presets=RotatorPresets(...)` or `qwp_presets=RotatorPresets(...)` to
`api.imcontrol.buildWorkflowFacade(...)`.

## Available Scripts

### 01_WidefieldSTARSS_example.py
Record polarisation-resolved Widefield STARSS image stacks using Teensy TTL
pulse generation. Acquires horizontal and vertical stacks with interleaved
488 nm signal and 405 nm background phases.

**Output**: TIFF files in `~/ImSwitchMeasurements/`

### 02_zstack.py
Acquire a Z-stack using hardware-triggered or software acquisition mode.

**Output**: TIFF file in `~/ImSwitchMeasurements/`

### 03_cwstarss.py
Run a complete CW-STARSS photoselection experiment: 488-only phase followed by
488+405 combined phase for each H/V polarisation. Streams camera data and saves
per-phase time series.

**Output**: CWSTARSS TIFF stacks under `~/ImSwitchMeasurements/<date>/`

### 04_calibration.py
Calibrate rotators (HWP/QWP) by sweeping angles and capturing images at each
position. Used for polarisation optics characterisation.

**Output**: Calibration CSV under `~/ImSwitchMeasurements/<date>/`

### 05_tiling.py
Acquire a tiled scan on a spiral grid and save individual tiles plus a master H5
file. This example keeps cell targeting disabled so it is overview-only.

**Output**: `Tiling_measurement.h5` and `img_new_*.npy` files in
`~/ImSwitchMeasurements/<date>/tiling_<time>/`

### 06_defocus_scan.py
Acquire a defocus scan by moving through Z offsets and running a
WidefieldStarss acquisition at each plane.

**Output**: One WidefieldStarss acquisition per Z plane under
`~/ImSwitchMeasurements/`

### 07_serial_cwstarss.py
Run CWSTARSS experiments across multiple (488 nm, 405 nm) power combinations,
moving to a new spiral grid position for each combination. Enables systematic
power-sweep studies.

**Output**: CWSTARSS TIFF stacks under `~/ImSwitchMeasurements/<date>/` for each power combination

### 08_multi_well_tiling.py
Acquire tiled scans across multiple wells, with autofocus performed at each well
before tiling.

**Output**: `Tiling_measurement.h5` and optional `img_new_*.npy` files per well in `~/ImSwitchMeasurements/well_r{row}_c{col}/`

### 09_AutoWidefieldSTARSS_example.py
Automated workflow that tiles an overview, segments target cells, moves to each
accepted target, and runs WidefieldStarss at each cell.

**Output**: Tiling overview files plus one WidefieldStarss H/V acquisition per
cell.

## How to Run

1. Open ImSwitch and load your setup configuration (e.g., `example_kiralux_teensy.json`)
2. Open the **Scripting** widget (usually under Tools menu)
3. Load one of the scripts above using the "Load Script" button or paste the script content
4. Click "Run" to execute the workflow
5. Monitor progress in the ImSwitch status bar and console output
6. Check the output directory (`~/ImSwitchMeasurements/` by default) for saved data

**Important**: These scripts **must** be run from the Scripting widget, as they
reference the global `api` object provided by ImSwitch.

## Modifying Parameters

Each script uses a `*Params` dataclass to configure the workflow. Common parameters include:

- **Timing**: `fps`, `duration_s`, `dwelltime`, `exposure_us`
- **Power**: `laser_power_488_mw`, `power_488_mw`, `power_405_mw`
- **Geometry**: `n_tiles`, `step_units`, `n_planes`, `step_um`
- **Hardware**: `pin488`, `pin405`, `camerapin`, `laser_pin`, `camera_pin`
- **Save location**: `measurements_root` (defaults to `~/ImSwitchMeasurements/`)

Refer to `docs/scripting-wfs-workflows.rst` and the workflow source code in
`imswitch/imcontrol/model/workflows/` for complete parameter documentation.

## Troubleshooting

- **"api is not defined"**: Script must be run from the Scripting widget, not from a standalone Python interpreter.
- **"Hardware not found"**: Check that your setup JSON includes the hardware names specified in the script's `api.imcontrol.buildWorkflowFacade(...)` call.
- **Teensy connection errors**: Verify the Teensy device is connected and the port/baud settings match your `teensyPulse` configuration.

For detailed workflow implementation, see `imswitch/imcontrol/model/workflows/`.
