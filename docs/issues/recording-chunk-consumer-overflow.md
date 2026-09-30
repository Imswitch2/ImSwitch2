# ChunkConsumerOverflowError on high-frame-rate "Run until STOP" recordings

**Status:** diagnosed against the failing sources, not fixed. Workaround confirmed.
**Component:** `imcontrol` — recording + detector chunk broker
**Verified against:** the actual failing files — `RecordingManager.py` (3387 lines),
`DetectorsManager.py`, `DetectorManager.py` (804 lines). All four traceback frames
(2728 / 3168 / 3351 / 595) resolve exactly. No unknowns remain.

> **Revised.** An earlier draft of this document blamed the live-view bulk hand-off
> against a 1000-frame cap. With `DetectorManager.py` in hand that is wrong in its
> most important detail: **the recorder's cap is 16, not 1000**, and the error
> message that says otherwise is itself a bug. See *Root cause*.

---

## Symptom

Recording in **Run until STOP** mode on a fast camera fails partway through:

```
WARNING [HamamatsuManager -> Red] readChunk consumer "RecordingManager" is registered
        but not polling; dropping its oldest frames (cap 16). The consumer will fail
        rather than accept an incomplete stream; call releaseChunkConsumer when done.
INFO    [RecordingManager] Stopping recording
ERROR   [RecordingWorker] Recording failed: readChunk consumer "RecordingManager" fell
        behind by more than 1000 frames; its stream is incomplete
Traceback (most recent call last):
  RecordingManager.py, line 2728, in run           -> self._record()
  RecordingManager.py, line 3168, in _record       -> newFrames = self._getNewFrames(detectorName)
  RecordingManager.py, line 3351, in _getNewFrames -> ... .readChunk(...)
  DetectorManager.py,  line 595, in readChunk      -> raise ChunkConsumerOverflowError(...)
ChunkConsumerOverflowError: readChunk consumer "RecordingManager" fell behind by more
than 1000 frames; its stream is incomplete
ERROR   [RecordingController] Recording failed: ...
```

The recording aborts and its partial output is removed — by design, a truncated
stream is treated as a failure rather than a short file.

**The log contradicts itself, and the warning is the one telling the truth.**
The cap that was actually applied is **16**. See *Defect 0*.

### Reproducing configuration

| Setting | Value |
|---|---|
| Camera | Hamamatsu C14440-20UP (x2: "Green" and "Red") |
| ROI | X0 1224, Y0 920, **W 76 x H 20** |
| Binning | 1 |
| Exposure | 100.38 us |
| Internal frame interval | 107.09 us |
| **Internal frame rate** | **9338.1 fps** |
| Trigger | Internal |
| Recording mode | Run until STOP |
| **LIVEVIEW** | **on**, both detectors |

---

## Root cause

A cap sized for one kind of detector is being applied to a completely different
one. The recording consumer is allowed **16 queued frames** on a camera running at
**9338 fps** — a stall tolerance of **1.7 milliseconds**.

### The two caps

`DetectorManager.py` defines two, and `_distributeChunkLocked` picks between them
per consumer (`:691`):

```python
cap = (MAX_QUEUED_RAW_FRAMES if kind is ChunkKind.RAW
       else MAX_QUEUED_CONSUMER_FRAMES)
```

| Constant | Value | Applies to |
|---|---|---|
| `MAX_QUEUED_CONSUMER_FRAMES` | 1000 | `ChunkKind.DISPLAY` consumers |
| `MAX_QUEUED_RAW_FRAMES` | **16** | `ChunkKind.RAW` consumers |

**The recorder is a RAW consumer.** `RecordingManager.py:3140` registers it that
way deliberately, and correctly:

```python
startConsumer(_RECORDING_CHUNK_CONSUMER, kind=ChunkKind.RAW)
```

with the reasoning that "a recording wants the measurement, not the picture of
it" — a point detector's display frame is whatever `_linestep_view_mode` reduced
the scan to, which must not decide what gets saved. That is sound.

### Why 16 is fatal here

`MAX_QUEUED_RAW_FRAMES = 16` is justified in its own docstring:

> *a raw consumer receives one frame per completed scan, not one per boundary,
> so falling a thousand behind is not a backlog, it is a leak.*

That is true **for a scan-driven point detector**. It is false for a camera. The
default `drainChunk()` (`DetectorManager.py:548-563`) is explicit about it:

```python
frames = self.getChunk()
return ChunkPayload(display=frames, raw=frames)
```

`raw` **is** `display` — the same object. No detector in the failing setup
overrides `drainChunk`, so for the Hamamatsu there is **one raw frame per camera
frame**, not one per scan. The premise the constant rests on does not hold, and
the cap intended to catch a slow leak becomes a 1.7 ms deadline.

| Frame rate | 16 frames buys | 1000 frames would buy |
|---|---|---|
| 100 fps | 160 ms | 10 s |
| 1000 fps | 16 ms | 1 s |
| **9338 fps** | **1.7 ms** | 107 ms |

This is the whole rate dependence. At ordinary rates a writer stall never
survives 160 ms, so the cap is never reached and nobody notices it is three
orders of magnitude too small.

### The failure, step by step

1. The writer thread cannot drain fast enough, so `enqueue_frames` **blocks** —
   intended backpressure.
   `RecordingManager.py:2736` (call site), `:2186` (`self._queue.put(..., timeout=0.1)`).
2. While blocked, the record loop is not calling `readChunk`, so frames accrue in
   the hardware queue. At 107 us each, **17 frames accrue in 1.8 ms**.
3. The next drain — by *anyone* — fans that backlog into every registered
   consumer queue, the recorder's included.
4. The recorder's queue exceeds 16. Its oldest frames are discarded, it is added
   to `_chunkConsumersOverflowed`, and the "registered but not polling" warning
   fires with the correct `cap 16` (`:691-706`).
5. The recorder's next `readChunk` sees the flag and raises (`:594`) — with the
   **wrong** number in the message.

### The live view is an amplifier, not a prerequisite

The earlier draft treated the live-view poll thread as the mechanism. With the
real cap known, it is not required for the failure — note the ordering inside
`readChunk` (`:588-599`):

```python
self._distributeChunkLocked(self.drainChunk())     # may overflow the caller
if consumerKey in self._chunkConsumersOverflowed:  # ...checked immediately after
    raise ChunkConsumerOverflowError(...)
```

The recorder's **own** drain distributes into its **own** queue before the check.
So a 1.8 ms stall is sufficient on its own: the recorder resumes, drains 17
accrued frames into a 16-slot queue, and raises on the very call that fetched
them. No second consumer needed.

`LVWorker` (in `DetectorsManager`, `updatePeriod = 300 ms` from
`MasterController.py:59`) still makes it far likelier and far worse:

```
camera --> DetectorManager consumer queue --> record loop --> writer queue --> disk
             cap 16 FRAMES (RAW)                               cap 64 CHUNKS
                  ^
                  | LVWorker poll thread, every 300 ms, drains ALL accrued
                  | hardware frames and fans them into EVERY consumer queue
                  | -- including the stalled recorder's
```

It contributes three things: CPU/GIL contention that makes writer stalls more
frequent; asynchronous bulk hand-offs that inflate the recorder's queue between
its own polls; and a hand-off size of **2,801 frames** per tick at 9338 fps —
**175x** the cap of 16. That is why switching it off is an effective workaround
even though it is not the underlying defect.

### Defects, in order of consequence

**Defect 0 — the error message reports the wrong cap.** The raise at `:596`
hardcodes `MAX_QUEUED_CONSUMER_FRAMES` regardless of the consumer's kind, so a
RAW consumer that exceeded 16 is reported as having "fallen behind by more than
1000 frames". This is why the log is self-contradictory, and it is what sent the
first pass of this analysis down the wrong path. Cheapest fix in the file,
disproportionate diagnostic value.

**Defect 1 — `MAX_QUEUED_RAW_FRAMES` is sized on an assumption that does not
hold for cameras.** Discussed above. This is the actual cause.

**Defect 2 — the writer queue is bounded by *chunk count*, not *frame count*.**
At a 0.1 ms poll with a 107 us frame interval each chunk holds roughly one frame,
so `WRITER_QUEUE_MAXSIZE = 64` buys about **7 ms** of buffering rather than the
seconds that constant implies. Any disk or compression hiccup longer than ~7 ms
stalls the acquisition loop — and with a 1.7 ms consumer tolerance downstream,
every such hiccup is fatal.

**Defect 3 — one live-view tick can exceed either cap on its own.** 2,801 frames
per 300 ms tick at this rate: 175x the RAW cap, and still 2.8x the DISPLAY cap.
Raising the RAW cap to 1000 would not by itself be enough.

### Relevant constants

| Constant | Value | Location |
|---|---|---|
| `FRAME_POLL_INTERVAL` | `0.0001` s | `RecordingManager.py:37` |
| `WRITER_QUEUE_MAXSIZE` | `64` **chunks** | `RecordingManager.py:44` |
| `WRITE_BATCH_FRAMES` | `32` frames | `RecordingManager.py:45` |
| `MAX_QUEUED_CONSUMER_FRAMES` | `1000` frames | `DetectorManager.py:75` |
| **`MAX_QUEUED_RAW_FRAMES`** | **`16` frames** | **`DetectorManager.py:82`** |

Derived for 9338 fps x 2 detectors:

| Quantity | Value |
|---|---|
| Frame interval | 107 us |
| Frames returned per `readChunk` | **~1** (0.1 ms poll < 107 us frame interval) |
| **Recorder stall tolerance** | **16 frames = 1.7 ms** |
| Writer-queue buffering | 64 chunks x ~1 frame = 64 frames = **6.9 ms** |
| Write calls | 584/s, 97 kB each |
| Aggregate data rate | ~57 MB/s |

Note the inversion: the writer is allowed to stall for 6.9 ms, but the consumer
downstream of it tolerates only 1.7 ms. The system cannot use its own buffer.

---

## Confirmed workaround

**Turn LIVEVIEW off while recording at these rates.** Verified by the reporter to
resolve the failure.

Why it works: it removes the CPU contention that provokes writer stalls and the
bulk hand-offs that inflate the recorder's queue between its own polls. It does
not raise the writer's ceiling and it does not fix the cap — a long enough disk
hiccup will still exceed 1.7 ms with live view off.

---

## Proposed fixes

### 1. Report the cap that was actually applied

`DetectorManager.py:594-599`. Use the consumer's own cap and name its kind:

```python
kind = (self._chunkKinds() or {}).get(consumerKey, ChunkKind.DISPLAY)
cap = MAX_QUEUED_RAW_FRAMES if kind is ChunkKind.RAW else MAX_QUEUED_CONSUMER_FRAMES
raise ChunkConsumerOverflowError(
    f'readChunk consumer "{consumerKey}" ({kind.value}) fell behind by more '
    f'than {cap} frames; its stream is incomplete'
)
```

Do this first regardless of what else is decided. It is a few lines, it cannot
regress anything, and without it the next person to hit this reads a message that
is off by a factor of 62.

### 2. Make the raw cap follow the detector's raw cadence, not the kind label

The real fix. `MAX_QUEUED_RAW_FRAMES` is small because a scan-driven detector
emits one raw frame per completed scan; a camera emits one per frame. The cap
should be a property of the detector, not of the enum member.

Concretely: give `DetectorManager` a `maxQueuedRawFrames` property defaulting to
`MAX_QUEUED_CONSUMER_FRAMES` — correct for every camera, which does not override
`drainChunk` — and let a detector that *does* override `drainChunk` to reduce
something lower it to 16. That inverts the default so the surprising value is
opt-in, at the same place the raw/display distinction is actually made.

A `frames_per_raw_unit` hint on the payload would work too, but the property is
smaller and needs no protocol change.

### 3. Do not let a non-recording consumer inflate the recorder's queue

Still unimplemented, and still the most principled fix. Note that `ChunkKind`
does **not** already do this: it separates which *representation* each consumer
receives, while `_distributeChunkLocked` (`:680-690`) continues to fan every
drain out to every registered consumer regardless of who drained. Live view only
needs the newest frame — which `_distributeChunkLocked` already latches into
`self.__image` before the fan-out loop — so a display path that refreshes from
that latch without extending other consumers' queues removes this failure mode at
the source, independent of frame rate and period.

### 4. Bound the writer queue by frames, not chunks

`WRITER_QUEUE_MAXSIZE = 64` was evidently sized on the assumption that chunks
carry many frames. Replace the `queue.Queue(maxsize=64)` with a frame-counted
bound, so buffering depth is what it claims to be regardless of poll cadence or
frame rate. This shortens the stalls; it does not change what happens when one
occurs, so it complements (2) rather than substituting for it.

### 5. Poll less often, so chunks are fat

A **slower** `FRAME_POLL_INTERVAL` makes each `readChunk` return many frames, so
every writer-queue slot carries many and per-chunk overhead drops:

| Poll interval | Frames/chunk @ 9338 fps | Writer buffer |
|---|---|---|
| 0.1 ms (current) | ~1 | 6.9 ms |
| 1 ms | ~9 | 64 ms |
| 5 ms | ~47 | 320 ms |
| 10 ms | ~93 | 640 ms |

Keep it well under the consumer-side slack — which, until (2) lands, is 1.7 ms.
This one is actively dangerous before (2): a 5 ms poll interval guarantees ~47
frames per drain into a 16-slot queue and would fail immediately.

### Also worth doing

**Surface the stall.** The recorder blocking in `enqueue_frames` is silent today.
A warning when a producer blocks for more than a few milliseconds would have made
this diagnosable without reading the source.

---

## Verification of the analysis

Checked directly against the failing sources; nothing outstanding.

- Traceback lines **2728** (`self._record()`), **3168**
  (`self._getNewFrames(detectorName)`), **3351** (`readChunk(...)`) and **595**
  (`raise ChunkConsumerOverflowError`) all resolve exactly.
- `FRAME_POLL_INTERVAL = 0.0001`, `WRITER_QUEUE_MAXSIZE = 64`,
  `WRITE_BATCH_FRAMES = 32` — identical to the analysed branch.
- `enqueue_frames` still blocks on a full queue (intended backpressure).
- `_distributeChunkLocked` still fans out to all consumers (`:680-690`).
- The recorder registers as `ChunkKind.RAW` (`RecordingManager.py:3140`).
- No `drainChunk` override exists for the camera, so `raw is display`.

The supplied branch is newer than the one first analysed — `RecordingManager.py`
is 3387 lines vs 2941 and adds `StreamPayloadInfo`, `FailureKind`,
`PayloadLocator` and snap-preview methods; `DetectorManager.py` adds
`ChunkKind`, `ChunkPayload`, `drainChunk` (replacing bare `getChunk` in the
broker) and `_chunkKinds`. The `ChunkKind` work is what introduced
`MAX_QUEUED_RAW_FRAMES`, so this failure is **specific to the newer branch**.
None of the other additions touch the mechanism.

---

## Suggested verification

Existing coverage is in
`imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py`; the
registered-but-not-polling cap is already exercised there, which is the exact
mechanism at fault.

Three tests are worth adding:

1. **The cap a camera raw consumer actually gets.** Register a consumer with
   `kind=ChunkKind.RAW` on a detector that does *not* override `drainChunk`,
   distribute 17 frames, and assert it has **not** overflowed. This fails today
   and is the regression test for fix (2).
2. **The message matches the cap.** Overflow a RAW consumer and assert the
   raised text names 16 (or whatever the applicable cap is), not 1000. Fix (1).
3. **The stall interaction.** Register a "recording" consumer and stop polling it
   (simulating a blocked writer); drive frames through `getLatestFrameShared()` —
   the live-view path — and **not** through `readChunk`; assert the recorder's
   next `readChunk` raises. Then assert the same scenario survives a stall of
   realistic duration once fixed. Fix (3).

A writer-throughput benchmark (sustained frames/s into the storer for a small ROI
at ~9 kfps, per format) is also worth having: the caps only buy headroom, they do
not raise the writer's ceiling.
