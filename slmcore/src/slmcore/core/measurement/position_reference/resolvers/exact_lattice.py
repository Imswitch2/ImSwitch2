"""Exact finite-lattice correspondence for neutral position references."""

from __future__ import annotations

from collections import Counter
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

_ORDERING_LINEAR={
    SpotOrdering.CANONICAL:np.array([[1.0,0.0],[0.0,1.0]]),
    SpotOrdering.FLIP_X:np.array([[-1.0,0.0],[0.0,1.0]]),
    SpotOrdering.FLIP_Y:np.array([[1.0,0.0],[0.0,-1.0]]),
    SpotOrdering.ROTATE_180:np.array([[-1.0,0.0],[0.0,-1.0]]),
    SpotOrdering.TRANSPOSE:np.array([[0.0,1.0],[1.0,0.0]]),
    SpotOrdering.ROTATE_90_CW:np.array([[0.0,-1.0],[1.0,0.0]]),
    SpotOrdering.ROTATE_90_CCW:np.array([[0.0,1.0],[-1.0,0.0]]),
    SpotOrdering.ANTI_TRANSPOSE:np.array([[0.0,-1.0],[-1.0,0.0]]),
}


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


def _quantized_logical(points: np.ndarray,tolerance: float) -> np.ndarray:
    return np.rint(np.asarray(points,dtype=np.float64)/float(tolerance)).astype(np.int64)


def _topology_anchor_indices(reference: ReferenceGeometry) -> tuple[int,...]:
    logical=np.asarray(reference.lattice.logical_positions,dtype=np.float64)
    count=logical.shape[1]
    anchors={0,count-1}
    for axis in (0,1):
        anchors.add(int(np.argmin(logical[axis])))
        anchors.add(int(np.argmax(logical[axis])))
    basis=np.asarray(reference.lattice.basis_indices,dtype=np.int64)
    for value in np.unique(basis):
        locations=np.flatnonzero(basis==value)
        if locations.size:
            anchors.add(int(locations[0]))
            anchors.add(int(locations[-1]))
    if count>1:
        anchors.update(int(value) for value in np.linspace(0,count-1,min(8,count)))
    return tuple(sorted(anchors))


def _topology_mapping_candidates(
    reference: ReferenceGeometry,
    target: PositionReferenceTarget,
    linear: np.ndarray,
) -> list[tuple[np.ndarray,np.ndarray]]:
    """Return target->source mappings plus logical translations for one orientation.

    The source and target are finite windows cut from periodic lattices.  Therefore
    equivalent motifs may differ by a cell phase and need not have a perfect
    one-to-one finite overlap.  We infer the phase translation that maximizes
    logical-site overlap and retain several best candidates for detector-space
    scoring.
    """
    source_logical=np.asarray(reference.lattice.logical_positions,dtype=np.float64)
    target_logical=np.asarray(target.lattice.logical_positions,dtype=np.float64)
    transformed=np.asarray(linear,dtype=np.float64)@source_logical
    span=max(
        1.0,
        float(np.max(np.ptp(source_logical,axis=1))),
        float(np.max(np.ptp(target_logical,axis=1))),
    )
    tolerance=1e-7*span
    target_q=_quantized_logical(target_logical,tolerance)

    # Candidate phase shifts are differences between a small topology-aware set
    # of source anchors and all target sites.  Keep an exact floating representative
    # for each quantized phase: adding separately rounded source + translation keys
    # can otherwise lose half-cell sites through one-unit rounding at finite edges.
    counts: Counter[tuple[int,int]]=Counter()
    representatives: dict[tuple[int,int],np.ndarray]={}
    for source_index in _topology_anchor_indices(reference):
        differences=target_logical-transformed[:,source_index,None]
        difference_q=_quantized_logical(differences,tolerance)
        for index,(qx,qy) in enumerate(difference_q.T):
            key=(int(qx),int(qy))
            counts[key]+=1
            representatives.setdefault(key,np.asarray(differences[:,index],dtype=np.float64))
    if not counts:
        return []

    target_lookup={
        (int(target_q[0,index]),int(target_q[1,index])):index
        for index in range(target_q.shape[1])
    }
    # Evaluate the most repeatedly suggested phases plus enough alternatives to
    # cover cases where one selected finite edge anchor happens to be unmatched.
    phase_candidates=sorted(
        counts.items(),
        key=lambda item:(-item[1],item[0][0]*item[0][0]+item[0][1]*item[0][1],item[0]),
    )[:64]
    evaluated=[]
    for phase_key,_frequency in phase_candidates:
        translation=representatives[phase_key]
        shifted_q=_quantized_logical(
            transformed+translation[:,None],tolerance,
        )
        source_indices=np.full(target.point_count,-1,dtype=np.int64)
        for source_index in range(reference.point_count):
            key=(int(shifted_q[0,source_index]),int(shifted_q[1,source_index]))
            target_index=target_lookup.get(key)
            if target_index is not None:
                source_indices[target_index]=source_index
        count=int(np.count_nonzero(source_indices>=0))
        evaluated.append((
            -count,float(np.dot(translation,translation)),phase_key[0],phase_key[1],
            source_indices,np.asarray(translation,dtype=np.float64),
        ))
    evaluated.sort(key=lambda item:item[:4])
    if not evaluated:
        return []
    best_count=-evaluated[0][0]
    # Different cell phases can have the same maximum finite overlap (notably
    # +stagger vs -stagger). Keep all best-overlap phases for detector scoring.
    return [
        (item[4],item[5]) for item in evaluated
        if -item[0]==best_count
    ]


def _combined_permutation(
    lattice_indices: np.ndarray,
    source_ordering: SpotOrdering,
    correspondence_ordering: SpotOrdering,
) -> np.ndarray:
    source=spot_ordering_permutation(lattice_indices,source_ordering)
    correspondence=spot_ordering_permutation(lattice_indices,correspondence_ordering)
    return np.asarray(source,dtype=np.int64)[np.asarray(correspondence,dtype=np.int64)]


def _detector_frame_ordering(
    logical_positions: np.ndarray,
    detector_positions: np.ndarray,
    valid_mask: np.ndarray,
) -> SpotOrdering:
    """Choose a D4 coordinate frame whose detector basis is camera-canonical.

    This differs from localization ``available_spot_orderings``: no detector
    points are permuted here.  We only choose the abstract lattice coordinate
    frame used for source/target correspondence.  That lets a staggered lattice
    use the same camera convention as an unstaggered lattice even when the
    finite stagger motif itself does not admit every D4 permutation as a strict
    self-symmetry.
    """
    logical=np.asarray(logical_positions,dtype=np.float64)
    detector=np.asarray(detector_positions,dtype=np.float64)
    valid=np.asarray(valid_mask,dtype=bool)
    if logical.shape!=detector.shape or logical.ndim!=2 or logical.shape[0]!=2:
        raise ValueError("Logical and detector positions must share shape (2, N)")
    if valid.shape!=(logical.shape[1],):
        raise ValueError("Detector-frame valid mask must align with positions")
    if int(np.count_nonzero(valid))<3:
        return SpotOrdering.CANONICAL

    def wrapped_abs_degrees(value: float) -> float:
        return abs((float(value)+180.0)%360.0-180.0)

    best=None
    for priority,ordering in enumerate(SpotOrdering):
        transformed=_ORDERING_LINEAR[ordering]@logical[:,valid]
        linear,_translation,rms=_fit_affine(transformed,detector[:,valid])
        x_axis=linear[:,0]
        y_axis=linear[:,1]
        angle_x=wrapped_abs_degrees(np.degrees(np.arctan2(x_axis[1],x_axis[0])))
        handedness_penalty=0 if float(np.linalg.det(linear))>=0.0 else 1
        angle_y=wrapped_abs_degrees(
            np.degrees(np.arctan2(y_axis[1],y_axis[0]))-90.0
        )
        score=(
            round(float(angle_x),9),handedness_penalty,
            round(float(angle_y),9),round(float(rms),9),priority,
        )
        if best is None or score<best[0]:
            best=(score,ordering)
    return SpotOrdering.CANONICAL if best is None else best[1]

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

    topology_available=reference.lattice is not None and target.lattice is not None
    source_frame=SpotOrdering.CANONICAL
    target_frame=SpotOrdering.CANONICAL
    if topology_available:
        source_frame=_detector_frame_ordering(
            np.asarray(reference.lattice.logical_positions,dtype=np.float64),
            np.asarray(reference.positions_px,dtype=np.float64),
            np.asarray(reference.valid_mask,dtype=bool),
        )
        target_frame=_detector_frame_ordering(
            np.asarray(target.lattice.logical_positions,dtype=np.float64),
            np.asarray(target.measured_positions_px,dtype=np.float64),
            np.asarray(target.matched_mask,dtype=bool),
        )
        target_inverse=np.linalg.inv(_ORDERING_LINEAR[target_frame])

    for ordering in orderings:
        if topology_available:
            # User-visible correspondence is defined in one common detector/camera
            # frame.  Localization may have numbered source and target lattices in
            # different D4 conventions (especially for signed stagger, where not
            # every rectangular permutation is a finite self-symmetry).  Normalize
            # those conventions here before applying the requested correspondence.
            linear=(
                target_inverse
                @_ORDERING_LINEAR[ordering]
                @_ORDERING_LINEAR[source_frame]
            )
            mappings=_topology_mapping_candidates(reference,target,linear)
        else:
            mappings=[(
                _combined_permutation(target.lattice_indices,fixed_source,ordering),
                np.zeros(2,dtype=np.float64),
            )]
        for source_indices,logical_translation in mappings:
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
            # Geometry is authoritative for reference correspondence. Reflection
            # and large relative rotations are valid between different SLM/camera
            # conventions and are therefore tie-breakers rather than hard priors.
            ordering_priority={value:index for index,value in enumerate(SpotOrdering)}[ordering]
            score=(
                1 if metrics["reflection"] else 0,
                round(abs(float(metrics["rotation_deg"])),6),
                round(float(metrics["deformation_score"]),8),
                round(float(rms),6),-fit_count,
                round(median_displacement,6),ordering_priority,
                float(np.linalg.norm(logical_translation)),
            )
            candidates.append((
                score,ordering,source_indices,logical_translation,metrics,fit_count,
                median_displacement,
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
        "source_detector_frame":source_frame.value,
        "target_detector_frame":target_frame.value,
        "relative_rotation_deg":float(metrics["rotation_deg"]),
        "rms_px":float(metrics["rms_px"]),
        "reflection":bool(metrics["reflection"]),
        "singular_values":tuple(float(v) for v in metrics["singular_values"]),
        "deformation_score":float(metrics["deformation_score"]),
        "candidate_count":len(candidates),
        "fit_count":fit_count,
        "median_displacement_px":median_displacement,
        "logical_translation":tuple(float(v) for v in logical_translation),
        "topology_aware":bool(topology_available),
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
