# No-Hardware Validation Guide

## Overview

ImSwitch2 provides **no-hardware validation** to enable testing and development
without requiring physical microscope hardware. This guide explains how to run
these tests, what they validate, and how to add new tests safely.

> **Important:** No-hardware validation tests configuration parsing, manager
> construction, and control logic. It is **not hardware safety validation**.
> Physical hardware testing requires actual devices and is outside the scope of
> this validation layer.

---

## Running No-Hardware Tests

### Quick Start

To run the complete no-hardware test suite, use the following command from the repository root:

```bash
QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
  imswitch/imcontrol/_test/unit \
  imswitch/test_no_hardware_profile.py \
  imswitch/test_no_hardware_ui_smoke.py -q
```

### What This Command Does

1. **Runs configuration validation** (`imswitch/test_no_hardware_profile.py`)
   - Verifies the `example_no_hardware.json` setup parses correctly
   - Confirms no physical I/O channels are configured
   - Validates manager instantiation with mock devices

2. **Runs unit tests** (`imswitch/imcontrol/_test/unit/`)
   - Tests controllers, managers, and model logic
   - Uses mock hardware and Qt test fixtures
   - Validates contracts, state persistence, and workflows

3. **Runs ImControl startup smoke validation** (`imswitch/test_no_hardware_ui_smoke.py`)
   - Constructs the ImControl view/controller graph with the no-hardware setup
   - Verifies expected widgets and controllers are present
   - Stubs heavy GUI rendering dependencies so local napari/vispy/matplotlib
     incompatibilities do not block no-hardware startup validation

---

## Environment Variables Explained

### `QT_QPA_PLATFORM=offscreen`

**Purpose:** Runs Qt applications in headless mode without requiring a display server (X11/Wayland on Linux, native window system on macOS/Windows).

**Why it's needed:**
- Prevents GUI windows from appearing during tests
- Enables testing in CI environments without graphical displays
- Avoids race conditions from window manager interactions

**When to use:**
- Always for automated test runs
- In CI pipelines
- When running tests over SSH without X forwarding

### `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`

**Purpose:** Disables automatic loading of third-party pytest plugins.

**Why it's needed:**
- ImSwitch's test suite has specific plugin loading requirements
- Some plugins conflict with Qt test fixtures
- Explicitly loading `pytest-qt` (via `-p pytestqt.plugin`) ensures correct initialization order

**When to use:**
- Always when running no-hardware tests that use Qt widgets
- When you encounter plugin conflicts or initialization errors

---

## Test Categories

ImSwitch2 organizes tests into four categories by pytest marker:

| Marker          | Purpose                                      | Hardware Required | GUI Stack Imported |
|-----------------|----------------------------------------------|-------------------|--------------------|
| `@pytest.mark.nohardware` | Config parsing, manager construction, pure logic | No                | Minimal (Qt only)  |
| `@pytest.mark.ui`         | Widget behavior, controller-view contracts   | No                | Yes (napari/matplotlib) |
| `@pytest.mark.redzone`    | Hardware timing, DAQ waveforms, scan generation | No (simulation)   | Partial            |
| `@pytest.mark.hardware`   | Physical device communication                | Yes               | Yes                |

**Current status:**
- `nohardware` tests run in CI (this guide)
- `ui`, `redzone`, and `hardware` tests require additional setup

---

## What No-Hardware Tests May and May Not Do

### No-Hardware Tests SHOULD:

- Parse configuration files (`SetupInfo.from_json`)
- Instantiate managers with mock devices
- Test controller logic with mock signals
- Validate state persistence (save/load widget states)
- Test pure model functions (calculations, transformations)
- Verify contracts and interfaces (signal signatures, method existence)
- Check error handling for invalid configurations

### No-Hardware Tests MUST NOT:

- Trigger physical hardware actions (laser enable, stage movement, DAQ acquisition)
- Import the full napari/matplotlib stack during collection
- Require actual RS232/USB devices to be connected
- Depend on hardware timing or synchronization
- Modify hardware configuration files outside the test environment

### Safety Rules:

1. **Never enable hardware in test fixtures**: use `MockPositionerManager`,
   `AVManager` with `"cameraListIndex": "mock"`, and `nidaq.simulation = true`
2. **Never modify red-zone files** without explicit maintainer approval (see `AGENTS.md`)
3. **Never assume hardware safety validation**: no-hardware tests validate
   logic, not physical safety

---

## The No-Hardware Configuration Profile

Location: `imswitch/_data/user_defaults/imcontrol_setups/example_no_hardware.json`

**Key features:**
- **Mock camera:** `AVManager` with `"cameraListIndex": "mock"`
- **Mock positioners:** `MockPositionerManager` for X/Y/Z axes
- **No lasers:** Laser control omitted entirely
- **Simulated DAQ:** `"nidaq": {"simulation": true}`
- **No RS232 devices:** Serial communication disabled
- **No physical I/O channels:** All `analogChannel` and `digitalLine` fields are `null`

**Example snippet:**
```json
{
  "detectors": {
    "Mock Camera": {
      "managerName": "AVManager",
      "managerProperties": {
        "cameraListIndex": "mock",
        "exposure": 50
      }
    }
  },
  "positioners": {
    "Mock X": {
      "managerName": "MockPositionerManager",
      "axes": ["X"]
    }
  },
  "nidaq": {
    "simulation": true
  }
}
```

### Mock Scan Profiles

Additional scan-focused hardware-free setup files are available in
`imswitch/_data/user_defaults/imcontrol_setups/`:

- `mock_scan_setup.json`: pure software MoNaLISA/live-reconstruction setup with
  an `AVManager` mock camera, mock positioners, no physical AO/DO channels, and
  `nidaq.simulation = true`.
- `hamamatsu_mock_scan_setup.json`: pure software scan setup with a
  `HamamatsuManager` mock camera in external frame-trigger mode. The simulated
  NI-DAQ scan coordinator generates virtual camera triggers; all device
  `analogChannel` and `digitalLine` fields are `null`.
- `mixed_hamamatsu_apd_mock_scan_setup.json`: mixed mock detector setup with a
  trigger-gated mock Hamamatsu camera and synthetic APD scan data. The APD
  `ctrInputLine` and `terminal` entries are fake simulated input identifiers
  required by `APDManager`; no NI-DAQ input task is opened while
  `nidaq.simulation = true`. Optional APD mock properties such as
  `mockPhotonCountMean` and `mockPhotonCountMax` tune the synthetic integer
  count range.

APD mock detector data is modeled as cumulative counter input that reconstructs
to non-negative integer photon counts. PMT mock detector data is modeled as
`float32` voltage samples in a configurable range, defaulting to `-5.0..5.0` V.

Treat these as distinct modes: null AO/DO channels mean no physical scan output
is built, while fake `Dev1/...` detector input names are only manager
configuration placeholders for synthetic detector data.

---

## Adding New No-Hardware Tests

### Guidelines

1. **Import only what you need:**
   ```python
   # Good: imports only the manager
   from imswitch.imcontrol.model.managers import LaserManager
   
   # Bad: imports the full GUI stack
   from imswitch.imcontrol.view.widgets import LaserWidget
   ```

2. **Use pytest markers:**
   ```python
   import pytest
   
   pytestmark = pytest.mark.nohardware
   
   def test_laser_power_calculation():
       # Test logic here
       pass
   ```

3. **Use mock fixtures for Qt widgets:**
   ```python
   def test_controller_with_mock_widget(qtbot, mock_master_controller):
       widget = MockLaserWidget()
       qtbot.addWidget(widget)
       controller = LaserController(mock_master_controller, widget)
       # Test controller logic
   ```

4. **Validate configuration, not hardware:**
   ```python
   def test_nidaq_simulation_mode():
       setup_info = SetupInfo.from_json(NO_HARDWARE_CONFIG)
       assert setup_info.nidaq.simulation is True
       # Do NOT: actually initialize NidaqManager and send commands
   ```

### Example Test

```python
from pathlib import Path
import pytest

pytestmark = pytest.mark.nohardware


def test_widget_state_persistence_round_trip(tmp_path, monkeypatch):
    """Verify widget state can be saved and loaded without hardware."""
    from imswitch.imcontrol.model import getWidgetStatePersistence
    
    persistence = getWidgetStatePersistence()
    monkeypatch.setattr(persistence, "_stateDir", str(tmp_path / "widget_states"))
    
    class MockController:
        def __init__(self):
            self.state = {"laser_value": 50.0, "exposure": 100.0}

        def getWidgetState(self):
            return self.state

        def setWidgetState(self, state):
            self.state = state

    controller = MockController()
    persistence.register("MockController", controller)
    
    assert persistence.saveWidgetState("MockController", "test_state")
    controller.state = {}
    loaded = persistence.loadWidgetState("MockController", "test_state")
    
    assert loaded == {"laser_value": 50.0, "exposure": 100.0}
    assert controller.state == loaded
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'napari'"

**Cause:** A test file imports the full GUI stack during collection.

**Fix:** Move the import inside the test function or use a pytest fixture:
```python
def test_something():
    from imswitch.imcontrol.view.widgets import SomeWidget  # Import inside
    # test code
```

### "QApplication instance already exists"

**Cause:** Multiple tests try to create Qt applications.

**Fix:** Use the `qtbot` fixture from `pytest-qt`:
```python
def test_widget(qtbot):
    widget = MyWidget()
    qtbot.addWidget(widget)
    # test code
```

### "RuntimeError: No NI-DAQ device found"

**Cause:** Test is trying to use real hardware.

**Fix:** Ensure `nidaq.simulation = true` in your test configuration.

---

## CI Integration

The no-hardware validation should run in CI wherever the project test workflow
is enabled. Check the active GitHub Actions workflow before relying on a branch
protection rule.

**Expected CI job name:** "No-Hardware Validation"

**Command used:**
```bash
QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
  imswitch/imcontrol/_test/unit \
  imswitch/test_no_hardware_profile.py \
  imswitch/test_no_hardware_ui_smoke.py -v
```

**Exit criteria:**
- All tests pass
- No import errors from missing hardware libraries
- Execution time < 5 minutes on a typical local or CI runner

---

## Future Work

### Planned Test Coverage Expansions

1. **UI tests** (`@pytest.mark.ui`)
   - Widget rendering and layout
   - User interaction simulation (clicks, text entry)
   - Controller-view signal contracts

2. **Red-zone tests** (`@pytest.mark.redzone`)
   - DAQ waveform generation (simulated)
   - Scan pattern validation
   - TTL timing verification (no physical pulses)

3. **Full application startup**
   - Launch ImSwitch with `example_no_hardware.json`
   - Verify all widgets initialize
   - Check for startup errors or warnings

### Contributing

To add a new no-hardware test:

1. Write the test in `imswitch/imcontrol/_test/unit/`
2. Mark it with `@pytest.mark.nohardware`
3. Ensure it passes locally with the command above
4. Open a PR with the test included

---

## Related Documentation

- [AGENTS.md](../AGENTS.md): AI agent rules and red-zone files
- [Contributing Guide](contributing.rst): Development workflow
- [Architecture Overview](design/ARCHITECTURE.md): System structure
- [Adding Device Support](adding-device-support.rst): Hardware integration

---

## Summary

**No-hardware validation enables safe, fast testing of ImSwitch2 without physical microscopes.**

Key takeaways:
- Use `QT_QPA_PLATFORM=offscreen` for headless Qt testing
- Use `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` to avoid plugin conflicts
- Mark tests with `@pytest.mark.nohardware`
- Never import the full GUI stack during test collection
- Never trigger physical hardware actions in tests
- This is **logic validation**, not **hardware safety validation**

For questions or issues, see the [Contributing Guide](contributing.rst) or open a GitHub issue.
