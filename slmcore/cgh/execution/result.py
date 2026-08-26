from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field,replace
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..computations.types import CGHIterationMetrics
from .errors import InvalidCGHResultError
from .spec import CGHSpec

@dataclass(frozen=True)
class CGHResult:
    """Validated session result completed from one typed algorithm output."""

    generation: int
    spec: CGHSpec
    target_name: str
    pattern: np.ndarray
    metrics: tuple[CGHIterationMetrics, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        generation = int(self.generation)
        if generation <= 0:
            raise InvalidCGHResultError(
                "CGH result generation must be >= 1"
            )
        if not isinstance(self.spec,CGHSpec):
            raise InvalidCGHResultError("CGH result spec must be CGHSpec")

        target_name = str(self.target_name or self.spec.target_type).strip()
        if not target_name:
            raise InvalidCGHResultError("CGH result target_name cannot be empty")

        pattern = np.asarray(self.pattern)
        if pattern.shape != self.spec.context.shape:
            raise InvalidCGHResultError(
                f"CGH pattern shape {pattern.shape}; expected "
                f"{self.spec.context.shape}"
            )
        if not np.iscomplexobj(pattern):
            raise InvalidCGHResultError(
                "CGH pattern must be a complex phase field"
            )
        if not np.all(np.isfinite(pattern)):
            raise InvalidCGHResultError(
                "CGH pattern contains non-finite values"
            )
        if not np.allclose(np.abs(pattern),1.0,rtol=0.0,atol=1e-6):
            raise InvalidCGHResultError(
                "CGH pattern must have unit amplitude"
            )

        pattern = np.array(pattern,dtype=np.complex128,copy=True)
        pattern.setflags(write=False)

        metrics = tuple(self.metrics or ())
        for metric in metrics:
            if not isinstance(metric,CGHIterationMetrics):
                raise InvalidCGHResultError(
                    "CGH result metrics must contain CGHIterationMetrics"
                )

        warnings = tuple(
            warning for warning in (
                str(value).strip() for value in (self.warnings or ())
            ) if warning
        )

        diagnostics = self.diagnostics
        if diagnostics is None:
            diagnostics = {}
        if not isinstance(diagnostics,Mapping):
            raise InvalidCGHResultError(
                "CGH result diagnostics must be a mapping"
            )
        diagnostics = MappingProxyType(deepcopy(dict(diagnostics)))

        object.__setattr__(self,"generation",generation)
        object.__setattr__(self,"target_name",target_name)
        object.__setattr__(self,"pattern",pattern)
        object.__setattr__(self,"metrics",metrics)
        object.__setattr__(self,"warnings",warnings)
        object.__setattr__(self,"diagnostics",diagnostics)

    def clone(self) -> "CGHResult":
        """Return a fully detached copy safe for application consumers."""
        spec = CGHSpec(
            context=replace(self.spec.context),
            target_type=self.spec.target_type,
            algorithm=self.spec.algorithm,
            target_params=self.spec.target_params,
            compute_params=self.spec.compute_params,
            feedback_target_signature=self.spec.feedback_target_signature,
        )
        return type(self)(
            generation=self.generation,
            spec=spec,
            target_name=self.target_name,
            pattern=self.pattern,
            metrics=self.metrics,
            warnings=self.warnings,
            diagnostics=self.diagnostics,
        )

    @property
    def target_type(self) -> str:
        return self.spec.target_type

    @property
    def algorithm(self) -> str:
        return self.spec.algorithm
