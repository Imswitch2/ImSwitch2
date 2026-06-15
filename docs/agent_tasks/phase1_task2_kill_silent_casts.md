File: imswitch/imcontrol/model/managers/detectors/APDManager.py
File: imswitch/imcontrol/model/managers/RecordingManager.py

Task summary
Eliminate SILENT dtype casts on the acquisition/recording path. This is Phase 1,
Task 2 of docs/recording_dataflow_plan.md. Phase 1 Task 1 (the dtype contract:
DetectorManager.dtype/bitDepth + storers create from declared dtype + warn-once
on mismatch) is already merged on this branch — build on it, do not redo it.
Two concrete silent casts to fix:
(1) APDManager writes computed pixel values (float in the TTL path, int64 in the
    non-TTL path) into a uint16/float32 buffer via a plain `self._image[...] = `
    assignment — a silent narrowing cast.
(2) RecordingWorker._getNewFrames does `np.array(newFrames)`, which both copies
    and can silently promote/return float64 (e.g. for an empty chunk).

Work on the existing branch `feature/dtype-contract` (do NOT create a new branch;
the agents share one checkout and one branch).

Todo list
- Make the APD pixel-to-buffer write dtype-consistent and EXPLICIT (no silent
  narrowing), aligned with the detector's declared dtype.
- Make RecordingWorker._getNewFrames preserve the native frame dtype (no
  promotion, no float64 empty array) and avoid an unnecessary extra copy.
- Add/adjust unit tests proving no silent cast / dtype preservation.

Do NOTs
- Do NOT change any detector's declared dtype (APD stays uint16 non-TTL /
  float32 TTL; that is the Task 1 contract). This task changes HOW values are
  written, not WHAT dtype is recorded.
- Do NOT touch the poll loop, time.sleep, RAM mode, compression, or the
  ChunkBroker — later phases.
- Do NOT change display-only / live-view conversions or the image viewer.
- Do NOT alter the dtype-contract code from Task 1 (properties, storer
  create-from-declared, warn-once logic).
- Do NOT silently cast anywhere: any unavoidable dtype conversion must be
  explicit in code and commented.

Implementation
1. APDManager.py — updateImage (the `self._image[...] = pixels` writes, around
   the 2D and >=3D branches):
   - The destination buffer dtype is `self._image.dtype` (== self.dtype: uint16
     non-TTL, float32 TTL). Convert `pixels` to that dtype EXPLICITLY before
     assignment instead of relying on numpy's implicit narrowing.
   - For an INTEGER destination (non-TTL uint16): use `np.rint(pixels)` then
     `.astype(self._image.dtype, copy=False)` so values are rounded, not
     truncated, and the cast is explicit. (Counts are integer-valued, so rint is
     a no-op numerically but makes intent and safety explicit.)
   - For a FLOAT destination (TTL float32): cast explicitly with
     `.astype(self._image.dtype, copy=False)` (preserves NaN no-data markers).
   - Add a brief comment noting the uint16 buffer's value range (0..65535) is a
     deliberate contract choice; do NOT change the dtype here, but the explicit
     cast must not hide overflow — leave a `# TODO(phase): uint16 photon-count
     overflow guard` comment if a pixel can exceed 65535.
   - Keep `samples_to_pixels` returning its natural numeric result, but make its
     empty-return dtype match the buffer (`np.zeros((0,), dtype=self.dtype)`
     instead of hard-coded `float`).
2. RecordingManager.py — RecordingWorker._getNewFrames:
   - `readChunk` returns a list of per-frame ndarrays already in the detector's
     native dtype. Replace `np.array(newFrames)` with dtype-preserving stacking:
       - if the list is empty, return an empty array shaped/typed correctly
         (e.g. `np.empty((0,), dtype=self.__recordingManager.detectorsManager
         [detectorName].dtype)`), NOT a float64 `np.array([])`;
       - otherwise `np.stack(newFrames)` (preserves native dtype, no promotion).
   - Do NOT force-cast to the declared dtype here — dtype mismatches are already
     surfaced by the storer's warn-once logic (Task 1). The goal is "no silent
     promotion / no float64 surprise," not casting.

Sanity checks
- Run: `python -m pytest imswitch/imcontrol/_test/unit/ -k "record or chunk or
  detector or apd" -q` — no regressions.
- Add a unit test: drive APDManager.updateImage (non-TTL) with float-valued
  pixels and assert `self._image.dtype` stays the declared dtype AND values are
  correctly rounded (not truncated).
- Add a unit test: _getNewFrames returns an array whose dtype equals the
  detector's native dtype for a normal chunk, and an empty non-float64 array for
  an empty chunk.
- Grep to confirm no remaining `np.array(newFrames)` and no bare
  `dtype=float`/`dtype=np.float64` introduced on the recorded APD path.

Commit instructions
- Stay on branch `feature/dtype-contract`.
- One focused commit. Message summarizing the two silent-cast fixes, ending with
  the trailer:
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Do NOT push and do NOT open a PR; leave the branch local for review.
