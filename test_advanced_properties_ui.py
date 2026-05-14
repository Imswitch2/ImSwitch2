#!/usr/bin/env python3
"""
Test script for Hamamatsu Advanced Properties UI

This test verifies:
1. AdvancedPropertiesWidget can be instantiated
2. Widget can display property data
3. Widget can show messages
4. Signals are properly defined
5. SettingsWidget can handle advanced properties parameter
"""

import sys
from qtpy import QtWidgets, QtCore


def test_advanced_properties_widget():
    """Test the AdvancedPropertiesWidget class."""
    print("Testing AdvancedPropertiesWidget...")
    
    # Import the widget
    from imswitch.imcontrol.view.widgets.SettingsWidget import AdvancedPropertiesWidget
    
    # Create QApplication if it doesn't exist
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    
    # Create widget
    widget = AdvancedPropertiesWidget()
    
    # Test 1: Widget has required attributes
    assert hasattr(widget, 'table'), "Widget should have 'table' attribute"
    assert hasattr(widget, 'refreshButton'), "Widget should have 'refreshButton' attribute"
    assert hasattr(widget, 'sigRefreshClicked'), "Widget should have 'sigRefreshClicked' signal"
    print("✓ Widget attributes OK")
    
    # Test 2: Test setProperties with mock data
    mock_properties = [
        {
            'name': 'exposure_time',
            'id': 1,
            'value': 0.1,
            'type': 'REAL',
            'readable': True,
            'writable': True,
            'range': (0.001, 10.0),
            'text_options': None,
            'error': None
        },
        {
            'name': 'binning',
            'id': 2,
            'value': 1,
            'type': 'MODE',
            'readable': True,
            'writable': True,
            'range': None,
            'text_options': {b'1x1': 1, b'2x2': 2, b'4x4': 4},
            'error': None
        },
        {
            'name': 'sensor_temperature',
            'id': 3,
            'value': -20.5,
            'type': 'REAL',
            'readable': True,
            'writable': False,
            'range': (-50.0, 0.0),
            'text_options': None,
            'error': None
        },
        {
            'name': 'failed_property',
            'id': 4,
            'value': None,
            'type': 'NONE',
            'readable': False,
            'writable': False,
            'range': None,
            'text_options': None,
            'error': 'Property not supported'
        }
    ]
    
    widget.setProperties(mock_properties)
    assert widget.table.rowCount() == 4, f"Table should have 4 rows, got {widget.table.rowCount()}"
    assert widget.table.columnCount() == 5, f"Table should have 5 columns, got {widget.table.columnCount()}"
    print("✓ setProperties() works with mock data")
    
    # Test 3: Test showMessage
    widget.showMessage("Test message")
    assert widget.table.rowCount() == 1, "showMessage should create single row"
    assert widget.table.columnCount() == 1, "showMessage should create single column"
    print("✓ showMessage() works")
    
    # Test 4: Test signal connection
    signal_received = []
    widget.sigRefreshClicked.connect(lambda: signal_received.append(True))
    widget.refreshButton.click()
    assert len(signal_received) == 1, "Signal should be emitted when refresh button clicked"
    print("✓ Signal connection works")
    
    print("✓ All AdvancedPropertiesWidget tests passed!\n")
    return True


def test_settings_widget_integration():
    """Test that SettingsWidget can accept supportsAdvancedProperties parameter."""
    print("Testing SettingsWidget integration...")
    
    # Import necessary modules
    from imswitch.imcontrol.view.widgets.SettingsWidget import SettingsWidget
    
    # Create QApplication if it doesn't exist
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    
    # Create SettingsWidget
    widget = SettingsWidget()
    
    # Test that widget has new attributes
    assert hasattr(widget, 'advancedWidgets'), "SettingsWidget should have 'advancedWidgets' attribute"
    assert hasattr(widget, 'getAdvancedWidget'), "SettingsWidget should have 'getAdvancedWidget' method"
    assert hasattr(widget, 'hasAdvancedWidget'), "SettingsWidget should have 'hasAdvancedWidget' method"
    print("✓ SettingsWidget has new attributes and methods")
    
    # Test addDetector with supportsAdvancedProperties=False (default behavior)
    # We can't fully test this without a complete environment, but we can verify the method signature
    import inspect
    sig = inspect.signature(widget.addDetector)
    assert 'supportsAdvancedProperties' in sig.parameters, \
        "addDetector should have 'supportsAdvancedProperties' parameter"
    assert sig.parameters['supportsAdvancedProperties'].default == False, \
        "supportsAdvancedProperties should default to False"
    print("✓ addDetector has correct signature")
    
    print("✓ All SettingsWidget integration tests passed!\n")
    return True


def test_controller_method_exists():
    """Test that SettingsController has the refreshAdvancedProperties method."""
    print("Testing SettingsController...")
    
    # Import the controller
    from imswitch.imcontrol.controller.controllers.SettingsController import SettingsController
    
    # Check that refreshAdvancedProperties method exists
    assert hasattr(SettingsController, 'refreshAdvancedProperties'), \
        "SettingsController should have 'refreshAdvancedProperties' method"
    
    # Check method signature
    import inspect
    sig = inspect.signature(SettingsController.refreshAdvancedProperties)
    assert 'detectorName' in sig.parameters, \
        "refreshAdvancedProperties should have 'detectorName' parameter"
    
    print("✓ SettingsController has refreshAdvancedProperties method")
    print("✓ All SettingsController tests passed!\n")
    return True


def main():
    """Run all tests."""
    print("=" * 70)
    print("Hamamatsu Advanced Properties UI Test Suite")
    print("=" * 70 + "\n")
    
    all_passed = True
    
    try:
        test_advanced_properties_widget()
    except Exception as e:
        print(f"✗ AdvancedPropertiesWidget tests failed: {e}\n")
        all_passed = False
    
    try:
        test_settings_widget_integration()
    except Exception as e:
        print(f"✗ SettingsWidget integration tests failed: {e}\n")
        all_passed = False
    
    try:
        test_controller_method_exists()
    except Exception as e:
        print(f"✗ SettingsController tests failed: {e}\n")
        all_passed = False
    
    print("=" * 70)
    if all_passed:
        print("✓ All tests passed successfully!")
        print("=" * 70)
        return 0
    else:
        print("✗ Some tests failed")
        print("=" * 70)
        return 1


if __name__ == '__main__':
    sys.exit(main())
