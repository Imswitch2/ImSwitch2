# Widget State Persistence Framework

## Overview

The Widget State Persistence Framework provides a centralized, reusable system for saving and loading widget controller states to persistent storage. This allows ImSwitch to remember user preferences, widget configurations, and UI states across sessions.

**Key Design Principles:**
- **Controller-driven**: Controllers opt-in by implementing simple interface methods
- **Hardware-independent**: Pure UI state, never triggers hardware actions automatically
- **Robust**: Graceful handling of missing widgets, corrupted files, and version mismatches
- **Safe**: Explicit filtering of hardware-active states (lasers on, acquisition running, etc.)

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│  WidgetStatePersistence Service (Singleton)             │
│  - Registry of state-persistable controllers            │
│  - Save/load individual or all widget states            │
│  - Storage in ~/ImSwitchConfig/imcontrol_widget_states/ │
└─────────────────────────────────────────────────────────┘
                          │
                          │ registers with
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Widget Controllers (opt-in)                            │
│  - Implement getWidgetState() -> dict                   │
│  - Implement setWidgetState(state: dict) -> None        │
│  - Optional: getStateSchemaVersion() -> int             │
└─────────────────────────────────────────────────────────┘
```

### Storage Structure

```
~/ImSwitchConfig/
├── config/
├── imcontrol_setups/
└── imcontrol_widget_states/          # ← New directory
    ├── LaserController/
    │   ├── default.json
    │   ├── high_power.json
    │   └── scanning.json
    ├── PositionerController/
    │   └── default.json
    └── RecordingController/
        └── default.json
```

### State File Format

Each state file is a JSON document with metadata and state data:

```json
{
  "_metadata": {
    "controller_name": "LaserController",
    "schema_version": 1,
    "widget_type": "LaserWidget"
  },
  "state": {
    "laser_values": {
      "488 nm": 50.0,
      "561 nm": 30.5
    },
    "modulation_frequencies": {
      "488 nm": 1000
    },
    "modulation_duty_cycles": {
      "488 nm": 50
    },
    "selected_preset": "default"
  }
}
```

## Making a Widget State-Persistable

### Step 1: Implement Interface Methods

Add these methods to your controller class:

```python
from typing import Dict, Any
from imswitch.imcontrol.model import getWidgetStatePersistence

class MyWidgetController(ImConWidgetController):
    
    def getWidgetState(self) -> Dict[str, Any]:
        """
        Return current widget state as a dict.
        
        IMPORTANT: Do NOT include hardware-active states like:
        - laser enable/on states
        - acquisition running states
        - motor movement commands
        
        Only include passive configuration values.
        """
        return {
            'setting1': self._widget.getSetting1(),
            'setting2': self._widget.getSetting2(),
            # ... other passive settings
        }
    
    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """
        Restore widget state from a dict.
        
        IMPORTANT: This should NOT trigger hardware actions.
        Only update UI elements and configuration values.
        
        Handle missing keys gracefully (use dict.get with defaults).
        """
        try:
            setting1 = state.get('setting1')
            if setting1 is not None:
                self._widget.setSetting1(setting1)
            
            setting2 = state.get('setting2')
            if setting2 is not None:
                self._widget.setSetting2(setting2)
                
            self._logger.info('Widget state restored')
        except Exception as e:
            self._logger.error(f'Failed to restore widget state: {e}')
    
    def getStateSchemaVersion(self) -> int:
        """
        Optional: Return schema version for compatibility checking.
        Increment this when you make breaking changes to the state format.
        """
        return 1
```

### Step 2: Register with Persistence Service

In your controller's `__init__` method, register after setup is complete:

```python
def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    
    # ... normal initialization ...
    
    # Register for widget state persistence (at the end of __init__)
    getWidgetStatePersistence().register('MyWidgetController', self)
```

### Step 3: Done!

Your controller is now state-persistable. The framework will automatically:
- Handle save/load operations
- Manage storage location
- Log errors gracefully
- Handle missing/corrupted files

## Using the Persistence Service

### From Controllers

```python
from imswitch.imcontrol.model import getWidgetStatePersistence

persistence = getWidgetStatePersistence()

# Save current state
persistence.saveWidgetState('LaserController', 'my_preset')

# Load a saved state
persistence.loadWidgetState('LaserController', 'my_preset')

# List saved states
states = persistence.listSavedStates('LaserController')

# Delete a state
persistence.deleteWidgetState('LaserController', 'my_preset')

# Save all registered controllers
persistence.saveAllWidgetStates('session_snapshot')

# Load all registered controllers
persistence.loadAllWidgetStates('session_snapshot')
```

### From API (Future)

```python
# Via ImSwitch REST API
POST /api/widget_states/save
{
  "controller": "LaserController",
  "state_name": "my_preset"
}

GET /api/widget_states/load?controller=LaserController&state_name=my_preset
```

## Safety Considerations

### Critical Safety Rules

**⚠️ NEVER restore these states automatically:**
- Laser enable/on states
- Motor movement commands
- Acquisition start/run states
- Scan trigger states
- Any hardware action that could:
  - Damage equipment
  - Expose users to hazards
  - Waste consumables
  - Interfere with ongoing operations

### Safe States to Persist

**✅ Safe to restore automatically:**
- Laser power values (but NOT on/off state)
- Camera ROI coordinates
- Exposure times
- Gain/offset values
- UI layout preferences
- Selected options in dropdowns
- Numeric configuration values
- Text field contents

### Example: Laser Controller

```python
def getWidgetState(self):
    return {
        # ✅ Safe: power values (doesn't turn laser on)
        'laser_values': {...},
        
        # ✅ Safe: configuration values
        'modulation_frequencies': {...},
        'modulation_duty_cycles': {...},
        
        # ✅ Safe: UI state only
        'selected_preset': 'default'
        
        # ❌ NOT INCLUDED: enable states
        # 'laser_enabled': {...}  # ← Would be dangerous!
    }
```

## Error Handling

The framework is designed to never crash on errors:

### Missing Controller
```python
# Controller not registered → logs warning, continues
persistence.saveWidgetState('NonExistentController', 'default')
# → Logs: "Controller NonExistentController not registered..."
```

### Missing State File
```python
# File doesn't exist → returns None, logs debug message
state = persistence.loadWidgetState('LaserController', 'missing')
# → Returns: None
# → Logs: "No saved state found for LaserController/missing"
```

### Corrupted File
```python
# JSON parse error → returns None, logs error
state = persistence.loadWidgetState('LaserController', 'corrupted')
# → Returns: None
# → Logs: "Failed to load widget state: ..."
```

### Schema Version Mismatch
```python
# Saved version ≠ current version → logs warning, tries to load anyway
persistence.loadWidgetState('LaserController', 'old_version')
# → Logs: "Schema version mismatch: saved=1, current=2..."
# → Still attempts to load (controller handles compatibility)
```

### Missing Keys in State
```python
# Controller's setWidgetState should use dict.get() with defaults
def setWidgetState(self, state):
    # ✅ Good: handles missing key gracefully
    value = state.get('my_value', default_value)
    
    # ❌ Bad: crashes if key missing
    value = state['my_value']  # KeyError if missing!
```

## Integration with Existing Preset Systems

The widget state persistence framework is designed to work alongside existing preset systems:

### Laser Preset System

The `LaserController` now supports both:

1. **Legacy preset system** (existing functionality preserved):
   - Stored in setup JSON (`~/ImSwitchConfig/imcontrol_setups/<setup>.json`)
   - Managed via Laser widget buttons (Load/Save/Save As/Delete)
   - Used for scan default presets
   - **Continues to work exactly as before**

2. **New widget state persistence** (additional capability):
   - Stored separately in widget state JSON files
   - Can be used programmatically or via API
   - More general-purpose (works for any widget)
   - Includes more state (modulation settings, selected preset, etc.)

**Both systems coexist peacefully.** The existing laser preset UI and functionality are unchanged.

### Migration Strategy for Other Widgets

If a widget already has a preset/state system:

1. **Option A: Keep both systems**
   - Like LaserController: both work independently
   - Use legacy system for user-facing features
   - Use new system for programmatic/API access

2. **Option B: Migrate gradually**
   - Implement new system alongside old
   - Add deprecation warnings to old system
   - Migrate users over time

3. **Option C: New widgets use new system only**
   - Clean slate for new controllers
   - No legacy burden

## Examples

### Example 1: Simple Settings Widget

```python
class SettingsController(ImConWidgetController):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ... setup ...
        getWidgetStatePersistence().register('SettingsController', self)
    
    def getWidgetState(self):
        return {
            'exposure_time': self._widget.getExposureTime(),
            'gain': self._widget.getGain(),
            'selected_detector': self._widget.getSelectedDetector()
        }
    
    def setWidgetState(self, state):
        if 'exposure_time' in state:
            self._widget.setExposureTime(state['exposure_time'])
        if 'gain' in state:
            self._widget.setGain(state['gain'])
        if 'selected_detector' in state:
            self._widget.setSelectedDetector(state['selected_detector'])
```

### Example 2: Complex Widget with Nested State

```python
class ScanController(ImConWidgetController):
    
    def getWidgetState(self):
        return {
            'scan_dimensions': {
                'x_range': self._widget.getXRange(),
                'y_range': self._widget.getYRange(),
                'z_range': self._widget.getZRange()
            },
            'scan_resolution': {
                'x_pixels': self._widget.getXPixels(),
                'y_pixels': self._widget.getYPixels()
            },
            'scan_speed': self._widget.getScanSpeed(),
            'bidirectional': self._widget.isBidirectional()
        }
    
    def setWidgetState(self, state):
        # Handle nested state carefully
        dimensions = state.get('scan_dimensions', {})
        self._widget.setXRange(dimensions.get('x_range', [0, 100]))
        self._widget.setYRange(dimensions.get('y_range', [0, 100]))
        
        resolution = state.get('scan_resolution', {})
        self._widget.setXPixels(resolution.get('x_pixels', 512))
        self._widget.setYPixels(resolution.get('y_pixels', 512))
```

### Example 3: Version-Aware State

```python
class MyController(ImConWidgetController):
    
    def getWidgetState(self):
        # Schema version 2 adds new 'advanced_mode' field
        return {
            'basic_setting': self._widget.getBasicSetting(),
            'advanced_mode': self._widget.getAdvancedMode()  # New in v2
        }
    
    def setWidgetState(self, state):
        # Always provide defaults for backward compatibility
        basic = state.get('basic_setting', 'default')
        self._widget.setBasicSetting(basic)
        
        # New field might not exist in old states
        advanced = state.get('advanced_mode', False)
        self._widget.setAdvancedMode(advanced)
    
    def getStateSchemaVersion(self):
        return 2  # Increment when making breaking changes
```

## Testing

### Unit Testing State Persistence

```python
import pytest
from imswitch.imcontrol.model import getWidgetStatePersistence

def test_state_save_load():
    persistence = getWidgetStatePersistence()
    
    # Mock controller
    class MockController:
        def __init__(self):
            self.state = {'value': 42}
        
        def getWidgetState(self):
            return self.state.copy()
        
        def setWidgetState(self, state):
            self.state = state
    
    controller = MockController()
    persistence.register('MockController', controller)
    
    # Save state
    assert persistence.saveWidgetState('MockController', 'test')
    
    # Modify state
    controller.state = {'value': 0}
    
    # Load state
    persistence.loadWidgetState('MockController', 'test')
    assert controller.state['value'] == 42
```

### Integration Testing

```python
def test_laser_controller_state_persistence():
    # Assuming LaserController is instantiated
    persistence = getWidgetStatePersistence()
    
    # Set known state
    controller.setLaserValue('488 nm', 50.0)
    
    # Save
    persistence.saveWidgetState('LaserController', 'test')
    
    # Change state
    controller.setLaserValue('488 nm', 0.0)
    
    # Load
    persistence.loadWidgetState('LaserController', 'test')
    
    # Verify
    assert controller._widget.getValue('488 nm') == 50.0
```

## Future Enhancements

Potential improvements to consider:

1. **Automatic state saving**
   - Auto-save on app close
   - Auto-restore on app start
   - Configurable per-controller

2. **UI for state management**
   - Widget to browse/load/save states
   - State preview before loading
   - State diff visualization

3. **Cloud sync**
   - Sync states across machines
   - Team-shared configurations
   - Version control for states

4. **State validation**
   - Schema validation with jsonschema
   - Type checking
   - Range validation

5. **Migration tools**
   - Automatic state migration between schema versions
   - Batch update tools

6. **Export/Import**
   - Export states to standalone files
   - Share configurations between setups
   - Import community configurations

## Troubleshooting

### "Controller not registered" warning

**Problem:** Controller doesn't implement required methods or registration wasn't called.

**Solution:**
```python
# 1. Check method names (case-sensitive!)
def getWidgetState(self):  # ✅ Correct
def getwidgetstate(self):  # ❌ Wrong case

# 2. Ensure registration is called
getWidgetStatePersistence().register('MyController', self)
```

### State not restoring correctly

**Problem:** State loads but widget doesn't update.

**Solution:**
```python
# Check that setWidgetState actually calls widget methods
def setWidgetState(self, state):
    value = state.get('my_value')
    if value is not None:
        self._widget.setMyValue(value)  # ✅ Actually calls widget
        # self.my_value = value  # ❌ Only updates internal var
```

### Schema version mismatch

**Problem:** Old states don't work with new code.

**Solution:**
```python
def setWidgetState(self, state):
    # Handle multiple schema versions
    version = state.get('_schema_version', 1)
    
    if version == 1:
        # Convert v1 format to current
        value = state.get('old_key_name')
    elif version == 2:
        value = state.get('new_key_name')
    
    self._widget.setValue(value)
```

## Related Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) - ImSwitch architecture overview (same directory)
- [../adding-device-support.rst](../adding-device-support.rst) - Adding new hardware support
- [../scripting.rst](../scripting.rst) - Scripting API documentation

## API Reference

### WidgetStatePersistence

Main service class for widget state persistence.

#### Methods

- `register(controller_name, controller)` - Register a controller
- `unregister(controller_name)` - Unregister a controller
- `saveWidgetState(controller_name, state_name)` - Save a controller's state
- `loadWidgetState(controller_name, state_name, apply_immediately)` - Load a controller's state
- `saveAllWidgetStates(state_name)` - Save all registered controllers' states
- `loadAllWidgetStates(state_name)` - Load all registered controllers' states
- `listSavedStates(controller_name)` - List saved state names for a controller
- `deleteWidgetState(controller_name, state_name)` - Delete a saved state
- `getRegisteredControllers()` - Get list of registered controller names

### Controller Interface

Controllers implementing state persistence must provide:

#### Required Methods

- `getWidgetState() -> Dict[str, Any]` - Return current state as dict
- `setWidgetState(state: Dict[str, Any]) -> None` - Restore state from dict

#### Optional Methods

- `getStateSchemaVersion() -> int` - Return schema version (default: 1)

---

**Copyright (C) 2025 ImSwitch developers**
