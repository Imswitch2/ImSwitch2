from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields
import types
from typing import (
    Any,
    Iterator,
    Mapping,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)
from .loading import ConfigPath,ConfigWarning
from ..parameters.spec import ParamSpec, PARAM_OPTIONS_KEY, validate_param_links

SERIALIZE_KEY = "serialize"

ParamPath = tuple[str, ...]


def runtime_field(
    default: Any = MISSING,
    *,
    default_factory: Any = MISSING,
    repr: bool = False,
    compare: bool = False,
):
    """Declare a runtime-only dataclass field.

    Phase 1 uses the explicit ``serialize=False`` marker. More detailed clone,
    diff and traversal policies can be added later without changing callers.
    """

    if default is not MISSING and default_factory is not MISSING:
        raise TypeError("runtime_field cannot define both default and default_factory")

    kwargs: dict[str, Any] = {
        "repr": repr,
        "compare": compare,
        "metadata": {SERIALIZE_KEY: False},
    }
    if default_factory is not MISSING:
        kwargs["default_factory"] = default_factory
    elif default is not MISSING:
        kwargs["default"] = default
    else:
        kwargs["default"] = None
    return field(**kwargs)


@dataclass(frozen=True)
class ParameterRef:
    path: ParamPath
    spec: ParamSpec
    value: Any


class StateModel:
    """Base class for recursively validated typed state objects."""

    def param_specs(self) -> Mapping[str, ParamSpec]:
        """Return parameters directly owned by this state,
        discovered via class fields metadata."""

        try:
            data_fields = fields(self)
        except TypeError:
            return {}

        type_hints = get_type_hints(type(self))
        specs: dict[str, ParamSpec] = {}

        for data_field in data_fields:
            options = data_field.metadata.get(PARAM_OPTIONS_KEY)
            if options is None:
                continue

            if data_field.default is MISSING:
                raise TypeError(
                    f"Parameter field '{data_field.name}' must define a default value"
                )

            annotation = type_hints.get(data_field.name, data_field.type)
            ptype = _parameter_type(annotation, bool(options.get("allow_none")))
            
            specs[data_field.name] = ParamSpec(
                default=data_field.default,
                ptype=ptype,
                **options,
            )

        return specs

    def child_states(self) -> Mapping[str, "StateModel"]:
        """Return directly nested state nodes.

        Direct dataclass fields containing ``StateModel`` instances are exposed
        by their field names. Storage containers such as dynamic ``items`` are
        exposed by the owning subclass.
        """
        try:
            data_fields = fields(self)
        except TypeError:
            return {}

        children: dict[str, StateModel] = {}
        for data_field in data_fields:
            value = getattr(self, data_field.name)
            if isinstance(value, StateModel):
                children[data_field.name] = value
        return children
    
    def resolve_parameter(
        self, path: ParamPath,
    ) -> tuple[StateModel, str, ParamSpec]:
        
        """Resolve an operational parameter parameter path against this state tree."""

        if not path:
            raise ValueError("Parameter path cannot be empty")

        current: StateModel = self
        traversed: ParamPath = ()

        for segment in path[:-1]:
            children = current.child_states()
            if segment not in children:
                location = ".".join(traversed) or type(self).__name__
                raise KeyError(f"Unknown child '{segment}' below {location}")
            current = children[segment]
            traversed += (segment,)

        key = path[-1]
        specs = current.param_specs()
        if key not in specs:
            location = ".".join(path[:-1]) or type(self).__name__
            raise KeyError(f"Unknown parameter '{key}' below {location}")

        return current, key, specs[key]
    
    def apply_requested_values(
        self,
        changes:Mapping[ParamPath,Any]
    )->dict[ParamPath, Any]:
        """Apply requested parameter changes and return their normalized values."""
        return {
            path: self.set_parameter(path,value)
            for path,value in changes.items()
        }

    def get_parameter(self,path: ParamPath) -> Any:
        owner,key,_ = self.resolve_parameter(path)
        return owner.get_param_value(key)

    def set_parameter(self,path: ParamPath,value: Any) -> Any:
        owner,key,_ = self.resolve_parameter(path)
        return owner.set_param_value(key,value)

    def get_param_value(self, key: str) -> Any:
        if key not in self.param_specs():
            raise KeyError(f"Unknown parameter '{key}' on {type(self).__name__}")
        return getattr(self, key)


    def set_param_value(self, key: str, value: Any) -> Any:
        specs = self.param_specs()
        if key not in specs:
            raise KeyError(f"Unknown parameter '{key}' on {type(self).__name__}")

        try:
            converted = specs[key].validate(value)
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from error

        setattr(self, key, converted)
        return converted
    

    def validate(self) -> None:
        """Validate local parameters and recursively validate child states."""

        validate_param_links(self.param_specs())

        for key in self.param_specs():
            self.set_param_value(key, self.get_param_value(key))

        for child in self.child_states().values():
            child.validate()

    def iter_parameters(self, prefix: ParamPath = ()) -> Iterator[ParameterRef]:
        """Recursively yield path, specification and current value."""

        for key, spec in self.param_specs().items():
            yield ParameterRef(
                path=prefix + (key,),
                spec=spec,
                value=self.get_param_value(key),
            )

        for child_key, child in self.child_states().items():
            yield from child.iter_parameters(prefix + (child_key,))

    def to_dict(self) -> dict[str, Any]:
        """Serialize state values recursively, excluding runtime fields."""

        try:
            data_fields = fields(self)
        except TypeError:
            raise TypeError(f"{type(self).__name__} must be a dataclass to serialize")

        result: dict[str, Any] = {}
        for data_field in data_fields:
            if not data_field.metadata.get(SERIALIZE_KEY, True):
                continue
            result[data_field.name] = _serialize_value(getattr(self, data_field.name))
        return result
    
    
    def load_dict(
        self,
        data: Mapping[str,Any],
        *,
        warnings: list[ConfigWarning] | None = None,
        path: ConfigPath = (),
    ) -> None:
        
        """Tolerantly load local parameters and existing child states."""
        warnings = [] if warnings is None else warnings

        if not isinstance(data,Mapping):
            warnings.append(ConfigWarning(
                path,
                f"Expected a mapping, got {type(data).__name__}; defaults kept",
            ))
            return

        local_specs = self.param_specs()
        children = self.child_states()
        allowed = set(local_specs) | set(children)

        for key in set(data) - allowed:
            warnings.append(ConfigWarning(
                path + (str(key),),
                "Unknown field; value skipped",
            ))

        for key in local_specs:
            if key not in data:
                continue

            try:
                self.set_param_value(key,data[key])
            except (TypeError,ValueError) as error:
                warnings.append(ConfigWarning(
                    path + (key,),
                    f"Invalid value {data[key]!r}; default kept ({error})",
                ))

        for key,child in children.items():
            if key in data:
                child.load_dict(
                    data[key],
                    warnings=warnings,
                    path=path + (key,),
                )

def _parameter_type(annotation: Any, allow_none: bool) -> type:
    """Resolve the conversion type from a fixed field annotation."""

    origin = get_origin(annotation)
    union_type = getattr(types, "UnionType", None)
    if origin is Union or (union_type is not None and origin is union_type):
        args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
        if allow_none and len(args) == 1:
            annotation = args[0]
        else:
            raise TypeError(
                f"Parameter annotation {annotation!r} is not a single conversion type"
            )

    if annotation is Any or not isinstance(annotation, type):
        raise TypeError(
            f"Parameter annotations must resolve to a concrete type, got {annotation!r}"
        )
    return annotation



def _serialize_value(value: Any) -> Any:
    if isinstance(value, StateModel):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value
