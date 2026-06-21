# Audit Report 6 — Test-Suite Health

## Summary

- **Collection health**: 968 tests collected, **1 collection error** (`test_example_setups.py` fails with `KeyError: 'maxScanTimeMin'` on all platforms). CI works around this by excluding UI tests (ci.yml) or skipping the failing file (imswitch-test.yml).
- **Coverage gap**: **37/50 controllers untested** (74%), **66/80 managers untested** (82%). High-priority gaps include `BeadRecController` (21 commits in 2024), `EventTriggeredBaseController` (safety-critical smart microscopy), and DAQ-related managers (`NidaqLaser`, `TriggerScopeLaser`).
- **No-hardware profile**: Works correctly; `test_no_hardware_profile.py` validates that mock hardware can construct core managers without I/O leakage. No physical port access found in no-hardware tests.
- **Brittle contract tests**: 5 tests use `inspect.getsource()` to assert on source text (e.g., checking registration order in `__init__`), which breaks on refactoring.
- **CI masking**: `.github/workflows/ci.yml` avoids the collection error by excluding `imswitch/imcontrol/_test/ui` entirely. Manual workflow (`imswitch-test.yml`) documents the issue but still ignores the file on macOS and Windows.
- **Test structure**: 26k lines of test code across 98 test files; moderate fixture duplication (26 unique mock fixtures, avg 3.1 per file); sleeps are patched out for speed.

## Collection status

### Command output

```bash
$ python3 -m pytest --collect-only -q imswitch/imcontrol/_test
[968 tests collected, 1 error in 2.16s]

ERROR imswitch/imcontrol/_test/ui/test_example_setups.py - KeyError: 'maxScanTimeMin'
```

**Verified count**: 968 tests collected (unit + UI).

### What's blocked

- **`test_example_setups.py`** (imswitch/imcontrol/_test/ui/test_example_setups.py): Collection fails with:
  ```
  KeyError: 'maxScanTimeMin'
  ... dataclasses_json/core.py:185: in _decode_dataclass
      field_value = kvs[field.name]
  ```
  - **Root cause**: Setup config schema mismatch (missing required field `maxScanTimeMin` in scan configuration). This is a **config validation error**, not a napari plugin issue.
  - **Impact**: All 3 test functions in this file are uncollectable. Estimated ~10-15 tests blocked (parametrized setups).

- **`-p no:napari` does NOT help**: Running with `pytest -p no:napari` still yields the same error. The issue is in config deserialization, not the napari pytest11 plugin.

### CI workaround strategy

**`.github/workflows/ci.yml`** (per-push, no-hardware):
```yaml
run: |
  pytest -p pytestqt.plugin -p pytest_timeout \
    imswitch/test_no_hardware_profile.py \
    imswitch/imcontrol/_test/unit \
    imswitch/imcontrol/controller/_test \
    imswitch/improcess/_test \
    --ignore=imswitch/improcess/_test/test_snouty.py \
    --timeout=180 -v
```
- **Excludes** `imswitch/imcontrol/_test/ui` entirely → avoids collection error.
- **Also sets** `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (disables napari plugin auto-loading).

**`.github/workflows/imswitch-test.yml`** (manual, full-suite):
```yaml
# Lines 59-60, 66-67
--ignore=imswitch/imcontrol/_test/ui/test_example_setups.py \
--ignore=imswitch/imcontrol/_test/ui/test_liveview.py  # Windows only
```
- **Ignores** `test_example_setups.py` on **Windows and macOS** (lines 59-67).
- **Reason** (from comments): "LiveView test fails on GitHub actions on Windows with napari 0.4.7. This is likely a problem specific to this GitHub action... TODO: Remove this special case when napari is unpinned" (imswitch-test.yml:53-57).

**Verdict**: CI is effectively **dependent on the ignore workaround**. Without it, all runs would fail at collection. The suite is **not fully executable** without manual exclusion.

---

## Coverage gap map

### Controllers: 13/50 tested (26%)

**TESTED** (test file exists and imports controller):
- `BeadRec` (partial: `test_beadrec_scan_source.py` covers scan source, not full controller)
- `EtMonalisa`, `EtSnouty`, `EtSTED` (event-triggered smart modes)
- `FlipMirror`, `Image`, `Laser`, `LeicaStand`, `Positioner`, `Recording`, `SLM`, `Settings`, `Tiling`

**UNTESTED** (37 controllers, selected high-priority):
- **`BeadRecController`**: 21 commits in 2024 (most-changed controller); core BeadRec feature; **CRITICAL GAP**.
- **`EventTriggeredBaseController`**: Base class for smart microscopy modes (event-triggered acquisition); safety-critical, **NO TESTS**.
- **`AutofocusController`**: Hardware control (focus lock); actively maintained; **NO TESTS**.
- **`BSC203Controller`**: Stage controller (Thorlabs BSC203); added 2024; **NO TESTS**.
- **`FocusLockController`**: Real-time focus stabilization; hardware-intensive; **NO TESTS**.
- **`ScanControllerAdvanced`**: 20 commits in 2024; scan orchestration; **NO TESTS** (only indirect coverage via `test_scan_lifecycle.py`).
- **`ScanControllerMoNaLISA`**: 11 commits; MoNaLISA-specific scan; **NO TESTS**.
- **`RotationScanController`**: 10 commits; 3D rotation scan; **NO TESTS**.

See also: `AlignAverage`, `AlignXY`, `AlignmentLine`, `BFTimelapse`, `Console`, `FFT`, `FLIMHist`, `LightSheetMulticolor`, `LineProfile`, `MotCorr`, `RotationScan`, `STEDTimelapse`, `ScriptingAutomation`, `StandControl`, `TriggerScopeRaster`, `UCManager`, `View`, `ViewerTools` (all untested).

### Managers: 14/80 tested (18%)

**TESTED**:
- `AVManager`, `DetectorManager`, `ESP32Manager`, `HamamatsuManager`, `LaserManager`, `MultiManager`, `PositionerManager`, `PulseGeneratorManager`, `RecordingManager`, `SLMManager`, `StandManager`, `TISManager`, `TeensyPulseManager`, `TriggerScopeManager`

**UNTESTED** (66 managers, safety-critical subset):
- **DAQ/Laser safety**: `NidaqLaserManager`, `TriggerScopeLaserManager`, `PulseStreamerLaserManager` (control laser power via DAQ; **safety-critical**, NO TESTS).
- **DAQ/Positioner safety**: `NidaqPositionerManager`, `TriggerScopePositionerManager` (control stage via DAQ; **safety-critical**, NO TESTS).
- **New laser manager**: `Cobolt0601NewLaserManager` (replacement for `Cobolt0601LaserManager`; actively developed; **NO TESTS**).
- **Vendor SDKs**: `BaslerManager`, `PhotometricsManager`, `ThorCamTSIManager`, `GXPIPYManager` (camera SDKs; untested).
- **Stage managers**: `BSC203StageManager`, `KDC101PositionerManager`, `KinesisStageManager`, `PIStageManager`, `SmarACTPositionerManager` (hardware control; untested).

**Total manager gap**: 66/80 (82%) have NO unit tests. The untested set includes **safety-critical DAQ/laser/stage managers** that directly control hardware via analog/digital lines.

---

## Findings

### [HIGH] Collection error blocks 10+ tests; CI silently ignores failing file
**Evidence**:
- `pytest --collect-only` fails with `KeyError: 'maxScanTimeMin'` in `test_example_setups.py` (imswitch/imcontrol/_test/ui/test_example_setups.py).
- CI workflows explicitly exclude this file (ci.yml excludes all UI; imswitch-test.yml ignores the file on macOS/Windows).
- Root cause: Setup config schema changed but example configs not updated. The `dataclasses_json` deserializer expects a required field `maxScanTimeMin` in `ScanInfo`, but at least one example setup JSON is missing it.

**Recommendation**:
1. **Fix the schema mismatch**: Add `maxScanTimeMin` to all example setups in `imswitch/_data/user_defaults/imcontrol_setups/*.json`, OR make the field optional with a default in `model/SetupInfo.py`.
2. **Remove CI ignore**: Once fixed, remove the `--ignore` workaround from imswitch-test.yml (lines 59-67) and re-enable UI tests in ci.yml.
3. **Add schema validation tests**: Create a dedicated test (`test_example_setups_schema.py`) that validates all bundled configs parse without errors, so schema drift is caught early.

---

### [HIGH] BeadRecController has NO tests despite 21 commits in 2024
**Evidence**:
- `git log --since="2024-01-01" --name-only` shows `BeadRecController.py` with 21 commits (most-changed controller).
- Only related test is `test_beadrec_scan_source.py`, which covers the `_BeadRecScanSource` helper class, NOT the controller itself.
- `BeadRecController` orchestrates live bead tracking, reconstruction, and scan feedback — core ImSwitch functionality with complex state.

**Recommendation**:
1. **Create `test_beadrec_controller.py`**: Cover controller initialization, scan start/stop, reconstruction triggering, and state transitions.
2. **Focus on safety**: Test that failed reconstruction does not crash scan, and that scan abort properly cleans up resources.
3. **Use existing mock harness**: `test_beadrec_scan_source.py` already mocks `RecordingManager` and `BeadRecManager`; reuse fixtures.

---

### [HIGH] EventTriggeredBaseController (smart microscopy safety) is untested
**Evidence**:
- `EventTriggeredBaseController` (imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py) is the base class for event-triggered smart modes (`EtSTEDController`, `EtMonalisaController`, `EtSnoutyController`).
- Defines core event-handling (`_onDynamicEvent`, `_onLoopCycleReached`, `_onDynamicAutosave`) and setup orchestration (`_setupAllManagers`).
- **No unit tests** exist for this base class. Derived classes (`EtSTED`, `EtMonalisa`) have tests (`test_event_triggered_smart_modes.py`), but they only test derived behavior, NOT the shared base orchestration.

**Recommendation**:
1. **Create `test_event_triggered_base.py`**: Test base class event registration, loop cycle handling, and autosave triggering.
2. **Test setup ordering**: Verify `_setupAllManagers` calls happen in correct order (DetectorsManager, LaserManager, RecordingManager, ScanManager).
3. **Test error propagation**: Ensure that exceptions in event handlers do not leave the controller in a broken state.

---

### [MED] 82% of managers untested; includes DAQ/laser/stage safety-critical code
**Evidence**:
- 66/80 managers have NO unit tests (see coverage gap map above).
- Untested set includes:
  - **DAQ laser control**: `NidaqLaserManager`, `TriggerScopeLaserManager`, `PulseStreamerLaserManager` (set laser power via analog output).
  - **DAQ positioner control**: `NidaqPositionerManager`, `TriggerScopePositionerManager` (move stage via analog output).
  - **New laser manager**: `Cobolt0601NewLaserManager` (replacement for old Cobolt driver; added 2024).
- These managers directly write to hardware via `nidaqmx` or serial ports — bugs can damage equipment or harm samples.

**Recommendation**:
1. **Prioritize DAQ safety**: Create `test_nidaq_laser_manager.py`, `test_triggerscope_laser_manager.py` with mocked DAQ tasks. Test:
   - Power setpoint clamping (0-100% range).
   - Analog channel assignment (correct AO line).
   - Shutdown behavior (laser off on finalize).
2. **Test stage safety**: `test_nidaq_positioner_manager.py`, `test_triggerscope_positioner_manager.py`:
   - Position clamping (min/max axis limits).
   - Move abort (stop on controller finalize).
   - Error handling (DAQ task failure does not crash manager).
3. **Add `@pytest.mark.redzone`**: Flag all DAQ/laser/stage tests as `redzone` (safety-critical) per pyproject.toml markers.

---

### [MED] CI only runs no-hardware subset; full suite only in manual workflow
**Evidence**:
- `.github/workflows/ci.yml` (per-push):
  - **Excludes** `imswitch/imcontrol/_test/ui` (all napari GUI tests).
  - **Only runs**: `test_no_hardware_profile.py`, `imcontrol/_test/unit`, `imcontrol/controller/_test`, `improcess/_test`.
  - **Env**: `QT_QPA_PLATFORM=offscreen`, `MPLBACKEND=Agg`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.
- `.github/workflows/imswitch-test.yml` (manual `workflow_dispatch`):
  - **Runs** full suite including UI tests, but **ignores** `test_example_setups.py` (collection error) and `test_liveview.py` (Windows napari crash).
  - **Multi-OS**: Ubuntu, macOS, Windows.

**Impact**:
- **Per-push CI never runs UI tests** → napari integration regressions are not caught until manual workflow.
- **Manual workflow is not triggered on PRs** → UI regressions can merge to main.

**Recommendation**:
1. **Run lightweight UI tests in per-push CI**: Include `test_no_hardware_ui_smoke.py` (already uses offscreen Qt and stubs napari) in ci.yml.
2. **Optional: Add xvfb UI tests to ci.yml**: Use `xvfb-run` (Linux only) to run a subset of UI tests (`test_liveview.py`, `test_without_widgets.py`) on every push. Exclude Windows/macOS if napari is unstable there.
3. **Document why UI is excluded**: Add comment in ci.yml explaining napari plugin collection issues and pointing to imswitch-test.yml for full coverage.

---

### [MED] 5 tests use `inspect.getsource()` for contract validation (brittle)
**Evidence**:
- **Files using source introspection**:
  - `test_flipmirror_component_state.py` (425 lines; asserts `registerFlipMirror` is called in `FlipMirrorController.__init__`).
  - `test_slm_component_state.py` (asserts SLM registration order).
  - `test_shortcuts_leicastand.py` (checks for presence of `_shortcutMap` in source).
  - `test_leicastand_component_state.py` (checks registration sequence).
  - `test_event_gated_target_acquisition.py` (checks event handler registration).

- **Pattern**: Tests parse `inspect.getsource(Controller.__init__)` as a string and grep for method calls (e.g., `assert 'registerFlipMirror' in source`).

**Brittleness**:
- **Breaks on refactoring**: If a developer extracts registration logic into a helper method, the test fails even though behavior is identical.
- **Fragile to formatting**: Adding comments or whitespace can break string-based assertions.
- **Does not test behavior**: Only checks source text, not runtime behavior (e.g., does not verify that `registerFlipMirror` actually registers the correct state).

**Recommendation**:
1. **Replace with behavior tests**: Instead of grepping source, test the side effect:
   ```python
   # BAD (brittle):
   assert 'registerFlipMirror' in inspect.getsource(FlipMirrorController.__init__)

   # GOOD (behavioral):
   ctrl = FlipMirrorController(...)
   assert 'FlipMirror' in ctrl._widget.getState()  # Verify registration happened
   ```
2. **Use mock spies**: If you must verify call order, use `unittest.mock.Mock` with `assert_called_once` instead of grepping source.
3. **Document why contract exists**: If source-level contracts are intentional (e.g., to prevent accidental removal of safety-critical registration), add a comment explaining the rationale and the refactoring constraint.

---

### [MED] No-hardware profile works but requires conftest stubs; fragile to import order
**Evidence**:
- **Root conftest** (`conftest.py:12-46`) stubs `napari`, `vispy`, `matplotlib` with `MagicMock()` if import fails.
- **UI smoke test** (`test_no_hardware_ui_smoke.py:20-253`) replaces stubs with custom fake `napari.Viewer` and `vispy` classes to enable shallow UI construction.
- **No hardware leakage found**: `grep -r "serial.Serial\|/dev/tty\|COM\d" imswitch/imcontrol/_test/unit/*.py` only finds string literals in config builders (e.g., `port='/dev/ttyACM0'` in `test_pulse_generator_integration.py:3`), never actual port opens.

**Fragility**:
- **Import order matters**: If a test imports `napari` before `conftest.py` runs, the MagicMock stub is not installed and the test fails with `ModuleNotFoundError`.
- **Custom fakes are incomplete**: The 233-line fake napari in `test_no_hardware_ui_smoke.py` only implements a subset of the API. If ImControl starts using new napari features (e.g., `viewer.layers.events.inserted`), the fake must be extended.

**Recommendation**:
1. **Make stubs more robust**: Replace `MagicMock()` with minimal real stubs (like `test_no_hardware_ui_smoke.py` does) in `conftest.py`, so import order does not matter.
2. **Alternatively, use pytest-lazy-fixture**: Ensure stubs are installed before any test collection (current approach is correct, but document the constraint).
3. **Pin napari/pydantic**: Document the known incompatibility (`imswitch-test.yml:53-57` mentions napari 0.4.7 + pydantic issues) and pin compatible versions in `requirements-dev.txt`.

---

### [LOW] Test suite is moderately fast but has some timing dependencies
**Evidence**:
- **Total test code**: 26,098 lines across 98 test files (imswitch/imcontrol/_test).
- **Timing**: CI runs the no-hardware suite in ~3 minutes (ci.yml uses `--timeout=180`; full suite in imswitch-test.yml uses `--timeout=300`).
- **Sleeps are patched**: Tests that interact with hardware-like managers patch out sleeps:
  - `test_leicastand_component_state.py:133`: `ctrl.FLUO_SETTLE_SECONDS = 0.0`
  - `test_leicastand_component_state.py:134`: `ctrl._sleepPumpingEvents = lambda seconds: None`
  - `test_pulsegenerator_base.py`: Uses `time.sleep(0.005)` in simulation (minimal).
- **No `@pytest.mark.slow` found**: No tests are explicitly marked as slow.

**Recommendation**:
- **Current speed is acceptable** (~3min for unit suite). No action needed unless tests become slower.
- **If adding hardware integration tests**: Mark them with `@pytest.mark.hardware` (per pyproject.toml) so they can be excluded from CI.

---

### [LOW] Moderate fixture duplication; no shared conftest for common mocks
**Evidence**:
- **26 unique mock fixtures** across 17 test files (avg 3.1 fixtures per file).
- **10 fixtures** follow pattern `mock_*_manager` or `mock_*_controller` (e.g., `mock_nidaq_manager`, `mock_detectors_manager`).
- **No shared conftest** in `imswitch/imcontrol/_test/` → each test file defines its own mocks.

**Duplication example**:
- `test_recording.py`, `test_scan_lifecycle.py`, `test_beadrec_scan_source.py` all define separate `mock_recording_manager` fixtures with similar structure.

**Recommendation**:
1. **Create `imswitch/imcontrol/_test/conftest.py`**: Extract common fixtures (`mock_nidaq_manager`, `mock_detectors_manager`, `mock_lasers_manager`, `mock_recording_manager`) into a shared conftest.
2. **Use fixture factories**: For manager mocks that need customization, provide a factory fixture:
   ```python
   @pytest.fixture
   def make_mock_recording_manager():
       def _factory(**overrides):
           mock = MagicMock(spec=RecordingManager)
           mock.record = MagicMock()
           for key, val in overrides.items():
               setattr(mock, key, val)
           return mock
       return _factory
   ```
3. **Keep test-specific fixtures local**: Only promote fixtures that are reused in 3+ test files.

---

## Severity table

| Severity | Issue | Impact | Effort to Fix |
|----------|-------|--------|---------------|
| **HIGH** | Collection error in `test_example_setups.py` | 10+ tests uncollectable; CI silently ignores file | **Low** (add missing field to configs) |
| **HIGH** | `BeadRecController` untested (21 commits) | Core feature with no regression protection | **Medium** (create controller test file) |
| **HIGH** | `EventTriggeredBaseController` untested | Smart microscopy base class; safety-critical event orchestration | **Medium** (create base class test) |
| **MED** | 82% of managers untested (66/80) | DAQ/laser/stage safety-critical code has no tests | **High** (80+ test files to create) |
| **MED** | CI only runs no-hardware; UI tests manual-only | napari GUI regressions not caught on per-push CI | **Low** (include ui smoke test in ci.yml) |
| **MED** | 5 tests use `inspect.getsource()` (brittle) | Source introspection breaks on refactoring | **Low** (replace with behavior tests) |
| **MED** | No-hardware stubs fragile to import order | Tests fail if napari imported before conftest runs | **Medium** (replace MagicMock with real stubs) |
| **LOW** | Test suite speed (3 min for unit tests) | None; acceptable speed | N/A |
| **LOW** | Moderate fixture duplication (26 fixtures, 17 files) | Maintenance overhead; slight code duplication | **Low** (extract shared conftest) |

---

## Appendix: Quantified metrics

### Test suite size
- **Total test code**: 26,098 lines (all test files under `imswitch/imcontrol/_test`).
- **Unit tests**: 25,434 lines (96 files under `_test/unit`).
- **UI tests**: 664 lines (2 files under `_test/ui`; `test_example_setups.py` uncollectable).
- **Smoke tests**: 2 top-level files (`test_no_hardware_profile.py`, `test_no_hardware_ui_smoke.py`): 397 lines total.

### Collection results (verified)
```
$ python3 -m pytest --collect-only -q imswitch/imcontrol/_test
968 tests collected, 1 error in 2.16s

$ python3 -m pytest --collect-only -q -p no:napari imswitch/imcontrol/_test
968 tests collected, 1 error in 1.84s  # Same error; -p no:napari does not help
```

### Coverage gaps (exact counts)
- **Controllers**: 50 total (49 `*Controller.py` + 1 `__init__.py`).
  - **Tested**: 13 (26%)
  - **Untested**: 37 (74%)
- **Managers**: 80 total (`find imswitch/imcontrol/model/managers -name "*Manager.py" | wc -l`).
  - **Tested**: 14 (18%)
  - **Untested**: 66 (82%)

### CI workflows
- **ci.yml** (per-push):
  - **Triggers**: `push` to main, `pull_request` to main.
  - **Runs**: `imswitch/test_no_hardware_profile.py`, `imcontrol/_test/unit`, `imcontrol/controller/_test`, `improcess/_test` (excludes UI).
  - **Timeout**: 180s.
  - **Env**: `QT_QPA_PLATFORM=offscreen`, `MPLBACKEND=Agg`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.
- **imswitch-test.yml** (manual):
  - **Triggers**: `workflow_dispatch` (manual only).
  - **Runs**: Full suite (`pytest --pyargs imswitch`) with platform-specific ignores.
  - **Timeout**: 300s.
  - **Ignores**: `test_example_setups.py` (all OS), `test_liveview.py` (Windows only).
  - **Multi-OS**: ubuntu-latest, macos-latest, windows-latest.

### Brittle tests (source introspection)
- **5 test files** use `inspect.getsource()`:
  - `test_flipmirror_component_state.py` (425 lines)
  - `test_slm_component_state.py`
  - `test_shortcuts_leicastand.py`
  - `test_leicastand_component_state.py`
  - `test_event_gated_target_acquisition.py`

### Hardware leakage
- **No vendor SDK imports found** in no-hardware tests (`grep -r "import serial\|import thorlabs\|import hamamatsu" imswitch/imcontrol/_test/unit` → 0 results excluding mocks).
- **No physical port opens** found (`grep -r "serial.Serial\|/dev/tty\|COM\d" imswitch/imcontrol/_test/unit/*.py` → only string literals in config builders).
- **Verdict**: No-hardware profile is clean; no I/O leakage detected.

---

## Conclusions

1. **Collection is functional but fragile**: 968 tests collected, but 1 collection error (`test_example_setups.py`) is worked around in CI via file ignores. Fix requires trivial config schema update.

2. **Coverage is dangerously low for core/safety-critical code**:
   - 74% of controllers untested (37/50).
   - 82% of managers untested (66/80).
   - High-priority gaps: `BeadRecController` (most-changed in 2024), `EventTriggeredBaseController` (smart microscopy base), DAQ/laser/stage managers (safety-critical).

3. **No-hardware profile is well-designed**: `test_no_hardware_profile.py` validates that mock hardware can construct managers without I/O. UI smoke test uses custom fakes to avoid napari dependency. No hardware leakage found in tests.

4. **CI masks the collection error**: Per-push CI excludes UI tests entirely; manual workflow ignores the failing file. The error is documented but not fixed. Full suite is NOT executable without manual `--ignore`.

5. **Brittle contract tests are a maintenance risk**: 5 tests use `inspect.getsource()` to assert on source text (e.g., checking registration order in `__init__`). These break on refactoring and should be replaced with behavioral assertions.

6. **Test structure is reasonable**: 26k lines of test code, moderate fixture duplication (26 fixtures across 17 files), sleeps are patched out. No significant speed issues (3 min for no-hardware suite).

**Overall assessment**: The test suite is **partially trustworthy** for the code it covers, but **dangerously incomplete** for controllers/managers. The no-hardware profile works well, but the collection error and low coverage for safety-critical DAQ/laser code are **high-priority risks**. CI is functional but **dependent on workarounds** that mask the collection failure.
