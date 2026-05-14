# Hamamatsu Advanced Property Introspection

## Overview

This document describes the advanced property introspection feature added to the Hamamatsu camera backend in ImSwitch2.

## Purpose

The `getAdvancedPropertyInfo()` method provides a comprehensive view of all DCAM camera properties with their metadata, making it easier to:
- Discover available camera settings
- Understand property constraints (ranges, options)
- Build dynamic UI controls
- Debug property access issues
- Generate configuration documentation

## API

### Camera Interface Layer

**Location:** `imswitch/imcontrol/model/interfaces/hamamatsu.py`

#### `HamamatsuCamera.getAdvancedPropertyInfo()`

Returns a list of dictionaries containing metadata for all discovered DCAM properties.

**Returns:** `List[Dict]` where each dictionary contains:

```python
{
    'name': str,              # Property name (e.g., 'exposure_time')
    'id': int,                # DCAM property ID
    'value': Union[int, float, None],  # Current property value
    'type': str,              # 'MODE', 'LONG', 'REAL', or 'NONE'
    'readable': bool,         # Whether property is readable
    'writable': bool,         # Whether property is writable
    'range': Optional[Tuple], # (min, max) for numeric properties
    'text_options': Optional[Dict],  # {b'text': value} for MODE properties
    'error': Optional[str]    # Error message if introspection failed
}
```

**Example:**

```python
camera = HamamatsuCameraMR(camera_id=0)
properties = camera.getAdvancedPropertyInfo()

for prop in properties:
    print(f"{prop['name']}: {prop['value']}")
    if prop['range']:
        print(f"  Range: {prop['range'][0]} to {prop['range'][1]}")
    if prop['text_options']:
        print(f"  Options: {list(prop['text_options'].keys())}")
```

### Manager Layer

**Location:** `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py`

#### `HamamatsuManager.getAdvancedPropertyInfo()`

Manager-level wrapper that provides UI-safe access to property introspection without exposing DCAM internals.

**Returns:** `List[Dict]` with same structure as camera interface method.

**Example:**

```python
# In a controller or UI handler
manager = detectorManagers['MyHamamatsuCamera']
properties = manager.getAdvancedPropertyInfo()

# Filter for writable properties only
writable_props = [p for p in properties if p['writable']]

# Build UI controls dynamically
for prop in writable_props:
    if prop['type'] == 'MODE' and prop['text_options']:
        # Create dropdown/combobox
        create_dropdown(prop['name'], prop['text_options'])
    elif prop['type'] in ['LONG', 'REAL'] and prop['range']:
        # Create slider or spinbox
        create_slider(prop['name'], prop['range'], prop['value'])
```

## Error Handling

The implementation uses **per-property error handling**:
- If one property fails to read, other properties continue to be introspected
- Errors are logged at DEBUG level to avoid noise
- Each property's `error` field contains failure details if applicable
- The method never throws exceptions - it returns an empty list on catastrophic failure

## Mock Support

**Location:** `imswitch/imcontrol/model/interfaces/hamamatsu_mock.py`

The `MockHamamatsu` class includes a full implementation of `getAdvancedPropertyInfo()` with realistic mock data:
- Proper property types (REAL, LONG, MODE)
- Realistic ranges
- Text options for MODE properties
- All standard camera properties

This allows testing without physical hardware.

## Property Types

### MODE
Properties with discrete text options (e.g., trigger mode, binning).
- Has `text_options` dict mapping names to values
- May have numeric range indicating number of modes

### LONG
Integer properties (e.g., image width, height).
- Has numeric `range` with (min, max)
- Value is an integer

### REAL
Floating-point properties (e.g., exposure time, frame rate).
- Has numeric `range` with (min, max)
- Value is a float

### NONE
Properties with unknown or unsupported type.
- Usually read-only status properties
- May not have range or options

## Backward Compatibility

This feature is **completely backward compatible**:
- All existing methods unchanged
- No changes to startup behavior
- Mock camera behavior preserved
- Manager properties (`managerProperties['hamamatsu']`) unchanged
- Existing detector controls continue to work

## Implementation Notes

### Thread Safety
The method queries properties synchronously. It should be called:
- Before acquisition starts, or
- When camera is idle, or
- In a safe camera action context (see `HamamatsuManager._performSafeCameraAction`)

### Performance
Property introspection involves DCAM API calls for each property. On a typical camera with 100+ properties:
- Initial call: ~100-500ms
- Results can be cached if needed
- No impact on frame acquisition

### Logging
Failed property reads are logged at DEBUG level:
```
DEBUG: Failed to get value for property 'unsupported_prop': Property not supported
```

Summary logging at INFO level:
```
INFO: Introspected 127 camera properties
```

## Usage Recommendations

### For UI Development
1. Call `getAdvancedPropertyInfo()` once during initialization
2. Cache results and use to build dynamic controls
3. Filter by `writable=True` for user-editable settings
4. Use `type` and `range`/`text_options` to choose control types

### For Debugging
1. Call after camera initialization to verify available properties
2. Check `error` field to diagnose property access issues
3. Compare properties between different camera models
4. Export to JSON for documentation

### For Configuration
1. Query properties to validate config file entries
2. Use ranges to validate parameter values
3. Use text_options to validate mode settings

## Future Enhancements

Potential future additions (not implemented yet):
- Property grouping (by category/iGroup)
- Property units (from iUnit field)
- Property dependencies (which properties affect others)
- Property change notifications
- Batch property get/set operations

## Testing

To test the implementation:

```python
# Test with mock camera (no hardware required)
from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu

camera = MockHamamatsu()
props = camera.getAdvancedPropertyInfo()
print(f"Found {len(props)} properties")

# Verify structure
assert all('name' in p for p in props)
assert all('type' in p for p in props)
assert all('value' in p for p in props)
```

## Related Files

- `imswitch/imcontrol/model/interfaces/hamamatsu.py` - Real camera implementation
- `imswitch/imcontrol/model/interfaces/hamamatsu_mock.py` - Mock camera implementation
- `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py` - Manager wrapper
- `test_hamamatsu_introspection.py` - Test script (requires dependencies)

## Commit Message

```
Add Hamamatsu advanced property introspection

Implement backend-side property introspection for Hamamatsu cameras:
- Add HamamatsuCamera.getAdvancedPropertyInfo() to expose all DCAM properties
- Returns structured metadata: value, type, range, text options, R/W status
- Graceful per-property error handling with logging
- Add MockHamamatsu implementation for testing without hardware
- Add HamamatsuManager.getAdvancedPropertyInfo() wrapper for UI layer
- Fully backward compatible, no changes to existing behavior
- Enables dynamic UI generation and better debugging

Addresses: Backend property introspection requirements
```
