from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any,ClassVar,Mapping
from types import MappingProxyType

from .base import ParamPath,StateModel,runtime_field
from .loading import ConfigPath,ConfigWarning
from ..parameters.spec import ParamSpec, validate_param_links


@dataclass
class ParameterSetState(StateModel):
    """Current values for one runtime-defined parameter schema."""

    values: dict[str, Any] = field(default_factory=dict)
    _specs: Mapping[str, ParamSpec] = runtime_field(default_factory=dict)

    def __post_init__(self) -> None:
        """ Validate and freeze _specs """
        if not isinstance(self._specs,Mapping):
            raise TypeError(
                f"_specs must be a Mapping[str,ParamSpec], got {type(self._specs).__name__}"
            )
        
        specs: dict[str, ParamSpec] = {}

        for key,spec in self._specs.items():
            if not isinstance(key,str) or not key.strip():
                raise ValueError("Parameter specification keys must be non-empty strings")

            key = key.strip()
            if key in specs:
                raise ValueError(f"Duplicate parameter specification '{key}'")

            if not isinstance(spec,ParamSpec):
                raise TypeError(
                    f"Specification '{key}' must be a ParamSpec, got {type(spec).__name__}"
                )

            specs[key] = spec
        
        # freeze: _specs should be immutable.
        self._specs = MappingProxyType(specs)

    @classmethod
    def from_specs(
        cls,
        specs: Mapping[str, ParamSpec],
        values: Mapping[str, Any] | None = None,
    ) -> "ParameterSetState":
        normalized_specs = dict(specs)
        current_values = {
            key: spec.default for key, spec in normalized_specs.items()
        }

        if values is not None:
            unknown = set(values) - set(normalized_specs)
            if unknown:
                raise KeyError(f"Unknown parameter value(s): {sorted(unknown)}")
            current_values.update(values)

        state = cls(values=current_values, _specs=normalized_specs)
        state.validate()
        return state

    def param_specs(self) -> Mapping[str, ParamSpec]:
        return self._specs

    def get_param_value(self, key: str) -> Any:
        if key not in self._specs:
            raise KeyError(f"Unknown parameter '{key}'")
        return self.values[key]

    def set_param_value(self, key: str, value: Any) -> Any:
        if key not in self._specs:
            raise KeyError(f"Unknown parameter '{key}'")

        try:
            converted = self._specs[key].validate(value)
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from error

        self.values[key] = converted
        return converted

    def validate(self) -> None:

        validate_param_links(self._specs)

        value_keys = set(self.values)
        spec_keys = set(self._specs)
        if value_keys != spec_keys:
            missing = spec_keys - value_keys
            unknown = value_keys - spec_keys
            parts = []
            if missing:
                parts.append(f"missing values: {sorted(missing)}")
            if unknown:
                parts.append(f"unknown values: {sorted(unknown)}")
            raise ValueError("Parameter values/specs mismatch (" + ", ".join(parts) + ")")

        for key in self._specs:
            self.set_param_value(key, self.values[key])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)

    def load_dict(
        self,
        data: Mapping[str,Any],
        *,
        warnings: list[ConfigWarning] | None = None,
        path: ConfigPath = (),
    ) -> None:
        warnings = [] if warnings is None else warnings

        if not isinstance(data,Mapping):
            warnings.append(ConfigWarning(
                path,
                f"Expected a parameter mapping, got {type(data).__name__}; defaults kept",
            ))
            return

        for key in set(data) - set(self._specs):
            warnings.append(ConfigWarning(
                path + (str(key),),
                "Unknown parameter; value skipped",
            ))

        for key,value in data.items():
            if key not in self._specs:
                continue

            try:
                self.set_param_value(key,value)
            except (TypeError,ValueError) as error:
                warnings.append(ConfigWarning(
                    path + (key,),
                    f"Invalid value {value!r}; default kept ({error})",
                ))


@dataclass
class ItemState(StateModel):
    """Generic selected registry item containing one parameter set."""

    PARAMS_STATE_KEY: ClassVar[str] = "params"

    params: ParameterSetState = field(default_factory=ParameterSetState)

    @classmethod
    def params_path(cls,item_key: str) -> ParamPath:
        """Return the group-relative path to one item's parameter state."""
        return item_key,cls.PARAMS_STATE_KEY


@dataclass
class CGHTargetState(ItemState):
    """CGH target state with its computation state and capabilities."""

    COMPUTATION_STATE_KEY: ClassVar[str] = "computation"

    algorithm: str = runtime_field(default="")
    feedback_capabilities: tuple[str, ...] = runtime_field(default_factory=tuple)
    computation: ItemState = field(default_factory=ItemState)
    lock_state: StateModel | None = None

    @classmethod
    def computation_params_path(cls,target_key: str) -> ParamPath:
        """Return the group-relative path to target computation parameters."""
        return (
            target_key,cls.COMPUTATION_STATE_KEY,cls.PARAMS_STATE_KEY,
        )

    def validate(self) -> None:
        if not self.algorithm:
            raise ValueError("CGH target algorithm cannot be empty")
        super().validate()

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.lock_state is None:
            data.pop("lock_state",None)
        return data
