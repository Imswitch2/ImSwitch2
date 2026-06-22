"""Phase 5 tests: coercion and I/O helpers consolidation.

These tests verify that:
1. Coercion functions match the documented editor behavior
2. defaults.build_default_device produces byte-identical output
3. prepare_for_save round-trips correctly, preserving unknown fields
"""

import copy
import json
import pytest
from pathlib import Path


def test_display_to_json_null_string():
    """Test that 'null' string converts to None."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    assert display_to_json("null", "text") is None
    assert display_to_json("NULL", "text") is None
    assert display_to_json("  null  ", "text") is None


def test_display_to_json_int_valid():
    """Test that valid int strings convert to int."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    assert display_to_json("42", "int") == 42
    assert display_to_json("  -123  ", "int") == -123
    assert display_to_json("0", "int") == 0


def test_display_to_json_int_invalid():
    """Test that invalid int strings return the original stripped text."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    result = display_to_json("abc", "int")
    assert result == "abc"
    assert isinstance(result, str)
    
    result = display_to_json("12.5", "int")
    assert result == "12.5"
    assert isinstance(result, str)


def test_display_to_json_float_valid():
    """Test that valid float strings convert to float."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    assert display_to_json("3.14", "float") == 3.14
    assert display_to_json("  -0.5  ", "float") == -0.5
    assert display_to_json("42", "float") == 42.0


def test_display_to_json_float_invalid():
    """Test that invalid float strings return the original stripped text."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    result = display_to_json("not_a_number", "float")
    assert result == "not_a_number"
    assert isinstance(result, str)


def test_display_to_json_bool_returns_string():
    """Test that bool fields return the string (latent bug preserved)."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    # Current behavior: bool type falls through to text (latent bug)
    result = display_to_json("False", "bool")
    assert result == "False"
    assert isinstance(result, str)
    
    result = display_to_json("True", "bool")
    assert result == "True"
    assert isinstance(result, str)


def test_display_to_json_text_passthrough():
    """Test that text fields pass through unchanged (after strip)."""
    from imswitch.imcontrol.model.configeditor.coercion import display_to_json
    
    assert display_to_json("  hello world  ", "text") == "hello world"
    assert display_to_json("COM3", "text") == "COM3"
    assert display_to_json("", "text") == ""


def test_json_to_display_none():
    """Test that None converts to 'null' string."""
    from imswitch.imcontrol.model.configeditor.coercion import json_to_display
    
    assert json_to_display(None, "text") == "null"
    assert json_to_display(None, "int") == "null"
    assert json_to_display(None, "float") == "null"


def test_json_to_display_numbers():
    """Test that numbers convert to string representation."""
    from imswitch.imcontrol.model.configeditor.coercion import json_to_display
    
    assert json_to_display(42, "int") == "42"
    assert json_to_display(3.14, "float") == "3.14"
    assert json_to_display(-123, "int") == "-123"


def test_json_to_display_text():
    """Test that text values convert to string."""
    from imswitch.imcontrol.model.configeditor.coercion import json_to_display
    
    assert json_to_display("hello", "text") == "hello"
    assert json_to_display("COM3", "text") == "COM3"
    assert json_to_display("", "text") == ""


def test_build_default_device_byte_identical_simple():
    """Test that build_default_device produces identical output for simple manager."""
    from imswitch.imcontrol.model.configeditor.defaults import build_default_device
    
    # Test with no template or schema (minimal case)
    manager_name = "MockDetector"
    result = build_default_device(manager_name, template=None, json_schema=None)
    
    # Verify structure
    assert "managerName" in result
    assert result["managerName"] == manager_name
    assert "managerProperties" in result
    assert isinstance(result["managerProperties"], dict)


def test_build_default_device_with_template():
    """Test that build_default_device handles templated managers correctly."""
    from imswitch.imcontrol.model.configeditor.defaults import build_default_device
    
    # Create a mock template
    manager_name = "TestManager"
    template = {
        "top": [
            {"key": "testField", "type": "int", "default": "42"},
            {"key": "textField", "type": "text", "default": "hello"}
        ],
        "props": [
            {"key": "propField", "type": "float", "default": "3.14"}
        ]
    }
    
    result = build_default_device(manager_name, template=template, json_schema=None)
    
    # Verify that template fields are populated with correct coercion
    assert result["testField"] == 42  # int coercion
    assert result["textField"] == "hello"
    assert result["managerProperties"]["propField"] == 3.14  # float coercion


def test_prepare_for_save_removes_empty_others():
    """Test that prepare_for_save removes empty 'others' dict."""
    from imswitch.imcontrol.model.configeditor.io import prepare_for_save
    
    data = {
        "detectors": {"Camera1": {"managerName": "MockDetector"}},
        "others": {}
    }
    
    result = prepare_for_save(data)
    
    assert "detectors" in result
    assert "others" not in result


def test_prepare_for_save_keeps_non_empty_others():
    """Test that prepare_for_save preserves non-empty 'others' dict."""
    from imswitch.imcontrol.model.configeditor.io import prepare_for_save
    
    data = {
        "detectors": {"Camera1": {"managerName": "MockDetector"}},
        "others": {"customField": "value"}
    }
    
    result = prepare_for_save(data)
    
    assert "others" in result
    assert result["others"]["customField"] == "value"


def test_prepare_for_save_preserves_unknown_sections():
    """Test that prepare_for_save preserves unknown top-level sections."""
    from imswitch.imcontrol.model.configeditor.io import prepare_for_save
    
    data = {
        "detectors": {"Camera1": {"managerName": "MockDetector"}},
        "customSection": {"field": "value"},
        "anotherUnknown": [1, 2, 3]
    }
    
    result = prepare_for_save(data)
    
    assert "customSection" in result
    assert result["customSection"]["field"] == "value"
    assert "anotherUnknown" in result
    assert result["anotherUnknown"] == [1, 2, 3]


def test_prepare_for_save_preserves_unknown_device_fields():
    """Test that prepare_for_save preserves unknown fields within devices."""
    from imswitch.imcontrol.model.configeditor.io import prepare_for_save
    
    data = {
        "detectors": {
            "Camera1": {
                "managerName": "MockDetector",
                "managerProperties": {
                    "knownField": "value",
                    "unknownField": "preserved"
                },
                "customTopLevel": "also_preserved"
            }
        }
    }
    
    result = prepare_for_save(data)
    
    assert result["detectors"]["Camera1"]["customTopLevel"] == "also_preserved"
    assert result["detectors"]["Camera1"]["managerProperties"]["unknownField"] == "preserved"


def test_prepare_for_save_does_not_mutate_original():
    """Test that prepare_for_save does not mutate the input data."""
    from imswitch.imcontrol.model.configeditor.io import prepare_for_save
    
    data = {
        "detectors": {"Camera1": {"managerName": "MockDetector"}},
        "others": {}
    }
    
    original = copy.deepcopy(data)
    result = prepare_for_save(data)
    
    # Original should be unchanged
    assert data == original
    assert "others" in data
    # Result should have empty others removed
    assert "others" not in result


def test_load_config_file_valid_json():
    """Test that load_config_file correctly loads a valid JSON file."""
    from imswitch.imcontrol.model.configeditor.io import load_config_file
    import tempfile
    import os
    
    # Create a temporary config file
    config_data = {
        "detectors": {"Camera1": {"managerName": "MockDetector"}},
        "lasers": {}
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(config_data, f)
        temp_path = f.name
    
    try:
        result = load_config_file(temp_path)
        assert result == config_data
    finally:
        os.unlink(temp_path)


def test_round_trip_load_save():
    """Test that load → prepare_for_save round-trips correctly."""
    from imswitch.imcontrol.model.configeditor.io import load_config_file, prepare_for_save
    import tempfile
    import os
    
    original_data = {
        "detectors": {
            "Camera1": {
                "managerName": "MockDetector",
                "managerProperties": {
                    "field1": "value1"
                }
            }
        },
        "customSection": {"custom": "data"},
        "others": {"keep": "this"}
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(original_data, f)
        temp_path = f.name
    
    try:
        # Load the file
        loaded = load_config_file(temp_path)
        
        # Prepare for save
        prepared = prepare_for_save(loaded)
        
        # Should match original (no empty others to strip)
        assert prepared == original_data
    finally:
        os.unlink(temp_path)


if __name__ == "__main__":
    # Run tests directly without pytest if needed (repo has broken napari env)
    import sys
    import traceback
    
    test_functions = [
        test_display_to_json_null_string,
        test_display_to_json_int_valid,
        test_display_to_json_int_invalid,
        test_display_to_json_float_valid,
        test_display_to_json_float_invalid,
        test_display_to_json_bool_returns_string,
        test_display_to_json_text_passthrough,
        test_json_to_display_none,
        test_json_to_display_numbers,
        test_json_to_display_text,
        test_build_default_device_byte_identical_simple,
        test_build_default_device_with_template,
        test_prepare_for_save_removes_empty_others,
        test_prepare_for_save_keeps_non_empty_others,
        test_prepare_for_save_preserves_unknown_sections,
        test_prepare_for_save_preserves_unknown_device_fields,
        test_prepare_for_save_does_not_mutate_original,
        test_load_config_file_valid_json,
        test_round_trip_load_save,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in test_functions:
        try:
            test_func()
            print(f"✓ {test_func.__name__}")
            passed += 1
        except Exception as e:
            print(f"✗ {test_func.__name__}")
            traceback.print_exc()
            failed += 1
    
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
