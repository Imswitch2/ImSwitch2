# Phase 5: Stall Watchdog - Implementation Summary

## Overview
Added a configurable per-detector stall watchdog to RecordingManager that detects when cameras aren't producing frames and aborts the recording cleanly with diagnostic logging.

## Problem Statement
The recording loop could hang indefinitely if a detector failed to produce frames due to:
- Incorrect camera triggering configuration
- Hardware disconnection
- Firmware issues
- Misconfigured `numCamTTL` parameter

This resulted in silent infinite loops with no user feedback.

## Solution
Implemented a timeout-based watchdog that tracks the last time each detector produced a frame and aborts the recording if no progress is detected within a configurable timeout period.

## Changes Made

### Constants Added
```python
FRAME_POLL_INTERVAL = 0.0001  # seconds; prevents UI freezing during acquisition
DEFAULT_STALL_TIMEOUT = 10.0  # seconds; watchdog triggers if no frames arrive within this period
```

### New Signal
- `sigRecordingStalled(str)`: Emitted when watchdog detects a stall, passes detector name

### API Changes
- `startRecording()`: Added optional `stallTimeout` parameter (default: 10 seconds)
  - Only applies to streaming recording modes (SpecFrames, ScanOnce, ScanLapse)
  - Does not apply to snap() operations

### Recording Loop Changes
1. **Timestamp Tracking**: Initialize `lastFrameTime` dict at recording start
2. **Frame Receipt**: Update timestamp whenever frames are successfully retrieved
3. **Stall Detection**: Check for timeout violations in each loop iteration
4. **Clean Abort**: Log detailed diagnostics and emit stall signal, then exit loop

### Diagnostic Logging
When stall detected, logs:
- Detector name
- Elapsed time since last frame
- Configured timeout
- Current frame count vs. expected count
- Actionable advice ("Check camera triggering and numCamTTL configuration")

## Testing
Added `test_recording_stall_watchdog()` that:
- Mocks detector to return zero frames (simulates camera stall)
- Uses short timeout (0.5s) for fast test execution
- Verifies signal emission within expected time window
- Confirms error logging with correct message
- Validates recording terminates cleanly (no thread hang)

**Test Result**: ✅ PASSED (4/9 total tests passing, 5 pre-existing failures)

## Safety & Backward Compatibility
- **No breaking changes**: New parameter is optional with sensible default
- **No hardware timing changes**: Only adds timeout check, no DAQ/trigger modifications
- **Graceful degradation**: Watchdog only triggers on genuine stalls, normal recordings unaffected
- **Thread-safe**: Uses existing thread coordination mechanisms

## Usage Example
```python
# Use default 10s timeout
recordingManager.startRecording(
    detectorNames=['CAM'],
    recMode=RecMode.SpecFrames,
    recFrames=1000,
    ...
)

# Use custom timeout for fast-triggering systems
recordingManager.startRecording(
    detectorNames=['CAM'],
    recMode=RecMode.SpecFrames,
    recFrames=1000,
    stallTimeout=2.0,  # Abort after 2s without frames
    ...
)

# Connect to stall signal for custom handling
recordingManager.sigRecordingStalled.connect(lambda name: print(f"Camera {name} stalled!"))
```

## Validation
- ✅ Syntax: Python AST parsing passed
- ✅ Linting: `ruff check` passed
- ✅ Unit tests: New test passes, existing tests unaffected
- ✅ Integration: Stall detection confirmed via test output logs

## Files Modified
1. `imswitch/imcontrol/model/managers/RecordingManager.py`
   - Added constants (lines 22-24)
   - Added `sigRecordingStalled` signal (line 593)
   - Updated `startRecording()` signature and docstring (lines 629-642)
   - Initialized stall watchdog timestamps (line 843)
   - Updated timestamp on frame receipt (line 921)
   - Added stall detection logic (lines 935-954)
   - Replaced bare `sleep()` with named constant (line 960)

2. `imswitch/imcontrol/_test/unit/test_recording.py`
   - Added `test_recording_stall_watchdog()` (lines 277-343)
   - Uses monkeypatch to mock detector's getChunk method
   - Validates signal emission, error logging, and timing

## Next Steps (Future Work)
- Consider adding stall detection to SpecTime mode (currently only SpecFrames/Scan modes)
- Optionally emit progress warnings before full timeout (e.g., at 50% of timeout)
- Add per-detector timeout configuration in setup JSON
- Expose watchdog status in GUI (e.g., progress bar color change on slow acquisition)

## Commit Details
**Commit Message**: "Add stall watchdog to RecordingManager for camera freeze detection"
**Files**: RecordingManager.py, test_recording.py
**Status**: Ready to commit

---
**Phase 5 Status**: ✅ COMPLETE
**Date**: 2026-05-22
**Committed by**: AI Agent (OpenHands)
