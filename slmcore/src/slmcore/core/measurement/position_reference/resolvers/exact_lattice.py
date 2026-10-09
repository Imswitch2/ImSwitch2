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


def _logical_positions(target: PositionReferenceTarget) -> np.ndarray | None:
    value=dict(target.metadata or {}).get("ordering_logical_positions")
    if value is None or target.lattice_indices is None:
        return None
    logical=np.asarray(value,dtype=np.float64)
    return logical if logical.shape==target.lattice_indices.shape else None


def exact_lattice_correspondence_options(
    target: PositionReferenceTarget,
) -> tuple[str,...]:
    """Return AUTO plus finite-lattice ordering symmetries valid for ``target``."""
    if target.lattice_indices is None:
        return ("auto","canonical")
    result=["auto"]
    try:
        orderings=available_spot_orderings(target.lattice_indices,_logical_positions(target))
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


def _resolved_from_permutation(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
    permutation: np.ndarray,
    *,
    correspondence: dict[str,Any],
) -> ResolvedPositionReference:
    count=target.point_count
    permutation=np.asarray(permutation,dtype=np.int64)
    if permutation.shape!=(count,):
        raise ValueError("Exact-lattice permutation must align with target sites")
    if reference.point_count!=count:
        raise ValueError("Finite-lattice correspondence requires equal source/target sizes")
    desired=np.asarray(reference.positions_px,dtype=np.float64)[:,permutation]
    valid=np.asarray(reference.valid_mask,dtype=bool)[permutation]
    weights=(None if reference.weights is None else np.asarray(reference.weights)[permutation])
    return ResolvedPositionReference(
        desired_positions_px=desired,valid_mask=valid,weights=weights,
        source_geometry=reference,source_indices=permutation,
        correspondence=correspondence,
        diagnostics={
            "resolver":"exact_lattice","source_count":reference.point_count,
            "target_count":target.point_count,
            "matched_site_count":count,"valid_site_count":int(np.count_nonzero(valid)),
            "missing_target_site_ids":(),"unused_source_site_ids":(),
        },
    )


def resolve_exact_lattice_correspondence(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
    correspondence: Any="auto",
) -> ResolvedPositionReference:
    """Resolve same-size finite lattices using current AUTO/manual ordering semantics.

    This is the neutral migration of the historical saved-reference correspondence
    scoring.  The affine fit is used only to score candidate finite-lattice
    permutations; its transform is never used as the correction field itself.
    """
    if not isinstance(reference,ReferenceGeometry):
        raise TypeError("reference must be a ReferenceGeometry")
    if not isinstance(target,PositionReferenceTarget):
        raise TypeError("target must be a PositionReferenceTarget")
    if target.lattice_indices is None:
        # Without finite-lattice topology, strict site identity is the only exact path.
        return resolve_exact_lattice(reference,target)
    if reference.point_count!=target.point_count:
        raise ValueError("Finite-lattice correspondence requires equal source/target sizes")

    requested=_normalize_correspondence(correspondence)
    options=exact_lattice_correspondence_options(target)
    if requested!="auto" and requested not in options:
        raise ValueError(
            "Reference correspondence '%s' is not valid for this finite lattice" % requested
        )
    matched=np.asarray(target.matched_mask,dtype=bool)
    if int(np.count_nonzero(matched))<3:
        raise RuntimeError(
            "Reference correspondence needs at least three genuinely localized spots"
        )
    orderings=(
        (SpotOrdering.normalize(requested),) if requested!="auto"
        else tuple(SpotOrdering.normalize(item) for item in options if item!="auto")
    )
    candidates=[]
    current=np.asarray(target.measured_positions_px,dtype=np.float64)
    for ordering in orderings:
        permutation=spot_ordering_permutation(target.lattice_indices,ordering)
        candidate=np.asarray(reference.positions_px,dtype=np.float64)[:,permutation]
        linear,_translation,rms=_fit_affine(current[:,matched],candidate[:,matched])
        metrics=_transform_metrics(linear,rms)
        score=(
            1 if metrics["reflection"] else 0,
            abs(float(metrics["rotation_deg"])),float(metrics["deformation_score"]),
            float(rms),ordering.value,
        )
        candidates.append((score,ordering,permutation,metrics))
    if not candidates:
        raise RuntimeError("No valid reference correspondence is available")
    candidates.sort(key=lambda item:item[0])
    _score,resolved,permutation,metrics=candidates[0]
    details={
        "requested":requested,"resolved":resolved.value,
        "resolved_label":spot_ordering_label(resolved),
        "relative_rotation_deg":float(metrics["rotation_deg"]),
        "rms_px":float(metrics["rms_px"]),
        "reflection":bool(metrics["reflection"]),
        "singular_values":tuple(float(v) for v in metrics["singular_values"]),
        "deformation_score":float(metrics["deformation_score"]),
        "candidate_count":len(candidates),
    }
    return _resolved_from_permutation(
        reference,target,permutation,correspondence=details,
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
    count=target.point_count
    desired=np.array(target.measured_positions_px,dtype=np.float64,copy=True)
    source_indices=np.full(count,-1,dtype=np.int64)
    valid=np.zeros(count,dtype=bool)
    resolved_weights=None if reference.weights is None else np.zeros(count,dtype=np.float64)
    present=np.zeros(reference.point_count,dtype=bool)
    missing=[]
    for target_index,site_id in enumerate(target.site_ids):
        source_index=lookup.get(site_id)
        if source_index is None:
            missing.append(site_id); continue
        present[source_index]=True
        source_indices[target_index]=source_index
        desired[:,target_index]=reference.positions_px[:,source_index]
        valid[target_index]=bool(reference.valid_mask[source_index])
        if resolved_weights is not None:
            resolved_weights[target_index]=float(reference.weights[source_index])
    unused=tuple(reference.site_ids[index] for index in np.flatnonzero(~present))
    return ResolvedPositionReference(
        desired_positions_px=desired,valid_mask=valid,weights=resolved_weights,
        source_geometry=reference,source_indices=source_indices,
        correspondence={"mode":"exact_lattice","source_indices":tuple(int(v) for v in source_indices)},
        diagnostics={
            "resolver":"exact_lattice","source_count":reference.point_count,
            "target_count":target.point_count,
            "matched_site_count":int(np.count_nonzero(source_indices>=0)),
            "valid_site_count":int(np.count_nonzero(valid)),
            "missing_target_site_ids":tuple(missing),"unused_source_site_ids":unused,
        },
    )


__all__=[
    "exact_lattice_correspondence_options","resolve_exact_lattice",
    "resolve_exact_lattice_correspondence",
]
