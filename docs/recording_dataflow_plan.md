# Recording data-flow redesign — design doc

Status: **design only, no code yet.** Decision requested from maintainer before
implementation. Scope is the acquisition → recording → disk/RAM path.

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

## Phase 1 work items (priorities: dtype, casts, throughput)

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
- Move compression **off** the acquisition/write thread, or default the
  streaming write path to uncompressed and offer compression as a
  post-process / opt-in (`RecordingManager.py:492`).
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
- Introduce a `ChunkBroker` behind `readChunk` with **explicit per-subscriber
  queues**, defined replay rules, unregister behavior, and a per-consumer
  overflow policy (block vs drop-with-counter — no silent drop).
- **Characterization tests first:** lock in today's fan-out behavior
  (`test_detector_chunk_consumers.py:75`) before changing anything, then refactor
  to the broker keeping those tests green.

## Phase 2 (later) — producer-driven migration (small surface)
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

## Candidate agent (OpenHands) tasks
Run via the `openhands-clean` venv in **headless** mode
(`openhands -f <taskfile> --headless --always-approve`). Sequenced (per repo
memory: single shared checkout, **sequential runs only**, never parallel):

**Phase 1 (now):**
1. Dtype contract (DetectorManager `dtype`/`bitDepth` + storers validate, not
   infer + per-manager dtype).
2. Kill silent casts (APD float→uint16 buffer + recording worker stacking).
3. Throughput (compression off-thread/opt-in + batched chunked writes).

**Phase 1.5+:** ChunkBroker characterization tests, then broker refactor —
handed to agents only after Phase 1 lands and is reviewed.
