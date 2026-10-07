"""Polarisation maths shared by acquisition mocks, live display and analysis.

Conventions (stated once, used everywhere):

- azimuth ψ ∈ [−π/2, π/2] and ellipticity angle χ ∈ [−π/4, π/4], in radians;
- normalised Stokes direction of the polarised part
  ``s = (cos2χ·cos2ψ, cos2χ·sin2ψ, sin2χ)`` on the Poincaré sphere;
- full Stokes vector ``S = P·(1, d·s)`` with power ``P`` and degree of
  polarisation ``d``;
- handedness: ``s3 > 0`` is called **right circular** by default
  (``RCP_SIGN = +1``). Whether that matches the instrument's own convention
  must be verified on the rig with a known quarter-wave plate
  (``docs/design/plans/transient-instruments-step-scans.md`` §9.3); the
  convention is a parameter, never implicit.

Angles are never averaged arithmetically: ψ = −89° and ψ = +89° describe
nearly the same orientation. Points are aggregated as Stokes vectors.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

#: Sign of s3 that is called right circular.
RCP_SIGN = 1.0


# ----------------------------------------------------------- conversions
def direction_from_angles(azimuth, ellipticity) -> np.ndarray:
    """(…, 3) unit Stokes direction from azimuth and ellipticity (rad)."""
    psi = np.asarray(azimuth, dtype=float)
    chi = np.asarray(ellipticity, dtype=float)
    c2chi = np.cos(2 * chi)
    return np.stack(
        [c2chi * np.cos(2 * psi), c2chi * np.sin(2 * psi), np.sin(2 * chi)],
        axis=-1,
    )


def angles_from_direction(direction) -> Tuple[np.ndarray, np.ndarray]:
    """Azimuth and ellipticity (rad) of a (…, 3) Stokes direction."""
    s = np.asarray(direction, dtype=float)
    norm = np.linalg.norm(s, axis=-1)
    with np.errstate(invalid='ignore', divide='ignore'):
        unit = s / norm[..., None]
    chi = 0.5 * np.arcsin(np.clip(unit[..., 2], -1.0, 1.0))
    psi = 0.5 * np.arctan2(unit[..., 1], unit[..., 0])
    return psi, chi


def stokes_from_samples(azimuth, ellipticity, dop=None, power=None) -> np.ndarray:
    """(N, 4) Stokes vectors; unit power / full polarisation if absent."""
    direction = direction_from_angles(azimuth, ellipticity)
    n = direction.shape[0]
    d = np.ones(n) if dop is None else np.asarray(dop, dtype=float)
    p = np.ones(n) if power is None else np.asarray(power, dtype=float)
    return np.column_stack([p, (p * d)[:, None] * direction])


def angular_distance_deg(a, b) -> np.ndarray:
    """Angle on the Poincaré sphere between unit directions, in degrees."""
    dot = np.sum(np.asarray(a, float) * np.asarray(b, float), axis=-1)
    return np.degrees(np.arccos(np.clip(dot, -1.0, 1.0)))


# -------------------------------------------------------------- Mueller model
def retarder_mueller(retardance: float, fast_axis: float) -> np.ndarray:
    """Mueller matrix of a linear retarder (radians)."""
    c, s = math.cos(2 * fast_axis), math.sin(2 * fast_axis)
    cd, sd = math.cos(retardance), math.sin(retardance)
    return np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, c * c + s * s * cd, c * s * (1 - cd), -s * sd],
        [0.0, c * s * (1 - cd), s * s + c * c * cd, c * sd],
        [0.0, s * sd, -c * sd, cd],
    ])


@dataclass(frozen=True)
class TwoPlateModel:
    """Input state → plate 1 → plate 2. Angles of the plates in degrees.

    ``offset*_deg`` are the fast-axis angles at a rotator reading of 0°.
    """

    retardance1: float = math.pi / 2       # quarter-wave
    retardance2: float = math.pi            # half-wave
    offset1_deg: float = 0.0
    offset2_deg: float = 0.0
    input_stokes: Tuple[float, float, float, float] = (1.0, 1.0, 0.0, 0.0)

    def output(self, angle1_deg: float, angle2_deg: float) -> np.ndarray:
        m1 = retarder_mueller(self.retardance1, math.radians(angle1_deg + self.offset1_deg))
        m2 = retarder_mueller(self.retardance2, math.radians(angle2_deg + self.offset2_deg))
        return m2 @ m1 @ np.asarray(self.input_stokes, dtype=float)


# ---------------------------------------------------------------- aggregation
@dataclass(frozen=True)
class PolarisationAggregate:
    """One point's polarisation, from its raw paired samples."""

    n: int
    #: False when the mean polarised vector has no direction (zero power or
    #: a vanishing mean vector): such a point is never matched.
    defined: bool
    direction: np.ndarray        # unit (3,), NaN if undefined
    azimuth: float               # rad, from the mean direction
    ellipticity: float           # rad
    #: Mean of the instrument-reported DOP of each sample (light within a read).
    dop_instrument: float
    #: |mean polarised Stokes| / mean S0 — also falls when the state wanders.
    dop_aggregate: float
    #: RMS angle (deg) of the samples' directions from the mean direction.
    dispersion_deg: float
    power_mean: float
    power_std: float


def aggregate_polarisation(azimuth, ellipticity, dop=None, power=None) -> PolarisationAggregate:
    psi = np.asarray(azimuth, dtype=float)
    n = int(psi.size)
    nan3 = np.full(3, np.nan)
    if n == 0:
        return PolarisationAggregate(0, False, nan3, math.nan, math.nan,
                                     math.nan, math.nan, math.nan, math.nan, math.nan)
    stokes = stokes_from_samples(azimuth, ellipticity, dop, power)
    s0 = float(np.mean(stokes[:, 0]))
    pol = np.mean(stokes[:, 1:], axis=0)
    pol_norm = float(np.linalg.norm(pol))
    dop_instrument = float(np.mean(dop)) if dop is not None else math.nan
    power_values = np.asarray(power, dtype=float) if power is not None else None
    power_mean = float(np.mean(power_values)) if power_values is not None else math.nan
    power_std = float(np.std(power_values, ddof=1)) if power_values is not None and n > 1 else (
        0.0 if power_values is not None else math.nan)
    if not (s0 > 0.0) or not (pol_norm > 1e-12):
        return PolarisationAggregate(n, False, nan3, math.nan, math.nan, dop_instrument,
                                     0.0 if s0 > 0 else math.nan, math.nan,
                                     power_mean, power_std)
    direction = pol / pol_norm
    sample_dirs = direction_from_angles(psi, ellipticity)
    spread = angular_distance_deg(sample_dirs, direction[None, :])
    psi_mean, chi_mean = angles_from_direction(direction)
    return PolarisationAggregate(
        n=n, defined=True, direction=direction,
        azimuth=float(psi_mean), ellipticity=float(chi_mean),
        dop_instrument=dop_instrument, dop_aggregate=pol_norm / s0,
        dispersion_deg=float(np.sqrt(np.mean(spread ** 2))),
        power_mean=power_mean, power_std=power_std,
    )


# ------------------------------------------------------------------ targets
@dataclass(frozen=True)
class Target:
    name: str
    direction: np.ndarray


def default_targets(linear_step_deg: float = 10.0, *, rcp_sign: float = RCP_SIGN) -> List[Target]:
    """Right and left circular, and linear states every ``linear_step_deg``."""
    targets = [
        Target('RCP', np.array([0.0, 0.0, rcp_sign])),
        Target('LCP', np.array([0.0, 0.0, -rcp_sign])),
    ]
    count = int(round(180.0 / linear_step_deg))
    for k in range(count):
        theta = k * linear_step_deg
        rad = math.radians(theta)
        targets.append(Target(
            f'linear {theta:g}°',
            np.array([math.cos(2 * rad), math.sin(2 * rad), 0.0]),
        ))
    return targets


# ----------------------------------------------------------------- matching
PASS = 'pass'
FAILED = 'failed'
UNQUALIFIED = 'unqualified'


@dataclass(frozen=True)
class Match:
    target: str
    #: Index into the candidate points, or ``None`` if no point is eligible.
    index: Optional[int]
    distance_deg: float
    status: str


def match_targets(
    targets: Sequence[Target],
    directions: np.ndarray,
    eligible: np.ndarray,
    verified: np.ndarray,
    threshold_deg: float,
) -> List[Match]:
    """Nearest ELIGIBLE point per target; eligibility is decided first.

    ``eligible`` must already combine committed status, a defined direction
    and the DOP criteria — a closer ineligible point never hides an eligible
    one. A match whose point was acquired with unverified timing is
    ``unqualified``, never ``pass`` or ``failed``.
    """
    directions = np.asarray(directions, dtype=float)
    eligible = np.asarray(eligible, dtype=bool)
    verified = np.asarray(verified, dtype=bool)
    candidates = np.flatnonzero(eligible)
    matches = []
    for target in targets:
        if candidates.size == 0:
            matches.append(Match(target.name, None, math.nan, FAILED))
            continue
        distance = angular_distance_deg(directions[candidates], target.direction[None, :])
        best = int(np.argmin(distance))
        index = int(candidates[best])
        d = float(distance[best])
        if not verified[index]:
            status = UNQUALIFIED
        else:
            status = PASS if d <= threshold_deg else FAILED
        matches.append(Match(target.name, index, d, status))
    return matches
