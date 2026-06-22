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
    Generate X and Y pixel coordinates for concentric rectangles.

    Args:
        num_rects: Number of rectangles.

    Returns:
        X and Y pixel coordinates for the rectangles.
    """
    num_rects += 1
    start = 1 - num_rects
    stop = num_rects
    steps = 1

    X, Y = np.meshgrid(
        np.arange(start, stop, steps, dtype=float),
        np.arange(start, stop, steps, dtype=float),
    )

    return X.flatten(), Y.flatten()


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
        num_rects: Number of rectangles used to model the foci.

    Returns:
        Flattened X and Y interpolation coordinates clipped to frame boundaries.
    """
    Xc, Yc = get_center_coords(xp, xo, yp, yo, nx_c, ny_c)
    Xr, Yr = get_rectangles_coords(num_rects)

    Xi = Xc.reshape((-1, 1)) + Xr
    Yi = Yc.reshape((-1, 1)) + Yr

    Xi[Xi < 0] = 0
    Xi[Xi > num_cols - 1] = 0
    Yi[Yi < 0] = 0
    Yi[Yi > num_rows - 1] = 0

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
