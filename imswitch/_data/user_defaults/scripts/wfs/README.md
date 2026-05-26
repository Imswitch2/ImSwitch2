# WFS Workflow Example Scripts

This directory contains runnable example scripts for all WFS (Widefield STARSS) workflows ported to ImSwitch2.

## Prerequisites

- **Setup configuration**: `example_kiralux_teensy.json` (or equivalent)
- **Hardware**: Lasers (488 nm, 405 nm), Kiralux camera, XY/Z positioners, HWP/QWP rotators, Teensy pulse generator
- **Running environment**: ImSwitch Scripting widget (provides global `api` object)

All scripts assume the hardware names match those in `example_kiralux_teensy.json`. If your setup uses different names, edit the `laser_aliases`, `detector_name`, and positioner/rotator names in the `api.workflowFacade.build()` calls.

## Available Scripts

### 01_recording.py
Record polarised CWSTARSS image stacks using Teensy TTL pulse generation. Acquires horizontal and vertical polarisation stacks with interleaved 488 nm (signal) and 405 nm (background) phases.

**Output**: TIFF files in `~/ImSwitchMeasurements/`

### 02_zstack.py
Acquire a Z-stack using hardware-triggered or software acquisition mode.

**Output**: TIFF file in `~/ImSwitchMeasurements/`

### 03_cwstarss.py
Run a complete CW-STARSS photoselection experiment: 488-only phase, 488+405 combined phase, and background phase. Streams camera data and saves brightness time series.

**Output**: CSV files (signal_bwd.csv, combined_bwd.csv, background_bwd.csv) in `~/ImSwitchMeasurements/<timestamp>/`

### 04_calibration.py
Calibrate rotators (HWP/QWP) by sweeping angles and capturing images at each position. Used for polarisation optics characterisation.

**Output**: TIFF file + metadata JSON in `~/ImSwitchMeasurements/`

### 05_tiling.py
Acquire a tiled scan on a spiral grid, automatically stitch tiles into a single image, and optionally run cell segmentation for targeting.

**Output**: PNG stitched image + individual tile .npy files in `~/ImSwitchMeasurements/<timestamp>/`

### 06_defocus_scan.py
Acquire a defocus scan (Z-stack with optional scrambled order) for PSF calibration or 3D structure analysis.

**Output**: Returns data in-memory (no automatic file saving)

### 07_serial_cwstarss.py
Run CWSTARSS experiments across multiple (488 nm, 405 nm) power combinations, moving to a new spiral grid position for each combination. Enables systematic power-sweep studies.

**Output**: CSV files (per power combination) in `~/ImSwitchMeasurements/<timestamp>_<powers>/`

### 08_multi_well_tiling.py
Acquire tiled scans across multiple wells (e.g., multi-well plate), with autofocus performed at each well before tiling.

**Output**: Stitched images + tile files per well in `~/ImSwitchMeasurements/<timestamp>/well_r{row}_c{col}/`

## How to Run

1. Open ImSwitch and load your setup configuration (e.g., `example_kiralux_teensy.json`)
2. Open the **Scripting** widget (usually under Tools menu)
3. Load one of the scripts above using the "Load Script" button or paste the script content
4. Click "Run" to execute the workflow
5. Monitor progress in the ImSwitch status bar and console output
6. Check the output directory (`~/ImSwitchMeasurements/` by default) for saved data

**Important**: These scripts **must** be run from the Scripting widget, as they reference the global `api` object provided by ImSwitch.

## Modifying Parameters

Each script uses a `*Params` dataclass to configure the workflow. Common parameters include:

- **Timing**: `fps`, `duration_s`, `dwelltime`, `exposure_us`
- **Power**: `laser_power_488_mw`, `power_488_mw`, `power_405_mw`
- **Geometry**: `n_tiles`, `step_units`, `n_planes`, `step_um`
- **Hardware**: `pin488`, `pin405`, `camerapin`, `laser_pin`, `camera_pin`
- **Save location**: `measurements_root` (defaults to `~/ImSwitchMeasurements/`)

Refer to the workflow source code in `imswitch/imcontrol/model/workflows/` for complete parameter documentation.

## Troubleshooting

- **"api is not defined"**: Script must be run from the Scripting widget, not from a standalone Python interpreter.
- **"Hardware not found"**: Check that your setup JSON includes the hardware names specified in the script's `api.workflowFacade.build()` call.
- **Teensy connection errors**: Verify the Teensy device is connected and the port/baud settings match your `teensyPulse` configuration.

For detailed workflow implementation, see `imswitch/imcontrol/model/workflows/`.
