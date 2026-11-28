"""Core dimensions: the axes one call of a plugin consumes, and what is left to loop over.

A plugin may declare the dimensions it works on, its *core dims*
(``core_dims_in``): SNOUTY deskews one ``(planes, cam_y, cam_x)`` stack, a
2-D filter one ``(Y, X)`` plane. Whatever the input has *in front* of them
(T, C, a position) the runner loops over, LabVIEW-style auto-indexing, and
stacks the outputs with those axes kept in front
(:mod:`~imswitch.improcess.model.iteration`). Most loops then never have to
be written.

How a declaration reads:

* the core dims are always the **trailing** axes of the input, in order; the
  loop axes are the ones in front of them;
* an entry is an axis label (``"Y"``), several labels any of which will do
  (``"Z|Frame|Plane"``: the plane axis of a raw stack is called different
  things by different writers), or ``"*"`` for any axis;
* labels compare case-insensitively.

An input of exactly the core rank is passed to the plugin as it always was,
whatever its labels say: a raw file's labels are often a guess from its rank,
and refusing it now would break files that reconstruct today. Labels are
checked only when there is something to loop over, because only then does
the declaration decide which axes are which.

Pure: no Qt, no plugin imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

#: The reserved step parameter that switches auto-indexing off for one step
#: (``params: {_iterate: false}``): the plugin then gets the whole stack. Never
#: passed to the plugin; recorded on the provenance node as ``iterate: false``.
ITERATE_PARAM = "_iterate"

#: Matches any axis.
ANY_AXIS = "*"

#: ``core_dims_in = WHOLE_INPUT``: the plugin consumes its whole input in one
#: call and is never auto-indexed (view-only, a localizer that wants the whole
#: movie). Declared on purpose -- unlike ``None``, which means the plugin was
#: written before core dims existed and says nothing (it is treated the same
#: way, so old and third-party plugins keep working).
WHOLE_INPUT: tuple = ()


def declared(core_dims) -> bool:
    """Whether a plugin stated its core dims (a tuple, possibly
    :data:`WHOLE_INPUT`) rather than leaving them ``None``."""
    return core_dims is not None


class CoreDimsError(ValueError):
    """The input's axes do not fit the plugin's declared core dims."""


@dataclass(frozen=True)
class CoreMatch:
    """How an input's axes split into loop axes and core axes."""

    #: Labels of the leading axes the runner loops over (may be empty).
    loop_labels: tuple[str, ...] = ()
    #: Their lengths.
    loop_shape: tuple[int, ...] = ()
    #: Labels of the trailing axes one call consumes.
    core_labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def iterates(self) -> bool:
        return bool(self.loop_labels)

    @property
    def count(self) -> int:
        total = 1
        for size in self.loop_shape:
            total *= int(size)
        return total


def _alternatives(entry: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in str(entry).split("|") if part.strip())


def entry_matches(entry: str, label: str) -> bool:
    """Whether the axis ``label`` satisfies one core-dims ``entry``."""
    options = _alternatives(entry)
    return ANY_AXIS in options or str(label).lower() in options


def describe(core_dims: Sequence[str]) -> str:
    """``(Z|Frame, Y, X)`` for a message."""
    return "(" + ", ".join(str(entry) for entry in core_dims) + ")"


def match_core_dims(axis_labels: Sequence[str], shape: Sequence[int], core_dims: Sequence[str] | None) -> CoreMatch:
    """Split ``axis_labels``/``shape`` into loop and core axes; :class:`CoreDimsError` if they do not fit.

    ``core_dims`` ``None`` (the plugin declares nothing) or an input of
    exactly the core rank give an empty loop: the plugin is called once, as
    it always was.
    """
    labels = [str(label) for label in (axis_labels or [])]
    shape = tuple(int(size) for size in shape)
    if len(labels) != len(shape):
        # Labels that do not describe the array cannot decide anything.
        labels = [f"axis{index}" for index in range(len(shape))]
    if not core_dims:
        return CoreMatch(core_labels=tuple(labels))
    core = tuple(str(entry) for entry in core_dims)
    if len(shape) < len(core):
        raise CoreDimsError(
            f"needs at least {len(core)} axes {describe(core)}, got {len(shape)} "
            f"({', '.join(labels) or 'none'}, shape {shape})"
        )
    if len(shape) == len(core):
        return CoreMatch(core_labels=tuple(labels))
    split = len(shape) - len(core)
    trailing = labels[split:]
    wrong = [(entry, label) for entry, label in zip(core, trailing) if not entry_matches(entry, label)]
    if wrong:
        hint = ""
        named = [entry for entry in core if ANY_AXIS not in _alternatives(entry)]
        present = [entry for entry in named if any(entry_matches(entry, label) for label in labels)]
        if len(present) == len(named):
            hint = " (the axes are there but not last: reorder them, e.g. in a Python step)"
        raise CoreDimsError(
            f"works on the last {len(core)} axes {describe(core)}, but the input's are "
            f"({', '.join(trailing)}) of ({', '.join(labels)}){hint}; relabel the axes "
            f"(a Python step's make_result(..., axes=[...])) or set {ITERATE_PARAM}: false "
            "to hand the plugin the whole stack"
        )
    return CoreMatch(
        loop_labels=tuple(labels[:split]),
        loop_shape=tuple(shape[:split]),
        core_labels=tuple(trailing),
    )


def wants_iteration(params: dict | None) -> bool:
    """``False`` when the step asked for the whole stack (``_iterate: false``)."""
    value = (params or {}).get(ITERATE_PARAM, True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def without_reserved(params: dict | None) -> dict:
    """``params`` without the reserved :data:`ITERATE_PARAM` key."""
    return {key: value for key, value in dict(params or {}).items() if key != ITERATE_PARAM}


__all__ = [
    "ANY_AXIS",
    "WHOLE_INPUT",
    "declared",
    "CoreDimsError",
    "CoreMatch",
    "ITERATE_PARAM",
    "describe",
    "entry_matches",
    "match_core_dims",
    "wants_iteration",
    "without_reserved",
]


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
