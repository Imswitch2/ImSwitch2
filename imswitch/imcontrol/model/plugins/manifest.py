"""Device plugin manifest parsing and contribution dataclass."""

import json
import logging
from dataclasses import dataclass
from typing import Literal


class ManifestError(Exception):
    """Raised when a manifest is invalid."""
    pass


# Kinds that are loaded through MultiManager and fully support plugin resolution.
# These have deterministic registry-first resolution with legacy fallback.
MULTIMANAGER_BACKED_KINDS = {
    "detector",
    "laser",
    "positioner",
    "rotator",
    "rs232",
    "flip_mirror",
    "slm",
}

# Kinds that are registry-backed but not loaded by MultiManager. These use a
# dedicated loader that still resolves through DevicePluginRegistry.
STANDALONE_REGISTRY_BACKED_KINDS = {
    "stand",
}

# Kinds that use bespoke loaders in the current implementation. Plugin support
# for these kinds is NOT yet implemented in runtime loading. Manifests declaring
# these kinds will be rejected with a clear error.
BESPOKE_LOADER_KINDS = {
    "pulse_generator",
}

# All kinds that can currently be resolved through DevicePluginRegistry.
REGISTRY_BACKED_KINDS = (
    MULTIMANAGER_BACKED_KINDS | STANDALONE_REGISTRY_BACKED_KINDS
)

# All valid device kinds (union of registry-backed and remaining bespoke kinds).
ALL_VALID_KINDS = REGISTRY_BACKED_KINDS | BESPOKE_LOADER_KINDS

# Type hint for device kinds (accepts all kinds for forward compatibility).
DeviceKind = Literal[
    "detector",
    "laser",
    "positioner",
    "rotator",
    "rs232",
    "flip_mirror",
    "slm",
    "stand",
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
    source_package: str | None = None
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
    source_package: str | None = None,
) -> list[DeviceManagerContribution]:
    """Parse a manifest dictionary and return device manager contributions.
    
    Args:
        data: The parsed manifest JSON dictionary.
        plugin_name: The distribution name of the plugin.
        plugin_version: The version of the plugin, or None if unavailable.
        source_package: The package name for resolving resources, or None.
    
    Returns:
        List of DeviceManagerContribution instances.
    
    Raises:
        ManifestError: If required fields are missing or invalid, or if a
            kind is not supported by runtime loading.
    """
    logger = logging.getLogger('imswitch.plugins.manifest')
    contributions = []
    device_managers = data.get("contributions", {}).get("device_managers", [])
    
    for entry in device_managers:
        # Validate required fields
        for field in ["id", "kind", "display_name", "python_name"]:
            if field not in entry:
                raise ManifestError(
                    f"Missing required field '{field}' in plugin '{plugin_name}'"
                )
        
        # Validate kind
        kind = entry["kind"]
        if kind not in ALL_VALID_KINDS:
            raise ManifestError(
                f"Unknown device kind '{kind}' in plugin '{plugin_name}'. "
                f"Valid kinds: {sorted(ALL_VALID_KINDS)}"
            )
        
        # Reject kinds that use bespoke loaders (not yet supported by plugin system)
        if kind in BESPOKE_LOADER_KINDS:
            raise ManifestError(
                f"Device kind '{kind}' in plugin '{plugin_name}' is not supported "
                f"by runtime plugin loading. Kind '{kind}' uses a bespoke loader "
                f"and plugin support is not yet implemented. "
                f"Supported registry-backed kinds: {sorted(REGISTRY_BACKED_KINDS)}"
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
            source_package=source_package,
            mock_python_name=entry.get("mock_python_name"),
            manager_name_aliases=manager_name_aliases,
            manager_properties_schema=entry.get("manager_properties_schema"),
            setup_templates=setup_templates,
            docs_url=entry.get("docs_url"),
            supported_platforms=supported_platforms,
        )
        contributions.append(contribution)
    
    return contributions
