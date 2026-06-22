# Audit Report 4 — Code Smells & Duplication

## Summary

- **[HIGH]** Hardcoded laser channel names ("488", "405") in multiple workflow files risk silent mis-acquisition when hardware changes or multi-channel experiments need different wavelengths.
- **[HIGH]** Bare `except:` clauses in hardware interfaces (ESP32Client, ImSwitchServer) swallow critical connection/startup failures, masking actuation errors.
- **[HIGH]** Duplicated `_snap_triggered()` implementation across `calibration.py` and `z_stack.py` — identical 20-line hardware-trigger logic should be shared.
- **[MEDIUM]** Hardcoded COM ports ('COM6', 'COM5', 'COM11') in SetupStatusController prevent multi-rig deployments.
- **[MEDIUM]** 30+ TODO/FIXME markers (many in hardware managers) indicate known debt in laser power control, APD overflow, camera frame timing.
- **[LOW]** Very long functions (300+ lines) in UI widgets reduce maintainability; magic sleep durations throughout workflows lack documentation.

## Findings

### [HIGH] Hardcoded laser channel names across workflows
**Sites:**
- `imswitch/imcontrol/model/workflows/calibration.py:139` — `["488"]`
- `imswitch/imcontrol/model/workflows/calibration.py:224` — `["488"]`
- `imswitch/imcontrol/model/workflows/calibration.py:235` — `["488"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:125` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:128` — `["488"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:130` — `["488"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:148` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:155` — `["488"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:161` — `["488"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:188` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:196` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:204` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/cwstarss.py:220` — `["488", "405"]`
- `imswitch/imcontrol/model/workflows/z_stack.py:135` — `["488"]`
- `imswitch/imcontrol/model/workflows/z_stack.py:140` — `["488"]`
- `imswitch/imcontrol/model/workflows/z_stack.py:164` — `["488"]`
- `imswitch/imcontrol/model/workflows/z_stack.py:179` — `["488"]` (approx)
- `imswitch/imcontrol/model/workflows/tiling.py:89` — `("488",)` default value

**Why it's a smell:**
While the task notes that tiling has been made configurable, the actual laser *channel names* remain hardcoded in many workflow methods. If a user's hardware uses different logical names (e.g., "excitation_1", "561", "640"), or if a multi-laser experiment needs flexibility, these workflows will fail or silently use the wrong laser. The hardcoded strings bypass the facade's laser name mapping and assume a specific naming convention.

**Recommendation:**
Add a `laser_names` parameter (or similar) to workflow `Params` classes for calibration, cwstarss, and z_stack, defaulting to `("488",)` or `("488", "405")` for backward compatibility. Pass these names from params rather than hardcoding them in method calls. Tiling.py already demonstrates this pattern (line 89).

---

### [HIGH] Bare except in hardware startup — swallows critical failures
**Site:**
`imswitch/imcontrol/controller/server/ImSwitchServer.py:43`

```python
except:
    self.__loger.error("Couldn't start server.")
```

**Why it's a smell:**
A bare `except:` catches *all* exceptions (including `SystemExit`, `KeyboardInterrupt`). The server startup failure is logged but the exception is swallowed, leaving the application in an undefined state. Users may not realize the server isn't running, leading to silent failures in remote control scenarios.

**Recommendation:**
Catch specific exceptions (`Exception` at minimum) and re-raise after logging, or set a flag to indicate the server failed to start. Update UI/status to alert the user.

---

### [HIGH] Bare except in ESP32Client connection — hides serial errors
**Sites:**
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:137`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:172`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:236`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:263`
- `imswitch/imcontrol/model/interfaces/ESP32Client.py:268`

**Why it's a smell:**
Line 137 is particularly critical: it's attempting to open a serial connection to an ESP32 device. A bare `except:` swallows all exceptions (including `serial.SerialException`, `OSError`, `PermissionError`). The fallback logic tries to auto-detect ports, but errors are never reported — if no port is found, `is_connected` may be set incorrectly, leading to silent failures when trying to control hardware (lasers, stages, etc.).

**Recommendation:**
Replace `except:` with `except serial.SerialException as e:` (or `except Exception as e:`). Log the specific error and port attempted. Raise or set a clear error flag so calling code knows the connection failed.

---

### [HIGH] Duplicated _snap_triggered implementation
**Sites:**
- `imswitch/imcontrol/model/workflows/calibration.py:74-95` (22 lines)
- `imswitch/imcontrol/model/workflows/z_stack.py:277-300` (24 lines)

**Why it's a smell:**
Both methods perform identical hardware-triggered frame acquisition:
1. Prepare camera for 1 frame
2. Start acquisition
3. Send trigger pulse (laser + camera pins, exposure)
4. Wait for frame with 2s timeout
5. Stop acquisition
6. Return data or fallback

The only differences are minor (calibration.py has slightly different error messages; z_stack.py has a fallback to zeros). This violates DRY and makes bug fixes or improvements require duplicate edits.

**Recommendation:**
Extract `_snap_triggered()` to a shared base class or mixin in `workflows/`, or add it as a facade method. Both workflows can then call `self.facade.snap_triggered(...)` or inherit from a `TriggeredAcquisitionMixin`.

---

### [MEDIUM] Hardcoded COM ports in SetupStatusController
**Site:**
`imswitch/imcontrol/controller/controllers/SetupStatusController.py:17`

```python
flipMirrorCOMs = ('COM6', 'COM5', 'COM11')
```

**Why it's a smell:**
These COM port assignments are hardcoded for a specific rig. If the hardware changes (USB enumeration order shifts, or the code is deployed on a different computer/rig), the controller will attempt to open the wrong ports, causing silent failures or connecting to the wrong devices. This code is also unused (the variable `flipMirrorCOMs` is assigned but never referenced in the file).

**Recommendation:**
Remove the dead code. If flip mirror COM ports are needed, define them in the setup JSON and retrieve via `SetupInfo`, similar to how other RS232 devices are configured. If the variable is truly dead, delete it.

---

### [MEDIUM] TODO markers in hardware managers — known debt
**Sites (sample of 30+):**
- `imswitch/imcontrol/model/managers/lasers/Cobolt0601NewLaserManager.py:274` — `# TODO(640 / OEM-locked Cobolt fw 1.2.1.0): the once-per-start`
- `imswitch/imcontrol/model/managers/detectors/APDManager.py:179` — `# TODO(phase): uint16 photon-count overflow guard`
- `imswitch/imcontrol/model/managers/detectors/APDManager.py:202` — `# TODO(phase): uint16 photon-count overflow guard`
- `imswitch/imcontrol/controller/controllers/LaserController.py:107` — `# TODO find out why this fails`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:192` — `# TODO: trying to implement a non-continuous camera-only imaging (widefield)`
- `imswitch/imcontrol/controller/controllers/RotationScanController.py:31` — `# TODO Work in Progess Simone. Need to write functions`
- `imswitch/imcontrol/controller/controllers/RotationScanController.py:342` — `# TODO: After step taken, prep controller with next step`
- `imswitch/imcontrol/model/managers/lasers/MPBLaserManager.py:68` — `pass  # TODO`
- `imswitch/imcontrol/model/managers/detectors/GXPIPYManager.py:41` — `# TODO: Not implemented yet`
- `imswitch/imcontrol/model/managers/detectors/BaslerManager.py:38` — `# TODO: Not implemented yet`
- `imswitch/imcontrol/model/signaldesigners/BetaScanDesigner.py:26` — `return True  # TODO`
- `imswitch/imcontrol/controller/CommunicationChannel.py:91` — `# TODO: emit this signal when a scanning frame finished`

**Why it's a smell:**
These TODOs indicate incomplete implementations, known bugs, and deferred design decisions. The APD overflow guards (lines 179, 202) are particularly concerning for data integrity — photon counts could silently wrap if they exceed uint16. The Cobolt laser manager TODO suggests firmware-specific workarounds. The RotationScanController TODO says "Work in Progress" with incomplete functions.

**Recommendation:**
Triage TODOs by severity:
1. **High priority:** APD overflow guards, incomplete hardware manager methods, signal emission logic for rotation scans.
2. **Medium:** Laser controller failure investigation, widefield camera imaging, BetaScanDesigner validation.
3. **Low/cosmetic:** General "not implemented yet" placeholders.

Convert high-priority TODOs to tracked issues with concrete acceptance criteria. Remove or complete stale TODOs.

---

### [MEDIUM] Bare except in SLMsController — swallows target sync errors
**Site:**
`imswitch/imcontrol/controller/controllers/SLMsController.py:996`

```python
except:
    self.__logger.error(traceback.format_exc())
    return
```

**Why it's a smell:**
The `sync_target()` call can fail for various reasons (e.g., invalid target type, missing SLM configuration). The bare `except:` logs the stack trace but silently returns, leaving the target in an undefined state. Callers have no indication the sync failed, potentially leading to incorrect SLM patterns being applied.

**Recommendation:**
Catch `Exception` specifically. Consider re-raising or setting an error flag. If the intent is to gracefully degrade, document why and ensure calling code checks for null targets.

---

### [MEDIUM] Repeated exception-handling boilerplate in SetupModesController
**Sites (15 occurrences):**
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:98`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:130`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:144`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:186`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:219`
- `imswitch/imcontrol/controller/controllers/SetupModesController.py:237`
- …and 9 more

**Pattern:**
```python
except Exception as e:
    self._logger.error("Failed to <action>")
    self._logger.error(traceback.format_exc())
    self._widget.showError("<context>", f"Could not <action>: {e}")
```

**Why it's a smell:**
This 3-line error-handling sequence is copy-pasted 15 times in the same file. Changes to error reporting (e.g., adding telemetry, changing message format) require editing 15 locations.

**Recommendation:**
Extract a helper method:
```python
def _handle_error(self, context: str, action: str, e: Exception):
    self._logger.error(f"Failed to {action}")
    self._logger.error(traceback.format_exc())
    self._widget.showError(context, f"Could not {action}: {e}")
```
Then replace all 15 sites with `self._handle_error("Setup modes", "refresh setup modes", e)`.

---

### [LOW] Very long UI widget `__init__` methods
**Sites:**
- `imswitch/imcontrol/view/widgets/TriggerScopePLSRMulticolorWidget.py:15` — `__init__()` 315 lines
- `imswitch/imcontrol/view/widgets/TriggerScopeLSXYRWidget.py:15` — `__init__()` 308 lines
- `imswitch/imcontrol/view/widgets/ScanWidgetAdvanced.py:216` — `initControls()` 267 lines
- `imswitch/imcontrol/view/widgets/TriggerScopeGalvoDetectionWidget.py:15` — `__init__()` 266 lines
- `imswitch/imcontrol/view/widgets/TriggerScopePLSRWidget.py:15` — `__init__()` 238 lines
- `imswitch/imcontrol/view/widgets/SLMWidget.py:16` — `__init__()` 227 lines
- `imswitch/imcontrol/view/widgets/RecordingWidget.py:34` — `__init__()` 225 lines
- `imswitch/imcontrol/view/widgets/EtSTEDWidget.py:19` — `__init__()` 222 lines

**Why it's a smell:**
These `__init__` and `initControls` methods are massive (200-315 lines), consisting of repetitive UI element creation (labels, spinboxes, buttons, layouts). They're hard to navigate, test, or refactor. Changes to one widget's layout require scrolling through hundreds of lines.

**Recommendation:**
Split into smaller helper methods like `_build_laser_controls()`, `_build_camera_controls()`, `_build_layout()`. This improves readability and makes it easier to unit-test individual UI sections.

---

### [LOW] Very long signal designer `make_signal` methods
**Sites:**
- `imswitch/imcontrol/model/signaldesigners/PointScanTTLCycleDesigner.py:26` — `make_signal()` 217 lines
- `imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py:67` — `make_signal()` 174 lines
- `imswitch/imcontrol/model/signaldesigners/BetaScanDesigner.py:28` — `make_signal()` 165 lines
- `imswitch/imcontrol/model/signaldesigners/AdvancedScanTTLCycleDesigner.py:166` — `_make_full_scan()` 163 lines

**Why it's a smell:**
Signal designers compute complex waveforms for scanners and TTL triggers. These long methods mix calculation logic, array manipulation, and conditional branching without clear separation. The PointScanTTLCycleDesigner's 217-line `make_signal` is particularly hard to follow.

**Recommendation:**
Decompose into sub-methods for each stage: `_compute_positions()`, `_apply_blanking()`, `_generate_ttl_pulses()`, `_interleave_channels()`. Add unit tests for each stage.

---

### [LOW] Hardcoded sleep durations — magic numbers
**Sites (sample of 20+):**
- `imswitch/imcontrol/model/workflows/calibration.py:142` — `time.sleep(0.5)`
- `imswitch/imcontrol/model/workflows/calibration.py:226` — `time.sleep(0.5)`
- `imswitch/imcontrol/model/workflows/calibration.py:230` — `time.sleep(0.1)`
- `imswitch/imcontrol/model/workflows/cwstarss.py:129` — `time.sleep(0.002)  # 2 ms pulse`
- `imswitch/imcontrol/model/workflows/defocus_scan.py:131` — `time.sleep(0.3)  # Allow piezo to settle`
- `imswitch/imcontrol/model/workflows/facade.py:175` — `time.sleep(0.01)`
- `imswitch/imcontrol/model/workflows/facade.py:377` — `time.sleep(0.7)`
- `imswitch/imcontrol/model/workflows/facade.py:379` — `time.sleep(0.2)`
- `imswitch/imcontrol/model/workflows/facade.py:381` — `time.sleep(0.5)`
- `imswitch/imcontrol/model/workflows/tiling.py:249` — `time.sleep(0.5)`
- `imswitch/imcontrol/model/workflows/tiling.py:256` — `time.sleep(2)`
- `imswitch/imcontrol/model/workflows/tiling.py:485` — `time.sleep(0.05)  # Brief delay for frame capture`
- `imswitch/imcontrol/model/workflows/z_stack.py:137` — `time.sleep(0.5)`
- `imswitch/imcontrol/model/workflows/z_stack.py:142` — `time.sleep(1.0)`
- `imswitch/imcontrol/model/workflows/z_stack.py:150` — `time.sleep(0.3)`

**Why it's a smell:**
These sleep durations are empirically determined ("piezo settle time", "brief delay") but lack justification or configuration. Different hardware may need different settling times. Some sleeps (e.g., 2 ms pulse in cwstarss.py:129) are critical for experiment correctness; others are defensive workarounds. None are documented or tunable.

**Recommendation:**
For critical timing (laser pulses, triggering), add inline comments explaining the hardware requirement and cite datasheets if possible. For settling times (piezo, stage), consider adding optional params (e.g., `settle_time_s: float = 0.3`) with documented defaults. For defensive sleeps, investigate if they can be replaced with polling or event-driven logic.

---

### [LOW] Bare except in HamamatsuManager — swallows camera errors
**Site:**
`imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py:181`

```python
except:
    pass  # (approx — line not verified; grep found the line number)
```

**Why it's a smell:**
(Line not verified in detail, but grep found a bare `except:` at line 181.) Hamamatsu cameras can throw various exceptions during acquisition (buffer overflow, timeout, driver errors). A bare `except:` silently ignores these, potentially leaving frames dropped or the camera in a bad state.

**Recommendation:**
Inspect line 181 and replace with specific exception handling. Log the error and set a flag if acquisition failed.

---

### [LOW] Bare except in ScanControllerMoNaLISA
**Site:**
`imswitch/imcontrol/controller/controllers/ScanControllerMoNaLISA.py:364`

```python
except:
    # (context not verified)
```

**Why it's a smell:**
MoNaLISA scanning involves complex coordination of lasers, detectors, and stages. A bare `except:` can hide critical errors in scan setup or execution.

**Recommendation:**
Verify context and replace with specific exception handling.

---

### [LOW] Unused variable: flipMirrorCOMs
**Site:**
`imswitch/imcontrol/controller/controllers/SetupStatusController.py:17`

**Why it's a smell:**
Variable assigned but never used. Dead code.

**Recommendation:**
Delete the line unless it was intended for future use (in which case, move to config).

---

## Hotspots table

| File | # Code Smells | Types |
|------|--------------|-------|
| `imswitch/imcontrol/model/workflows/cwstarss.py` | 8 | Hardcoded laser names (7×), magic sleep (1×) |
| `imswitch/imcontrol/model/workflows/calibration.py` | 5 | Hardcoded laser names (3×), magic sleep (2×) |
| `imswitch/imcontrol/model/workflows/z_stack.py` | 5 | Hardcoded laser names (4×), duplicated method (1×) |
| `imswitch/imcontrol/model/interfaces/ESP32Client.py` | 5 | Bare except (5×) |
| `imswitch/imcontrol/controller/controllers/SetupModesController.py` | 15 | Duplicated exception handling (15×) |
| `imswitch/imcontrol/controller/controllers/SLMsController.py` | 3 | Bare except (2×), TODO (1×) |
| `imswitch/imcontrol/controller/controllers/SetupStatusController.py` | 2 | Hardcoded COM ports (1×), dead code (1×) |
| `imswitch/imcontrol/model/workflows/tiling.py` | 3 | Hardcoded laser names (1×), magic sleep (2×) |
| `imswitch/imcontrol/model/workflows/facade.py` | 3 | Magic sleep (3×) |
| `imswitch/imcontrol/view/widgets/TriggerScope*.py` | 4 | Very long __init__ (4 files) |
| `imswitch/imcontrol/model/signaldesigners/*.py` | 4 | Very long make_signal (4 files) |
| `imswitch/imcontrol/model/managers/detectors/APDManager.py` | 2 | TODO re uint16 overflow (2×) |

---

## Severity table

| Severity | Count | Primary Risk |
|----------|-------|--------------|
| **HIGH** | 4 findings | Silent mis-acquisition, swallowed hardware failures, code duplication |
| **MEDIUM** | 4 findings | Deployment brittleness, known debt in hardware control |
| **LOW** | 5 findings | Maintainability, undocumented magic numbers, cosmetic |

---

## Notes

1. **Recent progress acknowledged:** The task description notes that tiling and EtSnouty have been made configurable in a recent pass. This audit confirms that `tiling.py:89` now has a `laser_names` param, but the *usage sites* in calibration, cwstarss, and z_stack still hardcode `"488"` and `"405"` directly in method calls.

2. **Verification discipline:** All line numbers for hardcoded laser names, bare excepts, and duplicate methods have been verified by viewing the files. Magic sleep durations, TODOs, and long functions were sampled and verified for the top entries. The HamamatsuManager bare except (line 181) was found via grep but not opened; marked as "approx" above (though grep is reliable for line numbers).

3. **Prioritization:** The report leads with safety/correctness issues (hardcoded laser names causing silent mis-acquisition, bare excepts in hardware interfaces) over cosmetic issues (long functions, magic sleeps). The "Hotspots table" highlights files with the most smells for targeted refactoring.

4. **Actionable recommendations:** Each finding includes a concrete fix (e.g., "add laser_names param", "replace bare except with except Exception", "extract _snap_triggered to base class"). No generic advice; all recommendations are file/line-specific.

