"""Shared semantic lattice definitions and continuous rasterization helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any,Iterable,Mapping,Sequence

import numpy as np

from ...engine.state import ConfigWarning,StateModel
from ...engine.parameters import (
    EditorKind,
    FourierDisplacementConverter,
    METRIC_UNIT,
    ParamSpec,
    ParamLink,
    SLM_UNIT,
)
from ..coordinates import reference_px_to_k
from ..lattice_geometry import LatticeRepresentation


_PERIOD_STEP_BY_UNIT = {
    SLM_UNIT:0.05,
    METRIC_UNIT:0.005,
}
_PERIOD_DECIMALS_BY_UNIT = {
    SLM_UNIT:2,
    METRIC_UNIT:4,
}
_FOV_STEP_BY_UNIT = {
    SLM_UNIT:1.0,
    METRIC_UNIT:1.0,
}
_FOV_DECIMALS_BY_UNIT = {
    SLM_UNIT:0,
    METRIC_UNIT:1,
}


BASE_LATTICE_PARAMS = {
    "square":ParamSpec(False,bool,label="Square"),
    "square_unit":ParamSpec(
        SLM_UNIT,
        str,
        choices=(SLM_UNIT,METRIC_UNIT),
        hidden=True,
    ),
    "period_x_px":ParamSpec(
        6.0,float,min_value=1e-9,
        converter=FourierDisplacementConverter("x"),
        step_by_unit=_PERIOD_STEP_BY_UNIT,
        decimals_by_unit=_PERIOD_DECIMALS_BY_UNIT,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        converted_label = "Period X (um)",
        layout_group="period",
        links=ParamLink(
            target="period_y_px",
            enabled_by="square",
            unit_by="square_unit",
        )
    ),
    "period_y_px":ParamSpec(
        6.0,float,min_value=1e-9,
        converter=FourierDisplacementConverter("y"),
        step_by_unit=_PERIOD_STEP_BY_UNIT,
        decimals_by_unit=_PERIOD_DECIMALS_BY_UNIT,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        converted_label = "Period Y (um)",
        layout_group="period"
    ),
    "fov_x_px":ParamSpec(
        180.0,float,min_value=0.0,
        converter=FourierDisplacementConverter("x"),
        step_by_unit=_FOV_STEP_BY_UNIT,
        decimals_by_unit=_FOV_DECIMALS_BY_UNIT,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        converted_label = "FOV X (um)",
        layout_group="fov",
        links=ParamLink(
            target="fov_y_px",
            enabled_by="square",
            unit_by="square_unit",
        )
    ),
    "fov_y_px":ParamSpec(
        180.0,float,min_value=0.0,
        converter=FourierDisplacementConverter("y"),
        step_by_unit=_FOV_STEP_BY_UNIT,
        decimals_by_unit=_FOV_DECIMALS_BY_UNIT,
        editor=EditorKind.DOUBLE_SPIN_BOX,
        converted_label = "FOV Y (um)",
        layout_group="fov"
    ),
    "n_foci_x":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        layout_group="n_foci",
        links=ParamLink(target="n_foci_y",enabled_by="square")
    ),
    "n_foci_y":ParamSpec(
        31,int,min_value=1,step=1,editor=EditorKind.SPIN_BOX,
        layout_group="n_foci"
    ),
}



@dataclass
class LatticeLockState(StateModel):
    """Persistent raster-lattice intent across separate parameter patches.

    ``kind`` is deliberately limited to the two dependent quantities whose
    realized values may drift across repeated raster resolutions. ``reference``
    stores the requested X/Y values, independently of the currently realized
    canonical values shown by the parameter form.
    """

    kind: str | None = None
    reference: tuple[float, float] | None = None

    KINDS = ("fov","n_foci")

    def validate(self) -> None:
        if self.kind is None:
            if self.reference is not None:
                raise ValueError("Unlocked lattice state cannot keep a reference")
            return
        if self.kind not in self.KINDS:
            raise ValueError(f"Unknown lattice lock kind {self.kind!r}")
        if self.reference is None or len(self.reference) != 2:
            raise ValueError("Locked lattice state requires an X/Y reference")
        x,y = self.reference
        if self.kind == "fov":
            values = (float(x),float(y))
            if any(value < 0 for value in values):
                raise ValueError("Locked FOV references must be >= 0")
            self.reference = values
        else:
            values = (int(x),int(y))
            if any(value <= 0 for value in values):
                raise ValueError("Locked foci-count references must be > 0")
            self.reference = values

    def set(self,kind: str | None,reference=None) -> None:
        self.kind = None if kind is None else str(kind)
        self.reference = None if reference is None else tuple(reference)
        self.validate()

    def to_dict(self) -> dict[str, Any]:
        if self.kind is None:
            return {"kind":None,"reference":None}
        return {"kind":self.kind,"reference":list(self.reference)}

    def load_dict(
        self,data: Mapping[str,Any],*,warnings: list[ConfigWarning] | None=None,
        path=(),
    ) -> None:
        warnings = [] if warnings is None else warnings
        if not isinstance(data,Mapping):
            warnings.append(ConfigWarning(path,"Invalid lattice lock; unlocked state kept"))
            return
        try:
            self.set(data.get("kind"),data.get("reference"))
        except (TypeError,ValueError) as error:
            warnings.append(ConfigWarning(path,f"Invalid lattice lock; unlocked state kept ({error})"))
            self.set(None,None)


@dataclass(frozen=True)
class LatticeLockRequest:
    """Atomic request to replace one raster target's persistent lock."""

    target_key: str
    kind: str | None
    reference: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if not str(self.target_key or ""):
            raise ValueError("Lattice lock request requires a target key")
        probe = LatticeLockState()
        probe.set(self.kind,self.reference)
        object.__setattr__(self,"kind",probe.kind)
        object.__setattr__(self,"reference",probe.reference)


@dataclass(frozen=True)
class LatticeAxisIntent:
    """Requested semantic values used to rank raster realizations."""

    period_px: float
    n_foci: int
    fov_px: float
    explicit: frozenset[str] = frozenset()
    persistent: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LatticeResolutionIntent:
    x: LatticeAxisIntent
    y: LatticeAxisIntent


LATTICE_MODIFIER_PARAMS = {
    "rotation_deg":ParamSpec(
        0.0,float,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
    ),
    "skew_deg":ParamSpec(
        0.0,float,step=0.1,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
    ),
    "stagger":ParamSpec(
        0.0,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
    ),
}


def lattice_param_specs(modifiers: Sequence[str]=()) -> dict[str, ParamSpec]:
    """Return common lattice specs plus explicitly supported modifiers."""
    specs = dict(BASE_LATTICE_PARAMS)
    for key in modifiers:
        if key not in LATTICE_MODIFIER_PARAMS:
            raise KeyError(f"Unknown lattice modifier '{key}'")
        specs[key] = LATTICE_MODIFIER_PARAMS[key]
    return specs


def reconcile_lattice_params(
    params: Mapping[str,Any],
    changed_keys: Iterable[str],
) -> dict[str, Any]:
    """Reconcile the continuous lattice without persistent raster intent."""
    resolved,intent = reconcile_lattice_params_with_intent(
        params,changed_keys,lock=None,
    )
    del intent
    return resolved


def reconcile_lattice_params_with_intent(
    params: Mapping[str,Any],
    changed_keys: Iterable[str],
    *,
    lock: LatticeLockState | None=None,
) -> tuple[dict[str, Any], LatticeResolutionIntent]:
    """Reconcile one coalesced edit batch and retain all requested quantities.

    Explicit values in the current batch take precedence over a persistent
    lock reference. The current canonical state is used only when neither
    supplies intent. The returned parameters are internally consistent; the
    separate intent retains over-constrained requests for raster scoring.
    """
    resolved = dict(params)
    changed = set(changed_keys)
    lock = lock if isinstance(lock,LatticeLockState) else None
    if lock is not None:
        lock.validate()
    x = _reconcile_axis_params(resolved,changed,"x",lock)
    y = _reconcile_axis_params(resolved,changed,"y",lock)
    return resolved,LatticeResolutionIntent(x=x,y=y)


def validate_lattice_params(params: Mapping[str,Any]) -> None:
    """Validate canonical period/FOV/number-of-foci relationships."""
    for axis in ("x","y"):
        period = float(params[f"period_{axis}_px"])
        fov = float(params[f"fov_{axis}_px"])
        n_foci = int(params[f"n_foci_{axis}"])

        if period <= 0:
            raise ValueError(f"period_{axis}_px must be > 0")
        if fov < 0:
            raise ValueError(f"fov_{axis}_px must be >= 0")
        if n_foci <= 0:
            raise ValueError(f"n_foci_{axis} must be > 0")

        expected_fov = fov_from_n_foci(n_foci,period)
        if not np.isclose(fov,expected_fov,rtol=0.0,atol=1e-9):
            raise ValueError(
                f"fov_{axis}_px={fov} is inconsistent with "
                f"period_{axis}_px={period} and n_foci_{axis}={n_foci}; "
                f"expected {expected_fov}"
            )


def _reconcile_axis_params(
    params: dict[str, Any],
    changed_keys,
    axis: str,
    lock: LatticeLockState | None,
) -> LatticeAxisIntent:
    period_key = f"period_{axis}_px"
    fov_key = f"fov_{axis}_px"
    n_foci_key = f"n_foci_{axis}"

    current_period = float(params[period_key])
    current_fov = float(params[fov_key])
    current_n = int(params[n_foci_key])
    if current_period <= 0:
        raise ValueError(f"{period_key} must be > 0, got {current_period}")
    if current_fov < 0:
        raise ValueError(f"{fov_key} must be >= 0, got {current_fov}")
    if current_n <= 0:
        raise ValueError(f"{n_foci_key} must be > 0, got {current_n}")

    explicit = set()
    if period_key in changed_keys:
        explicit.add("period")
    if n_foci_key in changed_keys:
        explicit.add("n_foci")
    if fov_key in changed_keys:
        explicit.add("fov")

    period = current_period
    n_foci = current_n
    fov = current_fov
    intent_fov_reference = None

    lock_value = None
    if lock is not None and lock.kind is not None and lock.reference is not None:
        lock_value = lock.reference[0 if axis == "x" else 1]

    # Select two semantic constraints. Two or more explicit geometry edits
    # fully define this transaction; a lock only fills missing intent.
    if len(explicit) >= 2:
        if "period" in explicit and "n_foci" in explicit:
            period = current_period
            n_foci = current_n
            fov = fov_from_n_foci(n_foci,period)
        elif "period" in explicit and "fov" in explicit:
            period = current_period
            fov = current_fov
            n_foci = n_foci_from_fov(fov,period)
            fov = fov_from_n_foci(n_foci,period)
        else:  # n_foci + fov (possibly all three)
            n_foci = current_n
            requested_fov = current_fov
            if n_foci == 1:
                if not math.isclose(requested_fov,0.0,abs_tol=1e-12):
                    raise ValueError(
                        f"{fov_key} must be 0 when {n_foci_key} is 1"
                    )
                period = current_period
            else:
                period = requested_fov / float(n_foci - 1)
            fov = fov_from_n_foci(n_foci,period)
    elif explicit == {"period"}:
        period = current_period
        if lock is not None and lock.kind == "n_foci":
            n_foci = int(lock_value)
        else:
            target_fov = (
                float(lock_value)
                if lock is not None and lock.kind == "fov"
                else current_fov
            )
            intent_fov_reference = target_fov
            n_foci = n_foci_from_fov(target_fov,period)
        fov = fov_from_n_foci(n_foci,period)
    elif explicit == {"n_foci"}:
        n_foci = current_n
        if lock is not None and lock.kind == "fov":
            target_fov = float(lock_value)
            intent_fov_reference = target_fov
            if n_foci == 1:
                if not math.isclose(target_fov,0.0,abs_tol=1e-12):
                    raise ValueError(
                        "Cannot keep a non-zero FOV reference with one focus"
                    )
                period = current_period
            else:
                period = target_fov / float(n_foci - 1)
        else:
            period = current_period
        fov = fov_from_n_foci(n_foci,period)
    elif explicit == {"fov"}:
        requested_fov = current_fov
        if lock is not None and lock.kind == "n_foci":
            n_foci = int(lock_value)
            if n_foci == 1:
                if not math.isclose(requested_fov,0.0,abs_tol=1e-12):
                    raise ValueError(
                        f"{fov_key} must be 0 when locked {n_foci_key} is 1"
                    )
                period = current_period
            else:
                period = requested_fov / float(n_foci - 1)
        else:
            period = current_period
            n_foci = n_foci_from_fov(requested_fov,period)
        fov = fov_from_n_foci(n_foci,period)
    else:
        # Policy/modifier/context-only changes still resolve from a persistent
        # lock when present, preventing the lock reference from being forgotten.
        if lock is not None and lock.kind == "fov":
            target_fov = float(lock_value)
            intent_fov_reference = target_fov
            period = current_period
            n_foci = n_foci_from_fov(target_fov,period)
            fov = fov_from_n_foci(n_foci,period)
        elif lock is not None and lock.kind == "n_foci":
            period = current_period
            n_foci = int(lock_value)
            fov = fov_from_n_foci(n_foci,period)

    params[period_key] = period
    params[n_foci_key] = n_foci
    params[fov_key] = fov

    # Keep the user's requested values for raster scoring, even when three
    # explicit values are inconsistent and the canonical semantic mapping must
    # itself satisfy FOV=(N-1)*period.
    intent_period = current_period if "period" in explicit else period
    intent_n = current_n if "n_foci" in explicit else n_foci
    if "fov" in explicit:
        intent_fov = current_fov
    elif intent_fov_reference is not None:
        intent_fov = float(intent_fov_reference)
    else:
        intent_fov = fov_from_n_foci(intent_n,intent_period)
    if lock is not None and lock.kind == "n_foci" and len(explicit) < 2 and "n_foci" not in explicit:
        intent_n = int(lock_value)

    persistent = set()
    if (
        lock is not None and lock.kind is not None
        and lock.kind not in explicit and len(explicit) < 2
    ):
        persistent.add(lock.kind)

    return LatticeAxisIntent(
        period_px=float(intent_period),
        n_foci=int(intent_n),
        fov_px=float(intent_fov),
        explicit=frozenset(explicit),
        persistent=frozenset(persistent),
    )


def n_foci_from_fov(fov,period):
    """Return the largest count whose first-to-last span does not exceed FOV."""
    return int(math.floor(float(fov) / float(period))) + 1


def fov_from_n_foci(n_foci,period):
    """Return the first-to-last span of one regular lattice axis."""
    return (int(n_foci) - 1) * float(period)


@dataclass(frozen=True)
class LatticeDefinition:
    """Canonical semantic lattice independent of any sampled raster grid."""

    period_x_px: float
    period_y_px: float
    n_foci_x: int
    n_foci_y: int
    rotation_deg: float = 0.0
    skew_deg: float = 0.0
    stagger: float = 0.0

    def __post_init__(self) -> None:
        if self.period_x_px <= 0 or self.period_y_px <= 0:
            raise ValueError("Lattice periods must be > 0")
        if self.n_foci_x <= 0 or self.n_foci_y <= 0:
            raise ValueError("Lattice numbers of foci must be > 0")
        if not 0.0 <= self.stagger <= 1.0:
            raise ValueError("Lattice stagger must be in [0, 1]")

    @classmethod
    def from_params(cls,params: Mapping[str,Any]) -> "LatticeDefinition":
        """Build a semantic lattice, defaulting absent modifiers to zero."""
        return cls(
            period_x_px=float(params["period_x_px"]),
            period_y_px=float(params["period_y_px"]),
            n_foci_x=int(params["n_foci_x"]),
            n_foci_y=int(params["n_foci_y"]),
            rotation_deg=float(params.get("rotation_deg",0.0)),
            skew_deg=float(params.get("skew_deg",0.0)),
            stagger=float(params.get("stagger",0.0)),
        )

    @property
    def fov_x_px(self) -> float:
        return fov_from_n_foci(self.n_foci_x,self.period_x_px)

    @property
    def fov_y_px(self) -> float:
        return fov_from_n_foci(self.n_foci_y,self.period_y_px)

    @property
    def period_kxy(self) -> tuple[float, float]:
        return (
            reference_px_to_k(self.period_x_px),
            reference_px_to_k(self.period_y_px),
        )

    @property
    def n_spots(self) -> int:
        return self.n_foci_x * self.n_foci_y

    def to_params(self,base: Mapping[str,Any]) -> dict[str, Any]:
        """Write this lattice geometry into a complete target parameter mapping."""
        params = dict(base)
        params.update({
            "period_x_px":self.period_x_px,
            "period_y_px":self.period_y_px,
            "fov_x_px":self.fov_x_px,
            "fov_y_px":self.fov_y_px,
            "n_foci_x":self.n_foci_x,
            "n_foci_y":self.n_foci_y,
        })
        for key in ("rotation_deg","skew_deg","stagger"):
            if key in params:
                params[key] = getattr(self,key)
        return params

    def with_geometry(
        self,
        *,
        period_x_px: float,
        period_y_px: float,
        n_foci_x: int,
        n_foci_y: int,
    ) -> "LatticeDefinition":
        """Return the same lattice modifiers with new canonical axis geometry."""
        return LatticeDefinition(
            period_x_px=float(period_x_px),
            period_y_px=float(period_y_px),
            n_foci_x=int(n_foci_x),
            n_foci_y=int(n_foci_y),
            rotation_deg=self.rotation_deg,
            skew_deg=self.skew_deg,
            stagger=self.stagger,
        )

    def lattice_indices(self) -> np.ndarray:
        """Return stable integer ``(i,j)`` identifiers with shape ``(2,N)``."""
        i = np.arange(self.n_foci_x,dtype=np.int64)
        j = np.arange(self.n_foci_y,dtype=np.int64)
        grid_i,grid_j = np.meshgrid(i,j,indexing="xy")
        return np.vstack([grid_i.ravel(),grid_j.ravel()])

    def lattice_representation(self) -> LatticeRepresentation:
        """Return the canonical translation-cell + motif representation."""
        return LatticeRepresentation.alternating_rows(
            self.lattice_indices(),
            self.stagger,
        )

    def spot_positions_kxy(self) -> np.ndarray:
        """Return ideal continuous positions with shape ``(2,N)``.

        Target UI parameters are converted first to the canonical finite-lattice
        representation.  Period, skew and rotation are then applied as a
        separate continuous transform.  This keeps spot identities stable even
        for special stagger values that admit a smaller primitive Bravais cell.
        """
        representation = self.lattice_representation()
        logical = representation.logical_positions
        center = np.array([
            (self.n_foci_x - 1) / 2.0,
            (self.n_foci_y - 1) / 2.0,
        ],dtype=np.float64)
        logical = logical - center[:,None]

        period_kx,period_ky = self.period_kxy
        skew_rad = np.deg2rad(self.skew_deg)
        affine = np.array([
            [period_kx,period_ky * np.sin(skew_rad)],
            [0.0,period_ky * np.cos(skew_rad)],
        ],dtype=np.float64)
        positions = affine.dot(logical)

        theta = np.deg2rad(self.rotation_deg)
        rotation = np.array([
            [np.cos(theta),-np.sin(theta)],
            [np.sin(theta), np.cos(theta)],
        ],dtype=np.float64)
        return rotation.dot(positions)


def snap_spots_to_grid(
    positions_kxy: np.ndarray,
    output_shape: tuple[int, int],
    strict: bool=False,
):
    """Snap continuous Fourier positions to one centered output grid."""
    height,width = validate_shape(output_shape)
    positions = np.asarray(positions_kxy,dtype=np.float64)
    if positions.ndim != 2 or positions.shape[0] != 2:
        raise ValueError(
            f"positions_kxy must have shape (2, N), got {positions.shape}"
        )

    x_pixels = np.rint(width / 2.0 + positions[0] * width).astype(np.int64)
    y_pixels = np.rint(height / 2.0 + positions[1] * height).astype(np.int64)
    inside = (
        (x_pixels >= 0) & (x_pixels < width)
        & (y_pixels >= 0) & (y_pixels < height)
    )

    if strict and not np.all(inside):
        raise ValueError(
            f"{int(np.count_nonzero(~inside))} lattice spot(s) fall outside "
            f"raster shape {(height,width)}"
        )

    snapped = np.vstack([
        (x_pixels.astype(np.float64) - width / 2.0) / width,
        (y_pixels.astype(np.float64) - height / 2.0) / height,
    ])
    return snapped,x_pixels,y_pixels,inside


def rasterize_spots(
    positions_kxy: np.ndarray,
    intensities: np.ndarray,
    output_shape: tuple[int, int],
    strict: bool=False,
    normalize: bool=True,
) -> np.ndarray:
    """Rasterize relative spot intensities for previews and sampled views."""
    height,width = validate_shape(output_shape)
    intensities = np.asarray(intensities,dtype=np.float64)
    _,x_pixels,y_pixels,inside = snap_spots_to_grid(
        positions_kxy,(height,width),strict=strict,
    )
    if intensities.shape != (x_pixels.size,):
        raise ValueError(
            f"intensities must have shape ({x_pixels.size},), got "
            f"{intensities.shape}"
        )
    if not np.all(np.isfinite(intensities)):
        raise ValueError("intensities contain non-finite values")
    if np.any(intensities < 0):
        raise ValueError("intensities cannot contain negative values")

    target = np.zeros((height,width),dtype=np.float64)
    np.maximum.at(
        target,
        (y_pixels[inside],x_pixels[inside]),
        intensities[inside],
    )
    if normalize and target.size and np.max(target) > 0:
        target /= np.max(target)
    return target


def validate_shape(shape):
    """Return a validated positive ``(height,width)`` shape."""
    if shape is None or len(shape) != 2:
        raise ValueError(f"shape must be (height, width), got {shape}")
    height,width = int(shape[0]),int(shape[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"shape must be positive, got {shape}")
    return height,width
