# Live Reconstruction Audit — upstream comparison & root causes

Date: 2026-06-24

Scope: why live reconstruction is "not right" and fails for both Zarr and
HDF5, focused on file watching and frame getting. Compares the upstream
ImSwitch (`imreconstruct`) approach with the current ImSwitch2 (`improcess`)
streaming pipeline and identifies concrete root causes.

## TL;DR

Upstream never reads a recording until it is **finished**, then reconstructs
the whole file once. ImSwitch2 reads frames **while the recorder is still
writing**, which is much harder and is where it breaks. The two universal
failure modes are:

1. **Zarr partial-read race** — the recorder resizes the array *before* writing
   the frame data, and the live reader trusts `array.shape`, so it can read
   (and permanently bake in) uninitialised/zero frames.
2. **Completion detection is unreliable** — HDF5's final `writing=False` flag is
   invisible to the live SWMR reader, so completion depends entirely on
   `recording:expected_frames`; when that is absent (SpecTime/UntilStop) or
   wrong, the stream hangs or stops early.

Both are consequences of streaming-during-write without a "committed frames"
barrier. Upstream sidesteps both by construction.

## How upstream handles it (imreconstruct)

`imswitch/imreconstruct/controller/WatcherFrameController.py` +
`imswitch/imcommon/view/guitools/FileWatcher.py`:

- `FileWatcher` is a single `QThread` that polls a directory once per second
  and emits the set of newly-appeared files by extension.
- For each new file the controller opens it, calls `DataObj.checkLock()`, which
  **raises `OSError` if the `writing` attribute is still True**, and on that
  error puts the file back to be retried on the next poll.
- Only once the file is complete does it emit `sigReconstruct(dataObjs, True)`
  and reconstruct the **entire** file in one batch (synchronously), then move to
  the next file. One file at a time, guarded by an `execution` flag.

There is essentially **one** worker thread (the watcher); reconstruction is
whole-file and synchronous. It is simple and robust because it never races the
writer: it reads only finished data.

## What ImSwitch2 does (improcess)

A much more ambitious live-streaming design with **two** worker threads per
store, driven by a folder-watching controller:

- `LiveModeController` — a `QTimer` (1 s) recursively walks the folder
  (`_discoverStores`), dedups/groups per-file timelapse members (`_lapse_key`),
  queues stores, and starts one at a time
  ([LiveModeController.py:121](../imswitch/improcess/controller/LiveModeController.py:121),
  [:232](../imswitch/improcess/controller/LiveModeController.py:232)).
- `LiveReconstructionController` — spins up two `QThread`s:
  `LiveStreamWorker` (open-with-retry → collect first stack → gate on
  `begin()` → poll the growing array, emitting `Chunk`s) and `LiveProcessWorker`
  (push chunks into a `StreamingSession`, emit results at a cadence)
  ([LiveReconstructionController.py:171](../imswitch/improcess/controller/LiveReconstructionController.py:171)).
- `live/sources.py` — per-format `LiveSource`s whose `poll()` reads
  `array[cursor:readable_length]` and advances a cursor; `is_complete()` decides
  when to stop.

The first-stack handoff is actually **correct**: the worker buffers exactly
`frames_per_stack` frames for `begin()`, routes any overflow to
`_pending_chunks`, and `MonalisaLiveSession.begin()` pushes the first stack with
global indices `[0, frames_per_stack)`
([live_session.py:204](../imswitch/improcess/reconstructors/monalisa/live_session.py:204)),
so the first stack is neither lost nor double-counted. The bugs are in the
source layer, not the handoff.

## Root causes

### 1 (CRITICAL, Zarr). Resize-before-write partial-read race

`ZarrStorer.writeFrames` resizes then assigns, with no barrier:

```
dataset.resize((newSize, ...))   # RecordingManager.py:462 — shape grows first
dataset[it:newSize, :, :] = frames  # :463 — data written after
```

`ZarrLiveSource` re-opens the store each poll and trusts the shape:

```
readable_length = min(current_length, expected_frames)  # sources.py:410
data = self._array[start:end]; self._cursor = end       # sources.py:281
```

If a poll lands between the resize and the chunk write, the reader sees the new
length but the new chunk files do not exist yet → zarr returns the fill value
(0). The cursor then advances past those frames, so the zeros are **permanent**
in the reconstruction. Intermittent, timing-dependent → "sometimes not right".
Unlike HDF5, Zarr has no flush/refresh barrier between writer and reader.

### 2 (CRITICAL, HDF5). Completion depends solely on `expected_frames`

The recorder sets the dataset's final `writing=False` by **reopening the file
in `r+` after closing its SWMR session**
([RecordingManager.py:838-841](../imswitch/imcontrol/model/managers/RecordingManager.py:838)).
A live reader holding an SWMR handle and calling `dataset.refresh()` refreshes
the data extent, **not** an attribute rewritten by a separate `r+` reopen — so
the live reader keeps seeing `writing=True`
([sources.py:948](../imswitch/improcess/live/sources.py:948) `is_complete`).
Completion therefore reduces to `cursor >= expected_frames`. When
`recording:expected_frames` is present (SpecFrames/ScanOnce/ScanLapse) this
works, but for SpecTime/UntilStop it is never written, so the **HDF5 stream
never completes** (hangs), and any mismatch between `expected_frames` and the
frames actually written stops the stream early or late.

### 3 (MEDIUM, both). Streaming reads ahead of committed data generally

Both formats lack a single source of truth for "frames the recorder has
actually committed". `_readable_length()` trusts `array.shape` /
`dataset.shape`, which (Zarr) can run ahead of the data and (HDF5) is only safe
because of the post-write `file.flush()`
([RecordingManager.py:780](../imswitch/imcontrol/model/managers/RecordingManager.py:780)).
There is no explicit committed-frame count the reader can rely on across
formats.

### 4 (MEDIUM, both). Over-eager lapse grouping

`LiveModeController._lapse_key` treats **any** store whose name ends in
`[_-.]<digits>` as a per-file timelapse member
([LiveModeController.py:185](../imswitch/improcess/controller/LiveModeController.py:185)),
grouping unrelated single recordings (e.g. `..._rec_1.zarr`) into one
multi-file lapse job and reconstructing them through the wrong
(`*MultiFileLapseSource`) path. This corrupts geometry/accounting for stores
that merely happen to end in a number.

## Recommended improvement

The core fix is to give the reader a **committed-frames barrier** so it never
reads ahead of finished data, and reliable completion — bringing upstream's
"only read finished data" guarantee into the streaming model without losing
live updates.

- **Storer side (RecordingManager):** after each batched write+flush, update a
  single authoritative attribute, e.g. `recording:frames_committed` (and set it
  one last time at finalize). Cheap, format-agnostic, and the natural source of
  truth.
- **Source side (live/sources.py):** `_readable_length()` becomes
  `min(frames_committed, expected_frames or ∞)` instead of trusting raw shape.
  Zarr stops reading uninitialised frames; HDF5 stops depending on the
  invisible `writing` flag.
- **Completion:** complete when `cursor >= expected_frames` (when known) OR
  `frames_committed` has stopped growing AND the store is marked done
  (`writing=False` for Zarr, which the reader *can* see; a final
  `frames_committed`/`complete` marker for HDF5). Add a stall fallback (no
  growth for N polls + stable mtime) so SpecTime/UntilStop terminate.
- **Grouping:** tighten `_lapse_key` to the explicit `scan<NN>` template +
  metadata (`recording:single_lapse_file=False` and `num_timepoints>1`), and
  drop the bare trailing-digit heuristic.

### Alternative (max robustness, less live)

If live-incremental updates are not essential, adopt the upstream guarantee
directly: don't open a store for reconstruction until it is complete
(`writing=False` / `frames_committed` final), then stream it once start-to-end.
This removes every write-race by construction and keeps multi-timepoint
accumulation; it only gives up sub-stack live preview.

## Suggested order

1. Add `recording:frames_committed` to the Zarr + HDF5 storers (one barrier
   attribute, updated after flush).
2. Make `_readable_length()` honour it in all live sources; re-verify the
   first-stack/overflow path.
3. Fix completion (committed-stalled + done-marker + stall fallback) so HDF5 and
   SpecTime/UntilStop terminate.
4. Tighten lapse grouping.
5. Streaming race/■completion regression tests with a fake recorder that resizes
   before committing, to pin the contract deterministically.

## Status update (2026-07-02): per-timepoint progressive lapse updates

The "Alternative (max robustness, less live)" completion gate landed first
(commit `ba44cd38`). On top of it, per-timepoint progressive updates for
multi-file lapses are now deliberate behavior rather than an accident:

- **Multi-file lapse jobs start on seed-file completion**
  (`LiveModeController._is_lapse_complete`), not on the full file set. The
  multi-file sources tail-follow later timepoint files and their
  `is_complete()` ends the job, so each timepoint reconstructs as it lands.
  Previously this liveness only happened because `_read_num_timepoints` read
  file-ROOT attrs that neither storer writes (`recording:*` lives on the
  dataset), which forced the lenient unknown-count branch — HDF5 multi-file
  lapses were progressively live by accident, and would have silently lost
  that liveness had the root attrs ever been added.
- **`ZarrMultiFileLapseSource` advance is completion-gated**: it only opens
  the next timepoint store once its `writing` flag clears. Advancing on file
  existence re-triggered root cause 1 (resize-before-write) on every
  timepoint after the first. The HDF5 sibling still advances on existence —
  SWMR flush ordering makes shape-visible imply data-visible, which also
  preserves its (safe) sub-stack streaming within later timepoints.
- **Single-file (`scan{N}` in one file) lapse completion was broken, now
  fixed**: `_check_hdf5_complete`/`_check_zarr_complete` did not understand
  the `scan{N}/{detector}/data` layout and returned False forever, so
  single-file lapse recordings were NEVER reconstructed. They now complete
  when all `recording:num_timepoints` scan groups are present and
  write-complete. Deliberately NOT progressive: between cycles every present
  group is momentarily complete and the recorder reopens the file in append
  mode for the next timepoint, so an early reader would race the recording.
  Progressive single-file lapse updates require the `frames_committed`
  barrier design above (still open).

Still open from the primary recommendation: `frames_committed` for sub-stack
liveness within a running (single) scan, and the SpecTime/UntilStop stall
fallback.
