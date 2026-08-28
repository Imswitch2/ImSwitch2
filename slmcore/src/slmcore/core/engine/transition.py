from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import TYPE_CHECKING,Any,Mapping

from .state import GroupTopology,ParamPath

if TYPE_CHECKING:
    from .section.snapshot import SLMSectionSnapshot


@dataclass(frozen=True)
class GroupStateDelta:
    """Committed state delta for one section group.

    ``changed_values`` contains only parameters that existed both before and
    after the transition and whose committed value changed. Parameters added or
    removed with a structural change are represented by the topology delta.
    """

    before_topology: GroupTopology
    after_topology: GroupTopology
    changed_values: Mapping[ParamPath,Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "changed_values",
            MappingProxyType(deepcopy(dict(self.changed_values))),
        )

    @property
    def topology_changed(self) -> bool:
        return self.before_topology != self.after_topology

    @property
    def values_changed(self) -> bool:
        return bool(self.changed_values)

    @property
    def changed_paths(self) -> tuple[ParamPath, ...]:
        """Return changed paths for compatibility with the original contract."""
        return tuple(self.changed_values)


@dataclass(frozen=True)
class SectionStateTransition:
    """Generic outcome of one successfully committed section transition."""

    base_revision: int
    snapshot: "SLMSectionSnapshot"
    group_deltas: Mapping[str,GroupStateDelta]
    calibration_changed: bool = False
    cgh_pattern_changed: bool = False
    artifacts_recomputed: bool = False
    frame_changed: bool = False

    def __post_init__(self) -> None:
        base_revision = int(self.base_revision)
        if base_revision < 0:
            raise ValueError("Section transition base revision must be >= 0")
        if self.snapshot.revision != base_revision + 1:
            raise ValueError(
                "Section transition revision must be exactly one greater "
                "than its base revision"
            )
        object.__setattr__(self,"base_revision",base_revision)
        object.__setattr__(
            self,"group_deltas",MappingProxyType(dict(self.group_deltas)),
        )

    @property
    def revision(self) -> int:
        return self.snapshot.revision

    @property
    def changed_values(self) -> Mapping[ParamPath,Any]:
        return MappingProxyType({
            path:value
            for delta in self.group_deltas.values()
            for path,value in delta.changed_values.items()
        })

    @property
    def changed_paths(self) -> tuple[ParamPath, ...]:
        return tuple(self.changed_values)

    @property
    def topology_changed(self) -> bool:
        return any(
            delta.topology_changed for delta in self.group_deltas.values()
        )

    @property
    def state_changed(self) -> bool:
        return bool(self.group_deltas)

    @property
    def topology_changed_group_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key,delta in self.group_deltas.items()
            if delta.topology_changed
        )

    @property
    def value_changed_group_keys(self) -> tuple[str, ...]:
        """Return groups whose retained fields can be synchronized in place."""
        return tuple(
            key for key,delta in self.group_deltas.items()
            if not delta.topology_changed and delta.values_changed
        )
