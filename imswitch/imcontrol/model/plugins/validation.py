"""Device plugin validation utilities and setup file validation."""

import importlib.resources
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

from .manifest import DeviceManagerContribution
from .registry import DevicePluginRegistry


# Inverse of MultiManager's map: kind -> subpackage name
KIND_TO_SUBMANAGERS_PACKAGE: dict[str, str] = {
    "detector": "detectors",
    "laser": "lasers",
    "positioner": "positioners",
    "rotator": "rotators",
    "rs232": "rs232",
    "flip_mirror": "flipMirrors",
    "slm": "slms",
}

# Setup JSON section name -> device kind
SETUP_SECTION_TO_KIND: dict[str, str] = {
    "detectors": "detector",
    "lasers": "laser",
    "positioners": "positioner",
    "rotators": "rotator",
    "rs232devices": "rs232",
    "slms": "slm",
    "flipMirrors": "flip_mirror",
}


def legacy_manager_exists(kind: str, manager_name: str) -> bool:
    """Check if a legacy internal manager module exists without importing it.
    
    Args:
        kind: The device kind (e.g., "detector", "laser").
        manager_name: The manager class name to check.
    
    Returns:
        True if the module exists, False otherwise.
    """
    subpackage = KIND_TO_SUBMANAGERS_PACKAGE.get(kind)
    if subpackage is None:
        return False
    
    module_path = f"imswitch.imcontrol.model.managers.{subpackage}.{manager_name}"
    
    try:
        spec = importlib.util.find_spec(module_path)
        return spec is not None
    except Exception:
        # If find_spec raises (e.g. missing hardware dep in __init__.py), return False
        return False


def load_jsonschema_validator():
    """Lazily load the jsonschema module, returning None if unavailable.
    
    Returns:
        The jsonschema module if installed, or None.
    """
    try:
        import jsonschema
        return jsonschema
    except ImportError:
        return None


def resolve_schema(contribution: DeviceManagerContribution) -> dict | None:
    """Load and parse a manager properties JSON schema from a contribution.
    
    Args:
        contribution: The device manager contribution.
    
    Returns:
        The parsed schema dict, or None if unavailable or invalid.
    """
    if contribution.source_package is None:
        return None
    if contribution.manager_properties_schema is None:
        return None
    
    try:
        resource = (
            importlib.resources.files(contribution.source_package)
            / contribution.manager_properties_schema
        )
        schema_text = resource.read_text(encoding="utf-8")
        return json.loads(schema_text)
    except Exception:
        # File not found, JSON parse error, etc. - just return None
        return None


def validate_manager_properties(schema: dict, properties: dict) -> list[str]:
    """Validate manager properties against a JSON schema.
    
    Args:
        schema: The JSON schema dictionary.
        properties: The properties to validate.
    
    Returns:
        List of human-readable validation error messages (empty if valid).
        Returns empty list if jsonschema is not installed (graceful degradation).
    """
    jsonschema = load_jsonschema_validator()
    if jsonschema is None:
        # Gracefully degrade - return empty list, not an error
        return []
    
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    
    for error in validator.iter_errors(properties):
        # Build a readable error message with path and message
        path = ".".join(str(p) for p in error.path) if error.path else "(root)"
        errors.append(f"{path}: {error.message}")
    
    return errors


@dataclass
class DeviceValidationResult:
    """Validation result for a single device in a setup file."""
    section: str
    device_name: str | None
    manager_name: str
    resolved_via: str  # "registry", "legacy", or "UNRESOLVED"
    schema_warnings: list[str]


@dataclass
class ValidationReport:
    """Validation report for an entire setup file."""
    path: str
    devices: list[DeviceValidationResult]
    jsonschema_available: bool
    
    @property
    def has_errors(self) -> bool:
        """Check if any devices are unresolved."""
        return any(d.resolved_via == "UNRESOLVED" for d in self.devices)
    
    def format(self) -> str:
        """Format the report as a multi-line human-readable string."""
        lines = [f"Validation report for: {self.path}", ""]
        
        if not self.jsonschema_available:
            lines.append("NOTE: jsonschema not installed, schema validation skipped.")
            lines.append("")
        
        if not self.devices:
            lines.append("No devices found in setup file.")
            return "\n".join(lines)
        
        # Group by section
        by_section: dict[str, list[DeviceValidationResult]] = {}
        for device in self.devices:
            by_section.setdefault(device.section, []).append(device)
        
        for section in sorted(by_section.keys()):
            lines.append(f"[{section}]")
            for device in by_section[section]:
                device_label = device.device_name or "(unnamed)"
                status_marker = "✓" if device.resolved_via != "UNRESOLVED" else "✗"
                lines.append(
                    f"  {status_marker} {device_label}: {device.manager_name} "
                    f"→ {device.resolved_via}"
                )
                
                if device.schema_warnings:
                    for warning in device.schema_warnings:
                        lines.append(f"      ⚠ {warning}")
            lines.append("")
        
        if self.has_errors:
            lines.append("❌ Validation FAILED: unresolved managers detected.")
        else:
            lines.append("✓ All managers resolved successfully.")
        
        return "\n".join(lines)


def validate_setup_file(path: str | Path, registry: DevicePluginRegistry) -> ValidationReport:
    """Validate a setup JSON file against the registry.
    
    Args:
        path: Path to the setup JSON file.
        registry: The device plugin registry to resolve managers against.
    
    Returns:
        A ValidationReport with per-device results.
    """
    path = Path(path)
    jsonschema = load_jsonschema_validator()
    
    with open(path, "r", encoding="utf-8") as f:
        setup_data = json.load(f)
    
    devices = []
    
    for section_name, kind in SETUP_SECTION_TO_KIND.items():
        section_data = setup_data.get(section_name)
        if section_data is None:
            continue
        
        for device_entry in section_data:
            manager_name = device_entry.get("managerName")
            if manager_name is None:
                continue
            
            device_name = device_entry.get("name")
            manager_properties = device_entry.get("managerProperties", {})
            
            # Resolve the manager
            contribution = registry.resolve(kind, manager_name)
            
            if contribution is not None:
                resolved_via = "registry"
            elif legacy_manager_exists(kind, manager_name):
                resolved_via = "legacy"
            else:
                resolved_via = "UNRESOLVED"
            
            # Validate properties if we have a schema
            schema_warnings = []
            if contribution is not None and jsonschema is not None:
                schema = resolve_schema(contribution)
                if schema is not None:
                    validation_errors = validate_manager_properties(
                        schema, manager_properties
                    )
                    schema_warnings.extend(validation_errors)
            
            devices.append(
                DeviceValidationResult(
                    section=section_name,
                    device_name=device_name,
                    manager_name=manager_name,
                    resolved_via=resolved_via,
                    schema_warnings=schema_warnings,
                )
            )
    
    return ValidationReport(
        path=str(path),
        devices=devices,
        jsonschema_available=jsonschema is not None,
    )
