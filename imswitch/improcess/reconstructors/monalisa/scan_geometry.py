"""Scan geometry helpers for MoNaLISA live reconstruction."""

import numpy as np


def get_center_coords(
    xp: float,
    xo: float,
    yp: float,
    yo: float,
    nx_c: int,
    ny_c: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate a grid pattern based on localization parameters.

    Args:
        xp: Period along x-axis in pixels.
        xo: Offset along x-axis in pixels.
        yp: Period along y-axis in pixels.
        yo: Offset along y-axis in pixels.
        nx_c: Number of foci along x-axis.
        ny_c: Number of foci along y-axis.

    Returns:
        Flattened grid coordinates for X and Y.
    """
    x_stop = xo + (nx_c - 1) * xp
    y_stop = yo + (ny_c - 1) * yp

    X, Y = np.meshgrid(np.linspace(xo, x_stop, nx_c), np.linspace(yo, y_stop, ny_c))

    return X.flatten(), Y.flatten()


def get_rectangles_coords(num_rects: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate X and Y pixel offsets for concentric rectangular shells.

    Args:
        num_rects: Number of rectangular shells.

    Returns:
        X and Y pixel offsets for the shells.
    """
    num_rects = int(num_rects)
    if num_rects < 1:
        raise ValueError("num_rects must be >= 1")

    all_x = []
    all_y = []
    for rect_idx in range(num_rects):
        if rect_idx == 0:
            # Innermost "shell" is the single central pixel. Emit it once;
            # the generic perimeter construction below would duplicate it
            # (top and bottom rows both collapse onto the center), which would
            # double-weight the center in the Gaussian fit.
            all_x.append(np.array([0.0]))
            all_y.append(np.array([0.0]))
            continue

        width = rect_idx * 2
        height = rect_idx * 2

        x_left, x_right = -width / 2, width / 2
        y_bottom, y_top = -height / 2, height / 2

        top_x = np.arange(x_left, x_right + 1, 1, dtype=float)
        top_y = np.ones(len(top_x), dtype=float) * y_top

        bottom_x = np.arange(x_left, x_right + 1, 1, dtype=float)
        bottom_y = np.ones(len(bottom_x), dtype=float) * y_bottom

        side_y = np.arange(y_bottom + 1, y_top, 1, dtype=float)
        right_x = np.ones(len(side_y), dtype=float) * x_right
        left_x = np.ones(len(side_y), dtype=float) * x_left

        all_x.extend([top_x, bottom_x, left_x, right_x])
        all_y.extend([top_y, bottom_y, side_y, side_y])

    return np.concatenate(all_x), np.concatenate(all_y)


def get_pinhole_footprint(radius_px: float) -> tuple[np.ndarray, np.ndarray]:
    """
    Integer pixel offsets within a circular detection pinhole.

    Returns a filled disc of the given radius (each offset once, center
    included exactly once), suitable as a physically-sized detection footprint
    in the spirit of image-scanning-microscopy pinholing. A smaller radius
    trades signal for resolution; a larger one trades resolution for SNR.

    Args:
        radius_px: Pinhole radius in footprint pixels (must be > 0).

    Returns:
        Flattened X and Y pixel offsets inside the pinhole disc.
    """
    radius_px = float(radius_px)
    if radius_px <= 0:
        raise ValueError("pinhole radius must be > 0")

    half = int(np.ceil(radius_px))
    coords = np.arange(-half, half + 1, dtype=float)
    X, Y = np.meshgrid(coords, coords)
    mask = (X ** 2 + Y ** 2) <= radius_px ** 2
    return X[mask], Y[mask]


def get_interp_coords(
    xp: float,
    xo: float,
    yp: float,
    yo: float,
    nx_c: int,
    ny_c: int,
    num_rows: int,
    num_cols: int,
    num_rects: int = 3,
    footprint: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate local interpolation coordinates around each grid focus center.

    Args:
        xp: X-axis period.
        xo: X-axis offset.
        yp: Y-axis period.
        yo: Y-axis offset.
        nx_c: Number of foci along x-axis.
        ny_c: Number of foci along y-axis.
        num_rows: Total number of rows in the frame.
        num_cols: Total number of columns in the frame.
        num_rects: Number of rectangular shells (used only when ``footprint``
            is None — the legacy shell footprint).
        footprint: Optional ``(x_offsets, y_offsets)`` to use directly (e.g. a
            circular pinhole from :func:`get_pinhole_footprint`). When given it
            overrides ``num_rects`` so the interpolation and the fit weights can
            share the exact same footprint.

    Returns:
        Flattened X and Y interpolation coordinates clipped to frame boundaries.
    """
    Xc, Yc = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
    if footprint is not None:
        Xr, Yr = footprint
    else:
        Xr, Yr = get_rectangles_coords(num_rects)

    Xi = Xc.reshape((-1, 1)) + Xr
    Yi = Yc.reshape((-1, 1)) + Yr

    Xi[Xi < 0] = 0
    Xi[Xi > num_cols - 1] = num_cols - 1
    Yi[Yi < 0] = 0
    Yi[Yi > num_rows - 1] = num_rows - 1

    return Xi.flatten(), Yi.flatten()


def _get_bases(
    nx_c: int,
    ny_c: int,
    nx_s: int,
    ny_s: int,
    scan_ori: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Create base indices for rows and columns based on scan dimensions.

    Args:
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        scan_ori: Scan orientation string (e.g. "+x+y").

    Returns:
        Base indices for x and y.
    """
    x_start = 0 if scan_ori.find("+x") != -1 else nx_s - 1
    x_stop = x_start + nx_c * nx_s
    x_steps = nx_s

    y_start = 0 if scan_ori.find("+y") != -1 else ny_s - 1
    y_stop = y_start + ny_c * ny_s
    y_steps = ny_s

    xb = np.arange(x_start, x_stop, x_steps, dtype=int)
    yb = np.arange(y_start, y_stop, y_steps, dtype=int)

    return xb, yb


def _get_shifts(
    nx_s: int,
    ny_s: int,
    scan_ori: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Create shift indices based on the scanning orientation sequence.

    Args:
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        scan_ori: Scan orientation string.

    Returns:
        Flattened x and y shift indices.
    """
    x_start = 0
    x_stop = nx_s
    x_steps = 1

    y_start = 0
    y_stop = ny_s
    y_steps = 1

    x_range = np.arange(x_start, x_stop, x_steps, dtype=int)
    y_range = np.arange(y_start, y_stop, y_steps, dtype=int)

    if scan_ori[1] == "y":
        ys, xs = np.meshgrid(y_range, x_range)
    else:
        xs, ys = np.meshgrid(x_range, y_range)

    xs = -xs if scan_ori.find("-x") != -1 else xs
    ys = -ys if scan_ori.find("-y") != -1 else ys

    return xs.flatten(), ys.flatten()


def get_1d_indices(
    nx_c: int,
    ny_c: int,
    nx_s: int,
    ny_s: int,
    scan_ori: str,
) -> np.ndarray:
    """
    Calculate the final 1D index mapping for vectorized super-array updates.

    Args:
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        scan_ori: Scan orientation string.

    Returns:
        A 2D mapping of (scan_steps, foci_total) to flat 1D indices.
    """
    xb, yb = _get_bases(nx_c, ny_c, nx_s, ny_s, scan_ori)
    xb = xb.reshape(1, 1, nx_c)
    yb = yb.reshape(1, ny_c, 1)

    xs, ys = _get_shifts(nx_s, ny_s, scan_ori)
    xs = xs.reshape(nx_s * ny_s, 1, 1)
    ys = ys.reshape(nx_s * ny_s, 1, 1)

    frame_inds = (yb + ys) * (nx_s * nx_c) + (xb + xs)

    return frame_inds.reshape((nx_s * ny_s, nx_c * ny_c))


def get_orientation(
    nx_c: int,
    ny_c: int,
    nx_s: int,
    ny_s: int,
    proc_pixels: np.ndarray,
) -> str:
    """
    Determine the scanning orientation that minimizes total variation.

    Args:
        nx_c: Number of foci along x.
        ny_c: Number of foci along y.
        nx_s: Scanner steps along x.
        ny_s: Scanner steps along y.
        proc_pixels: Processed pixels from the first chunk.

    Returns:
        The determined scan orientation (e.g. "+x+y", "-y+x").
    """
    rec_x = nx_c * nx_s
    rec_y = ny_c * ny_s
    rec_img = np.zeros((rec_y * rec_x), dtype=np.float32)

    orients = ("+x+y", "+x-y", "-x+y", "-x-y", "+y+x", "+y-x", "-y+x", "-y-x")
    score_arr = np.zeros((len(orients)), dtype=float)

    for i in range(len(orients)):
        frame_inds = get_1d_indices(nx_c, ny_c, nx_s, ny_s, scan_ori=orients[i])
        rec_img[frame_inds.flatten()] = proc_pixels.flatten()

        # Total variation
        dx = np.abs(np.diff(rec_img.reshape((rec_y, rec_x)), axis=1), dtype=float)
        dy = np.abs(np.diff(rec_img.reshape((rec_y, rec_x)), axis=0), dtype=float)
        res = np.sum(dx) + np.sum(dy)

        score_arr[i] = res

    return orients[np.argmin(score_arr)]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


def scan_params_from_layout(resolved, axis_labels: dict) -> dict | None:
    """MoNaLISA scan-dialog values from a resolved acquisition layout.

    The dialog used to be pre-filled by re-parsing ``ScanStage:*`` attributes,
    and its position counts came from ``sqrt(numFrames)`` -- the same square
    guess that turned a 648-frame 18x18x2 line-step scan into 25x25 in
    BeadRec. The resolver already knows the real counts, pitches and
    directions, so they come from there.

    ``axis_labels`` maps semantic names to the widget's dimension strings
    (``r_l_text``/``u_d_text``/``b_f_text``/``timepoints_text``/``p_text``/
    ``n_text``). Returns ``None`` when the layout describes no scan axis.
    """
    layout = getattr(resolved, "layout", None)
    if layout is None or not getattr(resolved, "is_usable", False):
        return None

    kind_to_label = {
        "scan_x": axis_labels["r_l_text"],
        "scan_y": axis_labels["u_d_text"],
        "scan_z": axis_labels["b_f_text"],
        "time": axis_labels["timepoints_text"],
    }
    # event_loops run outermost to innermost; the dialog lists the fast axis
    # first, which is the same order reversed.
    loops = [
        loop for loop in reversed(layout.event_loops) if loop.kind in kind_to_label
    ]
    if not any(loop.kind.startswith("scan_") for loop in loops):
        return None

    dimensions: list[str] = []
    directions: list[str] = []
    steps: list[str] = []
    step_sizes: list[str] = []
    for loop in loops:
        dimensions.append(kind_to_label[loop.kind])
        directions.append(
            axis_labels["n_text"] if loop.direction == -1 else axis_labels["p_text"]
        )
        steps.append(str(int(loop.count)))
        # The dialog holds nanometres; layout pitches are micrometres.
        step_sizes.append(str(float(loop.step) * 1000.0) if loop.step else "1")

    # Pad to the dialog's fixed four slots, timepoints last.
    for label in (
        axis_labels["r_l_text"],
        axis_labels["u_d_text"],
        axis_labels["b_f_text"],
        axis_labels["timepoints_text"],
    ):
        if label not in dimensions:
            dimensions.append(label)
            directions.append(axis_labels["p_text"])
            steps.append("1")
            step_sizes.append("1")
    return {
        "dimensions": dimensions[:4],
        "directions": directions[:3],
        "steps": steps[:4],
        "step_sizes": step_sizes[:4],
    }
