# Scan lifecycle and the active scan source

How ImSwitch tracks *which controller is running a scan*, and how consumers
such as BeadRec resolve scan geometry and running-state from it.

Status: implemented (June 2026). Supersedes the capability-lookup-only design
described in "Rework 1" of [beadrec_audit_plan.md](beadrec_audit_plan.md).

Extended July 2026 with the **scan-execution coordinator** (§2.4): scan *runs*
and *iterations* are now reserved through a shared coordinator that owns the
participant snapshot, the SCAN detector lease and the end-of-scan barrier. The
active-scan-source mechanism below is unchanged; the coordinator sits under it
and decides *whether* an arm is allowed and *when* completion is published.
Design record: [design/plans/detector-acquisition-selection.md](design/plans/detector-acquisition-selection.md).

---

## 1. The problem this solves

Several widgets need to know, at runtime, which controller is currently
running a hardware scan:

- `BeadRecController` polls `isScanRunning()` from its worker thread and reads
  scan dimensions / step sizes / frames-per-pixel on every scan start.
- Future consumers (recording orchestration, display pipelines) have the same
  question.

Historically `CommunicationChannel` answered by **guesswork**:

1. Try the controller registered under the hardcoded widget key `'Scan'`
   (only the NIDAQ family registers there).
2. Else iterate *all* registered controllers and return the **first** one
   implementing the `BeadRecScanSource` protocol.

Two failure modes:

- **Silent death for new scanners.** A scan controller registered under its
  own widget key (every TriggerScope controller) was invisible to step 1; if
  it also lacked the protocol, BeadRec went silently inactive.
- **Wrong source with multiple implementers.** Step 2 returns whichever
  compatible controller happens to come first in the registration dict —
  regardless of which one is actually scanning. Registration order decided
  correctness.

The root cause: the channel tried to *pull* a fact by iteration that is known
with certainty in exactly one place — the controller that started the scan.
The fix is to *push* it.

## 2. Architecture

### 2.1 `ScanLifecycleMixin` (controller/basecontrollers.py)

Every scan controller in the codebase already maintains a `self.isRunning`
flag: set `True` at the top of `runScanAdvanced()`, set `False` in
`scanDone()`, `scanFailed()` and exception handlers. The mixin turns that
flag into a property whose setter announces the transition to the channel:

```python
class ScanLifecycleMixin:
    _isRunningFlag = False

    @property
    def isRunning(self) -> bool:
        return self._isRunningFlag

    @isRunning.setter
    def isRunning(self, value: bool) -> None:
        self._isRunningFlag = bool(value)
        if self._isRunningFlag:
            self._commChannel.setActiveScanSource(self)
        else:
            self._commChannel.clearActiveScanSource(self)
```

**Why a property instead of explicit announce calls:** the audit found ~30
assignment sites across 11 controllers, including inside `except` blocks. The
property absorbs every existing and future assignment, so no site can be
forgotten — a controller cannot start a scan "secretly" as long as it
maintains `isRunning`, which all of them already do.

**Why this is safe at construction time:** `ImConWidgetController.__init__`
assigns `self._commChannel` before any subclass `__init__` body runs, so the
first `self.isRunning = False` in a controller's constructor finds the
channel. That initial `False` is a no-op clear (see identity guard below).

### 2.2 `CommunicationChannel` active-source API

```python
setActiveScanSource(controller)    # called on isRunning -> True; last wins
clearActiveScanSource(controller)  # called on isRunning -> False; identity-guarded
getActiveScanSource()              # the running controller, or None
isScanRunning()                    # _activeScanSource is not None
```

The **identity guard** in `clearActiveScanSource` only clears when the caller
*is* the active source. This protects against two real situations:

- A controller constructed while another controller is scanning initializes
  `isRunning = False` — must not evict the running scan.
- Controller A's scan is superseded by controller B (last-announce-wins);
  when A later flips its flag to False, B's announcement survives.

These are plain methods, not Qt signals — no change to the channel's signal
inventory, and reads from the BeadRec worker thread are single-reference
loads (atomic enough under CPython, same guarantees as the previous
`isRunning` attribute read).

### 2.3 Metadata resolution order

`getDimsScan()`, `getScanStepSizes()`, `getNumLineSteps()`,
`getFramesPerScanPixel()` and `getBeadRecScanSource()` now resolve:

1. **Active scan source** — if it satisfies the `BeadRecScanSource` protocol
   (see `controllers/_beadrec_scan_source.py`) and reports
   `isBeadRecCompatible() == True`.
2. **`'Scan'` widget key** — legacy path; covers `ScanControllerAdvanced` /
   `ScanControllerMoNaLISA`, which expose legacy accessors (`getDimsScan`
   etc.) but not the protocol.
3. **First-compatible iteration** (`getBeadRecScanSource`) — idle fallback
   only, e.g. when the user presses BeadRec *Run* before starting a scan.

BeadRec reads its parameters on `sigScanStarted`, which always fires *after*
the controller set `isRunning = True` (see the sequence below). During a scan
the answer therefore always comes from the controller actually scanning; the
iterate-and-guess path only ever serves idle reads.

Unchanged and out of scope: `getNumScanPositions()`, `getNumCamTTL()`,
`getNextAxial()` stay on the `'Scan'` key (consumed by RecordingController
and the MoNaLISA axial workflow only).

### 2.4 `ScanExecutionCoordinator` (model/managers/_scan_execution.py)

`isRunning` answers *who is scanning*. It does not answer *may this scan
start*, *which detectors belong to it* or *when is it safe to tear down*.
Those live in a single shared, framework-free coordinator
(`getSharedScanExecutionCoordinator`, created by `MasterController`), because
there are **five** direct `nidaqManager.runScan` callers — the four
`SuperScanController` subclasses and `EtSTEDTriggeredScanRunner` — so the
lifecycle cannot live on `SuperScanController` without missing one. Today
exactly one call site reaches `runScan`, inside the coordinator.

Two deliberately distinct levels:

| Level | Token | Spans |
|---|---|---|
| **Scan run** | `ScanRunToken` (`reserveRun` / `releaseRun`) | one user-initiated scan *including* every repeat iteration, MoNaLISA `autoAxial` follow-up and `isNonFinalPartOfSequence` part |
| **Scan iteration** | `ScanIterationToken` (`arm` / `resolve`) | one `runScan` → `scanDone` cycle, i.e. one frame |

The run reservation is what stops another entry point from arming *in the gaps
between* repeat frames, where no iteration is in flight. Re-reserving by the
same owner is idempotent; a different owner is refused with `ScanBusyError`
(`NidaqManagerError` subclass) — a refusal starts nothing and emits no
lifecycle signal.

**`arm(signalDict, scanInfoDict, owner=...)`** does four things in order:

1. **Composes the participant snapshot** — `(selected scan-driven detectors) |
   (scan-driven detectors held by an override purpose)`, minus faulted ones. A
   detector whose stop failed never rejoins a scan. Override purposes
   (`RECORDING`, `SNAP`, `WORKFLOW`, `EVENT_STREAM`, `EVENT_DIRECT`, `GENERIC`)
   win over deselection: deselecting is a preference, an active recording is a
   commitment. Deferred selection changes are flushed here — the one point
   where a new selection takes effect without disturbing an iteration already
   in flight.
2. **Takes the `SCAN` lease** on those detectors (`DetectorsManager.acquire`).
3. **Injects the snapshot into `scanInfoDict`** as `participants` and
   `excludedDetectors`. Participation travels as scan-scoped data on the dict
   `sigScanBuilt` already carries, so there is no mirror to go stale between
   iterations. A dict *without* `participants` means "legacy scan": every
   scan-driven detector participates, which preserves behavior for any entry
   point not routed through the coordinator.
4. Calls `nidaqManager.runScan`.

Any failure — busy refusal, build failure, arm exception — resolves the token
and unwinds the lease before propagating, so a refused arm cannot strand
ownership. Completion runs **exactly once** per iteration across all five
terminations (busy refusal, build failure, arm exception, normal completion,
abort).

**`resolve(token, mode, onComplete=...)` is the finish barrier.** It asks every
participant `finishScan(mode, acknowledge)` and then *waits asynchronously* for
their acknowledgements before releasing the SCAN lease — the call never blocks
the UI thread. `mode` is `'graceful'` or `'abort'`. Point detectors integrate a
whole scan and publish their frame at the very end, so releasing the lease at
`sigScanDone` used to tear the worker down mid-read and lose that frame.
`finishScan` runs for every participant regardless of refcount (a TimeTagger
that also holds a `WORKFLOW` lease goes 2→1, so no hardware stop happens, but
it still needs its final read); hardware stop happens only at aggregate zero.
Implemented by `APDManager`, `PMTManager` and `SwabianTimeTaggerManager`; the
`DetectorManager` base acknowledges immediately. A 5 s deadline
(`DEFAULT_FINISH_TIMEOUT_S`, armed through an injected scheduler —
`QTimer.singleShot` in the GUI) releases anyway rather than wedging the GUI:
losing one final frame beats a stuck application.

`onComplete` fires when the barrier clears, on whichever thread acknowledged
last. **`scanDone` hangs off this**, which is why the sequence in §3 publishes
completion after the barrier rather than directly from the NI-DAQ signal.

```
user / external trigger
  └─ runScanAdvanced()
       ├─ coordinator.reserveRun(self) ─► ScanRunToken   (ScanBusyError if
       │                                  another owner holds the run)
       ├─ isRunning = True  ──────────► channel.setActiveScanSource(self)
       ├─ sigScanStarting               (unless already emitted by trigger)
       ├─ coordinator.arm(...) ────────► participants + SCAN lease +
       │                                  scanInfoDict['participants'],
       │                                  then nidaqManager.runScan
       ├─ sigScanBuilt(deviceList)      (NIDAQ: relayed from nidaqManager;
       │                                 TriggerScope: emitted by controller)
       ├─ hardware starts
       └─ sigScanStarted                (relayed from nidaqManager /
                                         scanManager — active source is
                                         already set at this point)
  ... scan runs; consumers poll isScanRunning(), read getDimsScan() etc. ...
NI-DAQ reports done / build failure
  └─ coordinator.resolve(token, graceful|abort)      ← finish barrier
       ├─ finishScan(mode, ack) to every participant
       ├─ wait (async) for all acknowledgements, or 5 s deadline
       ├─ release the SCAN lease
       └─ onComplete ─► scanDone() / scanFailed()   (re-queued onto the
            ├─ isRunning = False ─────► clearActiveScanSource(self)
            ├─ sigScanDone              controller's own thread)
            └─ sigScanEnded             (skipped for non-final parts;
                                         run reservation released here)
```

Only the controller that *armed* the iteration reacts: `tokenForOwner(self)`
returns `None` for everyone else, so the NI-DAQ done signal — broadcast to
every scan controller — cannot make a bystander publish an early
`sigScanEnded`.

**Repeat mode** does *not* re-enter the scan machinery synchronously. `scanDone`
runs inside the just-finished scan's task-completion slot; calling
`runScanAdvanced` from there recreated NI-DAQ tasks, `WaitThread`s and
per-detector scan `QThread`s while the previous ones were still tearing down —
a QThread destroyed while still running, which crashed the GUI on real hardware
every time *Repeat* was enabled. `_armRepeatScan` therefore defers the next
frame with `QTimer.singleShot(0, self._fireRepeatScan)` so the current signal
chain unwinds first. `_repeatPending` is cleared by `abortScan` / `scanFailed`,
and `_fireRepeatScan` re-checks `_shouldContinueRepeat()`, so aborting or
un-checking *Repeat* in that gap cancels cleanly and terminalizes exactly as
`scanDone` would have. `isScanRunning()` is briefly `False` between repeat
frames, but the **run reservation is retained**, so no other entry point can
arm in the gap.

## 4. Contract for new scan controllers

A new controller that runs hardware scans MUST:

1. Inherit `ScanLifecycleMixin` — directly, or via `SuperScanController`
   (which already inherits it):

   ```python
   from ..basecontrollers import ImConWidgetController, ScanLifecycleMixin

   class MyNewScanController(ScanLifecycleMixin, ImConWidgetController):
       ...
   ```

2. Maintain `self.isRunning` around its scan lifecycle: `True` when starting,
   `False` on done / failure / error — including in exception handlers of
   `runScanAdvanced()`.

3. Emit the channel scan signals (`sigScanStarting`, `sigScanBuilt`,
   `sigScanStarted`, `sigScanDone`, `sigScanEnded`) following the sequence in
   §3.

4. **Never call `nidaqManager.runScan` directly.** Reserve the run with
   `self._scanCoordinator.reserveRun(self)`, arm each iteration through
   `self._scanCoordinator.arm(signalDict, scanInfoDict, owner=self)`, and let
   `ScanBusyError` propagate as a refusal rather than starting hardware
   anyway. `SuperScanController` already does all of this in
   `_armScanIteration`; a controller outside that hierarchy (the
   `EtSTEDTriggeredScanRunner` pattern) must do it itself.

5. **Publish completion from the finish barrier, not from the NI-DAQ signal.**
   Resolve the iteration with
   `coordinator.resolve(token, mode, onComplete=...)` and call `scanDone()` /
   `scanFailed()` from `onComplete`. Guard with `tokenForOwner(self)` so a
   broadcast completion signal is ignored by non-owners, and re-queue
   `onComplete` onto the controller's own thread — the last acknowledgement can
   arrive on a detector worker thread.

6. **Re-arm repeats deferred**, via `_armRepeatScan()` (or an equivalent
   zero-delay timer) — never synchronously from `scanDone` (see §3).

7. *(Optional, for BeadRec support)* additionally inherit
   `BeadRecScanSourceMixin` and implement `getBeadRecScanDims()` /
   `getBeadRecStepSizes()`; override `isBeadRecCompatible()` to return
   `False` if the frame stream does not map to a 2D raster.

A **scan-driven detector manager** (one whose `isScanDriven` is `True`) has a
matching obligation: override `finishScan(mode, acknowledge)` if it has
end-of-scan work — a final read, a last frame to publish — and call
`acknowledge()` when that work is done, from any thread. The base
implementation acknowledges immediately, which is correct only for managers
with nothing outstanding. Not acknowledging costs a 5 s stall per iteration and
then proceeds without you.

**Enforcement:** the adoption-audit test in
`imswitch/imcontrol/_test/unit/test_scan_lifecycle.py`
(`test_every_scan_controller_inherits_scan_lifecycle_mixin`) AST-scans every
controller module. Any class that assigns `self.isRunning` or defines
`runScanAdvanced` without inheriting the mixin (transitively) fails CI with a
message pointing here. Forgetting the contract is a test failure, not a
silent runtime defect.

## 5. Scan controller inventory (audited June 2026)

**NIDAQ family** — inherit `SuperScanController` (lifecycle-aware via its
bases), registered under widget key `'Scan'` as
`ScanController{scanWidgetType}` (`ImConMainController`):

| Controller | scanWidgetType | BeadRec support |
|---|---|---|
| `ScanControllerBase` | `Base` | protocol (`BeadRecScanSourceMixin`) |
| `ScanControllerPointScan` | `PointScan` | none |
| `ScanControllerAdvanced` | `Advanced` | legacy accessors via `'Scan'` key |
| `ScanControllerMoNaLISA` | `MoNaLISA` | legacy accessors + axial workflow |

**TriggerScope family + standalone** — inherit
`ScanLifecycleMixin, ImConWidgetController` directly, registered under their
own widget keys; scan start is relayed from
`ScanManagerTriggerScope.sigScanStarted`, completion from the **board-level**
`sigScanDone` (shared by all TriggerScope controllers — each one's
`scanDone()` ignores it unless its own `isRunning` is set):

| Controller | BeadRec support |
|---|---|
| `TriggerScopeRasterController` | full protocol (the Snouty BeadRec target) |
| `TriggerScopeScanController` (unified RESOLFT façade) | none |
| `TriggerScopePLSRController` | none |
| `TriggerScopePLSRMulticolorController` | none |
| `TriggerScopeLSXYRController` | none |
| `TriggerScopeGalvoDetectionController` | none |
| `LightSheetMulticolorController` | none |

**Not scan sources** (orchestrate around scans, never own the lifecycle —
deliberately excluded): `EtSnoutyController` (event-triggered workflow that
*requests* scans via `sigRunScanTriggerScopePLSRMulticolor`),
`RotationScanController` (steps rotators between scans run by others).

## 6. How BeadRec consumes this

- `BeadWorker` polls `commChannel.isScanRunning()` in its acquisition loop —
  now answered by the active source for *any* scanner family, so the worker
  no longer dies on setups without a `'Scan'` widget (e.g. Snouty).
- **Frames come via `DetectorManager.readChunk('BeadRec')`**, the
  multi-consumer chunk distributor. The raw `getChunk()` is a destructive
  read; when the RecordingManager polled the same camera during a scan-once
  recording, BeadRec and the recording each received a random subset of the
  frames (both incomplete). `readChunk` drains the hardware once and gives
  every registered consumer a full copy; consumers release their queue when
  done (`releaseChunkConsumer`). BeadRec also only reads the **current**
  detector — the right camera must be selected in the view.

  All destructive frame consumers go through `readChunk` (consumer keys:
  `'RecordingManager'`, `'BeadRec'`, `'WorkflowFacade'` for the WFS workflow
  camera facade). Components that only *peek* (live view, focus lock,
  autofocus, EtSnouty event detection, tiling preview) use the
  non-destructive `getLatestFrame` and need no registration. An enforcement
  test (`test_detector_chunk_consumers.py::
  test_no_production_code_calls_get_chunk_outside_detector_layer`) fails CI
  if new production code calls `.getChunk()` directly outside the detector
  layer.
- On `sigScanStarted`, `BeadRecController.updateParameters()` reads
  `getDimsScan()` / `getScanStepSizes()` / `getFramesPerScanPixel()` — all
  resolved from the announcing controller.
- If no scan source exists at all, `getDimsScan()` still raises
  `RuntimeError` and BeadRec logs its one-shot "inactive" warning;
  `isScanRunning()` returns `False` instead of raising.

### Hardware caveats for BeadRec + TriggerScope raster (verified June 2026)

- **The deployed firmware pulses TTL lines 0–3 together.** The TriggerSwitch
  0.1 sketch (`runPixelCycle()` in the local Triggerscope repo) contains a
  "temporary fix" that drives TTL 0–3 in a single window per pixel, using
  the earliest TTL row's start/end (`p1StartUs`/`p1EndUs`). The per-device
  line selection (`p1Line`) and all other TTL rows are **ignored** — laser
  emission during raster scans is controlled purely by arming
  (`sigScanBuilt` → `setScanModeActive`), not by the TTL table. The firmware
  parameter parser already supports three independent pulses (p1/p2/p3);
  restoring the commented-out per-line code in `runPixelCycle()` would
  enable real per-device pulse windows. Note for that fix: the parser
  truncates start/end to `uint16_t` (max ~65.5 ms), and the panel TTL
  labels vs. the firmware's 0-based `ttl[]` indexing must be reconciled.
- **Cameras must be armed to see triggers.** Detectors acquire only while
  live view or a recording is active (handle-based `DetectorsManager`);
  an idle externally-triggered camera ignores all TTL pulses.
- **Dwell time must exceed camera exposure + readout**, otherwise triggers
  arriving during readout are dropped (~half the frames, never exactly).
  The raster controller logs a warning with the exact numbers at scan
  start. 128×128 px at 5 ms dwell confirmed working on the Snouty rig.
- **Unidirectional raster assumed.** BeadRec fills its reconstruction buffer
  row-major. If the firmware `RASTER_SCAN` is bidirectional, every other
  line appears mirrored (cf. the MoNaLISA bidirectional fix, commit
  `a9923c10`).
- **Pixel-count convention** is `round(axis_length / axis_step_size)`,
  matching the raster widget's steps display.
- **Trailing pixels.** Frames still in transit when the firmware reports
  done are dropped (same semantics as the NIDAQ path).

## 7. Edge cases and known limits

- **Concurrent scans:** now *refused*, not merely reported. A second entry
  point trying to reserve a run or arm an iteration while another owner holds
  one gets `ScanBusyError` (§2.4), including in the gaps between repeat frames.
  The channel-level rule below still applies as a backstop for controllers
  outside the coordinator: last announcement wins, and the identity guard
  ensures the earlier controller's teardown cannot clear the newer scan.
- **Cont-laser-pulses mode** (`ScanControllerBase`): `isRunning` is set even
  though scan signals are suppressed — `isScanRunning()` reports `True`
  during continuous pulsing, identical to pre-refactor behavior.
- **A controller bypassing `isRunning`:** impossible to announce without it;
  this is exactly what the adoption-audit test guards (§4).

## 8. Tests

- `imswitch/imcontrol/_test/unit/test_scan_lifecycle.py` — mixin behavior
  (announce / withdraw / identity guard / last-wins), the adoption audit,
  and channel contract assertions.
- `imswitch/imcontrol/_test/unit/test_beadrec_scan_source.py` — BeadRec
  protocol/mixin defaults and active-source-first resolution contract.
- `imswitch/imcontrol/_test/unit/test_communication_channel_contract.py` —
  signal inventory unchanged (the active-source API is methods, not signals).

Coordinator layer (§2.4):

- `test_scan_execution_coordinator.py` — arm/resolve exactly-once across all
  five terminations, participant composition, finish barrier and its timeout.
- `test_scan_busy_refusal.py` — a refused arm starts nothing, emits no
  lifecycle signal and strands no ownership.
- `test_scan_repeat_rearm.py` — repeat defers instead of running synchronously;
  abort / un-check / already-running cancel the pending frame.
- `test_detector_selection.py` — selection, override purposes, deferred
  application at the next iteration, back-compatibility without selection
  support.
- `test_acquisition_leases.py`, `test_detectors_manager_leases.py`,
  `test_acquisition_concurrency.py` — lease refcounting, stop contract and
  concurrency regressions.
