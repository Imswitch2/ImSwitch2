# Phase 4 Implementation - COMPLETE ✅

## Summary

Phase 4 of the shared validation service has been successfully implemented. The validation logic is now unified in the model layer with a single diagnostic type (`SetupDiagnostic`), consumed by the CLI, editor, and tests.

## Key Changes

### 1. Model Layer (`imswitch/imcontrol/model/plugins/validation.py`)

**New Classes:**
- `SetupFix` - Represents fixable issues with action and target
- `SetupDiagnostic` - THE single diagnostic type (replaces `DeviceValidationResult`)
  - Fields: `severity`, `code`, `message`, `path`, `fix`
  - Severities: "error", "warning", "note"
  - Plain-text messages (no HTML)
- `ValidationContext` - Optional editor-supplied inputs
  - `widget_requires_section`
  - `known_reconstructor_ids`
  - `known_processor_ids`

**Updated Classes:**
- `ValidationReport` - Now has `diagnostics` list instead of `devices`
  - `has_errors` property checks for error-severity diagnostics
  - `format()` method groups by severity

**Core Functions:**
- `validate_setup_data(data, registry, *, context=None, source_path=None)` - Dict-based validation
- `validate_setup_file(path, registry, *, context=None)` - File wrapper

**Validation Checks Implemented:**
1. **Manager Resolution** (from existing code)
   - `manager.unresolved` - Manager cannot be resolved
   - `manager.legacy-fallback` - Resolved via legacy import
   - `manager.schema` - JSON schema validation warnings

2. **DAQ Conflicts** (ported from editor)
   - `daq.conflict` - Channel used by multiple devices

3. **Cross-References** (ported from editor)
   - `xref.focuslock.*` - focusLock section and camera/positioner references
   - `xref.autofocus.*` - autofocus camera/positioner references
   - `xref.tiling.*` - tiling camera/positioner references
   - `xref.scan.*` - scan section and forScanning positioner
   - `xref.etsted.*` - etSTED requires scan
   - `xref.processing.*` - reconstructor/processor ID validation
   - `xref.microscopestand.*` - stand RS232 device reference
   - `widget.missing-section` - availableWidgets requires section (with fix metadata)

4. **Legacy Checks** (ported from editor)
   - `legacy.slm-singular` - Singular "slm" should be "slms"
   - `legacy.pulsestreamer` - pulseStreamer note

### 2. CLI (`imswitch/imcontrol/model/plugins/__main__.py`)

**Updated:**
- `cmd_validate_setup()` - Uses new `ValidationReport.diagnostics`
- Enhanced output formatting with severity grouping
- Exit code behavior preserved: 1 for errors, 0 otherwise
- Now surfaces DAQ and cross-reference diagnostics

### 3. Editor (`utility_scripts/imswitch_config_editor.py`)

**Updated:**
- `ValidationPanel.validate()` - Calls `validate_setup_data()` from model
- Builds `ValidationContext` with editor's widget requirements and improcess IDs
- Renders diagnostics to HTML with severity grouping
- Builds "Configure..." links from `fix` metadata
- Graceful fallback if model import fails

**Removed:**
- Duplicate validation logic (now in model)

### 4. Tests

**Migrated:**
- `imswitch/imcontrol/_test/unit/test_device_plugin_diagnostics.py`
  - Updated to use `report.diagnostics` instead of `report.devices`
  - Tests manager resolution, legacy fallback, unresolved managers
  - All tests passing

**Created:**
- `imswitch/imcontrol/_test/unit/test_setup_validation_xref.py`
  - 11 comprehensive test cases covering:
    - DAQ conflict detection
    - focusLock cross-references (missing section, undefined camera)
    - Tiling cross-references (undefined zPositioner)
    - Autofocus cross-references (undefined positioner)
    - Scan cross-references (no scanning positioner)
    - MicroscopeStand RS232 references
    - Widget section requirements (with/without context)
    - Legacy checks (singular slm)
    - Clean config validation
  - All tests passing

## Verification Results

### ✅ Core Requirements Met

1. **Single diagnostic type**: `DeviceValidationResult` removed, only `SetupDiagnostic` exists
2. **Plain-text messages**: All diagnostic messages are plain text (no HTML)
3. **Structured data**: Diagnostics return data (severity, code, path, fix), not HTML
4. **CLI preserved**: Exit codes and behavior maintained
5. **Editor integration**: Renders diagnostics from model, builds links from fix metadata
6. **Graceful degradation**: Works without context, without jsonschema, without improcess
7. **No Qt in model**: Model layer has no Qt dependencies
8. **All checks preserved**: Every existing validation check maintained with same severity

### ✅ Test Results

```
Cross-reference validation tests: 11/11 passed
Device plugin diagnostic tests: All passed
Integration tests: All passed
CLI tests: Exit code 0 for clean, 1 for errors
```

### ✅ Verification Commands

```bash
# DeviceValidationResult no longer exists
grep -rn "class DeviceValidationResult" imswitch
# Returns nothing (exit code 1)

# Editor syntax valid
python -c "import ast; ast.parse(open('utility_scripts/imswitch_config_editor.py').read()); print('OK')"
# Prints: OK

# CLI works
python -m imswitch.imcontrol.model.plugins validate-setup <setup.json>
# Returns 0 for clean, 1 for errors
```

## Files Modified

1. `imswitch/imcontrol/model/plugins/validation.py` - Core validation service
2. `imswitch/imcontrol/model/plugins/__main__.py` - CLI integration
3. `utility_scripts/imswitch_config_editor.py` - Editor integration
4. `imswitch/imcontrol/_test/unit/test_device_plugin_diagnostics.py` - Migrated tests
5. `imswitch/imcontrol/_test/unit/test_setup_validation_xref.py` - New tests (created)

## Usage Examples

### From Model Layer
```python
from imswitch.imcontrol.model.plugins.validation import (
    validate_setup_data, ValidationContext
)
from imswitch.imcontrol.model.plugins.registry import build_default_registry

registry = build_default_registry()
data = {...}  # Setup JSON as dict

# Basic validation
report = validate_setup_data(data, registry)

# With editor context
context = ValidationContext(
    widget_requires_section={"FocusLock": "focusLock"},
    known_reconstructor_ids=("beadrec",),
)
report = validate_setup_data(data, registry, context=context)

# Check results
if report.has_errors:
    for diag in report.diagnostics:
        if diag.severity == "error":
            print(f"{diag.code}: {diag.message} at {diag.path}")
```

### From CLI
```bash
python -m imswitch.imcontrol.model.plugins validate-setup config.json
```

Output:
```
Validation report for: config.json

❌ ERRORS:
  [manager.unresolved] detectors.Camera1.managerName
    Manager 'BadManager' cannot be resolved for kind 'detector'.
  [daq.conflict] <daq>.Dev1/AO0
    DAQ channel 'Dev1/AO0' used by multiple devices: Laser1, Laser2

⚠️ WARNINGS:
  [widget.missing-section] 
    Widget 'FocusLock' requires section 'focusLock' which is not configured.

ℹ️ NOTES:
  [legacy.slm-singular] slm
    Use 'slms' (plural) instead of 'slm' (singular).

❌ Validation FAILED.
```

### From Editor
The `ValidationPanel.validate()` method now:
1. Calls `validate_setup_data()` with editor context
2. Renders diagnostics to HTML with color-coded severity
3. Builds `fixsection:` links from `fix.action == "configure_section"`

## Design Compliance

✅ **Phase 4 scope only** - No module split (that's Phase 5)
✅ **Single diagnostic type** - `SetupDiagnostic` is THE only diagnostic dataclass
✅ **Plain-text messages** - HTML/markup generation is editor's responsibility
✅ **Preserved checks** - Every existing validation check maintained
✅ **Graceful degradation** - Works without optional dependencies
✅ **No Qt in model** - Model layer is pure Python
✅ **Exit codes preserved** - CLI maintains backward compatibility
✅ **Structured fixes** - Fix metadata as data, not embedded in messages

## Next Steps (Phase 5)

Phase 4 is complete. Phase 5 (if needed) would involve:
- Module split into separate validation submodules
- Additional validation rules
- Enhanced diagnostic codes

## Contact

Implementation completed per `openhands_tasks/config_editor/PHASE4_shared_validation.md` specification.
