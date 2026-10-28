"""
Test config editor widget→section helper functionality.
"""
import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


def test_widget_section_map_built_from_schemas():
    """
    Widget→section map must be derived from SECTION_SCHEMAS and contain all
    pairings including MotCorr→microscopeStand.
    """
    # Expected pairings from the task spec
    expected_pairings = {
        "FocusLock": "focusLock",
        "Autofocus": "autofocus",
        "Tiling": "tiling",
        "EtSTED": "etSTED",
        "Scan": "scan",
        "MotCorr": "microscopeStand",
    }
    
    # All expected pairings must be in the map
    for widget, section_key in expected_pairings.items():
        assert widget in editor._WIDGET_REQUIRES_SECTION, \
            f"Widget '{widget}' missing from _WIDGET_REQUIRES_SECTION"
        assert editor._WIDGET_REQUIRES_SECTION[widget] == section_key, \
            f"Widget '{widget}' maps to wrong section: " \
            f"expected '{section_key}', got '{editor._WIDGET_REQUIRES_SECTION[widget]}'"


def test_widget_section_map_matches_schemas():
    """
    Every section schema that declares requires_widget must appear in the map,
    and vice versa.
    """
    # Build the expected map from schemas
    expected_from_schemas = {}
    for section_key, schema in editor.SECTION_SCHEMAS.items():
        widget = schema.get("requires_widget")
        if widget:
            expected_from_schemas[widget] = section_key
    
    # The map should exactly match what's in the schemas
    assert editor._WIDGET_REQUIRES_SECTION == expected_from_schemas, \
        "Widget→section map doesn't match section schemas"


def test_xref_issues_emits_warning_when_widget_enabled_without_section():
    """
    _collect_xref_issues must emit a warning when a requiring widget is enabled
    but the matching section is not configured.
    """
    # Config with FocusLock widget enabled but no focusLock section
    data = {
        "availableWidgets": ["FocusLock"],
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Should contain a warning about FocusLock
    warnings = [msg for severity, msg in issues if severity == "warning"]
    focuslock_warnings = [w for w in warnings if "FocusLock" in w and "focusLock" in w]
    assert len(focuslock_warnings) > 0, \
        "Expected warning about FocusLock widget without focusLock section"


def test_xref_issues_no_warning_when_section_present():
    """
    _collect_xref_issues must NOT emit a warning when a requiring widget is
    enabled and the matching section IS configured.
    """
    # Config with FocusLock widget enabled AND focusLock section configured
    data = {
        "availableWidgets": ["FocusLock"],
        "focusLock": {
            "positioner": "SomePositioner",
            "updateFreq": 10
        },
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Should NOT contain a warning about FocusLock
    warnings = [msg for severity, msg in issues if severity == "warning"]
    focuslock_warnings = [w for w in warnings if "FocusLock" in w and "focusLock" in w]
    assert len(focuslock_warnings) == 0, \
        "Unexpected warning about FocusLock when focusLock section is configured"


def test_build_default_section_for_requiring_sections():
    """
    _build_default_section must return a dict containing the section's required
    field keys for each section that requires a widget.
    """
    # Test all sections that require a widget
    for widget, section_key in editor._WIDGET_REQUIRES_SECTION.items():
        schema = editor.SECTION_SCHEMAS.get(section_key)
        assert schema is not None, f"Schema missing for section '{section_key}'"
        
        defaults = editor._build_default_section(schema)
        
        assert isinstance(defaults, dict), \
            f"_build_default_section({section_key}) must return a dict"
        
        # Check that required fields are present
        required_fields = schema.get("required", [])
        for field in required_fields:
            assert field in defaults, \
                f"Required field '{field}' missing from defaults for section '{section_key}'"


def test_microscope_stand_section_uses_loadable_manager_name():
    """The config editor must not suggest the LeicaStand controller as a manager.

    ``managerName`` has no code default, so the section starts with it empty
    and the operator names it; the one name the tip offers as an example is a
    manager the loader can build.
    """
    schema = editor.SECTION_SCHEMAS.get("microscopeStand")
    assert schema is not None

    defaults = editor._build_default_section(schema)
    assert defaults["managerName"] == ""

    field = next(f for f in schema["fields"] if f["key"] == "managerName")
    assert field["req"]
    example = "LeicaDMIStandMockManager"
    assert example in field["tip"]
    assert example in editor.CAT_MANAGERS.get("stands", [])


def test_processing_section_exposes_monalisa_reconstructor():
    """The config editor exposes the public MoNaLISA plugin id."""
    reconstructors = editor._section_option_values("processing", "reconstructors")

    assert "monalisa" in reconstructors


def test_config_editor_defaults_to_imswitch_user_setup_folder():
    """The editor should use the same user config root as ImSwitch itself."""
    root = editor._imswitch_user_root()

    assert root.name == "ImSwitchConfig"
    assert editor._default_setup_dir() == root / "imcontrol_setups"
    assert editor._default_config_dir() == root / "config"


def test_processing_section_exposes_minimal_gui_flags():
    """The config editor exposes ImProcess layout-minimal mode toggles."""
    schema = editor.SECTION_SCHEMAS.get("processing")
    assert schema is not None

    fields = {field["key"]: field for field in schema.get("fields", [])}
    for key in (
        "parameterPanel",
        "napariLayerControls",
        "reconstructionPanel",
        "actionsPanel",
        "fileWatcherPanel",
        "multiDataPanel",
        "currentDataPanel",
        "resultsPanel",
    ):
        assert fields[key]["type"] == "bool"
        assert fields[key]["default"] is True
        assert fields[key]["grp"] == "Startup layout"

    defaults = editor._build_default_section(schema)
    assert defaults["parameterPanel"] is True
    assert defaults["napariLayerControls"] is True
    assert defaults["reconstructionPanel"] is True
    assert defaults["actionsPanel"] is True
    assert defaults["fileWatcherPanel"] is True
    assert defaults["multiDataPanel"] is True
    assert defaults["currentDataPanel"] is True
    assert defaults["resultsPanel"] is True


def test_validation_warning_includes_clickable_link():
    """
    Widget→section validation warnings must include a clickable 'Configure…' link.
    """
    # Config with Tiling widget enabled but no tiling section
    data = {
        "availableWidgets": ["Tiling"],
        "detectors": {},
        "lasers": {},
    }
    
    issues = editor._collect_xref_issues(data)
    
    # Find the Tiling warning
    tiling_warnings = [
        msg for severity, msg in issues
        if severity == "warning" and "Tiling" in msg and "tiling" in msg
    ]
    assert len(tiling_warnings) > 0, "Expected warning about Tiling widget"
    
    # Check that the warning includes a clickable link
    warning_msg = tiling_warnings[0]
    assert "fixsection:tiling" in warning_msg, \
        "Warning should include 'fixsection:tiling' link"
    assert "<a href=" in warning_msg, \
        "Warning should include HTML anchor tag"


def test_applying_the_scan_form_keeps_what_it_does_not_show_and_takes_an_expert_scan(qtbot,
                                                                                    monkeypatch):
    """Applying a section's form replaced the whole section with the form's
    fields: scan.engine was dropped, and the setup then failed to load or
    ran on the NI-DAQ (review 2026-10-16). The form's fields now go over the
    section as it was; and the designers are not required, so an Expert
    scan (which leaves them null) can be applied."""
    import json
    from qtpy.QtWidgets import QDialog
    expert = json.load(open(
        'imswitch/_data/user_defaults/imcontrol_setups/expert_step_boards_mock_setup.json'))
    assert expert['scan']['engine'] == 'triggerscope'
    panel = editor.PropertyEditor()
    qtbot.addWidget(panel)
    panel._data = expert
    panel._refresh_sections = lambda: None
    applied = []

    def accept(dialog):
        dialog._try_apply()                                  # the form, unchanged
        applied.append(dialog.result() == QDialog.Accepted)
        return dialog.result()
    monkeypatch.setattr(editor.SectionEditorDialog, 'exec_', accept)
    panel._open_section_schema('scan')
    assert applied == [True]                                 # no "Missing required"
    assert panel._data['scan']['engine'] == 'triggerscope'   # kept
    assert panel._data['scan']['scanDesigner'] is None
    assert 'engine' in {f['key'] for f in editor.SECTION_SCHEMAS['scan']['fields']}


def test_scan_section_uses_nested_cloak_forms_instead_of_raw_json():
    schema = editor.SECTION_SCHEMAS["scan"]

    assert "simplePointScan" in schema.get("nested", {})
    assert "simpleMoNaLISA" in schema.get("nested", {})
    assert all(
        field["key"] not in {"simplePointScan", "simpleMoNaLISA"}
        for field in schema.get("fields", [])
    )

    point_keys = {field["key"] for field in schema["nested"]["simplePointScan"]}
    assert {"objectiveNA", "overviewFieldUm", "overviewFrameTimeS"} <= point_keys

    monalisa_keys = {field["key"] for field in schema["nested"]["simpleMoNaLISA"]}
    assert {
        "fovSource",
        "fovUm",
        "ulensPeriodPolicy",
        "ulensPeriodUm",
        "defaultCameraReadoutTimeMs",
        "interPulseDelayMs",
        "readoutPulseTimeMs",
    } <= monalisa_keys
    assert "profiles" not in monalisa_keys


def _minimal_nested_section_schema():
    return {
        "display": "Nested section",
        "fields": [
            {"key": "backend", "label": "Backend", "type": "text", "default": "Advanced", "req": False, "grp": "Basic"},
        ],
        "nested": {
            "simple": [
                {"key": "period", "label": "Period", "type": "float", "default": 0.72, "req": False, "grp": "Simple"},
            ],
        },
    }


def test_section_nested_apply_preserves_unknown_children_and_section_keys(qapp):
    from PyQt5.QtTest import QTest

    current = {
        "backend": "Advanced",
        "futureTopLevel": {"keep": True},
        "simple": {
            "period": 0.72,
            "profiles": {"green": {"label": "Green"}},
            "futureNested": 123,
        },
    }
    dlg = editor.SectionEditorDialog(
        "scan", _minimal_nested_section_schema(), current, {}, None
    )
    fw = dlg._field_widgets[("nested:simple", "period")]
    fw._w.selectAll()
    QTest.keyClicks(fw._w, "0.85")
    assert fw.is_touched()

    dlg._try_apply()

    assert dlg.result_data["simple"]["period"] == pytest.approx(0.85)
    assert dlg.result_data["simple"]["profiles"] == current["simple"]["profiles"]
    assert dlg.result_data["simple"]["futureNested"] == 123
    assert dlg.result_data["futureTopLevel"] == {"keep": True}


def test_section_nested_container_is_not_created_when_untouched(qapp):
    current = {"backend": "Advanced"}
    dlg = editor.SectionEditorDialog(
        "scan", _minimal_nested_section_schema(), current, {}, None
    )

    dlg._try_apply()

    assert "simple" not in dlg.result_data


def test_touching_nested_field_materializes_only_that_field(qapp):
    from PyQt5.QtTest import QTest

    current = {"backend": "Advanced", "simple": None}
    dlg = editor.SectionEditorDialog(
        "scan", _minimal_nested_section_schema(), current, {}, None
    )
    fw = dlg._field_widgets[("nested:simple", "period")]
    fw._w.selectAll()
    QTest.keyClicks(fw._w, "0.9")

    dlg._try_apply()

    assert dlg.result_data["simple"] == {"period": pytest.approx(0.9)}


def _tabTitles(dlg, *, visible=True):
    tabs = dlg._tab_widget
    return {tabs.tabText(i) for i in range(tabs.count()) if tabs.isTabVisible(i) == visible}


def _scanDialog(current):
    return editor.SectionEditorDialog(
        "scan", editor.SECTION_SCHEMAS["scan"], current, {}, None
    )


@pytest.mark.parametrize("cloak, shown, hidden", [
    (None, set(), {"SimplePointScan", "SimpleMoNaLISA"}),
    ("SimplePointScan", {"SimplePointScan"}, {"SimpleMoNaLISA"}),
    ("SimpleMoNaLISA", {"SimpleMoNaLISA"}, {"SimplePointScan"}),
])
def test_a_cloaks_options_tab_shows_only_while_that_cloak_is_selected(qapp, cloak, shown, hidden):
    dlg = _scanDialog({"scanWidgetType": "Advanced", "cloak": cloak})

    assert shown <= _tabTitles(dlg)
    assert hidden <= _tabTitles(dlg, visible=False)
    assert "Basic" in _tabTitles(dlg)


def test_the_legacy_cloak_spelling_shows_its_options_tab(qapp):
    dlg = _scanDialog({"scanWidgetType": "SimplePointScan"})

    assert "SimplePointScan" in _tabTitles(dlg)
    assert "SimpleMoNaLISA" in _tabTitles(dlg, visible=False)


def test_choosing_a_cloak_in_the_form_shows_its_tab_and_hiding_keeps_its_options(qapp):
    current = {
        "scanWidgetType": "Advanced",
        "cloak": "SimplePointScan",
        # What the loader requires: the form seeds these empty, as missing.
        "scanDesignerParams": {}, "TTLCycleDesignerParams": {}, "sampleRate": 100000,
        "simplePointScan": {"objectiveNA": 1.4, "futureKey": 1},
    }
    dlg = _scanDialog(current)
    combo = dlg._field_widgets[("field", "cloak")]._w

    combo.setCurrentIndex(combo.findData("SimpleMoNaLISA"))
    combo.activated.emit(combo.currentIndex())     # what a user's pick sends

    assert "SimpleMoNaLISA" in _tabTitles(dlg)
    assert "SimplePointScan" in _tabTitles(dlg, visible=False)
    dlg._try_apply()
    assert dlg.result_data["cloak"] == "SimpleMoNaLISA"
    assert dlg.result_data["simplePointScan"] == current["simplePointScan"]
    assert "simpleMoNaLISA" not in dlg.result_data
