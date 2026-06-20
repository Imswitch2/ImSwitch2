"""Device plugin discovery through entry points."""

import importlib.metadata
import importlib.resources
import json
from dataclasses import dataclass

from .manifest import parse_manifest, DeviceManagerContribution, ManifestError


@dataclass
class DiscoveryError:
    """Error encountered during plugin discovery."""
    plugin_name: str
    manifest: str | None
    error: str


def discover_contributions(
    entry_points=None,
) -> tuple[list[DeviceManagerContribution], list[DiscoveryError]]:
    """Discover device manager contributions from installed plugins.
    
    Args:
        entry_points: Optional iterable of entry points to process.
            If None, reads from importlib.metadata with group "imswitch.manifest".
    
    Returns:
        Tuple of (contributions, errors). Errors are collected per-plugin
        so one broken plugin does not prevent others from being discovered.
    """
    if entry_points is None:
        entry_points = importlib.metadata.entry_points(group="imswitch.manifest")
    
    contributions = []
    errors = []
    
    for entry_point in entry_points:
        plugin_name = entry_point.name
        manifest_path = None
        
        try:
            # Parse the entry point value as "package:resource_path"
            # DO NOT call entry_point.load() - it would try to import an attribute
            raw_value = entry_point.value
            if ":" not in raw_value:
                raise ValueError(
                    f"Entry point value must be 'package:resource_path', got '{raw_value}'"
                )
            
            package_name, resource_path = raw_value.split(":", 1)
            manifest_path = f"{package_name}:{resource_path}"
            
            # Resolve the manifest file
            resource = importlib.resources.files(package_name) / resource_path
            manifest_text = resource.read_text(encoding="utf-8")
            manifest_data = json.loads(manifest_text)
            
            # Get plugin version from distribution if available
            plugin_version = None
            if hasattr(entry_point, "dist") and entry_point.dist is not None:
                plugin_version = entry_point.dist.version
            
            # Parse the manifest
            plugin_contributions = parse_manifest(
                manifest_data,
                plugin_name=plugin_name,
                plugin_version=plugin_version,
            )
            contributions.extend(plugin_contributions)
            
        except Exception as e:
            error = DiscoveryError(
                plugin_name=plugin_name,
                manifest=manifest_path,
                error=str(e),
            )
            errors.append(error)
    
    return contributions, errors
