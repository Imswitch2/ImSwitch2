"""Reference-to-localization spot correspondence for position feedback."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from ..localization import (
    LocalizationResult,SpotOrdering,available_spot_orderings,
    spot_ordering_label,spot_ordering_permutation,
)


class ReferenceCorrespondence(str,Enum):
    """How a saved reference is paired with the current localized lattice."""

    AUTO = "auto"
    CANONICAL = "canonical"
    FLIP_X = "flip_x"
    FLIP_Y = "flip_y"
    ROTATE_180 = "rotate_180"
    TRANSPOSE = "transpose"
    ROTATE_90_CW = "rotate_90_cw"
    ROTATE_90_CCW = "rotate_90_ccw"
    ANTI_TRANSPOSE = "anti_transpose"

    @classmethod
    def normalize(cls,value: Any) -> "ReferenceCorrespondence":
        if isinstance(value,cls):
            return value
        text = str(value or cls.AUTO.value).strip().lower()
        aliases = {"identity":cls.CANONICAL.value,"none":cls.CANONICAL.value}
        return cls(aliases.get(text,text))

    @property
    def ordering(self) -> SpotOrdering | None:
        if self is ReferenceCorrespondence.AUTO:
            return None
        return SpotOrdering.normalize(self.value)


@dataclass(frozen=True)
class ReferenceCorrespondenceResolution:
    requested: ReferenceCorrespondence
    resolved: SpotOrdering
    relative_rotation_deg: float
    rms_px: float
    reflection: bool
    singular_values: tuple[float,float]
    deformation_score: float
    candidate_count: int

    def to_dict(self) -> dict[str,Any]:
        return {
            "requested":self.requested.value,
            "resolved":self.resolved.value,
            "resolved_label":spot_ordering_label(self.resolved),
            "relative_rotation_deg":self.relative_rotation_deg,
            "rms_px":self.rms_px,
            "reflection":self.reflection,
            "singular_values":self.singular_values,
            "deformation_score":self.deformation_score,
            "candidate_count":self.candidate_count,
        }


def reference_correspondence_options(
    localization: LocalizationResult,
) -> tuple[ReferenceCorrespondence,...]:
    logical = _logical_positions(localization)
    result = [ReferenceCorrespondence.AUTO]
    try:
        orderings = available_spot_orderings(
            localization.lattice_indices,logical,
        )
    except ValueError:
        orderings = (SpotOrdering.CANONICAL,)
    for ordering in orderings:
        result.append(ReferenceCorrespondence(ordering.value))
    return tuple(result)


def resolve_reference_correspondence(
    localization: LocalizationResult,
    reference_positions_px: np.ndarray,
    correspondence: ReferenceCorrespondence | str=ReferenceCorrespondence.AUTO,
) -> tuple[np.ndarray,ReferenceCorrespondenceResolution]:
    """Return reference positions reindexed to the current localization identities.

    Auto tests only exact finite-lattice ordering symmetries.  Each candidate is
    globally registered current->reference for *scoring only*.  Gross reflection
    is rejected first, then the correspondence requiring the smallest residual
    rotation/deformation is preferred.  The returned positions remain the real
    per-spot reference coordinates; the fitted affine is never used as the
    position correction itself.
    """
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    reference = np.asarray(reference_positions_px,dtype=np.float64)
    current = np.asarray(localization.measured_positions_px,dtype=np.float64)
    if reference.shape != current.shape:
        raise ValueError("Reference positions must align with localization spots")

    requested = ReferenceCorrespondence.normalize(correspondence)
    options = reference_correspondence_options(localization)
    manual = requested.ordering
    if manual is not None and requested not in options:
        raise ValueError(
            "Reference correspondence '%s' is not valid for this finite lattice"
            % requested.value
        )

    diagnostics = dict(localization.diagnostics or {})
    matched_value = diagnostics.get("matched_mask")
    matched = (
        np.ones(current.shape[1],dtype=bool)
        if matched_value is None
        else np.asarray(matched_value,dtype=bool)
    )
    if matched.shape != (current.shape[1],):
        raise RuntimeError("Localization matched-mask shape is inconsistent")
    if int(np.count_nonzero(matched)) < 3:
        raise RuntimeError(
            "Reference correspondence needs at least three genuinely localized spots"
        )

    candidates = []
    orderings = (
        (manual,) if manual is not None
        else tuple(item.ordering for item in options if item.ordering is not None)
    )
    for ordering in orderings:
        permutation = spot_ordering_permutation(
            localization.lattice_indices,ordering,
        )
        candidate = reference[:,permutation]
        linear,translation,rms = _fit_affine(
            current[:,matched],candidate[:,matched],
        )
        metrics = _transform_metrics(linear,rms)
        # Reflection should already be described by FeedbackOrientation.  After
        # that fixed section mapping, prefer the correspondence needing the
        # smallest residual rotation and deformation. RMS breaks remaining ties.
        score = (
            1 if metrics["reflection"] else 0,
            abs(float(metrics["rotation_deg"])),
            float(metrics["deformation_score"]),
            float(rms),
            ordering.value,
        )
        candidates.append((score,ordering,candidate,metrics,translation))

    if not candidates:
        raise RuntimeError("No valid reference correspondence is available")
    candidates.sort(key=lambda item:item[0])
    _score,resolved,candidate,metrics,_translation = candidates[0]
    resolution = ReferenceCorrespondenceResolution(
        requested=requested,
        resolved=resolved,
        relative_rotation_deg=float(metrics["rotation_deg"]),
        rms_px=float(metrics["rms_px"]),
        reflection=bool(metrics["reflection"]),
        singular_values=tuple(float(value) for value in metrics["singular_values"]),
        deformation_score=float(metrics["deformation_score"]),
        candidate_count=len(candidates),
    )
    return candidate,resolution


def _logical_positions(localization: LocalizationResult) -> np.ndarray | None:
    diagnostics = dict(localization.diagnostics or {})
    value = diagnostics.get("ordering_logical_positions")
    if value is None:
        return None
    logical = np.asarray(value,dtype=np.float64)
    return logical if logical.shape == localization.lattice_indices.shape else None


def _fit_affine(source: np.ndarray,target: np.ndarray):
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    design = np.vstack((source,np.ones((1,source.shape[1])))).T
    coefficients,_,_,_ = np.linalg.lstsq(design,target.T,rcond=None)
    linear = coefficients[:2,:].T
    translation = coefficients[2,:]
    residual = target-(linear@source+translation[:,None])
    rms = float(np.sqrt(np.mean(np.sum(residual*residual,axis=0))))
    return linear,translation,rms


def _transform_metrics(linear: np.ndarray,rms: float) -> dict[str,Any]:
    linear = np.asarray(linear,dtype=np.float64)
    determinant = float(np.linalg.det(linear))
    u,singular,vt = np.linalg.svd(linear)
    rotation = u@vt
    if np.linalg.det(rotation) < 0:
        u = np.array(u,copy=True)
        u[:,-1] *= -1.0
        rotation = u@vt
    angle = float(np.degrees(np.arctan2(rotation[1,0],rotation[0,0])))
    singular = np.maximum(np.asarray(singular,dtype=np.float64),1e-12)
    deformation = float(np.sqrt(np.mean(np.square(np.log(singular)))))
    return {
        "rotation_deg":angle,
        "rms_px":float(rms),
        "reflection":bool(determinant < 0.0),
        "singular_values":tuple(float(value) for value in singular),
        "deformation_score":deformation,
    }


__all__ = [
    "ReferenceCorrespondence",
    "ReferenceCorrespondenceResolution",
    "reference_correspondence_options",
    "resolve_reference_correspondence",
]
