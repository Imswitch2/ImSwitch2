"""Acquire a tiled scan with optional stitching and cell segmentation.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - XY positioner available
  - Laser: 488 nm (EXC)
  - Teensy device available (if pulsed=True)

Output: ~/ImSwitchMeasurements/<timestamp>/stitched_image.png
        ~/ImSwitchMeasurements/<timestamp>/tile_*.npy (if save_individual=True)
"""
from imswitch.imcontrol.model.workflows import TilingWorkflow, TilingParams

facade = api.workflowFacade.build(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

params = TilingParams(
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
    measurements_root=None,  # defaults to ~/ImSwitchMeasurements/
)

wf = TilingWorkflow(facade, params)
wf.run()

print(f"Tiling complete: {params.n_tiles} tiles acquired and stitched.")
