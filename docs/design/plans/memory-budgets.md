# Memory budgets: declared numbers, derived shares

*Status: proposal, revised after review rounds 1 and 2 (2026-09-21). Written
out of the question the magic-number audit left open — the audit replaced frame
counts with byte budgets, and the byte budgets are still literals in the
source. This note says how to make them settable without recreating the defect
class the audit closed. Nothing here is implemented. Not part of PR #29.*

*Eleven findings across two rounds, all confirmed against the code. Round 1
corrected the accounting and demoted the ordering invariant; round 2 closed the
custody gap between the two queues, bounded the preview's spatial extent, and
settled that both modules share one process. Records at the end, followed by
the two decisions taken on them.*

## The question

> Couldn't `WRITER_QUEUE_MAX_BYTES` be a global parameter, editable under
> preferences, to optimize for good and less good hardware? The same could be
> useful for ImProcess to specify VRAM (default 1 GB)?

Yes for a settable budget. No for one setting per constant — and that
distinction is the whole content of this note.

## Why not one field per constant

Not because the constants must stand in a fixed order — they need not, see
below — but because **they compose into one quantity the operator actually
cares about and none of them is that quantity**.

A recording survives a disk or compression stall for as long as the frames it
cannot yet write have somewhere to sit. Those places are, in order:

1. the **writer queue** (`WRITER_QUEUE_MAX_BYTES`, 512 MiB): the acquisition
   loop blocks on `enqueue_frames` once it is full — graceful backpressure;
2. the **chunk in the worker's hands**, drained but not yet admitted;
3. the **detector chunk queue** (`MAX_QUEUED_CONSUMER_BYTES`, 256 MiB): while
   the acquisition loop is blocked it is not calling `readChunk`, so the
   recording's own consumer queue grows behind it. When *that* overflows the
   consumer is marked overflowed and its stream is declared incomplete — the
   recording fails.

So the tolerance is **additive**, and the split decides only *how much of it is
graceful*. An operator asked to set five numbers has to know all of this to set
any. Asked for a pooled total and one per-queue limit — "how much RAM may
ImSwitch hold, and how much may any one detector queue hold" — they can answer
both from what their machine has.

The historical defect that motivated bytes at all was not an ordering
violation: the writer queue held 64 *items*, which at the 0.1 ms poll interval
is about 64 frames — 7 ms on a fast camera, not the seconds the number
suggested. The unit was wrong, and the fix was to state the buffer in the unit
that bounds memory.

## What the budget must account for

A declared total is only honest if the accounting covers everything the
declaration implies. Four gaps in the current code have to close as part of
this work, or the number promises what it cannot deliver.

### 1. The detector allowance is per queue, not per process

`DetectorManager._distributeChunkLocked` applies `MAX_QUEUED_CONSUMER_BYTES`
to **each consumer key on each detector**. Three detectors with a recorder and
a live-view consumer each is six independent 256 MiB allowances — 1.5 GiB,
under a setting that would claim 1 GiB.

**Decided (see Decisions): the first pass keeps the per-queue limit and names
it accordingly.** Pooling it behind one allocator is the better end state but
is the largest piece of work in this note, and a wrong name is the more urgent
defect: a number that silently means "× the number of queues" is exactly what
this note exists to avoid.

So the detector allowance is `perDetectorQueueMB`, a second setting that is
**not** drawn from the pooled total, and the note claims no process-wide total
that includes it. What the operator gets instead is the worst case, computed
where it is known and stated plainly at startup:

> Memory budget: 2048 MiB pooled, plus up to 6 × 256 MiB of detector queues
> (2 detectors × 3 consumers) — 3584 MiB worst case.

That line is honest, it is derived from the configuration rather than assumed,
and it tells an operator on a small machine exactly what to lower. Pooling
later collapses the two settings into one without changing any default; until
then, two numbers that each mean what they say beat one that does not.

### 2. Custody is dropped between the queues

`readChunk` ends with `frames = list(queue); queue.clear();
self._chunkConsumerBytes()[consumerKey] = 0; return frames` — the detector pool
is credited **before** the caller owns the frames. `_getNewFrames` then
`np.stack`s them into a fresh array, and `enqueue_frames` may block on a full
writer queue while holding it. During that block the queue is empty and free to
refill to its full allowance, so the peak is `writer + detector queue + chunk
in hand`, and the middle term is invisible to both.

**Contract: the reservation follows the payload.** `readChunk` returns the
frames *and* a reservation covering their bytes; the queue is credited only
when that reservation is released, and releasing it is the same act as
acquiring against the writer queue — a transfer, not a release followed by an
acquire. A consumer that drops the frames releases it directly. This holds
whether the allowance is per queue or pooled, so it is not deferred with the
pooling.

`np.stack` should also go. With the preallocated batch buffer below, the chunk
is copied twice today (once into the stacked array, once into the batch); the
list can be handed to the writer as it is, and copied straight into the batch
buffer. That removes a full copy per chunk *and* the transient double inside
`np.stack`, which is the part the reservation cannot cover.

### 3. Writer batches and their temporaries are outside the accounting

The writer releases a chunk's byte reservation when it **dequeues** it, then
accumulates up to `WRITE_BATCH_FRAMES` (32) frames in `self._batches` and
`np.concatenate`s them. For a 2048 × 2048 uint16 camera that is 256 MiB of
accumulated input plus another 256 MiB for the concatenated copy.

**Contract:**

- the reservation is released when the batch is **written**, not when it is
  dequeued, so accumulated frames stay inside the queue budget;
- the batch is filled into a **preallocated** `(WRITE_BATCH_FRAMES, *shape)`
  buffer per detector instead of being concatenated, which removes the
  transient double and makes the batch's cost exactly one buffer, declared and
  constant.

### 4. Oversized payloads

Today a single payload larger than the whole budget is admitted **alone** — the
`queuedBytes == 0 or …` rule — because waiting for room that can never appear
is a deadlock rather than backpressure. But "alone" bounds only the writer
queue: the detector queues and the batch buffers still hold their own bytes, so
the declared total is exceeded while the rule congratulates itself.

**Contract:**

- a multi-frame payload larger than the writer's free space is **split** into
  admissions that fit, rather than admitted whole;
- an **indivisible** payload — one frame larger than the writer share — is
  refused at arm time, where the frame size is known (see *Validation*), not
  admitted at run time;
- what remains is a bounded, stated overshoot: at most one frame plus one batch
  buffer per recording detector. The setting is a cap on what ImSwitch
  *chooses* to hold, and that residue is what it cannot choose.

## What is exposed

Both modules load into **one process** — `imswitch/__main__.py` imports every
enabled module package and builds them into a single `MultiModuleWindow` under
one `QApplication` — so a separate budget per module would double-count the
same RAM. One pooled total, split by module, sub-split by role; plus the
per-queue detector limit, which stands outside it until pooling lands:

```
imswitchMemoryBudgetMB = 2048      # pooled: writer queue, batch buffers, processing
perDetectorQueueMB     = 256       # each (detector, consumer) chunk queue, on top
```

The total is a **fixed** 2048 MiB, not a fraction of physical RAM: every
machine then starts from the same number, a bug report quotes a value that
means the same thing everywhere, and the default can be written in a document
instead of being discovered. A machine that wants something else says so.

| Module | Share | At the default |
|---|---|---|
| acquisition (imcontrol) | 50 % | 1024 MiB |
| processing (improcess) | 50 % | 1024 MiB |

### Acquisition

| Derived | Share of the module's | At the default | Today's literal |
|---|---|---|---|
| writer queue (`WRITER_QUEUE_MAX_BYTES`) | 50 % | 512 MiB | 512 MiB |
| batch buffers (`WRITE_BATCH_FRAMES` × frame × detectors) | ≤ 25 % | derived, capped | unaccounted |
| in-flight headroom (chunks in custody transfer) | 25 % | 256 MiB | unaccounted |

Neither of the last two rows is a free parameter. The batch share is
`WRITE_BATCH_FRAMES × frame bytes × recording detectors`, computed when the
recording arms; if it exceeds its share the batch depth is reduced for that
recording and logged, rather than the budget being exceeded silently. The
headroom row is the chunk each recording detector holds between `readChunk` and
writer admission (gap 2) — named here because round 2 found it accounted
nowhere, and sized to one chunk per recording detector with room to spare.

`MAX_QUEUED_CONSUMER_BYTES` is `perDetectorQueueMB`, applied per queue exactly
as today. For the single-detector, single-consumer case — every in-tree test
and most rigs — the defaults reproduce today's constants exactly, and a
recording's stall tolerance is still `512 + 256 = 768 MiB`. A multi-camera rig
gets what it gets today too, and is *told* so by the startup line above rather
than left to find out.

### Processing

| Derived | Share of the module's | At the default | Today's literal |
|---|---|---|---|
| contrast working set (`_SAMPLE_WORKING_SET_BYTES`) | 25 % | 256 MiB | 256 MiB |
| preview working set (`getMeanData`) | 25 % | 256 MiB | *(unbounded in-plane)* |
| live poll (`LIVE_POLL_MAX_BYTES`) | 6.25 % | 64 MiB | 64 MiB |
| materialise-on-open ceiling | 100 % | 1 GiB | *(no such rule today)* |

Three rules these rows imply:

**The sample count follows the allowance.** `sample_values(data, *,
max_samples=_MAX_SAMPLE_VALUES)` caps at two million values *independently* of
`_SAMPLE_WORKING_SET_BYTES`, so lowering the contrast allowance to 16 MiB still
returns two million values — about 32 MiB by the module's own
`_WORKING_SET_BYTES_PER_ELEMENT` estimate, twice what was allowed. Derive it:
`max_samples = min(_MAX_SAMPLE_VALUES, allowance // _WORKING_SET_BYTES_PER_ELEMENT)`,
and test at small budgets, not only at the default.

**The preview is bounded in-plane, not only in plane count.**
`MEAN_PREVIEW_MAX_PLANES` (256) bounds how many planes are read; it says
nothing about how large one is. `getMeanData` allocates a full-plane float64
accumulator and then a second full-plane array for the division — a
16 384 × 16 384 plane is 2 GiB each, against a 1 GiB module budget, on a
perfectly lazy source. The preview is therefore computed at a **spatial
stride** chosen so that accumulator plus result fit the preview share, logged
once with the stride used. A preview may be coarse; it may not be 4 GiB.

**The open threshold compares the decoded dataset, not the file.** `shape ×
dtype.itemsize` of the *selected* dataset, read from metadata without
materialising. A compressed HDF5 well under a gigabyte expands to many; a Zarr
source is a directory; one HDF5 file holds several datasets and only the
selected one is being opened.

And one thing the rule must not assume: **virtual is not automatically
bounded.** `TiffVirtualArray.__init__` sets `supports_lazy_indexing = False`
when `series.aszarr()` fails, and `__getitem__` then serves *every* plane by
`self.asarray()[key]` — a whole-series read per plane. Opening such a file
"virtually" and striding 256 planes is 256 full-series reads: worse than
materialising once. Phase D consults `supports_lazy_indexing` and either
refuses with a message naming the reason or falls back to a single bounded
materialisation — never pretends the virtual path bounded anything. Same for
`EagerVirtualArray`.

## What is deliberately NOT exposed

**`TARGET_CHUNK_BYTES` (4 MiB).** Not an in-flight budget — the on-disk chunk
layout, bounded by the *reader's* chunk cache (h5py's default is 8 MiB), not by
the writing machine's RAM. Raising it on a strong machine makes every file
produced there slower to read on every machine that later opens it. If ever
settable it belongs beside the save format as a storage choice.

**`QUEUED_FRAME_OVERHEAD_BYTES` (128 B).** A measured property of a Python
object, not a policy.

**VRAM.** See below.

## Where it lives

A field on `Options` (`imswitch/imcontrol/model/Options.py`), stored in
`imcontrol_options.json` under the per-machine config directory, beside the
existing `RecordingOptions` and `WatcherOptions` groups:

```python
@dataclass(frozen=True)
class MemoryOptions:
    budgetMB: int = 2048                # pooled
    perDetectorQueueMB: int = 256       # each (detector, consumer) queue, on top
    acquisitionSharePercent: int = 50   # override; processing takes the rest
```

`perDetectorQueueMB` disappears into the pooled total when the queues are
pooled; keeping it a named field now means that change can drop it without
having to explain what a previously-pooled number used to mean.

**Not the setup file.** The setup file describes the microscope; it is copied
between machines and shared with collaborators. A memory budget is a property
of the computer, and putting it there means a laptop's budget riding into a
workstation's rig config.

ImProcess reads it without a new dependency: `load_processing_config` already
calls `configfiletools.loadOptions()` through the single allowlisted
improcess → imcontrol edge.

**UI.** There is no preferences dialog today. Ship the contract first and let
the file be hand-edited; a dialog that edits two integers can follow. The value
of this change is the derivation and the validation, not the widget.

## Validation: what stops the knob becoming the next trap

1. **A floor at startup**, against the configured detectors' *display* frames:
   a budget that cannot hold a handful of frames of the largest is refused by
   name at construction, with the frame size in the message.
2. **Revalidation when the payload is known.** The startup check is necessary
   and not sufficient: a point detector's raw frame is the assembled scan
   volume, whose dimensions come from the scan, not the manager (`APDManager`
   constructs with `fullShape = (100, 100)` and reallocates in `initiateImage`
   from `ScanWorker._output_image_dims`; a 512 × 512 × 256 uint16 volume is
   128 MiB against a 20 kB construction-time shape). The budget is re-checked
   against the real raw payload and dtype when the scan is built and when the
   recording arms — and that is where an indivisible oversized frame is
   refused, with the frame size, the share and the setting named.
3. **Every message that quotes a budget names the setting.** The detector
   overflow warning and the `readChunk` overflow exception already quote the
   budget in MiB and the frames it bought for *this* detector; they must end
   with the setting name — `perDetectorQueueMB` for those two,
   `imswitchMemoryBudgetMB` for the writer's. A refusal names the control that
   fixes it.
4. **The worst case is stated at startup**, once the detectors and their
   consumers are known: pooled total, plus queues × `perDetectorQueueMB`, plus
   the sum. Until the queues are pooled this line is the only place the real
   ceiling appears, so it is part of the contract rather than a nicety.

### Backpressure is reported when it starts

The first draft promised the producer-stall warning before any overflow.
`enqueue_frames` waits `PRODUCER_STALL_WARN_S` (1 s) before logging, and a
256 MiB pool at 512 MiB/s fills in half of that — so the overflow can arrive
first and the promise is not the implementation's to keep.

**Contract:** warn on the *first* block, with no delay. The block is the
signal, the existing `stallReported` flag already makes it one line per
episode, and the ordering then holds by construction: the writer queue fills,
the producer blocks, the line is logged, and only then can the detector pool
fill. Keep a periodic repeat for a block that persists. The stall test below
includes a high-throughput case where the pool fills in well under a second.

### The test is stall behaviour, not an inequality

The first draft made `writer > detector` the correctness argument. It is not
one. Tolerance is additive, so a smaller writer queue backed by a larger
detector pool absorbs a short stall perfectly well, and any split fails a long
enough one; survival depends on incoming byte rate, total capacity, drain rate
and payload size. The 50/25 split is a **policy** — spend the budget where
exhaustion is graceful backpressure rather than a fatal overflow — worth
keeping as a default and not worth asserting as a law.

So the test is a stall test: a storer that blocks for `T`, a producer paced at
`R` bytes/s, and the assertion that the recording

- survives when `T·R` fits the total capacity, with no frame lost;
- fails **predictably and loudly** when it does not — the block reported first,
  then an overflow naming the detector, the budget and the setting; never a
  silent gap;
- holds both properties at a high `R`, where the pool fills in a fraction of a
  second, and at an inverted split, to pin that behaviour tracks capacity
  rather than which side holds it.

## VRAM: deferred, and why

There is real GPU code — `cupy` in the Snouty deskew
(`reconstructors/snouty/deskew_gpu.py`) and `torch` in the Denoiser/UNet — and
neither manages memory at all: no pool limit, no chunking, no OOM handling.

A `gpuMemoryBudgetMB` today would be a number nothing reads, and a number
nothing reads is worse than no number: in a preferences dialog it reads as a
guarantee. It becomes meaningful when a GPU path can *obey* it — when the
deskew chunks its volume to fit a declared cap and the denoiser sizes its batch
from one. That is the work to schedule; the setting follows it.

Note also which problem is actually reported today: every ImProcess memory
defect the audit verified was **host** RAM — the mean preview, the contrast
working set, the live poll, the unbounded open.

## Phases

- **A — accounting, no setting yet.** Make the reservation follow the payload
  into the writer queue and drop `np.stack`; hold the writer reservation until
  the batch is written and fill a preallocated buffer; split oversized
  multi-frame payloads. No behaviour change at today's constants — this phase
  only makes the bytes visible to whoever counts them. Tests: custody across a
  blocked enqueue, batch accounted, oversized payload split.
- **B — the contract.** `MemoryOptions` on `Options`; one `memory_budgets.py`
  in imcommon deriving module shares and role shares from the pooled total, and
  passing `perDetectorQueueMB` through unpooled; the literals read from it.
  Defaults identical to A. Tests: derivation table, defaults unchanged, batch
  share reduces batch depth rather than overrunning, contrast sample count
  follows the allowance at small budgets.
- **C — validation.** Startup floor and the worst-case line; revalidation at
  scan build and arm, with the indivisible-frame refusal; immediate
  backpressure reporting; messages name the setting; the stall test at two
  splits and two throughputs.
- **D — the preview and the open rule.** Spatial stride for `getMeanData`
  derived from the preview share; decoded-size comparison on open;
  `supports_lazy_indexing` check with an explicit refusal or bounded fallback,
  tested on the non-lazy TIFF path. Closes the unbounded-`asarray` finding.
- **E — optional.** Config-editor fields.
- **Later — pool the detector queues.** One allocator on `DetectorsManager`
  keyed by `(detector, consumer)`; `perDetectorQueueMB` folds into the pooled
  total as a share, the startup worst-case line loses its second term, and a
  multi-camera rig gets a real ceiling instead of a stated one. Defaults do not
  move. This is the end state the first pass is honest about not having
  reached.
- **Later, separately.** GPU chunking in the Snouty deskew and the denoiser;
  `gpuMemoryBudgetMB` once either can honour it.

## Decisions

- **Per-queue naming for the first pass** (2026-09-21). The detector allowance
  stays per `(detector, consumer)` and the setting is named `perDetectorQueueMB`
  rather than being pooled into the total. Pooling becomes a later phase, and
  the startup worst-case line carries the real ceiling until it lands.
- **The default total is a fixed 2048 MiB** (2026-09-21), not a fraction of
  physical RAM. Every machine starts from the same number, a bug report quotes
  a value that means the same thing everywhere, and the default can be written
  down rather than discovered.

## Open questions for review

1. **The shares.** 50/25/≤25 lands on today's constants for the common case.
   Is reproducing them the right anchor, or should the split be re-derived from
   measured rig throughput?
2. **Per-rig override.** May a setup file *lower* the machine budget (a rig
   sharing a workstation), or does that reintroduce the travelling-config
   problem?

## Review round 1 (2026-09-21)

Six findings, all confirmed; four changed the design.

1. **[P1] The detector share was not a module-wide limit.** Per
   `(detector, consumer)`. → pooled allocator, with honest per-queue naming as
   the stated fallback — and the fallback is what was chosen (see *Decisions*),
   with pooling scheduled as a later phase.
2. **[P1] The reserved 25 % could not cover writer batches.** Reservation
   released on dequeue; `np.concatenate` doubles the batch — 512 MiB for a
   2048² camera against 256 MiB of "headroom". → reservation held until
   written, preallocated buffer, batch share derived and capped.
3. **[P1] The open threshold must use decoded dataset size.** → `shape ×
   itemsize` of the selected dataset.
4. **[P1] Opening virtually does not always bound anything.**
   `TiffVirtualArray` falls back to whole-series `asarray()` per plane. →
   `supports_lazy_indexing` consulted; explicit refusal or bounded fallback.
5. **[P2] Startup validation cannot see later scan payloads.** → revalidation
   at scan build and arm.
6. **[P2] `writer > detector` is policy, not a safety invariant.** → demoted
   to a default; correctness argument replaced by a stall test.

## Review round 2 (2026-09-21)

Five findings plus a factual correction, all confirmed; three changed the
design and one changed the shape of the setting.

1. **[P1] Frames between the detector and the writer were unaccounted.**
   `readChunk` credits the pool before the caller owns the frames; `np.stack`
   copies them; `enqueue_frames` may block while holding the copy, and the
   pool refills meanwhile. → the reservation transfers with the payload, and
   `np.stack` is dropped (it was a second copy of data the batch buffer copies
   anyway).
2. **[P1] The oversized-payload exception contradicted the total.** "Alone"
   bounded only writer reservations. → multi-frame payloads split into
   admissions that fit; indivisible oversized frames refused at arm; the
   residual overshoot (one frame + one batch buffer per detector) stated
   rather than implied away.
3. **[P1] A lazy dataset could still blow the processing budget in preview.**
   `MEAN_PREVIEW_MAX_PLANES` bounds plane count, not plane size; a
   16 384² plane needs 2 GiB for the float64 accumulator alone, plus as much
   again for the division. → preview computed at a spatial stride derived from
   the preview share.
4. **[P2] Lowering the contrast allowance did not lower the sample.**
   `sample_values` caps at two million values independently — about 32 MiB
   under a 16 MiB allowance. → `max_samples` derived from the allowance;
   tested at small budgets.
5. **[P2] Warning-before-overflow was not guaranteed.** The warning waits 1 s;
   a 256 MiB pool at 512 MiB/s fills in 0.5 s. → warn on the first block with
   no delay, which makes the ordering structural; high-throughput case added
   to the stall test.
6. **[fact] Both modules run in one process.** `__main__.py` imports every
   enabled module and builds them into one `MultiModuleWindow` under a single
   `QApplication`. → two independent budgets would double-count the same RAM;
   merged into one pooled total with module shares, and open question 3 of
   round 1 resolved rather than left open. (The detector queues sit outside
   that total for now — see *Decisions*.)
