#!/usr/bin/env python3
"""
Test script for Hamamatsu advanced property introspection.

This script demonstrates the new getAdvancedPropertyInfo() functionality
by loading a mock Hamamatsu camera and introspecting all its properties.

Run with: python test_hamamatsu_introspection.py
"""

import sys
import json
from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu


def format_property_info(prop_info):
    """Format a property info dict for display."""
    lines = [f"\nProperty: {prop_info['name']}"]
    lines.append(f"  ID: {prop_info['id']}")
    lines.append(f"  Type: {prop_info['type']}")
    lines.append(f"  Value: {prop_info['value']}")
    lines.append(f"  Readable: {prop_info['readable']}, Writable: {prop_info['writable']}")
    
    if prop_info['range'] is not None:
        lines.append(f"  Range: {prop_info['range'][0]} to {prop_info['range'][1]}")
    
    if prop_info['text_options'] is not None:
        lines.append(f"  Text Options: {prop_info['text_options']}")
    
    if prop_info['error'] is not None:
        lines.append(f"  ⚠️  Error: {prop_info['error']}")
    
    return "\n".join(lines)


def test_mock_camera_introspection():
    """Test property introspection with mock camera."""
    print("="*70)
    print("Hamamatsu Advanced Property Introspection Test")
    print("="*70)
    
    # Create mock camera
    print("\n1. Creating mock Hamamatsu camera...")
    camera = MockHamamatsu()
    print(f"   Camera model: {camera.camera_model}")
    print(f"   Camera ID: {camera.camera_id}")
    
    # Get basic properties
    print("\n2. Getting basic properties via getProperties()...")
    props = camera.getProperties()
    print(f"   Found {len(props)} properties")
    
    # Test advanced introspection
    print("\n3. Running advanced property introspection...")
    try:
        advanced_info = camera.getAdvancedPropertyInfo()
        print(f"   ✓ Successfully introspected {len(advanced_info)} properties")
    except Exception as e:
        print(f"   ✗ Failed: {e}")
        return False
    
    # Display results
    print("\n4. Property Details:")
    print("-"*70)
    
    # Group properties by type
    by_type = {}
    for prop in advanced_info:
        prop_type = prop['type']
        if prop_type not in by_type:
            by_type[prop_type] = []
        by_type[prop_type].append(prop)
    
    # Display by type
    for prop_type in ['REAL', 'LONG', 'MODE', 'NONE']:
        if prop_type in by_type:
            print(f"\n{prop_type} Properties ({len(by_type[prop_type])}):")
            print("-"*70)
            for prop in by_type[prop_type]:
                print(format_property_info(prop))
    
    # Summary statistics
    print("\n5. Summary Statistics:")
    print("-"*70)
    total = len(advanced_info)
    readable = sum(1 for p in advanced_info if p['readable'])
    writable = sum(1 for p in advanced_info if p['writable'])
    with_range = sum(1 for p in advanced_info if p['range'] is not None)
    with_text = sum(1 for p in advanced_info if p['text_options'] is not None)
    with_errors = sum(1 for p in advanced_info if p['error'] is not None)
    
    print(f"   Total properties: {total}")
    print(f"   Readable: {readable}")
    print(f"   Writable: {writable}")
    print(f"   With numeric range: {with_range}")
    print(f"   With text options: {with_text}")
    print(f"   With errors: {with_errors}")
    
    # Export to JSON for inspection
    print("\n6. Exporting to JSON...")
    try:
        # Convert bytes keys to strings for JSON serialization
        json_safe_info = []
        for prop in advanced_info:
            prop_copy = prop.copy()
            if prop_copy['text_options']:
                prop_copy['text_options'] = {
                    k.decode('utf-8') if isinstance(k, bytes) else k: v
                    for k, v in prop_copy['text_options'].items()
                }
            json_safe_info.append(prop_copy)
        
        with open('hamamatsu_properties.json', 'w') as f:
            json.dump(json_safe_info, f, indent=2)
        print("   ✓ Saved to hamamatsu_properties.json")
    except Exception as e:
        print(f"   ⚠️  JSON export failed: {e}")
    
    print("\n" + "="*70)
    print("Test completed successfully!")
    print("="*70)
    return True


if __name__ == '__main__':
    success = test_mock_camera_introspection()
    sys.exit(0 if success else 1)
