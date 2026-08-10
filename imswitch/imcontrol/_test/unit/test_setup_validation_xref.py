"""Tests for cross-reference validation in setup configurations."""

from imswitch.imcontrol.model.plugins.registry import build_default_registry
from imswitch.imcontrol.model.plugins.validation import (
    validate_setup_data,
    ValidationContext,
)


def test_daq_conflict():
    """Test that DAQ channel conflicts are detected."""
    registry = build_default_registry(discover=False)
    
    data = {
        "lasers": {
            "Laser1": {
                "managerName": "NidaqLaserManager",
                "analogChannel": "Dev1/AO0",
                "managerProperties": {},
            },
            "Laser2": {
                "managerName": "NidaqLaserManager",
                "analogChannel": "Dev1/AO0",  # Conflict!
                "managerProperties": {},
            },
        }
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have a DAQ conflict error
    conflicts = [d for d in report.diagnostics if d.code == "daq.conflict"]
    assert len(conflicts) == 1
    assert "Dev1/AO0" in conflicts[0].message
    assert "Laser1" in conflicts[0].message
    assert "Laser2" in conflicts[0].message
    assert conflicts[0].severity == "error"
    assert report.has_errors is True


def test_focuslock_missing_section():
    """Test focusLock detector flagged but no focusLock section."""
    registry = build_default_registry(discover=False)
    
    data = {
        "detectors": {
            "Camera1": {
                "managerName": "AVManager",
                "forFocusLock": True,
                "managerProperties": {},
            },
        }
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have focuslock.missing-section error
    errors = [d for d in report.diagnostics if d.code == "xref.focuslock.missing-section"]
    assert len(errors) == 1
    assert "Camera1" in errors[0].message
    assert errors[0].severity == "error"
    assert report.has_errors is True


def test_focuslock_camera_undefined():
    """Test focusLock.camera references undefined detector."""
    registry = build_default_registry(discover=False)
    
    data = {
        "detectors": {
            "Camera1": {
                "managerName": "AVManager",
                "forFocusLock": True,
                "managerProperties": {},
            },
        },
        "focusLock": {
            "camera": "UndefinedCamera",
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have camera-undefined error
    errors = [d for d in report.diagnostics if d.code == "xref.focuslock.camera-undefined"]
    assert len(errors) == 1
    assert "UndefinedCamera" in errors[0].message
    assert errors[0].path == ("focusLock", "camera")
    assert errors[0].severity == "error"
    assert report.has_errors is True


def test_tiling_zpositioner_undefined():
    """Test tiling.zPositioner references undefined positioner."""
    registry = build_default_registry(discover=False)
    
    data = {
        "tiling": {
            "zPositioner": "UndefinedPositioner",
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have zpositioner-undefined error
    errors = [d for d in report.diagnostics if d.code == "xref.tiling.zpositioner-undefined"]
    assert len(errors) == 1
    assert "UndefinedPositioner" in errors[0].message
    assert errors[0].path == ("tiling", "zPositioner")
    assert errors[0].severity == "error"
    assert report.has_errors is True


def test_widget_missing_section_with_context():
    """Test availableWidgets check with context detects missing section."""
    registry = build_default_registry(discover=False)
    
    data = {
        "availableWidgets": ["FocusLock"],
    }
    
    context = ValidationContext(
        widget_requires_section={"FocusLock": "focusLock"}
    )
    
    report = validate_setup_data(data, registry, context=context)
    
    # Should have widget.missing-section warning with fix
    warnings = [d for d in report.diagnostics if d.code == "widget.missing-section"]
    assert len(warnings) == 1
    assert "FocusLock" in warnings[0].message
    assert "focusLock" in warnings[0].message
    assert warnings[0].severity == "warning"
    assert warnings[0].fix is not None
    assert warnings[0].fix.action == "configure_section"
    assert warnings[0].fix.target == "focusLock"


def test_widget_missing_section_without_context():
    """Test availableWidgets check without context gracefully skips."""
    registry = build_default_registry(discover=False)
    
    data = {
        "availableWidgets": ["FocusLock"],
    }
    
    # No context provided
    report = validate_setup_data(data, registry, context=None)
    
    # Should NOT have widget.missing-section warning (gracefully skipped)
    warnings = [d for d in report.diagnostics if d.code == "widget.missing-section"]
    assert len(warnings) == 0


def test_legacy_slm_singular():
    """Test legacy singular slm section emits note."""
    registry = build_default_registry(discover=False)
    
    data = {
        "slm": {
            "SLM1": {
                "managerName": "SLMManager",
                "managerProperties": {},
            },
        }
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have legacy.slm-singular note
    notes = [d for d in report.diagnostics if d.code == "legacy.slm-singular"]
    assert len(notes) == 1
    assert "slms" in notes[0].message
    assert notes[0].severity == "note"
    assert report.has_errors is False


def test_clean_config():
    """Test a clean config with no issues."""
    registry = build_default_registry(discover=False)
    
    data = {
        "detectors": {
            "Camera1": {
                "managerName": "AVManager",
                "managerProperties": {},
            },
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have no error-severity diagnostics
    errors = [d for d in report.diagnostics if d.severity == "error"]
    assert len(errors) == 0, f"Expected no errors, got: {[e.message for e in errors]}"
    assert report.has_errors is False


def test_autofocus_positioner_undefined():
    """Test autofocus.positioner references undefined positioner."""
    registry = build_default_registry(discover=False)
    
    data = {
        "autofocus": {
            "positioner": "UndefinedPositioner",
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have autofocus.positioner-undefined error
    errors = [d for d in report.diagnostics if d.code == "xref.autofocus.positioner-undefined"]
    assert len(errors) == 1
    assert "UndefinedPositioner" in errors[0].message
    assert errors[0].path == ("autofocus", "positioner")
    assert errors[0].severity == "error"


def test_scan_no_scanning_positioner():
    """Test scan section without forScanning positioner."""
    registry = build_default_registry(discover=False)
    
    data = {
        "scan": {
            "scanMode": "2D",
        },
        "positioners": {
            "Stage1": {
                "managerName": "NidaqStageManager",
                "managerProperties": {},
            },
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have scan.no-scanning-positioner warning
    warnings = [d for d in report.diagnostics if d.code == "xref.scan.no-scanning-positioner"]
    assert len(warnings) == 1
    assert "forScanning" in warnings[0].message
    assert warnings[0].severity == "warning"


def test_microscopestand_rs232_undefined():
    """Test microscopeStand.rs232device references undefined RS232."""
    registry = build_default_registry(discover=False)
    
    data = {
        "microscopeStand": {
            "managerName": "LeicaDMIManager",
            "rs232device": "UndefinedRS232",
            "managerProperties": {},
        },
    }
    
    report = validate_setup_data(data, registry)
    
    # Should have microscopestand.rs232-undefined error
    errors = [d for d in report.diagnostics if d.code == "xref.microscopestand.rs232-undefined"]
    assert len(errors) == 1
    assert "UndefinedRS232" in errors[0].message
    assert errors[0].path == ("microscopeStand", "rs232device")
    assert errors[0].severity == "error"


def _shared_actuator_setup(focusActuator=None, scanActuator=None):
    """The example_sted.json shape: one piezo reachable under two names."""
    scanZ = {
        "managerName": "NidaqPositionerManager",
        "axes": ["Z"],
        "forScanning": True,
        "managerProperties": {},
    }
    focusZ = {
        "managerName": "PiezoconceptZManager",
        "axes": ["Z"],
        "forPositioning": True,
        "managerProperties": {},
    }
    if scanActuator is not None:
        scanZ["physicalActuator"] = scanActuator
    if focusActuator is not None:
        focusZ["physicalActuator"] = focusActuator

    return {
        "detectors": {
            "FocusCam": {
                "managerName": "AVManager",
                "forFocusLock": True,
                "managerProperties": {},
            },
        },
        "positioners": {"ND-PiezoZ": scanZ, "PiezoZ": focusZ},
        "focusLock": {"camera": "FocusCam", "positioner": "PiezoZ"},
    }


def _shared_actuator_warnings(data):
    registry = build_default_registry(discover=False)
    report = validate_setup_data(data, registry)
    return [
        d for d in report.diagnostics
        if d.code == "xref.focuslock.shared-actuator"
    ]


def test_focuslock_shares_axis_with_a_scanned_positioner():
    """Undeclared: the focus lock will pause for those scans, so say so."""
    warnings = _shared_actuator_warnings(_shared_actuator_setup())

    assert len(warnings) == 1
    assert "ND-PiezoZ" in warnings[0].message
    assert warnings[0].severity == "warning"
    assert warnings[0].path == ("focusLock", "positioner")


def test_declaring_physical_actuators_settles_the_question():
    """Either answer is explicit, so neither needs the warning."""
    assert _shared_actuator_warnings(
        _shared_actuator_setup(focusActuator="sample", scanActuator="sample")
    ) == []
    assert _shared_actuator_warnings(
        _shared_actuator_setup(focusActuator="sample", scanActuator="objective")
    ) == []


def test_declaring_only_one_side_is_still_ambiguous():
    assert len(_shared_actuator_warnings(
        _shared_actuator_setup(focusActuator="sample")
    )) == 1


def test_a_scan_that_cannot_reach_the_focus_axis_is_not_flagged():
    data = _shared_actuator_setup()
    data["positioners"]["ND-PiezoZ"]["axes"] = ["X"]

    assert _shared_actuator_warnings(data) == []


def test_a_non_scanning_positioner_sharing_the_axis_is_not_flagged():
    data = _shared_actuator_setup()
    data["positioners"]["ND-PiezoZ"]["forScanning"] = False

    assert _shared_actuator_warnings(data) == []
