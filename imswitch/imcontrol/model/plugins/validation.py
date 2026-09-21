"""Device plugin validation utilities and setup file validation."""

import importlib.resources
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

from .manifest import DeviceManagerContribution
from .registry import DevicePluginRegistry
from .setup_metadata import (
    KIND_METADATA,
    daq_device_categories,
    setup_section_to_kind,
)

# Compatibility exports.  Setup-kind metadata owns these mappings.
SETUP_SECTION_TO_KIND: dict[str, str] = setup_section_to_kind()
DAQ_DEVICE_CATEGORIES = list(daq_device_categories())


def legacy_manager_exists(kind: str, manager_name: str) -> bool:
    """Check if a legacy internal manager module exists without importing it.
    
    Args:
        kind: The device kind (e.g., "detector", "laser").
        manager_name: The manager class name to check.
    
    Returns:
        True if the module exists, False otherwise.
    """
    metadata = KIND_METADATA.get(kind)
    if metadata is None:
        return False

    module_path = (
        "imswitch.imcontrol.model.managers."
        f"{metadata.legacy_manager_directory}.{manager_name}"
    )
    
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


def schema_for(
    kind: str,
    manager_name: str,
    contribution: DeviceManagerContribution | None,
    *,
    schemas_root: Path | None = None,
) -> dict | None:
    """The one place a manager's ``managerProperties`` schema is looked up.

    A contribution that ships its own schema wins. Otherwise the generated
    schema from package data, by the contribution's id if there is one, else
    by the manager name -- which is how the legacy-scanned core managers get
    theirs. Returns None when nothing describes this manager.
    """
    if contribution is not None:
        schema = resolve_schema(contribution)
        if schema is not None:
            return schema
    from imswitch.imcontrol.model.configeditor import resources

    for name in ((contribution.id if contribution is not None else None), manager_name):
        if name:
            schema = resources.generated_schema_for(name, schemas_root)
            if schema is not None:
                return schema
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


@dataclass(frozen=True)
class SetupFix:
    """A suggested fix for a diagnostic issue."""
    action: str
    target: str | None = None


@dataclass(frozen=True)
class SetupDiagnostic:
    """A single validation diagnostic for a setup configuration."""
    severity: str
    code: str
    message: str
    path: tuple
    fix: SetupFix | None = None


@dataclass(frozen=True)
class ValidationContext:
    """Optional editor-supplied context for validation checks."""
    widget_requires_section: dict | None = None
    known_reconstructor_ids: tuple | None = None
    known_processor_ids: tuple | None = None


@dataclass
class ValidationReport:
    """Validation report for an entire setup file."""
    path: str | None
    diagnostics: list[SetupDiagnostic]
    jsonschema_available: bool
    
    @property
    def has_errors(self) -> bool:
        """Check if any diagnostics have error severity."""
        return any(d.severity == "error" for d in self.diagnostics)
    
    def format(self) -> str:
        """Format the report as a multi-line human-readable string."""
        lines = []
        if self.path:
            lines.append(f"Validation report for: {self.path}")
            lines.append("")
        
        if not self.jsonschema_available:
            lines.append("NOTE: jsonschema not installed, schema validation skipped.")
            lines.append("")
        
        if not self.diagnostics:
            lines.append("✓ No issues found.")
            return "\n".join(lines)
        
        # Group by severity
        by_severity = {"error": [], "warning": [], "note": []}
        for diagnostic in self.diagnostics:
            severity = diagnostic.severity
            if severity in by_severity:
                by_severity[severity].append(diagnostic)
        
        # Display errors first, then warnings, then notes
        for severity in ["error", "warning", "note"]:
            items = by_severity[severity]
            if not items:
                continue
            
            if severity == "error":
                lines.append("❌ ERRORS:")
            elif severity == "warning":
                lines.append("⚠️  WARNINGS:")
            else:
                lines.append("ℹ️  NOTES:")
            
            for diag in items:
                path_str = ".".join(str(p) for p in diag.path) if diag.path else "(root)"
                lines.append(f"  [{diag.code}] {path_str}")
                lines.append(f"    {diag.message}")
            lines.append("")
        
        if self.has_errors:
            lines.append("❌ Validation FAILED.")
        else:
            lines.append("✓ Validation passed (warnings/notes may be present).")
        
        return "\n".join(lines)


def validate_setup_data(
    data: dict,
    registry: DevicePluginRegistry,
    *,
    context: ValidationContext | None = None,
    source_path: str | None = None,
) -> ValidationReport:
    """Validate a setup configuration dict.
    
    Args:
        data: The setup configuration dictionary.
        registry: The device plugin registry to resolve managers against.
        context: Optional context with editor-supplied inputs.
        source_path: Optional source file path for reporting.
    
    Returns:
        A ValidationReport with all diagnostics.
    """
    jsonschema = load_jsonschema_validator()
    diagnostics = []
    
    # 1. Manager resolution checks
    diagnostics.extend(_validate_manager_resolution(data, registry, jsonschema))
    
    # 2. DAQ conflict checks
    diagnostics.extend(_validate_daq_channels(data))
    
    # 3. Cross-reference checks
    diagnostics.extend(_validate_cross_references(data, context))
    
    return ValidationReport(
        path=source_path,
        diagnostics=diagnostics,
        jsonschema_available=jsonschema is not None,
    )


def validate_setup_file(
    path: str | Path,
    registry: DevicePluginRegistry,
    *,
    context: ValidationContext | None = None,
) -> ValidationReport:
    """Validate a setup JSON file against the registry.
    
    Args:
        path: Path to the setup JSON file.
        registry: The device plugin registry to resolve managers against.
        context: Optional context with editor-supplied inputs.
    
    Returns:
        A ValidationReport with all diagnostics.
    """
    path = Path(path)
    
    with open(path, "r", encoding="utf-8") as f:
        setup_data = json.load(f)
    
    return validate_setup_data(
        setup_data,
        registry,
        context=context,
        source_path=str(path),
    )


def _validate_manager_resolution(
    data: dict,
    registry: DevicePluginRegistry,
    jsonschema,
) -> list[SetupDiagnostic]:
    """Validate manager resolution and schemas for all devices."""
    diagnostics = []
    
    for section_name, kind in SETUP_SECTION_TO_KIND.items():
        section_data = data.get(section_name)
        
        if section_name == "microscopeStand":
            if isinstance(section_data, dict):
                manager_name = section_data.get("managerName")
                if manager_name is not None:
                    diagnostics.extend(
                        _validate_device_manager(
                            registry=registry,
                            jsonschema=jsonschema,
                            section_name=section_name,
                            kind=kind,
                            device_name=None,
                            manager_name=manager_name,
                            manager_properties=section_data.get("managerProperties", {}),
                        )
                    )
            continue

        if not isinstance(section_data, dict):
            continue

        for device_name, device_entry in section_data.items():
            if not isinstance(device_entry, dict):
                continue
            manager_name = device_entry.get("managerName")
            if manager_name is None:
                continue

            diagnostics.extend(
                _validate_device_manager(
                    registry=registry,
                    jsonschema=jsonschema,
                    section_name=section_name,
                    device_name=device_name,
                    kind=kind,
                    manager_name=manager_name,
                    manager_properties=device_entry.get("managerProperties", {}),
                )
            )
    
    return diagnostics


def _validate_device_manager(
    *,
    registry: DevicePluginRegistry,
    jsonschema,
    section_name: str,
    kind: str,
    device_name: str | None,
    manager_name: str,
    manager_properties: dict,
) -> list[SetupDiagnostic]:
    """Validate a single device's manager resolution and schema."""
    diagnostics = []
    contribution = registry.resolve(kind, manager_name)
    resolved_via = None

    if contribution is not None:
        resolved_via = "registry"
    elif legacy_manager_exists(kind, manager_name):
        resolved_via = "legacy"
    elif kind == "stand" and registry.resolve(kind, f"{manager_name}_mock"):
        contribution = registry.resolve(kind, f"{manager_name}_mock")
        resolved_via = "registry-mock"
    elif kind == "stand" and legacy_manager_exists(kind, f"{manager_name}_mock"):
        resolved_via = "legacy-mock"
    else:
        # Unresolved manager
        path = (section_name, device_name, "managerName") if device_name else (section_name, "managerName")
        diagnostics.append(SetupDiagnostic(
            severity="error",
            code="manager.unresolved",
            message=f"Manager '{manager_name}' cannot be resolved for kind '{kind}'.",
            path=path,
        ))
        return diagnostics

    # If resolved via legacy fallback, emit a note
    if resolved_via in ("legacy", "legacy-mock"):
        path = (section_name, device_name, "managerName") if device_name else (section_name, "managerName")
        diagnostics.append(SetupDiagnostic(
            severity="note",
            code="manager.legacy-fallback",
            message=f"Manager '{manager_name}' resolved via legacy fallback.",
            path=path,
        ))

    # Schema validation: the contribution's own schema, else the generated
    # one -- for registered and legacy-scanned managers alike.
    schema = schema_for(kind, manager_name, contribution)
    field_path = (section_name, device_name, "managerProperties") if device_name else (section_name, "managerProperties")
    if schema is not None and jsonschema is not None:
        for error_msg in validate_manager_properties(schema, manager_properties):
            diagnostics.append(SetupDiagnostic(
                severity="warning",
                code="manager.schema",
                message=error_msg,
                path=field_path,
            ))
    if schema is not None and isinstance(manager_properties, dict):
        from imswitch.imcontrol.model.configeditor import resources

        for canonical, spellings in resources.alias_conflicts(schema, manager_properties):
            others = ", ".join(f"'{s}'" for s in spellings if s != canonical)
            diagnostics.append(SetupDiagnostic(
                severity="warning",
                code="manager.alias-conflict",
                message=(f"'{canonical}' is also given as {others}; the manager reads "
                         f"'{canonical}' and ignores the other spelling."),
                path=field_path + (canonical,),
            ))

    return diagnostics


def _validate_daq_channels(data: dict) -> list[SetupDiagnostic]:
    """Validate DAQ channel assignments for conflicts."""
    diagnostics = []
    used_channels: dict[str, list[str]] = {}
    
    for category in DAQ_DEVICE_CATEGORIES:
        section_data = data.get(category)
        if not isinstance(section_data, dict):
            continue
        
        for device_name, device_entry in section_data.items():
            if not isinstance(device_entry, dict):
                continue
            
            for key in ("analogChannel", "digitalLine"):
                channel = device_entry.get(key)
                if channel and channel != "null":
                    used_channels.setdefault(channel, []).append(device_name)
    
    for channel, devices in used_channels.items():
        if len(devices) > 1:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="daq.conflict",
                message=f"DAQ channel '{channel}' is used by multiple devices: {', '.join(devices)}.",
                path=("<daq>", channel),
            ))
    
    return diagnostics


def _validate_cross_references(data: dict, context: ValidationContext | None) -> list[SetupDiagnostic]:
    """Validate cross-references between sections."""
    diagnostics = []
    
    detectors = data.get("detectors") or {}
    positioners = data.get("positioners") or {}
    rs232s = data.get("rs232devices") or {}
    widgets = data.get("availableWidgets") or []
    
    # focusLock ⇄ detector forFocusLock
    focus_dets = [n for n, d in detectors.items() if d.get("forFocusLock")]
    fl = data.get("focusLock")
    
    if focus_dets and not fl:
        diagnostics.append(SetupDiagnostic(
            severity="error",
            code="xref.focuslock.missing-section",
            message=f"Detector(s) flagged forFocusLock={focus_dets} but no focusLock section.",
            path=("focusLock",),
        ))
    
    if fl:
        if not focus_dets:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.focuslock.no-detector",
                message="focusLock section present but no detector has forFocusLock=true.",
                path=("focusLock",),
            ))
        
        cam = fl.get("camera")
        pos = fl.get("positioner")
        
        if cam and cam not in detectors:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.focuslock.camera-undefined",
                message=f"focusLock.camera='{cam}' is not a defined detector.",
                path=("focusLock", "camera"),
            ))
        elif cam and not detectors.get(cam, {}).get("forFocusLock"):
            diagnostics.append(SetupDiagnostic(
                severity="warning",
                code="xref.focuslock.camera-not-flagged",
                message=f"focusLock.camera='{cam}' exists but does not have forFocusLock=true.",
                path=("focusLock", "camera"),
            ))
        
        if pos and pos not in positioners:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.focuslock.positioner-undefined",
                message=f"focusLock.positioner='{pos}' is not a defined positioner.",
                path=("focusLock", "positioner"),
            ))
        elif pos:
            # A scanned positioner carrying the focus axis is assumed to be the
            # same physical actuator, because it usually is -- one piezo
            # addressed as an analog scanner and as a serial positioner. The
            # focus lock then yields for those scans. Say so explicitly with
            # physicalActuator: matching ids confirm it, differing ids declare
            # two genuinely separate stages and keep the lock running.
            focusInfo = positioners.get(pos) or {}
            focusAxes = set(focusInfo.get("axes") or [])
            focusActuator = focusInfo.get("physicalActuator")
            ambiguous = sorted(
                name for name, info in positioners.items()
                if name != pos
                and (info or {}).get("forScanning")
                and focusAxes & set((info or {}).get("axes") or [])
                and not (focusActuator and (info or {}).get("physicalActuator"))
            )
            if ambiguous:
                diagnostics.append(SetupDiagnostic(
                    severity="warning",
                    code="xref.focuslock.shared-actuator",
                    message=(
                        f"focusLock.positioner='{pos}' shares an axis with "
                        f"scanned positioner(s) {ambiguous}. The focus lock "
                        f"will assume they drive the same actuator and pause "
                        f"during those scans. Set 'physicalActuator' on both "
                        f"to state this explicitly -- equal ids to confirm, "
                        f"different ids if they are independent stages."
                    ),
                    path=("focusLock", "positioner"),
                ))

    # autofocus ⇄ devices
    af = data.get("autofocus")
    if af:
        cam = af.get("camera")
        pos = af.get("positioner")
        
        if cam and cam not in detectors:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.autofocus.camera-undefined",
                message=f"autofocus.camera='{cam}' is not a defined detector.",
                path=("autofocus", "camera"),
            ))
        
        if pos and pos not in positioners:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.autofocus.positioner-undefined",
                message=f"autofocus.positioner='{pos}' is not a defined positioner.",
                path=("autofocus", "positioner"),
            ))
    
    # tiling ⇄ positioners + optional camera
    tl = data.get("tiling")
    if tl:
        xy = tl.get("xyPositioner")
        z = tl.get("zPositioner")
        cam = tl.get("camera")
        
        if xy and xy not in positioners:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.tiling.xypositioner-undefined",
                message=f"tiling.xyPositioner='{xy}' is not a defined positioner.",
                path=("tiling", "xyPositioner"),
            ))
        
        if z and z not in positioners:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.tiling.zpositioner-undefined",
                message=f"tiling.zPositioner='{z}' is not a defined positioner.",
                path=("tiling", "zPositioner"),
            ))
        
        if cam and cam not in detectors:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.tiling.camera-undefined",
                message=f"tiling.camera='{cam}' is not a defined detector.",
                path=("tiling", "camera"),
            ))
    
    # scan ⇄ forScanning positioners
    if data.get("scan"):
        scanning = [n for n, p in positioners.items() if p.get("forScanning")]
        if not scanning:
            diagnostics.append(SetupDiagnostic(
                severity="warning",
                code="xref.scan.no-scanning-positioner",
                message="scan section present but no positioner has forScanning=true.",
                path=("scan",),
            ))
    
    # etSTED ⇄ scan
    if data.get("etSTED") and not data.get("scan"):
        diagnostics.append(SetupDiagnostic(
            severity="warning",
            code="xref.etsted.no-scan",
            message="etSTED present but no scan section.",
            path=("etSTED",),
        ))
    
    # processing ⇄ reconstructor/processor IDs
    processing = data.get("processing")
    if processing:
        # Try to get known IDs from context or import directly
        known_reconstructors = None
        known_processors = None
        
        if context and context.known_reconstructor_ids:
            known_reconstructors = set(context.known_reconstructor_ids)
        if context and context.known_processor_ids:
            known_processors = set(context.known_processor_ids)
        
        # If not in context, try importing from improcess
        if known_reconstructors is None or known_processors is None:
            try:
                from imswitch.improcess.model.plugins.registry import build_default_registry as build_improcess_registry
                improcess_registry = build_improcess_registry(discover=True)
                if known_reconstructors is None:
                    known_reconstructors = {c.id for c in improcess_registry.list_contributions(kind="reconstructor")}
                if known_processors is None:
                    known_processors = {c.id for c in improcess_registry.list_contributions(kind="processor")}
            except Exception:
                pass
        
        reconstructors = processing.get("reconstructors")
        processors = processing.get("processors")
        
        if reconstructors is not None:
            if not isinstance(reconstructors, list) or not reconstructors:
                diagnostics.append(SetupDiagnostic(
                    severity="error",
                    code="xref.processing.reconstructors-invalid",
                    message="processing.reconstructors must be a non-empty list of ImProcess reconstructor IDs.",
                    path=("processing", "reconstructors"),
                ))
            elif known_reconstructors:
                unknown = [r for r in reconstructors if r not in known_reconstructors]
                if unknown:
                    diagnostics.append(SetupDiagnostic(
                        severity="error",
                        code="xref.processing.unknown-reconstructor",
                        message=f"processing.reconstructors contains unknown plugin ID(s): {unknown}.",
                        path=("processing", "reconstructors"),
                    ))
        
        if processors is not None:
            if not isinstance(processors, list):
                diagnostics.append(SetupDiagnostic(
                    severity="error",
                    code="xref.processing.processors-invalid",
                    message="processing.processors must be a list of ImProcess processor IDs.",
                    path=("processing", "processors"),
                ))
            elif known_processors:
                unknown = [p for p in processors if p not in known_processors]
                if unknown:
                    diagnostics.append(SetupDiagnostic(
                        severity="error",
                        code="xref.processing.unknown-processor",
                        message=f"processing.processors contains unknown plugin ID(s): {unknown}.",
                        path=("processing", "processors"),
                    ))
    
    # microscopeStand ⇄ rs232devices
    ms = data.get("microscopeStand")
    if ms:
        port = ms.get("rs232device")
        if port and port not in rs232s:
            diagnostics.append(SetupDiagnostic(
                severity="error",
                code="xref.microscopestand.rs232-undefined",
                message=f"microscopeStand.rs232device='{port}' is not a defined RS232 connection.",
                path=("microscopeStand", "rs232device"),
            ))
    
    # availableWidgets ↔ matching sections
    if context and context.widget_requires_section:
        for widget, section_key in context.widget_requires_section.items():
            if widget in widgets and not data.get(section_key):
                diagnostics.append(SetupDiagnostic(
                    severity="warning",
                    code="widget.missing-section",
                    message=f"Widget '{widget}' is enabled but no '{section_key}' section is configured.",
                    path=("availableWidgets",),
                    fix=SetupFix("configure_section", section_key),
                ))
    
    # Legacy / dormant section notes
    if data.get("pulseStreamer") and (data["pulseStreamer"] or {}).get("ipAddress"):
        diagnostics.append(SetupDiagnostic(
            severity="note",
            code="legacy.pulsestreamer",
            message="pulseStreamer is configured but MasterController no longer constructs PulseStreamerManager. Use teensyPulse instead.",
            path=("pulseStreamer",),
        ))
    
    if data.get("slm"):
        diagnostics.append(SetupDiagnostic(
            severity="note",
            code="legacy.slm-singular",
            message="slm (singular) is deprecated — prefer slms (plural) for new setups.",
            path=("slm",),
        ))
    
    return diagnostics
