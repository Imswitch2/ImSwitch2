# Live Reconstruction — Port & Generalization Plan

**Status:** Design — audit complete, port plan proposed
**Date:** 2026-06-22
**Scope:** Port the live-reconstruction pipeline from upstream
[`ImSwitch@testalab_liveRec_dev`](https://github.com/ImSwitch/ImSwitch/tree/testalab_liveRec_dev)
into ImSwitch2's `improcess`, generalized across reconstructors and file
formats, plus an in-RAM "reconstruct what imcontrol is recording" mode.
**Relates to:** ROADMAP Milestone 10 (Recording Manager Upgrade — "Port the
improcess live-reconstruction pipeline") and Milestone 12 ("Hook into M10's
live pipeline — drive any registered reconstructor, not only MoNaLISA").

---

## 1. What exists upstream (audit of `testalab_liveRec_dev`)

The upstream live pipeline is a **Zarr folder-watcher → stream → process**
state machine, hard-wired to the MoNaLISA *fast Gauss* reconstructor. Files
under `imswitch/imreconstruct/`:

| Upstream file | Role | LOC |
|---|---|---|
| `controller/WatcherFrameController.py` | Main-thread state machine: directory watcher → file watcher → init → stream, per-directory queueing | ~210 |
| `controller/karl_workers/DirectoryWatcher.py` | Polls a *root* folder for new sub-directories (one scan = one dir), 200 ms | ~35 |
| `controller/karl_workers/ZarrInitWorker.py` | On first stack: reads `ImswitchData` attrs → scan geometry, runs `localizer` + `get_orientation`, builds a configured `GaussProcessor`, emits `StreamArgs` | ~165 |
| `controller/karl_workers/ZarrStreamWorker.py` | Polls a growing `.zarr` array, reads it in `chunks[0]`-sized blocks, emits `sigLiveChunkReady(start, end)` into a shared `raw_data` buffer | ~90 |
| `controller/karl_workers/ProcessorWorker.py` | `processChunk(start, end)`: runs `processor.process_chunk` on the slice, scatters result pixels into `recon_obj.reconstructed` via `frame_inds`, advances the time slider | ~65 |
| `model/karl_models/GaussProcessorCPU.py` | Vectorized per-pixel Gauss-weighted reassignment (the "fast" reconstructor) | ~146 |
| `model/karl_models/GaussProcessorGPU.py` | CuPy mirror of the above | ~149 |
| `model/karl_models/localizer.py` | Detects scan grid / sub-image geometry from the first stack | ~231 |
| `model/karl_models/geometry.py` | `get_orientation` — scan-direction detection | ~247 |
| `view/WatcherFrame.py` | Folder picker + "Start File Watcher" checkbox + file list | ~70 |

**Communication-channel contract (upstream, on the *single-module*
imreconstruct comm channel):**

- `sigSetupLiveStream(name, processor, raw_data, [rows, cols, tpoints])` —
  allocate a `ReconObj` sized for the reconstruction and bind the live buffer.
- `sigLiveChunkReady(start, end)` — a chunk of raw frames landed in `raw_data`.
- `sigProcessingFinished()` — one time-point reconstructed; advance state.
- `sigStopLiveStream()` — tear down.

**Characteristics / limitations to fix on the way in:**

- **MoNaLISA-only.** `ZarrInitWorker` hard-codes `localizer`, `get_orientation`,
  `GaussProcessor`, the `frame_inds` scatter, and a fixed `num_rects=4`. None of
  this generalizes to other reconstructors.
- **Zarr-only.** The stream worker depends on `.zarray` chunk shape (`chunks[0]`)
  to decide block size and on the directory file-count to detect completeness.
- **Geometry from `ScanStage:*` attrs.** Stack size is derived from
  `axis_startpos/length/step_size` — MoNaLISA scan-specific keys.
- **Prototype-grade.** `type: ignore` on every file, `print` debug, a typo bug
  (`QtThread.mslee`/`msleep`), bare `except: pass`, Windows file-lock retries by
  swallowing all errors, threads that "can't be stopped once stuck" (noted in a
  TODO). Per ROADMAP M10 these must be dropped/cleaned on port.
- **Stateful streaming contract.** `process_chunk(chunk)` + `frame_inds` +
  external `reconstructed` buffer is a *different* contract from ImSwitch2's pure
  `Reconstructor.process(data_obj, params) -> ProcessingResult`.

---

## 2. What already exists in ImSwitch2 (so we don't re-port it)

The M12 generalization already landed a lot of the backbone:

- **`improcess` plugin architecture.** `reconstructors/base.py::Reconstructor`
  (pure `process(data_obj, params) -> ProcessingResult`), a `registry.py`
  populated from the setup's `processing:` block, and plugins for
  MoNaLISA / view-only / SNOUTY / WFS.
- **A batch file-watcher already exists:**
  `improcess/controller/WatcherFrameController.py` watches a folder for whole
  files (hdf5/tiff/zarr), and on each new *complete* file emits
  `sigReconstruct(dataObjs, True)` through the registry, then saves the result
  under `{watched}/{default_save_subdir}/`. **This is whole-file, not
  chunk-streaming** — it waits for the file to finish, then reconstructs it once.
- **`DataObj` already reads all three formats** (HDF5, TIFF, Zarr) with the
  structured per-detector layout (M10 work) — so format generality on the
  *load* side is solved for batch. It is still a whole-dataset loader today:
  `data` materializes the full selected array, so streaming needs a separate
  in-memory/array-slice payload contract instead of pretending every chunk is a
  normal file-backed `DataObj`.
- **Structured Zarr streaming save** (M10, 2026-05-29): `ZarrStorer` writes
  `(T,Y,X)` per-detector arrays via `resize`/slice-assign — the exact growing
  array the stream worker wants to follow.
- **In-RAM cross-module bridge already wired:**
  `ModuleCommunicationChannel.memoryRecordings` (`VFileCollection`).
  `RecordingManager` emits `sigMemoryRecordingAvailable` /
  `sigMemorySnapAvailable` on `SaveMode.RAM`/`DiskAndRAM`;
  `improcess/controller/MultiDataFrameController.py` already *consumes*
  `memoryRecordings.sigDataSet` to surface RAM recordings as loadable datasets.
  **Boundary:** this is currently HDF5-oriented. `VFileCollection` can save
  `BytesIO`/`h5py.File`, `MultiDataFrameController` opens non-`h5py.File`
  payloads with `h5py.File(...)`, Zarr RAM streaming is explicitly unsupported,
  and TIFF recording has no RAM finalize path. The RAM hand-off path exists
  today for completed HDF5 recordings, not frame-by-frame and not all formats.
- **imcontrol can display extra image layers:** `ImageController.addImage(name)`
  is not a public controller API today. The usable existing surface is
  `ImageWidget.addStaticLayer(name, image, scale)`, called from controller
  slots such as `ImageController.memorySnapAvailable`. A reconstructed result
  can be shown in the live viewer, but it needs a dedicated cross-module signal
  and imcontrol slot rather than overloading detector `sigUpdateImage`.
- **The fast Gauss reconstructor is NOT present in ImSwitch2.** `karl_models`
  (GaussProcessorCPU/GPU, localizer, geometry) were never ported; the ImSwitch2
  MoNaLISA plugin is the slower `SignalExtractor` path. Porting the live
  pipeline therefore *also* means porting the fast reconstructor as a plugin.

---

## 3. Design: generalize to a streaming reconstructor contract

The core mismatch is **batch pure-function** vs. **stateful chunk-streaming**.
We bridge it with an *optional* streaming capability on the plugin contract, so
existing batch plugins keep working untouched and new ones can opt in.

### 3.1 New optional contract — `StreamingReconstructor`

Add streaming types without changing the existing `Reconstructor.process(...)`
contract. The exact module can be `reconstructors/base.py` for discoverability
or `improcess/live/contracts.py` if the source-side contracts grow larger:

```python
@dataclass(frozen=True)
class StreamPlan:
    out_shape: tuple[int, ...]
    axis_labels: tuple[str, ...]
    view_modes: tuple[ViewMode, ...]
    dtype: np.dtype
    scale_unit: str = "px"
    axis_scales: tuple[float, ...] | None = None

@dataclass(frozen=True)
class StreamInit:
    """First frames plus metadata; avoids forcing chunks through file-backed DataObj."""
    name: str
    dataset_name: str
    data: np.ndarray
    attrs: dict
    source_path: str | None = None
    stack_info: StackInfo | None = None

class StreamingSession(ABC):
    """One live reconstruction in progress. Created per stack/recording."""
    @abstractmethod
    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan: ...
        # init_obj wraps the FIRST chunk/stack (+ metadata). The session
        # inspects geometry, allocates its output buffer, and returns a
        # StreamPlan(out_shape, axis_labels, view_modes, dtype).
    @abstractmethod
    def push(self, chunk: np.ndarray, start: int, end: int) -> None: ...
        # Process raw frames [start:end] into the internal output buffer.
    @abstractmethod
    def result(self) -> ProcessingResult: ...
        # Snapshot the current output as a ProcessingResult for display/save.
    def finish(self) -> ProcessingResult: ...   # default: return result()
    def close(self) -> None: ...                 # free GPU buffers etc.

class StreamingReconstructor(Reconstructor):
    supports_streaming: bool = True
    @abstractmethod
    def make_session(self) -> StreamingSession: ...
```

- A reconstructor advertises live capability via `supports_streaming`.
- **Batch fallback for everyone else:** for a plugin that is *not* streaming,
  the live controller buffers a complete logical stack, wraps it as an
  in-memory data object compatible with `process(...)`, and calls the existing
  batch path once per completed stack/time-point. This is a P0/P2 requirement,
  not a later stretch: without it, requirement (a) "arbitrary reconstructors"
  is not satisfied. Streaming-capable plugins additionally get sub-stack
  updates.
- The MoNaLISA fast-Gauss session wraps `GaussProcessor` + `frame_inds`: `begin`
  runs `localizer`/`get_orientation` (today's `ZarrInitWorker` body), `push` is
  today's `ProcessorWorker.processChunk`, `result` snapshots the scatter buffer.

### 3.2 Format-agnostic source adapters (requirement b)

Replace the Zarr-specific `ZarrStreamWorker`/`ZarrInitWorker` file-poking with a
`LiveSource` interface that yields `(chunk, start, end)` and a final
`stack_complete` signal:

```python
class LiveSource(ABC):
    def open(self, path_or_handle) -> StackInfo: ...   # frames-per-stack, dtype, shape, metadata
    def poll(self) -> list[Chunk]: ...                 # new frames since last poll
    def is_complete(self) -> bool: ...
    def close(self): ...
```

Implementations:

- **`ZarrLiveSource`** — follows a growing `(T,Y,X)` array via `array.shape[0]`
  (works against the M10 structured `ZarrStorer` layout; no `.zarray`
  file-count hack). Chunk size from `array.chunks[0]`; completeness from
  generic recording metadata plus the existing `writing` attribute.
- **`Hdf5LiveSource`** — re-open in SWMR (`libver='latest'`, `swmr=True`) and
  watch the resizable dataset's growing length, but only after a concrete
  writer-side SWMR protocol lands: create the detector group/dataset early
  enough for SWMR readers, flush after metadata/dataset creation, set
  `file.swmr_mode = True`, flush after each appended batch, and call
  `dataset.refresh()` in the reader before checking `shape[0]`. Current
  lazy dataset creation is not enough.
- **`TiffLiveSource`** — deferred for v1 streaming. Current TIFF recording writes
  append-style detector files, can roll over at 4 GB, and has no RAM finalize
  path. Keep TIFF on the batch watcher until a concrete TIFF-streaming
  acquisition exists.

Stack/geometry metadata cannot be assumed from today's structured storer attrs:
they currently persist detector/layout metadata (`axes`, `writing`,
`detector_name`, `element_size_um`) but not a generic frames-per-stack or
timepoint contract. Add a small, modality-neutral recording metadata block in
HDF5/Zarr before implementing non-MoNaLISA live sources:

- `recording:frames_per_stack`
- `recording:timepoints` or `recording:expected_frames`
- `recording:detector_name`
- `recording:dataset_path`
- `recording:source_format`

The MoNaLISA *session* may still read `ScanStage:*` keys for its own geometry;
the *source* layer stays modality-agnostic and uses the generic recording block
for stack boundaries.

### 3.3 Refactored controller / worker topology

Keep the upstream main-thread state machine (it is sound) but make it generic:

- `DirectoryWatcher` (root → new scan dirs) — port nearly as-is, cleaned.
- `FileWatcher` — reuse the **existing** `imcommon` `FileWatcher` (already used
  by the batch watcher) instead of duplicating.
- `LiveStreamWorker` — generic: drives a `LiveSource`, emits `sigChunkReady`.
- `LiveProcessWorker` — generic: owns a `StreamingSession`, runs `push`,
  emits `sigResultUpdated` for display and `sigStackComplete`.
- A single `LiveReconstructionController` replaces the bespoke
  `WatcherFrameController` state machine, parameterized by the active
  reconstructor (from the registry) and the selected `LiveSource` (from the file
  extension / source mode).

All four worker classes drop `type: ignore`, `print`, the `mslee` typo, bare
`except: pass` (narrow to the known Windows `Errno 13` retry with a logged
warning + bounded retry count), and gain clean stop semantics
(interruption-flag + `QThread.requestInterruption`, no un-stoppable timers).

GPU stays behind an optional extra: `GaussProcessorGPU`/CuPy import guarded,
falls back to CPU with a single info log (as upstream already does).

---

## 4. The in-RAM "reconstruct from imcontrol" mode (requirement c)

Goal: skip the disk round-trip — reconstruct frames as imcontrol records them,
optionally display the result back inside imcontrol.

Two depths, smallest-first:

### 4.1 Now-feasible: RAM-recording hand-off (whole stack)
For v1, scope this explicitly to completed **HDF5** memory recordings. On
`SaveMode.RAM`/`DiskAndRAM`, `RecordingManager` emits
`sigMemoryRecordingAvailable` and improcess's `MultiDataFrameController` already
ingests HDF5-backed `VFile` datasets. Add: when a live-reconstruction toggle is
on, auto-route a newly-arrived HDF5 memory recording through the active
reconstructor's batch `process()` (or a streaming session in one shot) and
display the result. **No new acquisition-thread coupling.** This delivers
"reconstruct what was just recorded from RAM, no file on disk" with minimal
risk for HDF5. Zarr RAM requires a new memory-store/VFile policy; TIFF RAM
recording is out of scope until the recording layer supports it.

### 4.2 Stretch: true live frame streaming from the acquisition buffer
The ROADMAP M10 "Phase 1.5 `ChunkBroker` subscription API" is the right hook:
let consumers subscribe to the recording manager's bounded frame queue. A
`ChunkBrokerLiveSource` then feeds `LiveProcessWorker` directly from RAM as
frames are captured — no Zarr, no files. This depends on ChunkBroker landing
first (currently 🔄 in M10) and must respect the existing off-thread-writer
backpressure. Recommend: design the `LiveSource` interface now so
`ChunkBrokerLiveSource` drops in later; implement 4.1 first.

### 4.3 Display in imcontrol
Two options, pick per how tightly coupled we want the modules:

1. **In improcess (default):** result shows in the ImProcess `ReconstructionView`
   (existing path via `sigResultProduced`). Lowest coupling, works today.
2. **In imcontrol viewer (opt-in):** push the result as a named static/live
   reconstruction layer via a small cross-module signal on
   `ModuleCommunicationChannel` (`sigLiveReconResult(name, image, scale)`).
   imcontrol's `ImageController` subscribes and calls an explicit helper on
   `ImageWidget` (likely `addStaticLayer` or a new "update reconstruction layer"
   helper) instead of overloading detector `sigUpdateImage`.

Recommend shipping (1) first; add (2) behind a config flag once (1) is proven.

---

## 5. Phased plan

| Phase | Deliverable | Depends on | Risk |
|---|---|---|---|
| **P0** | This audit + contract spec; land `StreamingReconstructor`/`StreamingSession`/`StreamPlan`/`StreamInit`/`LiveSource` ABCs in `reconstructors/base.py` and/or a `live/` package skeleton; add in-memory stack wrapper + unit tests for batch fallback (no hardware). | — | low |
| **P1** | Port the **fast Gauss reconstructor** (`GaussProcessorCPU`, `localizer`, `geometry`) into `reconstructors/monalisa/` as a streaming session; rename `karl_*`→descriptive; GPU behind extra. Unit-test `process_chunk` scatter against a saved stack. | P0 | med (numerics) |
| **P2** | Generic batch-fallback controller path + `ZarrLiveSource` + generic `LiveStreamWorker`/`LiveProcessWorker` + `LiveReconstructionController`; wire to the existing `WatcherFrame` UI. Reproduce upstream Zarr live behavior end-to-end, cleaned, while non-streaming plugins run at stack granularity. | P0,P1 | med |
| **P3** | Recording metadata block in HDF5/Zarr storers + `Hdf5LiveSource` (SWMR). Enable and test the writer/reader SWMR protocol. Format-matrix tests for HDF5/Zarr. | P2 | med (SWMR/locking) |
| **P4** | Broaden live UI/selection tests across view-only, SNOUTY, and WFS; keep TIFF batch-only unless a concrete TIFF live acquisition is introduced. | P2,P3 | low |
| **P5** | In-RAM mode 4.1 for completed HDF5 RAM recordings (auto-route RAM recordings through live recon). | P2 | low |
| **P6** | In-RAM mode 4.3 option (2): display result inside imcontrol viewer behind a config flag. | P5 | low |
| **P7** (stretch) | `ChunkBrokerLiveSource` true frame streaming once M10 ChunkBroker lands. | M10 ChunkBroker | high |

Each phase is independently shippable and testable without hardware up to the
acquisition boundary (synthetic growing Zarr/HDF5 fixtures stand in for live
recordings).

---

## 6. Decisions (2026-06-22)

1. **Priority: faithful fast-MoNaLISA port first.** Reproduce the upstream
   GaussProcessor + Zarr live behavior end-to-end (P1→P2), proving the streaming
   contract on the real workload, *then* generalize (P3/P4). Phasing above
   already reflects this.
2. **Display: both, configurable.** Ship ImProcess `ReconstructionView` display
   first (P2, zero coupling), then add the imcontrol-viewer overlay behind a
   config flag (P6). The `StreamPlan`/result-routing must therefore be
   display-target agnostic from P2.

### Still to confirm (lower stakes — sensible defaults assumed)

3. **GPU** — default: keep `GaussProcessorGPU`/CuPy as an untested optional
   extra (CPU fallback with one info log), validate later if a CUDA box appears.
4. **TIFF live** — default: hdf5 + zarr for v1 streaming; TIFF stays batch-only
   until a concrete TIFF-streaming acquisition exists (P3 `TiffLiveSource` is
   best-effort folder-poll, deferred if not needed).

---

## 7. Implementation status (2026-06-22, branch `feat/live-reconstruction`)

**Landed & tested** (44 live unit tests green in openhands-clean venv):

- **P0** — streaming contracts (`StreamingReconstructor`/`StreamingSession`/
  `StreamPlan`/`StreamInit`/`StackInfo`/`Chunk`), `ZarrLiveSource`,
  `InMemoryStackWrapper`, `recording:*` metadata block in both storers.
- **P1** — fast-Gauss MoNaLISA: `gauss_processor` (CPU + guarded GPU),
  `localizer`, `scan_geometry`, `MonalisaLiveSession`; `MonalisaReconstructor`
  is now a `StreamingReconstructor` (`supports_streaming=True`, `make_session()`).
- **P2** — generic runtime: `live/workers.py` (`LiveStreamWorker`,
  `LiveProcessWorker`), `LiveReconstructionController` (streaming + batch
  fallback), comm-channel `sigLiveResultUpdated`.
- **P3** — `Hdf5LiveSource` + SWMR writer protocol in `HDF5Storer`
  (gated to Disk/DiskAndRAM; full `test_recording` regression green).
- **Integration glue** — `begin()` accepts nested *or* flattened scan-geometry
  attrs and scatters its first chunk; controller threads a `source_arg` to
  `LiveSource.open()`.

**Also landed (waves D/E/P5/P6):**

- ✅ **UI wiring** (D): `WatcherFrame` "Live (stream)" toggle + `sigLiveChanged`;
  new `LiveModeController` owns the `LiveReconstructionController` lifecycle,
  selects the source via `make_live_source`, routes results to
  `ReconstructionView`. Qt end-to-end test (synthetic growing Zarr → controller
  → stub streaming session → result) included.
- ✅ **P4** (E): `make_live_source(path, fmt=…)` factory + batch-fallback
  coverage proving non-streaming reconstructors (view-only + stub) run live at
  stack granularity.
- ✅ **P5**: `MemoryLiveController` (default-OFF) subscribes
  `memoryRecordings.sigDataSet` and routes completed HDF5 RAM recordings through
  the active reconstructor's batch `process()`, emitting `sigResultProduced`.
- ✅ **P6**: optional imcontrol-viewer display — `ModuleCommunicationChannel.
  sigLiveReconResult`, an `ImProcessMainController` bridge gated by a
  `live_display_in_imcontrol` config flag (default OFF) + imcontrol-registered
  check, and an `ImageController.liveReconResultAvailable` slot →
  `ImageWidget.addStaticLayer`.

Total: 110 live + recording tests green.

**Open items / cleanup for review (2026-06-23):**

- ✅ **Full-stack init for orientation**: `begin()` localizes/orients from the
  *first chunk*. Upstream initialized from the first full stack. The controller
  should buffer `frames_per_stack` before `begin()` for robust orientation
  (implemented by startup buffering in `LiveReconstructionController`, with
  tests covering multi-chunk startup polls and `frames_per_stack` buffering).
- ✅ **Real end-to-end with MoNaLISA numerics**: the Qt e2e test uses a stub
  streaming session; no test drives a real `MonalisaLiveSession` from a
  `ZarrLiveSource` (now covered by a deterministic structured synthetic Zarr
  fixture that flattens scan metadata through `ZarrLiveSource` and initializes
  `MonalisaLiveSession`).
- ✅ **Param-fetch path**: `LiveModeController`/`MemoryLiveController`
  `_getReconstructorParams()` guesses `_widget.parTree.get_param_dict()` (try/
  except → `{}`); now prefers the view's `getReconstructionParams()` path used
  by regular reconstruction, with legacy param-widget fallbacks and focused
  tests.
- ✅ **P6 polish**: `live_display_in_imcontrol` is read assuming
  `__processingConfig` is a `dict` — verify against the real config type; move
  the in-method `import numpy`; the 2D slice picks the middle index of leading
  dims (now supports dict/object/setup-like config, imports NumPy at module
  scope, and chooses the latest frame for time-labelled axes).
- ⬜ **P7** (stretch): `ChunkBrokerLiveSource` true frame streaming once M10
  ChunkBroker lands.

---

## 8. Big sanity check (2026-06-23)

Deep review of the assembled branch across six dimensions. Confirmed sound:
GaussProcessor numerics (byte-faithful port), attrs reconciliation (validated
by the real-zarr e2e), cross-thread result copies, SWMR gating, contracts /
batch-fallback / factory / RAM-handoff / imcontrol-gating (120 tests green).

Findings and dispositions:

- ✅ **#1 UI not runnable end-to-end** — FIXED. `LiveModeController` now watches
  the selected *folder* (via the shared `FileWatcher`, keyed by the comm-channel
  extension), discovers each new `.zarr`/`.h5` store, and runs a
  `LiveReconstructionController` per store **sequentially** (queue + advance on a
  new `sigFinished`). `start()` returns whether it actually started so an
  empty/not-yet-ready store advances the queue instead of stalling it. Skips
  unsupported stores. Tests in `test_live_discovery.py`.
- ✅ **#2 multi-timepoint/lapse** — FIXED. (a) Self-describing metadata:
  `recording:num_timepoints`/`lapse_index`/`single_lapse_file` written by the
  recorder (#2a). (b) **Per-file** lapses (`name_scanN.zarr`) already stream via
  the discovery layer. (c) **Single-file** lapses (`scan{N}` groups in one store)
  stream via a new `ZarrLapseSource` that presents the groups as one continuous
  global frame stream, which the existing session accumulates into one
  multi-timepoint result (#2b); `make_live_source` auto-routes to it.
  `MonalisaLiveSession` reads `recording:num_timepoints`, and a latent
  `push()` bug (per-stack `frame_inds` indexed with global indices) was fixed +
  regression-tested. Layout confirmed: each timepoint is its own
  store/`scan{N}` group (never one array spanning timepoints).
- ✅ **#3 live-growing init / discovered-too-early stores** — FIXED. Open +
  first-stack collection moved onto the stream-worker thread:
  `LiveStreamWorker(do_open=True)` opens with bounded retry (a store discovered
  before its `data` array exists is *waited on*, not skipped), collects the
  first `frames_per_stack` frames, then a resume gate lets the controller
  `begin()` before the remainder streams. `start()` returns True for the
  streaming path and always drives a `sigFinished` (even on failure) so the
  discovery queue advances rather than stalling. No main-thread block. Covered
  by `test_stream_worker_startup_*`.
- 🟡 **#4/#5/#6 fixed** — controller lifecycle: path methods report success so
  `start()` only marks running on success and resets workers on failed startup;
  session closed via controller-owned reference (no private `_session` reach);
  dropped unused import.
- 🟡 **#7** — the imcontrol bridge forwards *every* `sigResultProduced` (batch
  too), not only live; harmless, default-OFF. Left as-is.
- 🟡 **#8 CI** — running the whole `improcess/_test/` dir segfaults (Qt/vispy
  native crash, unrelated to live code). Run the live suite as a **selected set**
  of files, not the full directory.
