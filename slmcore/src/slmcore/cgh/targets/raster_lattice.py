"""Exact integer-grid realization of regular raster CGH lattices."""

from __future__ import annotations

from bisect import bisect_left,bisect_right
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from functools import cached_property
import math
from typing import Any,Mapping,Sequence

import numpy as np

from ...engine.parameters import EditorKind,ParamDisplayLevel,ParamSpec
from ..coordinates import k_to_reference_px,reference_px_to_k
from .lattice import (
    LatticeAxisIntent,LatticeDefinition,LatticeResolutionIntent,
    fov_from_n_foci,lattice_param_specs,validate_shape,
)


class RasterResolutionPriority(str,Enum):
    """Primary quantity preserved when raster realization requires compromise."""

    PERIOD = "period"
    FOCI_COUNT = "foci_count"
    FOV = "fov"


RASTER_RESOLUTION_PARAMS = {
    "resolution_priority":ParamSpec(
        RasterResolutionPriority.PERIOD.value,
        str,
        choices=tuple(
            priority.value for priority in RasterResolutionPriority
        ),
        display_level=ParamDisplayLevel.ADVANCED,
        tooltip=(
            "Determines which requested quantity is prioritized when an exact "
            "raster representation is not possible: period, foci count or FOV."
        )
    ),
    "min_foci_delta":ParamSpec(
        -1,int,max_value=0,step=1,editor=EditorKind.SPIN_BOX,
        display_level=ParamDisplayLevel.ADVANCED,
        layout_group="foci_delta",
        tooltip=(
            "Minimum allowed change in the number of foci when finding"
            " a raster-compatible target."
        )
    ),
    "max_foci_delta":ParamSpec(
        1,int,min_value=0,step=1,editor=EditorKind.SPIN_BOX,
        display_level=ParamDisplayLevel.ADVANCED,
        layout_group="foci_delta",
        tooltip=(
            "Maximum allowed change in the number of foci when finding"
            " a raster-compatible target."
        )
    ),
}


def raster_lattice_param_specs(
    modifiers: Sequence[str]=(),
) -> dict[str, ParamSpec]:
    """Return semantic lattice specs plus raster-resolution policy options."""
    specs = lattice_param_specs(modifiers)
    specs.update(RASTER_RESOLUTION_PARAMS)
    return specs


@dataclass(frozen=True)
class RasterResolutionPolicy:
    """User-selected compromise between period and number of foci."""

    priority: RasterResolutionPriority
    min_foci_delta: int
    max_foci_delta: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"priority",RasterResolutionPriority(self.priority),
        )
        if self.min_foci_delta > self.max_foci_delta:
            raise ValueError("min_foci_delta cannot exceed max_foci_delta")
        if self.min_foci_delta > 0 or self.max_foci_delta < 0:
            raise ValueError("The allowed foci-delta range must include zero")

    @classmethod
    def from_params(cls,params: Mapping[str,Any]) -> "RasterResolutionPolicy":
        return cls(
            priority=RasterResolutionPriority(params["resolution_priority"]),
            min_foci_delta=int(params["min_foci_delta"]),
            max_foci_delta=int(params["max_foci_delta"]),
        )

    @staticmethod
    def _error_map(candidate: "_AxisCandidate") -> dict[RasterResolutionPriority, float]:
        return {
            RasterResolutionPriority.PERIOD:candidate.period_error_k,
            RasterResolutionPriority.FOCI_COUNT:float(abs(candidate.foci_delta)),
            RasterResolutionPriority.FOV:candidate.fov_error_px,
        }

    def scientific_score(
        self,candidate: "_AxisCandidate",intent: LatticeAxisIntent,
    ) -> tuple[Any, ...]:
        """Rank scientific compromises lexicographically."""
        errors = self._error_map(candidate)
        semantic = {
            "period":RasterResolutionPriority.PERIOD,
            "n_foci":RasterResolutionPriority.FOCI_COUNT,
            "fov":RasterResolutionPriority.FOV,
        }
        order = [self.priority]
        for keys in (intent.explicit,intent.persistent):
            for key in ("period","n_foci","fov"):
                quantity = semantic[key]
                if key in keys and quantity not in order:
                    order.append(quantity)

        # Preserve the historical Period/Foci behavior when FOV is neither
        # selected nor an explicit/persistent intent. FOV then does not become
        # a new hidden tie-breaker.
        fallback = (
            (RasterResolutionPriority.FOCI_COUNT,)
            if self.priority is RasterResolutionPriority.PERIOD else
            (RasterResolutionPriority.PERIOD,)
            if self.priority is RasterResolutionPriority.FOCI_COUNT else
            (RasterResolutionPriority.PERIOD,RasterResolutionPriority.FOCI_COUNT)
        )
        for quantity in fallback:
            if quantity not in order:
                order.append(quantity)
        return tuple(errors[quantity] for quantity in order)



@dataclass(frozen=True)
class RasterAxisGrid:
    """Final exact integer-grid representation of one lattice axis."""

    target_size: int
    raster_spacing: int
    n_foci: int
    first_pixel: int
    stagger_shift: int = 0

    @property
    def period_k(self) -> float:
        return float(self.raster_spacing) / float(self.target_size)

    @property
    def period_px(self) -> float:
        return k_to_reference_px(self.period_k)


@dataclass(frozen=True)
class _AxisCandidate:
    grid: RasterAxisGrid
    foci_delta: int
    period_error_k: float
    fov_error_px: float = 0.0


@dataclass(frozen=True)
class _RasterPairCandidate:
    x: _AxisCandidate
    y: _AxisCandidate


@dataclass(frozen=True)
class ResolvedRasterLattice:
    """Complete canonical lattice and its exact internal integer realization."""

    lattice: LatticeDefinition
    section_shape: tuple[int, int]
    x_grid: RasterAxisGrid
    y_grid: RasterAxisGrid

    def __post_init__(self) -> None:
        section_shape = validate_shape(self.section_shape)
        object.__setattr__(self,"section_shape",section_shape)

        if self.x_grid.target_size > section_shape[1]:
            raise ValueError("X target grid exceeds the section width")
        if self.y_grid.target_size > section_shape[0]:
            raise ValueError("Y target grid exceeds the section height")
        if self.x_grid.n_foci != self.lattice.n_foci_x:
            raise ValueError("X grid and canonical X number of foci disagree")
        if self.y_grid.n_foci != self.lattice.n_foci_y:
            raise ValueError("Y grid and canonical Y number of foci disagree")
        if not np.isclose(
            self.x_grid.period_px,self.lattice.period_x_px,
            rtol=0.0,atol=1e-12,
        ):
            raise ValueError("X grid and canonical X period disagree")
        if not np.isclose(
            self.y_grid.period_px,self.lattice.period_y_px,
            rtol=0.0,atol=1e-12,
        ):
            raise ValueError("Y grid and canonical Y period disagree")
        if not np.isclose(self.lattice.rotation_deg,0.0,atol=1e-12):
            raise ValueError("Raster lattice does not support rotation")
        if not np.isclose(self.lattice.skew_deg,0.0,atol=1e-12):
            raise ValueError("Raster lattice does not support skew")

        alignment_error = float(np.max(np.abs(
            self.spot_positions_kxy - self.lattice.spot_positions_kxy()
        )))
        if alignment_error > 1e-12:
            raise RuntimeError(
                "Canonical raster geometry and exact pixel layout disagree by "
                f"{alignment_error:.3g} kxy"
            )

    @property
    def target_shape(self) -> tuple[int, int]:
        return self.y_grid.target_size,self.x_grid.target_size

    @cached_property
    def lattice_indices(self) -> np.ndarray:
        return _freeze_array(self.lattice.lattice_indices())

    @cached_property
    def x_pixels(self) -> np.ndarray:
        indices = self.lattice_indices
        pixels = (
            self.x_grid.first_pixel
            + indices[0] * self.x_grid.raster_spacing
            + (indices[1] % 2) * self.x_grid.stagger_shift
        ).astype(np.int64)
        return _freeze_array(pixels)

    @cached_property
    def y_pixels(self) -> np.ndarray:
        indices = self.lattice_indices
        pixels = (
            self.y_grid.first_pixel
            + indices[1] * self.y_grid.raster_spacing
        ).astype(np.int64)
        return _freeze_array(pixels)

    @cached_property
    def spot_positions_kxy(self) -> np.ndarray:
        height,width = self.target_shape
        positions = np.vstack([
            (self.x_pixels.astype(np.float64) - width / 2.0) / width,
            (self.y_pixels.astype(np.float64) - height / 2.0) / height,
        ])
        return _freeze_array(positions)

    def render_internal(
        self,intensities: np.ndarray,normalize: bool=True,
    ) -> np.ndarray:
        """Render relative intensities on the exact internal integer grid."""
        intensities = np.asarray(intensities,dtype=np.float64)
        if intensities.shape != (self.lattice.n_spots,):
            raise ValueError(
                f"intensities must have shape ({self.lattice.n_spots},), got "
                f"{intensities.shape}"
            )
        if not np.all(np.isfinite(intensities)):
            raise ValueError("intensities contain non-finite values")
        if np.any(intensities < 0):
            raise ValueError("intensities cannot contain negative values")

        target = np.zeros(self.target_shape,dtype=np.float64)
        np.maximum.at(target,(self.y_pixels,self.x_pixels),intensities)
        if normalize and target.size and np.max(target) > 0:
            target /= np.max(target)
        return target

    def details(self) -> dict[str, Any]:
        return {
            "target_shape":self.target_shape,
            "raster_spacing_x":self.x_grid.raster_spacing,
            "raster_spacing_y":self.y_grid.raster_spacing,
            "first_pixel_x":self.x_grid.first_pixel,
            "first_pixel_y":self.y_grid.first_pixel,
            "stagger_shift_x":self.x_grid.stagger_shift,
            "preview_space":"internal_target",
        }


def validate_raster_resolution_params(params: Mapping[str,Any]) -> None:
    """Validate raster policy values independently of section context."""
    RasterResolutionPolicy.from_params(params)


def resolve_raster_lattice(
    lattice: LatticeDefinition,
    section_shape: tuple[int, int],
    policy: RasterResolutionPolicy,
    intent: LatticeResolutionIntent | None=None,
) -> ResolvedRasterLattice:
    """Choose one exact raster-compatible canonical lattice."""
    section_height,section_width = validate_shape(section_shape)
    if intent is None:
        intent = LatticeResolutionIntent(
            x=LatticeAxisIntent(
                lattice.period_x_px,lattice.n_foci_x,lattice.fov_x_px,
            ),
            y=LatticeAxisIntent(
                lattice.period_y_px,lattice.n_foci_y,lattice.fov_y_px,
            ),
        )

    y_candidates = _best_scientific_candidates(
        _axis_candidates(
            period_px=lattice.period_y_px,
            section_size=section_height,
            n_foci=lattice.n_foci_y,
            stagger=0.0,
            policy=policy,
            intent=intent.y,
        ),
        policy,intent.y,
    )

    pairs = []
    for y_candidate in y_candidates:
        x_stagger = (
            lattice.stagger if y_candidate.grid.n_foci > 1 else 0.0
        )
        try:
            x_candidates = _best_scientific_candidates(
                _axis_candidates(
                    period_px=lattice.period_x_px,
                    section_size=section_width,
                    n_foci=lattice.n_foci_x,
                    stagger=x_stagger,
                    policy=policy,
                    intent=intent.x,
                ),
                policy,intent.x,
            )
        except ValueError:
            continue
        pairs.extend(
            _RasterPairCandidate(x=x_candidate,y=y_candidate)
            for x_candidate in x_candidates
        )

    if not pairs:
        raise ValueError(
            "No exact uniform raster target fits the requested 2-D lattice "
            f"inside section shape {(section_height,section_width)}"
        )

    # A Y compromise can affect whether X staggering is required.  Preserve
    # the best X scientific compromise across all scientifically tied Y
    # candidates before using raster scale as a 2-D tie-break.
    best_x_score = min(
        policy.scientific_score(pair.x,intent.x) for pair in pairs
    )
    pairs = [
        pair for pair in pairs
        if policy.scientific_score(pair.x,intent.x) == best_x_score
    ]

    x_grid,y_grid = _select_common_scale_pair(pairs)

    canonical = lattice.with_geometry(
        period_x_px=x_grid.period_px,
        period_y_px=y_grid.period_px,
        n_foci_x=x_grid.n_foci,
        n_foci_y=y_grid.n_foci,
    )
    return ResolvedRasterLattice(
        lattice=canonical,
        section_shape=(section_height,section_width),
        x_grid=x_grid,
        y_grid=y_grid,
    )

def materialize_exact_raster_lattice(
    lattice: LatticeDefinition,
    section_shape,
) -> ResolvedRasterLattice:
    """Materialize a fixed lattice exactly, without optimization/search."""

    section_height,section_width = validate_shape(section_shape)

    y_grids = _exact_raster_axis_grids(
        period_px=lattice.period_y_px,
        section_size=section_height,
        n_foci=lattice.n_foci_y,
        stagger=0.0,
    )
    x_grids = _exact_raster_axis_grids(
        period_px=lattice.period_x_px,
        section_size=section_width,
        n_foci=lattice.n_foci_x,
        stagger=(lattice.stagger if lattice.n_foci_y > 1 else 0.0),
    )

    pairs = [
        _RasterPairCandidate(
            x=_AxisCandidate(x_grid,0,0.0),
            y=_AxisCandidate(y_grid,0,0.0),
        )
        for y_grid in y_grids
        for x_grid in x_grids
    ]
    x_grid,y_grid = _select_common_scale_pair(pairs)

    return ResolvedRasterLattice(
        lattice=lattice,
        section_shape=section_shape,
        x_grid=x_grid,
        y_grid=y_grid,
    )

def _exact_raster_axis_grids(
    period_px: float,
    section_size: int,
    n_foci: int,
    stagger: float=0.0,
) -> tuple[RasterAxisGrid, ...]:
    """Return every safe grid representing one fixed raster axis exactly."""
    requested_k = reference_px_to_k(period_px)
    section_size = int(section_size)
    n_foci = int(n_foci)
    stagger = float(stagger)

    if requested_k <= 0:
        raise ValueError("period_px must resolve to a positive frequency")
    if section_size <= 0:
        raise ValueError("section_size must be > 0")
    if n_foci <= 0:
        raise ValueError("n_foci must be > 0")
    if not 0.0 <= stagger <= 1.0:
        raise ValueError("stagger must be in [0, 1]")

    stagger_fraction = Fraction(str(stagger)).limit_denominator(
        section_size
    )
    if not np.isclose(
        float(stagger_fraction),
        stagger,
        rtol=0.0,
        atol=1e-12,
    ):
        raise ValueError(
            "stagger cannot be represented exactly on the available raster"
        )

    # ``period_px`` is expected to come from an already-resolved raster
    # axis, so its normalized frequency is an exact rational number.
    ratio = Fraction(requested_k).limit_denominator(1_000_000)

    if not np.isclose(
        float(ratio),
        requested_k,
        rtol=0.0,
        atol=1e-12,
    ):
        raise ValueError(
            f"Period {period_px} cannot be recovered as an exact "
            "raster ratio"
        )

    grids = _equivalent_grids(
        ratio,
        section_size,
        n_foci,
        stagger_fraction,
    )

    if not grids:
        raise ValueError(
            f"Period {period_px} with {n_foci} foci cannot be "
            f"represented exactly inside raster size {section_size}"
        )

    return grids



def _axis_candidates(
    period_px: float,
    section_size: int,
    n_foci: int,
    stagger: float,
    policy: RasterResolutionPolicy,
    intent: LatticeAxisIntent | None=None,
) -> tuple[_AxisCandidate, ...]:
    """Return safe candidates without making a raster-size choice."""
    requested_k = reference_px_to_k(period_px)
    requested_n = int(n_foci)
    if intent is None:
        intent = LatticeAxisIntent(
            period_px=float(period_px),
            n_foci=requested_n,
            fov_px=fov_from_n_foci(requested_n,period_px),
        )
    section_size = int(section_size)
    stagger = float(stagger)

    if requested_k <= 0:
        raise ValueError("period_px must resolve to a positive frequency")
    if section_size <= 0:
        raise ValueError("section_size must be > 0")
    if requested_n <= 0:
        raise ValueError("n_foci must be > 0")
    if not 0.0 <= stagger <= 1.0:
        raise ValueError("stagger must be in [0, 1]")

    stagger_fraction = Fraction(str(stagger)).limit_denominator(section_size)
    if not np.isclose(
        float(stagger_fraction),stagger,rtol=0.0,atol=1e-12,
    ):
        raise ValueError(
            "stagger cannot be represented exactly on the available raster"
        )

    fov_relevant = (
        policy.priority is RasterResolutionPriority.FOV
        or "fov" in intent.explicit
        or "fov" in intent.persistent
    )
    preferred_ratio = _preferred_ratio(
        requested_k,section_size,requested_n,stagger,
    )
    if preferred_ratio is not None:
        equivalent = _equivalent_grids(
            preferred_ratio,section_size,requested_n,stagger_fraction,
        )
        if equivalent:
            error = abs(float(preferred_ratio) - requested_k)
            preferred = tuple(
                _AxisCandidate(
                    grid=grid,
                    foci_delta=0,
                    period_error_k=error,
                    fov_error_px=abs(
                        fov_from_n_foci(grid.n_foci,grid.period_px)
                        - float(intent.fov_px)
                    ),
                )
                for grid in equivalent
            )
            # The historical fast path remains exact when FOV is irrelevant.
            # If FOV participates in scoring, it is also safe only when the
            # exact period/count realization already has zero FOV error.
            if (
                not fov_relevant
                or all(item.fov_error_px <= 1e-12 for item in preferred)
            ):
                return preferred

    spacing_lists = _valid_spacing_lists(section_size,stagger_fraction)
    minimum_delta = max(policy.min_foci_delta,1 - requested_n)
    maximum_delta = min(policy.max_foci_delta,section_size - requested_n)
    candidates = []

    for foci_delta in range(minimum_delta,maximum_delta + 1):
        effective_n = requested_n + foci_delta
        for target_size in range(1,section_size + 1):
            spacings = _center_compatible_spacings(
                spacing_lists,target_size,effective_n,
            )
            if not spacings:
                continue

            span_factor = (effective_n - 1) + 2.0 * stagger
            maximum_spacing = (
                section_size
                if span_factor <= 0
                else int(math.floor((target_size - 2.0) / span_factor + 1e-12))
            )
            upper = bisect_right(spacings,maximum_spacing)
            if upper <= 0:
                continue

            ideal_spacings = [requested_k * target_size]
            if fov_relevant and effective_n > 1:
                requested_fov_period = float(intent.fov_px) / float(effective_n - 1)
                ideal_spacings.append(
                    reference_px_to_k(requested_fov_period) * target_size
                )

            candidate_indices = set()
            for ideal_spacing in ideal_spacings:
                insertion = bisect_left(spacings,ideal_spacing,0,upper)
                candidate_indices.update((insertion - 1,insertion))
            for index in candidate_indices:
                if index < 0 or index >= upper:
                    continue
                grid = _try_axis_grid(
                    target_size,spacings[index],effective_n,stagger_fraction,
                )
                if grid is None:
                    continue
                candidates.append(_AxisCandidate(
                    grid=grid,
                    foci_delta=foci_delta,
                    period_error_k=abs(grid.period_k - requested_k),
                    fov_error_px=abs(
                        fov_from_n_foci(grid.n_foci,grid.period_px)
                        - float(intent.fov_px)
                    ),
                ))

    if not candidates:
        raise ValueError(
            "No exact uniform raster target fits the requested lattice inside "
            f"section size {section_size} and foci-delta range "
            f"[{policy.min_foci_delta}, {policy.max_foci_delta}]"
        )
    return tuple(candidates)


def _best_scientific_candidates(
    candidates: Sequence[_AxisCandidate],
    policy: RasterResolutionPolicy,
    intent: LatticeAxisIntent,
) -> tuple[_AxisCandidate, ...]:
    """Keep all candidates tied at the best period/foci compromise."""
    if not candidates:
        raise ValueError("Raster-axis candidate collection cannot be empty")
    best_score = min(
        policy.scientific_score(item,intent) for item in candidates
    )
    return tuple(
        item for item in candidates
        if policy.scientific_score(item,intent) == best_score
    )


def _select_common_scale_pair(
    pairs: Sequence[_RasterPairCandidate],
) -> tuple[RasterAxisGrid, RasterAxisGrid]:
    """Choose a coupled X/Y raster scale from scientifically tied pairs.

    The more constrained axis defines the largest common feasible scale.
    Both axes then stay as close to that scale as their exact integer-grid
    representations permit.  Section aspect ratio therefore remains only a
    hard size limit, not a preference for the target raster aspect ratio.
    """
    if not pairs:
        raise ValueError("Raster pair candidate collection cannot be empty")

    largest_x = max(pair.x.grid.target_size for pair in pairs)
    largest_y = max(pair.y.grid.target_size for pair in pairs)
    common_size = min(largest_x,largest_y)

    def score(pair: _RasterPairCandidate) -> tuple[Any, ...]:
        x_size = pair.x.grid.target_size
        y_size = pair.y.grid.target_size
        x_distance = abs(x_size - common_size)
        y_distance = abs(y_size - common_size)
        x_positive_delta = 0 if pair.x.foci_delta <= 0 else 1
        y_positive_delta = 0 if pair.y.foci_delta <= 0 else 1
        return (
            x_distance + y_distance,
            abs(x_size - y_size),
            -(x_size + y_size),
            x_positive_delta + y_positive_delta,
            x_positive_delta,
            y_positive_delta,
            pair.x.grid.raster_spacing,
            pair.y.grid.raster_spacing,
        )

    best = min(pairs,key=score)
    return best.x.grid,best.y.grid


def _preferred_ratio(
    requested_k: float,section_size: int,n_foci: int,stagger: float,
) -> Fraction | None:
    """Return the rational period selected by the previous rounded-fit intent."""
    best_ratio = None
    best_error = None
    best_size = -1
    tolerance = 1e-15

    for target_size in range(1,section_size + 1):
        ideal_spacing = requested_k * target_size
        for spacing in {int(math.floor(ideal_spacing)),int(math.ceil(ideal_spacing))}:
            if spacing <= 0:
                continue
            half_span = (n_foci - 1) * spacing / 2.0
            center = target_size / 2.0
            minimum = center - half_span
            maximum = center + half_span + stagger * spacing
            if round(minimum) < 0 or round(maximum) >= target_size:
                continue

            error = abs(float(spacing) / target_size - requested_k)
            if (
                best_error is None
                or error < best_error - tolerance
                or (abs(error - best_error) <= tolerance and target_size > best_size)
            ):
                best_ratio = Fraction(spacing,target_size)
                best_error = error
                best_size = target_size

    return best_ratio


def _equivalent_grids(
    ratio: Fraction,section_size: int,n_foci: int,stagger: Fraction,
) -> tuple[RasterAxisGrid, ...]:
    """Return every safe grid representing one rational period exactly."""
    grids = []
    for multiplier in range(1,section_size // ratio.denominator + 1):
        grid = _try_axis_grid(
            ratio.denominator * multiplier,
            ratio.numerator * multiplier,
            n_foci,
            stagger,
        )
        if grid is not None:
            grids.append(grid)
    return tuple(grids)


def _valid_spacing_lists(section_size: int,stagger: Fraction):
    """Return stagger-compatible spacings grouped by parity."""
    fraction = stagger
    all_spacings = []
    even_spacings = []
    odd_spacings = []

    for spacing in range(1,section_size + 1):
        if (fraction * spacing).denominator != 1:
            continue
        all_spacings.append(spacing)
        (even_spacings if spacing % 2 == 0 else odd_spacings).append(spacing)

    return tuple(all_spacings),tuple(even_spacings),tuple(odd_spacings)


def _center_compatible_spacings(spacing_lists,target_size,n_foci):
    """Return exact spacings compatible with a centered first pixel."""
    all_spacings,even_spacings,odd_spacings = spacing_lists
    if (n_foci - 1) % 2 == 0:
        return all_spacings if target_size % 2 == 0 else ()
    return even_spacings if target_size % 2 == 0 else odd_spacings


def _try_axis_grid(
    target_size: int,raster_spacing: int,n_foci: int,stagger: Fraction,
) -> RasterAxisGrid | None:
    """Return an exact safe grid, or ``None`` when one invariant fails."""
    centered_numerator = target_size - (n_foci - 1) * raster_spacing
    if centered_numerator % 2:
        return None

    first_pixel = centered_numerator // 2
    shift = stagger * raster_spacing
    if shift.denominator != 1:
        return None
    stagger_shift = int(shift)

    last_pixel = (
        first_pixel + (n_foci - 1) * raster_spacing + stagger_shift
    )
    if first_pixel < 0 or last_pixel >= target_size:
        return None

    return RasterAxisGrid(
        target_size=target_size,
        raster_spacing=raster_spacing,
        n_foci=n_foci,
        first_pixel=first_pixel,
        stagger_shift=stagger_shift,
    )


def _freeze_array(value):
    array = np.array(value,copy=True)
    array.setflags(write=False)
    return array
