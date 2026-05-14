# Hamamatsu Advanced Properties Editing

## Overview

The Hamamatsu Advanced Properties tab now supports **controlled editing** of writable camera properties. This feature allows users to safely modify camera parameters that are exposed by the DCAM API without breaking existing functionality.

## Feature Summary

### What Changed (from read-only to editable)

**Before (commit 8a6e358b):**
- Advanced Properties tab displayed all properties in read-only mode
- Users could only view current values, ranges, and options
- No way to change properties from the UI

**After (commit 24861632):**
- Writable properties have appropriate edit controls
- Users can modify values and click "Apply" to send changes to camera
- Read-only properties remain non-editable
- All changes are logged and validated

### UI Behavior

The Advanced Properties table now has 6 columns:

1. **Property** - Property name (e.g., `exposure_time`, `trigger_mode`)
2. **Current Value** - The current value from the camera
3. **New Value** - Edit control (spinbox, combo, or "—" for read-only)
4. **Apply** - Apply button (only for writable properties)
5. **Range/Options** - Numeric range or text options available
6. **Access** - R (readable), W (writable), or RW

### Edit Controls by Property Type

| Property Type | Edit Control | Notes |
|--------------|-------------|-------|
| Numeric with range (integer) | `QSpinBox` | Integer spinner with min/max from DCAM |
| Numeric with range (float) | `QDoubleSpinBox` | Float spinner with appropriate decimals |
| MODE with text options | `QComboBox` | Dropdown with all available text modes |
| Writable but no range/options | Read-only | Kept read-only for safety |
| Read-only | —  | No edit control shown |

### Safety Features

1. **Acquisition Stop/Restart**: Changes that require idle camera automatically:
   - Stop acquisition
   - Apply change
   - Restart acquisition

2. **Value Validation**: 
   - Numeric values clamped to camera-reported min/max
   - Text options validated against camera's list
   - Invalid properties rejected with logged error

3. **Error Handling**:
   - All failures logged with details
   - Properties refresh after successful change
   - Failed changes don't break the UI

4. **Logging**:
   - All change requests logged at INFO level
   - Success/failure logged with actual values
   - Errors logged at ERROR level with exception details

## Implementation Details

### Backend (HamamatsuManager)

**New Method**: `setAdvancedProperty(propertyName, value)`

```python
result = manager.setAdvancedProperty('exposure_time', 0.05)
# Returns: {'success': True, 'value': 0.05, 'error': None}
```

- Wraps `camera.setPropertyValue()` with safety checks
- Uses `_performSafeCameraAction()` for acquisition handling
- Returns structured result dict (success, value, error)
- Automatically converts string to bytes for text properties
- Fully compatible with mock camera

### UI (AdvancedPropertiesWidget)

**New Signal**: `sigPropertyChangeRequested(str, object)`

Emitted when user clicks Apply on a property.

**Methods**:
- `_createEditWidget(prop)` - Creates appropriate edit control based on property metadata
- `_onApplyClicked(row)` - Handles Apply button click, extracts value, emits signal
- `setProperties(properties)` - Updated to create edit controls for writable properties

### Controller (SettingsController)

**New Method**: `applyAdvancedProperty(detectorName, propertyName, value)`

- Receives property change request from widget
- Calls `manager.setAdvancedProperty()`
- Logs result
- Refreshes properties table on success

## Usage Example

1. Open ImSwitch with Hamamatsu camera configured
2. Go to Settings tab → Advanced Properties sub-tab
3. Find a writable property (marked **RW** in Access column)
4. Edit the value in the "New Value" column:
   - Use spinner controls for numeric properties
   - Use dropdown for MODE properties
5. Click **Apply** button for that property
6. The "Current Value" will update to reflect the change
7. Check logs for confirmation of the change

## Properties That Are Writable

Typical writable properties include:
- `exposure_time` - Camera exposure time
- `trigger_source` - Internal vs external trigger
- `trigger_mode` - Trigger mode options
- `binning` - Pixel binning
- `subarray_*` - ROI position and size
- Various sensor and readout modes (camera-specific)

**Note**: The exact list depends on your camera model and DCAM driver version.

## Backward Compatibility

✅ All existing behavior preserved:
- Read-only property display still works
- Non-Hamamatsu detectors unaffected
- Mock camera fully supported
- Existing managerProperties["hamamatsu"] config unchanged
- Standard detector controls (exposure, ROI, etc.) unchanged

## Testing Notes

- Syntax validation: ✅ All files pass `python -m py_compile`
- Mock camera support: ✅ Implemented and compatible
- Integration tests: ⚠️ Require full dependencies (not in CI yet)
- Manual testing: Required with real Hamamatsu hardware

## Files Modified

1. `imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py`
   - Added `setAdvancedProperty()` method (+70 lines)

2. `imswitch/imcontrol/view/widgets/SettingsWidget.py`
   - Rewrote `AdvancedPropertiesWidget` for editing (+262 lines, -55 lines)
   - Added `_createEditWidget()` helper
   - Added `_onApplyClicked()` handler
   - Added `sigPropertyChangeRequested` signal

3. `imswitch/imcontrol/controller/controllers/SettingsController.py`
   - Added `applyAdvancedProperty()` method (+64 lines)
   - Connected widget signals to handler

**Total**: +341 insertions, -55 deletions

## Future Enhancements

Possible improvements for future versions:
- Add visual feedback (success/error messages) in UI
- Add "Apply All" button for bulk changes
- Add property search/filter
- Add property change history/undo
- Add property presets/favorites
- Add property export/import

## Related Documentation

- Backend introspection: See commit 8a6e358b docs
- `HAMAMATSU_ADVANCED_PROPERTIES.md` - Original read-only implementation
- `HAMAMATSU_ADVANCED_PROPERTIES_IMPLEMENTATION.md` - Backend details

## Red-Zone Warning

⚠️ **Hardware Safety Note**: While this feature includes safety measures (acquisition stop/restart, value validation), modifying camera properties during an experiment can still cause issues:

- Some properties affect hardware timing
- Some properties can't be changed during acquisition (even with stop/restart)
- Invalid combinations of properties may cause errors

**Recommendation**: Test property changes with a test setup before using in production experiments.

## Questions?

For issues or questions about this feature:
1. Check ImSwitch logs for detailed error messages
2. Verify your DCAM driver version is compatible
3. Test with mock camera first to isolate hardware issues
4. Consult DCAM API documentation for property-specific details
