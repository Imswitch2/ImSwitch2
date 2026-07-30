"""Phase-correlation refinement of tile placement for spiral tiling scans.

A tiling mosaic is normally laid out from the commanded stage positions alone:
tile ``(gx, gy)`` goes at ``(gy * step_y_px, gx * step_x_px)``. That is only as
good as the assumptions behind it — that the stage lands where it was told,
that the sample-plane pixel size is right, and that the camera axes line up
with the stage axes. When the mosaic does not overlap cleanly, any of the three
could be at fault, and they are hard to tell apart by eye.

This module measures the actual displacement between a new tile and the
already-placed canvas, using normalized phase cross-correlation over the region
where the two are expected to overlap. The measured shift is used to correct
placement, and — just as usefully — is reported so the *pattern* of the
residuals identifies the underlying cause:

* residuals scattered randomly, growing with speed -> stage settling / precision
* residuals proportional to the expected shift (a constant scale factor)
  -> sample-plane pixel size is wrong (magnification, or binning not applied)

Registration assumes the mosaic is already being assembled along the right
axes; see ``TilingController._gridToImage`` for that separate concern.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

#: Fraction of the nominal tile step a refinement may not exceed. A correction
#: larger than this is far more likely to be a correlation on repeating sample
#: structure than a real stage error, so it is rejected rather than applied.
DEFAULT_MAX_SHIFT_FRACTION = 0.5

#: Minimum overlap extent, in pixels, worth correlating. Below this the
#: estimate is dominated by edge effects.
MIN_OVERLAP_PX = 16


@dataclass
class TileShift:
    """One tile's measured displacement from its commanded position."""

    grid: Tuple[int, int]
    #: Refinement actually applied, in pixels, as ``(dy, dx)``.
    applied: Tuple[float, float]
    #: Raw correlation result before clamping/rejection, as ``(dy, dx)``.
    measured: Tuple[float, float]
    #: Nominal separation from the reference neighbour, as ``(dy, dx)``.
    expected: Tuple[float, float]
    #: Normalized correlation peak in [0, 1]; higher is more trustworthy.
    confidence: float
    accepted: bool
    reason: str = ''


@dataclass
class RegistrationReport:
    """Aggregate diagnostics over a whole tiling run."""

    shifts: list = field(default_factory=list)

    def add(self, shift: TileShift) -> None:
        self.shifts.append(shift)

    @property
    def accepted(self) -> list:
        return [s for s in self.shifts if s.accepted]

    def residual_rms(self) -> Optional[float]:
        """RMS magnitude of accepted refinements, in pixels."""
        accepted = self.accepted
        if not accepted:
            return None
        return float(np.sqrt(np.mean([
            s.applied[0] ** 2 + s.applied[1] ** 2 for s in accepted
        ])))

    def scale_estimate(self) -> Optional[float]:
        """Ratio of measured to expected separation, over accepted tiles.

        A value consistently away from 1.0 means the nominal geometry itself is
        wrong — the sample-plane pixel size, most often because the configured
        value does not account for the magnification actually in the light path
        or for camera binning. A value near the binning factor is the classic
        signature of the latter.
        """
        ratios = []
        for shift in self.accepted:
            for axis in (0, 1):
                expected = shift.expected[axis]
                if abs(expected) < MIN_OVERLAP_PX:
                    continue
                actual = expected + shift.applied[axis]
                ratios.append(actual / expected)
        if not ratios:
            return None
        return float(np.median(ratios))

    def summary(self) -> str:
        accepted = self.accepted
        if not self.shifts:
            return 'Tile registration: no tiles were registered.'
        if not accepted:
            return (
                f'Tile registration: 0 of {len(self.shifts)} tiles could be '
                'registered — overlap may be too small or too featureless.'
            )

        rms = self.residual_rms()
        scale = self.scale_estimate()
        worst = max(
            accepted,
            key=lambda s: s.applied[0] ** 2 + s.applied[1] ** 2,
        )
        parts = [
            f'Tile registration: {len(accepted)}/{len(self.shifts)} tiles '
            f'registered, RMS correction {rms:.1f} px, '
            f'worst {np.hypot(*worst.applied):.1f} px at grid {worst.grid}.'
        ]
        if scale is not None and abs(scale - 1.0) > 0.05:
            parts.append(
                f'Measured spacing is {scale:.3f}x the nominal spacing. A '
                'consistent factor like this is a scale error, not settling: '
                'either the sample-plane pixel size is wrong (magnification, '
                'or binning not accounted for) or the stage\'s µm calibration '
                'is. Settling shows up as scatter that grows with speed, not '
                'as a constant ratio.'
            )
        return ' '.join(parts)


def _normalize(image: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-variance float32 view, safe on flat input."""
    arr = np.asarray(image, dtype=np.float32)
    arr = arr - arr.mean()
    std = float(arr.std())
    if std <= np.finfo(np.float32).eps:
        return arr
    return arr / std


def _overlap_slices(
    ref_shape: Tuple[int, int],
    mov_shape: Tuple[int, int],
    offset: Tuple[int, int],
):
    """Slices of the region shared by two arrays given ``mov - ref`` offset.

    ``offset`` is where the moving array's origin sits in the reference
    array's coordinates. Returns ``(ref_slices, mov_slices)`` or None when the
    overlap is too small to correlate.
    """
    off_y, off_x = int(round(offset[0])), int(round(offset[1]))

    ref_y0 = max(0, off_y)
    ref_x0 = max(0, off_x)
    ref_y1 = min(ref_shape[0], off_y + mov_shape[0])
    ref_x1 = min(ref_shape[1], off_x + mov_shape[1])

    if (ref_y1 - ref_y0) < MIN_OVERLAP_PX or (ref_x1 - ref_x0) < MIN_OVERLAP_PX:
        return None

    mov_y0 = ref_y0 - off_y
    mov_x0 = ref_x0 - off_x
    mov_y1 = mov_y0 + (ref_y1 - ref_y0)
    mov_x1 = mov_x0 + (ref_x1 - ref_x0)

    return (
        (slice(ref_y0, ref_y1), slice(ref_x0, ref_x1)),
        (slice(mov_y0, mov_y1), slice(mov_x0, mov_x1)),
    )


def estimate_shift(
    reference: np.ndarray,
    moving: np.ndarray,
    nominal_offset: Tuple[float, float],
    *,
    max_shift_px: float,
    upsample_factor: int = 10,
) -> Tuple[Tuple[float, float], float, str]:
    """Measure how far ``moving`` actually sits from ``nominal_offset``.

    Args:
        reference: Already-placed image (typically the canvas region).
        moving: The new tile.
        nominal_offset: Commanded ``(dy, dx)`` of the moving image's origin
            within the reference's coordinate system.
        max_shift_px: Reject corrections larger than this.
        upsample_factor: Sub-pixel refinement factor.

    Returns:
        ``((dy, dx), confidence, reason)``. ``reason`` is empty when accepted;
        otherwise it explains the rejection and the shift is ``(0.0, 0.0)``.
    """
    try:
        from skimage.registration import phase_cross_correlation
    except ImportError:
        return (0.0, 0.0), 0.0, 'scikit-image is not available'

    slices = _overlap_slices(reference.shape[:2], moving.shape[:2], nominal_offset)
    if slices is None:
        return (0.0, 0.0), 0.0, 'expected overlap is too small to correlate'

    ref_slices, mov_slices = slices
    ref_patch = _normalize(reference[ref_slices])
    mov_patch = _normalize(moving[mov_slices])

    if ref_patch.std() <= 1e-6 or mov_patch.std() <= 1e-6:
        return (0.0, 0.0), 0.0, 'overlap region is featureless'

    try:
        # normalization=None means plain cross-correlation. Phase
        # normalization whitens the spectrum, which suits data whose
        # information is spread across all frequencies; smooth biological
        # texture is the opposite case, and there phase normalization is
        # dominated by high-frequency noise and returns nonsense. The patches
        # are already zero-mean/unit-variance above, so brightness and gain
        # differences between tiles are handled without it.
        shift, error, _phasediff = phase_cross_correlation(
            ref_patch, mov_patch,
            upsample_factor=upsample_factor,
            normalization=None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return (0.0, 0.0), 0.0, f'correlation failed: {exc}'

    # skimage returns the translation that maps `moving` onto `reference`,
    # in (row, col) — which is exactly the correction to add to the tile's
    # nominal placement.
    dy, dx = float(shift[0]), float(shift[1])
    confidence = float(np.clip(1.0 - error, 0.0, 1.0)) if np.isfinite(error) else 0.0

    magnitude = float(np.hypot(dy, dx))
    if magnitude > max_shift_px:
        return (
            (0.0, 0.0), confidence,
            f'correction of {magnitude:.1f} px exceeds the {max_shift_px:.1f} px '
            'limit; likely a false match on repeating structure',
        )

    return (dy, dx), confidence, ''


def max_shift_for_step(step_y_px: int, step_x_px: int,
                       fraction: float = DEFAULT_MAX_SHIFT_FRACTION) -> float:
    """Largest refinement worth trusting for a given nominal tile step."""
    return max(MIN_OVERLAP_PX, fraction * float(min(step_y_px, step_x_px)))
