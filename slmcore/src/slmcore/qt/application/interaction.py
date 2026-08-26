"""Qt-only interaction semantics for retained SLM parameter editors.

The backend model remains unaware of editor timing and interaction policy.  This
module provides the small semantic layer used by :mod:`slmcore.qt` to classify
parameter edits, choose debounce intervals, and control typed numeric commit
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from ...engine.section.components import CGHState
from ...engine.state.base import ParamPath
from ...engine.state.items import CGHTargetState


class ParameterEditKind(str,Enum):
    """Semantic edit categories used by the Qt runtime/view binding."""

    STANDARD = "standard"
    CGH_TARGET = "cgh_target"


class ParameterCommitMode(str,Enum):
    """How free-form numeric text is committed by a retained editor."""

    LIVE = "live"
    EDIT_FINISHED = "edit_finished"


@dataclass(frozen=True)
class RuntimeViewInteractionSettings:
    """Shared timing policy for retained runtime/view interactions.

    These values describe application/UI interaction and intentionally do not
    belong to the persisted optical ``SLMConfig`` model.
    """

    standard_patch_debounce_ms: int = 300
    target_patch_debounce_ms: int = 500

    def __post_init__(self) -> None:
        for name in (
            "standard_patch_debounce_ms",
            "target_patch_debounce_ms",
        ):
            value = int(getattr(self,name))
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
            object.__setattr__(self,name,value)


DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS = RuntimeViewInteractionSettings()


def is_cgh_target_selector_path(path: ParamPath) -> bool:
    """Return whether one section-relative path selects the active CGH target."""
    path = tuple(path)
    return path == (CGHState.GROUP_KEY,CGHState.SELECTED_TARGET_KEY)


def is_cgh_target_parameter_path(path: ParamPath) -> bool:
    """Return whether one section-relative path addresses a CGH target param."""
    path = tuple(path)
    return bool(
        len(path) >= 4
        and path[0] == CGHState.GROUP_KEY
        and path[2] == CGHTargetState.PARAMS_STATE_KEY
    )


def is_group_target_parameter_path(path: ParamPath) -> bool:
    """Return whether one CGH-group-relative path addresses a target param."""
    path = tuple(path)
    return bool(
        len(path) >= 3
        and path[1] == CGHTargetState.PARAMS_STATE_KEY
    )


def classify_parameter_edit(
    changes: Mapping[ParamPath,object],
) -> ParameterEditKind:
    """Classify one atomic view-emitted patch.

    A selected-target change and a target-parameter action are both target
    edits for auto-compute eligibility.  Mixed/unknown patches stay on the
    conservative standard path.
    """
    paths = tuple(tuple(path) for path in dict(changes or {}))
    if not paths:
        return ParameterEditKind.STANDARD
    if all(
        is_cgh_target_selector_path(path)
        or is_cgh_target_parameter_path(path)
        for path in paths
    ):
        return ParameterEditKind.CGH_TARGET
    return ParameterEditKind.STANDARD


def is_target_selector_edit(
    changes: Mapping[ParamPath,object],
) -> bool:
    """Return whether an atomic patch is solely the discrete target selector."""
    paths = tuple(tuple(path) for path in dict(changes or {}))
    return bool(paths) and all(is_cgh_target_selector_path(path) for path in paths)
