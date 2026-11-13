# Unified ImProcess — M1B source/identity checkpoint

**Baseline:** `feat/improcess-generalized-session` at `8d629828` with M0/M1A foundation patch already applied.
**Reference:** `live/generalized-directory-watcher` at `fbd446d6` (reference only; not merged).
**Status:** M1B partial, **not yet the Milestone 1 parity gate**. Session, Auto and queue dispatch are not switched to the new executor.

## What this patch adds

- A pure `scanN` group-index helper used by the existing single-file Zarr/HDF5 lapse readers. A root with `scan0` and `scan2` now maps the latter to the correct physical group number, not a list index. A selected scope starting at `scan2` reads that actual group; a scope starting at missing `scan1` fails explicitly. Future appended/mixed logical lapses must still be resolved through Session `TimeLapseIndex` before M2.
- Single-file and per-timepoint-file lapse sources record `skipped_frame_ranges` only when a later real group/file demonstrates that an earlier timepoint is absent or short. Unknown trailing timepoints are **not** inferred finished.
- `FrameInbox` rejects all discontinuities by default; it only accepts those exact source-declared gaps, represents them without allocating full missing stacks, and materializes zero frames when consumed. `IncrementalReader.missing_ranges` and `QtIncrementalRunner.sigCoverage` expose those ranges for later result provenance. This preserves the existing empty-slot treatment, but **M2 must persist coverage/partial status** with the final result.
- The single-file Zarr lapse reader caps actively written arrays at `recording:frames_committed` rather than trusting allocated shape. Older completed stores without that marker remain readable. This does not add live HDF5 reading on Windows.
- A `make_incremental_disk_source` adapter reuses existing `make_live_source()` readers and metadata-only detector inspection, but rejects an ambiguous multi-detector source unless a detector identity is supplied. It is an adapter, **not** a new picker, a watcher, or a readiness gate.
- Chunk sizing for an intentional pause stops at the next timepoint boundary even if normal `chunk_frames` would cross it. Timepoint completion from `begin()` and missing-frame coverage are exposed as executor signals.
- Added focused source and regression tests (HDF5 real files in temporary directories; Zarr cases included for the full environment).

## Behavioral boundaries

- Legacy Session and queue code paths remain in use. The two existing `*LapseSource` implementations **are** shared by the legacy streaming route; their explicit `scanN` indexing and Zarr commit-barrier bug fixes therefore merit full Session regression testing before accepting this patch.
- No Follow Auto readiness logic is introduced here. `LiveSource.is_complete()` is still **reader exhaustion**, not trustworthy logical recording finalization. Do not feed an unmarked source into Auto: M2 must enforce producer lifecycle/commit markers first.
- The Session catalog and `TimeLapseIndex` remain the source of truth for logical acquisition membership. Physical `scanN` numbers and filename patterns **alone** are not sufficient to determine whether groups/files belong to the same logical acquisition (especially for files reused across runs). M2 must reconcile that identity before job submission.
- No automatic missing-*last*-timepoint recovery, no forced partial-result finalization, no detector selection UX, no new `.acquisition` manifests or heartbeat.
- The shared `FrameInbox` uses zero values for explicitly declared missing frames (matching the existing blank-slot policy). Downstream plugins that prefer masks/NaNs need to consume the preserved coverage metadata; this is deferred to result integration.

## Verification in this environment

- `python -m compileall` of modified/new Python: **PASS**.
- `git diff --check`: **PASS**.
- Dependency-isolated NumPy/HDF5 source tests: **15 passed** (previous pure tests + 9 new HDF5/gap/scope/pause checks). These tests used narrow import shims because full ImSwitch imports Qt/Zarr; HDF5 files and actual source class implementation were exercised. This is **not** a full ImSwitch test run.
- Representative uploaded HDF5 fixture metadata: checked `CAM/data` shape `(768, 412, 412)`, uint16 and adjacent `frames_committed` / `stream_complete` datasets. Full reconstruction/numerical output **not** validated.
- Real Zarr physical tests: **NOT RUN** (Zarr unavailable in environment).
- Actual Qt thread tests, Session queue tests and full CI: **NOT RUN** (qtpy/Qt/pytest-qt unavailable).
- Concurrent Windows Zarr writer+reader tests, HDF5 safe-close and direct detector/memory live reconstruction: **NOT RUN**.

## Required next checkpoint before M2

1. Run all focused tests under the project's Windows `imswitch2` environment, including `test_generalized_physical_sources.py`, `test_generalized_streaming_engine.py`, `test_single_file_lapse_reading.py`, `test_scoped_hdf5_completion_regression.py`, Session/timepoint/queue tests, and the Windows Zarr retry test.
2. Validate the six ImControl storage layout families with growing Zarr and finished HDF5 fixtures and explicit detector identities; test logical IDs from `TimeLapseIndex` including appended/multiple-run single-file stores, stopped/short/missing timepoints, and scope snapshots.
3. Run real Qt cancellation/reload/shutdown tests and verify that no worker thread remains running or publishes a canceled result. Do not introduce blocking GUI waits.
4. Profile initialization/streaming memory for MoNaLISA whole-stack startup and viewer preview on representative real data; confirm direct ImControl detector streaming still imports/works.
5. Implement the **separate marker-based readiness policy** in M2; never treat `is_complete()` or an observed expected-frame count as proof of acquisition finalization.
