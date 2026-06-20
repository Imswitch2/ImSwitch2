"""Device plugin manifest parsing and contribution dataclass."""

import json
from dataclasses import dataclass
from typing import Literal


class ManifestError(Exception):
    """Raised when a manifest is invalid."""
    pass


# NOTE: `stand` and `pulse_generator` are accepted in manifests but are NOT
# loaded through MultiManager in the first implementation (bespoke loaders).
DeviceKind = Literal[
    "detector",
    "laser",
    "positioner",
    "rotator",
    "rs232",
    "flip_mirror",
    "stand",
    "slm",
    "pulse_generator",
]


@dataclass(frozen=True)
class DeviceManagerContribution:
    """Metadata describing a device manager contribution from a plugin."""
    id: str
    kind: DeviceKind
    display_name: str
    python_name: str
    plugin_name: str
    plugin_version: str | None = None
    mock_python_name: str | None = None
    manager_name_aliases: tuple[str, ...] = ()
    manager_properties_schema: str | None = None
    setup_templates: tuple[str, ...] = ()
    docs_url: str | None = None
    supported_platforms: tuple[str, ...] = ()


def parse_manifest(
    data: dict,
    *,
    plugin_name: str,
    plugin_version: str | None,
) -> list[DeviceManagerContribution]:
    """Parse a manifest dictionary and return device manager contributions.
    
    Args:
        data: The parsed manifest JSON dictionary.
        plugin_name: The distribution name of the plugin.
        plugin_version: The version of the plugin, or None if unavailable.
    
    Returns:
        List of DeviceManagerContribution instances.
    
    Raises:
        ManifestError: If required fields are missing or invalid.
    """
    contributions = []
    device_managers = data.get("contributions", {}).get("device_managers", [])
    
    valid_kinds = {
        "detector", "laser", "positioner", "rotator", "rs232",
        "flip_mirror", "stand", "slm", "pulse_generator",
    }
    
    for entry in device_managers:
        # Validate required fields
        for field in ["id", "kind", "display_name", "python_name"]:
            if field not in entry:
                raise ManifestError(
                    f"Missing required field '{field}' in plugin '{plugin_name}'"
                )
        
        # Validate kind
        kind = entry["kind"]
        if kind not in valid_kinds:
            raise ManifestError(
                f"Unknown device kind '{kind}' in plugin '{plugin_name}'. "
                f"Valid kinds: {sorted(valid_kinds)}"
            )
        
        # Coerce list fields to tuples
        manager_name_aliases = tuple(entry.get("manager_name_aliases", []))
        setup_templates = tuple(entry.get("setup_templates", []))
        supported_platforms = tuple(entry.get("supported_platforms", []))
        
        contribution = DeviceManagerContribution(
            id=entry["id"],
            kind=kind,
            display_name=entry["display_name"],
            python_name=entry["python_name"],
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            mock_python_name=entry.get("mock_python_name"),
            manager_name_aliases=manager_name_aliases,
            manager_properties_schema=entry.get("manager_properties_schema"),
            setup_templates=setup_templates,
            docs_url=entry.get("docs_url"),
            supported_platforms=supported_platforms,
        )
        contributions.append(contribution)
    
    return contributions
