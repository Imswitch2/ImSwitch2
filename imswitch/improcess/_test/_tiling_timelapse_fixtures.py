"""Write small synthetic timelapses of tilings, laid out as ImSwitch writes them.

The layout is the Timelapse panel's (``TimelapseController._writeManifest``,
``timepoint_folder``) around the Tiling controller's own dataset format
(``imswitch-tiling/2``, ``tile_dataset.TileDataset._write_manifest``):

    <root>/timelapse.json
    <root>/t0/ tiles.json  TileConfiguration.txt  tile_x+00_y+00_CAM.ome.tiff ...
    <root>/t1/ ...

Ground truth is known by construction: every tile truly sits at its nominal
stage position plus a fixed per-tile offset (``jitter``), and the sample may
move between timepoints (``drift``). The pixels of tile ``i`` at timepoint
``t`` are the scene sampled there, with fresh noise.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

DETECTOR = "CAM"


def smooth_scene(shape=(400, 400), seed=0, sigma=2.5):
    """A reproducible texture with structure at the scale of a few pixels."""
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    scene = gaussian_filter(rng.random(shape).astype(np.float32), sigma)
    scene -= scene.min()
    scene /= scene.max()
    return scene


def grid_cells(nx, ny):
    """Row-major ``(grid_x, grid_y)`` cells, as the manifest numbers them."""
    return [(cx, cy) for cy in range(ny) for cx in range(nx)]


def true_positions(nx, ny, step_px, jitter=None, margin=40):
    """``{tile_id: (row, col)}`` where each tile really sits in the scene."""
    jitter = jitter or {}
    out = {}
    for tile_id, (cx, cy) in enumerate(grid_cells(nx, ny)):
        dy, dx = jitter.get(tile_id, (0, 0))
        out[tile_id] = (margin + cy * step_px[0] + dy, margin + cx * step_px[1] + dx)
    return out


def random_jitter(n_tiles, amplitude=4, seed=1, fixed_first=True):
    """Integer per-tile offsets in ``[-amplitude, amplitude]``; tile 0 stays put."""
    rng = np.random.default_rng(seed)
    out = {tile: tuple(int(v) for v in rng.integers(-amplitude, amplitude + 1, size=2))
           for tile in range(n_tiles)}
    if fixed_first:
        out[0] = (0, 0)
    return out


def write_tiling(folder, scene, *, nx=3, ny=3, tile=(64, 64), step_px=(48, 48), jitter=None,
                 shift=(0.0, 0.0), noise=0.0, seed=0, pixel_um=0.5, completed=True,
                 n_written=None, amplitude=4000.0, margin=40, extra_manifest=None):
    """One ``imswitch-tiling/2`` dataset. Returns the ``tiles.json`` path.

    ``shift`` is how far the sample has moved in the image since the scene was
    defined (rows, cols; a whole number of pixels is exact, others are
    interpolated). ``noise`` is Gaussian noise as a multiple of the scene's
    standard deviation. ``n_written`` writes only that many tiles (a run that
    stopped early).
    """
    import tifffile
    from scipy.ndimage import shift as ndshift

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    source = scene
    if any(float(v) != 0.0 for v in shift):
        source = ndshift(scene, shift, order=3, mode="nearest")
    truth = true_positions(nx, ny, step_px, jitter, margin)
    std = float(scene.std())
    entries = []
    cells = grid_cells(nx, ny)
    limit = len(cells) if n_written is None else int(n_written)
    for tile_id, (cx, cy) in enumerate(cells[:limit]):
        row, col = truth[tile_id]
        patch = source[row:row + tile[0], col:col + tile[1]].astype(np.float32)
        if noise:
            patch = patch + rng.normal(0.0, noise * std, size=patch.shape).astype(np.float32)
        data = np.clip(patch * amplitude + 1000.0, 0, 65535).astype(np.uint16)
        name = f"tile_x{cx:+03d}_y{cy:+03d}_{DETECTOR}.ome.tiff"
        tifffile.imwrite(str(folder / name), data)
        # Nominal pixel position (what the stage commanded), not the truth.
        nominal = (cy * step_px[0], cx * step_px[1])
        entries.append({
            "filename": name,
            "grid": [cx, cy],
            "stage_um": [cx * step_px[1] * pixel_um, cy * step_px[0] * pixel_um],
            "pixel_xy": [float(nominal[1]), float(nominal[0])],
            "correction_px": [0.0, 0.0],
            "alignment": {
                "detector": DETECTOR, "filename": name, "axes": "YX", "stored_axes": "YX",
                "shape": list(tile), "stored_shape": list(tile),
            },
            "payloads": {DETECTOR: {
                "path": name, "group": None, "detector": DETECTOR, "axes": "YX",
                "stored_axes": "YX", "shape": list(tile), "stored_shape": list(tile),
                "generation": 1, "complete": True, "transform_to_alignment": "identity",
            }},
        })
    payload = {
        "format": "imswitch-tiling/2",
        "pixel_size_um": {"y": pixel_um, "x": pixel_um},
        "tile_step_um": step_px[1] * pixel_um,
        "tile_shape_px": {"height": tile[0], "width": tile[1], "depth": 1},
        "z_step_um": 0.0,
        "orientation": {"flip_x": False, "flip_y": False, "swap_axes": False},
        "tiles": entries,
        "completed": bool(completed),
        "xy_positioner": "STAGE",
        "xy_axes": ["X", "Y"],
    }
    payload.update(extra_manifest or {})
    (folder / "tiles.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (folder / "TileConfiguration.txt").write_text(
        "dim = 2\n" + "".join(f"{e['filename']}; ; ({e['pixel_xy'][0]}, {e['pixel_xy'][1]})\n" for e in entries),
        encoding="utf-8",
    )
    return folder / "tiles.json"


def timepoint_name(index, total):
    """``TimelapseController``'s ``timepoint_folder`` rule."""
    return f"t{index:0{len(str(total))}d}"


def write_timelapse(root, scene, timepoints, *, interval_s=60.0, drifts=None, noise=0.0,
                    seed=0, incomplete=(), missing=(), unrecorded=(), grid_overrides=None,
                    extra_settings=None, **tiling):
    """A timelapse of tilings. Returns the ``timelapse.json`` path.

    ``drifts[t]`` is the sample's shift (rows, cols) at timepoint ``t``.
    ``incomplete``: timepoints whose tiling stopped early (recorded, not
    completed, half the tiles). ``missing``: timepoints never run. ``unrecorded``:
    a folder that exists but the manifest never heard of (the run was killed).
    ``grid_overrides[t]``: keyword arguments for that timepoint's ``write_tiling``
    (e.g. a different grid).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    drifts = drifts or {}
    points = []
    last_recorded = max([t for t in range(timepoints) if t not in missing and t not in unrecorded],
                        default=-1)
    for t in range(timepoints):
        if t in missing:
            continue
        folder = root / timepoint_name(t, timepoints)
        options = dict(tiling)
        options.update((grid_overrides or {}).get(t, {}))
        total_tiles = options.get("nx", 3) * options.get("ny", 3)
        stopped = t in incomplete
        write_tiling(
            folder, scene, shift=drifts.get(t, (0.0, 0.0)), noise=noise, seed=seed * 1000 + t,
            completed=not stopped and t not in unrecorded,
            n_written=(total_tiles // 2 if stopped else None), **options,
        )
        if t in unrecorded or t > last_recorded:
            continue
        points.append({
            "index": t, "folder": folder.name, "scheduled_offset_s": t * interval_s,
            "slip_s": 0.0, "completed": not stopped, "stopped": stopped,
            "message": "stopped" if stopped else "",
            "tiles_acquired": (total_tiles // 2 if stopped else total_tiles),
            "stale_tiles": 0, "illumination_gated": False,
        })
    settings = {
        "n_tiles_x": tiling.get("nx", 3), "n_tiles_y": tiling.get("ny", 3), "pattern": "spiral",
        "step_um": tiling.get("step_px", (48, 48))[1] * tiling.get("pixel_um", 0.5),
        "mode": "free_running", "detector_name": DETECTOR,
    }
    settings.update(extra_settings or {})
    manifest = root / "timelapse.json"
    manifest.write_text(json.dumps({
        "kind": "tiling", "timepoints": timepoints, "interval_s": interval_s,
        "interval_mode": "start_to_start", "lasers": {}, "illumination_settle_ms": None,
        "started_utc": "2026-09-23T12:00:00+00:00", "tiling_settings": settings, "points": points,
    }, indent=2), encoding="utf-8")
    return manifest


def layout_error(positions, truth, nx, ny):
    """RMS error (pixels) of solved ``positions`` against ``truth``, up to a common shift.

    Both are ``{tile_id: (row, col)}``; ``truth`` is where tiles sit in the
    scene and ``positions`` where the solver put them in the mosaic, so the
    nominal stage offset is part of the truth: compare against ``truth`` minus
    the nominal pixel step (``cy * step``).
    """
    ids = sorted(positions)
    diff = np.array([[positions[i][0] - truth[i][0], positions[i][1] - truth[i][1]] for i in ids])
    diff -= diff[0]
    return float(np.sqrt(np.mean(np.sum(diff ** 2, axis=1))))
