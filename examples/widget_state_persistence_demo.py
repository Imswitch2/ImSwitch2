"""
Widget State Persistence Framework - Demo Script

This script demonstrates how to use the widget state persistence framework
to save and restore controller states.

Note: This is a demonstration only. In actual usage, controllers automatically
register themselves and the persistence service is used programmatically.
"""

from typing import Dict, Any


# Minimal mock classes to demonstrate the pattern
class MockWidget:
    """Mock widget with some settings"""
    def __init__(self):
        self.laser_values = {'488nm': 0.0, '561nm': 0.0}
        self.exposure = 100.0
        self.gain = 1.0
    
    def setValue(self, laser, value):
        self.laser_values[laser] = value
    
    def getValue(self, laser):
        return self.laser_values[laser]
    
    def setExposure(self, exposure):
        self.exposure = exposure
    
    def getExposure(self):
        return self.exposure


class MockController:
    """
    Example controller implementing state persistence interface.
    
    This demonstrates the three methods needed:
    1. getWidgetState() - capture current state
    2. setWidgetState() - restore state
    3. getStateSchemaVersion() - optional version tracking
    """
    
    def __init__(self, widget):
        self._widget = widget
    
    def getWidgetState(self) -> Dict[str, Any]:
        """
        Capture current widget state.
        
        SAFETY: Does NOT include hardware-active states.
        Only passive configuration values.
        """
        return {
            'laser_values': self._widget.laser_values.copy(),
            'exposure': self._widget.exposure,
            'gain': self._widget.gain
        }
    
    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """
        Restore widget state.
        
        SAFETY: Does NOT trigger hardware actions.
        Uses dict.get() to handle missing keys gracefully.
        """
        # Restore laser values (but don't turn them on!)
        laser_values = state.get('laser_values', {})
        for laser, value in laser_values.items():
            if laser in self._widget.laser_values:
                self._widget.setValue(laser, value)
        
        # Restore settings
        exposure = state.get('exposure')
        if exposure is not None:
            self._widget.setExposure(exposure)
        
        gain = state.get('gain')
        if gain is not None:
            self._widget.gain = gain
    
    def getStateSchemaVersion(self) -> int:
        """Return schema version for compatibility checking"""
        return 1


def demo_basic_usage():
    """Demonstrate basic save/load operations"""
    print("=" * 60)
    print("Demo 1: Basic State Persistence")
    print("=" * 60)
    
    # Create mock controller
    widget = MockWidget()
    controller = MockController(widget)
    
    # Set some state
    print("\n1. Setting initial state:")
    controller._widget.setValue('488nm', 50.0)
    controller._widget.setValue('561nm', 30.0)
    controller._widget.setExposure(150.0)
    print(f"   488nm laser: {controller._widget.getValue('488nm')}")
    print(f"   561nm laser: {controller._widget.getValue('561nm')}")
    print(f"   Exposure: {controller._widget.getExposure()}")
    
    # Save state
    print("\n2. Capturing state:")
    saved_state = controller.getWidgetState()
    print(f"   Saved state: {saved_state}")
    
    # Change state
    print("\n3. Changing state:")
    controller._widget.setValue('488nm', 0.0)
    controller._widget.setValue('561nm', 0.0)
    controller._widget.setExposure(50.0)
    print(f"   488nm laser: {controller._widget.getValue('488nm')}")
    print(f"   561nm laser: {controller._widget.getValue('561nm')}")
    print(f"   Exposure: {controller._widget.getExposure()}")
    
    # Restore state
    print("\n4. Restoring state:")
    controller.setWidgetState(saved_state)
    print(f"   488nm laser: {controller._widget.getValue('488nm')}")
    print(f"   561nm laser: {controller._widget.getValue('561nm')}")
    print(f"   Exposure: {controller._widget.getExposure()}")
    
    print("\n✓ State restored successfully!")


def demo_missing_keys():
    """Demonstrate graceful handling of missing keys"""
    print("\n" + "=" * 60)
    print("Demo 2: Graceful Handling of Missing Keys")
    print("=" * 60)
    
    widget = MockWidget()
    controller = MockController(widget)
    
    # Set initial state
    print("\n1. Setting initial state:")
    controller._widget.setValue('488nm', 50.0)
    controller._widget.setExposure(150.0)
    print(f"   488nm laser: {controller._widget.getValue('488nm')}")
    print(f"   Exposure: {controller._widget.getExposure()}")
    
    # Try to restore state with missing keys
    print("\n2. Restoring partial state (missing 'gain' key):")
    partial_state = {
        'laser_values': {'488nm': 25.0},
        # 'gain' is missing - should use current value
    }
    controller.setWidgetState(partial_state)
    print(f"   488nm laser: {controller._widget.getValue('488nm')} (restored)")
    print(f"   Exposure: {controller._widget.getExposure()} (unchanged)")
    print(f"   Gain: {controller._widget.gain} (unchanged)")
    
    print("\n✓ Missing keys handled gracefully!")


def demo_unknown_lasers():
    """Demonstrate handling of unknown/missing hardware"""
    print("\n" + "=" * 60)
    print("Demo 3: Handling Unknown Hardware")
    print("=" * 60)
    
    widget = MockWidget()
    controller = MockController(widget)
    
    print("\n1. Current lasers:", list(widget.laser_values.keys()))
    
    # Try to restore state with unknown laser
    print("\n2. Restoring state with unknown laser '640nm':")
    state_with_unknown = {
        'laser_values': {
            '488nm': 50.0,
            '640nm': 40.0  # This laser doesn't exist
        }
    }
    
    controller.setWidgetState(state_with_unknown)
    print(f"   488nm laser: {controller._widget.getValue('488nm')} (restored)")
    print(f"   640nm laser: skipped (doesn't exist)")
    
    print("\n✓ Unknown hardware handled gracefully!")


def demo_safety_considerations():
    """Demonstrate safety features"""
    print("\n" + "=" * 60)
    print("Demo 4: Safety Considerations")
    print("=" * 60)
    
    print("""
State persistence NEVER includes:
  ❌ Laser enable/on states
  ❌ Acquisition running states
  ❌ Motor movement commands
  ❌ Any hardware-active states

State persistence ONLY includes:
  ✅ Laser power values (but doesn't turn them on)
  ✅ Exposure/gain/ROI settings
  ✅ UI configuration values
  ✅ Selected options in dropdowns

This ensures that loading a state cannot:
  • Damage equipment
  • Expose users to hazards
  • Interfere with ongoing operations
    """)
    
    print("Example - LaserController.getWidgetState():")
    print("""
    return {
        'laser_values': {...},              # ✅ Safe: values only
        'modulation_frequencies': {...},    # ✅ Safe: configuration
        'selected_preset': 'default'        # ✅ Safe: UI state
        
        # NOT INCLUDED:
        # 'laser_enabled': {...}            # ❌ Would turn lasers on!
    }
    """)
    
    print("✓ Safety-first design!")


def main():
    """Run all demonstrations"""
    print("\n" + "=" * 60)
    print("WIDGET STATE PERSISTENCE FRAMEWORK - DEMONSTRATION")
    print("=" * 60)
    
    demo_basic_usage()
    demo_missing_keys()
    demo_unknown_lasers()
    demo_safety_considerations()
    
    print("\n" + "=" * 60)
    print("For more information, see:")
    print("  docs/WIDGET_STATE_PERSISTENCE.md")
    print("=" * 60 + "\n")


if __name__ == '__main__':
    main()


# Copyright (C) 2025 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
