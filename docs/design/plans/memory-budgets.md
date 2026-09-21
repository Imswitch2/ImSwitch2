# Memory budgets: one declared number, derived shares

*Status: proposal, awaiting review. Written 2026-09-21 out of the question the
magic-number audit left open — the audit replaced frame counts with byte
budgets, and the byte budgets are still literals in the source. This note says
how to make them settable without recreating the defect class the audit closed.
Nothing here is implemented. Not part of PR #29.*

## The question

> Couldn't `WRITER_QUEUE_MAX_BYTES` be a global parameter, editable under
> preferences, to optimize for good and less good hardware? The same could be
> useful for ImProcess to specify VRAM (default 1 GB)?

Yes for a settable budget. No for one setting per constant — and that
distinction is the whole content of this note.

## Why not one field per constant

The budgets are not independent numbers. They stand in a fixed order, and the
order is what makes them work:

| Budget | Today | Its job |
|---|---|---|
| `WRITER_QUEUE_MAX_BYTES` (`RecordingManager.py:83`) | 512 MiB | absorbs a disk or compression hiccup between the acquisition loop and the storer |
| `MAX_QUEUED_CONSUMER_BYTES` (`DetectorManager.py:95`) | 256 MiB | per-detector chunk queue, shared by every consumer (recorder, live view, tiling) |
| `PRODUCER_STALL_WARN_S` (`RecordingManager.py`) | 1.0 s | warns that the producer is blocked, *before* the overflow it causes |

The writer queue must stay **larger** than the detector chunk queue. It is the
buffer that is supposed to absorb the hiccup; if it is the smaller of the two,
the acquisition loop blocks on a full writer queue, the detector's own queue
fills behind it and overflows first, and the recording aborts where it should
have waited. That is the Snouty chunk-cap failure in a new costume — and it is
exactly what the branch found and fixed: before the byte budget the writer
queue held 64 *items* (about 64 frames, ~7 ms), which was already shorter than
the detector-side budget downstream of it, so *a recording could not use its
own buffer*.

Expose the three as three fields and a well-meaning operator "optimising for a
weaker laptop" sets the writer queue to 64 MB, leaves the detector queue at
256 MB, and the failure returns — this time with a user-authored config to
blame it on, and no test that can catch it because the configuration is not in
the repository.

**The audit's rule applies to its own numbers: state the fact once, derive
what follows from it.**

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
| detector chunk queue (`MAX_QUEUED_CONSUMER_BYTES`) | 25 % | 256 MiB | 256 MiB |

The remaining 25 % is deliberately unallocated: it is the headroom the frames
in flight *outside* either queue occupy (the batch being assembled, the chunk
being compressed, the copy a storer makes). Budgeting it away would make the
declared number a lie in the direction that matters.

The defaults reproduce today's constants exactly, so an installation that never
opens the setting behaves identically. The ordering invariant (writer >
detector) holds at every budget by construction, and a test pins it.

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
*preview*, not the load). Today the only way to avoid it is knowing to pick
"Open data as a lazy virtual stack" from the toolbar. With a budget the rule
writes itself: a file larger than the budget opens virtually, with a log line
saying so and naming the setting. That converts a trap into a declared policy.

`MEAN_PREVIEW_MAX_PLANES` (256) stays a count: it bounds *how representative*
the preview is, not how much memory it costs, and 256 planes of a large frame
is already inside any sane budget.

## What is deliberately NOT exposed

**`TARGET_CHUNK_BYTES` (4 MiB, `RecordingManager.py:126`).** It is not an
in-flight budget — it is the on-disk chunk layout, and its bound comes from the
*reader's* chunk cache (h5py's default is 8 MiB), not from the writing
machine's RAM. Raising it on a strong machine makes every file produced there
slower to read on every machine that later opens it, including the strong one.
If it is ever settable it belongs beside the save format as a storage choice,
never inside a memory budget.

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
workstation's rig config — a fresh instance of "a fact right for the case it
was written for, silently wrong for its neighbour".

ImProcess can read this without a new dependency: `load_processing_config`
already calls `configfiletools.loadOptions()` through the single allowlisted
improcess → imcontrol edge, so the budget comes back from the same call that
already happens. The layering guard test needs no change.

**UI.** There is no preferences dialog today (options are edited by the config
tooling and by individual controllers). Two honest choices: add the two fields
to the config editor beside the existing sections, or ship the contract first
and let the file be hand-edited until a dialog exists. I would do the latter —
the value of this change is the derivation and the validation, not the widget,
and a dialog that edits two integers can follow whenever it is wanted.

## Validation: what stops the knob becoming the next trap

A settable budget that can be set wrong is only an improvement if being wrong
is *loud*. Three rules:

1. **A floor, checked at startup against the configured detectors.** A budget
   that cannot hold a handful of frames of the largest configured detector is
   refused by name at construction, with the frame size in the message —
   not discovered mid-scan as an overflow. (2048 × 2048 × uint16 = 8 MiB per
   frame; a 64 MB budget is eight frames of it.)
2. **Every message that quotes a budget names the setting that changes it.**
   The detector overflow warning and the `readChunk` overflow exception
   already quote the budget in MiB and the frames it bought for *this*
   detector; they must end with the setting name, so the operator reading the
   failure knows which control produced it. Same rule the rest of the branch
   follows: a refusal names the control that fixes it.
3. **The ordering invariant is a test, not a comment.** `writer > detector` at
   the default, at the floor, and at a large budget.

## VRAM: deferred, and why

There is real GPU code — `cupy` in the Snouty deskew
(`reconstructors/snouty/deskew_gpu.py`) and `torch` in the Denoiser/UNet — and
neither manages memory at all: no pool limit, no chunking, no OOM handling.

So a `gpuMemoryBudgetMB` today would be a number that nothing reads, and a
number that nothing reads is worse than no number: in a preferences dialog it
reads as a guarantee. The budget becomes meaningful the moment a GPU path can
*obey* it — when the deskew chunks its volume to fit a declared cap, and the
denoiser sizes its batch from one. That is implementation work in those two
reconstructors, and it is the work that should be scheduled; the setting is
the easy part and follows it.

Note also which problem is actually being reported today: every ImProcess
memory defect the audit verified was **host** RAM — the mean preview, the
contrast working set, the live poll, the unbounded open. A VRAM setting would
have touched none of them.

## Phases

- **A — contract, no behaviour change.** `MemoryOptions` on `Options`; a
  single `memory_budgets.py` in imcommon that derives the shares; the five
  literals become module-level values read from it. Defaults identical to
  today. Tests: derivation table, ordering invariant, defaults-unchanged.
- **B — validation.** Startup floor check against configured detectors;
  messages name the setting; refusal test.
- **C — the materialise-on-open rule.** `_loadAsCurrent` consults the
  processing budget and opens virtually above it, with a log line. This is the
  phase with user-visible value; it also closes the unbounded-`asarray`
  finding the audit left open.
- **D — optional.** Config-editor fields.
- **Later, separately.** GPU chunking in the Snouty deskew and the denoiser;
  `gpuMemoryBudgetMB` once either can honour it.

## Open questions for review

1. **The shares.** 50/25 (acquisition) and 25/6.25 (processing) are chosen to
   land exactly on today's constants at a 1 GiB budget, which makes the change
   provably behaviour-neutral. Is reproducing today's numbers the right anchor,
   or should the split be re-derived from what the hardware actually needs?
2. **One budget or two?** A single `imswitchMemoryBudgetMB` covering both
   modules is simpler to explain, but acquisition and processing usually run in
   different processes and compete for the same RAM only when they do not.
3. **Per-rig override.** Should a setup file be allowed to *lower* the
   machine budget (a rig that shares a workstation), or does that reintroduce
   the travelling-config problem this note argues against?
4. **Phase C's threshold** is "file larger than the budget opens virtually".
   Should the comparison be against the file, or against the working set the
   *first* operation on it would allocate (the contrast path's 17 B/element)?
