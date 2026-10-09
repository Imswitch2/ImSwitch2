"""Adapters from existing SLMCore geometry sources to neutral references.

The adapters intentionally live beside the neutral model rather than inside the
feedback service.  Pass 1 introduces them without changing current orchestration;
Pass 2 can route the existing service through the same constructors.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any,Mapping

import numpy as np

from ...cgh.lattice_geometry import LatticeRepresentation
from .model import PositionReferenceTarget,ReferenceGeometry,lattice_site_ids


def _merge_metadata(base: Mapping[str,Any],extra: Mapping[str,Any] | None) -> dict[str,Any]:
    result=deepcopy(dict(base))
    result.update(deepcopy(dict(extra or {})))
    return result


def _crop_offset(localization: Any) -> np.ndarray:
    y1,_y2,x1,_x2=localization.crop_coord
    return np.asarray([[float(x1)],[float(y1)]],dtype=np.float64)


def _positions_in_frame(positions: Any,localization: Any,*,full_image: bool) -> np.ndarray:
    result=np.asarray(positions,dtype=np.float64)
    return result+_crop_offset(localization) if full_image else result


def _lattice_from_localization(localization: Any) -> LatticeRepresentation | None:
    """Recover the finite target topology retained by localization.

    Position-reference resolution must know the translation-cell + motif structure
    (not merely stable lattice indices) so later commensurate resolvers can reason
    about sparse/staggered target lattices.  Current lattice localizers persist the
    resolved public stagger in diagnostics; target parameters are a compatibility
    fallback for older accepted localizations.
    """
    diagnostics=dict(getattr(localization,"diagnostics",{}) or {})
    payload=diagnostics.get("lattice_representation")
    if payload is not None:
        try:
            lattice=LatticeRepresentation.from_mapping(payload)
            if np.array_equal(lattice.lattice_indices,localization.lattice_indices):
                return lattice
        except (KeyError,TypeError,ValueError):
            pass
    stagger=diagnostics.get("resolved_stagger")
    if stagger is None:
        params=dict(getattr(localization,"target_params",{}) or {})
        stagger=params.get("stagger")
    if stagger is None:
        return None
    try:
        value=float(stagger)
        return LatticeRepresentation.alternating_rows(
            localization.lattice_indices,value,
        )
    except (TypeError,ValueError):
        # Reference geometry remains usable even for target families that do not
        # expose an alternating-row lattice topology.
        return None


def reference_geometry_from_localization(
    localization: Any,
    *,
    positions: str="measured",
    full_image: bool=True,
    valid_mask: Any=None,
    weights: Any=None,
    lattice: LatticeRepresentation | None=None,
    metadata: Mapping[str,Any] | None=None,
) -> ReferenceGeometry:
    """Build a reference from accepted localization geometry.

    ``positions`` may be ``"measured"`` or ``"expected"``.  By default measured
    references accept both genuine and explicitly inferred sites, matching the
    current position-reference creation semantics.
    """
    from ...cgh.localization import LocalizationResult,localization_site_masks

    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    kind=str(positions or "measured").strip().lower()
    if kind not in {"measured","expected"}:
        raise ValueError('positions must be "measured" or "expected"')
    values=(
        localization.measured_positions_px
        if kind=="measured" else localization.expected_positions_px
    )
    if valid_mask is None:
        _matched,_inferred,resolved=localization_site_masks(localization)
        valid_mask=resolved if kind=="measured" else np.ones(
            localization.lattice_indices.shape[1],dtype=bool,
        )
    if lattice is None:
        lattice=_lattice_from_localization(localization)
    return ReferenceGeometry(
        positions_px=_positions_in_frame(values,localization,full_image=full_image),
        site_ids=lattice_site_ids(localization.lattice_indices),
        lattice=lattice,
        valid_mask=valid_mask,
        weights=weights,
        metadata=_merge_metadata({
            "source":"localization",
            "positions_kind":kind,
            "coordinate_frame":"full_detector" if full_image else "localization_crop",
            "target_type":localization.target_type,
        },metadata),
    )


def reference_geometry_from_position_reference(
    reference: Any,
    *,
    lattice: LatticeRepresentation | None=None,
    weights: Any=None,
    metadata: Mapping[str,Any] | None=None,
) -> ReferenceGeometry:
    """Adapt the current persistent ``PositionReference`` record."""
    from ...cgh.feedback.reference import PositionReference

    if not isinstance(reference,PositionReference):
        raise TypeError("reference must be a PositionReference")
    if lattice is None:
        payload=dict(reference.metadata or {}).get("lattice_representation")
        if payload is not None:
            try:
                lattice=LatticeRepresentation.from_mapping(payload)
            except (KeyError,TypeError,ValueError):
                lattice=None
    return ReferenceGeometry(
        positions_px=reference.positions_px,
        site_ids=lattice_site_ids(reference.lattice_indices),
        lattice=lattice,
        valid_mask=np.ones(reference.positions_px.shape[1],dtype=bool),
        weights=weights,
        metadata=_merge_metadata({
            "source":"position_reference",
            "coordinate_frame":"full_detector",
            "name":reference.name,
            "observation_plane_name":reference.observation_plane_name,
            "detector_name":reference.detector_name,
            "image_shape":tuple(reference.image_shape),
            "created_at":reference.created_at,
            "reference_metadata":dict(reference.metadata),
        },metadata),
    )


def reference_geometry_from_editable_lattice(
    localization: Any,
    geometry: Any,
    *,
    full_image: bool=True,
    lattice: LatticeRepresentation | None=None,
    weights: Any=None,
    metadata: Mapping[str,Any] | None=None,
) -> ReferenceGeometry:
    """Adapt the existing editable-lattice reference into neutral geometry."""
    from ..reference_geometry import (
        EditablePositionReferenceGeometry,editable_reference_positions,
    )
    from ...cgh.localization import LocalizationResult

    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    if not isinstance(geometry,EditablePositionReferenceGeometry):
        geometry=EditablePositionReferenceGeometry.from_mapping(geometry)
    positions=editable_reference_positions(localization,geometry)
    if lattice is None:
        lattice=_lattice_from_localization(localization)
    return ReferenceGeometry(
        positions_px=_positions_in_frame(positions,localization,full_image=full_image),
        site_ids=lattice_site_ids(localization.lattice_indices),
        lattice=lattice,
        valid_mask=np.ones(localization.lattice_indices.shape[1],dtype=bool),
        weights=weights,
        metadata=_merge_metadata({
            "source":"editable_lattice",
            "coordinate_frame":"full_detector" if full_image else "localization_crop",
            "geometry":geometry.to_dict(),
        },metadata),
    )


def reference_geometry_from_global_fit(
    localization: Any,
    *,
    full_image: bool=True,
    lattice: LatticeRepresentation | None=None,
    weights: Any=None,
    metadata: Mapping[str,Any] | None=None,
) -> ReferenceGeometry:
    """Adapt the localization's registered/global-fit lattice geometry."""
    return reference_geometry_from_localization(
        localization,positions="expected",full_image=full_image,
        valid_mask=np.ones(localization.lattice_indices.shape[1],dtype=bool),
        weights=weights,lattice=lattice,
        metadata=_merge_metadata({"source":"global_fit"},metadata),
    )



def reference_geometry_from_current_target(
    target: PositionReferenceTarget,
    *,
    weights: Any=None,
    metadata: Mapping[str,Any] | None=None,
) -> ReferenceGeometry:
    """Use the target's current measured positions as a self/current reference."""
    if not isinstance(target,PositionReferenceTarget):
        raise TypeError("target must be a PositionReferenceTarget")
    return ReferenceGeometry(
        positions_px=target.measured_positions_px,site_ids=target.site_ids,
        lattice=target.lattice,valid_mask=np.ones(target.point_count,dtype=bool),
        weights=weights,metadata=_merge_metadata({
            "source":"current_target","coordinate_frame":"full_detector",
        },metadata),
    )

def position_reference_target_from_localization(
    localization: Any,
    *,
    full_image: bool=True,
    matched_mask: Any=None,
    lattice: LatticeRepresentation | None=None,
    metadata: Mapping[str,Any] | None=None,
) -> PositionReferenceTarget:
    """Build an alignment target from one accepted localization.

    The default ``matched_mask`` contains genuine detector observations only;
    inferred sites remain available for prediction but never become synthetic
    fit correspondences.
    """
    from ...cgh.localization import LocalizationResult,localization_site_masks

    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    if matched_mask is None:
        matched_mask,_inferred,_resolved=localization_site_masks(localization)
    if lattice is None:
        lattice=_lattice_from_localization(localization)
    return PositionReferenceTarget(
        registered_positions_px=_positions_in_frame(
            localization.expected_positions_px,localization,full_image=full_image,
        ),
        measured_positions_px=_positions_in_frame(
            localization.measured_positions_px,localization,full_image=full_image,
        ),
        matched_mask=matched_mask,
        site_ids=lattice_site_ids(localization.lattice_indices),
        lattice_indices=localization.lattice_indices,
        lattice=lattice,
        metadata=_merge_metadata({
            "source":"localization",
            "coordinate_frame":"full_detector" if full_image else "localization_crop",
            "target_type":localization.target_type,
            "ordering_logical_positions":dict(localization.diagnostics or {}).get(
                "ordering_logical_positions"
            ),
        },metadata),
    )


__all__=[
    "position_reference_target_from_localization",
    "reference_geometry_from_editable_lattice",
    "reference_geometry_from_global_fit",
    "reference_geometry_from_current_target",
    "reference_geometry_from_localization",
    "reference_geometry_from_position_reference",
]
