"""Helpers for temporarily disabling named hardware devices in setup files.

Disabled devices are stored outside the active setup sections under the
``disabledDevices`` top-level key.  This keeps the runtime invariant that
``SetupInfo.detectors`` / ``lasers`` / ``positioners`` / ... contain only
hardware that should be instantiated, while preserving the full device entry
for later re-enabling in the config editor.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from imswitch.imcontrol.model.plugins.setup_metadata import setup_kinds


DISABLED_DEVICES_KEY = "disabledDevices"

# Only repeated, registry-backed device sections are safe to move between the
# active setup and disabledDevices.  Singleton/special sections such as
# microscopeStand and the bespoke pulse-generator loader deliberately do not
# participate.  Nor do sections whose entries other sections name: a scan
# engine (``scan.engine``, another engine's ``triggerInput``) or a pulse
# generator (a laser's or an engine's ``pulseGenerator``) moved out of the
# active setup would leave those names dangling, and the setup refused at load.
_NAMED_ELSEWHERE = frozenset({"scanEngines", "pulseGenerators"})
DISABLEABLE_CATEGORIES = tuple(
    metadata.editor_category
    for metadata in setup_kinds()
    if metadata.supports_external_plugins
    and metadata.editor_category == metadata.setup_section
    and metadata.editor_category not in _NAMED_ELSEWHERE
)


def is_disableable_category(category: str) -> bool:
    return category in DISABLEABLE_CATEGORIES


def device_section(
    data: MutableMapping[str, Any],
    category: str,
    *,
    enabled: bool,
    create: bool = False,
) -> MutableMapping[str, Any]:
    """Return the active or disabled map for ``category``.

    ``create=False`` never mutates ``data`` and returns an empty temporary map
    when the requested section does not exist.  ``create=True`` creates the
    corresponding dictionaries as needed.
    """
    if enabled:
        section = data.get(category)
        if isinstance(section, MutableMapping):
            return section
        if create:
            section = {}
            data[category] = section
            return section
        return {}

    root = data.get(DISABLED_DEVICES_KEY)
    if not isinstance(root, MutableMapping):
        if not create:
            return {}
        root = {}
        data[DISABLED_DEVICES_KEY] = root

    section = root.get(category)
    if isinstance(section, MutableMapping):
        return section
    if create:
        section = {}
        root[category] = section
        return section
    return {}


def locate_device(
    data: MutableMapping[str, Any], category: str, name: str
) -> tuple[Any | None, bool | None]:
    """Return ``(device, enabled)`` for ``name`` or ``(None, None)``.

    Active entries take precedence if a malformed file contains the same name
    in both active and disabled sections.
    """
    active = device_section(data, category, enabled=True)
    if name in active:
        return active[name], True
    disabled = device_section(data, category, enabled=False)
    if name in disabled:
        return disabled[name], False
    return None, None


def set_device_enabled(
    data: MutableMapping[str, Any], category: str, name: str, enabled: bool
) -> bool:
    """Move one device between active and disabled storage.

    Returns ``True`` when a move occurred and ``False`` when the device was
    already in the requested state or did not exist.  A destination name
    collision is rejected rather than overwriting either definition.
    """
    if not is_disableable_category(category):
        raise ValueError(f"category {category!r} does not support temporary disabling")

    source = device_section(data, category, enabled=not enabled)
    if name not in source:
        return False
    destination = device_section(data, category, enabled=enabled, create=True)
    if name in destination:
        raise ValueError(
            f"cannot {'enable' if enabled else 'disable'} {name!r}: "
            "a device with that name already exists in the destination section"
        )

    destination[name] = source.pop(name)
    _prune_empty_disabled_sections(data)
    return True


def remove_device(data: MutableMapping[str, Any], category: str, name: str) -> Any | None:
    """Remove ``name`` from whichever state currently contains it."""
    device, enabled = locate_device(data, category, name)
    if enabled is None:
        return None
    section = device_section(data, category, enabled=enabled)
    removed = section.pop(name, None)
    _prune_empty_disabled_sections(data)
    return removed


def _prune_empty_disabled_sections(data: MutableMapping[str, Any]) -> None:
    root = data.get(DISABLED_DEVICES_KEY)
    if not isinstance(root, MutableMapping):
        return
    for category in list(root):
        section = root.get(category)
        if isinstance(section, MutableMapping) and not section:
            del root[category]
    if not root:
        data.pop(DISABLED_DEVICES_KEY, None)
