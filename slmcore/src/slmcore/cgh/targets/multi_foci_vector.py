"""Continuous multi-foci target for direct-summation computation."""

from __future__ import annotations

import numpy as np

from .base import Target
from .lattice import (
    LatticeDefinition,
    lattice_param_specs,
    rasterize_spots,
    reconcile_lattice_params,
    validate_lattice_params,
)
from .resolution import TargetResolution
from ..pattern_geometry import LatticeTargetGeometry
from ..feedback import FeedbackCapability
from ...engine.registry import (
    TargetPresentation,
    TargetPresentationField,
    TargetPresentationFieldKind,
    register_target,
)
from ...engine.parameters import apply_param_links

@register_target()
class MultiFociVectorTarget(Target):
    """Multi-foci lattice retaining continuous normalized Fourier positions."""

    target_type = "multi_foci_vector"
    algorithm = "direct_summation"
    feedback_capabilities = (
        FeedbackCapability.INTENSITY,
        FeedbackCapability.POSITION_CORRECTION,
    )
    presentation = TargetPresentation(
        title="Multi Foci",
        summary_fields=(
            TargetPresentationField(
                key="foci_count",
                parameter_keys=("n_foci_x","n_foci_y"),
                kind=TargetPresentationFieldKind.DIMENSIONS,
                label="Foci",
            ),
            TargetPresentationField(
                key="period_x",
                parameter_keys=("period_x_px",),
                label="Period X",
                compact_label="Px",
            ),
            TargetPresentationField(
                key="period_y",
                parameter_keys=("period_y_px",),
                label="Period Y",
                compact_label="Py",
            ),
        ),
    )

    target_params = lattice_param_specs(("rotation_deg", "skew_deg", "stagger"))

    @classmethod
    def validate_params(cls, params):
        """Validate parameter types and canonical lattice consistency."""
        super().validate_params(params)
        validate_lattice_params(params)

    @classmethod
    def canonicalize_params(
        cls,params,changed_keys,context=None,lock_state=None,
    ):
        """Canonicalize semantic lattice dependencies without runtime preparation."""
        resolved = reconcile_lattice_params(params,changed_keys)
        resolved = apply_param_links(
            resolved,
            cls.target_params,
            conversion_context=(
                None if context is None else context.calibration
            ),
        )
        validate_lattice_params(resolved)
        return resolved, None

    @property
    def n_foci_x(self):
        """Return the number of foci along X."""
        return int(self.params["n_foci_x"])

    @property
    def n_foci_y(self):
        """Return the number of foci along Y."""
        return int(self.params["n_foci_y"])

    @property
    def rotation_deg(self):
        """Return the global lattice rotation angle."""
        return float(self.params.get("rotation_deg", 0.0))

    @property
    def skew_deg(self):
        """Return the lattice basis skew angle."""
        return float(self.params.get("skew_deg", 0.0))

    @property
    def stagger(self):
        """Return the odd-row X shift as a fraction of X period."""
        return float(self.params.get("stagger", 0.0))

    def _build_resolution_impl(self):
        """Resolve exact continuous positions and a common section preview."""
        lattice = LatticeDefinition.from_params(self.params)
        indices = lattice.lattice_indices()
        positions = lattice.spot_positions_kxy()
        intensities = np.ones(lattice.n_spots,dtype=np.float64)
        preview = rasterize_spots(
            positions,
            intensities,
            self.context.shape,
            strict=False,
        )

        return TargetResolution(
            section_shape=self.context.shape,
            target_signature=self.signature,
            canonical_params=self.params,
            effective_params=self.params,
            adjustments=(),
            lattice_indices=indices,
            ideal_spot_positions_kxy=positions,
            spot_positions_kxy=positions,
            spot_intensities=intensities,
            preview=preview,
            target_array=None,
            geometry=LatticeTargetGeometry(
                period_x_reference_px=lattice.period_x_px,
                period_y_reference_px=lattice.period_y_px,
                stagger=lattice.stagger,
                count_x=lattice.n_foci_x,
                count_y=lattice.n_foci_y,
            ),
            details={"position_mode": "continuous"},
        )

    def _with_resolution_updates(
        self,
        base_resolution,
        *,
        spot_positions_kxy,
        spot_intensities,
    ):
        """Render a continuous target preview for adapted spot data."""
        preview = rasterize_spots(
            spot_positions_kxy,
            spot_intensities,
            base_resolution.preview.shape,
            strict=False,
        )
        return TargetResolution(
            section_shape=base_resolution.section_shape,
            target_signature=self.signature,
            canonical_params=base_resolution.canonical_params,
            effective_params=base_resolution.effective_params,
            adjustments=base_resolution.adjustments,
            lattice_indices=base_resolution.lattice_indices,
            ideal_spot_positions_kxy=(
                base_resolution.ideal_spot_positions_kxy
            ),
            spot_positions_kxy=spot_positions_kxy,
            spot_intensities=spot_intensities,
            preview=preview,
            target_array=None,
            geometry=base_resolution.geometry,
            details=base_resolution.details,
        )

    def create_target_name(self):
        """Return a compact name describing the continuous lattice geometry."""
        name = (
            f"mfvec_{self.n_foci_x}x{self.n_foci_y}foci_"
            f"Px{self._fmt_float(self.params['period_x_px'])}-"
            f"Py{self._fmt_float(self.params['period_y_px'])}"
        )

        if self.rotation_deg:
            name += f"_rot{self._fmt_float(self.rotation_deg)}deg"
        if self.skew_deg:
            name += f"_skew{self._fmt_float(self.skew_deg)}deg"
        if self.stagger:
            name += f"_stagg{self._fmt_float(self.stagger)}"
        return name

    @staticmethod
    def _fmt_float(value, precision=5):
        """Return compact deterministic formatting for target names."""
        return f"{float(value):.{precision}g}".replace("+", "")
