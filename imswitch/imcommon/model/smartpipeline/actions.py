"""What an action step may name: the catalogue's view of an action.

An :class:`ActionSpec` is the static half of an action -- what it is called,
which port kinds it takes, its parameters and their defaults, which
parameter names the frame its points must be in, and whether it touches
hardware. The dynamic half (starting it, its completion) is the engine's
action backend. The live catalogue is generated in ImControl from the
scripting API (Phase 2); the engine's tests and the offline run use a mock
one. The document checks an action step against whichever it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: What an action's input port may carry.
ACTION_INPUT_KINDS = ("points", "value", "event", "any")


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str = ""
    #: The port kinds the action's inputs may carry, in input order; a
    #: shorter tuple than the inputs repeats its last entry; ``"any"`` takes
    #: whatever arrives. An action with no inputs is triggered by the chain.
    accepts: tuple[str, ...] = ()
    #: Parameter defaults; a step may set any of these keys and no other.
    params: Mapping[str, Any] = field(default_factory=dict)
    #: The parameter whose value is the frame tag points must be in, or
    #: ``None`` for an action that takes no points.
    frame_param: str | None = None
    #: Touches hardware: logged and refused unless the run mode allows it.
    hardware: bool = True
    description: str = ""

    def __post_init__(self):
        for kind in self.accepts:
            if kind not in ACTION_INPUT_KINDS:
                raise ValueError(f"action {self.id!r}: input kind {kind!r} is not one of {ACTION_INPUT_KINDS}")
        object.__setattr__(self, "params", dict(self.params))

    def accepted_kind(self, index: int) -> str:
        """The kind the ``index``-th input may carry (``"any"`` past the end
        for an action that accepts nothing in particular)."""
        if not self.accepts:
            return "any"
        return self.accepts[min(index, len(self.accepts) - 1)]

    def frame_of(self, params: Mapping[str, Any]) -> str | None:
        """The frame tag this action's points must be in, with ``params``."""
        if not self.frame_param:
            return None
        value = {**self.params, **dict(params or {})}.get(self.frame_param)
        return str(value) if value else None


class ActionCatalog:
    """The actions a pipeline may name, by id."""

    def __init__(self, specs=()):
        self._by_id: dict[str, ActionSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ActionSpec) -> None:
        self._by_id[spec.id] = spec

    def get(self, action_id: str) -> ActionSpec | None:
        return self._by_id.get(str(action_id))

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def specs(self) -> tuple[ActionSpec, ...]:
        return tuple(self._by_id.values())

    def __contains__(self, action_id) -> bool:
        return str(action_id) in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


__all__ = ["ACTION_INPUT_KINDS", "ActionCatalog", "ActionSpec"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
