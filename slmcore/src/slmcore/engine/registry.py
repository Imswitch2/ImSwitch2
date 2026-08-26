from __future__ import annotations
import inspect
from dataclasses import dataclass,field
from enum import Enum
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Mapping,
    ClassVar,
    TYPE_CHECKING,
)

from .parameters.spec import ParamSpec

if TYPE_CHECKING:
    from ..cgh.computations.types import CGHComputeFunction

def _validate_and_freeze_params(
        owner: str,
        params: Mapping[str, ParamSpec] | None,
) -> Mapping[str,ParamSpec]:
    """
    Validate a parameter definition mapping and return a read-only copy.
    """
    if params is None:
        params = {}

    if not isinstance(params,Mapping):
        raise TypeError(
            f"{owner} parameters must be a Mapping[str,ParamSpec], "
            f"got {type(params).__name__}"
        )

    normalized: dict[str, ParamSpec] = {}

    for key,spec in params.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError(f"{owner} parameter keys cannot be empty")
        if normalized_key in normalized:
            raise ValueError(
                f"{owner} has duplicate parameter key '{normalized_key}'"
                )
        if not isinstance(spec,ParamSpec):
            raise TypeError(
                f"{owner} parameter '{normalized_key}' must be a "
                f"ParamSpec, got {type(spec).__name__}"
            )
        normalized[normalized_key] = spec

    return MappingProxyType(normalized)


def _freeze_metadata(metadata: Mapping[str, Any] | None) -> Mapping[str,Any]:
    """Return a read-only shallow copy of registration metadata."""
    if metadata is None:
        return MappingProxyType({})

    if not isinstance(metadata,Mapping):
        raise TypeError(
            "Registration metadata must be a mapping, "
            f"got {type(metadata).__name__}"
        )

    return MappingProxyType(dict(metadata))

def _description_from_docstring(obj: Any) -> str:
    """Return the cleaned first paragraph of an object's docstring."""
    doc = inspect.getdoc(obj) or ""
    if not doc:
        return ""

    first_paragraph = doc.split("\n\n",1)[0]

    # Keep tooltips compact even when the first paragraph spans
    # several source-code lines.
    return " ".join(first_paragraph.split())


def _registration_metadata(
    obj: Any,
    metadata: Mapping[str, Any] | None,
) -> Mapping[str,Any]:
    """Build registration metadata with a docstring description by default."""
    result = dict(metadata or {})

    # An explicitly registered description always takes precedence,
    # including an explicitly empty description.
    if "description" not in result:
        result["description"] = _description_from_docstring(obj)

    return result


class TargetPresentationFieldKind(str,Enum):
    """Semantic hints for target summary fields."""

    VALUE = "value"
    DIMENSIONS = "dimensions"


@dataclass(frozen=True)
class TargetPresentationField:
    """One semantic field exposed by a target for compact summaries."""

    key: str
    parameter_keys: tuple
    kind: TargetPresentationFieldKind = TargetPresentationFieldKind.VALUE
    label: str | None = None
    compact_label: str | None = None

    def __post_init__(self) -> None:
        key = str(self.key).strip()
        if not key:
            raise ValueError("Target presentation field key cannot be empty")

        parameter_keys = tuple(
            str(item).strip() for item in self.parameter_keys
        )
        if not parameter_keys:
            raise ValueError(
                f"Target presentation field '{key}' must reference parameters"
            )
        if any(not item for item in parameter_keys):
            raise ValueError(
                f"Target presentation field '{key}' has an empty parameter key"
            )
        if len(set(parameter_keys)) != len(parameter_keys):
            raise ValueError(
                f"Target presentation field '{key}' has duplicate parameters"
            )

        kind = TargetPresentationFieldKind(self.kind)
        label = None if self.label is None else str(self.label).strip()
        compact_label = (
            None if self.compact_label is None
            else str(self.compact_label).strip()
        )
        if label == "":
            label = None
        if compact_label == "":
            compact_label = None

        object.__setattr__(self,"key",key)
        object.__setattr__(self,"parameter_keys",parameter_keys)
        object.__setattr__(self,"kind",kind)
        object.__setattr__(self,"label",label)
        object.__setattr__(self,"compact_label",compact_label)


@dataclass(frozen=True)
class TargetPresentation:
    """Target-owned user-facing presentation metadata."""

    title: str
    summary_fields: tuple = ()

    def __post_init__(self) -> None:
        title = str(self.title).strip()
        if not title:
            raise ValueError("Target presentation title cannot be empty")

        fields = tuple(self.summary_fields or ())
        for field_item in fields:
            if not isinstance(field_item,TargetPresentationField):
                raise TypeError(
                    "Target presentation summary_fields must contain "
                    "TargetPresentationField instances"
                )

        keys = tuple(field_item.key for field_item in fields)
        if len(set(keys)) != len(keys):
            raise ValueError("Target presentation field keys must be unique")

        object.__setattr__(self,"title",title)
        object.__setattr__(self,"summary_fields",fields)


def _validate_target_presentation(
    owner: str,
    presentation: Any,
    params: Mapping[str,ParamSpec],
) -> TargetPresentation:
    """Validate target presentation metadata against registered parameters."""
    if not isinstance(presentation,TargetPresentation):
        raise TypeError(
            f"{owner} must define a TargetPresentation, got "
            f"{type(presentation).__name__}"
        )

    for field_item in presentation.summary_fields:
        unknown = set(field_item.parameter_keys) - set(params)
        if unknown:
            raise KeyError(
                f"{owner} presentation field '{field_item.key}' references "
                f"unknown parameter(s): {sorted(unknown)}"
            )

    return presentation


def _normalize_noll_by_key(
        owner: str,
        noll_by_key: Mapping[str, int] | None,
) -> Mapping[str,int]:
    """ Strip keys and ensure there are no duplicates or empty keys. """
    
    if noll_by_key is None:
        return MappingProxyType({})

    if not isinstance(noll_by_key,Mapping):
        raise TypeError(
            f"{owner} Noll mapping must be a Mapping[str,int], "
            f"got {type(noll_by_key).__name__}"
        )

    normalized: dict[str, int] = {}

    for key,value in noll_by_key.items():
        normalized_key = str(key).strip()

        if not normalized_key:
            raise ValueError(f"{owner} Noll keys cannot be empty")

        if normalized_key in normalized:
            raise ValueError(
                f"{owner} has duplicate Noll key '{normalized_key}'"
            )

        normalized[normalized_key] = int(value)

    return MappingProxyType(normalized)



@dataclass(frozen=True)
class Registration:
    key: str
    params: Mapping[str,ParamSpec]
    metadata: Mapping[str,Any]

    KIND: ClassVar[str] = "Registration"

    def __post_init__(self) -> None:
        key = str(self.key).strip()
        if not key:
            raise ValueError(f"{self.KIND} key cannot be empty")
        params =  _validate_and_freeze_params(f"{self.KIND} '{key}'",self.params)
        metadata =  _freeze_metadata(self.metadata)

        object.__setattr__(self,"key",key)
        object.__setattr__(self,"params",params)
        object.__setattr__( self,"metadata",metadata)

    @property
    def description(self) -> str:
        """Return the optional user-facing registration description."""
        value = self.metadata.get("description")
        return "" if value is None else str(value).strip()
    


@dataclass(frozen=True)
class PatternRegistration(Registration):
    KIND: ClassVar[str] = "Pattern"
    function: Callable[...,Any]

@dataclass(frozen=True)
class CGHAlgorithmRegistration(Registration):
    KIND: ClassVar[str] = "CGH algorithm"
    function: CGHComputeFunction

    def __post_init__(self) -> None:
        super().__post_init__()
        if not callable(self.function):
            raise TypeError("CGH algorithm function must be callable")

@dataclass(frozen=True)
class AberrationRegistration(Registration):
    """One aberration model, for example a Zernike coefficient set. """
    KIND: ClassVar[str] = "Aberration"
    function: Callable[...,Any]
    noll_by_key: Mapping[str,int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        
        noll_by_key = _normalize_noll_by_key(
            f"Aberration '{self.key}'",self.noll_by_key
        )

        unknown = set(noll_by_key) - set(self.params)
        if unknown:
            raise ValueError(
                f"Aberration '{self.key}' Noll mapping references "
                f"unknown parameter(s): {sorted(unknown)}"
            )

        object.__setattr__(self,"noll_by_key",noll_by_key)


@dataclass(frozen=True)
class TargetRegistration(Registration):
    KIND: ClassVar[str] = "Target"

    target_class: type[Any]
    algorithm: str
    presentation: TargetPresentation

    feedback_capabilities: tuple = ()
    
    def __post_init__(self) -> None:
        super().__post_init__()

        if not isinstance(self.target_class,type):
            raise TypeError("target_class must be a class")

        presentation = _validate_target_presentation(
            f"Target '{self.key}'",self.presentation,self.params,
        )
        object.__setattr__(self,"presentation",presentation)

        capabilities = tuple(
            str(getattr(item,"value",item)).strip()
            for item in (self.feedback_capabilities or ())
        )
        if any(not item for item in capabilities):
            raise ValueError("Feedback capability names cannot be empty")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("Feedback capabilities cannot contain duplicates")
        object.__setattr__(self,"feedback_capabilities",capabilities)



@dataclass(frozen=True)
class SLMRegistries:
    patterns: dict[str, PatternRegistration] = field(
        default_factory=dict)
    aberrations: dict[str, AberrationRegistration] = field(
        default_factory=dict)
    targets: dict[str, TargetRegistration] = field(
        default_factory=dict)
    algorithms: dict[str, CGHAlgorithmRegistration] = field(
        default_factory=dict)

    def register_pattern(self,registration: PatternRegistration) -> None:
        self._insert(self.patterns,registration.key, registration, "pattern")

    def register_aberration(self,registration: AberrationRegistration) -> None:
        self._insert(self.aberrations,registration.key,registration, "aberration")

    def register_target(self,registration: TargetRegistration) -> None:
        self._insert(self.targets,registration.key, registration,"target")

    def register_algorithm(self,registration: CGHAlgorithmRegistration) -> None:
        self._insert(self.algorithms,registration.key,registration,"CGH algorithm")

    def validate(self) -> None:
        self._validate_registry(self.patterns,PatternRegistration)
        self._validate_registry(self.aberrations,AberrationRegistration)
        self._validate_registry(self.targets,TargetRegistration)
        self._validate_registry(self.algorithms,CGHAlgorithmRegistration)

        for key,target in self.targets.items():
            if target.algorithm not in self.algorithms:
                raise ValueError(
                    f"Target '{key}' references unknown algorithm '{target.algorithm}'"
                )
            
    @staticmethod
    def _validate_registry(
            registry: Mapping[str,Registration],
            expected_type: type,
    ) -> None:
        for key,registration in registry.items():
            if not isinstance(registration,expected_type):
                raise TypeError(
                    f"{registration.KIND.capitalize()} registry entry '{key}' must be a "
                    f"{expected_type.__name__}, got {type(registration).__name__}")

            if key != registration.key:
                raise ValueError(
                    f"{registration.KIND.capitalize()} registry key '{key}' does not match "
                    f"registration key '{registration.key}'")

    @staticmethod
    def _insert(registry: dict[str, Any],key: str, value: Any,kind: str) -> None:
        key = str(key).strip()

        if not key:
            raise ValueError( f"{kind.capitalize()} key cannot be empty")

        if key in registry:
            raise KeyError(f"{kind.capitalize()} '{key}' is already registered")

        registry[key] = value


DEFAULT_REGISTRIES = SLMRegistries()


def register_pattern(
        key: str,
        params: Mapping[str,ParamSpec],
        *,
        metadata: Mapping[str, Any] | None = None,
        registries: SLMRegistries = DEFAULT_REGISTRIES,
):
    def decorator(function: Callable[...,Any]):
        registries.register_pattern(
            PatternRegistration(
                key=key,
                function=function,
                params=params,
                metadata=_registration_metadata(function,metadata),
            )
        )
        return function

    return decorator


def register_aberration(
        key: str,
        params: Mapping[str,ParamSpec],
        *,
        noll_by_key: Mapping[str, int] | None = None,
        metadata: Mapping[str, Any] | None = None,
        registries: SLMRegistries = DEFAULT_REGISTRIES,
):
    def decorator(function: Callable[...,Any]):
        registries.register_aberration(
            AberrationRegistration(
                key=key,
                function=function,
                params=params,
                noll_by_key=noll_by_key or {},
                metadata=_registration_metadata(function,metadata),
            )
        )
        return function

    return decorator


def register_target(
        *,
        metadata: Mapping[str, Any] | None = None,
        presentation: TargetPresentation | None = None,
        registries: SLMRegistries = DEFAULT_REGISTRIES,
):
    """
    Register a Target class from its class attributes.

    Required class attributes:
        - target_type
        - algorithm
        - presentation

    Optional class attributes:
        - target_params
        - feedback_capabilities
    """

    def decorator(target_class: type[Any]):

        target_name = getattr(target_class,"target_type",None)
        if not isinstance(target_name,str) or not target_name.strip():
            raise ValueError(
                f"{target_class.__name__} must define a non-empty target_type"
            )
        target_name = target_name.strip()

        algorithm = getattr( target_class,"algorithm",None)
        if algorithm is None or not isinstance(algorithm,str) or not algorithm.strip():
            raise ValueError(
                f"{target_class.__name__} must define a non-empty algorithm."
            )
        algorithm = algorithm.strip()

        params = getattr(target_class, "target_params",{})
        target_presentation = (
            presentation
            if presentation is not None
            else getattr(target_class,"presentation",None)
        )
        feedback_capabilities = getattr(target_class,"feedback_capabilities",())
        registries.register_target(
            TargetRegistration(
                key=target_name,
                target_class=target_class,
                params=params,
                metadata=_registration_metadata(
                    target_class, metadata),
                algorithm=algorithm,
                presentation=target_presentation,
                feedback_capabilities=feedback_capabilities,
            )
        )
        return target_class

    return decorator


def register_cgh_algorithm(
        key: str,
        params: Mapping[str,ParamSpec],
        *,
        metadata: Mapping[str, Any] | None = None,
        registries: SLMRegistries = DEFAULT_REGISTRIES,
):
    def decorator(function: CGHComputeFunction):
        registries.register_algorithm(
            CGHAlgorithmRegistration(
                key=key,
                function=function,
                params=params,
                metadata=_registration_metadata(function,metadata)
            )
        )
        return function

    return decorator


def load_default_registrations() -> SLMRegistries:
    """Import and validate the registrations shipped with slmcore.

    Registration declarations stay beside their implementations. This function
    is the explicit composition point that makes the standard registry
    population visible from the registry architecture itself.
    """
    from ..patterns import aberration as _pattern_aberration
    from ..patterns import focusing as _pattern_focusing
    from ..patterns import periodic as _pattern_periodic
    from ..patterns import phase_plates as _pattern_phase_plates
    from ..cgh.computations import direct_summation as _cgh_direct_summation
    from ..cgh.computations import gerchberg_saxton as _cgh_gerchberg_saxton
    from ..cgh.targets import multi_foci as _target_multi_foci
    from ..cgh.targets import multi_foci_vector as _target_multi_foci_vector

    # Keep imports local so registry module initialization does not depend on
    # feature import order and registration side effects stay explicit here.
    _ = (
        _pattern_aberration,
        _pattern_focusing,
        _pattern_periodic,
        _pattern_phase_plates,
        _cgh_direct_summation,
        _cgh_gerchberg_saxton,
        _target_multi_foci,
        _target_multi_foci_vector,
    )
    DEFAULT_REGISTRIES.validate()
    return DEFAULT_REGISTRIES
