from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


from .session_model import CGHWorkingRoundState


class CGHResultState(str,Enum):
    """Validity of the CGH result currently stored by the session."""

    MISSING = "missing"
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True)
class CGHStatus:
    """Snapshot describing the current CGH state and cached result."""

    enabled: bool
    active: bool
    target_type: str | None
    result_state: CGHResultState
    result_generation: int | None = None
    target_name: str | None = None
    committed_target_type: str | None = None
    current_round_index: int | None = None
    applied_round_index: int | None = None
    working_round_index: int | None = None
    working_round_state: CGHWorkingRoundState | None = None
    intensity_count: int = 0
    position_active: bool = False
    draft_target_changed: bool = False
    target_restore_available: bool = False
    unavailable_reason: str | None = None

    @property
    def applied(self) -> bool:
        """Whether the cached CGH result is currently used in section computation."""
        return (
            self.enabled
            and self.active
            and self.target_type is not None
            # and self.result_state is CGHResultState.CURRENT # this would mean removing pattern each time we change a target parameter
            and self.result_state is not CGHResultState.MISSING
        )
