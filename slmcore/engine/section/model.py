from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any,ClassVar,Mapping

from ..parameters import ParamRole
from ..registry import  SLMRegistries
from ..state import (
    ConfigPath,
    ConfigWarning,
    DynamicGroupState,
    GroupStateModel,
    GroupTopology,
    ParamPath,
    StateModel,
    runtime_field,
)

from .components import(
    OpticsState,
    PatternsState,
    AberrationsState,
    CGHState,
    CorrectionsState
)


SectionTopology = tuple[tuple[str,bool,tuple[str,...]],...]


@dataclass
class SLMSectionState(StateModel):
    """Complete authoritative parameter state for one physical SLM section."""

    optics: OpticsState = field(default_factory=OpticsState)
    patterns: PatternsState = field(default_factory=PatternsState)
    aberrations: AberrationsState = field(default_factory=AberrationsState)
    cgh: CGHState = field(default_factory=CGHState)
    corrections: CorrectionsState = field(default_factory=CorrectionsState)

    _registries: SLMRegistries = runtime_field()

    SCHEMA_VERSION: ClassVar[int] = 1

    @property
    def registries(self)-> SLMRegistries:
        """ Stored, read-only SLMRegistries reference"""
        return self._registries

    @classmethod
    def create(cls,registries: SLMRegistries) -> "SLMSectionState":
        """Create the canonical default section state."""
        registries.validate()
        state = cls._create_default(registries)
        state.validate()
        return state

    @classmethod
    def from_dict(
        cls,
        registries: SLMRegistries,
        state_dict: Mapping[str,Any],
        *,
        path: ConfigPath=(),
    ) -> tuple['SLMSectionState', tuple[ConfigWarning, ...]]:
        """Atomically construct a section state from serialized state data."""
        registries.validate()
        state = cls._create_default(registries)
        warnings: list[ConfigWarning] = []
        state.load_dict(state_dict,warnings=warnings,path=path)
        state.validate()
        return state,tuple(warnings)

    @classmethod
    def default_state_dict(
        cls,registries: SLMRegistries,
    ) -> dict[str, Any]:
        """Return the canonical all-available, all-inactive state dictionary."""
        return cls.create(registries).to_dict()

    @classmethod
    def _create_default( cls,registries: SLMRegistries) -> SLMSectionState:

        state = cls(
            patterns=PatternsState( _registry=registries.patterns),
            aberrations=AberrationsState(_registry=registries.aberrations),
            cgh=CGHState(
                _registry=registries.targets,
                _algorithm_registry=registries.algorithms,
            ),
            _registries=registries,
        )

        state.patterns.set_enabled_items(registries.patterns)
        state.aberrations.set_enabled_items(registries.aberrations)
        state.cgh.set_enabled_items(registries.targets)

        # Default config: everything available, nothing active.
        for parameter in state.iter_parameters():
            if parameter.spec.role is ParamRole.ACTIVE:
                state.set_parameter(parameter.path,False)

        state.cgh.select_target(None)
        return state

    def clone(self) -> "SLMSectionState":
        """Return a detached copy of the current section state."""
        clone,warnings = self.__class__.from_dict(
            self._registries,self.to_dict(),
        )

        if warnings:
            raise RuntimeError(
                f"Unexpected warnings while cloning state: {warnings}"
            )

        return clone

    def group_topologies(self) -> dict[str, GroupTopology]:
        """Return topology by group key in authoritative section order."""
        topologies = {}

        for group in self.child_states().values():
            if not isinstance(group,GroupStateModel):
                continue

            item_keys = (
                group.enabled_keys()
                if isinstance(group,DynamicGroupState)
                else ()
            )
            topologies[group.GROUP_KEY] = GroupTopology(
                enabled=group.enabled,item_keys=item_keys,
            )

        return topologies

    def topology_signature(self) -> SectionTopology:
        """Return the ordered structure that determines section UI/runtime shape."""
        return tuple(
            (key,topology.enabled,topology.item_keys)
            for key,topology in self.group_topologies().items()
        )

    def parameter_values(self) -> dict[ParamPath, Any]:
        """Return all operational parameter values keyed by canonical path."""
        return {
            parameter.path:parameter.value
            for parameter in self.iter_parameters()
        }


    def diff_group_parameter_paths(
        self,other: "SLMSectionState",
    ) -> dict[str, tuple[ParamPath, ...]]:
        """Return changed common parameter paths grouped by section group.

        Parameters introduced or removed by a topology change are represented by
        that group's topology delta rather than as value changes.
        """
        return {
            key:tuple(values)
            for key,values in self.diff_group_parameter_values(other).items()
        }

    def diff_group_parameter_values(
        self,other: "SLMSectionState",
    ) -> dict[str, dict[ParamPath, Any]]:
        """Return changed retained values grouped by section group.

        Parameters introduced or removed by a topology change are represented by
        the corresponding topology delta. Values are always taken from ``other``
        and therefore describe the committed destination state.
        """
        current = self.parameter_values()
        desired = other.parameter_values()
        group_keys = {
            child_key:group.GROUP_KEY
            for child_key,group in self.child_states().items()
            if isinstance(group,GroupStateModel)
        }
        changes: dict[str, dict[ParamPath, Any]] = {}

        for path,value in desired.items():
            if path not in current or current[path] == value:
                continue
            group_key = group_keys.get(path[0],path[0])
            changes.setdefault(group_key,{})[path] = value

        return changes

    def group_by_key(self,group_key: str) -> GroupStateModel:
        """Return one authoritative group by its public ``GROUP_KEY``."""
        for group in self.child_states().values():
            if isinstance(group,GroupStateModel) and group.GROUP_KEY == group_key:
                return group
        raise KeyError(f"Unknown section group '{group_key}'")

    def diff_parameter_values(
        self,other: "SLMSectionState",
    ) -> dict[ParamPath, Any]:
        """Return values from ``other`` that differ for an equal topology."""
        current = self.parameter_values()
        desired = other.parameter_values()

        if current.keys() != desired.keys():
            raise ValueError(
                "Cannot diff parameter values for different section topologies"
            )

        return {
            path:value
            for path,value in desired.items()
            if current[path] != value
        }

    def to_dict(self):
        return {
            "schema_version": self.SCHEMA_VERSION,
            **super().to_dict(),
        }


    def load_dict(
        self,
        data: Mapping[str,Any],
        *,
        warnings: list[ConfigWarning] | None = None,
        path: ConfigPath = (),
    ) -> None:
        """Load a serialized state by delegating to the state tree."""
        warnings = [] if warnings is None else warnings

        if not isinstance(data,Mapping):
            raise TypeError(
                f"SLM section state must be a mapping, "
                f"got {type(data).__name__}"
            )

        if "schema_version" not in data:
            warnings.append(ConfigWarning(
                path + ("schema_version",),
                f"Missing schema version; assuming {self.SCHEMA_VERSION}",
            ))
        else:
            try:
                version = int(data["schema_version"])
            except (TypeError,ValueError) as error:
                raise ValueError(
                    "Invalid section schema version"
                ) from error

            if version != self.SCHEMA_VERSION:
                raise ValueError(
                    f"Unsupported section schema version {version}; "
                    f"expected {self.SCHEMA_VERSION}"
                )

        children = self.child_states()
        allowed = set(children) | {"schema_version"}

        for key in set(data) - allowed:
            warnings.append(ConfigWarning(
                path + (str(key),),
                "Unknown section group; group skipped",
            ))

        for key,child in children.items():
            if key not in data:
                warnings.append(ConfigWarning(
                    path + (key,), "Missing section group; default group kept",
                ))
                continue

            child.load_dict(
                data[key], warnings=warnings, path=path + (key,),
            )

    def validate(self) -> None:
        if self.registries is None:
            raise RuntimeError(
                "SLMSectionState must be constructed through create()"
            )

        self.registries.validate()
        super().validate()
