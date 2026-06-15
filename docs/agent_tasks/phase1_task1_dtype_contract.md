File: imswitch/imcontrol/model/managers/detectors/DetectorManager.py
File: imswitch/imcontrol/model/managers/detectors/APDManager.py
File: imswitch/imcontrol/model/managers/detectors/PMTManager.py
File: imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py
File: imswitch/imcontrol/model/managers/RecordingManager.py

Task summary
Introduce a declared data-type ("dtype") contract on detectors so the recording
path has a single authoritative source of truth for each detector's data type,
and make the storers create datasets from that declared dtype instead of
inferring it from the first frame. On a dtype mismatch the storer must WARN
LOUDLY and keep the declared dtype (cast is allowed but must be logged — never
silent). This is Phase 1, Task 1 of docs/recording_dataflow_plan.md. Do not
touch the poll loop, time.sleep, RAM mode, or the multi-consumer broker — those
are later phases.

Todo list
- Add a `dtype` property and a `bitDepth` property to the DetectorManager base
  class.
- Make the base `dtype` learn from the latest frame at runtime, defaulting to
  uint16 before any frame exists.
- Override `dtype` explicitly on the scanned detectors (APD, PMT, Swabian).
- Make HDF5Storer and ZarrStorer create their datasets from the detector's
  declared dtype, and warn-once-per-detector on any frame/declared mismatch
  (keep declared dtype; logged cast, not silent).
- Make TiffStorer warn-once-per-detector on mismatch and write in the declared
  dtype.
- Add/extend unit tests covering the contract and the warning.

Do NOTs
- Do NOT remove or change the `time.sleep` in the recording loop.
- Do NOT change RAM mode / BytesIO behavior.
- Do NOT introduce a ChunkBroker, subscriber queues, or any producer/consumer
  rewrite.
- Do NOT change the `np.array(newFrames)` stacking in RecordingWorker
  (that is Phase 1 Task 2).
- Do NOT silently cast: every dtype change on the recorded path must be logged.
- Do NOT change display-only conversions or image-viewer code.
- Do NOT alter detector shape conventions.

Implementation
1. DetectorManager (base, DetectorManager.py):
   - Add property `dtype` returning a `numpy.dtype`:
       - return `np.dtype(self.image.dtype)` when a latest frame exists
         (`self.image is not None and getattr(self.image, "size", 0) > 0`),
       - otherwise return `np.dtype(np.uint16)` (sensible camera default before
         the first frame).
     This gives cameras a correct runtime dtype without SDK introspection
     ("the camera tells us at runtime"); scanned detectors override below.
   - Add property `bitDepth` returning `int(np.dtype(self.dtype).itemsize * 8)`.
   - Add concise docstrings explaining this is the authoritative recorded dtype.
2. APDManager.py:
   - Override `dtype` to return `np.dtype(np.float32) if self._ttlmultiplying
     else np.dtype(np.uint16)` (mirrors initiateImage's buffer choice).
3. PMTManager.py:
   - Override `dtype` to return `np.dtype(np.float32)` (analog voltage buffer).
4. SwabianTimeTaggerManager.py:
   - Override `dtype` to return `np.dtype(np.float32)` (intensity/lifetime
     buffers are float32).
5. RecordingManager.py — HDF5Storer and ZarrStorer:
   - In the lazy dataset-creation path of `writeFrames`, read
     `declared = self.detectorManager[detectorName].dtype`.
   - Create the dataset with `declared` instead of `frames.dtype`.
   - Track a per-storer set of detector names already warned
     (e.g. `self._dtypeWarned`). On the FIRST chunk whose `frames.dtype !=
     declared`, emit a prominent `logger.warning` naming the detector, the
     declared dtype, and the actual frame dtype; add the detector to the set so
     it warns at most once per recording. Then proceed (the assignment into the
     declared-dtype dataset performs the cast — now logged, not silent).
   - Keep snapshot (`snap`) behavior creating from the image's own dtype
     unchanged for this task (snapshots are not the streaming contract).
6. RecordingManager.py — TiffStorer:
   - In `writeFrames`, read the declared dtype the same way. If
     `frames.dtype != declared`, warn-once-per-detector (same pattern) and
     `frames = frames.astype(declared)` before `tiff.imwrite`, so the on-disk
     dtype matches the declared contract.
7. Initialize any new per-storer warning-tracking attribute in each storer's
   `openStream` (and guard `snap`-only usage so it never raises).

Sanity checks
- Run: `python -m pytest imswitch/imcontrol/_test/unit/ -k "record or chunk or
  detector" -q` and ensure no regressions.
- Add a unit test: a mock/declared detector records a stream to HDF5 and to
  Zarr; assert the on-disk dataset dtype equals the detector's declared dtype.
- Add a unit test using `caplog` (or the project's logging-capture pattern):
  feed frames whose dtype differs from the declared dtype and assert exactly one
  warning is logged per detector AND the recording still completes with the
  declared dtype on disk.
- Confirm `bitDepth` returns 16 for a uint16 detector and 32 for a float32
  detector.
- Grep the recorded path to confirm no remaining `frames.dtype`-based dataset
  creation in the streaming storers.

Commit instructions
- Work on a new branch off main, e.g. `feature/dtype-contract`.
- One focused commit. Message summarizing the dtype contract + loud-mismatch
  policy, ending with the trailer:
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
- Do NOT push and do NOT open a PR; leave the branch local for review.
