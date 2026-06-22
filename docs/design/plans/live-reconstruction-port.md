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
  *load* side is solved for batch.
- **Structured Zarr streaming save** (M10, 2026-05-29): `ZarrStorer` writes
  `(T,Y,X)` per-detector arrays via `resize`/slice-assign — the exact growing
  array the stream worker wants to follow.
- **In-RAM cross-module bridge already wired:**
  `ModuleCommunicationChannel.memoryRecordings` (`VFileCollection`).
  `RecordingManager` emits `sigMemoryRecordingAvailable` /
  `sigMemorySnapAvailable` on `SaveMode.RAM`/`DiskAndRAM`;
  `improcess/controller/MultiDataFrameController.py` already *consumes*
  `memoryRecordings.sigDataSet` to surface RAM recordings as loadable datasets.
  **The RAM hand-off path from imcontrol → improcess exists today — but only on
  recording *completion*, not frame-by-frame.**
- **imcontrol can display extra image layers:** `ImageController.addImage(name)`
  + `setImage(name, im, scale)` via `sigUpdateImage` — a reconstructed result
  can be shown as a named layer in the live viewer.
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

Add to `reconstructors/base.py`:

```python
class StreamingSession(ABC):
    """One live reconstruction in progress. Created per stack/recording."""
    @abstractmethod
    def begin(self, init_obj: DataObj, params: dict) -> StreamPlan: ...
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
  the live controller buffers a full stack and calls the existing
  `process(data_obj, params)` once per completed stack/time-point. This
  satisfies requirement (a) "arbitrary reconstructors" — every registered
  reconstructor works live, just at stack granularity rather than chunk
  granularity. Streaming-capable plugins additionally get sub-stack updates.
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
  file-count hack). Chunk size from `array.chunks[0]`.
- **`Hdf5LiveSource`** — re-open in SWMR (`libver='latest'`, `swmr=True`) and
  watch the resizable dataset's growing length. The M10 `WriterThread` writes
  HDF5 in resizable batches; enabling SWMR on the writer side makes this safe.
- **`TiffLiveSource`** — best-effort: TIFF is append-by-file (one frame/file or
  multi-page growth). Reuse the existing `FileWatcher` to detect new pages/files
  in a stack folder. Documented as "polling, no partial-frame guarantee".

Stack/geometry metadata (frames-per-stack, time-points) comes from the dataset
attributes the structured storers already write — **not** from MoNaLISA-specific
`ScanStage:*` keys. The MoNaLISA *session* may still read those keys for its own
geometry; the *source* layer stays modality-agnostic.

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
Already 90% wired. On `SaveMode.RAM`/`DiskAndRAM`, `RecordingManager` emits
`sigMemoryRecordingAvailable` and improcess's `MultiDataFrameController` already
ingests it as a `VFile` dataset. Add: when a live-reconstruction toggle is on,
auto-route a newly-arrived memory recording through the active reconstructor's
batch `process()` (or a streaming session in one shot) and display the result.
**No new acquisition-thread coupling.** This delivers "reconstruct what was just
recorded from RAM, no file on disk" with minimal risk.

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
2. **In imcontrol viewer (opt-in):** push the result as a named layer via
   `ImageController.addImage`/`setImage` + `sigUpdateImage`. Requires a small
   cross-module signal on `ModuleCommunicationChannel`
   (`sigLiveReconResult(name, image, scale)`); imcontrol's `ImageController`
   subscribes and shows it as an overlay layer alongside live detectors.

Recommend shipping (1) first; add (2) behind a config flag once (1) is proven.

---

## 5. Phased plan

| Phase | Deliverable | Depends on | Risk |
|---|---|---|---|
| **P0** | This audit + contract spec; land `StreamingReconstructor`/`StreamingSession`/`StreamPlan`/`LiveSource` ABCs in `reconstructors/base.py` + `live/` package skeleton, with unit tests on the batch-fallback path (no hardware). | — | low |
| **P1** | Port the **fast Gauss reconstructor** (`GaussProcessorCPU`, `localizer`, `geometry`) into `reconstructors/monalisa/` as a streaming session; rename `karl_*`→descriptive; GPU behind extra. Unit-test `process_chunk` scatter against a saved stack. | P0 | med (numerics) |
| **P2** | `ZarrLiveSource` + generic `LiveStreamWorker`/`LiveProcessWorker` + `LiveReconstructionController`; wire to the existing `WatcherFrame` UI. Reproduce upstream Zarr live behavior end-to-end, cleaned. | P0,P1 | med |
| **P3** | `Hdf5LiveSource` (SWMR) + `TiffLiveSource` (folder poll). Enable SWMR on the M10 HDF5 `WriterThread`. Format-matrix tests. | P2 | med (SWMR/locking) |
| **P4** | Generic **batch-fallback streaming** so *any* registered reconstructor runs live at stack granularity (view-only, SNOUTY, WFS). | P2 | low |
| **P5** | In-RAM mode 4.1 (auto-route RAM recordings through live recon). | P2 | low |
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
