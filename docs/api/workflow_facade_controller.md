# WorkflowFacadeController API

## Overview

`WorkflowFacadeController` is an API-only controller that provides a clean programmatic interface for constructing `MicroscopeFacade` objects without requiring direct access to internal modules or the `api._master` object.

## Purpose

Before this controller, users had to:
1. Import `build_facade_from_master` from internal workflow modules
2. Access `api._master` directly (breaking encapsulation)

With `WorkflowFacadeController`, facade construction is exposed as a public API method: `api.imcontrol.buildWorkflowFacade(...)`.

## API Method

### `api.imcontrol.buildWorkflowFacade(**kwargs)`

Constructs a `MicroscopeFacade` from the current ImSwitch master controller.

**Parameters:**
- `laser_aliases` (dict[str, str], optional): Map facade laser names to setupInfo laser names
  - Example: `{'488': 'Laser488', '405': 'Laser405'}`
- `detector_name` (str, optional): Name of detector/camera to expose
- `time_resolved_detector_name` (str, optional): Name of a detector manager that implements the time-resolved detector contract
- `xy_positioner_name` (str, optional): Name of XY positioner to expose
- `z_positioner_name` (str, optional): Name of Z positioner to expose
- `hwp_name` / `qwp_name` (str, optional): Names of HWP/QWP rotators
- `hwp_presets` / `qwp_presets` (`RotatorPresets`, optional): Script-level H/V angle overrides
- `pulsegen_name` (str, optional): Name of the ImSwitch pulse generator manager
- `wfs_teensy_port` / `wfs_teensy_baudrate` (optional): Direct serial connection for WFS Teensy firmware
- `scan_workflow` / `scan_done_signal` (optional): Overrides for scan orchestration. When omitted, the controller supplies the running communication channel's `scanWorkflow` and `sigScanDone`.

**Returns:**
- `MicroscopeFacade`: Facade object with hardware manager wrappers

**Example:**
```python
# Construct a facade with specific hardware
from imswitch.imcontrol.model.workflows import RotatorPresets

facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={'488': 'Laser488', '405': 'Laser405'},
    detector_name='Kiralux',
    xy_positioner_name='XY',
    z_positioner_name='Z',
    hwp_name='HWP',
    qwp_name='QWP',
    hwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
    qwp_presets=RotatorPresets(h_deg=0.0, v_deg=90.0),
)

# Use the facade in workflows
facade.laser_con.set_constant_power(['488'], [50.0])
facade.cam.prepare_live()
facade.cam.start_live()
facade.cam.stop_live()
```

### Time-resolved workflow example

```python
from imswitch.imcontrol.model.workflows import (
    GateSpec,
    GatedSTEDParams,
    GatedSTEDWorkflow,
)

facade = api.imcontrol.buildWorkflowFacade(
    time_resolved_detector_name='FLIM',
)

params = GatedSTEDParams(
    gates=(
        GateSpec('early', 0.5, 2.5),
        GateSpec('late', 2.5, 8.0),
    ),
    timeout_s=120.0,
)

# Uses facade.scan.run_once() automatically when no acquisition callable
# is passed, so it runs the currently configured ScanWidget scan.
result = GatedSTEDWorkflow(facade, params).run()
```

## Implementation Details

- **Controller Type**: API-only (no widget)
- **Registration**: Automatically registered in `ImConMainController.apiObjs`
- **Base Class**: `ImConWidgetController`
- **Exports**: `buildWorkflowFacade()` method via `@APIExport` decorator
- **Scan integration**: `buildWorkflowFacade()` attaches `facade.scan` by default when called from the running ImSwitch controller, allowing workflows to trigger and wait for the current scan configuration.

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
facade = api.imcontrol.buildWorkflowFacade(
    laser_aliases={'488': 'Laser488'},
    detector_name='Kiralux'
)
```

## Benefits

1. **Encapsulation**: No need to access `api._master`
2. **Cleaner API**: Single entry point for facade construction
3. **Maintainability**: Internal facade builder can be refactored without breaking user scripts
4. **Discoverability**: `api.imcontrol.buildWorkflowFacade(...)` is self-documenting

## Testing

Comprehensive unit tests validate:
- Argument forwarding to `build_facade_from_master`
- Facade object construction
- Time-resolved detector and scan facade construction
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
