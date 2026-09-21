# Memory budgets: one declared number, derived shares

*Status: proposal, revised after review round 1 (2026-09-21). Written out of
the question the magic-number audit left open — the audit replaced frame counts
with byte budgets, and the byte budgets are still literals in the source. This
note says how to make them settable without recreating the defect class the
audit closed. Nothing here is implemented. Not part of PR #29.*

*Review round 1 found six defects in the first draft, four of them P1. The
corrections are folded into the text below and recorded at the end; three of
them changed the design rather than the wording.*

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
2. the **detector chunk queue** (`MAX_QUEUED_CONSUMER_BYTES`, 256 MiB): while
   the acquisition loop is blocked it is not calling `readChunk`, so the
   recording's own consumer queue grows behind it. When *that* overflows the
   consumer is marked overflowed and its stream is declared incomplete — the
   recording fails.

So the tolerance is **additive**: roughly `(writer + detector) / byte-rate`
seconds, and the split decides only *how much of the tolerance is graceful*.
An operator asked to set two numbers has to know all of this to set either.
Asked to set one — "how much RAM may ImSwitch hold between the camera and the
disk" — they can answer from what their machine has.

The historical defect that motivated bytes at all was not an ordering
violation: the writer queue held 64 *items*, which at the 0.1 ms poll interval
is about 64 frames — 7 ms on a fast camera, not the seconds the number
suggested. The unit was wrong, and the fix was to state the buffer in the unit
that bounds memory.

## What the budget must account for

A declared total is only honest if the accounting covers everything the
declaration implies. Two gaps in the current code have to close as part of
this work, or the number promises something it does not deliver.

### The detector allowance is per queue, not per process

`DetectorManager._distributeChunkLocked` applies `MAX_QUEUED_CONSUMER_BYTES`
to **each consumer key on each detector** (`total = queuedBytes.get(key, 0) +
…; if total <= MAX_QUEUED_CONSUMER_BYTES`). Three detectors with a recorder and
a live-view consumer each is six independent 256 MiB allowances — 1.5 GiB,
under a setting that claims 1 GiB total.

**Contract:** the detector share is a single pool, reserved and released
against one allocator owned by `DetectorsManager`, keyed by
`(detector, consumer)`. Per-queue bookkeeping stays (that is what the overflow
message quotes), but admission tests the pool.

If that proves too invasive for a first pass, then the *setting* must be
renamed to what it is — `perDetectorQueueMB` — and the note's claim of a
process-wide total withdrawn. A number that silently means "× the number of
queues" is the defect this note exists to avoid.

### Writer batches and their temporaries are outside the accounting

The writer releases a chunk's byte reservation when it **dequeues** it, then
accumulates up to `WRITE_BATCH_FRAMES` (32) frames in `self._batches` and
`np.concatenate`s them. For a 2048 × 2048 uint16 camera that is 256 MiB of
accumulated input plus another 256 MiB for the concatenated copy — 512 MiB
that no budget sees, against the 25 % this note's first draft "reserved" for
exactly such data.

**Contract:**

- the reservation is released when the batch is **written**, not when it is
  dequeued, so accumulated frames stay inside the queue budget;
- the batch is filled into a **preallocated** `(WRITE_BATCH_FRAMES, *shape)`
  buffer per detector instead of being concatenated, which removes the
  transient double and makes the batch's cost exactly one buffer, declared
  and constant;
- a single payload larger than the whole budget is admitted **alone** (the
  existing `queuedBytes == 0 or …` rule) — waiting for room that can never
  appear is a deadlock, not backpressure. A frame larger than the batch buffer
  is written directly, bypassing batching.

With both closed, in-flight bytes = queue (including accumulated batches) +
one batch buffer per recording detector, and both terms are in the budget.

## What is exposed

One quantity per module, in plain language, with the shares derived:

```
acquisitionMemoryBudgetMB = 1024   # RAM ImSwitch may hold in flight, camera -> disk
processingMemoryBudgetMB  = 1024   # RAM ImProcess may spend on one display-time operation
```

### Acquisition (imcontrol)

| Derived | Share | At the default | Today's literal |
|---|---|---|---|
| writer queue (`WRITER_QUEUE_MAX_BYTES`) | 50 % | 512 MiB | 512 MiB |
| detector queue pool (`MAX_QUEUED_CONSUMER_BYTES`) | 25 % | 256 MiB **across all queues** | 256 MiB *per queue* |
| batch buffers (`WRITE_BATCH_FRAMES` × frame × detectors) | ≤ 25 % | derived, capped | unaccounted |

The batch share is not a free parameter: it is `WRITE_BATCH_FRAMES × frame
bytes × recording detectors`, computed when the recording arms. If that
exceeds its share the batch depth is reduced for that recording (and logged),
rather than the budget being exceeded silently. That makes the third row a
*consequence* of the first two, which is the property the whole note is after.

The defaults reproduce today's constants for the single-detector,
single-consumer case, which is every in-tree test and most rigs. A multi-camera
rig gets a *smaller* per-queue allowance than today — that is the point, and it
is a behaviour change to call out in the changelog rather than hide.

### Processing (improcess)

| Derived | Share | At the default | Today's literal |
|---|---|---|---|
| contrast working set (`_SAMPLE_WORKING_SET_BYTES`) | 25 % | 256 MiB | 256 MiB |
| live poll (`LIVE_POLL_MAX_BYTES`) | 6.25 % | 64 MiB | 64 MiB |
| materialise-on-open ceiling | 100 % | 1 GiB | *(no such rule today)* |

The third row is the one worth having. `FileIOController._loadAsCurrent`
defaults to `virtual=False`, so opening a file materialises it whole —
`DataObj.checkAndLoadData()` does an unbounded `.asarray()` **on the GUI
thread**, which the audit verified and this branch did not fix (it bounded the
*preview*, not the load). With a budget the rule writes itself: a dataset
larger than the budget opens virtually, with a log line naming the setting.

Two things that rule must get right (review round 1):

- **The comparison is the decoded dataset, not the file.** `shape ×
  dtype.itemsize` of the *selected* dataset, read from metadata without
  materialising. A compressed HDF5 well under a gigabyte expands to many; a
  Zarr source is a directory; one HDF5 file holds several datasets, and only
  the selected one is being opened.
- **Virtual is not automatically bounded.** `TiffVirtualArray.__init__` sets
  `supports_lazy_indexing = False` when `series.aszarr()` fails, and then
  `__getitem__` serves *every* plane by `self.asarray()[key]` — a whole-series
  read per plane. Opening such a file "virtually" and letting the mean preview
  stride 256 planes is 256 full-series reads: worse than materialising once.
  Phase C must consult `supports_lazy_indexing` and, when it is false, either
  refuse with a message naming the reason or fall back to a single bounded
  materialisation — never pretend the virtual path bounded anything. The same
  applies to `EagerVirtualArray`.

`MEAN_PREVIEW_MAX_PLANES` (256) stays a count: it bounds *how representative*
the preview is, not how much memory it costs.

## What is deliberately NOT exposed

**`TARGET_CHUNK_BYTES` (4 MiB).** Not an in-flight budget — the on-disk chunk
layout, bounded by the *reader's* chunk cache (h5py's default is 8 MiB), not by
the writing machine's RAM. Raising it on a strong machine makes every file
produced there slower to read on every machine that later opens it. If it is
ever settable it belongs beside the save format as a storage choice.

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
    acquisitionBudgetMB: int = 1024
    processingBudgetMB: int = 1024
```

**Not the setup file.** The setup file describes the microscope; it is copied
between machines and shared with collaborators. A memory budget is a property
of the computer, and putting it there means a laptop's budget riding into a
workstation's rig config.

ImProcess can read this without a new dependency: `load_processing_config`
already calls `configfiletools.loadOptions()` through the single allowlisted
improcess → imcontrol edge, so the budget comes back from the same call that
already happens.

**UI.** There is no preferences dialog today. Ship the contract first and let
the file be hand-edited; a dialog that edits two integers can follow. The value
of this change is the derivation and the validation, not the widget.

## Validation: what stops the knob becoming the next trap

A settable budget is only an improvement if being wrong is *loud*.

1. **A floor at startup**, against the configured detectors' *display* frames:
   a budget that cannot hold a handful of frames of the largest one is refused
   by name at construction, with the frame size in the message.
2. **Revalidation when the payload is known.** The startup check is necessary
   and not sufficient: a point detector's raw frame is the assembled scan
   volume, whose dimensions come from the scan, not from the manager
   (`APDManager` constructs with `fullShape = (100, 100)` and reallocates in
   `initiateImage` from `ScanWorker._output_image_dims`; a 512 × 512 × 256
   uint16 volume is 128 MiB against a 20 kB construction-time shape). The
   budget is therefore re-checked against the real raw payload and dtype when
   the scan is built and when the recording arms, and a scan whose single raw
   frame does not fit is refused there — where the numbers exist — rather than
   discovered as an overflow mid-run.
3. **Every message that quotes a budget names the setting.** The detector
   overflow warning and the `readChunk` overflow exception already quote the
   budget in MiB and the frames it bought for *this* detector; they must end
   with the setting name. Same rule as the rest of the branch: a refusal names
   the control that fixes it.

### The test is stall behaviour, not an inequality

The first draft made `writer > detector` the correctness argument. It is not
one. Tolerance is additive, so a smaller writer queue backed by a larger
detector queue absorbs a short stall perfectly well, and any split fails a
long enough one; what actually decides survival is incoming byte rate,
total capacity, drain rate and payload size. The 50/25 split is a **policy** —
spend the budget where exhaustion is graceful backpressure rather than a fatal
overflow — worth keeping as a default and not worth asserting as a law.

So the test is a stall test: a storer that blocks for `T`, a producer paced at
`R` bytes/s, and the assertion that the recording

- survives when `T·R` fits the total capacity, with no frame lost, and
- fails **predictably and loudly** when it does not — the producer-stall
  warning first, then an overflow naming the detector, the budget and the
  setting; never a silent gap.

Run it at the default split and at an inverted one, to pin that the behaviour
degrades with capacity rather than with which side holds it.

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
working set, the live poll, the unbounded open. A VRAM setting touches none.

## Phases

- **A — accounting, no setting yet.** Close the two gaps: pool the detector
  allowance across queues; hold the writer reservation until the batch is
  written and fill a preallocated batch buffer. Behaviour at today's constants
  is unchanged for one detector with one consumer; the multi-detector change
  is real and goes in the changelog. Tests: pool admission across two
  detectors, batch accounted, oversized payload admitted alone.
- **B — the contract.** `MemoryOptions` on `Options`; one `memory_budgets.py`
  in imcommon deriving the shares; the literals read from it. Defaults
  identical to A. Tests: derivation table, defaults unchanged, batch share
  reduces batch depth rather than overrunning.
- **C — validation.** Startup floor; revalidation at scan build and at arm;
  messages name the setting; the stall test above.
- **D — materialise-on-open.** Decoded-size comparison, `supports_lazy_indexing`
  check with an explicit refusal or bounded fallback, and a test for the
  non-lazy TIFF path specifically. Closes the unbounded-`asarray` finding.
- **E — optional.** Config-editor fields.
- **Later, separately.** GPU chunking in the Snouty deskew and the denoiser;
  `gpuMemoryBudgetMB` once either can honour it.

## Open questions for review

1. **Aggregate or per-queue.** The note specifies a pooled detector allowance
   because that is what makes the declared number true. It is the largest piece
   of work here. Is the honest alternative — naming the setting
   `perDetectorQueueMB` and dropping the process-wide claim — preferable for a
   first pass?
2. **The shares.** 50/25/≤25 lands on today's constants for the common case.
   Is reproducing them the right anchor, or should the split be re-derived from
   measured rig throughput?
3. **One budget or two?** A single `imswitchMemoryBudgetMB` is simpler to
   explain, but acquisition and processing usually run in different processes.
4. **Per-rig override.** May a setup file *lower* the machine budget (a rig
   sharing a workstation), or does that reintroduce the travelling-config
   problem?

## Review round 1 (2026-09-21)

Six findings, all confirmed against the code; four changed the design.

1. **[P1] The detector share was not a module-wide limit.** The allowance is
   per `(detector, consumer)`. Draft claimed a process-wide total. → pooled
   allocator specified, with the honest per-queue naming as the stated
   fallback.
2. **[P1] The reserved 25 % could not cover writer batches.** The reservation
   is released on dequeue and `np.concatenate` doubles the batch transiently:
   512 MiB for a 2048² camera against 256 MiB of "headroom". → reservation
   held until written, preallocated batch buffer, batch share derived and
   capped, oversized-payload rule stated.
3. **[P1] The open threshold must use decoded dataset size.** "A file larger
   than the budget" is wrong for compressed HDF5, Zarr directories and
   multi-dataset files. → `shape × itemsize` of the selected dataset, resolved
   before the phase rather than left to implementation.
4. **[P1] Opening virtually does not always bound anything.**
   `TiffVirtualArray` falls back to whole-series `asarray()` per plane when
   `aszarr()` fails, so a bounded preview becomes 256 full-series reads. →
   `supports_lazy_indexing` consulted, explicit refusal or bounded fallback,
   test for that path.
5. **[P2] Startup validation cannot see later scan payloads.** A point
   detector's raw frame is the assembled volume, known at scan build. →
   revalidation at scan build and arm.
6. **[P2] `writer > detector` is policy, not a safety invariant.** Tolerance
   is additive; survival depends on rate, capacity, drain and payload size. →
   the ordering is demoted to a default policy and the correctness argument
   replaced by a stall test at two splits.
