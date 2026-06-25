# Task: Live reconstruction — upstream-parity completion gate (Phase 1)

You are working in the ImSwitch2 repo on branch `feat/live-reconstruction`.
Background and rationale: `docs/live_reconstruction_audit.md`. The chosen
direction is **upstream parity: never reconstruct a recording store until it is
COMPLETE, then read it whole.** This removes the Zarr resize-before-write
partial-read race and the HDF5 "never completes" hang by construction, because a
finished store has all data present and is read start-to-end with no writer
racing it.

Scope this change to `LiveModeController` + its tests. Do NOT change the live
sources, the storers (RecordingManager), or the worker threads. Do not commit.

## Why this works

A *completed* store already flows correctly through the existing streaming
machinery: `ZarrLiveSource`/`Hdf5LiveSource` read all frames and
`is_complete()` returns True once the cursor reaches the end. The only problem
today is that stores are handed to the reconstruction controller *while still
being written*. So the fix is purely a **gate**: only call
`self._liveController.start(...)` for a store once it is complete.

This mirrors upstream `imreconstruct/WatcherFrameController` +
`DataObj.checkLock()`, which retries a file until its `writing` attribute is
False, then reconstructs the whole file.

## Implementation — `imswitch/improcess/controller/LiveModeController.py`

### 1. Add a completeness check

Add `_is_store_complete(self, store_path: str, is_lapse: bool) -> bool`.

**Single-file (`is_lapse=False`):**
- Open the store FRESH and read-only — Zarr: `zarr.open(store_path, mode='r')`;
  HDF5: `h5py.File(store_path, 'r')` (a *plain* open, NOT swmr). Do this in a
  `try/except` over `(OSError, PermissionError, KeyError, ValueError, Exception)`
  and **return False on any failure** (a store still held by the SWMR writer, or
  half-created, is "not ready" → retry later). Always close handles
  (`with`/`finally`).
- Locate the detector dataset's `writing` attribute:
  - Zarr: if the root is an array, use its attrs. Otherwise find the first
    detector: a child group containing a `data` array → that `data` array's
    attrs; or a child array → its attrs.
  - HDF5: find the first group containing a `data` dataset → that dataset's
    attrs; otherwise the first dataset's attrs.
  - Reuse the structured-layout detection already used by the live sources for
    consistency (`<detector>/data`).
- Decide:
  - dataset/array not found yet → return False (still being set up);
  - `writing` attribute present and truthy → return False (still writing);
  - `writing` attribute present and falsy → return True (complete);
  - `writing` attribute ABSENT → return True (legacy/external store: assume
    complete, matching upstream `checkLock`'s "no writing attr ⇒ proceed").
  - Use the same bool coercion the sources use (`'0'/'false'/'no'/'off'`/empty
    ⇒ False; treat string/np types defensively).

**Lapse (`is_lapse=True`):**
- Use `_lapse_index_template(store_path)` (already imported from
  `imswitch.improcess.live`/`sources`) to get `(folder, prefix, width, suffix,
  first_index)`. If it returns None, fall back to the single-file check on
  `store_path`.
- Read `recording:num_timepoints` from the seed file's metadata (reuse the
  single-file fresh-open path; tolerate missing/unreadable → None).
- If `num_timepoints` is known (≥1): complete iff every timepoint file
  `prefix + str(first_index+i).zfill(width) + suffix` for `i` in
  `range(num_timepoints)` EXISTS **and** the last one passes the single-file
  completeness check.
- If `num_timepoints` is unknown: complete iff the highest *contiguous* present
  index file passes the single-file completeness check (process the complete
  files that are present; conservative but never reads an in-progress file).

Keep this helper free of side effects (no cursors, no caching beyond locals).

### 2. Gate processing on completeness

Rework `_processNextStore`:
- If `self._currentlyProcessing`: return.
- Iterate `self._storeQueue` (a `deque`) and pick the FIRST
  `(store_path, is_lapse)` for which `_is_store_complete(store_path, is_lapse)`
  is True. Remove THAT entry from the queue (not necessarily the head), set
  `_currentlyProcessing = True`, and proceed with the existing source-creation +
  `self._liveController.start(...)` logic unchanged.
- If no queued store is complete yet: return (it will be retried on the next
  timer tick). Do not pop incomplete stores; leave them queued.
- Preserve the existing "started is False ⇒ advance" safety so a store that
  opens-but-yields-nothing still advances.

This avoids head-of-line blocking: a still-recording store does not stall a
later already-complete one.

### 3. Re-check pending stores every tick

In `_scanForStores`, ALWAYS call `self._processNextStore()` at the end (today it
is only called when `new_jobs > 0`). Otherwise a store enqueued while still
writing would never be re-evaluated once writing finishes.

## Reconcile existing tests (IMPORTANT)

`imswitch/improcess/_test/test_live_discovery.py` has tests that manually
enqueue **non-existent** paths and patch the source factory, e.g.
`test_sequential_processing_one_at_a_time`, `test_lapse_job_builds_multifile_source`,
`test_non_lapse_job_uses_make_live_source`. With the new gate,
`_is_store_complete` on a non-existent path returns False, so `start()` would
never be called and these tests would break.

Those tests verify **source selection / sequencing**, not the completion gate.
Reconcile them with the LEAST-invasive change that preserves their intent —
prefer patching the gate to True for those specific tests, e.g.
`patch.object(LiveModeController, '_is_store_complete', return_value=True)`
(or create real complete stores at those paths). Do not weaken the gate itself
to make them pass. Run the whole improcess live suite and fix any other test
that implicitly assumed an incomplete/absent store gets processed immediately.

The discovery/grouping tests that only call `_discoverStores`/`_lapse_key`
(and `_make_zarr`/`_make_h5` helpers, which create stores with no `writing`
attr ⇒ "complete") should be unaffected.

## New tests — `imswitch/improcess/_test/test_live_completion_gate.py`

Follow the fakes in `test_live_discovery.py` (`_make_controller`,
`_FakeLiveReconstructionController` recording `start_calls`). Use `tmp_path` and
build real stores. Cover:

1. **Incomplete single-file store is not processed.** Build a structured Zarr
   store with the detector `data` array's `writing` attr = True; enqueue +
   `_scanForStores`/`_processNextStore`; assert `start` was NOT called.
2. **Becomes processed once complete.** Flip `writing` to False (or finalize);
   call `_processNextStore` again; assert `start` IS called with that store.
3. **Legacy store (no `writing` attr) is treated complete** and processed.
4. **HDF5 equivalents** of 1–3 (structured `<det>/data`, `writing` attr at the
   dataset).
5. **Lapse gating:** with `recording:num_timepoints=3`, only 2 of 3 timepoint
   files present (or the last still `writing=True`) ⇒ not processed; all 3
   present with the last complete ⇒ processed once.
6. **No head-of-line blocking:** queue `[incomplete, complete]`; assert the
   complete one is processed.

Keep tests deterministic — call `_scanForStores`/`_processNextStore` directly
(no real QTimer), as the existing discovery tests do.

## Acceptance criteria

1. A store is handed to `LiveReconstructionController.start` ONLY once
   `_is_store_complete` returns True; incomplete stores are retried on
   subsequent ticks; no head-of-line blocking.
2. New `test_live_completion_gate.py` passes; existing live tests reconciled and
   green:

   ```
   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytestqt.plugin \
     imswitch/improcess/_test/test_live_discovery.py \
     imswitch/improcess/_test/test_live_controller.py \
     imswitch/improcess/_test/test_zarr_live_source.py \
     imswitch/improcess/_test/test_hdf5_live_source.py \
     imswitch/improcess/_test/test_live_end_to_end.py \
     imswitch/improcess/_test/test_zarr_multifile_lapse_source.py \
     imswitch/improcess/_test/test_hdf5_lapse_source.py \
     imswitch/improcess/_test/test_live_completion_gate.py -q
   ```

   (Use the `python` on PATH — conda 3.12; `-p pytestqt.plugin` is REQUIRED.)
3. Only `LiveModeController.py` and the test files change. No storer/source/
   worker changes. Do not commit.

## Notes / guardrails

- Sequential, single-checkout workflow; edit directly in this checkout.
- Tradeoff being made deliberately (already decided): we give up sub-stack live
  preview. Per-timepoint progressive lapse updates are a possible future
  refinement; this task gates at the store/lapse-job level.
- When done, print a concise summary: files changed, the tests added/reconciled,
  and the final test result line.
