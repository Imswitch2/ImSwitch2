"""Neutral finite-lattice frame and correspondence helpers.

These utilities operate only on :class:`LatticeRepresentation` topology and
2D coordinate arrays.  They are shared by feedback orientation and position-
reference resolution so both workflows use one definition of motif/cell phase
mapping instead of independently permuting rectangular lattice indices.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np

from .lattice_geometry import LatticeRepresentation


_LATTICE_TRANSFORM_MATRICES = {
    "canonical":np.array([[1.0,0.0],[0.0,1.0]]),
    "flip_x":np.array([[-1.0,0.0],[0.0,1.0]]),
    "flip_y":np.array([[1.0,0.0],[0.0,-1.0]]),
    "rotate_180":np.array([[-1.0,0.0],[0.0,-1.0]]),
    "transpose":np.array([[0.0,1.0],[1.0,0.0]]),
    "rotate_90_cw":np.array([[0.0,-1.0],[1.0,0.0]]),
    "rotate_90_ccw":np.array([[0.0,1.0],[-1.0,0.0]]),
    "anti_transpose":np.array([[0.0,-1.0],[-1.0,0.0]]),
}


def normalize_lattice_transform(value: Any) -> str:
    text=str(value or "canonical").strip().lower()
    aliases={
        "identity":"canonical","none":"canonical",
        "flip_horizontal":"flip_x","horizontal_flip":"flip_x",
        "flip_vertical":"flip_y","vertical_flip":"flip_y",
        "180":"rotate_180","90_cw":"rotate_90_cw","90_ccw":"rotate_90_ccw",
    }
    text=aliases.get(text,text)
    if text not in _LATTICE_TRANSFORM_MATRICES:
        raise ValueError("Unknown lattice transform %r" % value)
    return text


def lattice_transform_matrix(value: Any) -> np.ndarray:
    """Return the 2x2 logical-coordinate matrix for one discrete transform."""
    return np.array(
        _LATTICE_TRANSFORM_MATRICES[normalize_lattice_transform(value)],
        dtype=np.float64,copy=True,
    )


def _fit_affine(source: np.ndarray,target: np.ndarray):
    source=np.asarray(source,dtype=np.float64)
    target=np.asarray(target,dtype=np.float64)
    design=np.vstack((source,np.ones((1,source.shape[1])))).T
    coefficients,_,_,_=np.linalg.lstsq(design,target.T,rcond=None)
    linear=coefficients[:2,:].T
    translation=coefficients[2,:]
    residual=target-(linear@source+translation[:,None])
    rms=float(np.sqrt(np.mean(np.sum(residual*residual,axis=0))))
    return linear,translation,rms


def detector_frame_transform(
    lattice: LatticeRepresentation,
    detector_positions: np.ndarray,
    valid_mask: np.ndarray | None=None,
) -> str:
    """Return the D4 logical frame whose basis is closest to camera +X/+Y.

    No detector points are reordered.  This only identifies which discrete
    logical frame the already-localized finite lattice currently uses.  It is
    therefore safe for staggered motifs that do not admit every D4 transform as
    a finite self-symmetry.
    """
    if not isinstance(lattice,LatticeRepresentation):
        raise TypeError("lattice must be a LatticeRepresentation")
    logical=np.asarray(lattice.logical_positions,dtype=np.float64)
    detector=np.asarray(detector_positions,dtype=np.float64)
    if logical.shape!=detector.shape or logical.ndim!=2 or logical.shape[0]!=2:
        raise ValueError("Logical and detector positions must share shape (2, N)")
    if valid_mask is None:
        valid=np.ones(logical.shape[1],dtype=bool)
    else:
        valid=np.asarray(valid_mask,dtype=bool)
    if valid.shape!=(logical.shape[1],):
        raise ValueError("Detector-frame valid mask must align with positions")
    if int(np.count_nonzero(valid))<3:
        return "canonical"

    def wrapped_abs_degrees(value: float) -> float:
        return abs((float(value)+180.0)%360.0-180.0)

    best=None
    for priority,name in enumerate(_LATTICE_TRANSFORM_MATRICES):
        transformed=_LATTICE_TRANSFORM_MATRICES[name]@logical[:,valid]
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
            best=(score,name)
    return "canonical" if best is None else best[1]


def _quantized_logical(points: np.ndarray,tolerance: float) -> np.ndarray:
    return np.rint(np.asarray(points,dtype=np.float64)/float(tolerance)).astype(np.int64)


def _topology_anchor_indices(lattice: LatticeRepresentation) -> tuple[int,...]:
    logical=np.asarray(lattice.logical_positions,dtype=np.float64)
    count=logical.shape[1]
    anchors={0,count-1}
    for axis in (0,1):
        anchors.add(int(np.argmin(logical[axis])))
        anchors.add(int(np.argmax(logical[axis])))
    basis=np.asarray(lattice.basis_indices,dtype=np.int64)
    for value in np.unique(basis):
        locations=np.flatnonzero(basis==value)
        if locations.size:
            anchors.add(int(locations[0]))
            anchors.add(int(locations[-1]))
    if count>1:
        anchors.update(int(value) for value in np.linspace(0,count-1,min(8,count)))
    return tuple(sorted(anchors))


def finite_lattice_mapping_candidates(
    source: LatticeRepresentation,
    target: LatticeRepresentation,
    linear: np.ndarray,
    *,
    best_overlap_only: bool=True,
    max_phases: int=64,
) -> list[tuple[np.ndarray,np.ndarray]]:
    """Return target->source mappings and logical phase translations.

    ``linear`` transforms source logical coordinates into the target logical
    frame.  A global phase translation is then inferred from the finite
    translation-cell + motif topology.  Missing finite-edge sites remain ``-1``;
    they are never cyclically wrapped to the opposite edge.
    """
    if not isinstance(source,LatticeRepresentation) or not isinstance(
        target,LatticeRepresentation
    ):
        raise TypeError("source and target must be LatticeRepresentation values")
    source_logical=np.asarray(source.logical_positions,dtype=np.float64)
    target_logical=np.asarray(target.logical_positions,dtype=np.float64)
    transform=np.asarray(linear,dtype=np.float64)
    if transform.shape!=(2,2) or not np.all(np.isfinite(transform)):
        raise ValueError("linear must be a finite 2x2 matrix")
    transformed=transform@source_logical
    span=max(
        1.0,
        float(np.max(np.ptp(source_logical,axis=1))),
        float(np.max(np.ptp(target_logical,axis=1))),
    )
    tolerance=1e-7*span
    target_q=_quantized_logical(target_logical,tolerance)

    counts: Counter[tuple[int,int]]=Counter()
    representatives: dict[tuple[int,int],np.ndarray]={}
    for source_index in _topology_anchor_indices(source):
        differences=target_logical-transformed[:,source_index,None]
        difference_q=_quantized_logical(differences,tolerance)
        for index,(qx,qy) in enumerate(difference_q.T):
            key=(int(qx),int(qy))
            counts[key]+=1
            representatives.setdefault(
                key,np.asarray(differences[:,index],dtype=np.float64),
            )
    if not counts:
        return []

    target_lookup={
        (int(target_q[0,index]),int(target_q[1,index])):index
        for index in range(target_q.shape[1])
    }
    phase_candidates=sorted(
        counts.items(),
        key=lambda item:(
            -item[1],item[0][0]*item[0][0]+item[0][1]*item[0][1],item[0],
        ),
    )[:max(1,int(max_phases))]
    evaluated=[]
    for phase_key,_frequency in phase_candidates:
        translation=representatives[phase_key]
        shifted_q=_quantized_logical(transformed+translation[:,None],tolerance)
        source_indices=np.full(target_logical.shape[1],-1,dtype=np.int64)
        for source_index in range(source_logical.shape[1]):
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
    if best_overlap_only:
        best_count=-evaluated[0][0]
        evaluated=[item for item in evaluated if -item[0]==best_count]
    return [(item[4],item[5]) for item in evaluated]


__all__=[
    "detector_frame_transform","finite_lattice_mapping_candidates",
    "lattice_transform_matrix","normalize_lattice_transform",
]
