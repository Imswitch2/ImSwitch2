"""Focus-lock frame trust, hold/lost state machine, bounded relock and
persistence -- the parts of the focus lock that need no Qt and no hardware.

The controller (``FocusLockController``) owns timing, widgets and the
actuator; this module only answers questions:

* :func:`measure_frame` -- the per-frame features of the reflected spot
  (row profile, intensity, width, second peak, saturation, clipping, a frame
  signature) plus, against a stored :class:`SpotReference`, the normalised
  cross-correlation (NCC) value and the sub-pixel shift it implies.
* :func:`assess_frame` -- is this frame trustworthy?  A verdict built from
  independent tests, each with its own threshold and each reported by name.
* :class:`TrustLearner` -- thresholds derived from the spread seen in the
  first seconds after locking, with fixed fallbacks.
* :class:`LockSupervisor` -- locked / holding / lost with hysteresis and N
  consecutive good frames; paused for orchestrators.
* :func:`plan_relock_step` -- one clamped Z step toward the setpoint.
* :func:`save_lock_state` / :func:`load_lock_state` -- atomic JSON per setup
  and per focus actuator.

Units: positions on the camera are rows (px) of the focus camera frame; Z is
in micrometres of the focus actuator; the calibration is the slope of signal
against Z in px/µm (what the focus-lock calibration sweep fits).
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

#: Half-height of the spot window, rows (matches the controller's estimator).
WINDOW_HALF = 50
#: Fraction of the background-subtracted peak below which pixels are ignored
#: for centroid, width and intensity (matches the controller's estimator).
THRESHOLD_FRACTION = 0.10
#: Largest displacement (rows) the correlation searches either way.
MAX_SHIFT_PX = 40


# ---------------------------------------------------------------------------
# Per-frame features
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameFeatures:
    centroid: float                 # thresholded row centroid, frame rows
    peakAboveBackground: float      # filtered peak minus frame background
    noise: float                    # robust noise of the filtered frame
    intensity: float                # integrated thresholded spot signal
    width: float                    # RMS width of the thresholded row profile
    secondPeakRatio: float          # strongest maximum outside the window / peak (nan: not tested)
    saturatedFraction: float        # of the raw spot window (nan: no level known)
    clipped: bool                   # thresholded spot touches the frame edge
    signature: int                  # crc of a raw subsample (duplicate detection)
    correlation: float = float('nan')   # NCC with the reference (nan: none)
    correlationPosition: float = float('nan')  # reference centroid + NCC shift
    profile: Optional[np.ndarray] = None       # background-subtracted, ref band
    profileStart: int = 0
    colLo: int = 0
    colHi: int = 0
    peakPosition: float = float('nan')         # sub-pixel maximum of the row profile

    @property
    def snr(self) -> float:
        return self.peakAboveBackground / max(self.noise, 1e-9)


@dataclass(frozen=True)
class SpotReference:
    """What the spot looked like when the lock was engaged."""
    profile: np.ndarray             # background-subtracted row profile
    start: int                      # frame row of profile[0]
    colLo: int
    colHi: int
    centroid: float
    intensity: float
    width: float
    peak: float
    createdAt: float = 0.0
    peakOffset: float = 0.0         # centroid - profile maximum, at reference time

    def to_dict(self) -> dict:
        return {
            'profile': [float(v) for v in self.profile],
            'start': int(self.start), 'colLo': int(self.colLo), 'colHi': int(self.colHi),
            'centroid': float(self.centroid), 'intensity': float(self.intensity),
            'width': float(self.width), 'peak': float(self.peak),
            'createdAt': float(self.createdAt), 'peakOffset': float(self.peakOffset),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SpotReference':
        profile = np.asarray(data['profile'], dtype=np.float32)
        if profile.ndim != 1 or profile.size < 5 or not np.all(np.isfinite(profile)):
            raise ValueError('stored focus-lock reference profile is invalid')
        values = {k: float(data[k]) for k in ('centroid', 'intensity', 'width', 'peak')}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError('stored focus-lock reference is not finite')
        return cls(profile=profile, start=int(data['start']), colLo=int(data['colLo']),
                   colHi=int(data['colHi']), createdAt=float(data.get('createdAt', 0.0)),
                   peakOffset=float(data.get('peakOffset', 0.0)), **values)


def frame_signature(raw: np.ndarray) -> int:
    sub = np.ascontiguousarray(np.asarray(raw)[::7, ::7])
    return zlib.crc32(sub.tobytes())


def _robust_background(filtered: np.ndarray):
    sub = filtered[::4, ::4]
    bg = float(np.median(sub))
    noise = 1.4826 * float(np.median(np.abs(sub - bg)))
    return bg, noise


def _thresholded(window: np.ndarray, bg: float):
    weights = np.asarray(window, dtype=np.float32) - bg
    peak = float(weights.max()) if weights.size else 0.0
    if peak <= 0.0:
        return np.zeros_like(weights), peak
    weights = weights - THRESHOLD_FRACTION * peak
    np.clip(weights, 0.0, None, out=weights)
    return weights, peak


def correlate_profile(reference: SpotReference, fullProfile: np.ndarray,
                      maxShift: int = MAX_SHIFT_PX):
    """``(ncc, shift)`` of ``reference.profile`` against a full-height profile.

    Zero-mean normalised cross-correlation over integer shifts within
    ``±maxShift`` rows, refined to sub-pixel with a parabola through the best
    three. ``(nan, nan)`` when no shift fits inside the frame.
    """
    ref = np.asarray(reference.profile, dtype=np.float64)
    n = ref.size
    lo = max(-maxShift, -reference.start)
    hi = min(maxShift, fullProfile.size - n - reference.start)
    if hi < lo:
        return float('nan'), float('nan')
    segment = np.asarray(fullProfile[reference.start + lo: reference.start + hi + n],
                         dtype=np.float64)
    windows = np.lib.stride_tricks.sliding_window_view(segment, n)
    refZero = ref - ref.mean()
    refNorm = math.sqrt(float((refZero ** 2).sum()))
    winZero = windows - windows.mean(axis=1, keepdims=True)
    winNorm = np.sqrt((winZero ** 2).sum(axis=1))
    denom = refNorm * winNorm
    with np.errstate(invalid='ignore', divide='ignore'):
        ncc = np.where(denom > 0, (winZero @ refZero) / denom, -1.0)
    best = int(np.argmax(ncc))
    shift = float(lo + best)
    if 0 < best < ncc.size - 1:
        a, b, c = ncc[best - 1], ncc[best], ncc[best + 1]
        curvature = a - 2 * b + c
        if curvature < 0:
            shift += float(np.clip(0.5 * (a - c) / curvature, -0.5, 0.5))
    return float(ncc[best]), shift


def measure_frame(filtered: np.ndarray, raw: np.ndarray, peakRow: int, peakCol: int,
                  reference: Optional[SpotReference] = None, *, twoFoci: bool = False,
                  saturationLevel: Optional[float] = None,
                  half: int = WINDOW_HALF) -> FrameFeatures:
    """Features of the spot around ``(peakRow, peakCol)`` of a filtered frame.

    ``raw`` is the unfiltered frame (same orientation) used for saturation and
    the duplicate signature. The correlation uses the reference's column band
    when a reference is given, else the spot window's.
    """
    filtered = np.asarray(filtered, dtype=np.float32)
    height, width = filtered.shape[:2]
    bg, noise = _robust_background(filtered)
    r0, r1 = max(0, peakRow - half), min(height, peakRow + half)
    c0, c1 = max(0, peakCol - half), min(width, peakCol + half)
    weights, peak = _thresholded(filtered[r0:r1, c0:c1], bg)
    rowProfile = weights.sum(axis=1)
    total = float(rowProfile.sum())
    if total > 0:
        rows = np.arange(rowProfile.size, dtype=np.float64)
        centre = float((rowProfile * rows).sum() / total)
        spread = math.sqrt(max(0.0, float((rowProfile * (rows - centre) ** 2).sum() / total)))
        centroid = centre + r0
    else:
        centroid, spread = float(peakRow), 0.0
    clipped = bool(
        (r0 == 0 and weights[0].any()) or (r1 == height and weights[-1].any())
        or (c0 == 0 and weights[:, 0].any()) or (c1 == width and weights[:, -1].any()))

    secondRatio = float('nan')
    if not twoFoci and peak > 0:
        outside = [filtered[:r0], filtered[r1:], filtered[r0:r1, :c0], filtered[r0:r1, c1:]]
        others = [float(block.max()) for block in outside if block.size]
        secondRatio = max(0.0, (max(others) - bg) / peak) if others else 0.0

    rawWindow = np.asarray(raw)[r0:r1, c0:c1]
    level = saturationLevel
    if level is None and np.issubdtype(rawWindow.dtype, np.integer):
        level = float(np.iinfo(rawWindow.dtype).max)
    saturated = (float(np.count_nonzero(rawWindow >= level)) / max(rawWindow.size, 1)
                 if level is not None else float('nan'))

    colLo, colHi = (reference.colLo, reference.colHi) if reference is not None else (c0, c1)
    colLo, colHi = max(0, min(colLo, width - 1)), max(1, min(colHi, width))
    fullProfile = (filtered[:, colLo:colHi] - bg).sum(axis=1)
    ncc, corrPos = float('nan'), float('nan')
    if reference is not None:
        ncc, shift = correlate_profile(reference, fullProfile)
        if math.isfinite(shift):
            corrPos = reference.centroid + shift
    # Sub-pixel maximum of the row profile near the spot: an estimator that,
    # unlike the centroid and the correlation, ignores the flanks -- so a
    # partially occluded (asymmetric) spot makes it disagree with the centroid.
    peakPos = float('nan')
    lo_, hi_ = max(1, r0), min(height - 1, r1)
    if hi_ - lo_ >= 3:
        i = lo_ + int(np.argmax(fullProfile[lo_:hi_]))
        if 0 < i < height - 1:
            a, b, c = (float(fullProfile[i - 1]), float(fullProfile[i]),
                       float(fullProfile[i + 1]))
            curvature = a - 2 * b + c
            peakPos = float(i) + (float(np.clip(0.5 * (a - c) / curvature, -0.5, 0.5))
                                  if curvature < 0 else 0.0)
    n = 2 * half
    start = int(min(max(0, peakRow - half), max(0, height - n)))
    return FrameFeatures(
        centroid=centroid, peakAboveBackground=peak, noise=noise, intensity=total,
        width=spread, secondPeakRatio=secondRatio, saturatedFraction=saturated,
        clipped=clipped, signature=frame_signature(raw), correlation=ncc,
        correlationPosition=corrPos, profile=fullProfile[start:start + n].astype(np.float32),
        profileStart=start, colLo=colLo, colHi=colHi, peakPosition=peakPos,
    )


def make_reference(features: FrameFeatures, createdAt: Optional[float] = None) -> SpotReference:
    if features.profile is None or features.profile.size < 5:
        raise ValueError('no spot profile to take a reference from')
    return SpotReference(
        profile=np.array(features.profile, dtype=np.float32, copy=True),
        start=int(features.profileStart), colLo=int(features.colLo), colHi=int(features.colHi),
        centroid=float(features.centroid), intensity=float(features.intensity),
        width=float(features.width), peak=float(features.peakAboveBackground),
        createdAt=time.time() if createdAt is None else float(createdAt),
        peakOffset=(float(features.centroid - features.peakPosition)
                    if math.isfinite(features.peakPosition) else 0.0),
    )


# ---------------------------------------------------------------------------
# Trust verdict
# ---------------------------------------------------------------------------

#: Test names, in the order they are reported.
TESTS = ('stale', 'signal', 'intensity', 'correlation', 'width', 'second-peak',
         'saturation', 'clipped', 'jump', 'agreement')


@dataclass(frozen=True)
class TrustThresholds:
    """One threshold per test. Fixed defaults are the fallbacks used before
    (or without) learning; :class:`TrustLearner` replaces them per lock."""
    minSnr: float = 8.0                 # peak above background, x noise
    intensityBand: tuple = (0.5, 2.0)   # x reference intensity
    minCorrelation: float = 0.85        # NCC with the reference
    widthBand: tuple = (0.7, 1.4)       # x reference width
    maxSecondPeakRatio: float = 0.6     # outside two-foci mode
    maxSaturatedFraction: float = 0.02  # of the spot window
    maxJumpPx: float = 5.0              # innovation: |jump - expected|
    maxDisagreementPx: float = 2.0      # |centroid - correlation position - baseline|
    staleFramesAllowed: int = 3         # duplicate frames skipped before failing
    learned: bool = False

    def strict(self) -> 'TrustThresholds':
        """Thresholds to *leave* holding: stricter than to enter it."""
        def narrow(band, f=0.75):
            return (1 - (1 - band[0]) * f, 1 + (band[1] - 1) * f)
        return dataclasses.replace(
            self, minSnr=self.minSnr * 1.25, intensityBand=narrow(self.intensityBand),
            minCorrelation=min(0.995, self.minCorrelation + 0.03),
            widthBand=narrow(self.widthBand),
            maxSecondPeakRatio=self.maxSecondPeakRatio * 0.8,
            maxSaturatedFraction=self.maxSaturatedFraction * 0.5,
            maxJumpPx=self.maxJumpPx * 0.6, maxDisagreementPx=self.maxDisagreementPx * 0.6)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d['intensityBand'] = list(self.intensityBand)
        d['widthBand'] = list(self.widthBand)
        return d


@dataclass(frozen=True)
class FrameVerdict:
    trusted: bool
    reasons: tuple                   # names of the failed tests
    metrics: dict                    # value per test
    confidence: float                # 0..1, for display (NCC when known)
    skip: bool = False               # stale but within allowance: no update, no state change

    @property
    def reasonText(self) -> str:
        return ', '.join(self.reasons) if self.reasons else 'ok'


@dataclass
class FrameContext:
    """What the controller knows beyond the frame itself."""
    previousSignal: Optional[float] = None
    expectedShiftPx: float = 0.0       # calibration x last applied correction
    staleCount: int = 0                # consecutive duplicates including this one
    disagreementBaseline: float = 0.0


def disagreement(features: FrameFeatures, reference: Optional[SpotReference],
                 baseline: float = 0.0) -> float:
    """Largest disagreement (px) of the centroid with the correlation
    position and with the profile maximum, each against its value at
    reference time; nan when neither is available."""
    values = []
    if math.isfinite(features.correlationPosition):
        values.append(abs(features.centroid - features.correlationPosition - baseline))
    if reference is not None and math.isfinite(features.peakPosition):
        values.append(abs(features.centroid - features.peakPosition - reference.peakOffset))
    return max(values) if values else float('nan')


def assess_frame(features: FrameFeatures, reference: Optional[SpotReference],
                 thresholds: TrustThresholds, context: FrameContext,
                 *, strict: bool = False) -> FrameVerdict:
    t = thresholds.strict() if strict else thresholds
    failed, metrics = [], {}

    def check(name, value, ok):
        metrics[name] = value
        if not ok:
            failed.append(name)

    stale = context.staleCount > 0
    metrics['stale'] = context.staleCount
    if stale and context.staleCount > t.staleFramesAllowed:
        failed.append('stale')
    check('signal', features.snr, features.snr >= t.minSnr)
    if reference is not None:
        ratio = features.intensity / max(reference.intensity, 1e-9)
        check('intensity', ratio, t.intensityBand[0] <= ratio <= t.intensityBand[1])
        ncc = features.correlation
        check('correlation', ncc, math.isfinite(ncc) and ncc >= t.minCorrelation)
        wratio = features.width / max(reference.width, 1e-9)
        check('width', wratio, t.widthBand[0] <= wratio <= t.widthBand[1])
    if math.isfinite(features.secondPeakRatio):
        check('second-peak', features.secondPeakRatio,
              features.secondPeakRatio <= t.maxSecondPeakRatio)
    if math.isfinite(features.saturatedFraction):
        check('saturation', features.saturatedFraction,
              features.saturatedFraction <= t.maxSaturatedFraction)
    check('clipped', features.clipped, not features.clipped)
    if context.previousSignal is not None and not stale:
        innovation = abs((features.centroid - context.previousSignal) - context.expectedShiftPx)
        check('jump', innovation, innovation <= t.maxJumpPx)
    gap = disagreement(features, reference, context.disagreementBaseline)
    if math.isfinite(gap):
        check('agreement', gap, gap <= t.maxDisagreementPx)
    order = {name: i for i, name in enumerate(TESTS)}
    reasons = tuple(sorted(failed, key=lambda n: order.get(n, 99)))
    ncc = features.correlation
    if math.isfinite(ncc):
        confidence = max(0.0, min(1.0, float(ncc)))
    else:
        confidence = max(0.0, min(1.0, features.snr / (2 * t.minSnr)))
    if reasons:
        confidence = min(confidence, 0.5)
    return FrameVerdict(trusted=not reasons, reasons=reasons, metrics=metrics,
                        confidence=confidence,
                        skip=stale and not reasons)


class TrustLearner:
    """Collect the normal spread in the first seconds after locking and turn
    it into per-lock thresholds; never looser than the floors below, so a
    lock taken on a bad spot cannot learn that bad is normal."""

    K = 8.0  # sigmas

    def __init__(self, windowS: float = 3.0, minSamples: int = 20):
        self.windowS = float(windowS)
        self.minSamples = int(minSamples)
        self._startedAt = None
        self._samples = []
        self.result: Optional[TrustThresholds] = None

    def start(self, now: float):
        self._startedAt = now
        self._samples = []
        self.result = None

    @property
    def active(self) -> bool:
        return self._startedAt is not None and self.result is None

    def add(self, features: FrameFeatures, reference: SpotReference, innovation: Optional[float],
            now: float, base: TrustThresholds) -> Optional[TrustThresholds]:
        if not self.active:
            return self.result
        self._samples.append((
            features.intensity / max(reference.intensity, 1e-9),
            features.width / max(reference.width, 1e-9),
            features.correlation, features.secondPeakRatio, features.snr,
            np.nan if innovation is None else innovation,
            disagreement(features, reference),
        ))
        if now - self._startedAt >= self.windowS and len(self._samples) >= self.minSamples:
            self.result = self.derive(np.asarray(self._samples, dtype=np.float64), base)
        return self.result

    @classmethod
    def derive(cls, samples: np.ndarray, base: TrustThresholds) -> TrustThresholds:
        k = cls.K

        def stat(col):
            values = samples[:, col]
            values = values[np.isfinite(values)]
            if values.size < 3:
                return None, None
            return float(np.mean(values)), float(np.std(values))

        updates = {'learned': True}
        m, s = stat(0)
        if m is not None:
            # Never stricter than the fixed band: the reflected intensity may
            # legitimately change from tile to tile with the sample.
            updates['intensityBand'] = (max(0.2, min(base.intensityBand[0], 1 - k * s)),
                                        min(5.0, max(base.intensityBand[1], 1 + k * s)))
        m, s = stat(1)
        if m is not None:
            updates['widthBand'] = (max(0.4, 1 - max(k * s, 0.2)), min(2.5, 1 + max(k * s, 0.3)))
        m, s = stat(2)
        if m is not None:
            updates['minCorrelation'] = float(np.clip(m - max(k * s, 0.01), 0.6, 0.995))
        m, s = stat(3)
        if m is not None:
            updates['maxSecondPeakRatio'] = float(np.clip(m + max(k * s, 0.15),
                                                          base.maxSecondPeakRatio, 0.9))
        m, s = stat(4)
        if m is not None:
            updates['minSnr'] = float(max(5.0, min(base.minSnr, 0.3 * m)))
        m, s = stat(5)
        if m is not None:
            updates['maxJumpPx'] = float(max(2.0, min(base.maxJumpPx * 3, m + k * s)))
        m, s = stat(6)
        if m is not None:
            updates['maxDisagreementPx'] = float(max(0.3, min(base.maxDisagreementPx * 3,
                                                          m + k * s)))
        return dataclasses.replace(base, **updates)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

LOCKED = 'locked'
HOLDING = 'holding'
LOST = 'lost'
PAUSED = 'paused'
RELOCKING = 'relocking'


@dataclass
class SupervisorSettings:
    goodFramesToResume: int = 5     # consecutive trustworthy frames to leave holding
    holdTimeoutS: float = 5.0       # holding longer than this -> lost


class LockSupervisor:
    """Locked / holding / lost. Pure bookkeeping: the controller decides
    what ``'apply'`` / ``'hold'`` mean for the actuator."""

    def __init__(self, settings: Optional[SupervisorSettings] = None):
        self.settings = settings or SupervisorSettings()
        self.state = LOCKED
        self.holdSince = None
        self.goodCount = 0
        self.lastReasons = ()

    def reset(self):
        self.state = LOCKED
        self.holdSince = None
        self.goodCount = 0
        self.lastReasons = ()

    def frame(self, enterVerdict: FrameVerdict, exitVerdict: FrameVerdict, now: float) -> str:
        """Advance by one frame; returns ``'apply'``, ``'hold'``, ``'resume'``
        (left holding on this frame: restart the loop fresh) or ``'lost'``."""
        if self.state == LOCKED:
            if enterVerdict.skip:
                return 'hold'
            if enterVerdict.trusted:
                return 'apply'
            self.enterHolding(now, enterVerdict.reasons)
            return 'hold'
        if self.state == HOLDING:
            if exitVerdict.skip:
                pass
            elif exitVerdict.trusted:
                self.goodCount += 1
                if self.goodCount >= max(1, int(self.settings.goodFramesToResume)):
                    self.state = LOCKED
                    self.holdSince = None
                    self.goodCount = 0
                    return 'resume'
            else:
                self.goodCount = 0
                self.lastReasons = exitVerdict.reasons
            if now - self.holdSince > float(self.settings.holdTimeoutS):
                self.state = LOST
                return 'lost'
            return 'hold'
        return 'hold'

    def enterHolding(self, now: float, reasons=()):
        self.state = HOLDING
        self.holdSince = now
        self.goodCount = 0
        self.lastReasons = tuple(reasons)


# ---------------------------------------------------------------------------
# Relock
# ---------------------------------------------------------------------------

@dataclass
class RelockLimits:
    maxStepUm: float = 0.5          # per commanded relock step
    maxExcursionUm: float = 3.0     # total from the last good Z
    tolerancePx: float = 1.0        # hand over to the PI loop within this error
    minCorrelation: float = 0.7     # below: no step that increases the excursion
    timeoutS: float = 10.0
    settleFrames: int = 2           # frames to wait after each step
    searchEnabled: bool = False     # bounded Z search when the spot is not found
    searchRangeUm: float = 2.0      # ± around the last good Z (<= maxExcursionUm)
    searchStepUm: float = 0.5

    def validate(self):
        for name in ('maxStepUm', 'maxExcursionUm', 'tolerancePx', 'timeoutS',
                     'searchRangeUm', 'searchStepUm'):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'focus-lock relock {name} must be positive, got {value!r}')
        if self.maxStepUm > self.maxExcursionUm:
            raise ValueError('focus-lock relock step must not exceed the total excursion')
        return self


@dataclass(frozen=True)
class RelockStep:
    stepUm: float                   # 0.0: no move
    done: bool                      # within tolerance: engage the loop
    reason: str
    clamped: bool = False


def plan_relock_step(errorPx: float, pxPerUm: Optional[float], excursionUm: float,
                     confidence: float, limits: RelockLimits) -> RelockStep:
    """One bounded step toward the setpoint.

    ``errorPx`` is setpoint minus measured position; ``excursionUm`` the Z
    already commanded since the last good Z. The step is clamped to
    ``maxStepUm`` and so that ``|excursion + step| <= maxExcursionUm``; on
    confidence below ``minCorrelation`` a step that would increase the
    excursion is refused.
    """
    if not math.isfinite(errorPx):
        return RelockStep(0.0, False, 'no measurement')
    if abs(errorPx) <= limits.tolerancePx:
        return RelockStep(0.0, True, 'within tolerance')
    if pxPerUm is None or not math.isfinite(pxPerUm) or abs(pxPerUm) < 1e-6:
        return RelockStep(0.0, False, 'no valid calibration')
    want = errorPx / pxPerUm
    step = max(-limits.maxStepUm, min(limits.maxStepUm, want))
    clamped = step != want
    target = excursionUm + step
    if abs(target) > limits.maxExcursionUm:
        bound = math.copysign(limits.maxExcursionUm, target)
        step = bound - excursionUm
        clamped = True
    increases = abs(excursionUm + step) > abs(excursionUm) + 1e-12
    if increases and not (math.isfinite(confidence) and confidence >= limits.minCorrelation):
        return RelockStep(0.0, False, 'low confidence', clamped)
    if abs(step) < 1e-9:
        return RelockStep(0.0, False, 'excursion limit reached', True)
    return RelockStep(float(step), False, 'step', clamped)


def search_offsets(limits: RelockLimits):
    """Z offsets (from the last good Z) of the optional bounded search:
    0, +s, -s, +2s, -2s, ... within ``min(searchRangeUm, maxExcursionUm)``."""
    span = min(limits.searchRangeUm, limits.maxExcursionUm)
    step = min(limits.searchStepUm, limits.maxStepUm)
    offsets, k = [], 1
    while k * step <= span + 1e-9:
        offsets += [k * step, -k * step]
        k += 1
    return offsets


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

STATE_VERSION = 1


def lock_state_path() -> Path:
    from imswitch.imcommon.model import dirtools
    return Path(dirtools.UserFileDirs.Config) / 'focus_lock_state.json'


def _read_states(path: Path) -> dict:
    try:
        content = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f'{path} is not a focus-lock state file')
    return content


def save_lock_state(setupName: str, actuator: str, state: dict,
                    path: Optional[Path] = None) -> Path:
    """Write ``state`` for (setup, actuator) atomically; other entries kept."""
    target = Path(path) if path is not None else lock_state_path()
    try:
        content = _read_states(target)
    except (ValueError, OSError):
        content = {}          # an unreadable file is replaced, not merged
    entry = dict(state)
    entry['version'] = STATE_VERSION
    entry['savedAt'] = time.time()
    content.setdefault(str(setupName or ''), {})[str(actuator)] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', prefix='.focuslock_',
                                         suffix='.tmp', dir=target.parent, delete=False) as fh:
            temp = fh.name
            json.dump(content, fh, indent=1)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
        temp = None
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)
    return target


def load_lock_state(setupName: str, actuator: str, path: Optional[Path] = None) -> Optional[dict]:
    target = Path(path) if path is not None else lock_state_path()
    entry = _read_states(target).get(str(setupName or ''), {}).get(str(actuator))
    if entry is None:
        return None
    if not isinstance(entry, dict) or entry.get('version') != STATE_VERSION:
        raise ValueError('focus-lock state was written by another version; lock again')
    return entry


def limits_from_dict(data: Optional[dict], default: Optional[RelockLimits] = None) -> RelockLimits:
    limits = dataclasses.replace(default or RelockLimits())
    for f in dataclasses.fields(RelockLimits):
        if data and f.name in data:
            setattr(limits, f.name, type(getattr(limits, f.name))(data[f.name]))
    return limits.validate()


# ---------------------------------------------------------------------------
# Engine: the controller-side bookkeeping, still Qt-free
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelockDecision:
    action: str                     # 'wait' | 'move' | 'success' | 'fail'
    stepUm: float = 0.0             # for 'move'; for 'fail' the restore move (0: none)
    reason: str = ''


class FocusLockEngine:
    """Hold / lost / pause / relock logic of one focus lock.

    The controller feeds it every focus estimate (``features`` may be None
    when the worker could not measure them; the engine then trusts the frame
    as the lock always did, so only the PI safety limit can hold it) and
    executes what it returns. All methods are called on the controller's
    thread; :meth:`statusSnapshot` returns an immutable copy for other threads.
    """

    def __init__(self):
        self.supervisorSettings = SupervisorSettings()
        self.supervisor = LockSupervisor(self.supervisorSettings)
        self.baseThresholds = TrustThresholds()
        self.thresholds = self.baseThresholds
        self.learner = TrustLearner()
        self.limits = RelockLimits()
        self.reference: Optional[SpotReference] = None
        self.pxPerUm: Optional[float] = None       # calibration slope, signal px per Z µm
        self.setPoint: Optional[float] = None      # kept across hold / lost / restart
        self.mode = LOCKED                         # LOCKED / HOLDING / PAUSED / RELOCKING
        self.lost = False
        self.lostReason = ''
        self.prevSignal = None
        self.lastSignature = None
        self.staleCount = 0
        self.lastAppliedUm = 0.0
        self.lastFeatures: Optional[FrameFeatures] = None
        self.lastVerdict: Optional[FrameVerdict] = None
        self.relock = None                         # dict while relocking
        #: Z commanded by relock/search since Z was last known good (lock
        #: correcting on trusted frames); bounded by limits.maxExcursionUm
        #: across relock attempts, back to 0 when a relock returns there.
        self.offsetFromGoodUm = 0.0
        self.lastRelockResult = ''
        self._snapshot = {}

    # -- lifecycle ---------------------------------------------------------
    def engage(self, setPoint: float, now: float):
        """A new lock: reference from the newest frame, learning restarts."""
        self.setPoint = float(setPoint)
        features = self.lastFeatures
        self.reference = None
        if features is not None and features.profile is not None:
            try:
                self.reference = make_reference(features)
            except ValueError:
                self.reference = None
        self.thresholds = self.baseThresholds
        self.learner.start(now)
        self._resetLoop()

    def reengage(self, setPoint: float):
        """The same lock back (after a scan or a relock): reference kept."""
        self.setPoint = float(setPoint)
        self._resetLoop()

    def disengage(self):
        """User unlock: nothing is held or lost any more. Reference, setpoint
        and calibration stay for an explicit re-engage."""
        self.lost = False
        self.lostReason = ''
        self.relock = None
        self._resetLoop()

    def _resetLoop(self):
        self.offsetFromGoodUm = 0.0
        self.supervisor.reset()
        self.mode = LOCKED
        self.lost = False
        self.relock = None
        self.prevSignal = None
        self.lastAppliedUm = 0.0

    def markLost(self, reason: str):
        self.lost = True
        self.lostReason = reason
        self.mode = LOCKED
        self.relock = None
        self.supervisor.reset()

    # -- per frame ---------------------------------------------------------
    def observe(self, features: Optional[FrameFeatures], signal: float):
        """Bookkeeping every frame, whatever the state."""
        self.lastFeatures = features
        if features is not None:
            if features.signature == self.lastSignature:
                self.staleCount += 1
            else:
                self.staleCount = 0
            self.lastSignature = features.signature

    def _context(self, signal, *, withJump=True):
        expected = 0.0
        if self.pxPerUm is not None and math.isfinite(self.pxPerUm):
            expected = self.lastAppliedUm * self.pxPerUm
        return FrameContext(previousSignal=self.prevSignal if withJump else None,
                            expectedShiftPx=expected, staleCount=self.staleCount)

    def _verdicts(self, features, signal, *, withJump=True):
        if features is None:
            trusted = FrameVerdict(True, (), {}, float('nan'))
            return trusted, trusted
        context = self._context(signal, withJump=withJump)
        enter = assess_frame(features, self.reference, self.thresholds, context)
        exitV = assess_frame(features, self.reference, self.thresholds, context, strict=True)
        # A frame taken before the last correction landed shows no shift; one
        # taken after shows the expected shift. Accept either (innovation is
        # min over both hypotheses), never a third.
        if context.expectedShiftPx and 'jump' in enter.reasons and context.previousSignal is not None:
            plain = dataclasses.replace(context, expectedShiftPx=0.0)
            e2 = assess_frame(features, self.reference, self.thresholds, plain)
            x2 = assess_frame(features, self.reference, self.thresholds, plain, strict=True)
            if 'jump' not in e2.reasons:
                enter = e2
            if 'jump' not in x2.reasons:
                exitV = x2
        return enter, exitV

    def lockedFrame(self, features, signal, now) -> str:
        """LOCKED/HOLDING frame: ``'apply'``, ``'hold'``, ``'resume'`` or ``'lost'``."""
        enter, exitV = self._verdicts(features, signal)
        if not enter.skip:
            self.prevSignal = signal if features is None else features.centroid
        self.lastAppliedUm = 0.0
        action = self.supervisor.frame(enter, exitV, now)
        self.lastVerdict = enter if self.supervisor.state == LOCKED or action == 'resume' else exitV
        if action == 'apply' and features is not None and self.reference is not None \
                and self.learner.active:
            innovation = (enter.metrics.get('jump') if 'jump' in enter.metrics else None)
            learned = self.learner.add(features, self.reference, innovation, now,
                                       self.baseThresholds)
            if learned is not None:
                self.thresholds = learned
        self.mode = HOLDING if self.supervisor.state == HOLDING else LOCKED
        if action == 'lost':
            self.markLost('held longer than %.1f s (%s)' % (
                self.supervisorSettings.holdTimeoutS,
                ', '.join(self.supervisor.lastReasons) or 'untrusted frames'))
        return action

    def holdForSafety(self, now, stepUm):
        reason = 'safety: step %.3f µm' % stepUm
        if self.supervisor.state == LOCKED:
            self.supervisor.enterHolding(now, ('safety',))
        else:
            self.supervisor.goodCount = 0
            self.supervisor.lastReasons = ('safety',)
        self.mode = HOLDING
        return reason

    def noteApplied(self, moveUm: float):
        self.lastAppliedUm += float(moveUm)

    # -- pause / relock ----------------------------------------------------
    def pause(self):
        self.mode = PAUSED
        self.relock = None
        self.supervisor.reset()
        self.prevSignal = None
        self.lastAppliedUm = 0.0

    def beginRelock(self, now: float, settleS: float = 0.0, timeoutS: Optional[float] = None):
        self.mode = RELOCKING
        self.lost = False
        self.supervisor.reset()
        timeout = float(self.limits.timeoutS if timeoutS is None else timeoutS)
        if self.relock is not None:
            # Already relocking: extend, never restart (the excursion budget
            # is tracked in offsetFromGoodUm either way).
            self.relock['deadline'] = max(self.relock['deadline'], now + timeout)
            return
        self.relock = {
            'settleUntil': now + max(0.0, float(settleS)),
            'deadline': now + max(0.0, float(settleS)) + timeout,
            'waitFrames': 0, 'good': 0,
            'searchIndex': 0, 'steps': 0, 'clamped': False,
        }

    def relockFrame(self, features, signal, now) -> RelockDecision:
        r = self.relock
        if r is None:
            return RelockDecision('wait')
        if now < r['settleUntil']:
            return RelockDecision('wait', reason='settling')
        if now > r['deadline']:
            return self._relockFail('relock timed out')
        if r['waitFrames'] > 0:
            r['waitFrames'] -= 1
            return RelockDecision('wait', reason='step settling')
        if self.setPoint is None:
            return self._relockFail('no setpoint to return to')
        enter, exitV = self._verdicts(features, signal, withJump=False)
        self.lastVerdict = exitV
        if enter.skip:
            return RelockDecision('wait', reason='stale frame')
        measured, confidence = float('nan'), float('nan')
        if features is None:
            measured = signal
        else:
            ncc = features.correlation
            if self.reference is not None and math.isfinite(ncc):
                confidence = float(ncc)
                if ncc >= self.limits.minCorrelation and 'signal' not in enter.reasons:
                    measured = features.correlationPosition
            elif 'signal' not in enter.reasons and not enter.reasons:
                measured, confidence = features.centroid, 1.0
        if not math.isfinite(measured):
            r['good'] = 0
            if self.limits.searchEnabled and self.pxPerUm is not None:
                return self._searchStep(r)
            return RelockDecision('wait', reason='spot not found')
        error = self.setPoint - measured
        plan = plan_relock_step(error, self.pxPerUm, self.offsetFromGoodUm, confidence,
                                self.limits)
        if plan.done:
            if exitV.trusted or features is None:
                r['good'] += 1
                if r['good'] >= max(1, int(self.supervisorSettings.goodFramesToResume)):
                    self.relock = None
                    self.mode = LOCKED
                    self.offsetFromGoodUm = 0.0      # Z is good again
                    self.lastRelockResult = 'locked'
                    return RelockDecision('success', reason='within %.2f px' % abs(error))
            else:
                r['good'] = 0
            return RelockDecision('wait', reason='confirming (%s)' % exitV.reasonText)
        r['good'] = 0
        r['clamped'] = r['clamped'] or plan.clamped
        if plan.stepUm == 0.0:
            if plan.reason in ('excursion limit reached', 'no valid calibration'):
                return self._relockFail(plan.reason)
            return RelockDecision('wait', reason=plan.reason)
        return RelockDecision('move', plan.stepUm, plan.reason + (' (clamped)' if plan.clamped else ''))

    def _searchStep(self, r) -> RelockDecision:
        offsets = search_offsets(self.limits)
        while r['searchIndex'] < len(offsets):
            target = offsets[r['searchIndex']]
            remaining = target - self.offsetFromGoodUm
            if abs(remaining) < 1e-9:
                r['searchIndex'] += 1
                continue
            step = max(-self.limits.maxStepUm, min(self.limits.maxStepUm, remaining))
            if abs(self.offsetFromGoodUm + step) > self.limits.maxExcursionUm + 1e-9:
                r['searchIndex'] += 1
                continue
            return RelockDecision('move', step, 'search %.2f µm' % target)
        return self._relockFail('spot not found within the search range')

    def noteRelockMove(self, stepUm: float):
        r = self.relock
        self.offsetFromGoodUm += float(stepUm)
        if r is not None:
            r['waitFrames'] = max(0, int(self.limits.settleFrames))
            r['steps'] += 1
        self.lastAppliedUm += float(stepUm)

    def relockExcursion(self) -> float:
        return float(self.offsetFromGoodUm)

    def noteRestored(self, stepUm: float):
        """The controller executed the restore move a failed relock asked for."""
        self.offsetFromGoodUm += float(stepUm)

    def _relockFail(self, reason) -> RelockDecision:
        restore = -self.offsetFromGoodUm
        if abs(restore) > self.limits.maxExcursionUm + 1e-9:
            restore = 0.0     # cannot happen by construction; never move further then
        self.lastRelockResult = 'failed: ' + reason
        self.markLost('relock failed: ' + reason)
        return RelockDecision('fail', restore, reason)

    def abortRelock(self, reason='cancelled'):
        """Stop/cancel: Z stays where it is, the lock is lost (setpoint kept)."""
        if self.mode == RELOCKING or self.relock is not None:
            self.lastRelockResult = 'failed: ' + reason
            self.markLost('relock ' + reason)

    # -- reporting ---------------------------------------------------------
    def publish(self, state: str, now: Optional[float] = None) -> dict:
        v = self.lastVerdict
        snap = {
            'state': state,
            'confidence': None if v is None or not math.isfinite(v.confidence) else round(v.confidence, 4),
            'reasons': list(v.reasons) if v is not None else [],
            'metrics': {k: (float(x) if isinstance(x, (int, float, np.floating)) else x)
                        for k, x in (v.metrics.items() if v is not None else ())},
            'heldFor': list(self.supervisor.lastReasons),
            'setPoint': self.setPoint,
            'lost': self.lost,
            'lostReason': self.lostReason,
            'hasReference': self.reference is not None,
            'calibrated': self.pxPerUm is not None,
            'thresholdsLearned': bool(self.thresholds.learned),
            'thresholds': self.thresholds.to_dict(),
            'lastRelockResult': self.lastRelockResult,
            'relockExcursionUm': self.relockExcursion(),
            'time': time.time() if now is None else now,
        }
        self._snapshot = snap
        return snap

    def statusSnapshot(self) -> dict:
        return dict(self._snapshot)

    # -- persistence -------------------------------------------------------
    def toState(self, extra: Optional[dict] = None) -> dict:
        state = {
            'setPoint': self.setPoint,
            'reference': self.reference.to_dict() if self.reference is not None else None,
            'pxPerUm': self.pxPerUm,
            'limits': dataclasses.asdict(self.limits),
            'hold': dataclasses.asdict(self.supervisorSettings),
        }
        state.update(extra or {})
        return state

    def applyStoredSettings(self, state: dict):
        """Limits and hold settings from a stored state (validated)."""
        self.limits = limits_from_dict(state.get('limits'), self.limits)
        hold = state.get('hold') or {}
        n = int(hold.get('goodFramesToResume', self.supervisorSettings.goodFramesToResume))
        t = float(hold.get('holdTimeoutS', self.supervisorSettings.holdTimeoutS))
        self.setHoldSettings(n, t)

    def applyStoredLock(self, state: dict):
        """Setpoint, reference and calibration for an explicit re-engage."""
        sp = state.get('setPoint')
        if sp is None or not math.isfinite(float(sp)):
            raise ValueError('no stored setpoint')
        ref = state.get('reference')
        px = state.get('pxPerUm')
        if px is None or not math.isfinite(float(px)) or abs(float(px)) < 1e-6:
            raise ValueError('no valid stored calibration; calibrate before re-engaging')
        self.reference = SpotReference.from_dict(ref) if ref else None
        self.pxPerUm = float(px)
        self.setPoint = float(sp)

    def setHoldSettings(self, goodFrames: int, timeoutS: float):
        goodFrames, timeoutS = int(goodFrames), float(timeoutS)
        if goodFrames < 1 or goodFrames > 1000:
            raise ValueError('good frames to resume must be 1..1000')
        if not math.isfinite(timeoutS) or timeoutS <= 0 or timeoutS > 600:
            raise ValueError('hold timeout must be in (0, 600] s')
        self.supervisorSettings.goodFramesToResume = goodFrames
        self.supervisorSettings.holdTimeoutS = timeoutS
