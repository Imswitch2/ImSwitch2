# Unified ImProcess — initial backend checkpoint

Baseline: `feat/improcess-generalized-session` at `8d629828` (2026-11-04).
Reference only: `live/generalized-directory-watcher` at `fbd446d6`.

## Scope of this patch

This is the first **M0 / M1A–M1B foundation** patch, **not the M1 parity gate**.
It does not change the Session UI, queue selection, recording readiness, or
ImControl's direct-detector reconstruction path.

- New UI-neutral `RawInfo` / `Contract` / `Incompatible` API. Existing streaming
  plugins inherit the default one-frame contract (no breaking `make_session`
  change). MoNaLISA explicitly requests a full scan stack for orientation;
  View Only explicitly starts with one frame.
- Adapt `StackInfo` metadata to `(T, *S, Y, X)` without classifying a
  `SpecFrames`/`SpecTime` camera series as a scan stack.
- `IncrementalReader` + `FrameInbox`: finite/growing source reuse,
  exact-once init, contiguous frame ranges, one in-flight acknowledged item,
  chunked reads, no implicit gap filling, safe final partial chunks, bounded
  buffering, and pause at timepoint boundaries.
- `QtIncrementalRunner` owns separate reader/processor QThreads, explicit
  queued signals, generation-aware outcomes, nonblocking cancellation and
  post-QThread-finished lifetime retention. No GUI/viewer dependency.
- Targeted ImControl Zarr-v3 `_RetryingLocalStore` fix for Windows atomic
  rename conflicts, from the watcher reference, with its Windows-only test.

### Integration boundary

The new runner currently consumes the **existing** ImProcess `LiveSource`
interface, whose six Zarr/HDF5 layout readers and Session timepoint indexing
remain in place. The DirectoryWatcher monolith is *not* copied or wired.
Future M1 work must explicitly validate each physical reader and metadata
layout against the new contract and cover real Zarr/HDF5 files before queue
integration. The Session still runs the old path by design.

The reader's `is_complete()` means its finite input is exhausted, **not**
that a Follow Auto acquisition is safe/finalized. M2 must supply the separate
marker-based readiness gate before using this backend for Auto jobs. It must
not infer completion from expected frame counts, silence or file stability.

## Verification on this patch

- `compileall` of modified/new Python modules: PASS.
- `git diff --check`: PASS.
- Pure NumPy reader/contract tests: **6 passed**, executed in a minimal
  isolated package harness (see test source). The full repository's root
  `conftest.py` cannot load here because Qt is not installed.
- Qt worker smoke test: **NOT RUN** (qtpy, Qt bindings, pytest-qt unavailable).
- Windows Zarr rename regression: **NOT RUN** (Windows + Zarr-v3 needed).
- Existing Session/queue/preview/real data tests: **NOT RUN** here (Qt/Zarr
  dependencies unavailable; this patch preserves the old path unchanged).

These missing validations are not reported as passes. In the intended
`imswitch2` conda environment, run at least:

```bash
pytest -q imswitch/improcess/_test/test_generalized_streaming_engine.py
pytest -q imswitch/imcontrol/_test/unit/test_zarr_storer_rename_retry.py
pytest -q \
  imswitch/improcess/_test/test_processing_queue.py \
  imswitch/improcess/_test/test_processing_session.py \
  imswitch/improcess/_test/test_session_processing_stability.py \
  imswitch/improcess/_test/test_single_file_lapse_reading.py \
  imswitch/improcess/_test/test_live_end_to_end.py \
  imswitch/improcess/_test/test_memory_live_controller.py
```

## Next checkpoint before M2

1. Validate the Qt worker smoke test and cancellation/shutdown, including on
   Windows. No blocking `QThread.wait()` is used in the new runner.
2. Cover source-format variants (six logical ImControl layouts, Zarr/HDF5),
   metadata inspection and the no-silent-detector-default rule, with finite
   and growing real stores. Preserve exact `scanN` identity, short/missing TP
   semantics and acquisition layout. Confirm Windows HDF5 finite-read safety.
3. Test the MoNaLISA full-stack orientation initialization on a real recording
   and View Only camera-series startup, with the actual Session/viewer axis
   shapes. Capture timings and peak memory; the large init stack must fit the
   configured budget, including read-ahead and transient copies.
4. Validate pause/skip asynchronous lifetime under heavy processing before
   replacing the old Session streaming path. Integrate Session result ownership,
   scopes, progress, and provenance in M2, not in this patch.
