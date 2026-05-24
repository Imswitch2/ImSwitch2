#!/usr/bin/env python
"""
Demonstration of widget state persistence for detector and scan controllers.

This script shows how to programmatically save and load widget states
for the newly integrated SettingsController and ScanControllerBase.

The user can also access these features via the UI:
- File > "Save Widget States…" (Ctrl+Shift+S)
- File > "Load Widget States…" (Ctrl+Shift+L)

Requirements:
- ImSwitch2 with widget state persistence framework
- Mock or real detector and scan controllers configured
"""

from imswitch.imcontrol.model import getWidgetStatePersistence
import json
from pathlib import Path


def demonstrate_persistence():
    """Demonstrate programmatic widget state persistence."""
    
    print("=" * 70)
    print("Widget State Persistence Demo")
    print("=" * 70)
    
    # Get the singleton persistence service
    persistence = getWidgetStatePersistence()
    
    print("\n1. Registered Controllers")
    print("-" * 70)
    registered = persistence._controllers.keys()
    if registered:
        for controller_name in registered:
            print(f"   - {controller_name}")
    else:
        print("   (No controllers registered yet - controllers register on init)")
    
    print("\n2. Available Saved States")
    print("-" * 70)
    try:
        states = persistence.listSavedStates()
        if states:
            for state_name in states:
                print(f"   - {state_name}")
        else:
            print("   (No saved states yet)")
    except Exception as e:
        print(f"   Error listing states: {e}")
    
    print("\n3. Example: Save All Widget States")
    print("-" * 70)
    print("   # In your controller code:")
    print("   persistence = getWidgetStatePersistence()")
    print("   persistence.saveAllStates('my_experiment_config')")
    print()
    print("   This saves states for:")
    print("   - Detector settings (exposure, ROI, binning, frame mode)")
    print("   - Scan parameters (size, step, center, TTL settings)")
    print("   - Laser settings (power, modulation, presets)")
    print("   - Any other registered controllers")
    
    print("\n4. Example: Load All Widget States")
    print("-" * 70)
    print("   # In your controller code:")
    print("   persistence = getWidgetStatePersistence()")
    print("   persistence.loadAllStates('my_experiment_config')")
    print()
    print("   This restores all widget states from the saved file.")
    
    print("\n5. Example: Controller Implementation")
    print("-" * 70)
    print("""
    class MyController:
        def __init__(self):
            # Register with persistence service
            persistence = getWidgetStatePersistence()
            persistence.register('MyController', self)
        
        def getWidgetState(self):
            '''Return current widget state as dict'''
            return {
                'data': {
                    'setting1': self._widget.getSetting1(),
                    'setting2': self._widget.getSetting2(),
                    # ... more settings
                },
                'metadata': {
                    'controller': 'MyController',
                    'schemaVersion': '1.0',
                    'widgetType': 'my_widget'
                }
            }
        
        def restoreWidgetState(self, state):
            '''Restore widget state from dict'''
            data = state.get('data', {})
            
            # Restore each property with error handling
            try:
                self._widget.setSetting1(data.get('setting1'))
            except Exception as e:
                self.__logger.warning(f"Failed to restore setting1: {e}")
            
            try:
                self._widget.setSetting2(data.get('setting2'))
            except Exception as e:
                self.__logger.warning(f"Failed to restore setting2: {e}")
    """)
    
    print("\n6. State File Format")
    print("-" * 70)
    state_dir = persistence._state_dir
    print(f"   Location: {state_dir}")
    print("   Format: JSON")
    print()
    print("   Example structure:")
    example_state = {
        "Settings": {
            "data": {
                "exposure": 50.0,
                "roi": {"x0": 0, "y0": 0, "x1": 512, "y1": 512},
                "binning": {"horizontal": 1, "vertical": 1},
                "frameMode": "Continuous",
                "detectorParameters": {"Gain": 10, "Offset": 0}
            },
            "metadata": {
                "controller": "Settings",
                "schemaVersion": "1.0",
                "widgetType": "detector_settings"
            }
        },
        "ScanController": {
            "data": {
                "scanSize": {"x": 100.0, "y": 100.0, "z": 10.0},
                "stepSize": {"x": 1.0, "y": 1.0, "z": 0.5},
                "centerPosition": {"x": 0.0, "y": 0.0, "z": 0.0},
                "ttlSettings": {"start": True, "each": False, "sequence": False},
                "dwellTime": 10.0
            },
            "metadata": {
                "controller": "ScanController",
                "schemaVersion": "1.0",
                "widgetType": "scan_controller"
            }
        }
    }
    print(json.dumps(example_state, indent=2))
    
    print("\n7. UI Integration")
    print("-" * 70)
    print("   Users can save/load widget states via the UI:")
    print()
    print("   File Menu:")
    print("   - File > 'Save Widget States…' (Ctrl+Shift+S)")
    print("   - File > 'Load Widget States…' (Ctrl+Shift+L)")
    print()
    print("   Workflow:")
    print("   1. Configure detector, scan, laser settings as desired")
    print("   2. File > Save Widget States (choose filename)")
    print("   3. Later: File > Load Widget States (select file)")
    print("   4. All widget states restored automatically")
    
    print("\n8. Safety Guarantees")
    print("-" * 70)
    print("   ✓ No automatic hardware actions on restore")
    print("   ✓ Per-property error handling (one failure doesn't break all)")
    print("   ✓ Only safe parameters persisted (no hardware-active states)")
    print("   ✓ Graceful degradation on missing/invalid properties")
    print("   ✓ Schema versioning for compatibility tracking")
    print("   ✓ Extensive logging for debugging")
    
    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)
    print()
    print("For more information, see:")
    print("- docs/design/WIDGET_STATE_PERSISTENCE.md")
    print("- PERSISTENCE_UI_INTEGRATION_SUMMARY.md")
    print()


def show_usage_examples():
    """Show practical usage examples."""
    
    print("\n" + "=" * 70)
    print("Practical Usage Examples")
    print("=" * 70)
    
    print("\n📋 Example 1: Quick Session Restore")
    print("-" * 70)
    print("""
    # Morning workflow:
    1. Start ImSwitch
    2. File > Load Widget States > "morning_routine.json"
    3. All settings restored (detector, scan, lasers)
    4. Ready to image immediately
    """)
    
    print("\n📋 Example 2: Configuration Sharing")
    print("-" * 70)
    print("""
    # Senior scientist to new student:
    1. Configure optimal imaging settings
    2. File > Save Widget States > "optimal_2P_imaging.json"
    3. Email file to student
    4. Student: File > Load Widget States > select received file
    5. Student has exact same settings
    """)
    
    print("\n📋 Example 3: Multiple Imaging Modes")
    print("-" * 70)
    print("""
    # Save different configs for different experiments:
    - File > Save Widget States > "widefield_10x.json"
    - File > Save Widget States > "confocal_60x.json"
    - File > Save Widget States > "tirf_100x.json"
    
    # Switch between modes instantly:
    - File > Load Widget States > "widefield_10x.json" → done!
    """)
    
    print("\n📋 Example 4: Before/After Comparisons")
    print("-" * 70)
    print("""
    # Before making risky changes:
    1. File > Save Widget States > "before_changes.json"
    2. Experiment with different settings
    3. If something breaks: File > Load Widget States > "before_changes.json"
    4. Instantly back to working state
    """)
    
    print("\n📋 Example 5: Programmatic Workflows")
    print("-" * 70)
    print("""
    # In a Python automation script:
    from imswitch.imcontrol.model import getWidgetStatePersistence
    
    persistence = getWidgetStatePersistence()
    
    # Load baseline config
    persistence.loadAllStates('baseline')
    
    # Run experiment 1
    run_experiment()
    
    # Load different config
    persistence.loadAllStates('high_power')
    
    # Run experiment 2
    run_experiment()
    """)
    
    print("\n" + "=" * 70)
    print()


if __name__ == '__main__':
    demonstrate_persistence()
    show_usage_examples()
    
    print("Try it yourself:")
    print("1. Start ImSwitch")
    print("2. Configure your settings")
    print("3. File > Save Widget States… (Ctrl+Shift+S)")
    print("4. Later: File > Load Widget States… (Ctrl+Shift+L)")
    print()
