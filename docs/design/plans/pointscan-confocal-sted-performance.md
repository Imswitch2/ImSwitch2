# Point-scan confocal / STED performance

Branch: `perf/pointscan-confocal-sted` (off `main`).

Scope: the galvo point-scan path — `ScanControllerAdvanced` +
`GalvoScanDesigner` + `AdvancedScanTTLCycleDesigner`, with `APDManager` /
`PMTManager` (and `SwabianTimeTaggerManager` for FLIM/STED) as detectors,
driven through `NidaqManager`.

Reported symptoms:
1. The whole GUI gets laggy during/after scanning.
2. It **crashes** when the *Repeat* checkbox is used.
3. Live image updating is not fluent (choppy / lagging behind acquisition).

---

## Root-cause findings

### F1 — Repeat re-arms the scan re-entrantly (the crash) — FIXED

`NidaqManager.scanDone()` emits `sigScanDone` from the just-finished scan's
task-completion slot (`taskDone` → `scanDone`, same thread). Every scan
controller's `scanDone()` is wired to that signal, and in the *Repeat* branch
it called the next frame **synchronously**:

```python
def scanDone(self):
    self.isRunning = False
    if not self._widget.repeatEnabled():
        ...                       # terminal path
    else:
        self.runScanAdvanced(sigScanStartingEmitted=True)   # re-enters runScan
```

`runScanAdvanced` → `nidaqManager.runScan()` then recreates the NI-DAQ tasks
and **reassigns `self.aoTaskWaiter` / `self.doTaskWaiter` / `self.timerTaskWaiter`**
(`NidaqManager.runScan`, ~line 416) while the *previous* `WaitThread`s are still
finishing their `run()`/`close()`. Dropping the last reference to a `QThread`
that is still running triggers *"QThread: Destroyed while thread is still
running"* → abort/segfault. The per-detector `ScanWorker` QThreads
(`APDManager`/`PMTManager.initiateScan`) are likewise torn down and recreated in
the same re-entrant window, and the old worker's queued
`stopAcquisitionLocal` can land on the *new* thread. With *Repeat* this happens
every frame, so it crashes quickly. (In NI-DAQ **simulation** mode the
`WaitThread`s are never created, which is why it only bites on real hardware.)

**Fix:** `SuperScanController._armRepeatScan()` defers the re-arm with
`QtCore.QTimer.singleShot(0, ...)` so the current signal chain unwinds and the
finished scan releases its tasks/threads before the next frame arms.
`_repeatPending` is cleared by `abortScan`/`scanFailed`, and `_fireRepeatScan`
re-checks `_shouldContinueRepeat()` so aborting or un-checking *Repeat* in the
gap cancels cleanly. Applied to `ScanControllerAdvanced`,
`ScanControllerPointScan`, `ScanControllerBase` and `ScanControllerMoNaLISA`
(the NI-DAQ-backed controllers; the last two override `_shouldContinueRepeat`
to keep looping in continuous-laser mode).

### F2 — Live-preview redraws gated on image *shape* with a random draw — FIXED

`APDManager.updateImage` / `PMTManager.updateImage` run once **per scan line**
(cross-thread `d2Step` → GUI thread) and pushed the whole in-progress image to
napari on a random subset of lines:

```python
if np.random.rand() < min(500 / sum(self._image.shape), 0.05):
    self.sigImageUpdated.emit(self._image, True, self.scale)
```

The gate depends on the array shape, not on wall-clock time, so the redraw rate
is uncontrolled and non-deterministic: a fast/small scan floods the GUI with
full-image redraws (lag), a slow/large scan barely refreshes (choppy). It also
competes with the periodic `LVWorker` timer that *already* refreshes the view.

**Fix:** `LiveDisplayThrottle` (`detectors/_live_display.py`) — a deterministic
wall-clock limiter. Redraws are capped at a fixed rate (default 50 ms / 20 Hz,
tunable per rig via the `liveUpdateIntervalMs` manager property) and reset at
the start of each scan so the first line always paints. Bounds napari redraws
regardless of line rate or image size.

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
