from __future__ import annotations

from abc import ABC, abstractmethod
from copy import copy,deepcopy
from typing import Any,Mapping

import numpy as np

from ...engine.parameters import ParamSpec
from ...engine.section.context import SectionContext
from ..feedback import FeedbackCapability
from ..signature import compute_target_definition_signature
from .resolution import TargetResolution

_UNSET = object()


class Target(ABC):
    """Base lifecycle for every persistent CGH target."""

    target_type: str = None
    algorithm: str = None
    target_params: Mapping[str, ParamSpec] = {}
    feedback_capabilities: tuple[FeedbackCapability, ...] = ()


    def __init_subclass__(cls, **kwargs):
        """Validate the static registration contract of concrete targets."""
        super().__init_subclass__(**kwargs)

        if getattr(cls, "target_type", None) is None:
            return

        params = cls.__dict__.get("target_params", None)
        if not isinstance(params, Mapping) or not params:
            raise TypeError(
                f"{cls.__name__}.target_params must be a non-empty "
                "Mapping[str,ParamSpec]."
            )

        for key, spec in params.items():
            if not isinstance(key, str) or not key.strip():
                raise TypeError(
                    f"{cls.__name__}.target_params keys must be non-empty strings."
                )
            if not isinstance(spec, ParamSpec):
                raise TypeError(
                    f"{cls.__name__}.target_params['{key}'] must be a "
                    f"ParamSpec, got {type(spec).__name__}."
                )

        algorithm = cls.__dict__.get("algorithm", None)
        if not isinstance(algorithm, str) or not algorithm:
            raise TypeError(
                f"{cls.__name__} must define algorithm as a non-empty string."
            )


    def __init__(
        self,
        context: SectionContext,
        prepared_definition: Any | None=None,
        **params,
    ):
        """Initialize one target from canonical parameters and optional preparation."""
        if not isinstance(context,SectionContext):
            raise TypeError(
                f"context must be SectionContext, got {type(context).__name__}"
            )
        validated = self._validated_params(params)
        self.validate_params(validated)

        if prepared_definition is None:
            canonical,prepared_definition = self.canonicalize_params(
                validated,(),context=context,
            )
            canonical = self._validated_params(canonical)
            self.validate_params(canonical)
            if canonical != validated:
                raise ValueError(
                    f"{self.target_type} parameters are not canonical for the "
                    "current section context"
                )

        self.context = context
        self.params = validated
        self.prepared_definition = prepared_definition
        self._signature = self.definition_signature_for(context,validated)
        self.name = None
        self.resolution = None
        self.array = None
        self.rebuild()

    @classmethod
    def validate_params(cls, params: Mapping[str, Any]) -> None:
        """Validate a complete target parameter mapping without mutating it."""
        cls._validated_params(params)

    @classmethod
    def _validated_params(cls, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return a complete mapping converted through the target ParamSpecs."""
        if not isinstance(params, Mapping):
            raise TypeError(
                f"Target parameters must be a mapping, got {type(params).__name__}"
            )

        expected = set(cls.target_params)
        received = set(params)
        if received != expected:
            missing = expected - received
            unknown = received - expected
            parts = []

            if missing:
                parts.append(f"missing parameters: {sorted(missing)}")
            if unknown:
                parts.append(f"unknown parameters: {sorted(unknown)}")

            raise ValueError(
                f"Invalid {cls.target_type} parameters ({', '.join(parts)})"
            )

        converted = {}
        for key, spec in cls.target_params.items():
            try:
                converted[key] = spec.validate(params[key])
            except ValueError as error:
                raise ValueError(f"{key}: {error}") from error

        return converted

    @classmethod
    def create_lock_state(cls):
        """Return optional persisted target-domain lock state."""
        return None

    @classmethod
    def canonicalize_params(
        cls,
        params: Mapping[str,Any],
        changed_keys: tuple[str, ...],
        context: SectionContext | None=None,
        lock_state: Any | None=None,
    ) -> tuple[dict[str, Any], Any | None]:
        """Return canonical parameters and optional prepared target definition."""
        return dict(params),None

    @classmethod
    def definition_signature_payload(
        cls,canonical_params: Mapping[str,Any],
    ) -> Any:
        """Return the semantic payload defining base-target identity.

        Concrete targets may exclude configuration/policy values that only
        influence how the finalized target is resolved. ``canonical_params``
        has already been validated before this hook is called.
        """
        return dict(canonical_params)

    @classmethod
    def definition_signature_for(
        cls,
        context: SectionContext,
        canonical_params: Mapping[str,Any],
    ):
        """Return the identity signature for one finalized target definition."""
        if not isinstance(context,SectionContext):
            raise TypeError(
                f"context must be SectionContext, got {type(context).__name__}"
            )
        params = cls._validated_params(canonical_params)
        cls.validate_params(params)
        payload = cls.definition_signature_payload(params)
        return compute_target_definition_signature(
            context,cls.target_type,payload,
        )

    @property
    def signature(self):
        return self._signature

    @property
    def section_size(self):
        """Return the target section shape as ``(height, width)``."""
        return self.context.shape

    @property
    def section_calibration(self):
        """Return the detached calibration stored in the section context."""
        return self.context.calibration

    @property
    def preview(self):
        """Return the target-specific resolved preview."""
        return self.resolution.preview

    @property
    def spot_vectors_kxy(self):
        """Compatibility alias for final effective spot positions."""
        return self.resolution.spot_positions_kxy

    @property
    def spot_intensities(self):
        """Return final effective relative spot intensities."""
        return self.resolution.spot_intensities

    @property
    def lattice_indices(self):
        """Return stable logical spot identifiers from the current resolution."""
        return self.resolution.lattice_indices


    def clone(self) -> Target:
        """Return a detached clone preserving all current target runtime state."""
        candidate = copy(self)

        # These objects are explicitly immutable/read-only and may be shared.
        shared_fields = {
            "prepared_definition",
            "resolution",
            "array",
        }

        for name,value in self.__dict__.items():
            if name not in shared_fields:
                setattr(candidate,name,deepcopy(value))

        return candidate

    def build(self) -> TargetResolution:
        """Build and validate the target-specific runtime resolution."""
        resolution = self._build_resolution_impl()
        if not isinstance(resolution, TargetResolution):
            raise TypeError(
                f"{type(self).__name__}._build_resolution_impl() must return "
                f"TargetResolution, got {type(resolution).__name__}"
            )
        if resolution.target_signature != self._signature:
            raise ValueError(
                f"{type(self).__name__} returned a TargetResolution with a "
                "different target signature"
            )
        return resolution

    def rebuild(self) -> TargetResolution:
        """Recompute resolution, compatibility array, and target name."""
        self.resolution = self.build()
        self.array = (
            self.resolution.target_array
            if self.resolution.target_array is not None
            else self.resolution.preview
        )
        self.name = self.create_target_name()
        return self.resolution

    def get_target_params(self):
        """Return a detached copy of current canonical target parameters."""
        return dict(self.params)

    def with_resolution_updates(
        self,
        base_resolution: TargetResolution,
        *,
        spot_positions_kxy: Any | None=None,
        spot_intensities: Any | None=None,
    ) -> TargetResolution:
        """Return an immutable resolution with target-supported spot updates.

        Feedback capabilities are the target contract: ``INTENSITY`` authorizes
        changed spot intensities and ``POSITION_CORRECTION`` authorizes changed
        spot positions.  The target remains immutable; concrete subclasses only
        render and return a detached ``TargetResolution``.
        """
        if not isinstance(base_resolution,TargetResolution):
            raise TypeError(
                "base_resolution must be TargetResolution, got "
                f"{type(base_resolution).__name__}"
            )
        if base_resolution.target_signature != self._signature:
            raise RuntimeError(
                "base_resolution belongs to a different target definition"
            )

        positions = self._normalize_resolution_update_array(
            spot_positions_kxy,
            base_resolution.spot_positions_kxy,
            "spot_positions_kxy",
            ndim=2,
        )
        intensities = self._normalize_resolution_update_array(
            spot_intensities,
            base_resolution.spot_intensities,
            "spot_intensities",
            ndim=1,
        )

        positions_changed = not np.array_equal(
            positions,base_resolution.spot_positions_kxy,
        )
        intensities_changed = not np.array_equal(
            intensities,base_resolution.spot_intensities,
        )
        capabilities = tuple(
            FeedbackCapability(item)
            for item in getattr(self,"feedback_capabilities",()) or ()
        )
        if (
            intensities_changed
            and FeedbackCapability.INTENSITY not in capabilities
        ):
            raise RuntimeError(
                f"{type(self).__name__} does not support intensity feedback"
            )
        if (
            positions_changed
            and FeedbackCapability.POSITION_CORRECTION not in capabilities
        ):
            raise RuntimeError(
                f"{type(self).__name__} does not support position correction"
            )

        if not positions_changed and not intensities_changed:
            return base_resolution

        resolution = self._with_resolution_updates(
            base_resolution,
            spot_positions_kxy=positions,
            spot_intensities=intensities,
        )
        if not isinstance(resolution,TargetResolution):
            raise TypeError(
                f"{type(self).__name__}._with_resolution_updates() must return "
                f"TargetResolution, got {type(resolution).__name__}"
            )
        if resolution.target_signature != self._signature:
            raise ValueError(
                f"{type(self).__name__}._with_resolution_updates() returned "
                "a TargetResolution with a different target signature"
            )
        return resolution

    @staticmethod
    def _normalize_resolution_update_array(
        value: Any | None,
        base: Any,
        name: str,
        *,
        ndim: int,
    ):
        array = np.asarray(base if value is None else value,dtype=np.float64)
        if array.ndim != ndim:
            raise ValueError(f"{name} must be {ndim}D, got shape {array.shape}")
        base_array = np.asarray(base,dtype=np.float64)
        if array.shape != base_array.shape:
            raise ValueError(
                f"{name} must have shape {base_array.shape}, got {array.shape}"
            )
        if not np.all(np.isfinite(array)):
            raise ValueError(f"{name} contains non-finite values")
        if name == "spot_intensities":
            if np.any(array < 0):
                raise ValueError("spot_intensities cannot contain negative values")
            if array.size and not np.any(array > 0):
                raise ValueError("spot_intensities must contain a positive value")
        return np.array(array,dtype=np.float64,copy=True)

    @abstractmethod
    def _build_resolution_impl(self) -> TargetResolution:
        """Return the target-specific runtime resolution."""
        raise NotImplementedError

    def _with_resolution_updates(
        self,
        base_resolution: TargetResolution,
        *,
        spot_positions_kxy,
        spot_intensities,
    ) -> TargetResolution:
        """Target-specific adapted-resolution renderer."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement resolution updates"
        )

    @abstractmethod
    def create_target_name(self) -> str:
        """Return the human-readable runtime target name."""
        raise NotImplementedError
