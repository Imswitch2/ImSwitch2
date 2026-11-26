"""Pure conversions from proven localization results to the shared contracts.

The adapter deliberately accepts *structural* ImProcess/uLenses objects rather
than importing either host package. Host-side reverse adapters reconstruct
legacy objects from the returned primitive components. No localization is run
here, and no prior is silently applied.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .contracts import PatternGeometry, PatternLocalizationResult, PatternPrior
from .model import LatticeRegistration


def from_periodic_localization(
    source: Any, *, prior: PatternPrior | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> PatternLocalizationResult:
    """View an ImProcess ``Lattice`` or ``PatternLocalization`` as periodic.

    The lattice/motif are copied without reducing/reordering the basis. This
    matters for half-period Bravais staggering and non-half-period motifs.
    No periodic sites are enumerated until a consumer supplies a FOV.
    """
    lattice = getattr(source, "lattice", source)
    geometry = PatternGeometry(
        origin_xy=lattice.offset,
        basis_xy=np.column_stack([lattice.a1, lattice.a2]),
        motif_offsets_xy=getattr(source, "motif", ((0., 0.),)),
        extent="periodic",
    )
    return PatternLocalizationResult(
        geometry=geometry, strategy="periodic", prior=prior,
        diagnostics=diagnostics or {},
        provenance=getattr(source, "provenance", {}),
    )


def periodic_components(result: PatternLocalizationResult) -> dict[str, Any]:
    """Lossless primitive values for an ImProcess PatternLocalization adapter."""
    if result.strategy != "periodic":
        raise ValueError("periodic_components requires a periodic result")
    geometry = result.geometry
    return {
        "a1": tuple(float(v) for v in geometry.basis_xy[:, 0]),
        "a2": tuple(float(v) for v in geometry.basis_xy[:, 1]),
        "offset": geometry.origin_xy,
        "motif": geometry.motif_offsets_xy,
        "provenance": dict(result.provenance),
    }


def from_finite_grid(
    source: Any, *, prior: PatternPrior | None = None,
) -> PatternLocalizationResult:
    """View uLenses ``GridLocalizationResult`` (full-image row/col) in XY.

    All site-aligned arrays retain the incoming row-major order.  The original
    ``num_cols``/``num_rows`` are retained separately from finite shape for
    exact reverse construction of the old result object.
    """
    stagger_axis = {None: None, "rows": "x", "columns": "y"}[source.stagger_axis]
    geometry = PatternGeometry(
        origin_xy=tuple(np.asarray(source.origin_rc)[::-1]),
        basis_xy=np.column_stack([
            np.asarray(source.basis_x_rc)[::-1],
            np.asarray(source.basis_y_rc)[::-1],
        ]),
        extent="finite",
        site_indices_ij=source.logical_indices,
        finite_shape_xy=(int(source.n_cols), int(source.n_rows)),
        stagger_axis=stagger_axis,
        stagger_fraction=(float(source.stagger_fraction) if stagger_axis is not None else None),
    )
    return PatternLocalizationResult(
        geometry=geometry,
        strategy="finite",
        predicted_positions_xy=np.asarray(source.grid_points_rc)[:, ::-1],
        measured_positions_xy=np.asarray(source.measured_points_rc)[:, ::-1],
        matched_mask=source.matched_mask,
        detected_positions_xy=np.asarray(source.detected_points_rc)[:, ::-1],
        diagnostics=source.diagnostics,
        prior=prior,
        compatibility={
            "num_cols": int(source.num_cols),
            "num_rows": int(source.num_rows),
        },
    )


def finite_grid_components(result: PatternLocalizationResult) -> dict[str, Any]:
    """Fields for reconstituting a uLenses ``GridLocalizationResult`` exactly.

    This reverse conversion is intentionally specific to ``from_finite_grid``:
    a generic LatticeRegistration has different diagnostics and does not have
    the legacy uLenses public count fields.
    """
    if result.strategy != "finite" or "num_cols" not in result.compatibility:
        raise ValueError("finite_grid_components requires a uLenses-derived finite result")
    if result.measured_positions_xy is None or result.matched_mask is None:
        raise ValueError("uLenses round-trip requires measurements and mask")
    geom = result.geometry
    return {
        "origin_rc": np.asarray(geom.origin_xy)[::-1].copy(),
        "basis_x_rc": geom.basis_xy[:, 0][::-1].copy(),
        "basis_y_rc": geom.basis_xy[:, 1][::-1].copy(),
        "n_cols": geom.finite_shape_xy[0],
        "n_rows": geom.finite_shape_xy[1],
        "stagger_axis": {None: None, "x": "rows", "y": "columns"}[geom.stagger_axis],
        "stagger_fraction": (geom.stagger_fraction if geom.stagger_fraction is not None else 0.0),
        "grid_points_rc": result.predicted_positions_xy[:, ::-1].copy(),
        "logical_indices": geom.site_indices_ij.copy(),
        "detected_points_rc": result.detected_positions_xy[:, ::-1].copy(),
        "matched_mask": result.matched_mask.copy(),
        "measured_points_rc": result.measured_positions_xy[:, ::-1].copy(),
        "num_cols": int(result.compatibility["num_cols"]),
        "num_rows": int(result.compatibility["num_rows"]),
        "diagnostics": dict(result.diagnostics),
    }


def from_lattice_registration(
    registration: LatticeRegistration, *, prior: PatternPrior | None = None,
) -> PatternLocalizationResult:
    """Convert finite backend registration, including crop-local to global XY.

    A ``LatticeRegistration`` uses ``(2,N)`` positions relative to its crop and
    mean-centered logical coordinates. Preserve its ``LatticeRepresentation``
    where supplied; never substitute a primitive periodic cell for its stable
    finite logical indices.  Original registration objects remain available at
    their existing API; this is a normalized view rather than an exact reverse
    serializer for all the detector's intermediate arrays.
    """
    if not isinstance(registration, LatticeRegistration):
        raise TypeError("registration must be a LatticeRegistration")
    crop = registration.detections.crop_coord
    crop_origin = np.array([crop[2], crop[0]], dtype=float)
    indices = registration.model.lattice_indices.T
    representation = registration.model.representation
    # Registration's translation is at centered logical origin. Compute the
    # physical image position of the *uncentered* logical (0, 0) instead.
    if representation is not None:
        logical_mean = np.mean(representation.logical_positions, axis=1)
    else:
        logical_mean = np.mean(indices.astype(float), axis=0)
    origin = (registration.affine_translation - registration.affine_linear @ logical_mean + crop_origin)
    shape = None
    if np.all(indices >= 0):
        shape = (int(np.max(indices[:, 0])) + 1, int(np.max(indices[:, 1])) + 1)
    diag = registration.model.diagnostics
    stagger = diag.get("stagger")
    if representation is not None and representation.diagnostics.get("family") == "alternating_rows":
        stagger = representation.diagnostics.get("stagger")
    stagger_axis = "x" if stagger is not None and abs(float(stagger)) > 0.0 else None
    geometry = PatternGeometry(
        origin_xy=tuple(origin), basis_xy=registration.affine_linear,
        extent="finite", site_indices_ij=indices, finite_shape_xy=shape,
        representation=representation,
        stagger_axis=stagger_axis,
        stagger_fraction=(float(stagger) if stagger_axis is not None else None),
    )

    def global_xy(points: np.ndarray) -> np.ndarray:
        return np.asarray(points, dtype=float).T + crop_origin

    diagnostics = dict(registration.diagnostics)
    diagnostics["crop_coord"] = tuple(int(v) for v in crop)
    diagnostics["finite_indices_source"] = diag.get("lattice_size_source", "unknown")
    diagnostics["rms_residual_px"] = float(registration.rms_residual_px)
    diagnostics["residuals_xy"] = np.asarray(registration.residuals_px).T.copy()
    return PatternLocalizationResult(
        geometry=geometry, strategy="finite",
        predicted_positions_xy=global_xy(registration.expected_positions_px),
        measured_positions_xy=global_xy(registration.measured_positions_px),
        matched_mask=registration.matched_mask,
        detected_positions_xy=global_xy(registration.detections.positions_px),
        prior=prior,
        diagnostics=diagnostics,
        compatibility={
            "detection_indices": np.asarray(registration.detection_indices).copy(),
            "reused_previous": bool(registration.reused_previous),
        },
    )
