# Live Reconstruction Rework — Session Checkpoint

**Branch:** `live-recon-dev`   **Date:** 2026-08-31
**Purpose of this file:** (1) a manual review record of everything implemented in
this session; (2) a context payload — re-upload this to the assistant to resume
from here.

---

## 1. Goal

Port the ImSwitch‑1 live *directory / file watching* reconstruction scheme
(originally in `ImSwitch/imswitch/imreconstruct/controller/karl_workers/`, branch
`liveRec_merge_imonalisa`) into ImSwitch‑2's `improcess/live/` architecture,
**keeping ImSwitch‑1's speed and GUI responsiveness** (which the old shared‑memory
+ per‑stack‑refresh design achieved well), but shaped to ImSwitch‑2 conventions
(passive pull‑based objects, generic workers, thin policy controllers, tested
units).

The concrete end‑to‑end flow being supported:

```
root folder                                   <- user picks this in the watcher pane
 └─ timelapse_00/  (a scan-lapse recording)    <- DirectoryWatcher emits this
     ├─ <name>_rec_scan00_CAM.zarr             <- one .zarr per timepoint = one raw stack
     ├─ <name>_rec_scan01_CAM.zarr                (each store: 3D array (frames, Y, X),
     └─ ...                                        chunked ~1 frame; ScanStage:* metadata)
```

---

## 2. Final architecture

```
DirectoryWatcher  (QTimer, GUI thread; os.scandir only)
   │  sigTimelapseFound(folder)
   ▼
JobQueue          (pure/sync; os.listdir only; no store opening)
   │  next_ready() -> DiscoveredJob(seed_path, folder)
   ▼
LiveModeController         (policy: watch -> queue -> one job at a time)
   │  LiveReconstructionController.start(reconstructor, source, params, source_arg=seed)
   ▼
LiveReconstructionController   (per-job lifecycle; owns StackRing + threads)
   ├── LiveStreamWorker  (own QThread)                LiveProcessWorker  (own QThread)
   │     _run_startup: open()+retry, collect              begin_session()  <- session.begin() OFF the GUI thread
   │       first stack -> sigInitStackReady               process_chunk(idx):
   │     _poll_loop -> _dispatch_chunks:                    frames = ring.read(idx)
   │       for each frame g in a polled Chunk:              session.push(frames, g, g+1)
   │         (barrier at stack edge:                        every >=0.75s OR at stack end:
   │          _await_stack_consumed())                        sigResultUpdated(session.result())
   │         ring.write(g, frame)                           at stack end:
   │         sigFramesReady(g) ───────────────────────►       sigTimepointDone(g // frames_per_stack)
   │                                                         stack_consumed.set()   (releases the barrier)
   │
   └── StackRing  (shared, stateless, one stack of single-frame slots; slot = g % num_frames)
                                                        │
     CommunicationChannel.sigLiveResultUpdated(result) ─┤
     CommunicationChannel.sigLiveTimepointDone(int)  ───┘
                                                        ▼
     ReconstructionViewController
       liveResultUpdated  -> coalesce (keep newest) -> QTimer.singleShot(0, _renderPendingLiveResult)
       _renderPendingLiveResult:
         first update for this result  -> fullUpdate()  (establishes napari layers)
         subsequent                    -> widget.fastLiveUpdate()  (in-place layer.data swap; NO rebuild)
         then _advanceLiveTimeSlider() -> widget.moveDimStep(T-axis, latest timepoint)
```

**Unit responsibilities (who does what):**

| Level | Unit | Watches for | Notes |
|---|---|---|---|
| `root/` | `DirectoryWatcher` | new immediate sub-directories | `QTimer` 1 s, GUI thread, `os.scandir` only. NOT its own thread. |
| `root/timelapse/` | `JobQueue` | the first `.zarr` store appearing in a pending folder | pure/sync, `os.listdir` only. No store opening, no readiness check, no lapse metadata parsing. |
| `timelapse/*.zarr` | `LiveSource.poll()` (`ZarrMultiFileLapseSource`) | the array growing + the next timepoint file | already existed; unchanged apart from the `_meta_lookup` fix below. |
| chunk → ring | `LiveStreamWorker` | — | writes each frame into `StackRing`, emits its global index |
| ring → recon | `LiveProcessWorker` | — | `session.begin()` + `session.push()` + result cadence, all off the GUI thread |

**Store readiness** ("is this store safe to open") is **NOT** decided by the
controller anymore. `LiveStreamWorker._run_startup` opens the source with bounded
retry (`open_max_attempts`), so a store that exists but is still being written is
*waited on*, not gated. ~500 lines of readiness/lapse-grouping code were deleted
from `LiveModeController`.

---

## 3. New files

### `imswitch/improcess/live/buffer.py` (~115 lines) — `StackRing`

Stack‑sized frame store shared between the stream and process workers. **Pure,
fixed, stateless**: a preallocated `(num_frames, *frame_shape)` numpy buffer and
nothing else — no threads, no locks, no queues, no epoch/tag.

- `num_frames == frames_per_stack`; slot `i` holds whichever frame has **ring
  index** `i (mod num_frames)`. The ring index is the *global* frame index
  (`chunk.start + offset`); the ring does the `% num_frames` internally.
- `write(index, frame)` — copies one frame into its slot.
- `read(index) -> np.ndarray` — a `(1, *frame_shape)` **view** (aliases the
  buffer; the consumer must consume it before the slot is next written).
- properties: `num_frames`, `frame_shape`, `dtype`, `nbytes`.
- Thread-safety without locks: within one stack each slot is written once
  (stream) and read once (process), with the Qt queued `sigFramesReady` signal
  as the happens‑before edge. The stream worker does not reuse the buffer for
  the next stack until the process worker has drained the current one — that
  barrier is a `threading.Event` in `LiveStreamWorker` ("option (b)").

Tests: `imswitch/improcess/_test/test_stack_ring.py` (9, no threads/Qt).

### `imswitch/improcess/live/discovery.py` (~245 lines) — `DirectoryWatcher` + `JobQueue`

**`DirectoryWatcher(QtCore.QObject)`**
- `sigTimelapseFound(str)` — absolute path of a newly-seen immediate sub-dir.
- Internal `QTimer` (default 1000 ms). `start()` = one scan now + start timer;
  does **not** clear the seen-set. `stop()` = stop timer, keep seen-set.
  `reset()` = clear seen-set (for a future hard-reset button).
- `scan_once() -> list[str]` — the testable core (`os.scandir`, exclude
  `_EXCLUDED_DIRS = {"rec","deskew","Mini_Recon_Results","__pycache__"}`,
  dirs only, sorted, `_meta_lookup`-free). `_tick()` calls it and emits.
- One level deep only.

**`JobQueue`** (plain object, no Qt)
- `add(folder)` — pending, idempotent.
- `next_ready() -> DiscoveredJob | None` — first pending folder that now
  contains a `.zarr`; removed from the queue and returned with
  `seed_path` = the timepoint‑0 store; folders with no store (or unreadable)
  stay pending.
- `reset()` — **NOT implemented** (raises `NotImplementedError`); deferred to
  the hard-reset button.
- `_seed_key(name)` — sort key = the substring from `"scan"` onward. Insight:
  `scan0` / `scan_00` / `scan__00__` is always the lexical minimum of a
  `scanN` family whether or not the index is zero‑padded, so `sorted(...)[0]`
  reliably picks timepoint 0. **Requires the literal substring `"scan"`** in
  every store name (raises `ValueError` otherwise). The full sorted order is
  *not* numerically correct for unpadded names — only `[0]` is used.

`DiscoveredJob` = frozen dataclass `(seed_path: str, folder: str)`.

Tests: `test_directory_watcher.py` (11), `test_job_queue.py` (11).

---

## 4. Modified files

### `imswitch/improcess/live/workers.py` (~497 lines)

**`LiveStreamWorker`**
- new signal `sigFramesReady(int)` (ring index). `sigChunkReady(object)` kept
  for the batch-fallback path.
- `__init__` gains `self._stack_consumed = threading.Event()` (worker-owned,
  like `_resume_event`) and `self._ring = None`.
- `resume(ring=None)` — store `ring`, then `_resume_event.set()` (the set is
  the happens-before edge). `ring is None` ⇒ chunk mode; `ring` set ⇒ ring mode.
- properties `frames_per_stack` (set by `_run_startup`) and `stack_consumed`.
- `_dispatch_chunks(chunks) -> bool` — called from both `_poll_loop` chunk
  sites. Chunk mode: emit each `Chunk` on `sigChunkReady`. Ring mode: unroll to
  per-frame `ring.write(ring_idx, chunk.data[offset])` + `sigFramesReady.emit`,
  with `_await_stack_consumed()` at each `ring_idx > 0 and ring_idx %
  frames_per_stack == 0` boundary. Returns `False` -> `_poll_loop` breaks.
  Asserts `frames_per_stack is not None`; per-chunk `_interrupted()` check.
- `_await_stack_consumed() -> bool` — polls `_stack_consumed` in
  `poll_interval_ms` slices, `_interrupted()` -> `False`, bounded by
  `stall_timeout_s` (on timeout: `sigStalled` + `sigStackComplete` + `False`),
  `None` timeout = wait forever. Clears the event; `return not _interrupted()`.
- `stop()` also `_stack_consumed.set()`.

**`LiveProcessWorker`**
- `__init__(session, ring, stack_consumed, frames_per_stack,
  min_result_interval_s=0.75)`. `session` is **not yet begun**.
- signals: `sigResultUpdated`, `sigStackFinished`, `sigSessionBegun(object)`,
  `sigTimepointDone(int)`, `sigFailed`.
- `set_init(init_obj, params)` — plain method, call before `moveToThread`.
- `begin_session()` — `@Slot()`, wired to `process_thread.started`, so
  `session.begin()` (MoNaLISA localizes the *whole first stack* — seconds of
  work) runs **on the process thread, never the GUI thread**. Emits
  `sigSessionBegun(plan)` / `sigFailed`.
- `process_chunk(index)` — `@Slot(int)`. Steps: bail on interruption ->
  `ring.read` -> `session.push(frames, index, index+1)` -> emit
  `session.result()` **at a stack boundary OR once `min_result_interval_s` of
  wall-clock has elapsed** (not per-N-frames) -> **outside the try**: if last
  frame of a stack, `sigTimepointDone.emit(index // frames_per_stack)` then
  `stack_consumed.set()`.
- No `stop()` — slot-driven; stopped by `thread.quit()` + the interruption
  guard.

### `imswitch/improcess/controller/LiveReconstructionController.py` (~410 lines)

Streaming path is now **three-phase** (was: build everything up front):

1. `_start_streaming_path` — make session; make **stream** thread+worker, wire
   its signals, start. No process worker yet.
2. `_on_opened(stack_info)` — `frames_per_stack = stream_worker.frames_per_stack`;
   **allocate `StackRing(frames_per_stack, frame_shape, dtype)`**. Bad
   `frame_shape` -> `_finish_without_result()`.
3. `_on_init_stack_ready(init_data)` — build `StreamInit`; build the **process**
   worker (`set_init(...)`, `moveToThread`, wire `sigResultUpdated /
   sigStackFinished / sigSessionBegun / sigTimepointDone / sigFailed`,
   `process_thread.started -> begin_session`), start the process thread.
   Does **not** call `session.begin()` itself.
4. `_on_session_begun(plan)` — wire `sigFramesReady -> process_chunk`,
   `sigStackComplete -> finalize`, `stack_consumed.set()` (pre-arm the first
   barrier — stack 0 went through `begin()`), `resume(ring)`.
5. `_on_timepoint_done(t)` -> `commChannel.sigLiveTimepointDone.emit(t)`.

`__init__` / `_reset_workers` gain `_ring`, `_frames_per_stack` (nulled on
teardown). Batch-fallback path unchanged.

### `imswitch/improcess/controller/LiveModeController.py` (688 -> 200 lines)

Rewritten. **Deleted** ~500 lines: `_scanForStores`, `_discoverStores`, all
suffix helpers, `_lapse_key`, `_is_multifile_lapse_from_metadata`, and the entire
readiness family (`_is_store_ready`, `_is_store_complete`,
`_has_streaming_barrier`, `_check_zarr_complete`, `_check_hdf5_complete`,
`_scan_groups_complete`, `_is_single_file_complete`, ...), `_processNextStore`,
`_onStoreFinished`, plus `zarr` / `h5py` / `re` / `deque` imports.

**Kept unchanged:** `_onLiveToggled`, `_getActiveReconstructor`,
`_getReconstructorParams`.

New flow:
- `_startLive()` — validate folder + reconstructor; new root -> fresh
  `JobQueue` + fresh `DirectoryWatcher` (same root -> reuse, so a live off/on
  toggle doesn't reprocess); create `LiveReconstructionController` if needed,
  wire `sigFinished -> _onJobFinished`; start a 1 s `_tickTimer ->
  _processNextJob`; `DirectoryWatcher.start()`.
- `_onTimelapseFound(folder)` -> `JobQueue.add(folder)` -> `_processNextJob()`.
- `_processNextJob()` — one run at a time (`_currentlyProcessing`); pull
  `JobQueue.next_ready()`; build `ZarrMultiFileLapseSource(job.seed_path)`;
  `LiveReconstructionController.start(...)`. `start()` False -> free + retry.
- `_onJobFinished()` -> free the slot + `_processNextJob()`.
- **`.zarr` only** (per scoping decision).

### `imswitch/improcess/controller/ReconstructionViewController.py`

- **Coalescing** — `liveResultUpdated` stores the newest result and schedules
  **one** `QTimer.singleShot(0, _renderPendingLiveResult)`. Newer results
  during a redraw just replace the stored one. The GUI can never fall behind.
- `_renderPendingLiveResult` — first update for a live result -> one
  `fullUpdate()` (establishes napari layers), `_liveEstablished = True`;
  subsequent -> `_widget.fastLiveUpdate(result, self._transposeOrder)`, falling
  back to `fullUpdate()` if it returns `False` (structure changed).
- `_onLiveTimepointDone(t)` — stores `_liveLatestTimepoint`.
- `_advanceLiveTimeSlider()` — finds `"T"` / `"Timepoints"` / `"Time"` in
  `_displayedAxisLabels` and calls `_widget.moveDimStep(axis, t)`. Runs inside
  the coalesced render so slider + pixels move together.

### `imswitch/improcess/view/ReconstructionView.py`

- `fastLiveUpdate(result, transposeOrder) -> bool` — ImSwitch‑1 style in-place
  update: **no** layer add/remove, **no** `display_layers()` rebuild, **no**
  axis-label/colormap/scale-bar/contrast reconfiguration. Uses
  `result.display_layer_data()` (raw per-layer view arrays, no percentile) ->
  shape check -> `layer.data = arr` + `layer.refresh()`. Returns `False` on any
  structure/shape mismatch so the caller falls back.
- `moveDimStep(axis, step)` — `napariViewer.dims.set_current_step(axis,
  clamp(step))`.
- `_setScaleBarUnit(unit)` — sets the scale-bar unit **only when it changed**
  (was re-set on every redraw) and suppresses the napari `FutureWarning`.
- Tracks `_imgLayerIsDisplayAnchor` (set in `setImage` / `setDisplayLayers`).
- **Removed** the `_requestCanvasRepaint()` helper that poked the deprecated
  `window.qt_viewer.canvas` (that caused a repeating `FutureWarning`).

### `imswitch/improcess/model/result.py`

- `ProcessingResult.display_layer_data() -> list[np.ndarray]` — default `[]`.
  Fast-path counterpart to `display_layers()`: pixel arrays only, no per-call
  contrast recompute.

### `imswitch/improcess/reconstructors/monalisa/result.py`

- `MonalisaProcessingResult.display_layer_data()` — per-base data **slices
  (views)** in the same order as `display_layers()`. The heavy
  `np.percentile` over the whole growing volume (in `display_layers()`) now
  runs only on the first full render, not every streaming update. **This was
  the main GUI-slowdown culprit.**

### `imswitch/improcess/controller/CommunicationChannel.py`

- new `sigLiveTimepointDone = Signal(int)`.

### `imswitch/improcess/live/sources.py`  (bug fix — see §6)

- `ZarrLiveSource._refresh_state_from_attrs` (+ the `Hdf5*` / `*Lapse` siblings)
  now use `_meta_lookup(attrs, key)` instead of `attrs.get(key)` for `writing`,
  `recording:expected_frames`, `recording:frames_committed`.

### `imswitch/imcommon/view/guitools/naparitools.py`  (bug fix — see §6)

- `addNapariGrayclipColormap` — `'grayclip' in AVAILABLE_COLORMAPS` (was
  `hasattr`), and `KeyError` added to the `except`.

---

## 5. GUI-responsiveness fixes (the last chunk of work)

The pipeline worked but the GUI first *froze*, then was *very slow*, then was
*slow during user interaction*. Root causes, in the order found and fixed:

1. **`session.begin()` on the GUI thread** — MoNaLISA localizes the whole first
   stack there. Fix: moved to `LiveProcessWorker.begin_session()` on the
   process thread (wired to `process_thread.started`). ImSwitch‑1 ran this on
   its `ZarrInitWorker` thread.
2. **Result updates every 5 frames, each a full `reconstructed.copy()` +
   `fullUpdate()`** — the GPU process loop outran the GUI's redraws and the
   event queue flooded. Fix: (a) wall-clock throttle `min_result_interval_s`
   (0.75 s) + always emit at a stack boundary; (b) GUI-side coalescing (keep
   newest, one scheduled render).
3. **Full napari layer rebuild per update** — `fullUpdate() ->
   setDisplayLayers()` does `_clearDisplayLayers()` (removes every managed
   napari layer) then recreates them. Fix: `fastLiveUpdate()` — in-place
   `layer.data` swap, no rebuild (ImSwitch‑1's `_handleFastUpdate` pattern).
4. **`MonalisaProcessingResult.display_layers()` recomputes `np.percentile`
   over the entire growing volume, per base, on the GUI thread, every 0.75 s**
   (~29M-element sorts). Fix: `display_layer_data()` returns views, no
   percentile; `display_layers()` (with the percentile) runs only on the first
   full render.
5. **Timepoint slider didn't follow the reconstruction.** Fix:
   `sigTimepointDone(int)` -> `sigLiveTimepointDone(int)` -> viewer moves the
   `"T"` dims slider inside the coalesced render (ImSwitch‑1's
   `sigMoveTimeSlider` pattern, axis found by label instead of hard-coded `2`).

Net per-update GUI cost now: N zero-copy view-slices + `layer.data =` +
`refresh()` + one `dims.set_current_step` — same order as ImSwitch‑1.

---

## 6. Bug fixes made along the way (pre-existing bugs, not from this work)

- **`naparitools.addNapariGrayclipColormap`** — `hasattr(dict, 'grayclip')` is
  always False; `imcontrol` registers the colormap first, then `improcess`
  loading second hit the unguarded path and `KeyError` propagated (only
  `AttributeError/TypeError` were caught), killing the ImProcess module when
  both run together. Fixed to `in` + catch `KeyError`.
- **`sources.py` legacy metadata nesting** — the user's real MoNaLISA
  recordings use the ImSwitch‑1 Zarr layout: all attrs nested under an
  `ImswitchData` attribute. `_refresh_state_from_attrs` read `writing` at the
  top level -> always `None` -> a *completed* store looked forever mid-write ->
  the "completed store -> `frames_per_stack = array.shape[0]`" fast path never
  fired -> it fell through to guessing from scan geometry
  (`ceil(0.69 / 0.03) = 23`, wrong; true value 24 -> 576 frames). Fixed with
  `_meta_lookup`. **NOT fixed:** `_derive_scan_frames_per_stack`'s
  `ceil(length/step)` is still wrong for a *still-writing* legacy store with no
  `recording:frames_per_stack` / `ScanTTL:Nx/Ny` (three different pixel-count
  conventions exist across ImSwitch vintages: `ceil`, `round`, endpoint-
  inclusive `+1`). Completed stores are unaffected (array length wins).

---

## 7. Test status

New test files (all pass):

| File | Count | Notes |
|---|---|---|
| `test_stack_ring.py` | 9 | pure, no threads/Qt |
| `test_directory_watcher.py` | 11 | needs a `QApplication` (module fixture), no event loop |
| `test_job_queue.py` | 11 | pure/sync |
| `test_live_workers.py` | 20 | ring-path `LiveProcessWorker` + `_dispatch_chunks` + `_await_stack_consumed`; 3 obsolete `Chunk`-based tests replaced |
| `test_live_mode_controller.py` | 5 | new `DirectoryWatcher + JobQueue` integration |

**Deleted:** `test_live_discovery.py` (~20 tests) and `test_live_completion_gate.py`
(~30 tests) — they tested `_discoverStores` / `_lapse_key` / `_storeQueue` /
`_processNextStore` / the readiness family, all removed from `LiveModeController`.
The "wait until readable" guarantee moved to `LiveStreamWorker._run_startup`
(covered by `test_live_workers.py::test_stream_worker_startup_*`).

**Modified:** `test_live_controller.py::test_controller_streaming_path` rewritten
to run `start()` end-to-end and assert the process worker is built lazily.

**Full `imswitch/improcess/_test/` suite:** `1019 passed, 12 failed, 17 errors`.
The 12 failures (`test_load_path_helper.py`, `test_result_pipeline_generality.py`)
and 17 errors (`qtbot` fixture not installed — `pytest-qt`) are **pre-existing on
the pristine baseline** and unrelated to this work.

Run tests with the conda env python (bare `python` on PATH has no pytest):
```
"$HOME/AppData/Local/miniconda3/envs/imswitch2/python.exe" -m pytest imswitch/improcess/_test/ -q
```

---

## 8. How to run the live pipeline manually

**Setup file (new):**
`imswitch/_data/user_defaults/imcontrol_setups/mock_scan_monalisa_live.json` —
merges `mock_scan_setup.json` (mock AVManager camera + MockPositionerManager +
`nidaq.simulation=true` + MoNaLISA scan widget) with a `processing` block
(`"reconstructors": ["monalisa", "view-only"]`). It auto-copies to
`~/Documents/ImSwitchConfig/imcontrol_setups/` on next launch.

**Activate:**
1. `~/Documents/ImSwitchConfig/config/modules.json` -> `{"enabled":
   ["imcontrol", "improcess"]}`
2. `~/Documents/ImSwitchConfig/config/imcontrol_options.json` ->
   `"setupFileName": "mock_scan_monalisa_live.json"`
3. `python -m imswitch`  (optionally `--debug`)

**Record -> watch loop:**
- ImControl Recording widget: output folder e.g. `C:\live_test\run1`,
  `includeDateInOutputFolder: false`, format **Zarr**, "single lapse file"
  **off**.
- ImProcess File-watcher pane: Browse to `C:\live_test` (the **parent** — the
  watcher wants `root/<timelapse>/<timepoint>.zarr`; the recorder writes the
  `.zarr` files flat into `run1/`). Active reconstructor = **MoNaLISA**. Tick
  **"Live (stream)"** (not "Watch and run" — that's the batch path).
- ImControl: run a MoNaLISA **Scan-lapse** (a few timepoints). Smoke-test with
  **Scan-once** first (single stack -> no ring/barrier exercised).

**Real test data used this session:**
`C:\Users\karl.hojlund\Documents\testalab\tl_data\temp\` — `c01/`, `c02/` each
a 20-timepoint MoNaLISA scan-lapse (`c01_rec_scan00_CAM.zarr` ...
`c01_rec_scan19_CAM.zarr`; array shape `(576, 540, 540)` int16;
`frames_per_stack = 576 = 24*24`; legacy `ImswitchData`-nested attrs).
`cupy 14.1.1` is installed; MoNaLISA CUDA DLLs are in `imswitch/_data/libs/`.

---

## 9. Deferred / open work

1. **"One toggle" UI consolidation.** The watcher pane has two independent,
   non-mutually-exclusive checkboxes: "Watch and run" (batch,
   `WatcherFrameController`) and "Live (stream)" (`LiveModeController`). Plan:
   one toggle, live-only, panel renamed "Directory watcher"; remove
   `WatcherFrameController` + `imcommon/.../FileWatcher.py`; alias the
   `fileWatcherPanel` config key. Decisions still needed: hard-remove batch or
   keep hidden; fold `MemoryLiveController` in or leave separate; rename
   `WatcherFrame` class / `sigLiveChanged` signal or just the label.
2. **`JobQueue.reset()`** — not implemented (raises). Wire to a future
   hard-reset button along with `DirectoryWatcher.reset()`.
3. **`.h5` timelapse support** in `JobQueue` (`.zarr`-only right now). Design
   already accommodates it — just widen `_STORE_SUFFIX` and add
   `Hdf5MultiFileLapseSource` selection by suffix in `_processNextJob`.
4. **`_derive_scan_frames_per_stack`** off-by-one for still-writing legacy
   recordings (see §6). Needs a decision on which convention / better metadata;
   doesn't affect completed data.
5. **`poll_into` zero-copy decode** — deferred. Currently `source.poll()`
   decodes a `Chunk` and `ring.write` copies it in (one copy; on par with
   ImSwitch‑1's store->temp->buffer). A `LiveSource.poll_into(ring)` using
   `zarr get_basic_selection(out=)` / `h5py read_direct` would remove the
   intermediate allocation. Adds a method to every source; only do it if
   profiling asks.
6. **`MonalisaLiveSession.result()` copies the whole `reconstructed` buffer**
   every ~0.75 s (process thread, ~11-50 ms). Fine for now; a no-copy live
   snapshot would help very large recons but risks the batch/finish path.
7. **`fastLiveUpdate` plain-image branch** (non-MoNaLISA, e.g. view-only)
   transposes `result.data` and swaps — untested against a real large
   view-only live stream.
8. **`imswitch/improcess/live/temp.py`** — 3-line user scratch file, **not part
   of this work**; delete it.

---

## 10. Environment / logistics

- Repo: `C:\Users\karl.hojlund\Documents\testalab\tl_github\imswitch_repos\Imswitch2`,
  branch `live-recon-dev` (base `b40deca6`).
- ImSwitch‑1 reference (read-only, for the `karl_workers` design):
  `...\imswitch_repos\ImSwitch\imswitch\imreconstruct\` (branch
  `liveRec_merge_imonalisa`).
- Test/run python: `~/AppData/Local/miniconda3/envs/imswitch2/python.exe`
  (Python 3.14.6, pytest 9.1.1, zarr 3.2.1). Bare `python` on PATH = PyManager
  3.14.5, **no pytest**.
- Nothing has been committed. `git status` shows: 10 modified, 2 deleted, 7
  untracked (5 real new files + the new setup JSON + `temp.py`).

---

## 11. Design decisions worth not relitigating

- **`StackRing` is stateless, option (b) barrier.** The consumer drains stack N
  fully before the producer writes N+1 (measured: reconstruction >> acquisition,
  so the barrier almost never actually blocks). No free-slot queue, no epoch —
  the ring index encodes everything; a stale read just returns the newer frame
  and correctness rests on the barrier.
- **Ring index == global frame index.** It flows source -> stream worker ->
  `sigFramesReady` -> process worker -> `session.push(f, idx, idx+1)` unchanged.
  Nothing "tracks" it; `chunk.start` (from the source's cursor) is authoritative.
- **`DirectoryWatcher` is a QTimer, not a thread.** The expensive part
  (opening stores for readiness) was moved out, so the scan is cheap `scandir`
  and doesn't need its own thread + teardown.
- **"Unit" (a class/file/tests with one responsibility) is separate from
  "thread".** ImSwitch‑1 fused them (one thread per pipeline stage) and that's
  why its teardown was fragile.
- **Controller = thin policy layer.** It decides and wires; it does not open
  files, reconstruct, or block. `session.begin()` moving off the GUI thread was
  a correction of a violation of this.
- **`JobQueue` never opens a store.** Readiness is the stream worker's
  bounded-retry-open problem.
