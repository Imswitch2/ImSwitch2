# Detector acquisition ownership & selection

Branch: `perf/pointscan-confocal-sted` (shared with the point-scan live-preview
performance fixes — see
[docs/pointscan-confocal-sted-performance.md](../../pointscan-confocal-sted-performance.md)).

Status: **v13 — the scoped Phase 1–4 ownership architecture remains
implemented and sound after another adversarial boundary audit. The bounded
Phase 4.5 camera-timelapse scheduler is implemented in software. R12 remains
green in the focused high-risk software suite (**539 passed**), and the Phase
4.5 recording/state subset is green (**137 passed**); the last complete
supported headless baseline predates R12, and the full supported lanes are
intentionally deferred to the pull-request/pre-merge gate. Controlled hardware
validation is still required, and Phase 5 remains a no-go until that gate
passes.**
[Review matrix](#review-response-matrix) and [acceptance
gate](#acceptance-gate) at the end.

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
- **`ViewController` was migrated in Phase 4 to
  `acquire(selectedFreeRunning, LIVE_VIEW)`**, before selection is enabled.
  The legacy `LIVE_VIEW` shim still preserves all-`forAcquisition` behavior for
  un-migrated external callers, but the application-owned live-view lease no
  longer includes scan-driven detectors.

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
mirror to go stale between iterations. APD, PMT and TimeTagger gate their
build, start and completion paths on that same iteration snapshot.

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

**Stop contract (Phase 1, all managers).** `stopAcquisition()` teardown
**raises on failure** (managers may still log first); every manager conforms,
so `DetectorsManager` can quarantine uncertain hardware state.
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
  incremented (incl. an already-≥1 detector A 2→1) and perform a compensating
  stop for every 0→1 start attempt, including the detector whose start raised
  after potentially partially arming; drop the handle; re-raise. A failed
  compensating stop quarantines that detector.
- **Failed stop → FAULTED + quarantine.** Set `_hardwareFaulted` (distinct from
  `_acquisitionLeased`), **exclude from every future participant/stream snapshot**
  (a broken point detector cannot rejoin scans), surface it; recovery is an
  explicit `retryStop()`/shutdown path, never a natural 1→0.
- Tests: existing-A + new-B + failing-C; rollback-stop throws; release-stop
  throws; fault quarantine + successful retry-stop.

## Scan lifecycle — a shared coordinator across all FIVE entry points (R4-3, R5-3, R5-4, R6-2, R6-5)

There are **five** NI-DAQ scan-initiation entry points, not four: the four
`SuperScanController` subclasses **and** `EtSTEDTriggeredScanRunner`
([:74](../../../imswitch/imcontrol/model/EtSTEDTriggeredScanRunner.py:74), reached
from `runSlowScan()`). (The seven TriggerScope-family `scanManager.runScan`
callers are **intentionally out of scope**: verified none starts/stops detector
acquisition — the only detector contact is a read-only parameter check in
`TriggerScopeRasterController`; their cameras are trigger-driven and record via
`RecordingManager`, which is a leaseholder in its own right. R7-s1.) A helper on
`SuperScanController` would miss the runner, so the lifecycle lives in a
**single shared `ScanExecutionCoordinator` per NI-DAQ manager**, used by all
five. Each token records its entry-point owner: NI-DAQ completion signals are
broadcast, but only the owner may clear the barrier and publish run-level
completion. A second arm is rejected while the active token is in flight or
finishing.

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

1. **Lease core + both manager contracts.** ✅ **DONE; hardened in R9.**
   `acquire`/`release`, `LeasePurpose`, per-detector refcounts, serialized
   transactions, FAULTED/quarantine via `_hardwareFaulted` (incl.
   acquire-reject on faulted + `retryStop`), the two mirrors, the **detector
   stop contract** (raise on failure; the three swallowing managers fixed) +
   the **graceful-finish contract definition** (R7-s4), compat shims. No
   normal-path acquisition-selection behaviour change; failure handling
   intentionally becomes fail-closed.

   Landed as:
   - `imswitch/imcontrol/model/managers/_acquisition_leases.py` — framework-free
     `AcquisitionLeaseTable`, `LeasePurpose`, `LeaseHandle`, `LeaseTransition`,
     `DetectorFaultedError`.
   - `DetectorsManager` — `acquire`/`release`/`retryStop`/`isDetectorLeased`/
     `isDetectorFaulted`; `startAcquisition`/`stopAcquisition` reduced to compat
     shims over the table (the hand-rolled handle lists and mutex are gone).
     Global signals and poll-thread bring-up run **outside** the lease-table
     lock in the legacy order. A separate manager-level lifecycle lock
     serializes frame-stream transitions around the outside-lock poll-thread
     join; the poll loop re-enters the table only for read-only membership.
   - `DetectorManager` — `_acquisitionLeased` / `_hardwareFaulted` mirrors with
     read-only properties, stop contract documented, `finishScan(mode)` hook.
   - APD / PMT / SwabianTimeTagger — `stopAcquisition` now raises; PMT's body
     extracted to `_teardownScan` so the `acqDoneSignal` →
     `stopAcquisitionLocal` path keeps swallowing as designed. Swept all
     detector managers: no other swallowing stop path exists.
   - Regression coverage includes transactional rollback, partial-start
     compensation, stop quarantine/retry and concurrent refcount churn.
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

   **Status: DONE in software; lifecycle-hardened in R9 and
   release-retry-hardened in R12.** FocusLock + Autofocus (FOCUS lease,
   `closeEvent` release — both previously bypassed the manager entirely and
   never released), CamFacade (WORKFLOW lease, legacy raw calls retained when
   constructed without a manager), TilingController (WORKFLOW lease over the
   whole `_runScan` loop, released in `finally`), RecordingManager
   (`startAcquisition`→`acquire` with explicit SNAP / RECORDING purposes).
   TimeResolved now holds WORKFLOW ownership from configure through
   final-product drain; BeadRec, event-triggered controllers and EtSnouty own
   and clean up their documented scopes. Close-time cleanup first cancels or
   joins active background work. Retry-sensitive frame-stream lease handles are
   cleared only after release succeeds; a failed release retains exact retry
   authority and prevents shutdown from being reported complete.
3. **`ScanExecutionCoordinator`** across all **five** NI-DAQ entry points; the
   run/iteration model; SCAN lease + participant injection into `scanInfoDict`
   (currently all scan-driven → no change); `finishScan` split; async barrier
   feeding the deferred re-arm; token; **`runScan` busy-refusal → arm failure**
   (R7-1); NI-DAQ-authoritative release.

   **Status: DONE (`abe8a4a3`), with the finish barrier completed in R8 and
   global-owner/generation hardening in R9–R12.** `_scan_execution.py` holds the
   framework-free coordinator (`ScanRunToken`, `ScanIterationToken`,
   `reserveRun`/`releaseRun`, `arm`/`resolve`, `PARTICIPANTS_KEY`). The
   participant barrier and SCAN-lease release complete before terminal
   publication, but the coordinator retains the globally active run reservation
   through controller-thread `sigScanEnded`, finalizes it only after
   publication, and then resolves the exact request terminal. Controllers
   expose themselves as the active scan source only after successful
   reservation, so a losing contender cannot replace the real owner.
   `ScanBusyError` makes
   refusal observable and `NidaqManagerError` now populates `str(exc)`;
   `isScanDriven` is true on APD/PMT/TimeTagger. All five entry points arm
   through one shared coordinator. Uncoordinated EtSTED ScanWidget triggering
   is rejected. APD/PMT/TimeTagger gate their build/start/done paths on the
   injected participant set; every queued APD/PMT pixel/frame callback and
   TimeTagger final callback carries a scan generation, so an old worker cannot
   mutate or acknowledge a newer scan.
4. **Frame delivery + recording scope + sim.** `LVWorker` polls
   `frameStreamMembership` (LIVE_VIEW ∪ EVENT_STREAM); **migrate
   `ViewController` to explicit LIVE_VIEW**; make `setUpdatePeriod`
   membership-aware (it previously quit and restarted the poll thread even
   with live view off, R7-s7); replace the `sleep(0.3)` UI-thread block in
   the LV start path; **narrow RecordingManager leases to the actual
   recorded/snapped detectors (all modes)**; Tiling/BeadRec/EtSnouty scoped
   leases; simulation snapshot = the exact participant snapshot.

   **Status: DONE in software; R12 boundary hardening included; hardware timing
   validation pending.**
   `FRAME_STREAM_PURPOSES` drives both the poll thread's lifecycle
   (`frameStreamFirst` plus manager-level last-streamer handling, replacing
   `liveViewFirst/Last`) and exactly which
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
   reconstruction detector for the whole scan. Temporary binary-mask
   acquisition has its own EVENT_STREAM lease plus laser/timeout cleanup.
   Manager-level lifecycle locks close the last-streamer acquire/release race
   without joining under the lease-table lock, and serialize an in-flight poll
   against detector stop. The latest-frame/chunk broker now fans one
   destructive hardware read out to concurrent preview/recording/BeadRec
   consumers and fails a lagging capped consumer explicitly instead of silently
   accepting an incomplete recording. Recording sessions use an atomic
   consumer boundary, abort partial output on writer or cleanup failure, and
   publish a generation-tagged writer-finalization terminal. A new recording
   resets prior scan and writer terminal state before any setup that may
   complete synchronously. Every cadence waits for its exact request terminal
   and writer terminal; non-final ScanLapse parts retain the run-level start and
   token, while final or stopped sessions additionally wait for the matching
   run-level `sigScanEnded`. Automated ScanLapse resolves and pins exactly one
   recording-capable scan source; it never broadcasts to every TriggerScope or
   LightSheet controller.

### R7-s8 — acceptance #8 is under-specified (found during Phase 4)
"Simulation receives exactly the hardware participant snapshot" cannot be taken
literally: the participant snapshot is **scan-driven only**, while the
simulator generates **camera** frame triggers. Wiring the snapshot in directly
would stop simulated scans producing any camera frames. For a camera the real
question is *"would this camera be armed for this scan"* — lease state, not
snapshot membership — and answering it needs a runtime lease-state provider
inside `ScanSimulationCoordinator`, which `NidaqManager` constructs before
`DetectorsManager` exists (the long-standing construction-order issue).

The coordinator publishes `EXCLUDED_KEY`
(`excludedDetectors`) beside the participants, and the simulator skips those —
simulating frames for a deliberately excluded detector would feed the recorder
data real hardware would never produce. Empty until Phase 5, so inert today.
The list is published explicitly rather than derived because the simulator is
built from `setupInfo` alone and cannot tell a scan-driven detector from a
camera.

**R9 resolves the camera half with the lease-state provider:** after
`DetectorsManager` construction, `MasterController` injects
`isDetectorLeased` as the simulator's runtime state provider. Simulated camera
frames are therefore emitted only for detectors that real hardware would have
armed for that scan.

4.5. **Camera-only timelapse.** ✅ **DONE in software; hardware validation
   pending.** `RecMode.CameraLapse` schedules discrete one-frame
   `RecordingManager` sessions. Frame 0 starts immediately; every later
   timepoint uses a monotonic deadline. Capture duration does not accumulate
   ordinary cadence drift, and a late callback or suspended workstation never
   produces a rapid catch-up burst. Every point has a fresh recording
   generation and post-writer-arm chunk boundary. Writer teardown releases its
   short `RECORDING` lease, and the scheduler will not start the next point
   until manager shutdown confirms that release path has completed. Detector
   selection is resolved once at sequence start and remains pinned.

   Camera lapse rejects scan-driven detectors, cameras that expose an explicit
   external `Trigger source`, a negative interval, an empty sequence, and
   single-file TIFF (the TIFF writer cannot safely reopen one grouped lapse
   container). HDF5/Zarr support one grouped file; all formats support one file
   per timepoint. A scan or unrelated recording active at a due timepoint stops
   the lapse with a visible failure rather than stealing ownership or capturing
   ambiguous data. Stop/close cancels the timer and aborts only the current
   one-frame writer. No recording lease or writer is held during hour-scale
   gaps, and no automatic laser switching is attempted.

   Per-timepoint metadata includes total timepoints, zero-based lapse index,
   requested interval, planned UTC start and actual acquisition start. Legacy
   saved `SpecLapse` state migrates to `CameraLapse`; settings and the
   UI-thread API are persistent but never auto-start acquisition. Focused
   coverage includes validation, exact generation rollover, missed-deadline
   no-burst behavior, zero-interval writer-drain retry, idle-gap stop,
   one-frame metadata, single-file HDF5/Zarr append, and absence of a
   `RECORDING` lease between points.

5. **Enable selection (perf win), feature-gated.** Selection default =
   `forAcquisition`; snapshot from selection ∪ explicit-lease overrides (incl.
   GENERIC); point-detector gate on `scanInfoDict['participants']`; TimeTagger
   stale-`_flim` clear; `setDetectorSelected` under the lock with LIVE_VIEW
   live-swap + queued inter-repeat changes; GUI checkboxes + `APIExport
   setDetectorSelected`.

   **Status: NOT STARTED.** The Phase 1–4.5 software architecture is ready for
   its pull-request gate, not yet for hardware-dependent selection rollout.
   Complete the supported pre-merge lanes and the pre-Phase-5 hardware smoke
   gate below before selection can change real scan output.

## Acceptance gate

R12 adds focused regression coverage for the highest-risk Phase 1–4 ownership,
publication, recording and cleanup boundaries. The last complete supported
headless baseline predates R12; the full lanes are deferred to the
pull-request/pre-merge gate, so this document does not claim a post-R12 broad
run. Criteria 2, 3, 7 and 14 are Phase 5 behavior and remain intentionally
pending. The device-specific variants in criteria 4–6 and 9–13 still require
the hardware gate; software coverage is not a substitute for exercising real
task teardown, trigger timing and device latency.

1. Legacy `startAcquisition`/`stopAcquisition` compat + explicit-purpose acquire;
   empty-iterable rejected; `None`=all-forAcquisition in shim.
2. Deselected point detector excluded from a plain scan.
3. Deselected point detector included when requested by recording/workflow/GENERIC.
4. Graceful SCAN completion while another purpose lease remains (2→1, still drains).
5. TimeTagger final product produced before repeat re-arm.
6. Build failure, exception, abort and normal completion resolve exactly once;
   validate all **five** NI-DAQ entry points and both EtSTED/EtMonalisa modes
   (direct ScanWidget-triggered and RecordingWidget-triggered) on hardware.
7. Queued selection change between repeat iterations applies next iteration.
8. Simulation honors `excludedDetectors` and emits simulated camera frames only
   when the injected runtime lease-state provider says that camera is armed.
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

## Round 8 — implementation review (R8-2 later superseded by R9)

| # | Finding | Resolution |
|---|---------|-----------|
| R8-1 | **Missing async finish barrier.** `resolve()` called `finishScan` synchronously and released the SCAN lease immediately; worse, *no manager overrode `finishScan`* — Phase 3 shipped it as a declared-but-unimplemented hook while claiming the split was done. TimeTagger's final read was therefore never awaited before teardown or repeat re-arm. | `finishScan(mode, acknowledge)`: the token enters a *finishing* state and the lease is held until every participant acknowledges. `SwabianTimeTaggerManager` overrides it, hooking the ack to `sigFrameReady(is_final=True)` — the async path that already existed but nothing waited on. Repeat re-arm gated by deferring `scanDone` until the barrier clears. A **timeout** (default 5 s, injected scheduler) releases anyway rather than wedging the GUI, and a manager that cannot be asked acknowledges at once. |
| R8-2 | **Poll-thread shutdown deadlock.** `onBeforeStops` joined the poll thread while holding the lease lock; since Phase 4 the poll loop calls `frameStreamMembership()`, which needs that same lock. | R8 moved the join outside the lease lock, but incorrectly described its check-then-act race as benign. R9 supersedes that resolution: a dedicated manager lifecycle lock serializes acquire/release/restart around the outside-lock join, handle validation precedes side effects, and a separate poll lock prevents non-last release from stopping hardware under an in-flight stale poll. |
| R8-3 | **Wrong lease purpose for signal-driven EtSnouty.** It always took EVENT_DIRECT, but its `ClockWidefield=False` branch consumes `sigUpdateImage` and so needs EVENT_STREAM; with live view off that mode would arm the detector and stall. A second bug underneath: the lease was acquired *before* `ClockWidefield` was assigned. | Purpose follows the clock mode; acquisition moved after the branch; method renamed `_acquireDetectorFastLease`; source-ordering guard test so it cannot drift back. |
| R8-4 | **Paused EVENT_STREAM lease leaked.** `pauseFastModality` clears `imageSignalConnected` while deliberately keeping the lease, so a release guarded on that flag skipped a paused run and leaked the handle for the session. | Release is unconditional in `_disconnectRunSignals` (the terminal path). Verified the resume path no-ops when the handle is already held, so pause/resume keeps exactly one lease. |

## Pre-hardware audit (software sanity pass)

Checked beyond the unit suite, before any rig time:

- **Contract conformance across every real manager.** Every `DetectorManager`
  subclass is enumerated and its `finishScan` signature verified — no manager
  is left on the old one-argument form, and only the coordinator calls it.
- **End-to-end with real managers** (`mixed_hamamatsu_apd_mock_scan_setup`, real
  `NidaqManager` + `DetectorsManager` + `APDManager`): `isScanDriven` correct;
  participants = `['APD']` only; the SCAN lease arms APD and *not* the camera;
  `APD.acquisition` is True **before** `runScan` (so the `sigScanBuilt` →
  `initiateScan` gate sees it); mirror agrees; participants/excluded reach
  `scanInfoDict`; frame-stream membership empty during a scan; barrier
  resolves, lease released, repeat gets a fresh token; live-view membership is
  the camera alone.
- **Concurrency under real threads** (`test_acquisition_concurrency.py` and
  manager-level frame-stream tests, all deadline-guarded): acquire during
  last-streamer join; non-last release during a blocking hardware poll;
  concurrent period change; invalid-handle side-effect guard; poll loop
  hammering membership against 200 acquire/release cycles; four threads
  churning one detector; off-thread acknowledgements; 50 iterations of
  acknowledgement racing timeout.
- **Non-NI-DAQ backends.** TriggerScope controllers do **not** subclass
  `SuperScanController` (they use `ScanLifecycleMixin`), so they never arm
  through the coordinator. Verified no TriggerScope setup contains a
  scan-driven detector — the only two setups with APD/PMT/TimeTagger
  (`mixed_hamamatsu_apd_mock_scan_setup`, `example_sted`) are both NI-DAQ. The
  `ViewController` narrowing therefore strands nothing.
- **Import smoke** across all touched managers and controllers.
- **Dead state removed.** `LeaseTransition.frameStreamLast` was computed and
  read by nothing after the deadlock fix; it is deleted rather than left as an
  invitation to wire poll-thread shutdown back into `release()`.

### R9 stabilization result

The deeper audit did uncover unanticipated integration defects, but they were
boundary errors rather than a failure of the lease model:

- lease-table concurrency tests did not exercise manager-level thread
  lifecycle, hiding a deterministic lost-poller race;
- per-controller coordinators did not match globally broadcast NI-DAQ
  completion;
- a fault-excluded detector still received global build/start signals;
- final-frame callbacks and close-time owners lacked iteration/operation
  identity.

R9 fixes those boundaries without changing the core model. APD/PMT/TimeTagger
now use `scanInfoDict[PARTICIPANTS_KEY]` as authoritative, APD/PMT reset their
coarse acquisition flag fail-closed, the shared coordinator holds a global
finishing gate, and TimeTagger acknowledgements are generation-safe.

### R10 boundary-hardening result

The R10 audit again supports the architecture rather than a step back: every
new defect was at an ownership/lifecycle boundary, and each could be fixed by
carrying exact identity and waiting for the already-defined terminal barrier.
R10 adds:

- bounded, retryable NI task finalization and strict APD/PMT worker teardown;
  NI remains busy/fail-closed while any task, waiter or simulator worker is
  unresolved;
- generation-gated APD/PMT queued pixel and frame callbacks;
- a detector frame broker with atomic consumer boundaries and explicit
  overflow failure, plus BeadRec read/cleanup serialization;
- transactional recording cleanup: writer-open/write/finalize/stall failures
  abort partial output and never emit ordinary success;
- exact recording-session generations and one terminal per generation; scan
  recording waits for both scan-run release and writer finalization;
- synchronous accepted/rejected scan requests carrying the accepting owner,
  exact run token and a token-scoped request terminal; stale global lifecycle
  signals cannot clear or prematurely complete a newer request;
- one pre-resolved source for automated ScanLapse (and canonical ScanOnce),
  preventing TriggerScope/LightSheet multi-start broadcasts; TriggerScope
  Raster is currently the only standalone source with the full recording
  capability surface, and other standalone modes fail before pre-arm;
- event-triggered RecordingWidget recovery waits for both its exact recording
  generation and the scan-run lifecycle terminal before resuming the fast
  modality;
- owner close/shutdown barriers, fail-closed manager finalization, and explicit
  UI-thread affinity on stage-mutating autofocus/tiling APIs.

### R11 terminal-boundary and shutdown result

One further adversarial pass found no reason to replace the lease/run/iteration
model. It did find more places where a compatibility signal, cached flag, or
return value could say “done” before the exact run, writer, worker, or native
handle was terminal. R11 hardens those paths:

- final exact scan-request success or failure resolves only after the
  participant barrier, SCAN-lease release and `sigScanEnded` publication; R12
  further keeps the global run reservation held through that publication,
  finalizing it immediately before the exact request callback. A request that
  reserved a token and then failed is still reported as accepted/owned and
  carries its exact deferred failure terminal;
- Recording validates a reported token against the coordinator's owner-scoped
  token, retains the real target's abort authority on malformed/raised reports,
  ignores stale global scan terminals, and keeps accepted identity pinned while
  a real scan barrier drains;
- ScanLapse stop aborts both an active writer and its owned scan, targeted
  legacy cadence gaps invoke the pinned source's terminal path, and a manually
  armed standalone ScanOnce can be cancelled without waiting for a scan that
  never started;
- recording writer or finalizer failures retain their worker/writer identity,
  late abort cannot publish a finalized partial output, and TIFF close/metadata
  failures are aggregated rather than converted to success;
- generic detector stops are one serialized, deadline-bounded operation per
  detector. A timed-out native call remains strongly referenced and FAULTED;
  retry joins that operation instead of issuing a concurrent second SDK stop;
- camera managers with cached `_running` state now clear it only after their
  SDK stop succeeds, so a retry really reaches hardware instead of falsely
  clearing quarantine;
- NI one-shot analog/digital tasks are registered before write/wait, use a
  finite DAQmx wait, and close through the same serialized bounded teardown as
  scan tasks. Failure to start a teardown thread restores the exact
  task/generation/waiter for retry;
- server-thread timeout blocks hardware finalization, explicit `False`
  finalizers are aggregated, and successful manager/submanager identities are
  skipped on a partial-shutdown retry while failed or replacement objects are
  retried.

### R12 UI-publication, request-atomicity, and retry-authority result

The newest audit again found integration-boundary races, not a defect in the
lease/run/iteration model. They reduced to three missing invariants: the global
run reservation must outlive terminal publication; workflow preflight, pre-arm
publication and source invocation must form one UI-thread transaction; and an
owner must retain the exact handle or token required to retry failed cleanup.
R12 enforces those invariants:

- after the physical finish barrier clears, the coordinator can hold release
  while the controller publishes `sigScanEnded` on its own thread. It finalizes
  global ownership afterward and only then wakes the exact request completion.
  An end observer therefore cannot reserve another owner ahead of that exact
  terminal;
- scan controllers become the active scan source only after successful run
  reservation, so a losing contender cannot replace the true owner. Shutdown
  readiness also accounts for a published-but-not-ended run start;
- facade starts and non-final continuations use one atomic UI-thread
  `run_scan_prepared` operation covering source resolution, busy and
  continuation validation, global pre-arm publication and source invocation.
  Busy or invalid requests publish no speculative lifecycle, while a
  synchronous rejection or pre-arm failure pairs any already-published end in
  the same transaction;
- targeted abort is fail-closed: when a run token is supplied, the service
  aborts only after verifying it as the selected source's current owned token;
- Recording binds callbacks to the validated terminal instead of trusting
  callback payloads, rejects malformed or stale ownership, resets previous
  terminal state before new setup, and keeps exact abort authority pinned
  across failure. Non-final ScanLapse parts wait for exact-request plus writer
  completion while retaining the run-level lifecycle; final or stopped
  sessions additionally wait for the matching global end. Authoritative state
  cleanup precedes best-effort UI cleanup, and failed synthetic end publication
  remains retryable;
- RecordingWidget-triggered event runs pin the selected scan source, its held
  run token and the exact next recording generation before accepting lifecycle
  completion. EtSnouty likewise requires the source it triggered to transition
  from running to idle. Foreign, stale, pre-capture and post-stop global ends
  therefore cannot resume either fast modality;
- frame-stream owners (live view, event-triggered loops and EtSnouty) clear
  lease handles only after successful release. A last-streamer or poller-stop
  timeout leaves the exact handle available for cleanup or close retry, and a
  restart retries that cleanup rather than mistaking the retained handle for a
  healthy stream.

Software verification on 2026-07-26 established the last complete supported
headless baseline before the R12 changes: the ImControl/no-hardware lane
completed with **1,793 passed, 4 skipped**, and the ImProcess lane completed
with **938 passed**. R12 was subsequently checked with the focused ownership,
terminal-publication, recording-cadence, event-recovery, source-targeting and
release-retry suite: **539 passed**. Per review scope, the two broad lanes were
not rerun after R12; they remain part of the pull-request/pre-merge gate.

Broader application/device concerns remain explicit rather than hidden inside this
feature:

- there is no global cross-controller stage-motion arbiter yet; the current
  changes enforce UI-thread affinity and local cleanup but do not serialize
  arbitrary autofocus/tiling/plugin stage owners. Camera-lapse due-time
  preflight rejects a scan already active on the UI turn, but cannot prevent an
  unrelated stage owner or a scan deliberately started immediately afterwards;
- the top-level multi-module application close path cannot yet present a
  retry/veto after one module refuses close. ImControl now skips sensitive
  manager finalization when scans, recording or leases remain active, but a
  full application-close retry protocol spans modules and belongs in its own
  change;
- an in-process deadline cannot forcibly reclaim a vendor call that ignores
  both its own timeout and a concurrent close. Such an operation remains
  registered and fail-closed, but a hard recovery guarantee requires driver
  reset or process isolation;
- `ThorCamTSIManager.stopAcquisition()` intentionally leaves that camera armed.
  Lease zero therefore means “not participating or polled by ImSwitch,” not
  physical disarm for that adapter. Phase 5 must explicitly accept that
  backend exception or add hardware-validated disarm/re-arm behavior before
  promising physical-off semantics for every camera.

None of these concerns invalidates detector acquisition ownership, but all must
remain visible during hardware validation and before claiming production-wide
shutdown guarantees.

### Hardware smoke gate before Phase 5

On one representative camera + APD/PMT + TimeTagger setup, verify:

1. live-view start/stop and adding/removing an EVENT_STREAM detector while
   polling, plus simultaneous preview/recording/BeadRec broker consumption and
   the explicit slow-consumer overflow path. Force a last-EVENT_STREAM
   poller-stop timeout and confirm the exact owner handle remains retryable and
   shutdown stays incomplete until release succeeds;
2. normal, repeat, abort and busy-refused scans, confirming one final product
   and one completion per iteration;
3. partial NI task-start rollback plus input/output stop/close timeout. A failed
   stop means physical state is **unknown**, not “known idle”: confirm
   quarantine prevents re-arm, run the explicit safe-off retry, and require
   operator confirmation before reuse. Include one-shot analog/digital wait
   timeout and teardown-thread-start failure/retry;
4. recording arm timeout, zero-progress stall, writer-open/write/finalize
   failure and consumer-cleanup failure; each must abort partial output and
   publish failure without a success terminal;
5. etSTED/EtMonalisa with live view off in both ScanWidget and RecordingWidget
   modes, including binary-mask timeout, stop during the slow scan, and
   success/failure recovery only after both terminals;
6. MoNaLISA autoAxial and non-final sequence parts emit one run-level start/end
   pair while final-product draining still occurs per iteration. Confirm a
   non-final part advances after exact-request plus writer completion without
   ending the run, while a final or stopped part waits for its matching
   run-level end;
7. targeted TriggerScope Raster ScanLapse runs only Raster for every
   timepoint; ambiguous or incapable standalone scan sources fail before
   `sigScanStarting` and before recording arms;
8. cross-owner contention and inter-iteration gaps: widget scan, event scan,
   workflow scan and recording request cannot steal or abort one another. A
   `sigScanEnded` observer cannot start owner B while A's terminal publication
   is held, but A's exact callback can start B after finalization; simultaneous
   facade requests produce one accepted owner and no speculative lifecycle
   from the loser;
9. close during an active scan, recording, event experiment, autofocus,
   tiling, BeadRec and EtSnouty leaves no laser, detector, NI task or worker
   active; a blocked generic camera stop returns by its deadline without a
   duplicate SDK call, remains quarantined, and can be retried;
10. qualify the continuously armed ThorCam adapter explicitly: confirm whether
    stopping ImSwitch participation/polling is sufficient, or validate a safe
    physical disarm/re-arm implementation before Phase 5;
11. exercise multi-module application close with one refusing module: confirm
    that ImControl skips sensitive finalization, and document that the
    top-level window still cannot veto process exit until the separate
    application-wide retry/veto flow is designed.
12. run camera-only timelapse on a representative internal-trigger camera:
    confirm frame 0 is immediate, later frames follow their requested cadence,
    no `RECORDING` lease remains between points, stop works both during capture
    and during the idle gap, a concurrent scan/recording fails the due point
    closed, suspend/resume produces no catch-up burst, and HDF5/Zarr grouped
    plus per-timepoint TIFF output carry correct lapse timing metadata.

If this gate passes, proceed with feature-gated Phase 5 rather than redesigning
the ownership architecture.

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
