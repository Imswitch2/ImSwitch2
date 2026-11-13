"""Device-level comparison helpers for Config Studio split view.

The comparison deliberately operates on the same named-device sections used by
Config Studio.  It compares the complete stored device definition *and* its
active/disabled state, while leaving config-wide/singleton settings out of
scope.  The result is presentation-neutral so the card view (3B) and a future
summary view can share the same semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .disabled_devices import DISABLED_DEVICES_KEY, locate_device


SAME = "same"
DIFFERENT = "different"
LEFT_ONLY = "left_only"
RIGHT_ONLY = "right_only"


def _category_names(data: Mapping[str, Any], category: str) -> set[str]:
    names: set[str] = set()
    active = data.get(category)
    if isinstance(active, Mapping):
        names.update(str(name) for name in active)

    disabled_root = data.get(DISABLED_DEVICES_KEY)
    if isinstance(disabled_root, Mapping):
        disabled = disabled_root.get(category)
        if isinstance(disabled, Mapping):
            names.update(str(name) for name in disabled)
    return names


def compare_device(
    left: Mapping[str, Any], right: Mapping[str, Any], category: str, name: str
) -> str | None:
    """Classify one named device across two setup dictionaries.

    ``same`` means both the complete definition and enabled/disabled state are
    equal.  A state-only change therefore counts as ``different``.
    """
    left_device, left_enabled = locate_device(left, category, name)
    right_device, right_enabled = locate_device(right, category, name)

    if left_device is None and right_device is None:
        return None
    if left_device is None:
        return RIGHT_ONLY
    if right_device is None:
        return LEFT_ONLY
    if left_enabled == right_enabled and left_device == right_device:
        return SAME
    return DIFFERENT


def compare_configs(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    categories: Iterable[str],
) -> dict[tuple[str, str], str]:
    """Return global comparison states keyed by ``(category, device name)``."""
    result: dict[tuple[str, str], str] = {}
    for category in categories:
        names = _category_names(left, category) | _category_names(right, category)
        for name in sorted(names, key=str.casefold):
            state = compare_device(left, right, category, name)
            if state is not None:
                result[(category, name)] = state
    return result


def status_for_side(state: str, side: str) -> str:
    """Translate a global comparison state into one pane's card status."""
    if state == SAME:
        return SAME
    if state == DIFFERENT:
        return DIFFERENT
    if state == LEFT_ONLY:
        return "only_here" if side == "left" else "missing_here"
    if state == RIGHT_ONLY:
        return "only_here" if side == "right" else "missing_here"
    raise ValueError(f"unknown comparison state {state!r}")
