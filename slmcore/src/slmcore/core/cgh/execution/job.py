from __future__ import annotations

from dataclasses import dataclass, field


import numpy as np

from ..computations.initialization import validate_initial_field
from ..computations.types import CGHAlgorithmOutput,CGHComputeFunction
from ..targets.resolution import TargetResolution
from .errors import CGHComputationError
from .result import CGHResult
from .session_model import CGHPreparedRequest
from .spec import CGHSpec


@dataclass(frozen=True)
class CGHJob:
    """Detached executable CGH computation using an immutable target resolution."""

    generation: int
    spec: CGHSpec
    target_name: str
    resolution: TargetResolution = field(repr=False, compare=False)
    compute_func: CGHComputeFunction = field(repr=False, compare=False)
    initial_field: np.ndarray | None = field(
        default=None, repr=False, compare=False)
    prepared_request: CGHPreparedRequest | None = field(
        default=None,repr=False,compare=False)
    fixed_target_phase: np.ndarray | None = field(
        default=None,repr=False,compare=False)

    def __post_init__(self) -> None:
        """Detach the optional initialization phase from runtime-owned arrays."""
        generation = int(self.generation)
        if generation <= 0:
            raise ValueError("CGHJob.generation must be >= 1")
        if not isinstance(self.spec,CGHSpec):
            raise TypeError("CGHJob.spec must be a CGHSpec instance")
        if not isinstance(self.resolution, TargetResolution):
            raise TypeError(
                "CGHJob.resolution must be a TargetResolution instance"
            )
        if self.resolution.section_shape != self.spec.context.shape:
            raise ValueError(
                "CGHJob resolution shape does not match specification context: "
                f"{self.resolution.section_shape} != {self.spec.context.shape}"
            )
        if not callable(self.compute_func):
            raise TypeError("CGHJob.compute_func must be callable")

        target_name = str(self.target_name or self.spec.target_type).strip()
        if not target_name:
            raise ValueError("CGHJob.target_name cannot be empty")

        initial_field = self.initial_field
        if initial_field is not None:
            initial_field = validate_initial_field(
                initial_field,self.spec.context.shape
            )

        fixed_target_phase = self.fixed_target_phase
        if fixed_target_phase is not None:
            fixed_target_phase = np.asarray(fixed_target_phase,dtype=np.float64)
            if fixed_target_phase.ndim != 2 or not np.all(np.isfinite(fixed_target_phase)):
                raise ValueError("CGHJob.fixed_target_phase must be a finite 2D array")
            fixed_target_phase = np.array(fixed_target_phase,copy=True)
            fixed_target_phase.setflags(write=False)

        prepared_request = self.prepared_request
        if (
            prepared_request is not None
            and not isinstance(prepared_request,CGHPreparedRequest)
        ):
            raise TypeError(
                "CGHJob.prepared_request must be CGHPreparedRequest or None"
            )

        object.__setattr__(self,"generation",generation)
        object.__setattr__(self,"target_name",target_name)
        object.__setattr__(self,"initial_field",initial_field)
        object.__setattr__(self,"fixed_target_phase",fixed_target_phase)

    def run(self) -> CGHResult:
        """Execute the registered algorithm and return a validated CGH result."""
        try:
            if self.fixed_target_phase is None:
                output = self.compute_func(
                    self.resolution,self.spec.compute_params,self.initial_field,
                )
            else:
                output = self.compute_func(
                    self.resolution,self.spec.compute_params,self.initial_field,
                    fixed_target_phase=self.fixed_target_phase,
                )
        except Exception as error:
            raise CGHComputationError(
                f"CGH algorithm '{self.spec.algorithm}' failed: {error}"
            ) from error

        if not isinstance(output,CGHAlgorithmOutput):
            raise CGHComputationError(
                f"CGH algorithm '{self.spec.algorithm}' returned "
                f"{type(output).__name__}; expected CGHAlgorithmOutput"
            )

        return CGHResult(
            generation=self.generation,
            spec=self.spec,
            target_name=self.target_name,
            pattern=output.pattern,
            target_phase=output.target_phase,
            metrics=output.metrics,
            warnings=output.warnings,
            diagnostics=output.diagnostics,
        )
