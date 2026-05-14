#!/usr/bin/env python3
"""
Simple import test for Hamamatsu Advanced Properties UI

This test verifies that the modified files can be imported without errors.
"""

import sys


def test_imports():
    """Test that modules can be imported."""
    print("Testing imports...")
    
    try:
        # Test SettingsWidget module import
        print("  Importing SettingsWidget module...")
        from imswitch.imcontrol.view.widgets import SettingsWidget as settings_module
        print("  ✓ SettingsWidget module imported")
        
        # Check that AdvancedPropertiesWidget class exists
        assert hasattr(settings_module, 'AdvancedPropertiesWidget'), \
            "Module should have AdvancedPropertiesWidget class"
        print("  ✓ AdvancedPropertiesWidget class found")
        
        # Check that SettingsWidget class exists
        assert hasattr(settings_module, 'SettingsWidget'), \
            "Module should have SettingsWidget class"
        print("  ✓ SettingsWidget class found")
        
        # Test SettingsController module import
        print("  Importing SettingsController module...")
        from imswitch.imcontrol.controller.controllers import SettingsController as controller_module
        print("  ✓ SettingsController module imported")
        
        # Check that SettingsController class exists
        assert hasattr(controller_module, 'SettingsController'), \
            "Module should have SettingsController class"
        print("  ✓ SettingsController class found")
        
        # Check that refreshAdvancedProperties method exists
        assert hasattr(controller_module.SettingsController, 'refreshAdvancedProperties'), \
            "SettingsController should have refreshAdvancedProperties method"
        print("  ✓ refreshAdvancedProperties method found")
        
        return True
        
    except Exception as e:
        print(f"  ✗ Import failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_class_structure():
    """Test that classes have expected structure."""
    print("\nTesting class structure...")
    
    try:
        from imswitch.imcontrol.view.widgets.SettingsWidget import (
            AdvancedPropertiesWidget,
            SettingsWidget
        )
        from imswitch.imcontrol.controller.controllers.SettingsController import (
            SettingsController
        )
        
        # Check AdvancedPropertiesWidget methods
        assert hasattr(AdvancedPropertiesWidget, 'setProperties'), \
            "AdvancedPropertiesWidget should have setProperties method"
        assert hasattr(AdvancedPropertiesWidget, 'showMessage'), \
            "AdvancedPropertiesWidget should have showMessage method"
        print("  ✓ AdvancedPropertiesWidget methods found")
        
        # Check SettingsWidget methods
        assert hasattr(SettingsWidget, 'addDetector'), \
            "SettingsWidget should have addDetector method"
        assert hasattr(SettingsWidget, 'getAdvancedWidget'), \
            "SettingsWidget should have getAdvancedWidget method"
        assert hasattr(SettingsWidget, 'hasAdvancedWidget'), \
            "SettingsWidget should have hasAdvancedWidget method"
        print("  ✓ SettingsWidget methods found")
        
        # Check method signatures
        import inspect
        
        # SettingsWidget.addDetector signature
        sig = inspect.signature(SettingsWidget.addDetector)
        params = list(sig.parameters.keys())
        assert 'supportsAdvancedProperties' in params, \
            "addDetector should have supportsAdvancedProperties parameter"
        print("  ✓ addDetector signature correct")
        
        # SettingsController.refreshAdvancedProperties signature
        sig = inspect.signature(SettingsController.refreshAdvancedProperties)
        params = list(sig.parameters.keys())
        assert 'detectorName' in params, \
            "refreshAdvancedProperties should have detectorName parameter"
        print("  ✓ refreshAdvancedProperties signature correct")
        
        return True
        
    except Exception as e:
        print(f"  ✗ Structure test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 70)
    print("Hamamatsu Advanced Properties UI - Import Test")
    print("=" * 70 + "\n")
    
    all_passed = True
    
    if not test_imports():
        all_passed = False
    
    if not test_class_structure():
        all_passed = False
    
    print("\n" + "=" * 70)
    if all_passed:
        print("✓ All import tests passed successfully!")
        print("=" * 70)
        return 0
    else:
        print("✗ Some tests failed")
        print("=" * 70)
        return 1


if __name__ == '__main__':
    sys.exit(main())
