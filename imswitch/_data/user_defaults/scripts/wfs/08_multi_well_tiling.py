"""Acquire tiled scans across multiple wells with autofocus per well.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - XY positioner with sufficient range for multi-well plate
  - Z positioner for autofocus
  - Laser: 488 nm (EXC)
  - Teensy device available (if pulsed=True)

Output: ~/ImSwitchMeasurements/<timestamp>/well_r0_c0/stitched_image.png (per well)
        ~/ImSwitchMeasurements/<timestamp>/well_r0_c0/tile_*.npy (if save_individual=True)
"""
from imswitch.imcontrol.model.workflows import (
    MultiWellTilingWorkflow,
    MultiWellTilingParams,
    TilingWorkflow,
    TilingParams,
    ZStackWorkflow,
    ZStackParams,
)

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

tiling_params = TilingParams(
    n_tiles=9,
    step_units=1560.0,
    laser_pin=8,
    camera_pin=11,
    pulsed=True,
    laser_power_488_mw=50.0,
    exposure_us=50_000,
    tile_display_size=256,
    save_individual=True,
    skip_cell_targeting=True,
)
tiling_wf = TilingWorkflow(facade, tiling_params)

zstack_params = ZStackParams(
    n_planes=5,
    step_um=2.0,
    laser_pin=8,
    camera_pin=11,
    pulsed=True,
    laser_power_488_mw=50.0,
    exposure_us=50_000,
)
zstack_wf = ZStackWorkflow(facade, zstack_params)

multi_well_params = MultiWellTilingParams(
    n_rows=2,
    n_cols=3,
    well_pitch_x_units=9000.0,
    well_pitch_y_units=9000.0,
    autofocus_n_planes=5,
    autofocus_step_um=2.0,
    autofocus_z_center_um=None,
    tiling_n_tiles=9,
    measurements_root=None,  # defaults to ~/ImSwitchMeasurements/
)

multi_well_wf = MultiWellTilingWorkflow(facade, tiling_wf, zstack_wf, multi_well_params)
multi_well_wf.run()

print(f"Multi-well tiling complete: {multi_well_params.n_rows}×{multi_well_params.n_cols} wells.")
