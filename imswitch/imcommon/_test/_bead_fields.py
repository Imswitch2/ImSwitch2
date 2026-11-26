"""Synthetic bead fields for the bead-registration tests: one sample of beads
in scan micrometres, rendered into a "camera" image through a known
camera-pixel -> µm affine and into an "APD scan" image through a scan
geometry (fast axis first, rows are the slow axis)."""
from __future__ import annotations

import math

import numpy as np


def similarity_matrix(scale_um_per_px, rotation_deg, mirror, translation_um):
    """2x3: camera (row, col) -> (target 0, target 1) µm."""
    t = math.radians(rotation_deg)
    rot = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    linear = scale_um_per_px * rot
    if mirror:
        linear = linear @ np.diag([1.0, -1.0])
    return np.column_stack([linear, np.asarray(translation_um, float)])


def random_beads(rng, centre_um, half_um, n, min_spacing_um):
    """``n`` beads (target 0, target 1) µm in a square, no two closer than
    ``min_spacing_um`` (Poisson-disc by rejection)."""
    pts = []
    tries = 0
    while len(pts) < n and tries < 200000:
        tries += 1
        p = np.asarray(centre_um, float) + rng.uniform(-half_um, half_um, 2)
        if all(np.hypot(*(p - q)) >= min_spacing_um for q in pts):
            pts.append(p)
    return np.asarray(pts)


def render(shape, positions_px, sigma_px, amplitudes, background, rng, noise=True, dtype=np.uint16,
           read_noise=0.0):
    """Gaussian spots on a background, Poisson noise, clipped to ``dtype``."""
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float64)
    image = np.full(shape, float(background))
    for (r, c), a in zip(positions_px, amplitudes):
        if -5 * sigma_px < r < shape[0] + 5 * sigma_px and -5 * sigma_px < c < shape[1] + 5 * sigma_px:
            r0, r1 = max(0, int(r - 5 * sigma_px)), min(shape[0], int(r + 5 * sigma_px) + 1)
            c0, c1 = max(0, int(c - 5 * sigma_px)), min(shape[1], int(c + 5 * sigma_px) + 1)
            image[r0:r1, c0:c1] += a * np.exp(-((rows[r0:r1, c0:c1] - r) ** 2 + (cols[r0:r1, c0:c1] - c) ** 2)
                                              / (2 * sigma_px ** 2))
    if noise:
        image = rng.poisson(image).astype(np.float64)
        if read_noise:
            image += rng.normal(0, read_noise, shape)
    if np.issubdtype(np.dtype(dtype), np.integer):
        image = np.clip(np.round(image), 0, np.iinfo(dtype).max)
    return image.astype(dtype)


def geometry(fast_device, slow_device, fast_step, slow_step, fast_count, slow_count, fast_centre, slow_centre):
    """A scan geometry dict as ``frame_geometry_from_scan`` makes it (whole
    numbers of steps: first pixel centred at centre - (N - 1) / 2 * step)."""
    return {"axes": [
        {"device": fast_device, "step_um": fast_step, "count": fast_count,
         "first_um": fast_centre - (fast_count - 1) / 2 * fast_step},
        {"device": slow_device, "step_um": slow_step, "count": slow_count,
         "first_um": slow_centre - (slow_count - 1) / 2 * slow_step},
    ]}


def apd_pixels(beads_um, geom, target_axes, fast_shift_px=0.0):
    """Bead µm (columns in ``target_axes`` order) -> APD (row, col) px."""
    fast, slow = geom["axes"]
    by = {target_axes[0]: beads_um[:, 0], target_axes[1]: beads_um[:, 1]}
    rows = (by[slow["device"]] - slow["first_um"]) / slow["step_um"]
    cols = (by[fast["device"]] - fast["first_um"]) / fast["step_um"] + fast_shift_px
    return np.column_stack([rows, cols])


def camera_pixels(beads_um, matrix):
    linear, t = matrix[:, :2], matrix[:, 2]
    return (beads_um - t) @ np.linalg.inv(linear).T


def scene(seed=0, *, rotation_deg=17.0, mirror=False, cam_scale=0.108, cam_shape=(400, 480),
          cam_sigma_px=1.6, apd_sigma_px=1.3, fast="X", slow="Y", fast_step=0.1, slow_step=0.1,
          apd_counts=(180, 150), apd_centre=(2.0, -3.0), n_beads=500, spacing_um=1.5, fast_shift_px=0.0,
          cam_background=300, cam_amp=1500, apd_background=3, apd_amp=60, missing_fraction=0.0,
          spurious=0, beads_um=None, target_axes=("Y", "X")):
    """(camera image, APD image, geometry, true matrix, bead µm). The bead
    field covers the camera field; the APD scans ``apd_counts`` (fast,
    slow) pixels around ``apd_centre`` (fast, slow) µm."""
    rng = np.random.default_rng(seed)
    centre_px = np.array([(cam_shape[0] - 1) / 2, (cam_shape[1] - 1) / 2])
    linear = similarity_matrix(cam_scale, rotation_deg, mirror, (0, 0))[:, :2]
    # Put the camera centre over the APD field centre (in target_axes order).
    apd_c = {fast: apd_centre[0], slow: apd_centre[1]}
    centre_um = np.array([apd_c[target_axes[0]], apd_c[target_axes[1]]]) + np.array([0.7, -1.1])
    matrix = np.column_stack([linear, centre_um - linear @ centre_px])
    half = 0.5 * cam_scale * max(cam_shape) * 1.5
    if beads_um is None:
        beads_um = random_beads(rng, centre_um, half, n_beads, spacing_um)
    geom = geometry(fast, slow, fast_step, slow_step, apd_counts[0], apd_counts[1], apd_centre[0], apd_centre[1])
    cam_px = camera_pixels(beads_um, matrix)
    apd_px = apd_pixels(beads_um, geom, target_axes, fast_shift_px)
    keep_cam = rng.random(len(beads_um)) >= missing_fraction
    keep_apd = rng.random(len(beads_um)) >= missing_fraction
    cam_amps = cam_amp * rng.uniform(0.7, 1.3, len(beads_um))
    apd_amps = apd_amp * rng.uniform(0.7, 1.3, len(beads_um))
    cam_pos, cam_a = list(cam_px[keep_cam]), list(cam_amps[keep_cam])
    apd_pos, apd_a = list(apd_px[keep_apd]), list(apd_amps[keep_apd])
    for _ in range(spurious):
        cam_pos.append(rng.uniform((0, 0), cam_shape))
        cam_a.append(cam_amp)
        apd_pos.append(rng.uniform((0, 0), (apd_counts[1], apd_counts[0])))
        apd_a.append(apd_amp)
    cam = render(cam_shape, cam_pos, cam_sigma_px, cam_a, cam_background, rng, read_noise=2.0)
    apd = render((apd_counts[1], apd_counts[0]), apd_pos, apd_sigma_px, apd_a, apd_background, rng)
    return cam, apd, geom, matrix, beads_um
