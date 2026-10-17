# Point-scan confocal / STED performance (open tail)

**Status (2026-10-17):** F1 (Repeat crash: re-arm now deferred via
`SuperScanController._armRepeatScan`, `QTimer.singleShot(0)`) and F2 (live-preview
redraw throttled by wall-clock, `test_live_display_throttle.py`,
`test_scan_repeat_rearm.py`) are fixed on main. The opportunities below are
**unimplemented** (verified: `d2Step` still emits per scan line from
`APDManager`/`PMTManager`; `_TTFlimWorker.LIVE_PREVIEW_S = 1.0` is a class
constant; `getLatestFrame` default unchanged). They are performance work that
needs a rig to judge, so each wants hardware numbers before and after.

Scope: the galvo point-scan path (`ScanControllerAdvanced`, `GalvoScanDesigner`,
`AdvancedScanTTLCycleDesigner`, `APDManager`/`PMTManager`, Swabian TimeTagger for
FLIM/STED, via `NidaqManager`).

---

## Remaining opportunities (not yet implemented — need rig validation)

### O1 — Per-line cross-thread event flooding
Even with F2, `d2Step` still posts one queued event per scan line to the GUI
thread. At high line rates this floods the event loop with buffer-write slots.
Options: batch N lines per emit, or write into a shared preallocated buffer from
the worker and let the throttled timer pull it (needs a lock / double-buffer).
Bigger change; validate on hardware.

### O2 — `getLatestFrame(is_save=True)` for the live view
`DetectorManager.updateLatestFrame` calls `getLatestFrame()` with the default
`is_save=True`, so the periodic live path emits the **raw stack** buffer, not
the 2D display frame. Harmless for plain 2D, wasteful for Z-stacks/linesteps.
Consider a dedicated `getDisplayFrame()` for the LV path.

### O3 — TimeTagger 1 s live-preview interval (`_TTFlimWorker.LIVE_PREVIEW_S`)
The "compromise" for FLIM/STED: the worker polls `Flim.getCurrentFrame()` once
per second. Fine for a final image, coarse for live STED tuning. Make it a
config knob and/or shorten it; the worker already wakes immediately on
`sigScanDone`, so only the *live* cadence is affected.

### O4 — Galvo signal rebuild cost
`GalvoScanDesigner.make_signal` is the heaviest per-scan step. Repeat already
skips it via the `_lastBuiltParams` cache in `ScanControllerAdvanced`; confirm
the cache key covers every live-editable parameter so first-frame latency
after a parameter change is the only rebuild.

### O5 — TriggerScope controllers
`TriggerScopeRasterController` (and siblings) share the same synchronous
repeat re-arm pattern on a *different* (firmware/serial) backend with
multi-controller scan-done fan-out. Left unchanged here to avoid perturbing
that backend; worth the same deferral in a focused follow-up.

---

## Tests
- `test_live_display_throttle.py` — 7 tests: first-fire, suppression window,
  interval measured from last fire, reset, bounded rate over 1000 fast calls.
- `test_scan_repeat_rearm.py` — 6 tests: deferral instead of sync run, deferred
  callback fires the frame, abort/un-check/already-running cancel the pending
  frame, continuous-mode override keeps looping.

Full scan/detector/galvo/triggerscope/monalisa/dtype unit suites: 293 passed.
