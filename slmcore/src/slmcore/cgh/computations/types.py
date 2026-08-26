"""Shared typed contracts for registered CGH computation algorithms."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping,Protocol

import numpy as np

from ..targets.resolution import TargetResolution


@dataclass(frozen=True)
class CGHIterationMetrics:
    """Algorithm-independent performance metrics for one completed iteration.

    ``uniformity`` and ``normalized_std`` describe the relative response
    ``measured_intensity / desired_intensity`` over the requested target
    support. ``efficiency`` is only populated when the algorithm can evaluate
    captured target power against total propagated power using a physically
    defined target support.
    """

    iteration: int
    efficiency: float | None
    uniformity: float
    normalized_std: float

    def __post_init__(self) -> None:
        iteration = int(self.iteration)
        if iteration <= 0:
            raise ValueError("CGHIterationMetrics.iteration must be >= 1")

        efficiency = self.efficiency
        if efficiency is not None:
            efficiency = float(efficiency)
            if not np.isfinite(efficiency):
                raise ValueError("CGH efficiency must be finite or None")
            if efficiency < 0.0 or efficiency > 1.0 + 1e-12:
                raise ValueError(
                    f"CGH efficiency must be in [0, 1], got {efficiency}"
                )
            efficiency = min(efficiency,1.0)

        uniformity = float(self.uniformity)
        normalized_std = float(self.normalized_std)
        if not np.isfinite(uniformity):
            raise ValueError("CGH uniformity must be finite")
        if not np.isfinite(normalized_std):
            raise ValueError("CGH normalized_std must be finite")
        if uniformity < 0.0 or uniformity > 1.0 + 1e-12:
            raise ValueError(
                f"CGH uniformity must be in [0, 1], got {uniformity}"
            )
        if normalized_std < 0.0:
            raise ValueError("CGH normalized_std cannot be negative")

        object.__setattr__(self,"iteration",iteration)
        object.__setattr__(self,"efficiency",efficiency)
        object.__setattr__(self,"uniformity",min(uniformity,1.0))
        object.__setattr__(self,"normalized_std",normalized_std)


@dataclass(frozen=True)
class CGHAlgorithmOutput:
    """Detached numerical output returned by every registered CGH algorithm."""

    pattern: np.ndarray
    metrics: tuple[CGHIterationMetrics, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pattern = np.asarray(self.pattern)
        if pattern.ndim != 2:
            raise ValueError(
                f"CGH algorithm pattern must be 2D, got shape {pattern.shape}"
            )
        if not np.iscomplexobj(pattern):
            raise ValueError("CGH algorithm pattern must be a complex field")
        if not np.all(np.isfinite(pattern)):
            raise ValueError("CGH algorithm pattern contains non-finite values")

        pattern = np.array(pattern,dtype=np.complex128,copy=True)
        pattern.setflags(write=False)

        metrics = tuple(self.metrics or ())
        for metric in metrics:
            if not isinstance(metric,CGHIterationMetrics):
                raise TypeError(
                    "CGHAlgorithmOutput.metrics must contain "
                    "CGHIterationMetrics instances"
                )

        warnings = tuple(
            warning for warning in (
                str(value).strip() for value in (self.warnings or ())
            ) if warning
        )

        diagnostics = _freeze_diagnostics(self.diagnostics)

        object.__setattr__(self,"pattern",pattern)
        object.__setattr__(self,"metrics",metrics)
        object.__setattr__(self,"warnings",warnings)
        object.__setattr__(self,"diagnostics",diagnostics)


class CGHComputeFunction(Protocol):
    """Callable contract implemented by every registered CGH algorithm."""

    def __call__(
        self,
        resolution: TargetResolution,
        compute_params: Mapping[str,Any],
        initial_field: np.ndarray | None,
    ) -> CGHAlgorithmOutput:
        ...


def _freeze_diagnostics(value: Mapping[str,Any]) -> Mapping[str,Any]:
    """Return detached lightweight diagnostics and reject numerical arrays."""
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError(
            "CGHAlgorithmOutput.diagnostics must be a mapping, got "
            f"{type(value).__name__}"
        )

    detached = deepcopy(dict(value))
    _validate_diagnostic_value(detached,"diagnostics")
    return MappingProxyType(detached)


def _validate_diagnostic_value(value: Any,path: str) -> None:
    """Reject large mutable numerical payloads from diagnostics."""
    if isinstance(value,np.ndarray):
        raise TypeError(
            f"{path} cannot contain numpy arrays; keep diagnostics lightweight"
        )
    if isinstance(value,Mapping):
        for key,item in value.items():
            _validate_diagnostic_value(item,f"{path}.{key}")
        return
    if isinstance(value,(list,tuple)):
        for index,item in enumerate(value):
            _validate_diagnostic_value(item,f"{path}[{index}]")
