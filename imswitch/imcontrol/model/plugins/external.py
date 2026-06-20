"""Install hints for device managers that live in external plugin packages.

This is the Phase 8 safety net for gradual extraction (see
``docs/design/DEVICE_PLUGINS.md``). When a setup file names a manager that
ImSwitch cannot resolve — neither a registered plugin contribution nor an
in-tree legacy module — these hints let ImSwitch tell the user exactly which
package provides it, instead of an opaque ``ImportError``.

When an in-tree manager is moved into a plugin, add its old setup ``managerName``
(plugin id, legacy class name, and any aliases) together with its ``kind`` here,
pointing at the package that now provides it. Keep the entry for at least two
minor releases so existing setup files get a clear "install <package>" message.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalManagerHint:
    """Where to obtain a device manager that is not currently installed."""

    package: str
    """Distribution name that provides the manager, e.g. ``imswitch-device-x``."""

    extra: str | None = None
    """Optional pip extra needed for hardware support, e.g. ``hardware``."""

    note: str | None = None
    """Optional human-readable note (what it is, where it lives)."""

    def install_command(self) -> str:
        target = f"{self.package}[{self.extra}]" if self.extra else self.package
        return f"pip install {target}"


# Example/bundled plugins. Real extractions of in-tree managers add their entries
# here when the manager leaves the core tree.
_ZHINST = ExternalManagerHint(
    package="imswitch-zhinst-devices",
    extra="hardware",
    note=(
        "Zurich Instruments lock-in detector; bundled under "
        "examples/plugins/imswitch-zhinst-devices in the ImSwitch repository."
    ),
)

#: Keyed by ``(kind, managerName)``. The name matches a setup ``managerName`` —
#: a plugin id, a legacy class name, or an alias. One hint may appear under
#: several keys.
# First extracted in-tree manager: Thorlabs TSI cameras moved to a plugin. The
# in-tree manager still exists during the transition, so this hint is dormant
# until the in-tree copy is removed; it then turns an old setup into a clear
# "install the plugin" message.
_THORLABS = ExternalManagerHint(
    package="imswitch-device-thorlabs",
    extra="hardware",
    note=(
        "Thorlabs scientific camera (TSI); bundled under "
        "examples/plugins/imswitch-device-thorlabs in the ImSwitch repository."
    ),
)

KNOWN_EXTERNAL_MANAGERS: dict[tuple[str, str], ExternalManagerHint] = {
    ("detector", "zhinst.lockin-demod"): _ZHINST,
    ("detector", "ZhinstLockinDetectorManager"): _ZHINST,
    ("detector", "ZurichLockinDetectorManager"): _ZHINST,
    ("detector", "thorlabs.tsi-camera"): _THORLABS,
    ("detector", "ThorCamTSIManager"): _THORLABS,
}


def lookup_external_hint(kind: str, manager_name: str) -> ExternalManagerHint | None:
    """Return the install hint for an unresolved manager, or ``None``."""
    return KNOWN_EXTERNAL_MANAGERS.get((kind, manager_name))
