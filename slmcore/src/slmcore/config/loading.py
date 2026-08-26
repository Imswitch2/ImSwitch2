from __future__ import annotations

from dataclasses import dataclass,field
from types import MappingProxyType
from typing import TYPE_CHECKING,Any,Mapping

from ..engine.state import ConfigWarning,ParamPath
from ..engine.transition import GroupStateDelta,SectionStateTransition

if TYPE_CHECKING:
    from ..engine.section.snapshot import SLMSectionSnapshot

# Compatibility alias. New code should use GroupStateDelta.
GroupConfigDelta = GroupStateDelta


@dataclass(frozen=True)
class SectionConfigLoadResult:
    """Config-specific metadata around a generic committed transition."""

    transition: SectionStateTransition
    warnings: tuple[ConfigWarning, ...] = ()
    cgh_session_restored: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self,"warnings",tuple(self.warnings))

    @property
    def base_revision(self) -> int:
        return self.transition.base_revision

    @property
    def revision(self) -> int:
        return self.transition.revision

    @property
    def snapshot(self) -> "SLMSectionSnapshot":
        return self.transition.snapshot

    @property
    def group_deltas(self) -> Mapping[str,GroupStateDelta]:
        return self.transition.group_deltas

    @property
    def calibration_changed(self) -> bool:
        return self.transition.calibration_changed

    @property
    def cgh_pattern_changed(self) -> bool:
        return self.transition.cgh_pattern_changed

    @property
    def artifacts_recomputed(self) -> bool:
        return self.transition.artifacts_recomputed

    @property
    def frame_changed(self) -> bool:
        return self.transition.frame_changed

    @property
    def changed_values(self) -> Mapping[ParamPath,Any]:
        return self.transition.changed_values

    @property
    def changed_paths(self) -> tuple[ParamPath, ...]:
        return self.transition.changed_paths

    @property
    def topology_changed(self) -> bool:
        return self.transition.topology_changed

    @property
    def state_changed(self) -> bool:
        return self.transition.state_changed

    @property
    def topology_changed_group_keys(self) -> tuple[str, ...]:
        return self.transition.topology_changed_group_keys

    @property
    def value_changed_group_keys(self) -> tuple[str, ...]:
        return self.transition.value_changed_group_keys


@dataclass(frozen=True)
class SLMConfigLoadReport:
    revision: int
    section_results: Mapping[str,SectionConfigLoadResult]
    failed_sections: Mapping[str,Exception] = field(default_factory=dict)
    warnings: tuple[ConfigWarning, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"section_results",MappingProxyType(dict(self.section_results)),
        )
        object.__setattr__(
            self,"failed_sections",MappingProxyType(dict(self.failed_sections)),
        )
        object.__setattr__(self,"warnings",tuple(self.warnings))

    @property
    def loaded_section_keys(self) -> tuple[str, ...]:
        """Return section keys successfully restored and committed."""
        return tuple(self.section_results)

    @property
    def computed_section_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key,result in self.section_results.items()
            if result.artifacts_recomputed
        )

    @property
    def frame_changed_section_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key,result in self.section_results.items()
            if result.frame_changed
        )

    @property
    def frame_changed(self) -> bool:
        return bool(self.frame_changed_section_keys)
