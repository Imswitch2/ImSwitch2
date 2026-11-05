# ImProcess M3A — Review/Follow Session observation checkpoint

This is the first M3 patch, on top of the M2D post-validation refinement.

## Scope

- **Review** is the default on every Session opening. It is passive: the folder
  is discovered once and subsequently only refreshed on explicit user request.
- **Follow** is selected in the compact Session header. The Session reconciles
  once, snapshots existing root entries as a baseline, and begins observing
  newly arriving/changed/removed physical entries.
- Changes in existing recognized growing recordings do not repeatedly reopen
  or rescan the full Session. Unrecognized root entries are retried on change.
- Returning to Review stops the Session watcher, not already queued or
  running reconstruction jobs. No preference is persisted to Session metadata.
- The independent legacy Live watcher remains available, without borrowing a
  Session watcher that can be stopped when switching back to Review. Session
  result-saving ownership is preserved.
- The existing old Auto implementation includes a historical backfill trigger.
  **Auto is intentionally disabled in both modes for this intermediate M3A
  checkpoint** until M3B replaces it with Follow-arrival-only admission. This
  prevents accidentally submitting old datasets while new semantics are incomplete.

## M3B handoff

- Enable Auto in Follow for **new logical arrivals only**, no historical
  backfill and no Auto from manual Refresh.
- Implement optional **Process while acquiring** separately from finite
  progressive output. Non-streaming reconstructors wait for explicit finality.
- Remove per-dataset Auto exclusions and old persisted Auto settings while
  continuing to tolerate existing Session manifests.

## Manual validation in ImSwitch

1. Open an existing Session with many recordings. It must show **Review**;
   the filesystem observer must be stopped. Manual Refresh should discover a
   new recording without processing it automatically.
2. Change to **Follow**. Existing files are catalogued without a new-job
   backfill. Creating a new recording should add it to the list automatically.
3. Change back to Review. A subsequent recording must not appear until a
   manual Refresh. Manual reconstruction and already queued jobs must work.
4. Close/reopen the Session after using Follow. It must start in Review again.
5. Confirm Auto is disabled during M3A and legacy Live remains usable.

Qt/Windows integration testing remains a local checkpoint; the standalone
baseline test environment may not contain Qt or the vendor libraries.
