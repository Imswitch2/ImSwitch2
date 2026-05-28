"""Acquire a tiled scan with optional stitching and cell segmentation.

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - XY positioner available
  - Laser: 488 nm (EXC)
  - Teensy device available (if pulsed=True)

Output: <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/tiling_<HHMMSS>/Tiling_measurement.h5
        <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/tiling_<HHMMSS>/img_new_*.npy
        if save_individual=True.
"""
# ruff: noqa: F821
from imswitch.imcontrol.model.workflows import TilingWorkflow, TilingParams

MEASUREMENTS_ROOT = "D:/Measurements"  # Adapt to your preferred measurement folder.

facade = api.imcontrol.buildWorkflowFacade(
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
    measurements_root=MEASUREMENTS_ROOT,
)

wf = TilingWorkflow(facade, params)
wf.run()

print(f"Tiling complete: {params.n_tiles} tiles acquired and stitched.")
