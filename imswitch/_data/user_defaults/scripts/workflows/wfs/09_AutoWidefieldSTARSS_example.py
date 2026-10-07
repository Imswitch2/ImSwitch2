"""AutoWidefieldSTARSS example — automated tile, segment, record-per-cell.

Composition pipeline:
  1. Snap a frame to read the current camera field-of-view.
  2. Run a 10x10 spiral tiling with 20% overlap of that FOV.
  3. Stitch tiles into a live overview, segment cells.
  4. At each accepted cell, move the stage and run a WidefieldSTARSS
     acquisition (H + V polarisation stack).

Prerequisites:
  - Setup loaded: example_kiralux_teensy.json (or equivalent)
  - XY positioner, Teensy, 488 nm laser, HWP/QWP rotators

Output:
  - <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/tiling_<HHMMSS>/img_new_*.npy
    + Tiling_measurement.h5
  - <MEASUREMENTS_ROOT>/<YYYY_MM_DD>/data_stack_cell_<i>_h.tif
    and data_stack_cell_<i>_v.tif
"""
# ruff: noqa: F821
from imswitch.imcontrol.model.workflows import (
    WidefieldStarssParams,
    WidefieldStarssWorkflow,
    StitchedImage,
    TilingParams,
    TilingWorkflow,
)

MEASUREMENTS_ROOT = "D:/Measurements"
N_TILES_PER_SIDE = 10        # 10x10 spiral
OVERLAP_FRACTION = 0.20      # 20% of FOV
SEG_FILTER = {
    "area_enabled": True,
    "area_um2_min": 50.0,
    "area_um2_max": 5000.0,
    "mean_intensity_enabled": True,
    "mean_intensity": 0.15,
    "eccentricity_enabled": True,
    "eccentricity": 0.95,
}

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={"488": "488 (EXC) sn27311", "405": "405 (ACT) sn26647"},
    detector_name="Kiralux",
    xy_positioner_name="XY",
    z_positioner_name="Z",
    hwp_name="HWP",
    qwp_name="QWP",
)

# ---------------------------------------------------------------------------
# 1. Read current FOV from the camera
# ---------------------------------------------------------------------------
detector = api.imcontrol.detectorsManager["Kiralux"]
sample_frame = detector.getLatestFrameShared()
if sample_frame is None or sample_frame.size == 0:
    raise RuntimeError("No frame available from camera — start a live view first.")

frame_shape = sample_frame.shape[:2]                 # (H, W)
pixel_size_um = float(detector.pixelSizeUm[-1])      # x pixel size
fov_x_um = frame_shape[1] * pixel_size_um
fov_y_um = frame_shape[0] * pixel_size_um
step_um = min(fov_x_um, fov_y_um) * (1.0 - OVERLAP_FRACTION)

print(f"FOV: {fov_x_um:.1f} x {fov_y_um:.1f} µm  |  step_um (20% overlap) = {step_um:.1f}")

# ---------------------------------------------------------------------------
# 2. Per-cell sub-workflow (WidefieldSTARSS acquisition)
# ---------------------------------------------------------------------------
widefield_starss_params = WidefieldStarssParams(
    pin488=8, pin405=6, camerapin=11,
    start488=0, start405=25_000, start_camera=0,
    width488=20_000, width405=20_000, width_camera=50_000,
    dwelltime=50_000, delay_time=0, frame_number=20,
    move_waveplate=True, record_h=True, record_v=True,
    measurements_root=MEASUREMENTS_ROOT,
)
widefield_starss_wf = WidefieldStarssWorkflow(facade, widefield_starss_params)

# ---------------------------------------------------------------------------
# 3. Tiling — build a live stitcher via the tile_callback hook
# ---------------------------------------------------------------------------
stitcher = StitchedImage(
    tile_size_px=None,
    tile_shape_px=frame_shape,
    tile_step_um=step_um,
    pixel_size_um=pixel_size_um,
    blend_overlaps=True,
)

grid_positions = []

def feed_stitcher(image, grid_x, grid_y, tile_idx, n_total):
    stitcher.add_tile(image, grid_x, grid_y)
    grid_positions.append((grid_x, grid_y))
    print(f"  tile {tile_idx + 1}/{n_total} at grid ({grid_x}, {grid_y})")

tiling_params = TilingParams(
    n_tiles=N_TILES_PER_SIDE * N_TILES_PER_SIDE,
    step_units=step_um,            # µm; the positioner takes µm
    laser_pin=8, camera_pin=11,
    pulsed=True,
    laser_power_488_mw=50.0,
    exposure_us=50_000,
    save_individual=True,
    measurements_root=MEASUREMENTS_ROOT,
)
tiling_wf = TilingWorkflow(facade, widefield_starss_wf, tiling_params, SEG_FILTER)

initial_pos = facade.stage_con.get_position()
tiling_wf.run(tile_callback=feed_stitcher)

# ---------------------------------------------------------------------------
# 4. Cell targeting — segment overview, run WidefieldSTARSS at each cell
# ---------------------------------------------------------------------------
# Canvas origin = stage position of pixel (0, 0) of the stitched overview.
# Tile (gx, gy) is placed at pixel (gy * step_y_px, gx * step_x_px) in the
# canvas, so canvas (0,0) corresponds to the centre of tile (min_gx, min_gy)
# shifted by half a tile to the top-left.
min_gx = min(gx for gx, _ in grid_positions)
min_gy = min(gy for _, gy in grid_positions)
canvas_origin = (
    initial_pos[0] + min_gx * step_um - (frame_shape[1] / 2) * pixel_size_um,
    initial_pos[1] + min_gy * step_um - (frame_shape[0] / 2) * pixel_size_um,
)

def acquire_at_cell(idx, cell_props, stage_xy):
    print(f"Cell {idx}: stage=({stage_xy[0]:.1f}, {stage_xy[1]:.1f}) µm  "
          f"area={float(cell_props['area_um2']):.1f} µm²")
    widefield_starss_wf.run(measurement_name_addition=f"_cell_{idx}")

tiling_wf.run_cell_targeting(
    stitched=stitcher,
    pixel_size_um=pixel_size_um,
    canvas_origin_stage=canvas_origin,
    for_each_feature=acquire_at_cell,
)

print("AutoWidefieldSTARSS complete.")
