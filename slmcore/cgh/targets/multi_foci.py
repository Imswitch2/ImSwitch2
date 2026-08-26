"""Rasterized multi-foci target for Gerchberg-Saxton computation."""

from __future__ import annotations

import numpy as np

from .base import Target
from .lattice import (
    LatticeAxisIntent,
    LatticeDefinition,
    LatticeLockState,
    LatticeResolutionIntent,
    reconcile_lattice_params_with_intent,
    validate_lattice_params,
)
from .raster_lattice import (
    RasterResolutionPolicy,
    ResolvedRasterLattice,
    raster_lattice_param_specs,
    resolve_raster_lattice,
    validate_raster_resolution_params,
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
class MultiFociTarget(Target):
    """Regular multi-foci lattice resolved onto an internal integer grid."""

    target_type = "multi_foci"
    algorithm = "gerchberg_saxton"
    feedback_capabilities = (
        FeedbackCapability.INTENSITY,
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

    target_params = raster_lattice_param_specs(("stagger",))

    @classmethod
    def validate_params(cls,params):
        """Validate context-independent canonical lattice values and policy."""
        super().validate_params(params)
        validate_lattice_params(params)
        validate_raster_resolution_params(params)

    @classmethod
    def create_lock_state(cls):
        return LatticeLockState()

    @classmethod
    def definition_signature_payload(cls,canonical_params):
        """Identify only the finalized lattice, not its resolver policy."""
        lattice = LatticeDefinition.from_params(canonical_params)
        return {
            "period_x_px":lattice.period_x_px,
            "period_y_px":lattice.period_y_px,
            "n_foci_x":lattice.n_foci_x,
            "n_foci_y":lattice.n_foci_y,
            "rotation_deg":lattice.rotation_deg,
            "skew_deg":lattice.skew_deg,
            "stagger":lattice.stagger,
        }

    @classmethod
    def canonicalize_params(
        cls,params,changed_keys,context=None,lock_state=None,
    ):
        """Resolve one exact raster-compatible canonical target definition."""
        if context is None:
            raise ValueError("Multi-foci canonicalization requires SectionContext")

        if lock_state is not None and not isinstance(lock_state,LatticeLockState):
            raise TypeError("multi_foci lock state must be LatticeLockState")
        semantic_params,intent = reconcile_lattice_params_with_intent(
            params,changed_keys,lock=lock_state,
        )
        semantic_params = apply_param_links(
            semantic_params,
            cls.target_params,
            conversion_context=context.calibration,
        )
        # Square is a hard source-X -> target-Y constraint. Once links have
        # been applied, Y raster scoring must follow the linked semantic values
        # rather than any pre-link Y draft/lock intent.
        if bool(semantic_params.get("square",False)):
            intent = LatticeResolutionIntent(
                x=intent.x,
                y=LatticeAxisIntent(
                    period_px=float(semantic_params["period_y_px"]),
                    n_foci=int(semantic_params["n_foci_y"]),
                    fov_px=float(semantic_params["fov_y_px"]),
                ),
            )
        validate_lattice_params(semantic_params)
        policy = RasterResolutionPolicy.from_params(semantic_params)
        resolved = resolve_raster_lattice(
            LatticeDefinition.from_params(semantic_params),
            context.shape,
            policy,
            intent=intent,
        )

        canonical = resolved.lattice.to_params(semantic_params)
        validate_lattice_params(canonical)
        return canonical,resolved
    
    @property
    def n_foci_x(self):
        """Return the number of foci along X."""
        return int(self.params["n_foci_x"])

    @property
    def n_foci_y(self):
        """Return the number of foci along Y."""
        return int(self.params["n_foci_y"])

    @property
    def npx(self):
        """Compatibility alias for the X number of foci."""
        return self.n_foci_x

    @property
    def npy(self):
        """Compatibility alias for the Y number of foci."""
        return self.n_foci_y

    @property
    def stagger(self):
        """Return the odd-row X shift as a fraction of X period."""
        return float(self.params.get("stagger", 0.0))

    def _build_resolution_impl(self):
        """Resolve the canonical lattice using uniform initial intensities."""
        intensities = np.ones(self.n_foci_x * self.n_foci_y,dtype=np.float64)
        return self._resolution_from_intensities(intensities)

    def _with_resolution_updates(
        self,
        base_resolution,
        *,
        spot_positions_kxy,
        spot_intensities,
    ):
        """Render an intensity-adapted exact-raster target resolution."""
        if not np.array_equal(
            spot_positions_kxy,base_resolution.spot_positions_kxy,
        ):
            raise RuntimeError(
                "MultiFociTarget does not support position-corrected "
                "target resolutions"
            )
        return self._resolution_from_intensities(spot_intensities)

    def _resolution_from_intensities(self,intensities):
        """Render relative intensities on the prepared exact raster definition."""
        resolved = self.prepared_definition
        if not isinstance(resolved,ResolvedRasterLattice):
            raise RuntimeError(
                "MultiFociTarget requires a ResolvedRasterLattice definition"
            )
        if resolved.section_shape != self.context.shape:
            raise RuntimeError(
                "Resolved raster definition does not match the section shape"
            )
        if resolved.lattice.to_params(self.params) != self.params:
            raise RuntimeError(
                "Resolved raster definition does not match canonical parameters"
            )

        intensities = np.asarray(intensities,dtype=np.float64)
        target_array = resolved.render_internal(intensities)

        return TargetResolution(
            section_shape=self.context.shape,
            target_signature=self.signature,
            canonical_params=self.params,
            effective_params=self.params,
            adjustments=(),
            lattice_indices=resolved.lattice_indices,
            ideal_spot_positions_kxy=resolved.lattice.spot_positions_kxy(),
            spot_positions_kxy=resolved.spot_positions_kxy,
            spot_intensities=intensities,
            preview=target_array,
            target_array=target_array,
            geometry=LatticeTargetGeometry(
                period_x_reference_px=resolved.lattice.period_x_px,
                period_y_reference_px=resolved.lattice.period_y_px,
                stagger=resolved.lattice.stagger,
                count_x=resolved.lattice.n_foci_x,
                count_y=resolved.lattice.n_foci_y,
            ),
            details=resolved.details(),
        )

    def create_target_name(self):
        """Return a name containing canonical geometry and resolved grid size."""
        target_shape = self.resolution.details.get("target_shape")
        target_text = (
            f"trgt{target_shape[1]}"
            if target_shape[0] == target_shape[1]
            else f"trgt{target_shape[1]}x{target_shape[0]}"
        )
        period_text = (
            f"P{self._fmt_float(self.params['period_x_px'])}"
            if self.params["period_x_px"] == self.params["period_y_px"]
            else (
                f"Px{self._fmt_float(self.params['period_x_px'])}-"
                f"Py{self._fmt_float(self.params['period_y_px'])}"
            )
        )
        name = (
            f"mf_{self.n_foci_x}x{self.n_foci_y}foci_"
            f"{period_text}_{target_text}"
        )

        if self.stagger:
            name += f"_stagg{self._fmt_float(self.stagger)}"
        return name

    @staticmethod
    def _fmt_float(value, precision=5):
        """Return compact deterministic formatting for target names."""
        return f"{float(value):.{precision}g}".replace("+", "")
