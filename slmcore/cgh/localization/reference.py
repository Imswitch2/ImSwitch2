"""Host-neutral target references for image localization workflows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any,Mapping

from ..targets.resolution import TargetResolution


def _frozen_mapping(value: Mapping[str,Any]) -> Mapping[str,Any]:
    return MappingProxyType(deepcopy(dict(value or {})))


@dataclass(frozen=True)
class TargetLocalizationReference:
    """Resolved structural target data needed by generic localization.

    ``resolution`` is the base target resolution selected by the section state.
    It deliberately excludes feedback-effective intensity or position changes.
    """

    target_type: str
    target_params: Mapping[str,Any]
    resolution: TargetResolution
    localization_context: Mapping[str,Any]
    target_signature: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_type,str) or not self.target_type.strip():
            raise ValueError("target_type must be a non-empty string")
        if not isinstance(self.resolution,TargetResolution):
            raise TypeError(
                "resolution must be TargetResolution, got "
                f"{type(self.resolution).__name__}"
            )
        object.__setattr__(self,"target_type",self.target_type.strip())
        object.__setattr__(
            self,"target_params",_frozen_mapping(self.target_params)
        )
        object.__setattr__(
            self,
            "localization_context",
            _frozen_mapping(self.localization_context),
        )


__all__ = ["TargetLocalizationReference"]
