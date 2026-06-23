# Recording data-flow redesign — design doc

Status: **Phase 1 implemented and merged to main (2026-06-15).** Dtype contract,
silent-cast removal, off-thread writer, and recording sink-abort are done; see
the per-section status below and the ROADMAP (Milestone 10). Phases 1.5 / 2 / 3
and scan source-abort remain designed-but-deferred. Scope is the
acquisition → recording → disk/RAM path.

## Goals (from maintainer)

1. Clean data flow with **known data types** end to end (cameras/APDs are
   integer devices; a manager should, ideally, declare its dtype/bit depth at
   runtime).
2. **No casting** to other dtypes anywhere in the acquisition path.
3. **No RAM-overflow danger.**
4. **Maximum acquisition rate** — efficient I/O, no avoidable copies.
5. **No `time.sleep`** in acquisition code.
6. Fix architectural workarounds a good developer would flag.

Maintainer priority for first implementation phase: **dtype contract**,
**kill silent casts**, **max throughput**. RAM-overflow handling is accepted as
a known item scheduled for a later phase.

---

## Current data flow (as built)

```
hardware driver
  → DetectorManager.getChunk()            # native dtype, destructive read
  → DetectorManager.readChunk(consumer)   # multi-consumer fan-out, under a Lock
  → RecordingWorker._record() poll loop   # busy-poll + time.sleep(1e-4)
      → np.array(frames)                   # copy per chunk
      → Storer.writeFrames()              # lazy dataset, dtype from first frame
          → HDF5/Zarr (gzip+shuffle, per-frame chunk, resize per chunk) | TIFF | BytesIO(RAM)
```

Key files:
- `imswitch/imcontrol/model/managers/RecordingManager.py` — `RecordingWorker._record`
  (loop), `Storer` subclasses (HDF5/Zarr/TIFF).
- `imswitch/imcontrol/model/managers/detectors/DetectorManager.py` — `getChunk`,
  `readChunk`, `MAX_QUEUED_CONSUMER_FRAMES`.
- Per-device managers (`APDManager`, `PMTManager`, `HamamatsuManager`,
  `GXPIPYManager`, `SwabianTimeTaggerManager`, …).

---

## Findings (evidence)

### A. `time.sleep` is architectural, not cosmetic
`RecordingManager.py:1164` sleeps `FRAME_POLL_INTERVAL = 1e-4 s` every loop
iteration. The recorder polls `readChunk`, which polls each manager's
`getChunk`. There is no blocking handoff (queue/condition) from producer
(driver) to consumer (writer). The sleep is a band-aid over a missing
producer/consumer mechanism.

### B. RAM mode is unbounded
`SaveMode.RAM` writes into a `BytesIO` that grows for the entire recording
(`RecordingManager.py:1012`). `UntilStop + RAM` grows until OOM. No cap, no
backpressure.

### C. No declared dtype / bit-depth contract
No `DetectorManager.dtype` (or bit-depth) property exists. Storers infer dtype
from the **first** frame (`frames.dtype`, `RecordingManager.py:614`) and never
validate later frames. A driver that changes dtype mid-stream is silently cast
on `dataset[...] = frames`.

### D. Silent casts that exist today
- APD: `samples_to_pixels()` returns **float**; assigned into a **uint16**
  buffer at `APDManager.py:170` → silent float→uint16 truncation. (Photon-count
  values are integral, so values survive, but the dtype path is implicit and
  fragile.)
- `np.array(newFrames)` (`RecordingManager.py:1195`) copies every chunk and
  could promote dtype if frame dtypes ever differ within a chunk.

### E. Throughput limiters
- gzip + shuffle compression runs **on the write thread**
  (`RecordingManager.py:492`) — caps sustained MB/s.
- Per-frame chunks `(1, Y, X)` plus a `resize()` per chunk → many tiny writes,
  high per-write overhead.
- `readChunk` holds a `Lock` around the hardware `getChunk()`
  (`DetectorManager.py:323`), serializing consumers and the driver read.

### F. Consumer queue silently drops frames
`MAX_QUEUED_CONSUMER_FRAMES = 1000` (`DetectorManager.py:329`) drops oldest
frames for a registered-but-not-polling consumer — data loss presented as a
memory safeguard, with no backpressure.

---

## Decision 1 — Poll loop vs event-driven

### Option P: keep poll loop, harden it
Keep `RecordingWorker._record` polling but: replace `time.sleep` with a short
blocking wait on a per-detector `threading.Event`/`Condition` that the manager
sets when a chunk is ready; fall back to a bounded wait for the watchdog.

- **Effort:** low–medium. Localized to `RecordingWorker` + a wake signal on
  `DetectorManager`.
- **Risk:** low. Behavior stays close to today; watchdog/stop semantics
  unchanged.
- **Wins:** removes the busy sleep; modest latency/CPU improvement.
- **Misses:** still pull-based; per-chunk copy and lock-around-getChunk remain.

### Option E: event-driven producer/consumer queue
Driver/manager pushes chunks; the writer thread blocks on a queue `get()`.
`time.sleep` disappears entirely.

- **Effort:** high.
- **Risk:** **high** (revised — not medium-high) if done repo-wide in one pass.
  The risky part is *not* the writer blocking on `queue.get()`; it is
  **preserving current acquisition semantics** across recording, BeadRec,
  workflow consumers, stop/watchdog behavior, and heterogeneous detectors.
- **Wins:** correct architecture, max throughput, no sleep.
- **Misses / caveats (from review):**
  - **Multi-consumer semantics are the hard part, and a single bounded queue is
    not enough.** `readChunk` exists *specifically* because destructive reads
    caused recording/BeadRec to split frames; it now fans every chunk to every
    registered consumer (`DetectorManager.py:303`, behavior pinned by
    `imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py:75`). Option
    E must define **explicit per-subscriber queues, replay rules, unregister
    behavior, and a per-consumer overflow policy** — not one shared queue.
  - **Backpressure does NOT, by itself, solve RAM overflow.** A bounded in-flight
    queue caps frames *in transit*, but `SaveMode.RAM` still writes the whole
    recording into a growing `BytesIO` (`RecordingManager.py:1008`). `UntilStop +
    RAM` still OOMs without a **separate hard RAM cap**. These are two different
    limits.
  - **Heterogeneous producer models.** Cameras are pull wrappers around SDK
    calls (`getFrames()` `HamamatsuManager.py:89`, `getLastChunk()`
    `GXPIPYManager.py:124`, `poll_frame()` `PhotometricsManager.py:80`); scan
    detectors already emit `sigNewFrame` on frame boundaries
    (`APDManager.py:266`). A push contract fits the scan detectors but needs a
    pull→push adapter for cameras.
  - **Option E does not automatically remove copies.** APD/PMT return `.copy()`
    of display buffers (`APDManager.py:232`, `PMTManager.py:302`); multi-consumer
    fan-out may need shared immutable arrays, ref-counting, or copies depending
    on lifetime guarantees. "Zero-copy" is a separate, deliberate effort.

### Option P caveat (from review)
A wait-on-event version of the poll loop is easy for APD/PMT/Swabian-style
detectors (they have a natural frame-ready signal), but **cameras do not emit a
frame-ready signal today**. Option P must therefore be defined as **"event where
available, bounded wait/poll adapter where not"** — otherwise it either keeps
hidden polling or silently turns into camera-specific work.

### Decision — tightened hybrid (maintainer-approved direction)
Do **not** go straight to full Option E (rated high risk as a repo-wide
rewrite). The direction is right; the sequencing is tightened to de-risk the
multi-consumer migration:

- **Phase 1 (now):** dtype contract, kill silent casts, batched/uncompressed
  streaming writes. (Poll loop stays; `time.sleep` removal deferred to keep
  Phase 1 low-risk.)
- **Phase 1.5:** introduce a **`ChunkBroker` / subscription API behind
  `readChunk`** — explicit subscriber queues, replay/unregister rules,
  per-consumer overflow policy — with tests that reproduce **today's** fan-out
  behavior (`test_detector_chunk_consumers.py`) before any behavior changes.
- **Phase 2:** migrate **one mock detector, one camera manager, one scan
  detector** to producer-driven mode (proves both producer models +
  pull→push adapter on a small surface).
- **Then** make Option E the default path repo-wide.
- **RAM safety is tracked as its own item** (hard byte cap for `SaveMode.RAM`),
  independent of queue backpressure.

---

## Phase 1 work items (priorities: dtype, casts, throughput) — ✅ DONE (merged 2026-06-15)

### 1. Dtype contract
- Add `DetectorManager.dtype` property (abstract or default), e.g. returning a
  `numpy.dtype`. Camera managers return the native sensor dtype
  (`uint8`/`uint16`); scanned managers return their buffer dtype
  (APD `uint16`, PMT/Swabian `float32`).
- Optional companion: `bitDepth` (int) where the device reports it; default
  derived from dtype.
- Storers: on `writeFrames`, **assert** `frames.dtype == detector.dtype`
  (log + raise, no silent cast). Use the declared dtype to create the dataset
  instead of inferring from the first frame (removes the "first frame defines
  everything" fragility).

### 2. Kill silent casts
- APD: make `samples_to_pixels` / the buffer dtype consistent. Either keep
  photon counts as an integer dtype throughout, or make the buffer match the
  computed dtype — no implicit float→uint16 assignment (`APDManager.py:170`).
- Recording worker: avoid `np.array(newFrames)` re-typing; if frames are
  already arrays of the declared dtype, stack with an explicit `dtype=` and
  `copy=False` where possible (`RecordingManager.py:1195`).
- Audit `PhotometricsManager` `np.array(...)` wraps and the Swabian float
  chains for any unintended promotion in the *recorded* path (display-only
  conversions are out of scope).

### 3. Max throughput
- **Decision (maintainer):** keep compression as the **default** (preserve
  on-disk file sizes) but move it **off the acquisition thread** — compress in a
  separate writer thread / queue so it never blocks frame intake
  (`RecordingManager.py:492`). Do NOT switch the default to uncompressed.
- Batch HDF5/Zarr writes: accumulate N frames (or T-sized chunks) and write/
  resize once per batch instead of per frame; set chunk shape to
  `(C, Y, X)` with C>1.
- Reconsider holding the `Lock` across the hardware `getChunk()` in
  `readChunk` — drain under lock, copy out, release before heavy work
  (`DetectorManager.py:323`).

### Sanity checks for Phase 1
- Existing recording unit tests pass (HDF5/Zarr/TIFF snap + stream).
- Recorded dtype on disk matches `detector.dtype` for camera + APD + PMT.
- No dtype-mismatch assertions trip in a normal scan-once and SpecTime run.
- Throughput: sustained frames/s on a mock high-rate detector improves vs
  baseline (uncompressed batched write).

---

## Phase 1.5 (later) — ChunkBroker / subscription API
Quick audit conclusion: this phase is **medium effort / moderate risk** if it
is scoped as a broker shim behind today's `readChunk`, and **high effort /
high risk** only if it tries to become the full producer-driven Option E rewrite.
Treat those as two separate deliverables.

### Phase 1.5a — broker shim behind `readChunk`
- **Effort:** ~2-4 focused days, or ~3-5 days including review and focused
  regression tests.
- Extract today's broker-like fan-out from `DetectorManager.readChunk()` into a
  formal `ChunkBroker` class, preserving current behavior first:
  - destructive detector `getChunk()` is still drained by the compatibility
    wrapper;
  - every published frame is fanned out to every registered consumer;
  - interleaved consumers do not steal frames from each other;
  - `releaseChunkConsumer()` stops retaining frames for that consumer.
- Required broker API shape:
  - `subscribe(key, max_frames=MAX_QUEUED_CONSUMER_FRAMES,
    overflow="drop_oldest")`;
  - `publish(frames)`;
  - `read(key) -> list[np.ndarray]`;
  - `wait_read(key, timeout=None) -> list[np.ndarray]` or an equivalent
    subscription object with a blocking drain method;
  - `release(key)`;
  - stats/introspection for queued frames, dropped frames, last publish time,
    and active subscribers.
- Overflow must be explicit and per-consumer. A slow live-view/reconstruction
  consumer should not silently endanger recording, and one shared bounded queue
  is still wrong because consumers would steal frames from each other.
- Keep `DetectorManager.readChunk(consumerKey)` as the compatibility API while
  migrating callers. It should drain `getChunk()`, call `broker.publish(...)`,
  and return `broker.read(consumerKey)`.
- **Characterization tests first:** keep the current fan-out tests
  (`test_detector_chunk_consumers.py:75`) green, then add direct broker tests for
  subscribe/read/release, per-consumer overflow counters, and blocking wait
  timeout/wakeup behavior.

### Phase 1.5b — live-reconstruction consumer
- **Effort:** +1-2 focused days after the broker exists.
- Add a `ChunkBrokerLiveSource` (or equivalent live-source adapter) that
  subscribes to one or more detector brokers and exposes frames to the live
  reconstruction pipeline.
- Initial implementation may still rely on the existing `readChunk` compatibility
  wrapper or recording/detector polling path to publish frames. This is useful
  for live reconstruction, but it does **not** by itself remove acquisition
  polling.
- Acceptance criteria: recording, BeadRec, workflow facade, and live
  reconstruction can subscribe independently without frame stealing; slow live
  reconstruction consumption reports drops/stats without breaking recording.

### Phase 1.5c — producer-driven readiness
- Add the blocking wait/condition plumbing needed for producer-driven detectors,
  but do not require every detector to use it yet.
- Document the first migration targets before implementation: one mock detector,
  one camera manager, and one scan detector. This keeps the next phase bounded
  and proves both producer models.
- Do not claim `time.sleep` is removed repo-wide until Phase 2 migrates real
  producers to `broker.publish(...)`.

## Phase 2 (later) — producer-driven migration (small surface)
- **Effort:** ~1-2+ weeks, high risk without hardware validation.
- Migrate exactly three detectors to producer-driven mode: one **mock**, one
  **camera** (needs a pull→push adapter — no frame-ready signal today), one
  **scan** detector (already emits `sigNewFrame`).
- Writer blocks on the broker; `time.sleep` removed on the migrated path.
- Validate against existing semantics (recording, BeadRec, stop/watchdog) on the
  migrated detectors before widening.

## Phase 3 (later) — Option E default + RAM cap
- Make producer-driven the default path once Phase 2 is proven per device.
- **Independent RAM-safety item:** `SaveMode.RAM` gets a hard byte cap with a
  documented policy when exceeded (this is NOT solved by queue backpressure —
  see Decision section).

---

## Recording abort

Abort decomposes into two independent halves:

### Sink abort — DONE (Phase 1)
Stopping the recording and discarding the partial output. Implemented on top of
the off-thread writer:
- `RecordingManager.abortRecording()` sets an abort flag + clears `record`; the
  acquisition loop stops at its next iteration.
- `WriterThread.abort()` sets an abort event, drains the queue (discarding queued
  frames), and calls `Storer.abortStream()` instead of `finalizeStream()`.
- `Storer.abortStream()` (HDF5/Zarr/TIFF) closes handles and **deletes the
  partial file(s)/store(s)**, including TIFF >4GB rollover parts.
- Fully testable, in the default path: free-running cameras
  (SpecFrames/SpecTime/UntilStop) are now cleanly abortable with no truncated
  file left behind.

### Source abort — DESIGNED, NOT IMPLEMENTED (hardware-gated follow-up)
Stopping the acquisition *source*. Trivial for free-running cameras (just stop
acquisition); the hard case is **scan-driven** detectors.

Findings from the scan-stack investigation:
- The existing `abortScan` (base/TriggerScope controllers) is a **no-op while a
  scan is running** — it only cleans up a scan that never started.
- `NidaqManager` has **no method to stop a running scan**: `runScan` starts the
  AO/DO/timer tasks and the only teardown is `WaitThread`s blocking on
  `wait_until_done(WAIT_INFINITELY)` until the precomputed waveform completes.
- The scaffolding that DOES exist: `sigAbortScan` (wired to every scan
  controller), the detector-side cooperative stop (APD/PMT `ScanWorker` checks a
  `scanning` flag at each line boundary — already used by `stopAcquisition`), and
  `NidaqManager.stopTask`.

Design for a cooperative, line-boundary scan abort:
1. New `NidaqManager.abortScan()`: stop AO/DO/timer tasks and unblock/clean up
   their `WaitThread`s; coordinate ordering so the `ScanWorker`'s pending
   `readInputTask` returns.
2. Set the detector `ScanWorker.scanning = False` so it ends at the next line.
3. Wire the already-present `sigAbortScan` to call the above when `isRunning`.
4. Combine with sink-abort for a full "abort everything".

Risks / why it is hardware-gated:
- **Galvo safety:** cutting AO mid-trajectory leaves mirrors at an arbitrary
  voltage; a safe abort needs a return-to-center ramp, not a hard stop.
- **stop-vs-wait race:** stopping/closing tasks while a `WaitThread` is in
  `wait_until_done` is racy in nidaqmx (may raise/hang).
- **No CI validation** possible (per `docs/no-hardware-validation.md`) — must be
  tested on the rig before it can be trusted.
- **TriggerScope is a separate effort** (firmware stop command).

---

## Candidate agent (OpenHands) tasks
Run via the `openhands-clean` venv in **headless** mode
(`openhands -f <taskfile> --headless --always-approve`). Sequenced (per repo
memory: single shared checkout, **sequential runs only**, never parallel):

**Phase 1 — ✅ done (OpenHands, merged 2026-06-15):**
1. ✅ Dtype contract (DetectorManager `dtype`/`bitDepth` + storers create from
   declared dtype + warn-on-mismatch + per-manager dtype).
2. ✅ Kill silent casts (APD float→uint16 buffer + recording worker stacking).
3. ✅ Throughput (compression off-thread on a `WriterThread` + batched chunked
   writes). Sink-abort added on top, implemented directly (not via agent).

Lessons for future agent runs: steer agents away from timing-sensitive
concurrency *integration* tests (they thrash and risk weakening the impl); add
`pytest-timeout` to CI so a hang fails loudly instead of running for minutes.

**Phase 1.5a — ChunkBroker shim:**
1. Characterization/contract agent: strengthen
   `test_detector_chunk_consumers.py` around current fan-out semantics,
   release behavior, idle-consumer overflow, and recording/workflow coexistence.
2. Broker implementation agent: extract the queue/fan-out logic into a
   `ChunkBroker` with `subscribe`, `publish`, `read`, `wait_read`, `release`,
   and stats, keeping `DetectorManager.readChunk()` as the compatibility
   wrapper.
3. Regression agent: run focused detector/recording tests, look for consumers
   that still call destructive `getChunk()` directly, and verify no frame
   stealing between recording, BeadRec, and workflow facade.

**Phase 1.5b — live-reconstruction consumer:**
1. Add a `ChunkBrokerLiveSource` adapter that subscribes to broker queues without
   changing the detector producer model.
2. Validate slow-consumer/drop-stat behavior so live reconstruction cannot
   silently harm recording.

**Phase 2 — producer-driven pilot:** use separate worktrees if available;
otherwise run OpenHands agents sequentially in the shared checkout. Split the
mock, camera pull→push adapter, and scan-detector migration into separate
agents/reviews so hardware-specific risk stays isolated.
