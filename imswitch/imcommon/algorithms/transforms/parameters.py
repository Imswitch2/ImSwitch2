"""Typed parameter metadata for transform calibration.

This is intentionally local to the transform backend for now.  It carries only
what the calibration algorithms and their small Qt editor need: defaults,
validation bounds and human-facing descriptions.  If the pattern proves useful
elsewhere it can later move to a wider ``imcommon`` parameter facility without
changing the transform/calibration contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "ParameterSpec",
    "parameter_defaults",
    "normalize_parameter_values",
]


@dataclass(frozen=True)
class ParameterSpec:
    """Description and basic validation for one calibration parameter."""

    default: Any
    value_type: type
    label: str
    description: str = ""
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    decimals: int | None = None
    choices: tuple[Any, ...] | None = None
    advanced: bool = False

    def normalize(self, value: Any) -> Any:
        """Coerce ``value`` to the declared type and enforce simple bounds."""
        if self.value_type is bool:
            normalized = _as_bool(value)
        elif self.value_type is int:
            # Reject non-integral floats rather than silently truncating them.
            if isinstance(value, float) and not float(value).is_integer():
                raise ValueError(f"{self.label} must be an integer")
            normalized = int(value)
        elif self.value_type is float:
            normalized = float(value)
        elif self.value_type is str:
            normalized = str(value)
        else:
            if not isinstance(value, self.value_type):
                raise TypeError(
                    f"{self.label} must be {self.value_type.__name__}, "
                    f"got {type(value).__name__}"
                )
            normalized = value

        if self.choices is not None and normalized not in self.choices:
            raise ValueError(
                f"{self.label} must be one of {list(self.choices)}, got {normalized!r}"
            )
        if self.minimum is not None and normalized < self.minimum:
            raise ValueError(f"{self.label} must be at least {self.minimum}")
        if self.maximum is not None and normalized > self.maximum:
            raise ValueError(f"{self.label} must be at most {self.maximum}")
        return normalized


def parameter_defaults(specs: Mapping[str, ParameterSpec]) -> dict[str, Any]:
    """Return a fresh default-value mapping for ``specs``."""
    return {name: spec.default for name, spec in specs.items()}


def normalize_parameter_values(
    specs: Mapping[str, ParameterSpec],
    values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a parameter mapping using defaults for omitted values.

    Unknown keys are deliberately ignored.  This lets older callers hand a
    previously combined calibration dictionary to a newer strategy without
    leaking transform-fit settings back into the localization backend.
    """
    supplied = dict(values or {})
    return {
        name: spec.normalize(supplied.get(name, spec.default))
        for name, spec in specs.items()
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"cannot interpret {value!r} as a boolean")
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise ValueError(f"cannot interpret {value!r} as a boolean")
