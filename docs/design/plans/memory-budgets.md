# Memory limits: buffering and automatic work, not what can be measured

*Status: proposal, revised after review round 3 (2026-09-21), which reversed
the direction rounds 1 and 2 had taken. Written out of the question the
magic-number audit left open — the audit replaced frame counts with byte
budgets, and the byte budgets are still literals in the source. This note says
how to make them settable without recreating the defect class the audit closed.
Nothing here is implemented, with one exception flagged below: round 3 found
that the byte budget already on this branch drops a large scan volume, and that
fix belongs in PR #29 before it merges. Records of all three rounds at the end.*

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
**whole assembled volume**: `APDManager.getChunk` returns
`expand_dims(self._image_display, 0)`, the raw volume is published once per
completed scan (`rawFrameIsDeferred`), and its dimensions come from
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

**Rule, at both boundaries:** a payload is admitted whenever the queue is
empty, however large; a second payload waits (writer) or overflows (detector)
as today. That is a controlled exception — one oversized payload, never an
unbounded backlog — and it is the same rule in both places. A refusal mode for
constrained installations is not built: it is the rule that restricted
measurement, and nobody has asked for it.

**Estimate, where the payload is known.** A point detector's raw payload is
known when the scan is built and again when the recording arms
(`_output_image_dims` × dtype). At arm the recording logs its estimate against
both limits — and *warns* when one payload exceeds a limit, because the
consequence is real and otherwise invisible: with no backlog possible, a
writer stall is fatal for the next volume. Warn, name the setting, proceed.

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
  `getDisplayedImage2D()`, with `sigma` in pixels.
- The pattern grid overlay is drawn on the displayed image's coordinates.

A strided preview is not a coarse picture; it is wrong input to two
reconstructions. So:

- **The mean preview is estimated before it is computed**: accumulator plus
  result, from the plane shape and dtype. Within `processingWorkingSetMB` it
  is computed exactly, as today. Above it, the *automatic* preview (on load)
  is skipped with a status line naming the estimate and the setting; the
  *explicit* one (the Show-mean button) proceeds after a warning naming the
  same numbers. The operator chose it; the note does not second-guess that.
- **The contrast sample count follows the working set** (round 2, kept):
  `max_samples = min(_MAX_SAMPLE_VALUES, allowance // _WORKING_SET_BYTES_PER_ELEMENT)`,
  tested at small budgets. Sampling is already an approximation with no
  coordinates in it, so this one is free.
- **Opening a file is explicit**, so it is estimate-and-say, not refuse: the
  decoded size of the *selected* dataset (`shape × itemsize`, from metadata —
  round 1, kept), a lazy path where the source truly supports one
  (`supports_lazy_indexing`; `TiffVirtualArray` without `aszarr()` serves
  every plane by a whole-series `asarray()` and must not be called bounded —
  round 1, kept), and one line naming the decoded size when a large dataset is
  materialised anyway. No threshold refuses an open.
- **An exact bounded preview for in-plane-huge sources** — tiled accumulation,
  identical values, more reads — is the later answer if a rig ever has 16k²
  planes. Not a stride.

## Diagnostics: the sum, stated where it is known

- **At arm, one estimate line per recording:** raw payload per detector; the
  two queue limits; batches up to `WRITE_BATCH_FRAMES` × frame per detector
  (the writer batches by count, and `np.concatenate` doubles it for the
  flush); one chunk in hand per detector. An estimate, not a cap: it tells the
  operator what a stall costs and which setting moves it.
- **Warn on the first block, with no delay** (round 2, kept).
  `enqueue_frames` today waits `PRODUCER_STALL_WARN_S` (1 s) before logging,
  and a 256 MiB queue at 512 MiB/s fills in half that, so today the overflow
  can arrive before the line that explains it.
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
  stated in the estimate line, not worth an API.
- **The preallocated batch buffer.** Removes the `np.concatenate` double (up
  to 32 frames per detector) and the `np.stack` copy in `_getNewFrames`. An
  optimisation; measure a rig first.
- **A strict-refusal mode.** Only if a constrained installation asks.
- **Tiled exact preview.** Only for in-plane-huge sources, if any appear.
- **GPU chunking**, then `gpuMemoryBudgetMB`.

## Acceptance

Not "no behaviour change" — round 3 showed that claim was false for what
rounds 1–2 proposed, and it is not the right criterion anyway. **Output
compatibility:** every existing recording test produces byte-identical files;
and a stall test — a storer that blocks for `T`, a producer paced at `R` — run
across four acquisition shapes:

- a large single volume (one frame above `perDetectorQueueMB`);
- small rapid frames (a fast camera on a small ROI);
- burst delivery (many frames per `readChunk`);
- multiple detectors with concurrent consumers (recording plus BeadRec or
  live view).

In each: no frame lost while `T·R` fits the backlog; a predictable, loud
failure when it does not — the block reported first, then an overflow naming
the detector, the limit and the setting; and the oversized volume delivered
intact at both boundaries.

## Phases

- **A — the regression, in PR #29.** Admit an oversized payload at the
  detector boundary when the queue is empty, mirroring the writer; the
  overflow message no longer blames a consumer that was handed nothing. Test:
  one raw volume above `MAX_QUEUED_CONSUMER_BYTES` reaches `readChunk` whole;
  a second one behind it overflows as today.
- **B — the settings.** `MemoryOptions` on `Options`; the three literals read
  from it; contrast sample count follows the working set. Defaults reproduce
  every literal. Tests: defaults unchanged; small-budget behaviour for each.
- **C — estimates and diagnostics.** The arm-time estimate line; the
  oversized-payload warning; warn on first block; messages name the setting.
- **D — safer automatic work.** Mean preview estimated, automatic skip above
  the working set, explicit warn-and-proceed; decoded size and
  `supports_lazy_indexing` on open. Closes the unbounded-`asarray` finding.
- **E — optional.** Config-editor fields; the four-shape stall test as a
  slow-marked suite.

## Decisions

- **Per-queue naming** (2026-09-21, round 2): the detector allowance stays per
  `(detector, consumer)` and is named so. Round 3 keeps this and removes the
  alternative — there is no pooled total for it to fold into.
- **Fixed defaults, not machine fractions** (2026-09-21, round 2, as "a fixed
  2048 MiB total"). The pooled total that number applied to is gone after
  round 3; what survives of the decision is its reason — the same default on
  every machine — applied to the three literals. *Flagged for confirmation:
  this restates a decision rather than keeping it verbatim.*

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
