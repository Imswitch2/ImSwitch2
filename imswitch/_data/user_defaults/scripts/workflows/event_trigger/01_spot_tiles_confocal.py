"""Event trigger 01 -- Widefield tiles, spots per tile, a confocal scan where there are spots.

What it does
  1. Takes a tiled widefield overview with the Tiling panel's settings, or
     reuses one you already took (TILING_FOLDER).
  2. Finds the diffraction-limited spots in every tile -- the detection of
     ImProcess' 'spot-count' and 'tile-spots' processors, the SMLM net
     gradient -- and ranks the tiles by how many they hold.
  3. Moves the stage to every tile with at least MIN_SPOTS spots, most spots
     first, and runs the Scan panel's scan there once.
  4. Puts the stage back where it was.

  There is no camera -> scanner calibration in this: the stage brings the
  tile's spots (their mean position, or the tile centre: TARGET) to where
  the camera's image centre was, and the confocal field is assumed to be
  centred on the same spot. Good enough when the scan field is larger than
  the misalignment.

Before you run it
  * Tiling panel: 5 x 5 tiles, the camera, mode free-running, "Save tiles"
    on (the spots are counted on the saved tiles), a step a bit smaller than
    the camera's field of view.
  * Scan panel: the confocal scan you want at every target (field, pixel,
    dwell, lasers). It runs as set; this script does not change it.
  * Tune the detection on a test overview first: open its tiles.json in
    ImProcess with the tiling-mosaic reconstructor, run "Tile spots" on the
    mosaic, and copy the settings that find your spots into SPOT_SETTINGS.
    DRY_RUN = True counts and lists the targets without moving anything.

Output, in the tiling run's folder
  spot_targets.csv   one row per tile, most spots first, with its stage target
  spots.csv          every spot: tile, pixel, stage position, scores
  confocal/<rank>_tile<i>/   the scan's own HDF5 file(s) for each visited tile

Setup
  A camera and a two-axis XY stage in the setup's `tiling` section (Tiling
  widget), and a point scan (Scan widget) with its detector(s). No shipped
  mock setup has all of them.
"""
import csv
import os

from imswitch.improcess.analysis.spot_count import SpotParams, count_spots_in_tiling

# -- settings ---------------------------------------------------------------------------------

# None: take a new overview now with the Tiling panel's settings. Or the folder
# (or tiles.json) of an earlier run to analyse and revisit.
TILING_FOLDER = None

# The detection, as in the 'spot-count' / 'tile-spots' processors.
#   threshold_mode 'absolute': threshold is the net gradient (SMLM's units);
#   'relative': threshold is in multiples of each tile's noise (about 6).
SPOT_SETTINGS = {
    'threshold': 500.0,
    'threshold_mode': 'absolute',
    'sigma': 1.0,          # px, pre-smoothing
    'roi': 7,              # px, box (odd)
    'reject_edges': True,  # drop the rim of large bright structures
}
MIN_SPOTS = 1                  # visit tiles with at least this many spots
TARGET = 'spot centroid'       # or 'tile centre'
MAX_TARGETS = None             # visit at most this many tiles (None: all selected)

SCAN_DETECTORS = ['APD']       # what the confocal scan records
STAGE = None                   # None: the stage tiles.json names; else its name
STAGE_AXES = ('X', 'Y')        # its x and y axes, used when STAGE is set here
ARRIVAL_TOLERANCE_UM = 0.5
SETTLE_S = 0.2                 # after the stage arrived, before the scan
TILING_TIMEOUT_S = 1800
SCAN_TIMEOUT_S = 600
DRY_RUN = False                # True: count and list the targets, move nothing

log = getLogger()


def takeOverview() -> str:
    """Runs the Tiling panel once and returns the folder it saved."""
    if api.imcontrol.isTilingRunning():
        raise RuntimeError('A tiling run is already in progress')
    before = api.imcontrol.getLastTilingFolder()
    api.imcontrol.startTiling()
    if not api.imcontrol.isTilingRunning() and api.imcontrol.getLastTilingFolder() == before:
        raise RuntimeError('The Tiling panel did not start a run (busy, or the setup '
                           'has no tiling section)')
    print('Tiling ...')
    try:
        waitUntil(lambda: not api.imcontrol.isTilingRunning(), timeout=TILING_TIMEOUT_S)
    except BaseException:
        api.imcontrol.stopTiling()   # the script was stopped, or the run took too long
        raise
    folder = api.imcontrol.getLastTilingFolder()
    if not folder or folder == before:
        raise RuntimeError('The tiling run saved nothing: switch "Save tiles" on in '
                           'the Tiling panel')
    return folder


def writeTables(found, folder: str) -> None:
    tileColumns = ['rank', 'tile', 'grid_row', 'grid_col', 'spots', 'selected',
                   'tile_x_um', 'tile_y_um', 'target_x_um', 'target_y_um', 'name']
    with open(os.path.join(folder, 'spot_targets.csv'), 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(tileColumns)
        for row in zip(*(found.tiles[c] for c in tileColumns)):
            writer.writerow([v.item() if hasattr(v, 'item') else v for v in row])
    spotColumns = ['frame', 'tile_id', 'y', 'x', 'stage_x_um', 'stage_y_um',
                   'net_gradient', 'intensity', 'contrast']
    with open(os.path.join(folder, 'spots.csv'), 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['tile' if c == 'frame' else c for c in spotColumns])
        for row in zip(*(found.spots[c] for c in spotColumns)):
            writer.writerow([v.item() if hasattr(v, 'item') else v for v in row])


def moveTo(stage: str, axes, x: float, y: float) -> None:
    """Absolute move of both axes, then wait for arrival and settle."""
    for axis, value in zip(axes, (x, y)):
        api.imcontrol.setPositioner(stage, axis, float(value))
    waitUntil(lambda: all(
        abs(api.imcontrol.getPositionerPosition(stage, axis) - value) <= ARRIVAL_TOLERANCE_UM
        for axis, value in zip(axes, (x, y))
    ), timeout=30)
    sleep(SETTLE_S)


# 1. the overview
folder = TILING_FOLDER or takeOverview()

# 2. spots per tile
found = count_spots_in_tiling(
    folder, SpotParams.from_params(SPOT_SETTINGS),
    min_spots=MIN_SPOTS, target=TARGET, progress=log.info,
)
runFolder = os.path.dirname(found.manifest)
writeTables(found, runFolder)

counts = found.tiles['spots']
print(f'{len(counts)} tiles, {int(counts.sum())} spots; '
      f'{int(found.tiles["selected"].sum())} tile(s) with >= {MIN_SPOTS}')
for rank, tile, n, x, y in zip(found.tiles['rank'], found.tiles['tile'], counts,
                               found.tiles['target_x_um'], found.tiles['target_y_um']):
    if n >= MIN_SPOTS:
        print(f'  #{rank}: tile {tile} with {n} spot(s) -> stage ({x:.1f}, {y:.1f}) um')

selected = found.selected
targets = list(zip(selected['rank'], selected['tile'], selected['spots'],
                   selected['target_x_um'], selected['target_y_um']))
if MAX_TARGETS is not None:
    targets = targets[:MAX_TARGETS]

if not targets:
    print(f'No tile has {MIN_SPOTS} spot(s) or more: nothing to scan.')
elif DRY_RUN:
    print(f'DRY_RUN: would scan {len(targets)} tile(s). Tables in {runFolder}')
else:
    # 3. revisit and scan
    stage = STAGE or found.positioner
    axes = tuple(STAGE_AXES) if STAGE else (found.axes or tuple(STAGE_AXES))
    if not stage:
        raise RuntimeError('tiles.json does not name the stage (a run saved before it '
                           'did): set STAGE and STAGE_AXES')
    if stage not in api.imcontrol.getPositionerNames():
        raise RuntimeError(f'No positioner {stage!r} in this setup')

    confocalFolder = os.path.join(runFolder, 'confocal')
    with returningToPositions(stages=[stage]):
        for index, (rank, tile, n, x, y) in enumerate(targets, start=1):
            print(f'[{index}/{len(targets)}] tile {tile} ({n} spots): stage to '
                  f'({x:.1f}, {y:.1f}) um, scanning')
            moveTo(stage, axes, x, y)
            result = api.imcontrol.recordScanToMemory(
                list(SCAN_DETECTORS), timeout=SCAN_TIMEOUT_S, structured=True)
            written = result.save(os.path.join(confocalFolder, f'{rank:02d}_tile{tile:02d}'))
            print(f'    saved {", ".join(os.path.basename(p) for p in written)}')
            try:
                result.show(f'confocal #{rank} tile {tile}')
            except RuntimeError:
                pass  # no image view in this setup
    print(f'Done: {len(targets)} confocal scan(s) in {confocalFolder}')
