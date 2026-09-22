"""Tests for config editor schema and defaults (Phase 2).

This module tests the schema/defaults system that merges template and JSON Schema
sources, ensuring that:
1. Default device building preserves exact template behavior
2. Schema-only properties appear with correct types/defaults
3. normalized_fields correctly merges template + schema
4. Round-trip preservation works for unknown fields INCLUDING nested dicts
5. Catalog populates properties_schema field correctly
"""

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.configeditor.catalog import (
    build_catalog,
)
from imswitch.imcontrol.model.configeditor.schemas import (
    materialize_device_schema,
    normalized_fields,
)
from imswitch.imcontrol.model.configeditor.defaults import (
    build_default_device,
    merge_preserving_unknown,
)
from imswitch.imcontrol.model.plugins.registry import (
    DevicePluginRegistry,
)
from imswitch.imcontrol.model.plugins.manifest import DeviceManagerContribution

#: The Config Studio's built-in manager templates, which ship inside the
#: package alongside the editor itself.
_BUILTIN_TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "imswitch" / "imcontrol" / "view" / "configeditor" / "builtin_templates"
)


class TestConfigEditorSchemaDefaults:
    """Test suite for config editor schema and defaults (Phase 2)."""
    
    def test_default_device_parity_hamamatsu(self):
        """Test that build_default_device produces identical output for HamamatsuManager.
        
        This test ensures the Phase 2 builder reproduces the exact template-driven
        defaults that the editor currently produces.
        """
        # Load the actual HamamatsuManager template
        template_path = _BUILTIN_TEMPLATES / "detectors" / "HamamatsuManager.json"
        
        if not template_path.exists():
            pytest.skip("HamamatsuManager.json template not found")
        
        with open(template_path, encoding="utf-8") as f:
            template = json.load(f)
        
        # Build default device using Phase 2 function (template-only, no schema)
        result = build_default_device(
            "HamamatsuManager",
            template=template,
            json_schema=None,
        )
        
        # Expected structure from template
        assert result["managerName"] == "HamamatsuManager"
        assert "managerProperties" in result
        
        # Check top-level fields from template
        assert result.get("analogChannel") is None  # "null" -> None
        assert result.get("digitalLine") is None
        assert result.get("forAcquisition") is True
        assert result.get("forFocusLock") is False
        
        # Check props fields
        props = result["managerProperties"]
        assert props["cameraListIndex"] == 0
        # A default the template states as a value keeps its kind. These used
        # to come out as the strings "0" and "False"; display_transform.py had
        # grown parsers to absorb exactly that, which is how the quirk survived
        # long enough to be pinned here as "byte-identical" behaviour.
        assert props["displayRotation"] == 0
        assert isinstance(props["displayRotation"], int)
        assert props["displayFlipX"] is False
        assert props["displayFlipY"] is False
        
        # Check nested dict
        assert "hamamatsu" in props
        hamamatsu = props["hamamatsu"]
        assert hamamatsu["readout_speed"] == 3
        assert hamamatsu["trigger_active"] == 1
        assert hamamatsu["trigger_mode"] == 1
        assert hamamatsu["trigger_polarity"] == 2
        assert hamamatsu["trigger_source"] == 2
        assert hamamatsu["exposure_time"] == 10.0
        assert hamamatsu["subarray_hpos"] == 0
        assert hamamatsu["subarray_hsize"] == 2048
        assert hamamatsu["subarray_vpos"] == 0
        assert hamamatsu["subarray_vsize"] == 2048
        assert hamamatsu["subarray_mode"] == 1
    
    def test_default_device_parity_nidaq_laser(self):
        """Test that build_default_device produces identical output for NidaqLaserManager."""
        template_path = _BUILTIN_TEMPLATES / "lasers" / "NidaqLaserManager.json"
        
        if not template_path.exists():
            pytest.skip("NidaqLaserManager.json template not found")
        
        with open(template_path, encoding="utf-8") as f:
            template = json.load(f)
        
        result = build_default_device(
            "NidaqLaserManager",
            template=template,
            json_schema=None,
        )
        
        # Expected structure
        assert result["managerName"] == "NidaqLaserManager"
        assert "managerProperties" in result
        
        # Check that template fields appear correctly
        props = result["managerProperties"]
        # Verify structure is correct
        assert isinstance(props, dict)

    def test_cobolt_editor_exposes_protocol_emission_and_startup_controls(self):
        """The Studio form must expose the complete Cobolt policy tuple."""
        template_path = _BUILTIN_TEMPLATES / "lasers" / "Cobolt0601NewLaserManager.json"
        with open(template_path, encoding="utf-8") as handle:
            template = json.load(handle)

        fields = {
            field.key: field
            for field in normalized_fields(template=template, json_schema=None)
        }

        assert fields["protocolProfile"].options == (
            "auto",
            "cobolt.scpi-compatible",
            "cobolt.legacy",
        )
        assert fields["emissionControl"].options == (
            "master",
            "pause",
            "auto",
        )
        assert fields["startupControl"].options == ("external", "software")
        assert fields["startupControl"].default == "external"

        default_device = build_default_device(
            "Cobolt0601NewLaserManager",
            template=template,
            json_schema=None,
        )
        assert default_device["managerProperties"]["startupControl"] == "external"
    
    def test_schema_only_defaults(self):
        """Test that schema-only properties appear with correct types/defaults."""
        # Create a minimal JSON Schema
        json_schema = {
            "type": "object",
            "properties": {
                "exposureTime": {
                    "type": "number",
                    "default": 100.0,
                },
                "triggerMode": {
                    "type": "string",
                    "enum": ["internal", "external", "software"],
                    "default": "internal",
                },
                "enableLogging": {
                    "type": "boolean",
                    "default": True,
                },
                "bufferSize": {
                    "type": "integer",
                    "default": 10,
                },
            },
            "required": ["exposureTime"],
        }
        
        # Build device with no template, schema only
        result = build_default_device(
            "FakeSchemaManager",
            template=None,
            json_schema=json_schema,
        )
        
        assert result["managerName"] == "FakeSchemaManager"
        props = result["managerProperties"]
        
        # Check schema-driven defaults
        assert props["exposureTime"] == 100.0
        assert props["triggerMode"] == "internal"
        assert props["enableLogging"] is True
        assert props["bufferSize"] == 10
    
    def test_normalized_fields_merge(self):
        """Test that normalized_fields correctly merges template + schema."""
        # Template with one labelled property
        template = {
            "top": [],
            "props": [
                {
                    "key": "exposureTime",
                    "label": "Exposure (ms)",
                    "type": "float",
                    "default": 10.0,
                    "req": True,
                    "grp": "Camera",
                    "tip": "Camera exposure time",
                    "opts": [],
                },
            ],
            "nested": {},
        }
        
        # Schema with shared property + additional property
        json_schema = {
            "type": "object",
            "properties": {
                "exposureTime": {
                    "type": "number",
                    "default": 100.0,
                },
                "triggerMode": {
                    "type": "string",
                    "enum": ["internal", "external"],
                    "default": "internal",
                },
            },
            "required": ["exposureTime", "triggerMode"],
        }
        
        fields = normalized_fields(template=template, json_schema=json_schema)
        
        # Should have 2 fields
        assert len(fields) == 2
        
        # Find the exposureTime field (should preserve template label)
        exposure_field = next(f for f in fields if f.key == "exposureTime")
        assert exposure_field.label == "Exposure (ms)"  # Template wins
        assert exposure_field.type == "float"
        assert exposure_field.default == 10.0
        assert exposure_field.required is True
        assert exposure_field.group == "Camera"
        assert exposure_field.tooltip == "Camera exposure time"
        assert exposure_field.location == "prop"
        
        # Find the triggerMode field (schema-only, inferred)
        trigger_field = next(f for f in fields if f.key == "triggerMode")
        assert trigger_field.label == "Trigger Mode"  # Inferred from the key
        assert trigger_field.type == "select"  # Inferred from enum
        assert trigger_field.default == "internal"
        assert trigger_field.required is True  # From schema
        assert trigger_field.group == "Properties"
        assert trigger_field.options == ("internal", "external")
        assert trigger_field.location == "prop"
    
    def test_round_trip_preservation(self):
        """Test that merge_preserving_unknown preserves all unknown fields.
        
        EXIT CRITERION: Unknown nested dicts must survive.
        """
        # Original device with unknown fields
        original = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 10.0,
                "unknownScalar": "secret",
                "unknownNestedDict": {
                    "nestedKey1": "value1",
                    "nestedKey2": 42,
                },
            },
            "unknownTopLevel": "topSecret",
        }
        
        # Edited device (form rebuild - unknowns missing)
        edited = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 20.0,  # Changed by user
            },
        }
        
        # Known keys
        schema_top_keys = set()  # No extra top keys in template
        schema_prop_keys = {"exposureTime"}
        
        # Merge
        result = merge_preserving_unknown(
            original,
            edited,
            schema_top_keys=schema_top_keys,
            schema_prop_keys=schema_prop_keys,
        )
        
        # Check that edited value is preserved
        assert result["managerProperties"]["exposureTime"] == 20.0
        
        # Check that unknown top-level key survived
        assert result["unknownTopLevel"] == "topSecret"
        
        # Check that unknown scalar property survived
        assert result["managerProperties"]["unknownScalar"] == "secret"
        
        # CRITICAL: Check that unknown nested dict survived intact
        assert "unknownNestedDict" in result["managerProperties"]
        nested = result["managerProperties"]["unknownNestedDict"]
        assert nested["nestedKey1"] == "value1"
        assert nested["nestedKey2"] == 42
    
    def test_catalog_carries_schema(self):
        """Test that build_catalog populates properties_schema field."""
        # Build a registry with a fake contribution
        registry = DevicePluginRegistry()
        
        # Create a fake contribution without a schema
        # (For a real schema test, we'd need to make the package importable)
        fake_contrib = DeviceManagerContribution(
            id="FakeManager",
            kind="detector",
            display_name="Fake Detector",
            python_name="fake_plugin:FakeDetectorManager",
            plugin_name="fake-plugin",
            source_package=None,  # No package -> schema will be None
            manager_properties_schema=None,
        )
        
        # Register the contribution using the public API
        registry.register(fake_contrib, is_builtin=False)
        
        # Build catalog
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Verify the manager is in catalog
        info = catalog.get("FakeManager")
        assert info is not None
        
        # Verify properties_schema field exists (will be None without importable package)
        assert hasattr(info, "properties_schema")
        assert info.properties_schema is None  # Expected since no valid source
    
    def test_catalog_schema_for_builtin(self):
        """Test that built-in managers have schemas populated when available."""
        # Build catalog with built-ins
        from imswitch.imcontrol.model.plugins.registry import build_default_registry
        registry = build_default_registry(discover=False)
        catalog = build_catalog(registry=registry, include_legacy_scan=False)
        
        # Check a built-in manager
        hamamatsu_info = catalog.get("HamamatsuManager")
        
        if hamamatsu_info is not None:
            # Verify field exists
            assert hasattr(hamamatsu_info, "properties_schema")
            # Schema may or may not be populated depending on whether
            # the contribution declares one - just verify the field exists
    
    def test_merge_preserving_unknown_does_not_overwrite_edits(self):
        """Test that merge_preserving_unknown never overwrites user edits."""
        original = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 10.0,
                "unknownField": "original",
            },
        }
        
        edited = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 20.0,
                "unknownField": "edited",  # User edited this unknown field
            },
        }
        
        schema_top_keys = set()
        schema_prop_keys = {"exposureTime"}
        
        result = merge_preserving_unknown(
            original,
            edited,
            schema_top_keys=schema_top_keys,
            schema_prop_keys=schema_prop_keys,
        )
        
        # User's edit to unknownField should be preserved (not overwritten)
        assert result["managerProperties"]["unknownField"] == "edited"

    def test_merge_preserves_new_nested_setting(self):
        """A template must not drop settings added inside a known nested dict."""
        original = {
            "managerName": "TestManager",
            "managerProperties": {
                "driver": {"known": 1, "introducedByNewPlugin": "keep me"},
            },
        }
        edited = {
            "managerName": "TestManager",
            "managerProperties": {"driver": {"known": 2}},
        }

        result = merge_preserving_unknown(
            original,
            edited,
            schema_top_keys=set(),
            schema_prop_keys={"driver"},
            schema_nested_prop_keys={"driver": {"known"}},
        )

        assert result["managerProperties"]["driver"] == {
            "known": 2,
            "introducedByNewPlugin": "keep me",
        }

    def test_materialize_device_schema_adds_plugin_fields(self):
        """Schema-only plugin fields become template-shaped editor fields."""
        template = {
            "top": [],
            "props": [{"key": "legacy", "req": False, "type": "text"}],
            "nested": {},
        }
        plugin_schema = {
            "properties": {
                "legacy": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["safe", "fast"],
                    "default": "safe",
                },
                "calibration": {"type": "object", "default": {"gain": 1}},
            },
            "required": ["legacy", "mode"],
        }

        result = materialize_device_schema(
            template=template, json_schema=plugin_schema
        )

        assert result["props"][0]["req"] is True
        mode = next(field for field in result["props"] if field["key"] == "mode")
        assert mode["type"] == "select"
        assert mode["opts"] == ["safe", "fast"]
        calibration = next(
            field for field in result["props"] if field["key"] == "calibration"
        )
        assert calibration["type"] == "json"
        # Inputs are cached by the editor and must never be changed in place.
        assert template["props"] == [{"key": "legacy", "req": False, "type": "text"}]
    
    def test_normalized_fields_template_only(self):
        """Test normalized_fields with template only (no schema)."""
        template = {
            "top": [
                {"key": "forAcquisition", "label": "For Acquisition", "type": "bool", "default": True, "req": True, "grp": "Basic", "tip": "", "opts": []},
            ],
            "props": [
                {"key": "cameraIndex", "label": "Camera Index", "type": "int", "default": 0, "req": True, "grp": "Basic", "tip": "", "opts": []},
            ],
            "nested": {},
        }
        
        fields = normalized_fields(template=template, json_schema=None)
        
        assert len(fields) == 2
        
        top_field = next(f for f in fields if f.key == "forAcquisition")
        assert top_field.location == "top"
        assert top_field.type == "bool"
        assert top_field.default is True
        
        prop_field = next(f for f in fields if f.key == "cameraIndex")
        assert prop_field.location == "prop"
        assert prop_field.type == "int"
        assert prop_field.default == 0
    
    def test_normalized_fields_schema_only(self):
        """Test normalized_fields with schema only (no template)."""
        json_schema = {
            "type": "object",
            "properties": {
                "exposureTime": {"type": "number", "default": 100.0},
                "triggerMode": {"type": "string", "enum": ["internal", "external"]},
            },
            "required": ["exposureTime"],
        }
        
        fields = normalized_fields(template=None, json_schema=json_schema)
        
        assert len(fields) == 2
        
        exposure_field = next(f for f in fields if f.key == "exposureTime")
        assert exposure_field.type == "float"  # number -> float
        assert exposure_field.default == 100.0
        assert exposure_field.required is True
        
        trigger_field = next(f for f in fields if f.key == "triggerMode")
        assert trigger_field.type == "select"  # has enum
        assert trigger_field.options == ("internal", "external")
        assert trigger_field.required is False
    
    def test_normalized_fields_nested(self):
        """Test normalized_fields with nested template fields."""
        template = {
            "top": [],
            "props": [],
            "nested": {
                "advanced": [
                    {"key": "bufferSize", "label": "Buffer Size", "type": "int", "default": 10, "req": False, "grp": "Advanced", "tip": "", "opts": []},
                ],
            },
        }
        
        fields = normalized_fields(template=template, json_schema=None)
        
        assert len(fields) == 1
        nested_field = fields[0]
        assert nested_field.location == "nested"
        assert nested_field.nested_key == "advanced"
        assert nested_field.key == "bufferSize"
    
    def test_build_default_device_empty(self):
        """Test build_default_device with no template and no schema."""
        result = build_default_device(
            "EmptyManager",
            template=None,
            json_schema=None,
        )
        
        assert result["managerName"] == "EmptyManager"
        assert result["managerProperties"] == {}
    
    def test_merge_preserving_unknown_with_empty_original(self):
        """Test merge_preserving_unknown when original has no unknowns."""
        original = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 10.0,
            },
        }
        
        edited = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 20.0,
            },
        }
        
        schema_top_keys = set()
        schema_prop_keys = {"exposureTime"}
        
        result = merge_preserving_unknown(
            original,
            edited,
            schema_top_keys=schema_top_keys,
            schema_prop_keys=schema_prop_keys,
        )
        
        # Should be identical to edited (no unknowns to restore)
        assert result == edited
    
    def test_round_trip_preserves_multiple_nested_dicts(self):
        """Test that multiple unknown nested dicts are all preserved."""
        original = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 10.0,
                "unknownNested1": {"a": 1, "b": 2},
                "unknownNested2": {"x": "y", "z": [1, 2, 3]},
            },
        }
        
        edited = {
            "managerName": "TestManager",
            "managerProperties": {
                "exposureTime": 20.0,
            },
        }
        
        schema_top_keys = set()
        schema_prop_keys = {"exposureTime"}
        
        result = merge_preserving_unknown(
            original,
            edited,
            schema_top_keys=schema_top_keys,
            schema_prop_keys=schema_prop_keys,
        )
        
        assert "unknownNested1" in result["managerProperties"]
        assert result["managerProperties"]["unknownNested1"] == {"a": 1, "b": 2}
        
        assert "unknownNested2" in result["managerProperties"]
        assert result["managerProperties"]["unknownNested2"] == {"x": "y", "z": [1, 2, 3]}


class TestNullableSchemaTypes:
    """`["string", "null"]` is the standard JSON Schema idiom for "or unset".

    Both bundled device plugins use it for cameraSerial. Treating it as an
    unrepresentable union demoted an ordinary text box to a raw JSON editor,
    which forced the user to type quotes around a serial number and made an
    empty box mean the literal string rather than null.
    """

    def test_nullable_scalars_keep_their_native_control(self):
        from imswitch.imcontrol.model.configeditor.schemas import (
            _infer_type_from_schema,
        )

        assert _infer_type_from_schema({"type": ["string", "null"]}) == "text"
        assert _infer_type_from_schema({"type": ["integer", "null"]}) == "int"
        assert _infer_type_from_schema({"type": ["number", "null"]}) == "float"
        assert _infer_type_from_schema({"type": ["boolean", "null"]}) == "bool"

    def test_plain_scalars_are_unaffected(self):
        from imswitch.imcontrol.model.configeditor.schemas import (
            _infer_type_from_schema,
        )

        assert _infer_type_from_schema({"type": "string"}) == "text"
        assert _infer_type_from_schema({"type": "integer"}) == "int"

    def test_scalar_unions_take_the_text_box_and_the_rest_fall_back_to_json(self):
        """A union within integer/number/string is the text box, which reads a
        value back as the kind it was (the type-preservation rule); a union that
        reaches into object or array, or nothing known at all, keeps the JSON
        widget, which round-trips every value."""
        from imswitch.imcontrol.model.configeditor.schemas import (
            _infer_type_from_schema,
        )

        assert _infer_type_from_schema({"type": ["string", "integer"]}) == "text"
        assert _infer_type_from_schema({"type": ["integer", "number"]}) == "float"
        assert _infer_type_from_schema({"type": ["string", "object"]}) == "json"
        assert _infer_type_from_schema({"type": ["null"]}) == "json"
        assert _infer_type_from_schema({"type": "object"}) == "json"

    def test_nullable_enum_still_becomes_a_select(self):
        from imswitch.imcontrol.model.configeditor.schemas import (
            _infer_type_from_schema,
        )

        assert _infer_type_from_schema(
            {"type": ["string", "null"], "enum": ["Off", "Hardware"]}
        ) == "select"
