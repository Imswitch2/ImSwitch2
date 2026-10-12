"""Exact finite-lattice correspondence for neutral position references."""

from __future__ import annotations

from typing import Any

import numpy as np

from ....cgh.localization import (
    SpotOrdering,available_spot_orderings,spot_ordering_label,
    spot_ordering_permutation,
)
from ..model import PositionReferenceTarget,ReferenceGeometry,ResolvedPositionReference


_CORRESPONDENCE_VALUES=(
    "auto","canonical","flip_x","flip_y","rotate_180","transpose",
    "rotate_90_cw","rotate_90_ccw","anti_transpose",
)

def _normalize_correspondence(value: Any) -> str:
    text=str(value or "auto").strip().lower()
    text={"identity":"canonical","none":"canonical"}.get(text,text)
    if text not in _CORRESPONDENCE_VALUES:
        raise ValueError("Unknown exact-lattice correspondence %r" % value)
    return text


def exact_lattice_correspondence_options(
    target: PositionReferenceTarget,
) -> tuple[str,...]:
    """Return AUTO plus shape-compatible finite-lattice orientation choices.

    Reference correspondence intentionally differs from localization ordering:
    a saved source motif need not be a self-symmetry of the current motif.  For
    example ``+0.5`` and ``-0.5`` alternating-row staggers are the same infinite
    lattice with an odd-row cell shift.  The topology-aware resolver below deals
    with that phase/cell shift after the user-visible orientation is chosen.
    """
    if target.lattice_indices is None:
        return ("auto","canonical")
    result=["auto"]
    try:
        orderings=available_spot_orderings(target.lattice_indices,None)
    except ValueError:
        orderings=(SpotOrdering.CANONICAL,)
    result.extend(ordering.value for ordering in orderings)
    return tuple(result)


def _fit_affine(source: np.ndarray,target: np.ndarray):
    design=np.vstack((source,np.ones((1,source.shape[1])))).T
    coefficients,_,_,_=np.linalg.lstsq(design,target.T,rcond=None)
    linear=coefficients[:2,:].T
    translation=coefficients[2,:]
    residual=target-(linear@source+translation[:,None])
    rms=float(np.sqrt(np.mean(np.sum(residual*residual,axis=0))))
    return linear,translation,rms


def _transform_metrics(linear: np.ndarray,rms: float) -> dict[str,Any]:
    determinant=float(np.linalg.det(linear))
    u,singular,vt=np.linalg.svd(linear)
    rotation=u@vt
    if np.linalg.det(rotation)<0:
        u=np.array(u,copy=True); u[:,-1]*=-1.0; rotation=u@vt
    angle=float(np.degrees(np.arctan2(rotation[1,0],rotation[0,0])))
    singular=np.maximum(np.asarray(singular,dtype=np.float64),1e-12)
    deformation=float(np.sqrt(np.mean(np.square(np.log(singular)))))
    return {
        "rotation_deg":angle,"rms_px":float(rms),
        "reflection":bool(determinant<0.0),
        "singular_values":tuple(float(v) for v in singular),
        "deformation_score":deformation,
    }


def _combined_permutation(
    lattice_indices: np.ndarray,
    source_ordering: SpotOrdering,
    correspondence_ordering: SpotOrdering,
) -> np.ndarray:
    source=spot_ordering_permutation(lattice_indices,source_ordering)
    correspondence=spot_ordering_permutation(lattice_indices,correspondence_ordering)
    return np.asarray(source,dtype=np.int64)[np.asarray(correspondence,dtype=np.int64)]


def _resolved_from_source_indices(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
    source_indices: np.ndarray,
    *,
    correspondence: dict[str,Any],
) -> ResolvedPositionReference:
    source_indices=np.asarray(source_indices,dtype=np.int64)
    if source_indices.shape!=(target.point_count,):
        raise ValueError("Exact-lattice source mapping must align with target sites")
    desired=np.array(target.measured_positions_px,dtype=np.float64,copy=True)
    valid=np.zeros(target.point_count,dtype=bool)
    weights=None if reference.weights is None else np.zeros(target.point_count,dtype=np.float64)
    used=np.zeros(reference.point_count,dtype=bool)
    for target_index,source_index in enumerate(source_indices):
        if source_index<0:
            continue
        used[source_index]=True
        desired[:,target_index]=reference.positions_px[:,source_index]
        valid[target_index]=bool(reference.valid_mask[source_index])
        if weights is not None:
            weights[target_index]=float(reference.weights[source_index])
    missing=tuple(
        target.site_ids[index] if target.site_ids is not None else int(index)
        for index in np.flatnonzero(source_indices<0)
    )
    unused=tuple(
        reference.site_ids[index] if reference.site_ids is not None else int(index)
        for index in np.flatnonzero(~used)
    )
    return ResolvedPositionReference(
        desired_positions_px=desired,valid_mask=valid,weights=weights,
        source_geometry=reference,source_indices=source_indices,
        correspondence=correspondence,
        diagnostics={
            "resolver":"exact_lattice","source_count":reference.point_count,
            "target_count":target.point_count,
            "matched_site_count":int(np.count_nonzero(source_indices>=0)),
            "valid_site_count":int(np.count_nonzero(valid)),
            "missing_target_site_ids":missing,"unused_source_site_ids":unused,
        },
    )


def resolve_exact_lattice_correspondence(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
    correspondence: Any="auto",
    *,
    source_ordering: Any="canonical",
) -> ResolvedPositionReference:
    """Resolve finite lattices using topology-aware orientation + phase matching.

    ``correspondence`` is defined in one detector/camera-normalized frame when
    both source and target carry lattice topology.  ``source_ordering`` remains
    only as a compatibility pre-ordering for legacy references without topology.
    """
    if not isinstance(reference,ReferenceGeometry):
        raise TypeError("reference must be a ReferenceGeometry")
    if not isinstance(target,PositionReferenceTarget):
        raise TypeError("target must be a PositionReferenceTarget")
    if target.lattice_indices is None:
        return resolve_exact_lattice(reference,target)
    if reference.point_count!=target.point_count:
        raise ValueError("Finite-lattice correspondence requires equal source/target sizes")

    requested=_normalize_correspondence(correspondence)
    fixed_source=SpotOrdering.normalize(source_ordering)
    options=exact_lattice_correspondence_options(target)
    if requested!="auto" and requested not in options:
        raise ValueError(
            "Reference correspondence '%s' is not valid for this finite lattice" % requested
        )
    orderings=(
        (SpotOrdering.normalize(requested),) if requested!="auto"
        else tuple(SpotOrdering.normalize(item) for item in options if item!="auto")
    )
    current=np.asarray(target.measured_positions_px,dtype=np.float64)
    target_matched=np.asarray(target.matched_mask,dtype=bool)
    candidates=[]

    topology_available=False

    for ordering in orderings:
        source_indices=_combined_permutation(
            target.lattice_indices,fixed_source,ordering,
        )
        source_indices=np.asarray(source_indices,dtype=np.int64)
        resolved_mask=source_indices>=0
        valid_source=np.zeros(target.point_count,dtype=bool)
        if np.any(resolved_mask):
            valid_source[resolved_mask]=np.asarray(reference.valid_mask,dtype=bool)[
                source_indices[resolved_mask]
            ]
        fit_mask=target_matched & resolved_mask & valid_source
        fit_count=int(np.count_nonzero(fit_mask))
        if fit_count<3:
            continue
        candidate=np.array(current,copy=True)
        candidate[:,resolved_mask]=np.asarray(reference.positions_px,dtype=np.float64)[
            :,source_indices[resolved_mask]
        ]
        linear_fit,_translation,rms=_fit_affine(
            current[:,fit_mask],candidate[:,fit_mask],
        )
        metrics=_transform_metrics(linear_fit,rms)
        median_displacement=float(np.median(np.linalg.norm(
            candidate[:,fit_mask]-current[:,fit_mask],axis=0,
        )))
        ordering_priority={value:index for index,value in enumerate(SpotOrdering)}[ordering]
        # Exact-reference AUTO is a local alignment choice, not a detector-frame
        # estimator. Prefer the correspondence requiring the smallest actual spot
        # motion; use affine diagnostics only as deterministic tie-breakers.
        score=(
            round(median_displacement,6),
            round(float(rms),6),
            round(float(metrics["deformation_score"]),8),
            1 if metrics["reflection"] else 0,
            round(abs(float(metrics["rotation_deg"])),6),
            ordering_priority,
        )
        candidates.append((
            score,ordering,source_indices,np.zeros(2,dtype=np.float64),metrics,
            fit_count,median_displacement,
        ))
    if not candidates:
        raise RuntimeError(
            "Reference correspondence needs at least three genuinely localized, "
            "topology-matched spots"
        )
    candidates.sort(key=lambda item:item[0])
    (
        _score,resolved,source_indices,logical_translation,metrics,fit_count,
        median_displacement,
    )=candidates[0]
    details={
        "requested":requested,"resolved":resolved.value,
        "resolved_label":spot_ordering_label(resolved),
        "source_ordering":fixed_source.value,
        "source_detector_frame":"target_indexed",
        "target_detector_frame":"target_indexed",
        "relative_rotation_deg":float(metrics["rotation_deg"]),
        "rms_px":float(metrics["rms_px"]),
        "reflection":bool(metrics["reflection"]),
        "singular_values":tuple(float(v) for v in metrics["singular_values"]),
        "deformation_score":float(metrics["deformation_score"]),
        "candidate_count":len(candidates),
        "fit_count":fit_count,
        "median_displacement_px":median_displacement,
        "logical_translation":tuple(float(v) for v in logical_translation),
        "topology_aware":False,
    }
    return _resolved_from_source_indices(
        reference,target,source_indices,correspondence=details,
    )


def resolve_exact_lattice(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
) -> ResolvedPositionReference:
    """Resolve a reference by strict logical site identity."""
    if not isinstance(reference,ReferenceGeometry):
        raise TypeError("reference must be a ReferenceGeometry")
    if not isinstance(target,PositionReferenceTarget):
        raise TypeError("target must be a PositionReferenceTarget")
    if reference.site_ids is None:
        raise ValueError("Exact-lattice resolution requires reference site IDs")
    if target.site_ids is None:
        raise ValueError("Exact-lattice resolution requires target site IDs")

    lookup={site_id:index for index,site_id in enumerate(reference.site_ids)}
    source_indices=np.asarray([
        int(lookup.get(site_id,-1)) for site_id in target.site_ids
    ],dtype=np.int64)
    return _resolved_from_source_indices(
        reference,target,source_indices,
        correspondence={
            "mode":"exact_lattice",
            "source_indices":tuple(int(v) for v in source_indices),
        },
    )


__all__=[
    "exact_lattice_correspondence_options","resolve_exact_lattice",
    "resolve_exact_lattice_correspondence",
]
