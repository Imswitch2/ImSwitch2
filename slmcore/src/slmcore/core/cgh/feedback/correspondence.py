"""Compatibility API for position-reference finite-lattice correspondence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from ...measurement.position_reference import (
    ReferenceGeometry,exact_lattice_correspondence_options,
    position_reference_target_from_localization,resolve_exact_lattice_correspondence,
)
from ..localization import LocalizationResult,SpotOrdering,spot_ordering_label


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


def _neutral_target(localization: LocalizationResult):
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    return position_reference_target_from_localization(localization,full_image=False)


def reference_correspondence_options(
    localization: LocalizationResult,
) -> tuple[ReferenceCorrespondence,...]:
    """Compatibility wrapper around the neutral exact-lattice resolver."""
    target=_neutral_target(localization)
    return tuple(
        ReferenceCorrespondence(value)
        for value in exact_lattice_correspondence_options(target)
    )


def resolve_reference_correspondence(
    localization: LocalizationResult,
    reference_positions_px: np.ndarray,
    correspondence: ReferenceCorrespondence | str=ReferenceCorrespondence.AUTO,
) -> tuple[np.ndarray,ReferenceCorrespondenceResolution]:
    """Return reference positions reindexed to current localization identities.

    The finite-lattice scoring implementation now lives only in the neutral
    ``exact_lattice`` resolver.  This function preserves the historical public
    ``LocalizationResult`` API and result value object.
    """
    target=_neutral_target(localization)
    reference=np.asarray(reference_positions_px,dtype=np.float64)
    if reference.shape!=target.measured_positions_px.shape:
        raise ValueError("Reference positions must align with localization spots")
    # This compatibility API accepts an already re-ordered bare position array,
    # so it has no trustworthy source topology to pair with those columns. Keep
    # the historical rectangular-permutation behavior here; persisted references
    # use the topology-aware path through PositionReferenceService.
    source=ReferenceGeometry(
        positions_px=reference,
        site_ids=target.site_ids,
        lattice=None,
        valid_mask=np.ones(target.point_count,dtype=bool),
    )
    requested=ReferenceCorrespondence.normalize(correspondence)
    resolved=resolve_exact_lattice_correspondence(
        source,target,requested.value,
    )
    details=dict(resolved.correspondence or {})
    resolution=ReferenceCorrespondenceResolution(
        requested=ReferenceCorrespondence.normalize(details["requested"]),
        resolved=SpotOrdering.normalize(details["resolved"]),
        relative_rotation_deg=float(details["relative_rotation_deg"]),
        rms_px=float(details["rms_px"]),
        reflection=bool(details["reflection"]),
        singular_values=tuple(float(value) for value in details["singular_values"]),
        deformation_score=float(details["deformation_score"]),
        candidate_count=int(details["candidate_count"]),
    )
    return np.asarray(resolved.desired_positions_px,dtype=np.float64),resolution


__all__ = [
    "ReferenceCorrespondence",
    "ReferenceCorrespondenceResolution",
    "reference_correspondence_options",
    "resolve_reference_correspondence",
]
