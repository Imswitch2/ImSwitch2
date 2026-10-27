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


# ── processing: built-in, drop-in and unknown plugin ids ───────────────────
def _processing_codes(processing, **context):
    registry = build_default_registry(discover=False)
    report = validate_setup_data(
        {"processing": processing}, registry, context=ValidationContext(**context)
    )
    return {d.code: d for d in report.diagnostics if d.code.startswith("xref.processing.")}


_BUILTIN = dict(known_reconstructor_ids=("view-only", "monalisa"), known_processor_ids=("resize",))


def test_processing_builtin_ids_are_fine():
    assert _processing_codes(
        {"reconstructors": ["monalisa"], "processors": ["resize"]}, **_BUILTIN
    ) == {}


def test_processing_dropin_ids_are_a_portability_warning():
    found = _processing_codes(
        {"reconstructors": ["view-only", "user.recon"], "processors": ["resize", "user.proc"]},
        dropin_reconstructor_ids=("user.recon",), dropin_processor_ids=("user.proc",), **_BUILTIN,
    )
    assert set(found) == {"xref.processing.dropin-reconstructor", "xref.processing.dropin-processor"}
    for diag in found.values():
        assert diag.severity == "warning"
        assert "drop-in plugin on this computer" in diag.message
        assert "computers without the file" in diag.message
    assert "user.recon" in found["xref.processing.dropin-reconstructor"].message
    assert found["xref.processing.dropin-processor"].path == ("processing", "processors")


def test_processing_unknown_ids_are_an_error_that_says_what_happens():
    found = _processing_codes(
        {"reconstructors": ["view-only", "nope"], "processors": ["resize", "nope2"]}, **_BUILTIN,
        dropin_reconstructor_ids=("user.recon",),
    )
    assert set(found) == {"xref.processing.unknown-reconstructor", "xref.processing.unknown-processor"}
    for diag in found.values():
        assert diag.severity == "error"
        assert diag.message.endswith("ImProcess skips unknown ids at startup.")
    assert "'nope'" in found["xref.processing.unknown-reconstructor"].message


def test_processing_mixed_ids_report_each_kind_once():
    found = _processing_codes(
        {"reconstructors": ["view-only", "user.recon", "nope"]},
        dropin_reconstructor_ids=("user.recon",), **_BUILTIN,
    )
    assert set(found) == {"xref.processing.dropin-reconstructor", "xref.processing.unknown-reconstructor"}
    assert "nope" not in found["xref.processing.dropin-reconstructor"].message
    assert "user.recon" not in found["xref.processing.unknown-reconstructor"].message


def test_a_builtin_id_wins_over_a_dropin_of_the_same_id():
    assert _processing_codes(
        {"reconstructors": ["monalisa"]}, dropin_reconstructor_ids=("monalisa",), **_BUILTIN
    ) == {}


def test_without_a_context_the_builtin_tables_decide():
    from imswitch.improcess.processors import available_processor_specs
    from imswitch.improcess.reconstructors import builtin_reconstructor_ids

    builtin_p = available_processor_specs(origin="builtin")[0][0]
    found = _processing_codes({
        "reconstructors": [builtin_reconstructor_ids()[0], "nope"],
        "processors": [builtin_p],
    })
    assert set(found) == {"xref.processing.unknown-reconstructor"}


def test_a_dropin_loaded_in_this_process_is_not_builtin(monkeypatch):
    from imswitch.improcess.reconstructors import _USER_RECONSTRUCTOR_CLASSES, _all_reconstructor_classes

    monkeypatch.setitem(_USER_RECONSTRUCTOR_CLASSES, "user.loaded", _all_reconstructor_classes()["view-only"])
    found = _processing_codes({"reconstructors": ["user.loaded"]})
    assert set(found) == {"xref.processing.unknown-reconstructor"}, "only the built-in table counts"


def test_nothing_known_and_the_import_failing_means_no_id_diagnostics(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "imswitch.improcess.reconstructors", None)
    monkeypatch.setitem(sys.modules, "imswitch.improcess.processors", None)
    assert _processing_codes({"reconstructors": ["whatever"], "processors": ["anything"]}) == {}


def test_nothing_known_still_reports_a_listed_dropin(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "imswitch.improcess.reconstructors", None)
    monkeypatch.setitem(sys.modules, "imswitch.improcess.processors", None)
    found = _processing_codes(
        {"reconstructors": ["user.recon", "other"]}, dropin_reconstructor_ids=("user.recon",),
    )
    assert set(found) == {"xref.processing.dropin-reconstructor"}


def test_the_shape_checks_stay():
    found = _processing_codes({"reconstructors": [], "processors": "resize"}, **_BUILTIN)
    assert set(found) == {"xref.processing.reconstructors-invalid", "xref.processing.processors-invalid"}
