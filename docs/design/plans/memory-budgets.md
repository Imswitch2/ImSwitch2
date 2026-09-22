# Memory limits: buffering and automatic work, not what can be measured

*Status: **implemented** on `feat/memory-limits` (stacked on PR #29), after
review rounds 3 and 4 (2026-09-21). Round 3 reversed the direction rounds 1
and 2 had taken; round 4 accepted that direction and corrected its claims.
Written out of the question the magic-number audit left open — the audit
replaced frame counts with byte budgets, and the byte budgets were literals in
the source. Phase A (the regression, `d648967d`) landed on PR #29 itself;
phases A′, B, C and D are on the stacked branch — see *Implementation* — with
one deviation from the reviewed text, recorded there. Records of all four
rounds at the end.*

## The principle

**Memory policy controls buffering and automatic work. It does not define
which acquisition data the system supports.**

Every rule below is tested against that sentence. Rounds 1 and 2 did not have
it, and each rule they added to make a process-wide cap *true* failed it in one
of three ways: it restricted what could be measured (an indivisible frame
refused at arm, a module refused at startup), it changed scientific output (a
preview computed at a spatial stride), or it reduced stall tolerance in the
name of exact accounting (reservations held longer). Round 3's correction was
that the better response to "the cap is not true" is to narrow the claim, not
to widen the accounting.

## The question

> Couldn't `WRITER_QUEUE_MAX_BYTES` be a global parameter, editable under
> preferences, to optimize for good and less good hardware? The same could be
> useful for ImProcess to specify VRAM (default 1 GB)?

Yes: three settable limits, each named for the one thing it bounds. No: not a
total, because ImSwitch cannot promise one (see *What no setting can claim*),
and not VRAM yet, because nothing could obey it (see *VRAM*).

## What the two acquisition numbers mean together

A recording survives a disk or compression stall for as long as the frames it
cannot yet write have somewhere to sit. Those places are, in order:

1. the **writer queue** (`WRITER_QUEUE_MAX_BYTES`, 512 MiB): the acquisition
   loop blocks on `enqueue_frames` once it is full — graceful backpressure;
2. the chunk in the worker's hands, drained but not yet admitted;
3. the **detector chunk queue** (`MAX_QUEUED_CONSUMER_BYTES`, 256 MiB, per
   `(detector, consumer)`): while the acquisition loop is blocked it is not
   calling `readChunk`, so the recording's own queue grows behind it. When
   *that* overflows the consumer is marked overflowed and its stream is
   declared incomplete — the recording fails.

So the tolerance is **additive**, and the split decides only *how much of it is
graceful*. That is documentation for the operator setting the two numbers, not
an invariant: a smaller writer queue backed by a larger detector queue absorbs
a short stall perfectly well, and any split fails a long enough one.

The historical defect that motivated bytes at all: the writer queue held 64
*items*, about 64 frames — 7 ms on a fast camera, not the seconds the number
suggested. The unit was wrong, and the fix was to state the buffer in the unit
that bounds memory.

## What is exposed

Three limits, each named for what it bounds, each defaulting to today's
literal. No pooled total, no module shares, no percentages.

```
writerQueueMB          = 512   # backlog the writer may hold before the
                               # acquisition loop blocks
perDetectorQueueMB     = 256   # backlog any one (detector, consumer) queue may
                               # hold before that consumer's stream is declared
                               # incomplete
processingWorkingSetMB = 256   # working set ImProcess may spend on automatic
                               # work: contrast sampling, the mean preview
```

| Setting | Replaces | Today's literal |
|---|---|---|
| `writerQueueMB` | `WRITER_QUEUE_MAX_BYTES` | 512 MiB |
| `perDetectorQueueMB` | `MAX_QUEUED_CONSUMER_BYTES` | 256 MiB, per queue |
| `processingWorkingSetMB` | `_SAMPLE_WORKING_SET_BYTES`, and bounds `getMeanData` | 256 MiB; preview unbounded |

The defaults are fixed literals, not fractions of physical RAM: every machine
starts from the same numbers, a bug report quotes values that mean the same
thing everywhere, and the defaults can be written in a document instead of
being discovered. A machine that wants something else says so.

### What no setting can claim

Round 3 verified that no number ImSwitch declares is a process limit, and the
note no longer pretends otherwise:

- `HamamatsuCameraMR.startAcquisition` allocates
  `2 * int(4 GiB / (2 * frame_bytes))` user buffers — **about 4 GiB** on
  every Hamamatsu rig, before a single frame is queued anywhere this note can
  see. (The comment beside it says 2 GB; the arithmetic says 4 GiB.)
- Retained datasets, reconstruction results and viewer layers in ImProcess
  are the data; they are as large as the data is.
- Chunk consumers register **dynamically** — BeadRec, tiling, autofocus and
  workflows each `startChunkConsumer` when they begin — so a queue count taken
  at startup is not a ceiling, and the "worst case" line rounds 1 and 2 wanted
  to log would have been wrong the moment a workflow started.

Both modules do load into one process (`__main__.py`, one `MultiModuleWindow`
under one `QApplication`), and that fact still matters for **naming**: no
module's limit may be called "the RAM ImSwitch holds". It does not imply that
the limits must merge. Separate limits for separate allocations are valid;
their potential usage adds, and the diagnostics below say so where the sum is
known.

## Large payloads: one rule, at both boundaries

A "frame" is not always a camera image. For a scan-driven detector it is the
**whole assembled volume**: the raw half of `APDManager.drainChunk` publishes
`expand_dims(np.array(self._image, copy=True), 0)` exactly once per completed
scan (`rawFrameIsDeferred`), the display half (`getChunk`) is the same volume
in its display form, and the dimensions come from
`ScanWorker._output_image_dims`, not from anything the manager declares. A
1024 × 1024 × 128 uint16 Z-stack is one 256 MiB frame; 2048 × 2048 × 64 is
512 MiB. These are measurements, and a buffering limit has no standing to
refuse them.

The writer already knows this: `enqueue_frames` admits any payload when the
queue is empty — "one assembled volume, say, would wait for room that can
never appear" — which bounds the backlog to one oversized payload without
refusing it. **The detector boundary does not.** `_distributeChunkLocked`
counts the whole raw volume against `MAX_QUEUED_CONSUMER_BYTES` with no such
exception, clears the queue and marks the consumer overflowed.

Reproduced on this branch (round-3 record, item 1): a 2 × 65 × 1024 ×
1024 uint16 volume, 260 MiB against the 256 MiB cap, is dropped at publish, and
the next `readChunk` raises `ChunkConsumerOverflowError` saying the consumer
"fell far enough behind" — it was never given the frame. Before the byte
budget (862786bd, this branch) the raw cap was **16 frames**, which admitted
one volume of any size; this is a regression the audit introduced, and PR #29
must not merge with it.

**Rule, at both boundaries:** one *delivery* is admitted whenever the queue
is empty, however large; a further delivery waits (writer) or overflows
(detector) as today. A delivery is what one drain hands a consumer — one
assembled volume for a scan-driven detector, or a burst of many frames for a
camera that was polled late — so the exception admits one delivery, which may
be more than one frame. That is a controlled exception — one oversized
delivery, never an unbounded backlog — and it is the same rule in both places.
Implemented at the detector boundary (`d648967d`), with tests for both forms, a
delivery arriving behind an oversized one (overflows, as before), deliveries
that fit after the oversized one is read (admitted, as before), and the
warning said once per consumer and naming the setting. A refusal mode for
constrained installations is not built: it is the rule that restricted
measurement, and nobody has asked for it.

**Estimate, where the payload is known.** A point detector's raw payload is
known when the scan is built and again when the recording arms
(`_output_image_dims` × dtype). At arm the recording logs its estimate against
both limits — and *warns* when one delivery exceeds a limit, because the
consequence is real and otherwise invisible: an oversized delivery leaves no
room for further backlog in *that* queue until it is read. Not "the next
volume is lost": the writer queue admits its own oversized payload when empty
and the worker holds one in hand, so two volumes can be in flight before a
third finds every queue occupied. Warn, name the setting, proceed.

**No startup floor.** `DetectorsManager` constructs every configured detector
whether or not a session uses it; a floor check against "the largest
configured detector" refuses the module for a detector nobody started.

## Automatic work: exact, or skipped — never approximated silently

Round 2 bounded the mean preview by computing it at a spatial stride. Round 3
traced who consumes that image:

- `MoNaLISAController.findPattern` runs the pattern finder on `getMeanData()`
  and feeds the found periods and offsets — **in pixels of that image** — into
  the reconstruction via `sigPatternUpdated`. At stride *k* the reconstruction
  would receive period/*k*.
- `ReconstructorManagerController._updateSmlmPreview` runs
  `compute_detection_preview(image, threshold, roi, sigma)` on
  `getDisplayedImage2D()`, with `sigma` in pixels. This affects the preview
  and how the operator reads the parameters, not the localised array.
- The pattern grid overlay is drawn on the displayed image's coordinates.

A strided preview is not a coarse picture; it is wrong input to one
reconstruction and one preview. But "exact, as today" is not what today does
either, and round 4 caught the note assuming it. `getMeanData` has **two
meanings already**: for a lazy, non-materialised source it averages at most
`MEAN_PREVIEW_MAX_PLANES` (256) planes taken at an even stride — a 512-plane
stack alternating between 0 and 100 returns 0 — while a materialised source
gets `mean_plane` over every plane, which returns 50. Native spatial
coordinates in both; which one `findPattern` receives depends on whether the
file was opened with Open or Open virtual. So:

- **This work changes neither meaning.** A preview that keeps native spatial
  coordinates and may sample across planes, and an exact mean over every
  selected plane, are two different things; making them one, and deciding
  which `findPattern` should get, is a separate check (*Follow-ups*).
- **The mean preview is estimated before it is computed**: accumulator plus
  result, from the plane shape and dtype. Within `processingWorkingSetMB` it
  is computed as today. Above it, the *automatic* preview (on load, in
  `DataFrameController.showMean`) is skipped with a status line naming the
  estimate and the setting; the *explicit* one (the Show-mean button) proceeds
  after a warning naming the same numbers. The skip lives in the caller,
  **never in `getMeanData`**: `findPattern` calls it directly and returns
  silently on an empty result, so an empty value there would turn a skipped
  preview into a silently skipped pattern search.
- **The contrast sample count follows the working set** (round 2, kept):
  `max_samples = min(_MAX_SAMPLE_VALUES, allowance // _WORKING_SET_BYTES_PER_ELEMENT)`,
  tested at small budgets. Sampling is already an approximation with no
  coordinates in it, so this one is free.
- **Opening a file stays possible, and a log line does not make it safer.**
  Ordinary Open (`quickLoadData` → `checkAndLoadData`) materialises the whole
  dataset today; Open virtual is a separate action that opens only the lazy
  handle. Above the working set, ordinary Open says what it is about to
  materialise — the decoded size of the *selected* dataset, `shape ×
  itemsize` from metadata (round 1, kept) — *before* it starts, in the status
  bar and the log, and says whether Open virtual offers a genuine lazy path
  for this source (`supports_lazy_indexing`; `TiffVirtualArray` without
  `aszarr()` serves every plane by a whole-series `asarray()` and must not be
  called bounded — round 1, kept). No threshold refuses an open.

  **Deviation from the round-4 text, deliberate:** Open does *not* switch to
  the lazy path automatically. Round 4 asked for both "prefer a genuine lazy
  path where available" and "do not silently change either meaning of the
  mean in this work" — and for a stack of more than 256 planes those
  conflict, because a lazily opened source gets the plane-sampled mean and a
  materialised one the exact mean, and `findPattern` takes whichever it is
  handed. Switching large files to lazy would change what the pattern finder
  receives for exactly the MoNaLISA stacks that are large. Until the
  follow-up settles what `findPattern` should receive, Open keeps its
  meaning and announces its cost; the automatic switch is the next step
  after that follow-up, not before it.
- **An exact bounded preview for in-plane-huge sources** — tiled accumulation,
  identical values, more reads — is the later answer if a rig ever has 16k²
  planes. Not a stride.

## Diagnostics: the sum, stated where it is known

- **At arm, one estimate line per recording:** raw payload per detector; the
  two queue limits; batches up to `WRITE_BATCH_FRAMES` × frame per detector
  (the writer batches by count, and `np.concatenate` doubles it for the
  flush); one chunk in hand per detector. An estimate, not a cap: it tells the
  operator what a stall costs and which setting moves it.
- **Warn on the first block, with no delay** (round 2, kept as a
  diagnostic, not as an ordering guarantee). `enqueue_frames` today waits
  `PRODUCER_STALL_WARN_S` (1 s) before logging, and a 256 MiB queue at
  512 MiB/s fills in half that. A detector overflow can also happen with the
  writer never blocked — BeadRec falling behind, or the acquisition worker
  slow for its own reasons — so no test may require the block line before
  every overflow.
- **Every message that quotes a limit names the setting**, and the overflow
  message stops blaming the consumer when the payload alone exceeded the
  limit.

## What is deliberately NOT exposed

**`TARGET_CHUNK_BYTES` (4 MiB).** The on-disk chunk layout, bounded by the
*reader's* chunk cache, not the writing machine's RAM. If ever settable it
belongs beside the save format.

**`QUEUED_FRAME_OVERHEAD_BYTES` (128 B).** A measured property of a Python
object.

**`LIVE_POLL_MAX_BYTES` (64 MiB), `MEAN_PREVIEW_MAX_PLANES` (256).** Latency
and plane-count bounds on the live poller and the preview; neither is where a
machine's RAM shows up, and both stay literals until one is reported.

**VRAM.** Below.

## VRAM: deferred, and why

`cupy` in the Snouty deskew and `torch` in the Denoiser/UNet manage no
memory: no pool limit, no chunking, no OOM handling. A `gpuMemoryBudgetMB`
would be a number nothing reads, and in a preferences dialog that reads as a
guarantee. It becomes meaningful when the deskew chunks its volume to a cap
and the denoiser sizes its batch from one. Every ImProcess memory defect the
audit verified was host RAM.

## Where it lives

A field on `Options` (`imswitch/imcontrol/model/Options.py`), stored in
`imcontrol_options.json` under the per-machine config directory, beside
`RecordingOptions` and `WatcherOptions`:

```python
@dataclass(frozen=True)
class MemoryOptions:
    writerQueueMB: int = 512
    perDetectorQueueMB: int = 256
    processingWorkingSetMB: int = 256
```

**Not the setup file.** It describes the microscope and travels between
machines; a memory limit is a property of the computer.

ImProcess reads it through `load_processing_config`, which already calls
`configfiletools.loadOptions()` through the single allowlisted
improcess → imcontrol edge.

**UI.** There is no preferences dialog today. Ship the settings and let the
file be hand-edited; a dialog that edits three integers can follow.

## Deferred, and what would justify each

- **Pooling the detector queues** behind one allocator. When a multi-camera
  rig shows that the per-queue sum, not any one queue, is what runs out.
- **Reservation tokens through `readChunk`.** It has five callers — the
  recording worker, the workflow facade, BeadRec, and fresh-frame acquisition
  for tiling and autofocus — with different lifetimes (BeadRec releases by
  scan generation; workflows release on abort). Returning `(frames,
  reservation)` is a compatibility and lifetime design in its own right, and
  the chunk-in-hand it would account for is one chunk per recording detector:
  stated in the estimate line, not worth an API. **The return contract — a
  list of frames — is preserved.** Any later ownership change needs a
  demonstrated problem and tests across cancellation, concurrent consumers
  and detector-buffer lifetimes.
- **Splitting a multi-frame delivery into admissions that fit.** Moot while
  both boundaries admit when empty. If ever revisited, measure it on its own:
  unlike holding reservations longer it does not reduce backlog, and can use
  free space sooner.
- **The preallocated batch buffer.** Removes the `np.concatenate` double (up
  to 32 frames per detector) and the `np.stack` copy in `_getNewFrames`. An
  optimisation; measure a rig first.
- **A strict-refusal mode.** Only if a constrained installation asks.
- **Tiled exact preview.** Only for in-plane-huge sources, if any appear.
- **GPU chunking**, then `gpuMemoryBudgetMB`.

## Acceptance

Not "no behaviour change" — round 3 showed that claim was false for what
rounds 1–2 proposed, and it is not the right criterion anyway. Not
"byte-identical files" either: recordings carry timestamps, and a container
may represent the same data differently. **Output compatibility** means the
acquired values, dtype, dimensions, ordering and completeness, and the
metadata that carries meaning, compare equal, with the volatile fields named
and allowed.

**Any change to queue behaviour** — this note's Phase A included, and any
later one — is gated on a stall test across four acquisition shapes. It is
not an optional phase:

- a large single volume (one delivery above `perDetectorQueueMB`);
- small rapid frames (a fast camera on a small ROI);
- burst delivery (many frames per drain, after a late poll);
- multiple detectors with concurrent consumers (recording plus BeadRec or
  live view).

The test drives **actual arrival schedules** and asserts on **per-queue
occupancy**, not on an average `T·R`: bursts and contention between consumers
are what an average hides. In each shape: no frame lost while the backlog
fits; a predictable, loud failure when it does not — an overflow naming the
detector, the limit and the setting, with the writer's block line present
when the writer was in fact the cause; and the oversized delivery delivered
intact at both boundaries. Phase A ships with its unit tests (both forms, the
delivery behind, the fit after, the warning); the four-shape test is owed
before Phase B changes anything else about the queues.

## Phases

- **A — the regression, in PR #29. Done (`d648967d`).** One delivery is
  admitted to an empty queue at the detector boundary whatever its size,
  mirroring the writer; an oversized admission is said once per consumer and
  names the setting; the overflow message quotes what was held and what
  arrived. Five tests: the volume form, the burst form, a delivery behind an
  oversized one overflows, deliveries that fit are admitted after it is read,
  the warning is said once and again for a fresh registration.
- **A′ — the four-shape stall test**, before B: actual arrival schedules,
  per-queue occupancy; the gate for every later queue change (*Acceptance*).
- **A′ — the four-shape stall test. Done.** `test_memory_limits_recording.py`:
  real broker, real `WriterThread`, a storer whose disk the test stalls;
  per-queue occupancy asserted at every step.
- **B — the settings. Done.** `MemoryOptions` on `Options`; the three
  literals read through `memory_limits.effectiveBytes` with the literal as
  the default (so an unconfigured process, and a test that patches a
  literal, behave exactly as before); contrast sample count follows the
  working set. Defaults reproduce every literal.
- **C — estimates and diagnostics. Done.** The arm-time estimate line
  (`_memoryEstimateLine`); the point detector's volume line at scan build;
  the oversized-delivery warning; the block reported at once and repeated;
  every message names the setting.
- **D — safer automatic work. Done, with the Open deviation above.** Mean
  preview estimated (`DataObj.meanPreviewNotice`), automatic skip to the
  first plane above the working set, explicit warn-and-proceed; decoded size
  and lazy-path check announced before an Open materialises.
- **E — optional.** Config-editor fields.

## Follow-ups outside this note

- **What `findPattern` should receive.** Today it depends on how the file was
  opened: a ≤ 256-plane sampled mean for a lazy source, the exact mean for a
  materialised one. Decide which the pattern finder wants and make it
  independent of the open path.
- **The Hamamatsu comment** says 2 GB where the code allocates 4 GiB.

## Implementation

| What | Where |
|---|---|
| The three limits, adopted once, literal until configured | `imswitch/imcommon/model/memory_limits.py`; `Options.MemoryOptions`; adopted in `imcontrol/__init__.py` and `improcess/model/processing_config.py` |
| Detector queue honours `perDetectorQueueMB`; admit-when-empty; messages | `DetectorManager._queueBudgetBytes`, `_distributeChunkLocked`, `readChunk` |
| Writer honours `writerQueueMB`; block reported at once; arm estimate | `RecordingManager._writerQueueMaxBytes`, `enqueue_frames`, `_memoryEstimateLine` |
| Point detector's volume line at scan build | `APDManager.initiateImage`, `PMTManager.initiateImage` |
| Contrast sample size follows the working set; strides from what a slice keeps | `contrast._max_samples`, `_strided_key`, `sample_values` |
| Decoded size, lazy path, preview estimate (measured against the peak; a non-lazy source charged its series), notice before a load | `DataObj.decodedBytes`, `sourceHasLazyPath`, `planeReadIsBounded`, `materializationNotice`, `meanPreviewNotice`, `checkAndLoadData` |
| Automatic vs explicit mean; status line | `DataFrameController.showMean(explicit=…)`, `DataEditController`, `FileIOController._loadAsCurrent`, `CommunicationChannel.sigStatusMessage` |
| Tests | `imcommon/_test/test_memory_limits.py`; `imcontrol/_test/unit/test_memory_limits_recording.py`, `test_chunk_contract.py`; `improcess/_test/test_contrast.py`, `test_data_obj_io.py`, `test_data_frame_virtual.py`, `test_data_edit_controller.py` |
| Docs | `docs/improcess.rst` *Memory limits*; changelog |

## Decisions

- **Per-queue naming** (2026-09-21, round 2): the detector allowance stays per
  `(detector, consumer)` and is named so. Round 3 keeps this and removes the
  alternative — there is no pooled total for it to fold into.
- **Fixed defaults, not machine fractions** (2026-09-21, round 2, as "a fixed
  2048 MiB total"). The pooled total that number applied to is gone after
  round 3; what survives of the decision is its reason — the same default on
  every machine — applied to the three literals. Restated as
  512 / 256 / 256 MiB and accepted in round 4.

## Review round 1 (2026-09-21)

Six findings, all confirmed. Disposition after round 3 in brackets.

1. **[P1] The detector share was not a module-wide limit.** Per
   `(detector, consumer)`. → per-queue naming. *[Kept; the only form now.]*
2. **[P1] The reserved 25 % could not cover writer batches.** Released on
   dequeue, `np.concatenate` doubles the batch. → hold until written,
   preallocate. *[Finding stands — the writer batches by `WRITE_BATCH_FRAMES`
   count; the fix is deferred as an optimisation, and the batch appears in the
   estimate line instead.]*
3. **[P1] The open threshold must use decoded dataset size.** *[Kept, as an
   estimate that is said, not a threshold that refuses.]*
4. **[P1] Opening virtually does not always bound anything.** *[Kept.]*
5. **[P2] Startup validation cannot see later scan payloads.** → revalidate
   at scan build and arm. *[Kept as estimate-and-warn; the refusal it fed is
   withdrawn.]*
6. **[P2] `writer > detector` is policy, not a safety invariant.** *[Kept.]*

## Review round 2 (2026-09-21)

Five findings plus a factual correction, all confirmed.

1. **[P1] Frames between the detector and the writer were unaccounted.** →
   reservation transfers with the payload. *[Finding stands; the fix is
   withdrawn — see round 3, item 4 — and the term goes in the estimate line.]*
2. **[P1] The oversized-payload exception contradicted the total.** → split
   multi-frame payloads, refuse indivisible ones at arm. *[Withdrawn: the
   refusal is the rule round 3 found restricts measurement. Replaced by the
   same admit-when-empty rule at both boundaries.]*
3. **[P1] A lazy dataset could still blow the processing budget in preview.**
   → spatial stride. *[Finding stands; the stride is withdrawn — see round 3,
   item 3. Replaced by estimate, exact-or-skip.]*
4. **[P2] Lowering the contrast allowance did not lower the sample.**
   *[Kept.]*
5. **[P2] Warning-before-overflow was not guaranteed.** *[Kept.]*
6. **[fact] Both modules run in one process.** → merged into one pooled
   total. *[Fact kept; the inference withdrawn — see round 3, item 2.]*

## Review round 3 (2026-09-21)

The reviewer's five concerns, each verified against the code, with what the
verification added. The reviewer also corrected their own earlier direction:
rounds 1–2 had pushed toward making the global-cap promise true, and narrowing
the promise was the better response. Agreed, and the note is restructured on
that basis.

1. **A buffering preference became a restriction on what can be measured.**
   *Confirmed, and already true on this branch.* A scan-driven detector's
   frame is the whole volume (`APDManager.getChunk`, `rawFrameIsDeferred`);
   the arm-time refusal would have refused valid Z-stacks. Verification found
   the detector boundary *already* refuses them: no admit-when-empty rule in
   `_distributeChunkLocked`, reproduced with a 260 MiB volume against the
   256 MiB cap. Before 862786bd the RAW cap was 16 frames and admitted one
   volume of any size. → Phase A, in PR #29 before merge. The startup floor is
   dropped: every configured detector is constructed whether used or not.
2. **The pooled total is neither necessary nor a real process limit.**
   *Confirmed.* `HamamatsuCameraMR` allocates ~4 GiB of user buffers; consumers
   register dynamically; datasets and results are unbounded by design. One
   nuance kept: the one-process fact still forbids calling any module's limit
   "ImSwitch's memory". → three limits named for what they bound; no total, no
   shares; the sum stated as an estimate where known.
3. **Downsampling the mean image changes scientific behaviour.** *Confirmed,
   and stronger than stated:* `findPattern` feeds pattern periods and offsets
   in pixels of the mean into the MoNaLISA reconstruction, and the SMLM
   detection preview takes `sigma` in pixels of the displayed image. A stride
   is wrong input, not a coarse picture. → estimate, exact-or-skip
   automatically, warn-and-proceed explicitly; tiled exact preview deferred.
4. **Reservation tracking expands into a public acquisition API change.**
   *Confirmed.* Five `readChunk` callers with different release lifetimes.
   The custody gap is real but is one chunk per recording detector; it
   belongs in the estimate, not in a token every caller must carry. →
   deferred, with the callers listed.
5. **"No behaviour change" was false for the proposed first phase.**
   *Confirmed, with the mechanism:* holding the writer reservation until the
   batch is written shrinks the admissible backlog by one batch per detector;
   transferring custody shrinks the detector queue's slack by one chunk;
   splitting changes admission timing. Each reduced stall tolerance at the
   same setting — exactness bought with the property the operator actually
   wants. → withdrawn; acceptance is output compatibility plus the four-shape
   stall test.

**Additions from verification.** The batch-double finding (round 1, item 2)
stands but is deferred as an optimisation. The Hamamatsu comment says 2 GB
where the code allocates 4 GiB; worth a one-line comment fix when someone is
in that file. The reproduction script for item 1 lives in the session
scratchpad and becomes the Phase A test.

## Review round 4 (2026-09-21)

Accepted the round-3 direction; corrected its claims. Each correction was
verified against the code before it was folded in.

1. **The regression is real; fix before merge.** Independently reproduced by
   the reviewer. Terminology corrected: the raw volume is the raw half of
   `drainChunk`; `getChunk` is the display half. "Payload" defined as one
   *delivery*, which may be one volume or a burst; both forms tested, plus a
   delivery behind an oversized one and deliveries that fit after it. "A
   writer stall is fatal for the next volume" withdrawn as too categorical: an
   oversized delivery leaves no room for further backlog in that queue. →
   Phase A implemented (`d648967d`); note corrected.
2. **Dropping the pooled total is correct.** Estimates stay estimates. → no
   change.
3. **Striding is wrong, but "exactly, as today" was too.** Verified: for a
   lazy source `getMeanData` already averages at most `MEAN_PREVIEW_MAX_PLANES`
   planes at an even stride, so a 512-plane 0/100 stack gives 0 where the
   materialised mean gives 50. The SMLM connection is a preview, not a
   reconstruction. A skipped automatic mean must not make `getMeanData` return
   empty to `findPattern`. → both meanings named and neither changed here; the
   skip placed in the caller; follow-up recorded.
4. **Deferring reservation tokens is justified.** → return contract stated as
   preserved; later changes need a demonstrated problem and tests across
   cancellation, concurrent consumers and buffer lifetimes.
5. **The stall-tolerance objection is correct, with one qualification.**
   Splitting deliveries does not necessarily reduce tolerance and should be
   measured on its own. "Byte-identical files" was the wrong criterion. →
   splitting separated and deferred; acceptance restated as values, dtype,
   dimensions, ordering, completeness and meaningful metadata, with volatile
   fields allowed.

**Two synthesis corrections.** Verified that ordinary Open materialises today
(`quickLoadData` → `_loadAsCurrent(virtual=False)` → `checkAndLoadData`) and
that Open virtual is a separate action, so a log line makes nothing safer: the
lazy path is preferred where real, an expensive fallback is announced before
it starts, the explicit full load stays, and the finding is narrowed rather
than closed. The four-shape test is required for any queue change, on actual
arrival schedules and per-queue occupancy, and warning-before-overflow is not
universal. The fixed 512 / 256 / 256 MiB defaults are accepted as the
restatement of the round-2 decision.

## Review round 6 (2026-09-22)

Reviewed the implementation (`49f23eff`). Five findings, all reproduced and
fixed without adding an acquisition restriction; the reviewer found no new
acquisition-path blocker.

1. **[P1] Invalid settings crashed at load, before validation.** The JSON
   loader coerces `int` fields itself: `"lots"` raised `ValueError` and `2.5`
   silently became `2`, so the promised warn-and-default never ran for a real
   file. → the three fields are typed `Any` so the raw value reaches
   `configure`; the bad-value test now goes through `Options.from_json`.
2. **[P1] A non-lazy TIFF bypassed the preview safeguard.** The estimate
   looked at the plane's size, not at whether reading a plane decodes the
   whole series (`TiffVirtualArray` without `aszarr()`); 150 whole-series
   reads with no warning, and the first-plane fallback cost the same. →
   `meanPreviewBytes` charges such a source its decoded size on top, the
   notice says why, and `planeReadIsBounded` tells the panels to show
   nothing rather than read a plane that costs the series.
3. **[P2] The edit window computed the mean it had just deferred.**
   `setData` set it aside and then called the same method the button uses.
   → automatic display (first plane, or nothing) and the explicit mean are
   separate methods, as in the current-data panel.
4. **[P2] Contrast sampling overshot for shapes with a singleton leading
   axis.** The stride arithmetic charged the singleton a share of the
   reduction it could not deliver: `(1, 100, 100, 100)` at 1 MiB returned
   200 000 values against 61 680. → strides are derived from what each
   slice actually keeps, only reducible axes count towards the exponent,
   and the spatial stride is raised until the counted total fits; a shape
   sweep pins the bound.
5. **[P2] The preview estimate omitted live allocations.** 12 B/px charged;
   22 B/px measured, from the retained input plane and a second float64
   plane the division made. → the division is in place (measured peak
   12 B/px) and the estimate charges 12 B/px plus the input plane's dtype;
   a tracemalloc test keeps the estimate a ceiling.
