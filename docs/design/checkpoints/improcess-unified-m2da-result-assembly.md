# ImProcess unified processing — M2D-A semantic result assembly and publication

**Baseline:** `feat/improcess-generalized-session` @ `8d629828`, after M1A/M1B/M2A/M2B/M2C (M2C ZIP).
**Scope:** Result semantics, job-relative coverage and shared result-publication lifecycle only. No UI, reconstruction algorithm, Session discovery or queue scheduling changes.

## Implemented

- `execution/result_assembly.py`: immutable `ResultAssemblyPlan` distinguishes logical T, independent Dataset and one-shot output. The plan is explicitly supplied from job/source identity; shape alone never determines the combination axis. Logical T coordinates preserve the requested `TimepointScope`, including nonzero range starts. Multi Data assembly requires explicit distinct dataset identities; automatic inference of membership is deferred.
- `ResultCoverage`: tracks actually completed/published logical positions and missing *frame* ranges separately from the requested scope. Uses compact half-open T ranges for metadata (normal consecutive updates are O(1) to describe). A cancelled/failed result cannot be reported complete; raw frame gaps remain visible in result metadata.
- `validate_append()` is a conservative preflight for future result adapters: semantic axes, result kind, non-assembly geometry, spatial scales, and presence of optional output layers must match. Historical fixed-6D MoNaLISA `T` with `acquisition_projection.components=('time','condition')` is **not** assumed to represent elapsed time. The method does not concatenate arrays.
- `execution/result_publication.py`: UI-neutral `ResultPublication` with injectable callbacks for snapshots, planes, timepoint completion, finalization, Session ownership, and conventional offline result production. Preserves one job-specific result entry (including same-named reruns) and retains already published snapshots after cancellation/failure; cancellation before first publish creates no result.
- `ProcessingQueueController`: M2C's incremental path and existing **final-only** offline path now invoke the shared publication lifecycle. Existing viewer signals, Session claims/provenance, finalization/autosave and queue teardown remain in their current owners. `incremental_completion` is retained for M2C consumers, with new generic `result_assembly` metadata for future PR #79 integration.

## Intentionally deferred

- **M2D-B:** replacing `MonalisaProcessingResult`/`MonalisaLatticeResult` with the canonical MoNaLISA result, including display/export/Legacy and coefficient compatibility.
- **M2D-C:** using per-timepoint `ReconstructionWorker.resultReady` for actual finite progressive assembly and Multi Data publication. M2B's atomic offline merge remains in place until the canonical adapters exist.
- PR #79 UI summary cache invalidation and detailed coverage presentation until that PR is merged. The new metadata does not depend on PR #79.
- Qt/Windows/Zarr end-to-end integration remains a workstation validation checkpoint.

## Validation

- 17 UI-independent semantic/publisher tests pass in the NumPy-only isolated harness.
- 5 targeted actual-controller-method smoke cases pass in a dependency-isolated harness: success, cancel after publication, cancellation before first publication, failure, and atomic offline final result.
- Python compilation and `git diff --check` pass.
- Full Qt/Windows integration and complete Session CI could not run in this environment (Qt bindings and Zarr unavailable). Preserve the already user-validated M2C pipeline and validate final result ownership/autosave with a completed finite lapse and Cancel on the local setup.

## Acceptance checklist on the workstation

1. Fast finite timelapse: T snapshots appear progressively; final result remains one Session entry with correct `result_assembly` logical T ranges and complete status.
2. Cancel after T1..Tk: existing object remains, `result_assembly.status='cancelled'`, original scope and absolute source T indices remain available; cancel before first publish adds no result.
3. Full/Robust finite timelapse: still produces one final result (progressive offline follows in M2D-C), with correct Session ownership/scope/autosave.
4. Repeated same-name runs produce distinct entries. Existing saved results and exporters still work.
