"""Scan-direction and geometry check from the data itself.

A fixed object swept by a tilted light sheet drifts across the camera rows
from one plane to the next: with the deskew convention (camera rows tilted
by ``alpha`` from the scan axis, the scan advancing the sample by ``dy`` per
plane) its row centroid moves by ``-dy cos(alpha) / c_px`` rows per plane.
Reconstructed with the wrong scan direction the row and scan contributions
to ``y`` add instead of cancelling, and a round cell comes out about three
times too long along ``y`` and sheared. The sign of the measured drift says
which direction the data was scanned in; its magnitude checks ``dy``,
``alpha`` and ``c_px`` jointly, since none of them enters elsewhere in the
same combination.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Below this many rows per plane the drift is too small to call a direction.
MIN_DRIFT_ROWS_PER_PLANE = 0.2
#: Fraction by which the measured drift may differ from the expected one.
MAGNITUDE_TOLERANCE = 0.35


def expected_row_drift(c_px: float, alpha_deg: float, dy: float, flip_data: bool) -> float:
    """Rows per plane the signal should drift in the stack as stored."""
    drift = -float(dy) * np.cos(np.deg2rad(float(alpha_deg))) / float(c_px)
    return -drift if flip_data else drift


def measure_row_drift(stack: np.ndarray, camera_offset: float = 0.0) -> tuple[float, float]:
    """``(rows per plane, weight)`` of the signal's row centroid across planes.

    Each plane's row profile (offset-corrected, background-subtracted) is
    reduced to its bright core, the rows above half its peak, so haze and
    noise spread over the frame do not pull the centroid. Those centroids are
    regressed on the plane index over the planes whose core carries at least
    a quarter of the strongest one, so empty planes do not vote. ``weight``
    is the fraction of planes used; zero means there was nothing to measure.
    """
    stack = np.asarray(stack)
    if stack.ndim != 3 or stack.shape[0] < 3:
        return 0.0, 0.0
    planes = stack.shape[0]
    rows = np.arange(stack.shape[1], dtype=np.float64)
    centroids = np.zeros(planes)
    weights = np.zeros(planes)
    for index in range(planes):
        profile = stack[index].astype(np.float32).sum(axis=1)  # (cam_y,)
        profile = profile - float(camera_offset) * stack.shape[2]
        profile = np.clip(profile - np.percentile(profile, 20), 0.0, None)
        if profile.max() <= 0:
            continue
        core = np.where(profile >= 0.5 * profile.max(), profile, 0.0)
        total = float(core.sum())
        weights[index] = total
        centroids[index] = float(np.dot(core, rows) / total)
    if weights.max() <= 0:
        return 0.0, 0.0
    use = weights >= 0.25 * weights.max()
    if use.sum() < 5:
        return 0.0, 0.0
    x = np.arange(planes, dtype=np.float64)[use]
    slope = np.polyfit(x, centroids[use], 1, w=np.sqrt(weights[use]))[0]
    return float(slope), float(use.mean())


@dataclass(frozen=True)
class ScanDirectionCheck:
    measured: float
    expected: float
    weight: float
    message: str | None

    @property
    def ok(self) -> bool:
        return self.message is None


def check_scan_direction(stack: np.ndarray, params: dict) -> ScanDirectionCheck:
    """Compare the drift in ``stack`` (one restacked timepoint) with the geometry.

    Returns a check whose ``message`` names the problem when the sign
    contradicts ``flip_data`` or the magnitude contradicts ``dy cos(alpha) /
    c_px``; ``None`` when the data agrees or is too faint to tell.
    """
    expected = expected_row_drift(
        params["c_px"], params["alpha_deg"], params["dy"], bool(params.get("flip_data", False))
    )
    measured, weight = measure_row_drift(stack, float(params.get("camera_offset", 0.0)))
    message = None
    if weight > 0 and abs(measured) >= MIN_DRIFT_ROWS_PER_PLANE:
        if np.sign(measured) != np.sign(expected):
            wanted = "on" if not params.get("flip_data", False) else "off"
            message = (
                f"The signal drifts {measured:+.2f} camera rows per plane across the "
                f"scan, but the geometry with Flip data {'on' if params.get('flip_data') else 'off'} "
                f"expects {expected:+.2f}: the recording was scanned the other way. "
                f"Set Flip data {wanted}, otherwise round objects come out stretched "
                f"and sheared along y."
            )
        elif abs(abs(measured) - abs(expected)) > MAGNITUDE_TOLERANCE * abs(expected):
            message = (
                f"The signal drifts {abs(measured):.2f} camera rows per plane across the "
                f"scan; the geometry predicts dy cos(alpha) / c_px = {abs(expected):.2f}. "
                f"Check the scan step, tilt angle and camera pixel size."
            )
    return ScanDirectionCheck(measured, expected, weight, message)


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
