# Recording data-flow redesign (open tail)

**Status (2026-10-17):** Phase 1 is on main: dtype contract
(`DetectorManager.dtype`/`bitDepth`, storers create datasets from the declared
dtype), silent casts removed, off-thread `WriterThread` with batched chunked
writes, and **sink abort** (`RecordingManager.abortRecording`,
`Storer.abortStream` deletes partial output). Streaming recordings are also
capped by a byte-budget queue (`memory_limits`, `_queueBudgetBytes`).
**Open (deferred by design):** Phase 1.5 `ChunkBroker`/subscription API (no
`ChunkBroker` class exists; `DetectorManager.readChunk` still does the fan-out),
Phase 2 producer-driven pilot (the poll loop in `RecordingManager` still sleeps,
`FRAME_POLL_INTERVAL`), Phase 3 producer-driven default plus a **hard byte cap
for `SaveMode.RAM`** (an `UntilStop + RAM` recording still grows a `BytesIO`
until OOM), and **scan source abort** (`NidaqManager` still has no
`abortScan`; `stopTask` only). All need hardware validation. Maintainer
decision pending on whether Phases 1.5-3 are wanted at all; the RAM cap and
source abort are independent and concrete.


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

### Decision — tightened hybrid (maintainer-approved direction)
"Option E" = an event-driven producer/consumer queue (the writer blocks on the
broker instead of polling `getChunk`). Do **not** go straight to full Option E (rated high risk as a repo-wide
rewrite). The direction is right; the sequencing is tightened to de-risk the
multi-consumer migration:

- **Phase 1 (done):** dtype contract, kill silent casts, batched/uncompressed
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
- **No CI validation** possible (per `docs/no-hardware-validation.md` (retired; see git history)) — must be
  tested on the rig before it can be trusted.
- **TriggerScope is a separate effort** (firmware stop command).

---

