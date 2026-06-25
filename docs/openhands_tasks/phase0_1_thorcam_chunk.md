# Task: ThorCam TSI recording chunk fix (Phases 0 + 1)

You are working in the ImSwitch2 repository on branch `feat/live-reconstruction`.
This task combines two phases from
`docs/advanced_scan_triggered_recording_audit.md`:

- **Phase 0** — add regression tests that pin the correct contract (and a
  trigger-aware mock so they can be written deterministically).
- **Phase 1** — fix `ThorCamTSIManager` so those tests pass.

Do BOTH so the tree ends green. Keep the change tightly scoped to the ThorCam
TSI detector path and its tests. Do not refactor unrelated managers or the
recording loop.

## Background — the bug (read carefully)

`ThorCamTSIManager.getChunk()` returns a single 2-D `(H, W)` frame
(`imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py`, `getChunk`
delegates to `getLatestFrame`). The documented contract for `getChunk()` is a
3-D `(numFrames, height, width)` array
(see `DetectorManager.getChunk` docstring in
`imswitch/imcontrol/model/managers/detectors/DetectorManager.py`).

The recording path consumes frames via `DetectorManager.readChunk()`, which does
`consumerQueue.extend(self.getChunk())`. `list.extend` over a 2-D ndarray
iterates the FIRST axis, so one `(H, W)` image is appended as `H` separate 1-D
rows of shape `(W,)`. Downstream (`RecordingManager._getNewFrames` ->
`np.stack`) this both corrupts the saved data and makes the frame counter
advance by `H` per poll. This corrupts EVERY ThorCam TSI recording, in every
mode, with software OR hardware trigger.

Second defect: in hardware-trigger mode `getLatestFrame()` polls ~10 ms and then
returns a fabricated zero frame when no frame is pending (the
`"No frame available, returning zeros"` path). Because `getChunk()` delegates to
`getLatestFrame()`, a recording waiting on hardware triggers receives fabricated
black frames instead of an empty chunk. Fabricating zeros is acceptable for
live-VIEW display, but NOT for a destructive recording read.

Every other camera manager returns 3-D `(n, H, W)` or a list of frames and an
empty result when nothing is pending. `ThorCamTSIManager` is the only bare-2-D
offender.

## Driver contract you can rely on

Both the real `ThorTSICamera` and `MockThorTSICamera` (in
`imswitch/imcontrol/model/interfaces/thorcamera_tsi.py`) expose:

- `arm(buffer_size)`, `disarm()`, `is_armed`-style state
  (mock uses `self._armed`; the real wrapper exposes `self._camera.is_armed`),
- `issue_software_trigger()`,
- `get_pending_frame()` -> returns an `(H, W)` ndarray, or `None` when no frame
  is pending,
- `image_height_pixels`, `image_width_pixels`.

Trigger mode is selectable via the `'Operation Mode'` detector parameter
(`'Software'`, `'Hardware'`, `'Bulb'`), applied to the camera through
`set_trigger_mode`.

## Phase 0 — trigger-aware mock + failing contract tests

### 0a. Make the mock model triggering

Edit `MockThorTSICamera` in
`imswitch/imcontrol/model/interfaces/thorcamera_tsi.py` so frames are gated by
triggers instead of being produced unconditionally:

- Track the trigger mode (it already stores `self._trigger_mode`:
  `0=software, 1=hardware, 2=bulb`).
- `issue_software_trigger()` should record a pending software trigger
  (e.g. increment a counter).
- Add a test helper `simulate_hardware_trigger(self, n: int = 1)` that queues
  `n` hardware frames.
- `get_pending_frame()`:
  - return `None` if not armed;
  - in software mode: return a synthetic frame only if a pending software
    trigger is available (consume one), else `None`;
  - in hardware/bulb mode: return a synthetic frame only if a queued hardware
    trigger is available (consume one), else `None`.

This MUST preserve existing software-mode behavior end-to-end: the manager's
`getLatestFrame()` issues a software trigger before polling, so live view and
existing default-mode usage keep working. Do not break any currently passing
test. (There are no existing ThorCam unit tests; the mock is used via setups
that default to Software mode.)

### 0b. Add a new contract test module

Create `imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py`.
Construct a real `ThorCamTSIManager` backed by the mock by passing a
`DetectorInfo` with `managerProperties={'cameraSerial': 'MOCK_TSI'}`. Follow the
construction pattern in
`imswitch/imcontrol/_test/unit/test_tis_manager_setparameter.py` and
`test_hamamatsu_crop.py` (build a `DetectorInfo`, instantiate the manager; the
`MOCK_` serial auto-loads `MockThorTSICamera`). Access the mock via
`manager._camera` to drive triggers.

Write tests asserting the TARGET contract (these MUST fail before Phase 1):

1. **Hardware mode, no trigger → empty 3-D chunk.** Set Operation Mode to
   `'Hardware'`. `chunk = manager.getChunk()` must satisfy `chunk.ndim == 3` and
   `len(chunk) == 0`. It must NOT be a 2-D frame and must NOT be fabricated
   zeros.
2. **Hardware mode, N triggers → N real frames.** After
   `manager._camera.simulate_hardware_trigger(3)`, `getChunk()` returns a chunk
   with `shape[0] == 3` and `ndim == 3`; a subsequent `getChunk()` with no new
   triggers returns an empty 3-D chunk.
3. **Software mode → 3-D single-frame chunks.** With Operation Mode `'Software'`,
   `getChunk()` returns `ndim == 3` with `shape[0] == 1` (a real frame), never a
   2-D array.
4. **`readChunk` distributes whole frames, not rows.** Register a consumer and
   feed frames (hardware mode + `simulate_hardware_trigger`, or software mode);
   assert every frame returned by
   `manager.readChunk('test')` has `shape == (H, W)` — i.e. rows are NOT being
   mis-split into `(W,)` vectors.
5. **No fabricated frames reach recording (hardware idle).** Hardware mode, no
   triggers: `manager.readChunk('rec')` returns an empty list across several
   polls (no black frames).

Use small ROI dimensions if helpful (set ROI parameters) to keep the synthetic
frames tiny and fast.

Run the tests and CONFIRM they fail against the current `ThorCamTSIManager`
(this proves they pin the real bug). Record the failure in your notes.

## Phase 1 — fix `ThorCamTSIManager.getChunk()`

Edit `getChunk()` in
`imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py` so it returns
real frames as a 3-D `(n, H, W)` array, or an empty 3-D chunk when none are
pending — never a 2-D frame and never fabricated zeros:

- **Software/Bulb modes that need a trigger to produce a frame:** issue ONE
  software trigger (software mode), then poll `get_pending_frame()` briefly for
  the resulting frame. Return it stacked as `(1, H, W)` if it arrives, else an
  empty chunk. Do NOT fall back to a zero frame.
- **Hardware mode:** do NOT issue any trigger and do NOT fabricate. Drain all
  currently-pending real frames by looping `get_pending_frame()` until it
  returns `None`, stack them as `(n, H, W)`, and return. Return an empty chunk
  if none are pending.
- **Empty chunk shape:** prefer `np.empty((0, H, W), dtype=<detector dtype>)`
  using the current ROI height/width (`image_height_pixels` /
  `image_width_pixels`) and the detector's `dtype`. `(0, 0, 0)` is an acceptable
  fallback only if the ROI shape is genuinely unknown. The hard contract is
  `len(chunk) == 0` for "no frame" and `ndim == 3` for real frames.
- Preserve the native dtype of frames (uint16); do not upcast.

Leave `getLatestFrame()` UNCHANGED — it remains the live-view display path and
may keep its software-trigger + zero-fill fallback behavior. The split is
deliberate: `getLatestFrame` = display (may fabricate), `getChunk` = recording
(never fabricates).

Add a short code comment on `getChunk()` explaining the contract and why it must
not delegate to `getLatestFrame()` (cross-reference
`DetectorManager.readChunk`).

After the fix, the Phase 0 tests must pass.

## Acceptance criteria

1. New module `test_thorcam_tsi_recording_contract.py` exists, expressing all
   five contract assertions above, and PASSES after the fix.
2. `ThorCamTSIManager.getChunk()` never returns a 2-D array and never returns
   fabricated zero frames; empty result is a 3-D zero-length chunk.
3. `getLatestFrame()` is unchanged.
4. The full baseline suite is green:

   ```
   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit/test_recording.py \
     imswitch/imcontrol/_test/unit/test_detector_chunk_consumers.py \
     imswitch/imcontrol/_test/unit/test_scan_once_recording_sources.py \
     imswitch/imcontrol/_test/unit/test_thorcam_tsi_recording_contract.py -q
   ```

   (Use the `python` on PATH — conda 3.12 — which has imswitch + pytest. Do NOT
   add `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` without `-p pytestqt.plugin`, or the
   `qtbot` tests will error.)

5. No unrelated files changed. Do not commit; leave changes in the working tree
   for review.

## Notes / guardrails

- Sequential, single-checkout workflow: do not spawn parallel work; make the
  edits directly in this checkout.
- If you discover the software-mode trigger handling is more subtle than
  described (e.g. the manager already issues a trigger elsewhere on the
  recording path), prefer the minimal change that satisfies the five tests and
  note the reasoning in a code comment — do not broaden scope.
- When done, print a concise summary: files changed, the pre-fix test failures
  you observed, and the final test result line.
