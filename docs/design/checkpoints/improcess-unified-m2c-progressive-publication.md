# ImProcess M2C — Incremental result publication and cancellation

This checkpoint supersedes the **Skip = discard all unfinished output** part of D1
in `improcess_unified_processing_implementation_plan.md` for incremental jobs.
The change was agreed after validating M2A and M2B.

- **Finite** describes an already-recorded source; it does not imply one-shot
  reconstruction or suppress viewer updates.
- A reconstructor with a `StreamingSession` publishes its first result snapshot
  during processing. Later planes/whole snapshots update its existing viewer
  entry using the same live-result signals as the previous Session streaming UX.
- **Cancel/Skip** retains that entry and already published pixels. It stops the
  worker, marks the retained result `partial`, and advances the queue. No new
  confirmation, keep/discard choice, or additional control is added.
- A job stopped before its first preview creates no result. Already published
  snapshots are not retroactively rebuilt from an interrupted worker.
- A completed incremental job finalizes the same viewer entry, **not** a second
  `sigResultProduced` entry. Session ownership and autosave go through
  `sigResultFinalized` after the worker threads have stopped.
- Provenance records partial/complete status, progress and the requested scope;
  a retained partial result must never claim the whole requested scope was
  reconstructed. Partial snapshots retain their source/parameter information.
- A new queue job gets a distinct viewer entry even if its label is identical
  to an earlier run (for example, rerunning the same lapse after Skip).
- Nonincremental reconstructors continue to publish only when they have a
  usable result; they are not required to implement partial publication.

The queue, Session catalog, Review/Follow roadmap and readiness policy remain
unchanged. This M2 refinement precedes M3's Session UI work.
