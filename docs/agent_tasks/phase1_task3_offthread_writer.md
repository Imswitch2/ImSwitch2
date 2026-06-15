File: imswitch/imcontrol/model/managers/RecordingManager.py

Task summary
Move disk I/O and compression OFF the acquisition thread so they never throttle
frame intake, and batch writes for efficiency. This is Phase 1, Task 3 of
docs/recording_dataflow_plan.md. Tasks 1 and 2 (dtype contract + silent-cast
removal) are already merged on this branch — build on them, do not redo or alter
them.

Maintainer decision (do NOT deviate): KEEP compression as the default (preserve
on-disk file sizes) but run it on a dedicated WRITER THREAD fed by a BOUNDED
queue from the acquisition loop. Do NOT switch the default to uncompressed.

Today `RecordingWorker._record` calls `storer.writeFrames(...)` inline on the
acquisition loop, so gzip+shuffle compression and the per-chunk HDF5
resize/assignment block frame intake. Fix: the acquisition loop only reads
frames + manages counters/watchdog/progress, then hands frames to a writer
thread that owns every storer call.

Work on the existing branch `feature/dtype-contract` (do NOT create a new
branch).

Todo list
- Add a dedicated writer thread that owns ALL storer calls (openStream,
  writeFrames, finalizeStream) — h5py/zarr file handles must be touched by one
  thread only.
- Feed it from the acquisition loop via a BOUNDED queue (backpressure, never
  unbounded growth).
- Batch frame writes (accumulate per detector, flush in batches) and use a
  multi-frame HDF5/Zarr chunk shape.
- Preserve all existing recording semantics and pass all existing recording
  tests.

Do NOTs
- Do NOT change the default compression (stays ON for disk; stays OFF for RAM
  mode exactly as today — the existing `_streamCompression` logic).
- Do NOT switch to an unbounded queue. The writer-feed queue MUST be bounded;
  `put` blocks when full (this is intended backpressure, not frame dropping).
- Do NOT drop frames anywhere. Backpressure = block, never discard.
- Do NOT touch the detector-side `time.sleep` poll cadence semantics, the
  ChunkBroker/multi-consumer design, `SaveMode.RAM` BytesIO growth (that hard
  RAM cap is a separate later phase), or Task 1/Task 2 code.
- Do NOT move the stall watchdog, progress-signal emission, frame counting
  (`currentFrame`), or SpecFrames clipping onto the writer thread — those stay
  on the acquisition loop (they must reflect frames READ from hardware).
- Do NOT change the public RecordingManager API or signal signatures.

Implementation
1. Add module constants near the existing recording constants, e.g.
   `WRITER_QUEUE_MAXSIZE = 64` (bounded backpressure) and
   `WRITE_BATCH_FRAMES = 32` (batch flush threshold). Brief comments explaining
   each.
2. Writer thread (use a plain `threading.Thread` + `queue.Queue(maxsize=
   WRITER_QUEUE_MAXSIZE)`; the storer is plain Python, no Qt event loop needed):
   - The writer OWNS the storer lifecycle. On start it calls
     `storer.openStream(...)`. Use a `threading.Event` "opened" handshake so the
     acquisition loop waits until openStream has either succeeded or raised; if
     it raised, store the exception and re-raise it on the acquisition thread so
     existing error behavior (e.g. file-exists) is preserved BEFORE the loop
     emits sigRecordingStarted.
   - Writer main loop: `item = queue.get()`. A `None` sentinel means "no more
     frames". For a real item `(detectorName, frames)`, append to a per-detector
     in-memory batch list; when a detector's accumulated frame count reaches
     `WRITE_BATCH_FRAMES`, concatenate and call `storer.writeFrames(detName,
     batch)` once, then clear that detector's batch.
   - On sentinel: flush all remaining per-detector batches with a final
     `writeFrames`, then call `storer.finalizeStream(...)` (finalize MUST run on
     the writer thread, same thread as the writes). Emitting
     `sigMemoryRecordingAvailable` from finalize is fine (Qt signals are
     thread-safe).
3. Acquisition loop (`_record`): unchanged responsibilities EXCEPT replace the
   inline `storer.writeFrames(detectorName, newFrames)` with a blocking
   `self._writeQueue.put((detectorName, newFrames))`. Keep `currentFrame`
   increment, SpecFrames clipping, progress signals, and the stall watchdog
   exactly where they are (acquisition thread). The blocking `put` provides
   backpressure if the writer falls behind.
4. Lifecycle / shutdown ordering:
   - Start writer thread (and await the "opened" handshake) BEFORE emitting
     sigRecordingStarted and entering the loop.
   - In the `finally` of `_record`, after the read loop ends: release chunk
     consumers (as today), enqueue the `None` sentinel, then `join()` the writer
     thread so finalize completes before `_record` returns and before
     `endRecording` is called. Move the existing `storer.finalizeStream(...)`
     call INTO the writer (do not also call it on the acquisition thread).
   - Ensure the writer thread cannot deadlock on a full queue during shutdown
     (e.g. if recording is stopped early): the join path must keep the queue
     drained / the sentinel must be deliverable. Document the chosen approach in
     a comment.
5. Batched chunk shape: when creating the streaming dataset, set the HDF5/Zarr
   chunk shape to `(min(WRITE_BATCH_FRAMES, ...), Y, X)` instead of `(1, Y, X)`
   so on-disk chunks span multiple frames (better compression ratio + fewer I/O
   ops). Partial final batches are fine (HDF5/Zarr handle partial chunks).
   Keep the lazy create-from-DECLARED-dtype logic from Task 1 intact.

Sanity checks
- Run: `python -m pytest imswitch/imcontrol/_test/unit/ -k "record or chunk or
  detector or apd or dtype" -q` — ALL existing recording tests must still pass
  (correctness, stall watchdog, RAM mode, multi-detector, lapse, dtype
  contract).
- Add a test: a recording to HDF5 produces a file whose dataset is STILL
  compressed (assert `dataset.compression` is set for disk mode) and whose
  frames/values match the input exactly (no loss, correct order).
- Add a test: recorded frame count and frame ORDER are correct when more frames
  are produced than `WRITER_QUEUE_MAXSIZE` and `WRITE_BATCH_FRAMES` (proves
  batching + backpressure preserve all frames in order, none dropped).
- Add a test: an openStream failure (e.g. file already exists with `w-` mode)
  still raises on the caller side and does not hang.
- Confirm no remaining inline `storer.writeFrames` call on the acquisition loop
  and that `finalizeStream` is invoked exactly once, on the writer thread.

Commit instructions
- Stay on branch `feature/dtype-contract`.
- One focused commit. Message summarizing the off-thread writer + bounded-queue
  backpressure + batched chunked writes (compression kept as default), ending
  with the trailer:
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Do NOT push and do NOT open a PR; leave the branch local for review.
