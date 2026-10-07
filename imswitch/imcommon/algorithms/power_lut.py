"""Laser power LUTs: from a measured (raw drive → power) sweep to a LUT file.

Measurement acceptance and LUT construction are separate steps
(``docs/design/plans/transient-instruments-step-scans.md`` §10):

1. **construction** — power is fitted as a monotone non-decreasing function
   of the raw drive (isotonic regression, weighted by sample count). Equal
   fitted powers (plateaus: a dark start, a saturated end, a reversal the fit
   flattened) collapse to their lowest drive, so the exported power is
   *strictly* increasing — what the inverse interpolation of the existing
   ``calibCsvPath`` consumer needs.
2. **acceptance** — the fit may only correct the measurement within its noise
   (default 3 σ), and the curve must rise well above the noise (default
   20 σ). A curve that fails either check is never exported.

The exported file is the two-column ``calibCsvPath`` format: ``#`` comment
lines, then ``setting power_W`` rows. The loaders read columns 0 and 1 with
``np.loadtxt``, subtract the minimum and normalise to 0–100 %.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np


def isotonic_increasing(values: Sequence[float], weights: Optional[Sequence[float]] = None) -> np.ndarray:
    """Weighted least-squares monotone non-decreasing fit (pool-adjacent-violators)."""
    y = np.asarray(values, dtype=float)
    w = np.ones_like(y) if weights is None else np.asarray(weights, dtype=float)
    if y.size == 0:
        return y.copy()
    # Each block: [weighted sum, weight, count]
    sums: List[float] = []
    wts: List[float] = []
    counts: List[int] = []
    for yi, wi in zip(y, w):
        sums.append(yi * wi)
        wts.append(wi)
        counts.append(1)
        while len(sums) > 1 and sums[-2] / wts[-2] > sums[-1] / wts[-1]:
            s, ww, c = sums.pop(), wts.pop(), counts.pop()
            sums[-1] += s
            wts[-1] += ww
            counts[-1] += c
    return np.concatenate([np.full(c, s / ww) for s, ww, c in zip(sums, wts, counts)])


@dataclass
class LutAnalysis:
    accepted: bool
    reasons: List[str]
    #: Per measured point, ascending drive.
    drive: np.ndarray
    power_measured: np.ndarray
    power_std: np.ndarray
    power_fitted: np.ndarray
    #: The rows the LUT exports (strictly increasing power).
    lut_drive: np.ndarray
    lut_power: np.ndarray
    noise_w: float
    correction_sigma: float
    dynamic_range_sigma: float


def construct_lut(
    drive: Sequence[float],
    power_mean: Sequence[float],
    power_std: Sequence[float],
    counts: Sequence[int],
    *,
    max_correction_sigma: float = 3.0,
    min_dynamic_range_sigma: float = 20.0,
    noise_floor_w: Optional[float] = None,
) -> LutAnalysis:
    """Fit, check and reduce a sweep; ``accepted`` says whether to export."""
    d = np.asarray(drive, dtype=float)
    p = np.asarray(power_mean, dtype=float)
    s = np.asarray(power_std, dtype=float)
    n = np.asarray(counts, dtype=float)
    order = np.argsort(d, kind='stable')
    d, p, s, n = d[order], p[order], s[order], n[order]
    reasons: List[str] = []

    if d.size < 2:
        reasons.append(f'{d.size} measured point(s); a LUT needs at least 2')
    if not (np.all(np.isfinite(d)) and np.all(np.isfinite(p))):
        reasons.append('non-finite drive or power values')
    if d.size and np.unique(d).size != d.size:
        reasons.append('the same drive value was measured more than once')
    if reasons:
        empty = np.zeros(0)
        return LutAnalysis(False, reasons, d, p, s, p.copy(), empty, empty,
                           math.nan, math.nan, math.nan)

    finite_std = s[np.isfinite(s)]
    # Noise: the scatter at the darkest point, or the typical scatter,
    # whichever is larger — never zero, so ratios stay finite.
    lowest = s[0] if np.isfinite(s[0]) else 0.0
    typical = float(np.median(finite_std)) if finite_std.size else 0.0
    # A separately measured dark noise (meter zeroed, beam blocked) is a floor.
    floor = float(noise_floor_w) if noise_floor_w is not None and math.isfinite(
        float(noise_floor_w)) else 0.0
    noise = max(lowest, typical, floor, 1e-15 * max(1.0, float(np.max(np.abs(p)))))
    fitted = isotonic_increasing(p, np.maximum(n, 1))
    correction = float(np.max(np.abs(p - fitted)) / noise)
    dynamic = float((fitted[-1] - fitted[0]) / noise)

    if correction > max_correction_sigma:
        worst = int(np.argmax(np.abs(p - fitted)))
        reasons.append(
            f'power is not monotonic in drive beyond the noise: the fit had to move the '
            f'point at drive {d[worst]:g} by {correction:.1f} σ (limit '
            f'{max_correction_sigma:g} σ); measure a narrower range')
    if dynamic < min_dynamic_range_sigma:
        reasons.append(
            f'power rises only {dynamic:.1f} σ above the noise (needs '
            f'{min_dynamic_range_sigma:g} σ); the curve is flat')

    keep = np.ones(d.size, dtype=bool)
    keep[1:] = fitted[1:] > fitted[:-1]        # first (lowest) drive of each plateau
    lut_d, lut_p = d[keep], fitted[keep]
    if lut_d.size < 2 and not reasons:
        reasons.append('fewer than two distinct power levels after the fit')
    return LutAnalysis(
        accepted=not reasons, reasons=reasons, drive=d, power_measured=p,
        power_std=s, power_fitted=fitted, lut_drive=lut_d, lut_power=lut_p,
        noise_w=noise, correction_sigma=correction, dynamic_range_sigma=dynamic,
    )


class LutRefused(RuntimeError):
    """The LUT does not meet the acceptance criteria; nothing was written."""


def write_calib_csv(path: Path, analysis: LutAnalysis, header: Mapping[str, object]) -> Path:
    """Write a ``calibCsvPath`` LUT atomically — only an accepted analysis."""
    if not analysis.accepted:
        raise LutRefused('LUT not exported: ' + '; '.join(analysis.reasons))
    path = Path(path)
    lines = [f'# {key}: {value}' for key, value in header.items()]
    lines.append('# columns: setting power_W  (calibCsvPath LUT; fitted, strictly increasing)')
    rows = [f'{d:.9g} {p:.9g}' for d, p in zip(analysis.lut_drive, analysis.lut_power)]
    tmp = path.with_name(f'.{path.name}.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines + rows) + '\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path
