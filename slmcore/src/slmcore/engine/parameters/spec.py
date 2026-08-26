from __future__ import annotations

import re
from dataclasses import dataclass,field
from enum import Enum
from types import MappingProxyType
from typing import Any,Iterable,Mapping

from .converters import ConverterProtocol

PARAM_OPTIONS_KEY = "param_options"


class EditorKind(str,Enum):
    """Framework-independent editor hints."""

    LINE_EDIT = "line_edit"
    SPIN_BOX = "spin_box"
    DOUBLE_SPIN_BOX = "double_spin_box"
    CHECK_BOX = "check_box"
    COMBO_BOX = "combo_box"


class ParamRole(str,Enum):
    """Semantic roles used by UIs and higher-level state logic."""

    VALUE = "value"
    ACTIVE = "active"
    SELECTOR = "selector"


class ParamDisplayLevel(str,Enum):
    """Presentation importance hint consumed by UIs."""

    PRIMARY = "primary"
    ADVANCED = "advanced"


def make_display_name(param_name: str) -> str:
    """Convert an internal parameter key into a user-facing label."""
    units = {"mm","um","nm","px","degrees","deg","rad","radians"}
    acronyms = {"fov":"FOV","slm":"SLM","cgh":"CGH","roi":"ROI"}

    if (
        " " in param_name
        or "(" in param_name
        or re.search(r"[A-Z].*[A-Z]",param_name)
    ):
        return param_name

    display_parts = []
    for part in param_name.split("_"):
        if not part:
            continue
        part_lower = part.lower()
        if part_lower in units:
            display_parts.append(f"({part_lower})")
        elif part_lower in acronyms:
            display_parts.append(acronyms[part_lower])
        else:
            display_parts.append(part.capitalize())
    return " ".join(display_parts)


def validate_param_links(
    specs: Mapping[str,ParamSpec],
) -> None:
    """Validate cross-parameter references declared by ParamLinks."""

    for source,spec in specs.items():
        for link in spec.links:
            if link.target not in specs:
                raise KeyError(
                    f"Parameter '{source}' links to unknown parameter '{link.target}'"
                )

            if link.enabled_by not in specs:
                raise KeyError(
                    f"Parameter '{source}' link uses unknown control '{link.enabled_by}'"
                )

            if specs[link.enabled_by].ptype is not bool:
                raise TypeError(f"ParamLink control '{link.enabled_by}' must be boolean")

            if link.unit_by is None:
                continue

            if link.unit_by not in specs:
                raise KeyError(
                    f"Parameter '{source}' link uses unknown unit control "
                    f"'{link.unit_by}'"
                )

            unit_spec = specs[link.unit_by]
            if unit_spec.ptype is not str:
                raise TypeError(
                    f"ParamLink unit control '{link.unit_by}' must be a string"
                )
            if unit_spec.choices is None:
                raise ValueError(
                    f"ParamLink unit control '{link.unit_by}' must define choices"
                )

            source_converter = spec.converter
            target_converter = specs[link.target].converter
            if source_converter is None or target_converter is None:
                raise TypeError(
                    "Unit-aware ParamLink requires converters on both "
                    f"'{source}' and '{link.target}'"
                )

            common_units = (
                set(source_converter.supported_units)
                & set(target_converter.supported_units)
            )
            unsupported = set(unit_spec.choices) - common_units
            if unsupported:
                raise ValueError(
                    f"ParamLink unit control '{link.unit_by}' contains "
                    f"unsupported unit(s): {sorted(unsupported)}"
                )


def apply_param_links(
    params: Mapping[str,Any],
    specs: Mapping[str,ParamSpec],
    conversion_context: Any = None,
) -> dict[str, Any]:
    """Apply active source -> target parameter links."""

    validate_param_links(specs)
    resolved = dict(params)

    for source,spec in specs.items():
        for link in spec.links:
            if not bool(resolved[link.enabled_by]):
                continue

            target_spec = specs[link.target]
            if link.unit_by is None:
                target_value = resolved[source]
            else:
                unit = str(resolved[link.unit_by])
                linked_value = spec.to_unit(
                    resolved[source],unit,conversion_context,
                )
                target_value = target_spec.from_unit(
                    linked_value,unit,conversion_context,
                )

            resolved[link.target] = target_spec.validate(target_value)

    return resolved



def param_field(
    default: Any,
    *,
    min_value: Any = None,
    max_value: Any = None,
    step: Any = None,
    step_by_unit: Mapping[str, Any] | None = None,
    decimals: int | None = None,
    decimals_by_unit: Mapping[str, int] | None = None,
    choices: Iterable[Any] | None = None,
    allow_none: bool = False,
    converter: ConverterProtocol | None = None,
    editor: EditorKind | None = None,
    role: ParamRole | None = None,
    display_level: ParamDisplayLevel = ParamDisplayLevel.PRIMARY,
    label: str | None = None,
    converted_label: str | None = None,
    tooltip: str | None = None,
    unit: str | None = None,
    hidden: bool = False,
    layout_group: str | None = None,
    links: Iterable[ParamLink] = (),
):
    """Declare a fixed typed parameter on a dataclass state."""
    options = {
        "min_value":min_value,
        "max_value":max_value,
        "step":step,
        "step_by_unit":step_by_unit,
        "decimals":decimals,
        "decimals_by_unit":decimals_by_unit,
        "choices":tuple(choices) if choices is not None else None,
        "allow_none":allow_none,
        "converter":converter,
        "editor":editor,
        "role":role,
        "display_level":display_level,
        "label":label,
        "converted_label":converted_label,
        "tooltip":tooltip,
        "unit":unit,
        "hidden":hidden,
        "layout_group":layout_group,
        "links":tuple(links),
    }
    return field(default=default,metadata={PARAM_OPTIONS_KEY:options})


def _validate_positive_step(name: str,value: Any) -> Any:
    try:
        numeric = float(value)
    except (TypeError,ValueError) as error:
        raise ValueError(f"{name} must be numeric, got {value!r}") from error
    if numeric <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return value


def _normalize_unit_map(
    *,
    name: str,
    values: Mapping[str, Any] | None,
    converter: ConverterProtocol | None,
    normalize_value,
) -> Mapping[str, Any] | None:
    if values is None:
        return None
    if converter is None:
        raise ValueError(f"{name} requires a converter")
    if not isinstance(values,Mapping):
        raise TypeError(f"{name} must be a mapping")

    supported_units = tuple(converter.supported_units)
    supported = set(supported_units)
    supplied = set(values.keys())
    missing = supported - supplied
    unknown = supplied - supported
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {tuple(sorted(missing))}")
        if unknown:
            parts.append(f"unknown {tuple(sorted(unknown))}")
        raise ValueError(
            f"{name} must define exactly converter supported units "
            f"{supported_units}; {', '.join(parts)}"
        )

    return MappingProxyType({
        unit:normalize_value(unit,values[unit])
        for unit in supported_units
    })


@dataclass(frozen=True)
class ParamLink:
    """ Describes a link between 2 ParamSpec objects.
    The target parameter is specified by ``target``, but
    the source is implicitly the parameter declaring the link.
    
    For example, to link X and Y axis when square is true:
        period_x = ParamSpec(
            0,
            int,
            ...,
            link=ParamLink(
                target="period_y"
                enabled_by="square"
            )
        )        
    """

    target: str
    enabled_by: str
    unit_by: str | None = None

    def __post__init__(self)->None:
        if not isinstance(self.target,str) or not self.target.strip():
            raise ValueError("ParamLink target must be a non-empty string")

        if not isinstance(self.enabled_by,str) or not self.enabled_by.strip():
            raise ValueError("ParamLink enabled_by must be a non empty string") 

        if self.unit_by is not None:
            if not isinstance(self.unit_by,str) or not self.unit_by.strip():
                raise ValueError("ParamLink unit_by must be a non-empty string")


@dataclass(frozen=True)
class ParamSpec:
    """Immutable framework-independent definition of one parameter value."""

    default: Any
    ptype: type[Any]

    min_value: Any = None
    max_value: Any = None
    step: Any = None
    step_by_unit: Mapping[str, Any] | None = None
    decimals: int | None = None
    decimals_by_unit: Mapping[str, int] | None = None
    choices: tuple[Any, ...] | None = None
    allow_none: bool = False

    converter: ConverterProtocol | None = None

    label: str | None = None
    converted_label: str | None = None
    tooltip: str | None = None
    unit: str | None = None
    hidden: bool = False

    editor: EditorKind | None = None
    role: ParamRole | None = None

    display_level: ParamDisplayLevel = ParamDisplayLevel.PRIMARY
    layout_group: str | None = None
    links: tuple[ParamLink, ...] = ()

    def __post_init__(self) -> None:
        if self.converter is not None:
            if self.step is not None:
                raise ValueError(
                    "step is only valid without a converter; "
                    "use step_by_unit for converted parameters"
                )
            if self.decimals is not None:
                raise ValueError(
                    "decimals is only valid without a converter; "
                    "use decimals_by_unit for converted parameters"
                )
        elif self.step_by_unit is not None or self.decimals_by_unit is not None:
            raise ValueError(
                "step_by_unit and decimals_by_unit require a converter"
            )

        if self.step is not None:
            _validate_positive_step("step",self.step)

        # validate choices
        if self.choices is not None and not isinstance(self.choices,tuple):
            object.__setattr__(self,"choices",tuple(self.choices))

        if self.decimals is not None:
            decimals = int(self.decimals)
            if decimals < 0:
                raise ValueError("decimals must be >= 0")
            object.__setattr__(self,"decimals",decimals)

        step_by_unit = _normalize_unit_map(
            name="step_by_unit",
            values=self.step_by_unit,
            converter=self.converter,
            normalize_value=lambda unit,value:_validate_positive_step(
                f"step_by_unit[{unit!r}]",value,
            ),
        )
        if step_by_unit is not None:
            object.__setattr__(self,"step_by_unit",step_by_unit)

        decimals_by_unit = _normalize_unit_map(
            name="decimals_by_unit",
            values=self.decimals_by_unit,
            converter=self.converter,
            normalize_value=self._normalize_unit_decimals,
        )
        if decimals_by_unit is not None:
            object.__setattr__(self,"decimals_by_unit",decimals_by_unit)

        #validate links
        if isinstance(self.links,ParamLink):
            object.__setattr__(self,"links",(self.links,))
        elif not isinstance(self.links,tuple):
            object.__setattr__(self,"links",tuple(self.links))
            
        for link in self.links:
            if not isinstance(link,ParamLink):
                raise TypeError(
                    "ParamSpec links must contain ParamLink instances"
                )
        # validate layout
        if self.layout_group is not None:
            group = str(self.layout_group).strip()
            if not group:
                raise ValueError("layout_group cannot be empty")
            object.__setattr__(self,"layout_group",group)

    @property
    def conversion_available(self) -> bool:
        """Whether this parameter supports alternate displayed units."""
        return self.converter is not None

    def step_for_unit(self,unit: str | None=None) -> Any:
        """Return the editor step resolved for one display unit."""
        if self.converter is None:
            if self.step is not None:
                return self.step
            return 1 if self.ptype is int else 1.0

        unit = self._display_unit(unit)
        if self.step_by_unit is not None:
            return self.step_by_unit[unit]
        return 1 if self.converter.type_for_unit(unit) is int else 1.0

    def decimals_for_unit(self,unit: str | None=None) -> int:
        """Return the spinbox/summary precision for one display unit."""
        if self.converter is None:
            if self.decimals is not None:
                return self.decimals
            return 0 if self.ptype is int else 4

        unit = self._display_unit(unit)
        if self.decimals_by_unit is not None:
            return self.decimals_by_unit[unit]
        return 0 if self.converter.type_for_unit(unit) is int else 4

    @property
    def display_choices(self) -> tuple[str, ...]:
        """Return default user-facing labels for the declared choices."""
        if self.choices is None:
            return ()
        return tuple(
            make_display_name(choice) if isinstance(choice,str) else str(choice)
            for choice in self.choices
        )

    def label_for(self,key: str) -> str:
        """Return the explicit label or derive one from the mapping key."""
        return self.label or make_display_name(key)

    def validate(self,value: Any) -> Any:
        """Convert and validate one canonical parameter value."""
        if value is None:
            if self.allow_none:
                return None
            raise ValueError("value cannot be None")

        try:
            converted_value = self.ptype(value)
        except (TypeError,ValueError) as error:
            type_name = getattr(self.ptype,"__name__",str(self.ptype))
            raise ValueError(
                f"value must be convertible to {type_name}, got {value!r}"
            ) from error

        if self.min_value is not None and converted_value < self.min_value:
            raise ValueError(
                f"value must be >= {self.min_value}, got {converted_value}"
            )
        if self.max_value is not None and converted_value > self.max_value:
            raise ValueError(
                f"value must be <= {self.max_value}, got {converted_value}"
            )
        if self.choices is not None and converted_value not in self.choices:
            raise ValueError(
                f"value must be one of {self.choices}, got {converted_value!r}"
            )
        return converted_value

    def to_unit(self,value: Any,unit: str,context: Any = None) -> Any:
        """Convert a canonical value to one supported display unit."""
        if self.converter is None:
            return value

        unit = self._display_unit(unit)
        if unit == self.converter.canonical_unit:
            return value

        return self.converter.to_unit(value,unit,context)

    def from_unit(self,value: Any,unit: str,context: Any = None) -> Any:
        """Convert one supported display-unit value back to canonical form."""
        if self.converter is None:
            return self.validate(value)

        unit = self._display_unit(unit)
        if unit == self.converter.canonical_unit:
            return self.validate(value)

        canonical = self.converter.to_unit(
            value,self.converter.canonical_unit,context,
        )
        return self.validate(canonical)

    def _display_unit(self,unit: str | None) -> str:
        if self.converter is None:
            raise RuntimeError("unit display is only available with a converter")
        if unit is None:
            return self.converter.canonical_unit
        if unit not in self.converter.supported_units:
            raise ValueError(
                f"Unit '{unit}' is not supported. Supported units: "
                f"{self.converter.supported_units}"
            )
        return unit

    @staticmethod
    def _normalize_unit_decimals(unit: str,value: Any) -> int:
        try:
            decimals = int(value)
        except (TypeError,ValueError) as error:
            raise ValueError(
                f"decimals_by_unit[{unit!r}] must be an integer, got {value!r}"
            ) from error
        if decimals < 0:
            raise ValueError(
                f"decimals_by_unit[{unit!r}] must be >= 0, got {decimals}"
            )
        return decimals
