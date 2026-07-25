# Detector acquisition ownership & selection

Branch: `perf/pointscan-confocal-sted` (shared with the point-scan live-preview
performance fixes — see
[docs/pointscan-confocal-sted-performance.md](../../pointscan-confocal-sted-performance.md)).

Status: **v8 — R7 findings incorporated; open decisions resolved; Phase 1 in
implementation.** [Review matrix](#review-response-matrix) and
[acceptance gate](#acceptance-gate) at the end.

## The one idea

> **A detector *participates in ImSwitch-managed acquisition* iff it holds ≥1
> acquisition lease.**

`DetectorsManager` is the sole authority. But two things a lease must *not*
conflate (R6-1): **arming** hardware (refcount) and **frame delivery** (who gets
polled into `sigUpdateImage`). Both are lease-derived, but from different lease
sets. The user's **selection** is a set that seeds the live-view/scan leases.

## Core model

### API and migration (R5-1, R6-3)
Real API:

- `acquire(detectorNames: Iterable[str], purpose: LeasePurpose) -> LeaseHandle` —
  validates + de-dups; `detectorNames` is an explicit iterable. **An explicit
  empty iterable is rejected** (never silently "all"). **Acquiring a FAULTED
  detector is rejected** (`DetectorFaultedError`) — quarantine applies to new
  leases, not only to snapshots (R7-s6).
- `release(handle)` — general release (scan completion uses `finishScan`, below,
  which is *separate*).

Compat shim preserving today's behaviour:

- `startAcquisition(liveView=False, *, detectorNames=None)`: when
  `detectorNames is None` (identity check, **not** `or ALL` — R6-3) resolve to
  **all `forAcquisition`** (legacy), purpose `LIVE_VIEW` if `liveView` else
  `GENERIC`. `stopAcquisition(handle, liveView=False)` → `release(handle)`.
- **`ViewController` is migrated to `acquire(selectedFreeRunning, LIVE_VIEW)`
  before selection is enabled** (Phase 4). Until then the legacy `LIVE_VIEW` shim
  leasing all `forAcquisition` is a documented temporary exception; it must not
  survive into Phase 5 (or a `LIVE_VIEW` lease would wrongly contain scan-driven
  detectors).

### Lease purpose (R4-2, R6-1)
`LeasePurpose ∈ {LIVE_VIEW, SCAN, RECORDING, SNAP, FOCUS, WORKFLOW, EVENT_STREAM,
EVENT_DIRECT, GENERIC}`.

- `GENERIC` — compat bucket for un-migrated `startAcquisition(liveView=False)`.
- `EVENT_STREAM` — a fast detector whose frames must be **polled/delivered** via
  `sigUpdateImage` even with live view off (etSTED / EtMonalisa detection loops;
  temporary binary-mask recording).
- `EVENT_DIRECT` — event ownership that **reads directly** (EtSnouty's
  `wait_and_get_NewFrame`) and does not need the poll loop.

### Frame-stream vs arming vs selection (R6-1)
Three distinct memberships, all lease-derived:

- **Arming (hardware on):** aggregate refcount > 0 → `_acquisitionLeased`.
- **Frame-stream (polled delivery):**
  `frameStreamMembership = LIVE_VIEW ∪ EVENT_STREAM`. The **`LVWorker` thread
  runs iff this is non-empty and polls exactly this set** — so an EVENT_STREAM
  lease keeps `detectorFast` delivering frames with live view off (fixes the
  "armed but no frames" stall, R6-1).
- **Selection lock:** `LIVE_VIEW` membership only (below). EVENT/other leases do
  not lock selection.

### Who leases what
- **LIVE_VIEW** — **selection-tracking** (R7-3): membership is always
  `current selection ∩ free-running ∖ FAULTED`. A selection change while live
  view is active is applied **immediately** by the manager as an atomic lease
  swap under the manager lock (newly selected detectors arm 0→1, deselected
  ones stop 1→0). No dead checkboxes, no one-way lock.
- **Scan participant snapshot** (composed by the scan-execution coordinator at
  arm of **each iteration**, **after** pre-arm leases are declared, R6-5):
  `participants = (selected scan-driven) ∪ (scan-driven held by
  RECORDING / SNAP / WORKFLOW / EVENT_* / GENERIC leases)`. **GENERIC is included
  until it is removed** — else a legacy caller's lease on a deselected point
  detector would silently drop it from the scan, breaking the compat guarantee
  (R6-3). The coordinator takes a **SCAN** lease over this exact snapshot and
  **injects the participant set into `scanInfoDict`** (see mirrors, R7-s5).
- **RECORDING/SNAP/FOCUS/WORKFLOW/EVENT_*** lease their own detectors. Consumers
  that need their detector **armed before scan TTL output starts** declare their
  lease **pre-arm** on `sigScanStarting` (R6-5). Note (R7-2a): for BeadRec this
  is an *arming* requirement — its camera is trigger-driven and by definition
  never appears in the scan-driven participant snapshot; do not "optimize" the
  pre-arm declaration away on those grounds.
- **Pre-arm lease span vs. repeat iterations (R7-2).** `sigScanStarting` and
  `sigScanEnded` fire once per **scan run** and bracket *all* repeat iterations
  (`_fireRepeatScan` passes `sigScanStartingEmitted=True`; the repeat branch of
  `scanDone` emits neither `sigScanDone` nor `sigScanEnded`). Pre-arm leases are
  therefore declared once per run and **held until `sigScanEnded`**; each
  iteration's snapshot reads the leases *active at that moment* — no
  re-declaration signal exists mid-run, and none is needed.

### Two mirrors + participation as scan data (R4-4, R5-2, R6-4, R7-s5)
Managers never read the lease table; `DetectorsManager` mirrors read-only base
attributes (written only by it):

- `_acquisitionLeased` — **lease bookkeeping only**: aggregate refcount > 0.
- `_hardwareFaulted` — **uncertain hardware state**, *separate* from lease state
  (R6-4): a stop that failed. Never conflated with `_acquisitionLeased`.

**Scan participation is not a mirror (R7-s5).** `sigScanBuilt` already carries
`scanInfoDict`; the coordinator injects the participant snapshot into it at arm
time, and the point-detector `sigScanBuilt`/`initiateScan` gate checks
`self.name in scanInfoDict['participants']` (absent key = legacy scan = all
scan-driven participate, preserving compat). Participation is thereby
scan-scoped *data*, atomically consistent with the scan build — no mutable
mirror to go stale between iterations. (`startScan` at `sigScanStarted` needs no
gate of its own: if `initiateScan` created no worker, it is a natural no-op.)

### Selection (R4-8, R5-c4, R7-3)
`DetectorsManager` owns the selection set, initialized to exactly the
`forAcquisition` set. `setDetectorSelected(name, active)` applies **atomically
under the manager lock** (GUI disabling is cosmetic). There is **no hard
rejection** anywhere; the two deferral rules are:

- **Free-running detector, live view active** → applied immediately via the
  LIVE_VIEW selection-tracking swap (above). Never queued, never locked.
- **Scan-driven detector, active SCAN lease** → **queued and applied at the next
  iteration's snapshot composition** (R6-c1) — otherwise auto-repeat leaves the
  user no window (acceptance #7).

Selection only seeds *new* lease composition. It never mutates an explicit
consumer's lease: deselecting a camera mid-recording does not touch the
RECORDING lease. Not persisted.

### Detector kinds (two axes)
`isScanDriven` (`True` only APD/PMT/TimeTagger) = ownership axis; frame clock
(internal / external-trigger / scan-clock) is the camera `Trigger source`
parameter. Trigger-driven camera = `isScanDriven=False`. *(Naming decision 3.)*

### Global signals (resolves R5-c3 / old decision 4)
`sigAcquisitionStarted/Stopped` fire on the first-start / last-stop of
**non-FOCUS** leases (user-visible acquisition). A permanent FOCUS startup lease
does **not** keep them asserted.

## Detector stop contract + transactions + faults (R1-7, R2-3, R4-5, R6-4)

**Stop contract (Phase 1, all managers).** `stopAcquisition()` today swallows
teardown errors in APD ([:163](../../../imswitch/imcontrol/model/managers/detectors/APDManager.py:163)),
PMT ([:208](../../../imswitch/imcontrol/model/managers/detectors/PMTManager.py:208)),
TimeTagger ([:655](../../../imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py:655)),
so `DetectorsManager` can't detect failure. The contract: teardown **raises on
failure** (managers may still log first); every manager conforms.
`DetectorsManager` is the only catcher and turns the exception into
`_hardwareFaulted`. Internal worker-completion paths (`acqDoneSignal` →
`stopAcquisitionLocal`) keep their own guarded handling — the contract binds the
`DetectorsManager`-facing `stopAcquisition()` only.

**Second manager contract — graceful finish (R7-s4).** `finishScan` (below)
implies a per-manager hook distinct from teardown: `finishScan(mode)` on
scan-driven managers (`graceful` = TimeTagger `signal_done()` → final
read/fit/emit; `abort` = abrupt; default no-op for others). The contract is
*defined* in Phase 1 alongside the stop contract; implementations land in
Phase 3.

All refcount mutations + hardware start/stop run under one serializing lock.
- **Acquire transactional:** on failure, decrement every detector this call
  incremented (incl. an already-≥1 detector A 2→1) and stop only those it started
  (0→1); drop the handle; re-raise.
- **Failed stop → FAULTED + quarantine.** Set `_hardwareFaulted` (distinct from
  `_acquisitionLeased`), **exclude from every future participant/stream snapshot**
  (a broken point detector cannot rejoin scans), surface it; recovery is an
  explicit `retryStop()`/shutdown path, never a natural 1→0.
- Tests: existing-A + new-B + failing-C; rollback-stop throws; release-stop
  throws; fault quarantine + successful retry-stop.

## Scan lifecycle — a shared coordinator across all FIVE entry points (R4-3, R5-3, R5-4, R6-2, R6-5)

There are **five** direct `nidaqManager.runScan` callers, not four: the four
`SuperScanController` subclasses **and** `EtSTEDTriggeredScanRunner`
([:74](../../../imswitch/imcontrol/model/EtSTEDTriggeredScanRunner.py:74), reached
from `runSlowScan()`). (The seven TriggerScope-family `scanManager.runScan`
callers are **intentionally out of scope**: verified none starts/stops detector
acquisition — the only detector contact is a read-only parameter check in
`TriggerScopeRasterController`; their cameras are trigger-driven and record via
`RecordingManager`, which is a leaseholder in its own right. R7-s1.) A helper on
`SuperScanController` would miss the runner, so the lifecycle lives in a
**reusable `ScanExecutionCoordinator`** used by all five.

**Run vs. iteration (R7-2, R7-4).** The coordinator models two levels:

- A **scan run** = one user/API-initiated scan including all repeat iterations,
  MoNaLISA `autoAxial` follow-up scans, and `isNonFinalPartOfSequence` sequence
  parts. `sigScanStarting`/`sigScanEnded` and **pre-arm lease span** are
  run-level.
- A **scan iteration** = one `nidaqManager.runScan` → `scanDone` cycle. The
  **participant snapshot, SCAN lease, lifecycle token, and `finishScan` are all
  iteration-level** — each iteration is a frame, and the TimeTagger final
  read/fit/emit runs per frame (this is what acceptance #5 already requires for
  repeats; axial follow-ups and sequence parts behave identically).

Lifecycle:

- **Pre-arm:** consumers that must be in the snapshot (BeadRec, recording)
  declare their detector lease on `sigScanStarting` — **before** the coordinator
  composes the participant snapshot (R6-5). BeadRec today starts at
  `sigScanStarted`/`onNewScan` ([:587](../../../imswitch/imcontrol/controller/controllers/BeadRecController.py:587)),
  which is too late (snapshot already built; a triggered camera may have missed
  initial TTL frames). BeadRec captures a **stable per-scan detector name** and
  reads exactly that, not `execOnCurrent`.
- **Arm:** compose snapshot → take SCAN lease → inject participants into
  `scanInfoDict` → issue one **idempotent per-iteration lifecycle token** → call
  `nidaqManager.runScan`. All **before** `sigScanBuilt`.
- **Busy refusal is an arm failure (R7-1).** `NidaqManager.runScan` today
  silently no-ops when `self.busy`
  ([:366](../../../imswitch/imcontrol/model/managers/NidaqManager.py:366)) — no
  exception, no `sigScanBuildFailed` — which would strand the SCAN lease and its
  token forever (e.g. an EtSTED trigger racing a widget scan). `runScan` gains a
  **return value / raises when busy**; the coordinator treats refusal exactly
  like an arm exception: resolve the token immediately, roll back the SCAN
  lease, clear the snapshot. This is the **fifth** exactly-once case: *scan
  never started, no failure signal*.
- **Completion is `finishScan`, not an aggregate refcount transition (R5-3).**
  `finishScan(snapshot, mode=graceful|abort)` runs for **every** participant on
  **every** termination regardless of aggregate refcount (a TimeTagger with
  WORKFLOW+SCAN leases goes 2→1 — hardware stop does nothing, but it still needs
  its final read). `graceful` = TimeTagger `signal_done()` → final read/fit/emit;
  `abort` = abrupt. General hardware stop happens only at aggregate 0 via `release`.
- **Async barrier, exactly-once, NI-DAQ-authoritative (R5-4, R6-c1, R7-1).**
  Completion is acknowledged asynchronously — **never blocking the UI thread or
  holding the `DetectorsManager` lock**. Completion runs **exactly once** across
  **busy refusal**, build failure, arm exception, normal completion, abort (the
  token). Because `abortScan()` does not stop a running NI-DAQ scan, SCAN
  ownership **persists until NI-DAQ actually stops** (`nidaqManager.scanDone`),
  not at the user's abort click. **Ordering:** barrier resolves → release SCAN
  lease + clear snapshot → **queue** the next repeat arm on a later event-loop
  turn (keeps the perf-branch deferral to avoid reentrancy — the barrier
  *feeds*, not *replaces*, the deferred re-arm), applying any queued selection
  change.

## Acquisition-owner audit (semantic owners; R4-6, R5-c1, R6-1, R6-5)

| Owner | Today | Classification |
|---|---|---|
| FocusLock / Autofocus | direct `startAcquisition()` in `__init__`, no `closeEvent` cleanup | **FOCUS** leaseholder; store + release on close |
| CamFacade | raw start/stop | **WORKFLOW** leaseholder |
| RecordingManager | own handle | **RECORDING/SNAP** leaseholder; declares pre-arm for scans |
| TimeResolvedScanWorkflow | no lease | **WORKFLOW** leaseholder, configure → final-product drain |
| TilingController ([_runScan](../../../imswitch/imcontrol/controller/controllers/TilingController.py:151)) | bg thread `getLatestFrame()` | **WORKFLOW** leaseholder (whole loop) |
| BeadRecController ([onNewScan](../../../imswitch/imcontrol/controller/controllers/BeadRecController.py:587)) | `readChunk`, starts at `sigScanStarted` | **Own lease declared pre-arm** (`sigScanStarting`) through `_completeEndedScan`; stable per-scan detector name |
| etSTED / EtMonalisa ([EventTriggeredBaseController](../../../imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py:358)) | subscribe `sigUpdateImage` for `detectorFast`; binary-mask recording subscribes independently ([:438](../../../imswitch/imcontrol/controller/controllers/EventTriggeredBaseController.py:438)) | **EVENT_STREAM** lease on `detectorFast` (loop) + a **temporary EVENT_STREAM** lease for mask recording with failure/timeout cleanup |
| EtSnoutyController ([:583](../../../imswitch/imcontrol/controller/controllers/EtSnoutyController.py:583), mask [:657](../../../imswitch/imcontrol/controller/controllers/EtSnoutyController.py:657)) | `wait_and_get_NewFrame`; mask/preview reads | **EVENT_DIRECT** leaseholder; temporary **SNAP/WORKFLOW** lease for mask/preview when not armed |

## Phases (selection enabled last, feature-gated — R5-5)

1. **Lease core + both manager contracts.** ✅ **DONE** (uncommitted).
   `acquire`/`release`, `LeasePurpose`, per-detector refcounts, serialized
   transactions, FAULTED/quarantine via `_hardwareFaulted` (incl.
   acquire-reject on faulted + `retryStop`), the two mirrors, the **detector
   stop contract** (raise on failure; the three swallowing managers fixed) +
   the **graceful-finish contract definition** (R7-s4), compat shims. No
   behaviour change.

   Landed as:
   - `imswitch/imcontrol/model/managers/_acquisition_leases.py` — framework-free
     `AcquisitionLeaseTable`, `LeasePurpose`, `LeaseHandle`, `LeaseTransition`,
     `DetectorFaultedError`.
   - `DetectorsManager` — `acquire`/`release`/`retryStop`/`isDetectorLeased`/
     `isDetectorFaulted`; `startAcquisition`/`stopAcquisition` reduced to compat
     shims over the table (the hand-rolled handle lists and mutex are gone).
     Global signals and poll-thread bring-up run **outside** the lease lock in
     the legacy order; only the pre-stop poll-thread teardown runs inside it
     (ordering-critical, and the poll loop never re-enters the table).
   - `DetectorManager` — `_acquisitionLeased` / `_hardwareFaulted` mirrors with
     read-only properties, stop contract documented, `finishScan(mode)` hook.
   - APD / PMT / SwabianTimeTagger — `stopAcquisition` now raises; PMT's body
     extracted to `_teardownScan` so the `acqDoneSignal` →
     `stopAcquisitionLocal` path keeps swallowing as designed. Swept all
     detector managers: no other swallowing stop path exists.
   - Tests: `test_acquisition_leases.py` (22), `test_detectors_manager_leases.py`
     (12), `test_detector_stop_contract.py` (9).
2. **Migrate every owner — each at *its own* current scope (R7-s3).** Full
   table incl. TimeResolved, Tiling, BeadRec pre-arm, etSTED/EtMonalisa
   EVENT_STREAM + temp mask leases, EtSnouty EVENT_DIRECT, FocusLock/Autofocus
   `closeEvent`, RecordingManager handle→lease bookkeeping. Resolve
   global-signal semantics (non-FOCUS). Three distinct starting points, and
   conflating them is how a "pure bookkeeping" phase silently changes hardware
   behaviour:
   - **Already detector-scoped today** (FocusLock, Autofocus, CamFacade,
     TimeResolved): migrate to a lease over *the same* detector. Behaviour
     preserved; the *fix* is that the detector is now refcounted, so a global
     stop can no longer pull it out from under them.
   - **Broadcasts today** (RecordingManager `snap()`/record via
     `startAcquisition()`): keep the all-`forAcquisition` scope in this phase.
     Narrowing to the actually-recorded detectors is a behaviour fix and
     belongs in Phase 4, not smuggled in here.
   - **Arms nothing today** (TilingController — it reads `getLatestFrame()` on
     a bare background thread and silently depends on live view being on):
     giving it a WORKFLOW lease *is* a behaviour change. Intentional, called
     out, and tested (acceptance #10) rather than presented as a no-op.

   **Status: PARTIAL (uncommitted).** Done: FocusLock + Autofocus (FOCUS lease,
   `closeEvent` release — both previously bypassed the manager entirely and
   never released), CamFacade (WORKFLOW lease, legacy raw calls retained when
   constructed without a manager), TilingController (WORKFLOW lease over the
   whole `_runScan` loop, released in `finally`), RecordingManager
   (`startAcquisition`→`acquire` with explicit SNAP / RECORDING purposes, scope
   deliberately unchanged). `ViewController` is intentionally still on the
   legacy shim — its LIVE_VIEW migration is Phase 4, before selection is
   enabled. Remaining: TimeResolvedScanWorkflow, BeadRec pre-arm,
   etSTED/EtMonalisa EVENT_STREAM + temp mask leases, EtSnouty EVENT_DIRECT —
   all four are entangled with Phase 3 (pre-arm signal timing) and Phase 4
   (frame-stream membership), so they land with those phases rather than
   half-wired here.
3. **`ScanExecutionCoordinator`** across all **five** NI-DAQ entry points; the
   run/iteration model; SCAN lease + participant injection into `scanInfoDict`
   (currently all scan-driven → no change); `finishScan` split; async barrier
   feeding the deferred re-arm; token; **`runScan` busy-refusal → arm failure**
   (R7-1); NI-DAQ-authoritative release.

   **Status: DONE (commit `abe8a4a3`).** `_scan_execution.py` holds the
   framework-free coordinator (`ScanIterationToken`, `PARTICIPANTS_KEY`,
   `arm`/`resolve`/`resolveActive`); `ScanBusyError` makes refusal observable
   and `NidaqManagerError` now populates `str(exc)`; `isScanDriven` added to
   `DetectorManager` (True on APD/PMT/TimeTagger). All five entry points arm
   through the coordinator, with resolvers connected *before* each controller's
   `scanDone`/`scanFailed` so the lease is released and final reads are done
   before a repeat frame re-arms. 22 tests incl. a source-level guard against a
   sixth direct `runScan` caller. The point-detector gate is intentionally
   absent (Phase 5), leaving this change behaviourally inert.
4. **Frame delivery + recording scope + sim.** `LVWorker` polls
   `frameStreamMembership` (LIVE_VIEW ∪ EVENT_STREAM); **migrate
   `ViewController` to explicit LIVE_VIEW**; `setUpdatePeriod` becomes
   membership-aware (today it unconditionally quits+restarts the poll thread
   even with live view off, R7-s7); replace the `sleep(0.3)` UI-thread block in
   the LV start path; **narrow RecordingManager leases to the actual
   recorded/snapped detectors (all modes)**; Tiling/BeadRec/EtSnouty scoped
   leases; simulation snapshot = the exact participant snapshot.

   **Status: DONE except the simulator's camera half (see R7-s8).**
   `FRAME_STREAM_PURPOSES` drives both the poll thread's lifecycle
   (`frameStreamFirst/Last`, replacing `liveViewFirst/Last`) and exactly which
   detectors it reads, re-read every tick so a mid-flight lease needs no thread
   restart. `ViewController` leases only free-running detectors — scan-driven
   ones are armed by the SCAN lease now, which removes the old hidden
   dependency where a scan needed live view on to produce data at all.
   `setUpdatePeriod` no longer resurrects an idle poll thread; the 300 ms
   settle moved into `LVWorker.run` (worker thread) so arming no longer freezes
   the UI. RecordingManager leases exactly what it records/snaps.
   etSTED/EtMonalisa hold EVENT_STREAM on `detectorFast` for the detection
   loop's lifetime (fixing the live-view-off stall); EtSnouty holds
   EVENT_DIRECT for its run plus a temporary SNAP lease around its
   mask/preview reads; BeadRec pre-arms on `sigScanStarting` and pins its
   reconstruction detector for the whole scan. 22 tests.

### R7-s8 — acceptance #8 is under-specified (found during Phase 4)
"Simulation receives exactly the hardware participant snapshot" cannot be taken
literally: the participant snapshot is **scan-driven only**, while the
simulator generates **camera** frame triggers. Wiring the snapshot in directly
would stop simulated scans producing any camera frames. For a camera the real
question is *"would this camera be armed for this scan"* — lease state, not
snapshot membership — and answering it needs a runtime lease-state provider
inside `ScanSimulationCoordinator`, which `NidaqManager` constructs before
`DetectorsManager` exists (the long-standing construction-order issue).

Implemented now (safe half): the coordinator publishes `EXCLUDED_KEY`
(`excludedDetectors`) beside the participants, and the simulator skips those —
simulating frames for a deliberately excluded detector would feed the recorder
data real hardware would never produce. Empty until Phase 5, so inert today.
The list is published explicitly rather than derived because the simulator is
built from `setupInfo` alone and cannot tell a scan-driven detector from a
camera.

**Open decision for the camera half:** (a) wire a lease-state provider
(correct, needs the construction-order fix), or (b) keep cameras always
simulated and narrow acceptance #8 to scan-driven detectors (simpler; an
unarmed camera in simulation just produces frames nobody reads). Recommend (b)
unless simulation/hardware divergence has actually bitten.

**Also still open from Phase 2:** `TimeResolvedScanWorkflow` is unmigrated. Like
Tiling it currently arms nothing, so its WORKFLOW lease is another intentional
behaviour change rather than bookkeeping.
5. **Enable selection (perf win), feature-gated.** Selection default =
   `forAcquisition`; snapshot from selection ∪ explicit-lease overrides (incl.
   GENERIC); point-detector gate on `scanInfoDict['participants']`; TimeTagger
   stale-`_flim` clear; `setDetectorSelected` under the lock with LIVE_VIEW
   live-swap + queued inter-repeat changes; GUI checkboxes + `APIExport
   setDetectorSelected`.

## Acceptance gate

1. Legacy `startAcquisition`/`stopAcquisition` compat + explicit-purpose acquire;
   empty-iterable rejected; `None`=all-forAcquisition in shim.
2. Deselected point detector excluded from a plain scan.
3. Deselected point detector included when requested by recording/workflow/GENERIC.
4. Graceful SCAN completion while another purpose lease remains (2→1, still drains).
5. TimeTagger final product produced before repeat re-arm.
6. Build failure, exception, abort, normal completion — exactly once — across
   **all five** entry points, incl. **both EtSTED/EtMonalisa modes** (direct
   ScanWidget-triggered and RecordingWidget-triggered).
7. Queued selection change between repeat iterations applies next iteration.
8. Simulation receives exactly the hardware participant snapshot.
9. Fault quarantine + successful retry-stop, incl. a manager whose teardown fails
   under the stop contract.
10. Tiling, BeadRec, EtSnouty, FocusLock, Autofocus lease cleanup on close.
11. etSTED/EtMonalisa detection loop receives `detectorFast` frames **with live
    view off** (its EVENT_STREAM lease keeps the poll running); temporary
    binary-mask lease cleans up on failure/timeout.
12. `runScan` busy refusal (second scan racing a running one) rolls back the
    SCAN lease and resolves the token — nothing leaks, selection unlocks (R7-1).
13. Repeat run: pre-arm lease declared at `sigScanStarting` still covers
    iteration N+1's snapshot; released at `sigScanEnded` (R7-2).
14. Selecting a free-running detector while live view is active arms and streams
    it immediately; deselecting stops it — atomic swap, recording leases
    untouched (R7-3).
15. MoNaLISA `autoAxial` follow-up and `isNonFinalPartOfSequence` sequence part:
    `finishScan(graceful)` runs per iteration; `sigScanEnded` and pre-arm lease
    release happen once, at run end (R7-4).

## Review-response matrix

Rounds 1–5 resolved as previously recorded; rounds 6–7 below.

| # | Finding | Resolution |
|---|---------|-----------|
| R6-1 | EVENT arms but no frames | `frameStreamMembership = LIVE_VIEW ∪ EVENT_STREAM` drives LVWorker; EVENT_STREAM vs EVENT_DIRECT; temp mask leases |
| R6-2 | 5th NI-DAQ entry (EtSTEDTriggeredScanRunner) | `ScanExecutionCoordinator` used by all five; acceptance tests both EtSTED modes |
| R6-3 | Compat breaks post-selection | Snapshot ∪ GENERIC scan-driven until GENERIC removed; migrate ViewController LIVE_VIEW; `is None` not `or ALL`; reject empty |
| R6-4 | FAULTED can't detect swallowed stops | Phase 1 detector **stop contract**; split `_hardwareFaulted` from `_acquisitionLeased` |
| R6-5 | BeadRec lease too late | Pre-arm (`sigScanStarting`) declaration before snapshot; stable per-scan detector name |
| R6-c1 | Barrier vs re-arm reentrancy | Barrier → clear → queued re-arm on later turn; queued inter-repeat selection |
| R6-c2 | Global-signal vs FOCUS decision | Resolved: non-FOCUS first-start/last-stop |
| R7-1 | `runScan` busy no-op leaks SCAN lease | Busy refusal = arm failure; `runScan` signals refusal; token resolved, lease rolled back; fifth exactly-once case; acceptance #12 |
| R7-2 | `sigScanStarting` not per repeat iteration | Run vs. iteration model; pre-arm leases span the run (declared `sigScanStarting`, released `sigScanEnded`); per-iteration snapshots read active leases; acceptance #13 |
| R7-3 | LIVE_VIEW one-way selection lock (dead checkbox) | LIVE_VIEW is selection-tracking: atomic lease swap on selection change while live; no hard locks anywhere; acceptance #14 |
| R7-4 | `finishScan` vs sequences/axial unspecified | Iteration-level `finishScan` (per frame); run-level `sigScanEnded`/pre-arm span; acceptance #15 |
| R7-s1 | TriggerScope `scanManager.runScan` callers unexplained | Documented out of scope (verified: no detector interaction) |
| R7-s2 | BeadRec pre-arm wording implied snapshot membership | Reworded: arming-before-TTL; camera never in scan-driven snapshot |
| R7-s3 | Phase 2 "no behaviour change" not strictly true; RecordingManager phase ambiguous | Phase 2 = bookkeeping at all-`forAcquisition` scope; scope-narrowing = Phase 4 |
| R7-s4 | `finishScan` implied an unnamed second manager contract | Graceful-finish contract defined Phase 1, implemented Phase 3 |
| R7-s5 | `_scanParticipating` mirror can go stale | Replaced by participant set injected into `scanInfoDict` (absent key = legacy = all) |
| R7-s6 | `acquire()` on FAULTED unspecified | Rejected with `DetectorFaultedError` |
| R7-s7 | `setUpdatePeriod` restarts poll thread unconditionally; `sleep(0.3)` blocks UI | Both fixed in Phase 4 (membership-aware) |

## Resolved decisions (were "open" through v7)

1. **Staging** — foundation first (Phases 1–4), then the perf win (Phase 5).
   Phases 1–2 are independently valuable (fault visibility, leak-proof
   FocusLock/CamFacade) and de-risk the only phase that changes scan output.
2. **Scan transport** — events + lease-check. A push coordinator would recreate
   the reentrancy the perf branch's deferred re-arm eliminated.
3. **Naming** — keep `isScanDriven`. The two-axis definition (ownership vs.
   frame clock) does the disambiguation; `scanClockDriven` would invite
   conflation with the camera trigger-source axis.
