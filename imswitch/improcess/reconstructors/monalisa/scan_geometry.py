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

    from imswitch.imcommon.model.acquisition_layout import (
        UnconsumedLoopError,
        select_loops,
    )

    kind_to_label = {
        "scan_x": axis_labels["r_l_text"],
        "scan_y": axis_labels["u_d_text"],
        "scan_z": axis_labels["b_f_text"],
        "time": axis_labels["timepoints_text"],
    }
    # The dialog has slots for three scan axes, timepoints and (via
    # n_linesteps) line-step conditions. A layout with any other loop cannot
    # be expressed by it, and a pre-fill that silently ignores a loop
    # reproduces a different scan, so it is declined instead.
    try:
        selected = select_loops(
            layout,
            consumer="MoNaLISA scan dialog",
            roles={
                "fast": "scan_x",
                "slow": "scan_y",
                "depth": "scan_z",
                "time": "time",
                "condition": "condition",
            },
        )
    except UnconsumedLoopError:
        return None
    placed = [
        selected[role] for role in ("fast", "slow", "depth", "time")
        if selected[role] is not None
    ]
    # event_loops run outermost to innermost; the dialog lists the fast axis
    # first, which is the same order reversed.
    loops = [loop for loop in reversed(layout.event_loops) if loop in placed]
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

    # The dialog has no condition axis. Line-step conditions ride on the
    # timepoints slot -- ``T = timepoints x conditions`` -- exactly as
    # ``coeffs_to_image`` reads it back, and ``n_linesteps`` says how to
    # de-interleave them. Leaving the condition loop out entirely made the
    # dialog claim one timepoint for a 648-frame 18x18x2 scan. The dialog's
    # arithmetic only knows conditions interleaved per line, so any other
    # placement of the condition loop is declined rather than pre-filled with
    # values that cannot reproduce the layout.
    from .coeffs_to_image import linestep_conditions_interleave_per_line

    condition = next(
        (loop for loop in layout.event_loops if loop.kind == "condition"), None
    )
    if condition is not None and not linestep_conditions_interleave_per_line(layout):
        return None
    n_linesteps = int(condition.count) if condition is not None else 1
    time_index = dimensions.index(axis_labels["timepoints_text"])
    steps[time_index] = str(int(steps[time_index]) * n_linesteps)

    # ``unidirectional`` is the dialog's word for "not a snake scan". The only
    # bidirectional order the dialog can express is a serpentine fast axis
    # whose parity is the (row, condition) pair coeffs_to_image assumes.
    # A scan of Y (and Z) alone has no scan_x loop: nothing can then be a
    # serpentine fast axis, and every rule must be forward.
    fast_id = next(
        (loop.id for loop in layout.event_loops if loop.kind == "scan_x"), None
    )
    allowed_parity = {
        loop.id for loop in layout.event_loops if loop.kind in ("scan_y", "condition")
    }
    unidirectional = True
    for rule in layout.traversal:
        if rule.order == "serpentine" and rule.loop_id == fast_id:
            if not set(rule.parity_loops) <= allowed_parity:
                return None
            unidirectional = False
        elif rule.order != "forward":
            # A retrace, or a serpentine on any other loop, is not a dialog scan.
            return None
    return {
        "dimensions": dimensions[:4],
        "directions": directions[:3],
        "steps": steps[:4],
        "step_sizes": step_sizes[:4],
        "n_linesteps": n_linesteps,
        "unidirectional": unidirectional,
    }
