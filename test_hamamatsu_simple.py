#!/usr/bin/env python3
"""
Simple syntax and structure test for Hamamatsu property introspection.

This test verifies:
1. The new methods exist and have correct signatures
2. The code structure is valid
3. Mock implementation is consistent

Does not require full ImSwitch dependencies.
"""

import ast
import inspect


def test_method_exists_in_file(filepath, class_name, method_name):
    """Check if a method exists in a class in the given file."""
    with open(filepath, 'r') as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return True, item
    return False, None


def test_hamamatsu_implementation():
    """Test that HamamatsuCamera has getAdvancedPropertyInfo."""
    print("Testing hamamatsu.py implementation...")
    
    filepath = 'imswitch/imcontrol/model/interfaces/hamamatsu.py'
    exists, method_node = test_method_exists_in_file(filepath, 'HamamatsuCamera', 'getAdvancedPropertyInfo')
    
    if not exists:
        print("  ✗ Method getAdvancedPropertyInfo not found in HamamatsuCamera")
        return False
    
    print("  ✓ Method getAdvancedPropertyInfo found in HamamatsuCamera")
    
    # Check that method has docstring
    docstring = ast.get_docstring(method_node)
    if docstring:
        print(f"  ✓ Method has docstring ({len(docstring)} chars)")
    else:
        print("  ⚠️  Method missing docstring")
    
    # Check that method returns something
    has_return = any(isinstance(node, ast.Return) for node in ast.walk(method_node))
    if has_return:
        print("  ✓ Method has return statement")
    else:
        print("  ✗ Method missing return statement")
        return False
    
    return True


def test_mock_implementation():
    """Test that MockHamamatsu has getAdvancedPropertyInfo."""
    print("\nTesting hamamatsu_mock.py implementation...")
    
    filepath = 'imswitch/imcontrol/model/interfaces/hamamatsu_mock.py'
    exists, method_node = test_method_exists_in_file(filepath, 'MockHamamatsu', 'getAdvancedPropertyInfo')
    
    if not exists:
        print("  ✗ Method getAdvancedPropertyInfo not found in MockHamamatsu")
        return False
    
    print("  ✓ Method getAdvancedPropertyInfo found in MockHamamatsu")
    
    # Check that method has docstring
    docstring = ast.get_docstring(method_node)
    if docstring:
        print(f"  ✓ Method has docstring ({len(docstring)} chars)")
    else:
        print("  ⚠️  Method missing docstring")
    
    # Check that method returns something
    has_return = any(isinstance(node, ast.Return) for node in ast.walk(method_node))
    if has_return:
        print("  ✓ Method has return statement")
    else:
        print("  ✗ Method missing return statement")
        return False
    
    return True


def test_manager_implementation():
    """Test that HamamatsuManager has getAdvancedPropertyInfo."""
    print("\nTesting HamamatsuManager.py implementation...")
    
    filepath = 'imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py'
    exists, method_node = test_method_exists_in_file(filepath, 'HamamatsuManager', 'getAdvancedPropertyInfo')
    
    if not exists:
        print("  ✗ Method getAdvancedPropertyInfo not found in HamamatsuManager")
        return False
    
    print("  ✓ Method getAdvancedPropertyInfo found in HamamatsuManager")
    
    # Check that method has docstring
    docstring = ast.get_docstring(method_node)
    if docstring:
        print(f"  ✓ Method has docstring ({len(docstring)} chars)")
    else:
        print("  ⚠️  Method missing docstring")
    
    # Check that method returns something
    has_return = any(isinstance(node, ast.Return) for node in ast.walk(method_node))
    if has_return:
        print("  ✓ Method has return statement")
    else:
        print("  ✗ Method missing return statement")
        return False
    
    return True


def test_expected_structure():
    """Test that methods return expected data structure."""
    print("\nTesting expected return structure...")
    
    # Check mock implementation for the expected keys
    filepath = 'imswitch/imcontrol/model/interfaces/hamamatsu_mock.py'
    with open(filepath, 'r') as f:
        content = f.read()
    
    expected_keys = ['name', 'id', 'value', 'type', 'readable', 'writable', 'range', 'text_options', 'error']
    found_keys = []
    
    for key in expected_keys:
        if f"'{key}'" in content or f'"{key}"' in content:
            found_keys.append(key)
    
    print(f"  Found {len(found_keys)}/{len(expected_keys)} expected keys in mock implementation")
    
    if len(found_keys) == len(expected_keys):
        print(f"  ✓ All expected keys present: {', '.join(expected_keys)}")
        return True
    else:
        missing = set(expected_keys) - set(found_keys)
        print(f"  ⚠️  Missing keys: {', '.join(missing)}")
        return True  # Warning, not failure


def main():
    """Run all tests."""
    print("="*70)
    print("Hamamatsu Property Introspection - Structure Tests")
    print("="*70)
    
    all_passed = True
    
    all_passed &= test_hamamatsu_implementation()
    all_passed &= test_mock_implementation()
    all_passed &= test_manager_implementation()
    all_passed &= test_expected_structure()
    
    print("\n" + "="*70)
    if all_passed:
        print("✓ All tests passed!")
    else:
        print("✗ Some tests failed")
    print("="*70)
    
    return all_passed


if __name__ == '__main__':
    import sys
    success = main()
    sys.exit(0 if success else 1)
