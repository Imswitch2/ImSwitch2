# WorkflowFacadeController API

## Overview

`WorkflowFacadeController` is an API-only controller that provides a clean programmatic interface for constructing `MicroscopeFacade` objects without requiring direct access to internal modules or the `api._master` object.

## Purpose

Before this controller, users had to:
1. Import `build_facade_from_master` from internal workflow modules
2. Access `api._master` directly (breaking encapsulation)

With `WorkflowFacadeController`, facade construction is exposed as a public API method: `api.workflowFacade.build()`.

## API Method

### `api.workflowFacade.build(**kwargs)`

Constructs a `MicroscopeFacade` from the current ImSwitch master controller.

**Parameters:**
- `laser_aliases` (dict[str, str], optional): Map facade laser names to setupInfo laser names
  - Example: `{'488': 'Laser488', '405': 'Laser405'}`
- `detector_name` (str, optional): Name of detector/camera to expose
- `z_stage_name` (str, optional): Name of Z positioner to expose
- `rotation_stage_name` (str, optional): Name of rotation stage to expose
- `trig_device_name` (str, optional): Name of TTL trigger device

**Returns:**
- `MicroscopeFacade`: Facade object with hardware manager wrappers

**Example:**
```python
# Construct a facade with specific hardware
facade = api.workflowFacade.build(
    laser_aliases={'488': 'Laser488', '405': 'Laser405'},
    detector_name='Kiralux',
    z_stage_name='Z-Piezo'
)

# Use the facade in workflows
facade.laser_con.set_constant_power('488', 50.0)
facade.laser_con.enable('488')
facade.cam.snap_image()
```

## Implementation Details

- **Controller Type**: API-only (no widget)
- **Registration**: Automatically registered in `ImConMainController.apiObjs`
- **Base Class**: `ImConWidgetController`
- **Exports**: `build()` method via `@APIExport` decorator

## Usage in Workflows

### Before (manual facade construction):
```python
from imswitch.imcontrol.model.workflows.facade import build_facade_from_master

# Bad: Accessing internal _master attribute
facade = build_facade_from_master(
    api._master,
    laser_aliases={'488': 'Laser488'},
    detector_name='Kiralux'
)
```

### After (clean API):
```python
# Good: Using public API method
facade = api.workflowFacade.build(
    laser_aliases={'488': 'Laser488'},
    detector_name='Kiralux'
)
```

## Benefits

1. **Encapsulation**: No need to access `api._master`
2. **Cleaner API**: Single entry point for facade construction
3. **Maintainability**: Internal facade builder can be refactored without breaking user scripts
4. **Discoverability**: `api.workflowFacade.build()` is self-documenting

## Testing

Comprehensive unit tests validate:
- Argument forwarding to `build_facade_from_master`
- Facade object construction
- Controller registration and API exposure
- No-argument (minimal facade) construction

Run tests:
```bash
pytest imswitch/imcontrol/_test/unit/test_workflow_facade_controller.py -v
```

## Related Documentation

- [MicroscopeFacade](../workflows/facade.md) - Facade pattern documentation
- [Workflow System](../workflows/README.md) - Workflow architecture overview
- [@APIExport Decorator](../api/api_export.md) - API method decoration

## Commit

Added in commit `30e5e6b0` (2026-05-26)
