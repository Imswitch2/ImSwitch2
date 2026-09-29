# Live reconstruction inside ImControl: investigation and plan

**Status:** Investigation, nothing implemented (2026-09-29).
**Scope:** (1) a new ImControl widget that loads ImProcess reconstructors,
subscribes to the acquisition stream the way BeadRec does, and shows the
reconstruction live, preferably as a layer in ImControl's napari viewer;
(2) recording into RAM so that the recording is effectively already loaded
for ImProcess.
**Relates to:** [live-reconstruction-port.md](live-reconstruction-port.md)
(P5/P6 landed, P7 parked), [recording_dataflow_plan.md](../../recording_dataflow_plan.md)
(ChunkBroker, RAM cap), [memory-budgets.md](memory-budgets.md),
[dynamic-layer-lifecycle.md](dynamic-layer-lifecycle.md),
[detector-acquisition-selection.md](detector-acquisition-selection.md),
ROADMAP Milestones 10 and 12.

Line numbers refer to the tree at commit `8faa06d`.

---

## 1. Summary

Both requests are mostly plumbing between pieces that already exist. Nothing
needs a new frame-transport mechanism, and neither needs the parked
`ChunkBroker` rewrite.

**Widget.** ImProcess already has a generic live runtime
(`improcess/live/`: `LiveStreamWorker`, `RawDataBuffer`, `LiveProcessWorker`,
`LiveReconstructionController`) whose only input is a `LiveSource` object with
`open()/poll()/is_complete()/close()`. Every source today reads a growing
file. The missing piece is a `LiveSource` that reads the per-consumer detector
chunk queue (`DetectorManager.readChunk(consumerKey)`), which is exactly what
BeadRec polls in-process. That source plus one imcontrol widget/controller
pair gives any streaming reconstructor (MoNaLISA fast-Gauss, SMLM localizer,
view-only) live, in-process reconstruction with no file in between. Batch
reconstructors (beadrec, snouty, widefield-starss, ...) need a small
"one stack, then `process()`" adapter, because the live controller is
streaming-only today (older docs claim a batch fallback that no longer exists).

**Display.** The main viewer already accepts result layers
(`ImageWidget.addStaticLayer`, used by RAM snaps and by the existing
ImProcess-to-ImControl bridge `sigLiveReconResult`), but every call adds a new
layer. An "upsert" helper (create once, then swap `layer.data` in place) and
the coalescing pattern ImProcess already uses make the main viewer viable.
The fallback the request mentions (a very simple view inside the widget) is a
pyqtgraph `ImageItem` like FFT and BeadRec use; both can coexist behind one
toggle.

**RAM recording.** A RAM save mode exists and is wired through to ImProcess:
"Save in memory for reconstruction" writes HDF5 into a `BytesIO`, and at
finalize the file object is handed over the module channel and appears as a
"(MEMORY)" row in ImProcess's Multidata list. What is missing for "effectively
already loaded": the row must still be set as current by hand; the automatic
RAM reconstruction controller (`MemoryLiveController`) exists but has no UI
toggle and two bugs; Zarr RAM raises `NotImplementedError` while TIFF silently
never hands off; RAM buffers have no cap and are never released by the
recording manager. All of these are small, independent fixes. A zero-copy
array hand-off is possible later but is not needed to meet the request.

**Recommended order:** DetectorChunkLiveSource + attrs parity (no GUI) →
LiveRecon widget/controller on the existing runtime → main-viewer upsert
layer (and fix the existing bridge to use it) → RAM quick wins → optional
array hand-off. Details and estimates in §6.

---

## 2. What exists today

### 2.1 Reconstructor contract and registry (ImProcess)

- `Reconstructor` (`improcess/reconstructors/base.py:148`): abstract
  `make_param_widget(parent) -> QWidget` (:297), `make_metadata_dialog` (:310,
  every built-in returns None) and
  `process(data_obj, params, context=None) -> ProcessingResult` (:322), a pure
  function over a DataObj-like object and a plain params dict. Optional hooks:
  `default_params()` (:206), `prepare_params()` (:240), `accepts_raw_source()`
  (:273), `validate_source()` (:425), `estimate_resources()` (:345),
  `acquisition_requirements`, `requires_frame_stacks`.
- `StreamingReconstructor` (:664) adds `supports_streaming = True` and
  `make_session() -> StreamingSession`. `StreamingSession` (:618):
  `begin(StreamInit, params) -> StreamPlan`, `push(chunk, start, end)` with
  global frame indices, `result()`, optional `live_plane()` (one timepoint
  copy for cheap viewer updates), `finish()`, `close()`.
- Supporting types: `StackInfo(frame_shape, dtype, attrs, frames_per_stack,
  expected_frames, detector_name, dataset_path, source_format,
  acquisition_layout)` (:570), `Chunk(data, start, end)` (:585),
  `StreamPlan` (:594), `StreamInit(name, dataset_name, data, attrs,
  source_path, stack_info)` (:606).
- Streaming built-ins: `monalisa` (fast-Gauss only; `begin` needs
  `ScanStage:axis_startpos/axis_length/axis_step_size` in the attrs and the
  acquisition layout or `ScanTTL:Nx/Ny`, `monalisa/live_session.py:229-273`),
  `smlm-localizer` (`smlm/live_session.py:14-176`, uses
  `params['pixel_size_nm'] or 1.0`), `view-only`
  (`view_only/live_session.py:63`, buffer `(T, frames_per_stack, H, W)`,
  drops frames beyond the allocated size). Everything else is batch:
  `beadrec`, `snouty`, `snouty-projections`, `widefield-starss`,
  `tiling-mosaic`, `time-lapse`, `monalisa-legacy`.
- Registry: `PluginRegistry` (`reconstructors/registry.py:13`) holds
  instances by id; `get_registry()` is a process-wide singleton (:192) that
  `ImProcessMainController._initialize_plugins` clears and repopulates from
  the setup's `processing:` block (`ImProcessMainController.py:187-238`).
  Package helpers `available_reconstructor_ids()`,
  `available_reconstructor_specs()` and
  `register_reconstructor_by_id(registry, id)`
  (`reconstructors/__init__.py:97-128`) enumerate and instantiate built-ins
  and drop-ins; ImControl's Config Studio already calls them
  (`imcontrol/view/configeditor/editor.py:328-335`).
- Parameter widgets are plain `QWidget(parent=None)` subclasses with
  `get_values() -> dict` and no ImProcess base class. They can be hosted in
  any Qt window. Two wrinkles: `WidefieldStarssParamsWidget` lazily imports
  `improcess.view.ResultsTableWidget`, which drags in the whole ImProcess view
  package and napari (`widefield_starss/params_widget.py:135`); and the
  ImProcess GUI does not use `make_param_widget` for MoNaLISA at all, it
  installs the legacy `ReconParTree` and injects `params['scan_params']` from
  `MoNaLISAController._scanParDict`
  (`ReconstructorManagerController.py:469-474, 601-608`), and
  `MonalisaReconstructor.process()` requires that key
  (`monalisa/reconstructor.py:185-187`).
- Results: `ProcessingResult(name, data, axis_labels, view_modes,
  display_levels, axis_scales, scale_unit, ...)` (`model/result.py:154`),
  `display_layers()`/`display_layer_data()` (:406/:415).
  `result_to_layer_data(result)` (`model/napari_layers.py:70-87`) turns a
  result into napari `(data, kwargs, layer_type)` tuples, handles colormap,
  scale, units, contrast limits and localization point layers, and does not
  import napari. Provenance is attached through `run_reconstruction` and
  `record_snapshot` (`reconstructors/run.py:29-92`).
- Import closure: `imswitch.improcess.reconstructors`, `improcess.live` and
  `improcess.controller.LiveReconstructionController` import qtpy and
  pyqtgraph but not napari and no `improcess.view`/`improcess.controller`
  module (AST walk; the interpreter in the investigation container had no Qt
  installed, so this was not executed). Importing any reconstructor submodule
  runs `reconstructors/__init__.py`, which imports all ten built-ins.

### 2.2 The ImProcess live runtime

- `LiveSource` ABC (`improcess/live/sources.py:316-345`): `open(arg) ->
  StackInfo`, `poll() -> list[Chunk]` (at most 64 MiB per call), `is_complete()`,
  `close()`, attribute `idles_between_stacks`. All six implementations read a
  growing Zarr or HDF5 store on disk, gated by the recorder's
  `recording:frames_committed` barrier. `InMemoryStackWrapper`
  (`sources.py:348-468`) is not a source; it is a DataObj-compatible wrapper
  around a numpy stack plus attrs, with axis labels/scales and a lazily
  resolved `acquisition_layout`, and is what the RAM controller feeds to
  `process()`.
- Workers (`improcess/live/workers.py`): `LiveStreamWorker(source, source_arg,
  do_open, poll_interval_ms=200, open_max_attempts=50, ...)` (:14) opens with
  bounded retry, collects the first stack, waits for `resume(raw_buffer)`,
  then writes every frame into a `RawDataBuffer` and emits its global index.
  `LiveProcessWorker(session, raw_buffer, frame_gate, frames_per_stack,
  viewer_update_interval_s=0.2)` (:400) runs `session.begin()` on its own
  thread, pushes one frame per index, and publishes the first whole
  `result()` then `live_plane()` timepoints (`_publish_update`, :587-610).
- `LiveReconstructionController(comm_channel)`
  (`improcess/controller/LiveReconstructionController.py:16`) is a plain
  QObject: `start(reconstructor, source, params=None, source_arg=None,
  name=None) -> bool` (:67), `stop(graceful=True)` (:161), `sigFinished`. It
  needs only an ImProcess `CommunicationChannel` instance to emit
  `sigLiveResultUpdated`, `sigLiveTimepointUpdated` and `sigLiveTimepointDone`
  on. **It refuses reconstructors without `supports_streaming`** (:112-122);
  the batch fallback described in `improcess/README.md:167,499` and
  `docs/design/plans/live-reconstruction-port.md` §3.1 no longer exists in
  code.
- The viewer side coalesces updates (keep newest, `QTimer.singleShot(0)`) and
  swaps `layer.data` in place (`ReconstructionViewController.py:711-732,
  821-866`; `ReconstructionView.tryFastLiveUpdate` :329-385).

### 2.3 How BeadRec subscribes to the acquisition stream (ImControl)

`BeadRecController` (`imcontrol/controller/controllers/BeadRecController.py`)
is the pattern to copy:

1. `sigScanStarting` → `onScanStarting` (:478-547): pin the detector name for
   the whole scan and take a lease,
   `detectorsManager.acquire([det], LeasePurpose.WORKFLOW)`, so a
   trigger-driven camera is armed before any TTL output.
2. `sigScanStarted` → `onNewScan` (:1132-1186): open an atomic "frames after
   now" boundary with `execOn(det, lambda c: c.startChunkConsumer('BeadRec'))`
   and read the scan geometry (`getDimsScan`, `getScanStepSizes`,
   `getFramesPerScanPixel`, :1311-1351).
3. A `BeadWorker` on its own `Thread` polls
   `execOn(det, lambda c: c.readChunk('BeadRec'))` (:408-434) while
   `commChannel.isScanRunning()` or draining, applies the detector display
   transform, and emits `sigNewChunk`; the GUI-thread slot updates the
   widget's pyqtgraph image (:1493-1511).
4. `sigScanEnded` → `onEndedScan` (:1194-1219): drain late frames for up to
   0.5 s, then `releaseChunkConsumer('BeadRec')` and
   `detectorsManager.release(handle)` (:1247-1294).

The chunk broker (`model/managers/detectors/DetectorManager.py`):
`readChunk(consumerKey)` (:625-679) drains the hardware once and fans the
frames out to every registered consumer, returning and clearing the caller's
queue; `startChunkConsumer(key, kind=ChunkKind.DISPLAY)` (:916-947);
`releaseChunkConsumer(key)` (:902-914); `ChunkKind.RAW` gives the
measurement (identical to DISPLAY for cameras; for APD/PMT the whole volume
once, at the scan terminal). Each `(detector, consumer)` queue is bounded by
`memory.perDetectorQueueMB` (256 MiB default); on overflow the queue is
cleared and the consumer's next `readChunk` raises
`ChunkConsumerOverflowError` (:656-675). There is no backpressure toward the
detector and no chunk signal; consumers must poll. `sigUpdateImage` is a
sampled latest-frame stream (300 ms poll) and does not deliver every frame.
Existing consumer keys: `RecordingManager` (RAW), `BeadRec`,
`WorkflowFacade`, `tiling`, `autofocus`. Calling `.getChunk()` outside the
detector layer fails CI (`test_detector_chunk_consumers.py:200-258`).

Scan lifecycle: `sigScanStarting` (once per run, before arming),
`sigScanStarted`/`sigScanDone` (per iteration), `sigScanEnded` (once per run,
every terminal path); the running controller is `commChannel.getActiveScanSource()`
(`docs/scan-lifecycle.rst`).

### 2.4 Scan geometry and acquisition layout

- Producer-side layouts: `AcquisitionLayoutSource.getAcquisitionLayouts(detectorNames)`
  (`_acquisition_layout_source.py:25-33`) is implemented by the MoNaLISA,
  Advanced, PointScan, TriggerScope raster and the TriggerScope geometry-mixin
  controllers. Its only consumer today is
  `RecordingController._applyProducerAcquisitionLayouts`
  (`RecordingController.py:1283-1328`), which passes the layouts into
  `startRecording(acquisitionLayouts=...)`.
- The recorder derives the per-detector frame count in
  `RecordingWorker._expectedFramesFor` (`RecordingManager.py:3990-4034`; the
  layout count wins, cameras get `recFrames * numCamTTL`, scan-driven
  detectors get 1) and builds the dataset attributes in
  `_augment_attrs_with_recording_metadata` (:3890-3981): `acquisition:*`,
  `recording:detector_name/source_format/expected_frames/planned_frames/
  frames_per_stack/planned_partitions/num_timepoints/lapse_index/...`,
  `AcquisitionLayout:schema` and `AcquisitionLayout:json`. Both are methods on
  the worker and read its `recMode`, `recFrames`, `numCamTTL`,
  `acquisitionLayouts` and lapse fields. `ScanStage:*`/`ScanTTL:*` come from
  `sharedAttrs.getHDF5Attributes()` (`RecordingController.py:368-372`;
  `basecontrollers.py:2381-2382`).
- Reader side: `resolve_acquisition_layout(attrs, shape=..., detector=...)`
  (`improcess/model/acquisition_layout_resolver.py:1539-1660`) prefers
  `AcquisitionLayout:json`, then legacy adapters (`ScanStage`, `ScanTTL`,
  TriggerScope raster, SNOUTY), then OME axes, then shape. The improcess
  `beadrec` reconstructor consumes the layout through
  `select_loops(consumer="BeadRec")` (`beadrec/reconstructor.py:80-99`) and
  requires an exact frame count; the imcontrol BeadRec widget does not use
  layouts at all (X-fast raster assumption).

**Consequence:** if the live path hands a reconstructor the same attrs dict
the recorder would have written, every reconstructor sees the same geometry
live as it does from a file. The attrs builder must be shared, not copied.

### 2.5 The ImControl viewer surface

- `ImageWidget.napariViewer` is an `EmbeddedNapari` (`naparitools.py:124-184`)
  with protected live layers `Live: <det>` (`ImageWidget.py:86-103`), µm world
  units, and `setImage` handling ndim transitions by recreating the layer
  (:194-236, `_recreateLiveLayer` :124-175).
- `addStaticLayer(name, im, scale=None)` (:177-186) always calls
  `add_image`; napari auto-suffixes duplicate names. Two callers:
  `ImageController.memorySnapAvailable` (:124-128) and
  `ImageController.liveReconResultAvailable` (:130-134), the receiving end of
  `ModuleCommunicationChannel.sigLiveReconResult(name, image, scale)`. The
  emitting end, `ImProcessMainController._bridgeResultToImcontrol`
  (:939-1001), is gated by the `processing.live_display_in_imcontrol` flag
  (default off) and reduces the result to one 2D slice. Because every update
  adds a layer, this path is unusable for a stream of updates as it stands.
- `ImageController.update` resets the view only on the first live frame
  (:76-98), so a result layer is not disturbed by the live view afterwards.
- No ImControl widget currently adds or updates a result image layer in the
  main viewer. FFT, BeadRec, Tiling and AlignXY draw in their own pyqtgraph
  views; EtSTED opens two extra `EmbeddedNapari` viewers; ULenses only draws
  a vispy overlay. Every widget receives the main viewer as the
  `napariViewer` factory kwarg (`basewidgets.py:68-91`, `NapariHybridWidget`
  :208-238).
- All layer mutation happens on the GUI thread; worker signals are delivered
  queued to GUI-thread QObject slots (BeadRec, FFT, FocusLock, Tiling all rely
  on this).

### 2.6 RAM recording today

- `SaveMode.RAM = 2` and `SaveMode.DiskAndRAM = 3` (`RecordingManager.py:1974-1978`),
  exposed in the Recording widget as "Save in memory for reconstruction" and
  "Save on disk and keep in memory" (`RecordingWidget.py:134-138`). The row is
  shown only when the improcess module is registered
  (`RecordingController.py:119-122`).
- HDF5 + RAM: `h5py.File(BytesIO)` without SWMR or compression
  (:1340-1358); at finalize the `recording:*` attrs and OME-XML are written,
  the file is closed, and `sigMemoryRecordingAvailable(basename, BytesIO,
  filePath, False)` is emitted per detector from the writer thread
  (:1506-1516, :1582-1584). HDF5 + DiskAndRAM writes a normal disk file and
  hands over a read-only `h5py.File` handle (:1586-1611).
- `MasterController.memoryRecordingAvailable` stores it as
  `moduleCommChannel.memoryRecordings[name] = VFileItem(data, filePath,
  savedToDisk)` (`MasterController.py:124-129`), which emits
  `VFileCollection.sigDataSet` (`imcommon/model/VFileCollection.py:53-61`).
- ImProcess `MultiDataFrameController.memoryDataSet` (:49-63) wraps any
  non-h5py payload in `h5py.File(...)`, enumerates datasets and adds
  `DataObj(name, dataset, path=None, file=h5)` rows labelled "(MEMORY)"
  (`MultiDataFrame.py:143-150`). The user must still "Set as current"
  (:175-186); Save writes to the planned disk path via `saveToDisk`.
- `MemoryLiveController` (`improcess/controller/MemoryLiveController.py`)
  listens to the same signal and, when enabled, runs the active
  reconstructor's batch `process()` on each dataset and emits
  `sigResultProduced(result, "Live (RAM)")`. It is `_enabled = False` (:26)
  and `setEnabled` has no production caller; the Directory-watcher pane has
  no toggle. It reads `dataset[:]` and the root attrs by hand, so it never
  flattens `<det>/metadata/<Category>` (MoNaLISA's `ScanStage:*` are missing)
  and never descends `scanN` lapse groups.
- Gaps: Zarr + RAM raises `NotImplementedError('Zarr RAM streaming is not
  supported yet')` in the writer thread and fails the recording (:854-855,
  pinned by `test_recording.py:3801-3810`); Zarr + DiskAndRAM hands over a
  zarr group that `MultiDataFrameController` then passes to `h5py.File`
  (broken, untested); TIFF never emits the signal in any mode (:1821-1873).
  The `BytesIO` grows without a cap (`recording_dataflow_plan.md:57-60`);
  `RecordingManager._memRecordings` keeps every RAM buffer for the life of the
  process (:2024, :3860-3869) even after ImProcess deletes the row; the
  recordings folder is created on disk even in RAM mode
  (`RecordingController.py:220-222, 334-336`).
- The three memory limits (`imcommon/model/memory_limits.py`) bound the writer
  queue, the per-detector consumer queues and ImProcess's automatic working
  set. None covers RAM recordings, by design ("memory policy controls
  buffering and automatic work, not what can be measured",
  `memory-budgets.md`).
- ImControl and ImProcess run in one process, one `QApplication` and share
  one `ModuleCommunicationChannel` (`imswitch/__main__.py:38-76`).

### 2.7 Module boundary

`test_layering_boundaries.py` forbids improcess → imcontrol imports (except
`processing_config.py`) and has no rule against imcontrol → improcess. The
2026-06 layering audit rates the imcontrol ↔ improcess cycle HIGH and wants
shared code in `imcommon`. Existing imcontrol → improcess edges are lazy,
guarded, function-local imports of ImProcess *library* layers
(`configeditor/editor.py:328-335`, `model/plugins/validation.py:724-734`, the
latter pointing at a module that does not exist). The design below follows
that precedent: imcontrol imports `improcess.reconstructors`,
`improcess.live` and `improcess.model` lazily, never `improcess.view` or
`improcess.controller.*` other than `LiveReconstructionController`. Moving
the contracts (`base.py` types, `LiveSource`, the workers) to
`imcommon.processing` later would resolve the audit finding without changing
this design.

---

## 3. Design: the ImControl live-reconstruction widget

Working names: widget key `LiveRecon`, dock title "Live reconstruction",
`LiveReconWidget` / `LiveReconController`, consumer key `'LiveRecon'`.

### 3.1 Frame source: `DetectorChunkLiveSource`

A `LiveSource` implementation living in imcontrol (it needs
`DetectorsManager`, which ImProcess must not import), e.g.
`imcontrol/model/live_recon/detector_chunk_source.py`:

```python
class DetectorChunkLiveSource(LiveSource):        # improcess.live.sources.LiveSource
    idles_between_stacks = True                    # a scan lapse pauses between stacks

    def __init__(self, detectorsManager, detectorName, *, consumerKey,
                 stack_info: StackInfo, drain_grace_s=0.5, kind=ChunkKind.RAW): ...

    def open(self, _arg=None) -> StackInfo:
        # startChunkConsumer(consumerKey, kind) on the detector; return stack_info
    def poll(self) -> list[Chunk]:
        # frames = detector.readChunk(consumerKey)  (thread-safe, called from the stream thread)
        # apply the detector display transform if the widget asks for "as displayed"
        # (BeadRec does; recordings do not), stack, and return one Chunk at the cursor
        # ChunkConsumerOverflowError -> count the loss, startChunkConsumer again, mark
        # the run "incomplete"; a preview may be lossy, a recording may not.
    def mark_stream_ended(self): ...                  # called by the controller on sigScanEnded / user stop
    def is_complete(self) -> bool:
        # expected_frames reached, or stream ended and the drain grace elapsed with an empty read
    def close(self): releaseChunkConsumer(consumerKey)
```

This is the "P7 `ChunkBrokerLiveSource`" of the port plan, implementable now:
the per-consumer fan-out the broker was going to formalize already exists in
`readChunk`. It changes no detector or recording code and does not remove
acquisition polling; those were the reasons P7 was parked, and they do not
apply to this use.

`StackInfo` fields come from the same places the recorder gets them:
`frame_shape`/`dtype` from the detector (`detector.shape`, `detector.dtype`),
`frames_per_stack`/`expected_frames` from `_expectedFramesFor`-equivalent
logic (`commChannel.getNumScanPositions()`, `getNumCamTTL()`), the
acquisition layout from the active scan source's `getAcquisitionLayouts`, and
`attrs` from the shared attrs plus the recorder's metadata block (§3.2).
`source_format` = `"memory"`, `dataset_path` = `f"/{det}/data"` (what the
HDF5 writer records and what provenance reads back).

### 3.2 Metadata parity (one shared builder)

Extract the two recorder methods into pure functions in
`imcontrol/model/managers/recording_metadata.py`, driven by a small plan
dataclass instead of `self`:

```python
@dataclass(frozen=True)
class RecordingPlan:
    recMode, recFrames, numCamTTL, acquisitionLayouts, saveFormatName,
    recLapseTotal=1, recLapseIndex=0, singleLapseFile=False,
    recLapseIntervalS=None, recLapseScheduledTime=None

def expected_frames_for(plan, detectorName, isScanDriven) -> int
def build_recording_attrs(plan, detectorAttrs: dict, detectorName, *,
                          expectedFrames, exposureMs=None, startTime=None) -> dict
```

`RecordingWorker` calls them (behaviour unchanged, covered by the existing
`test_recording.py`), and `LiveReconController` calls them with
`saveFormatName="memory"`. Every reconstructor then resolves the layout and
`recording:*` keys identically for a file, a RAM recording and the live
stream. Without this the live path would re-implement the recorder's frame
accounting and drift, which is the failure mode `beadrec_audit_plan.md`
documents for the BeadRec widget.

### 3.3 Reconstruction runtime: reuse `LiveReconstructionController`

Instantiate ImProcess's `LiveReconstructionController` from imcontrol with a
private ImProcess `CommunicationChannel()` as its signal bus (the ImProcess
tests construct it exactly this way). Connect its `sigLiveResultUpdated`,
`sigLiveTimepointUpdated`, `sigLiveTimepointDone` and `sigFinished` to the
imcontrol controller. That gives, for free: open-with-retry, first-stack
collection before `begin()` (MoNaLISA orients from the whole first stack),
the raw buffer and frame gate, off-GUI-thread `begin()`/`push()`, the
0.2 s viewer cadence, `live_plane()` timepoint updates, stall timeout, a
graceful drain on stop, and provenance snapshots. The alternative (a leaner
runner in imcontrol composing the workers directly) duplicates ~300 lines of
policy that took several review rounds to get right.

**Batch reconstructors.** The controller refuses non-streaming plugins. Add
a generic adapter in `improcess/live/` (it restores what the README promises
and also serves the Directory watcher):

```python
class StackBatchSession(StreamingSession):
    """Run a batch Reconstructor once per complete stack."""
    def begin(self, init_obj, params):   # remember attrs/name, allocate a (frames_per_stack, *frame) buffer
    def push(self, chunk, start, end):   # write frames; when a stack is complete:
                                         #   run_reconstruction(recon, InMemoryStackWrapper(...), params)
                                         #   keep the latest ProcessingResult (or accumulate T for lapses)
    def result(self): return self._latest   # None until the first stack has been processed
```

`LiveProcessWorker._publish_update` (`workers.py:587-610`) must tolerate
`result() is None` ("nothing to show yet"); today it emits whatever
`result()` returns. `StackBatchSession` runs `process()` on the process
thread, so a slow batch reconstructor delays the next stack, not the GUI;
the frame gate and the consumer queue absorb the backlog (see §3.6).
`LiveReconstructionController.start` then wraps any reconstructor without
`supports_streaming` in `StackBatchSession` instead of refusing.

**Where the reconstructor instances come from.** The widget builds its own
`PluginRegistry` from the setup's `processing.reconstructors` list plus
drop-ins (`register_reconstructor_by_id`), independent of the ImProcess
module's singleton, so it works with ImProcess not loaded and does not share
mutable state with the ImProcess Parameters dock. The widget lists
reconstructors filtered by `accepts_raw_source`/`acquisition_requirements`
against the current scan source's layout (`inspect_source` on a
`StackInfo`-shaped stub is enough), and greys out the rest with the reason.

**MoNaLISA specifics (open item).** `MonalisaLiveSession.begin` reads
`ScanStage:*` from attrs and the layout, which the live source supplies, but
`MonalisaReconstructor.process()` and the ImProcess GUI additionally require
`params['scan_params']`. Whether the live session also needs it has to be
checked on the real code path (`LiveModeController._getReconstructorParams`
is what the Directory watcher passes). If it does, the imcontrol side can
build it from the MoNaLISA scan controller's parameters, which is where
ImProcess's `ScanParamsDialog` values originate anyway.

### 3.4 Display

**Main viewer (preferred).** Add to `ImageWidget`:

```python
def setResultLayers(self, jobName, layerData: list[tuple[np.ndarray, dict, str]]) -> None
    # per (data, kwargs, layer_type): find self.resultLayers[(jobName, kwargs['name'])];
    # create with add_image/add_points if missing (unprotected, additive);
    # else if ndim/shape unchanged: layer.data = data  (in place, no rebuild);
    # else recreate (generalize _recreateLiveLayer to any managed layer)
def removeResultLayers(self, jobName) -> None
```

`ImageController` gets a `sigResultLayersUpdated(jobName, layerData)`
commChannel slot with the ImProcess coalescing pattern (keep newest,
`QTimer.singleShot(0)` render). `LiveReconController` converts results with
`result_to_layer_data(result)` (no napari import), converts `scale` to µm
when the result's `scale_unit` is `nm` (the ImControl viewer is in µm; MoNaLISA
results are in nm), and for `live_plane()` updates writes the plane into its
own copy of the result before re-rendering. Default presentation is the
latest 2D plane (T-like axes at their last index, other leading axes at the
middle, the rule the existing bridge uses) with a "Full N-D layer" option
that lets napari show dims sliders. The result layer is created once per
scan run and updated in place, named `Recon: <reconstructor> (<detector>)`.

Migrate the existing `sigLiveReconResult` bridge to the same helper so it
stops adding a layer per update (one-line change in
`ImageController.liveReconResultAvailable`).

Known interactions, none blocking: the result layer shares the world
coordinate system with the camera layer (µm), so a reconstruction with scan
step sizes overlays at physical size; `NapariUpdateLevelsWidget` acts on the
selected layer and is unaffected; layer visibility persistence is by name
(`ImageWidget.py:312-353`) and will remember the result layer's visibility;
an N-D result layer adds dims sliders while the live layers stay 2D, which
napari handles by broadcasting.

**Fallback (inside the widget).** A pyqtgraph `ImageItem` +
`HistogramLUTItem` like `FFTWidget.py:39-46`, showing the same latest plane,
plus a plane spinner for N-D results. A "Show in: Main viewer / Widget"
toggle selects one; both draw from the same coalesced result.

### 3.5 Lifecycle, leases and overflow

Scan-coupled mode (default), mirroring BeadRec:

| Event | Action |
|---|---|
| user ticks "Live" | build registry entry + param widget; if a scan is running, arm now |
| `sigScanStarting` | pin detector name; `acquire([det], LeasePurpose.WORKFLOW)`; read geometry, layouts, attrs; build `StackInfo` (§3.1/3.2) |
| `sigScanStarted` | `LiveReconstructionController.start(recon, DetectorChunkLiveSource(...), params, name=run_name)`; the source opens the consumer boundary on the stream thread |
| `sigScanDone` (repeat / lapse iterations) | nothing; the source keeps counting global frame indices; `frames_per_stack` boundaries give one timepoint per iteration |
| `sigScanEnded` | `source.mark_stream_ended()`; the stream worker drains up to `drain_grace_s`, `is_complete()` turns true, the controller finishes (final `result()`), `sigFinished` → release the lease |
| user unticks "Live" | `stop(graceful=True)`; release consumer and lease |
| `closeEvent` | `stop(graceful=False)`; the 30 s controller shutdown barrier covers thread joins |

Free-running mode (no scan; SMLM on a camera stream, view-only): "Live"
takes the WORKFLOW lease itself so the camera acquires even with live view
off, `frames_per_stack` is a user setting ("frames per update"),
`expected_frames` is None, and `is_complete()` is true only on user stop.
MoNaLISA is not offered here (`requires_frame_stacks`).

Overflow policy: if the session is slower than acquisition, the frame gate
blocks the stream worker, `readChunk` is not called, and the consumer queue
crosses `perDetectorQueueMB`, at which point the next read raises. The source
catches `ChunkConsumerOverflowError`, re-registers, counts the lost frames,
and the run is marked "incomplete: N frames dropped" in the widget status and
in the provenance snapshot status. This is the opposite of the recorder's
policy (fail the recording) and is the right one for a preview. A recording
running alongside is unaffected: it has its own consumer queue.

The thread model is unchanged from ImProcess: `poll()` and `push()` run on
the two worker QThreads; result signals arrive queued on the GUI thread;
`begin()` never runs on the GUI thread.

### 3.6 Widget UI

- Reconstructor combo (ids from the setup's `processing.reconstructors`, plus
  drop-ins; incompatible ones disabled with a tooltip) and a "Load
  reconstructor" action for the other built-ins, like ImProcess's Tools menu.
- Parameter area hosting `reconstructor.make_param_widget(self)`; values read
  with `get_values()` at scan start; `warn_contract_problems` on install.
- Detector picker (default: current detector, pinned per run).
- Mode: "During scans" / "Free-running (N frames per update)".
- Frames: "As recorded (RAW)" / "As displayed" (display transform applied,
  the BeadRec convention).
- "Live" toggle, status line (state, frames received/expected, last update
  age, dropped frames), "Show in: Main viewer / Widget", "Keep last result
  layer after run".
- Optional: "Save last result…" (`result.save(path, fmt)`), "Send last stack
  to ImProcess" (§4.5).
- `StatefulComponentMixin` state: reconstructor id, params, detector, mode,
  display target (registered as `'LiveRecon'`; JSON-safe).

### 3.7 Registration checklist

1. `view/widgets/LiveReconWidget.py` (subclass `Widget`; `NapariHybridWidget`
   is not needed since layers go through `ImageController`).
2. `_WIDGET_MODULES` in `view/widgets/__init__.py`.
3. `controller/controllers/LiveReconController.py` (`ImConWidgetController`).
4. `_CONTROLLER_MODULES` in `controller/controllers/__init__.py`.
5. `_DEFAULT_RIGHT_DOCK_INFOS` and `_DOCK_DISPLAY_NAMES` in `ImConMainView.py`.
6. `availableWidgets` docstring in `ViewSetupInfo.py`, the Config Studio
   group list (`editor.py:2593-2674`, group "Analysis"),
   `docs/setupinfo-reference.rst`, `docs/gui.rst`, `tools/screenshot_widgets.py`.
7. New `CommunicationChannel` signal `sigResultLayersUpdated(str, object)`
   (add to `communication_channel_signal_inventory.json`; the contract test
   pins the inventory).
8. Add `"LiveRecon"` to `mock_scan_monalisa_live.json` for the no-hardware
   loop.

### 3.8 Relation to the BeadRec widget

The improcess `beadrec` reconstructor plus this widget reproduces BeadRec's
core (ROI mean per scan position, layout-driven raster, fits) generically and
correctly for every scan source that publishes a layout, which the BeadRec
widget's raster assumption does not. BeadRec's extras (viewer ROI drawing,
the EtSTED centre-query workflow, TIFF load/save) are not covered. Recommend
keeping BeadRec as is now and revisiting once the new widget is in use; a
later step could make the BeadRec widget a thin front-end over the new
runtime.

---

## 4. Recording into RAM

### 4.1 What "already loaded for ImProcess" needs

The data already arrives in ImProcess without touching disk. What a user
experiences as "not loaded" is the row in the Multidata list that must be
selected by hand, the missing auto-reconstruct, and the formats for which
RAM mode silently or loudly does nothing. Fixes, in order of value per line:

1. **Auto-open policy in ImProcess.** A persisted preference
   (`improcess_options.json`, alongside `folder_preferences.py`):
   `memoryRecordings: "list" | "current" | "reconstruct"`. `current` calls
   the same `sigCurrentDataChanged` path "Set as current" uses
   (`MultiDataFrameController.py:175-186`), which already selects a
   compatible reconstructor, runs `inspect_source` and auto-runs pass-through
   plugins. `reconstruct` enables `MemoryLiveController`. A checkbox in the
   Multidata dock exposes it.
2. **Fix `MemoryLiveController`** to build its inputs the way the Multidata
   path does (`DataObj(name, ds, file=h5)` → `resolve_image` merges the
   `metadata/<Category>` groups and finds `scanN` datasets), instead of
   reading `dataset[:]` and root attrs by hand. Today MoNaLISA cannot run
   from a RAM recording through it, and lapses are skipped.
3. **Format honesty in the Recording widget.** Validate at REC time: RAM
   modes require HDF5 until Zarr/TIFF support lands (a status-bar refusal
   instead of a writer-thread `NotImplementedError` for Zarr and silence for
   TIFF). Do not create the output folder in RAM-only mode.

### 4.2 Zarr and TIFF in RAM

`DataObj(file=...)` already accepts a zarr group and a `tifffile.TiffFile`
(`image_sources.py:197-221`); only `MultiDataFrameController.memoryDataSet`
forces every payload through `h5py.File`. Passing zarr groups and TiffFile
objects straight through makes Zarr + DiskAndRAM work as is, and makes Zarr +
RAM a `zarr.storage.MemoryStore` root group handed over at finalize (the
"MemoryStore policy" ROADMAP M10 lists as remaining), with `saveToDisk`
implemented by `zarr.copy_store`. TIFF + RAM is `TiffFile(BytesIO)` handed
over from `TiffStorer.finalizeStream`. Both are contained changes;
`VFileItem.data` needs its type widened accordingly.

### 4.3 Memory policy

Consistent with `memory-budgets.md` (no limit may refuse a measurement):

- At REC in a RAM mode, surface the planned size the worker already logs
  (`RecordingManager.py:4500-4517`) in the status bar, and compare it with
  free system memory (psutil is already used for the RAM bar in
  `MultiModuleWindowController`); warn, do not refuse. For `UntilStop` and
  `SpecTime` (unbounded), show a running "held in RAM" figure and warn when
  it crosses a settable `memoryRecordingsWarnMB` in `Options.memory`.
- Release: drop the `_memRecordings` reference once the item is handed to
  the `VFileCollection` (it is only used for name de-duplication and lapse
  reuse; both can be keyed by path without holding the buffer), and connect
  `VFileCollection.sigDataRemoved` so deleting the row in ImProcess frees the
  memory. Today nothing does.
- No eviction of unsaved recordings; a user who wants memory back saves or
  deletes the row.

### 4.4 Streaming from a RAM recording (not recommended)

`Hdf5LiveSource` accepts an open `h5py.File`, including one on a `BytesIO`
(`sources.py:1299-1302, 1433-1442`), so one could imagine live-reading the
RAM recording while it is written. The RAM writer has no SWMR, no flushes and
no committed-frames barrier (:1340-1358), and h5py is not safe for a
concurrent reader on the same file object. The chunk-broker source (§3.1)
is the right live path; the RAM recording is the retained copy.

### 4.5 Zero-copy hand-off (later)

`InMemoryStackWrapper` is already DataObj-compatible and provenance describes
it as a "memory" source. A `VFileItem` variant carrying `(array, attrs)`
would let the live widget publish its last stack to ImProcess with one
signal and no HDF5 serialization, and would let a future array-backed RAM
recording skip the `BytesIO` round trip. It needs `VFileCollection.saveToDisk`
for arrays (write through `HDF5Storer.snap` or `result_io`) and a
`MultiDataFrameController` branch that adds the wrapper as a row. Worth doing
only if the HDF5-in-memory copy proves to be a bottleneck; for a typical
scan stack it is one memcpy.

### 4.6 The combined flow

With §3 and §4.1 in place, the user experience for "record into RAM and
reconstruct" is: pick "Save in memory for reconstruction", tick "Live" in
the LiveRecon widget, press REC and run the scan. The recorder and the live
widget are two consumers of the same chunk queue; the widget shows the
reconstruction as it forms in the main viewer; at finalize the recording
appears in ImProcess, is opened as current (or reconstructed) automatically,
and the full-fidelity result with provenance lives there. No new transport,
no file.

---

## 5. Risks and open questions

- **Batch fallback placement.** The `StackBatchSession` adapter and the
  `result() is None` tolerance change ImProcess code. The alternative is to
  keep both in imcontrol and construct the workers directly, at the cost of
  duplicating the controller's policy. Recommendation: put it in ImProcess;
  the README already claims the behaviour.
- **MoNaLISA `scan_params`** in the live path (§3.3): verify on the
  `mock_scan_monalisa_live.json` loop before committing to the parameter UI.
- **Multi-timepoint runs.** Sessions size their output from
  `recording:num_timepoints`; a repeat scan or a scan lapse without a
  recording has no known count. v1 treats each scan run as one job and each
  iteration as one timepoint, and passes `num_timepoints` only when a
  recording supplies it; view-only drops frames past its allocation, so
  "unknown T" needs a growth policy in `ViewOnlyLiveSession` or an explicit
  per-run reset.
- **Point detectors.** RAW chunks from APD/PMT arrive once at the scan
  terminal (the whole volume), so "live" degrades to "at scan end" for them,
  which matches what those reconstructors can do anyway.
- **Import cost.** Importing `improcess.reconstructors` imports all ten
  built-ins (scipy, skimage, pandas, optional cupy). Import lazily on first
  widget use, not at ImControl startup, and guard with the same try/except
  pattern Config Studio uses.
- **Widefield-STARSS param widget** drags in the ImProcess view package and
  napari through a lazy import; exclude it from the live widget's offer, or
  fix the import in ImProcess.
- **Layer inventory contract.** A new commChannel signal changes
  `communication_channel_signal_inventory.json`; that is intended, and the
  contract test must be updated in the same change.
- **Documentation drift found on the way.** `improcess/README.md` describes
  a batch fallback, a `WatcherFrameController`, an `update_cadence` chunk
  count and a `MemoryLiveController` role that the code does not have;
  `docs/issues/recording-chunk-consumer-overflow.md` cites frame-count
  constants that were replaced by byte budgets; `dynamic-layer-lifecycle.md`
  is still "NOT IMPLEMENTED". Worth a docs pass alongside this work.

---

## 6. Phased plan

| Phase | Deliverable | Depends on | Effort | Tests (no hardware) |
|---|---|---|---|---|
| 1 | `RecordingPlan` + `expected_frames_for` + `build_recording_attrs` extracted; `RecordingWorker` uses them | — | 1 day | existing `test_recording.py` unchanged; new unit tests for the pure functions |
| 2 | `DetectorChunkLiveSource` (open/poll/complete/overflow/close) | 1 | 1–2 days | fake detector with `readChunk`/`startChunkConsumer` (the `test_beadrec_prearm_lease.py` doubles), overflow re-registration, drain grace |
| 3 | `StackBatchSession` in `improcess/live/`, `_publish_update` tolerates None, `LiveReconstructionController.start` wraps batch plugins | — | 1 day | run beadrec and view-only through the existing Qt live tests with a stub source |
| 4 | `LiveReconWidget` / `LiveReconController`: registry, param hosting, lease lifecycle, scan-coupled and free-running modes, state persistence, registration checklist | 1–3 | 3–5 days | controller tests with fake master/commChannel; end-to-end on `mock_scan_monalisa_live.json` (view-only, beadrec, MoNaLISA fast-Gauss) |
| 5 | `ImageWidget.setResultLayers`/`removeResultLayers`, `sigResultLayersUpdated` + coalescing in `ImageController`, existing `sigLiveReconResult` bridge migrated, widget fallback view | 4 | 1–2 days | layer upsert unit tests (in-place update, ndim change, removal), inventory contract update |
| 6 | RAM quick wins: auto-open preference + `MemoryLiveController` fix and toggle, REC-time format validation, `_memRecordings` release, planned-size announcement | — | 2–3 days | `test_memory_live_controller.py` extended with metadata groups and `scanN`; `test_recording.py` for release; widget test for the refusal |
| 7 | Zarr `MemoryStore` and TIFF RAM hand-off, `memoryDataSet` payload pass-through | 6 | 2–3 days | RAM round trips per format in `test_recording.py`; Multidata rows per payload type |
| 8 (optional) | Array-backed `VFileItem` + "Send last stack to ImProcess" | 4, 6 | 2–4 days | provenance and save round trip |

Phases 1–3 and 6 are independent of each other and can proceed in parallel.
Rig validation is needed only for phase 4's overflow behaviour under real
frame rates and for MoNaLISA numerics, which the existing fast-Gauss tests
already pin.

---

## 7. Fact index (for review)

| Topic | Where |
|---|---|
| Chunk broker API and budget | `DetectorManager.py:78-114, 608-679, 902-947` |
| Leases | `_acquisition_leases.py:19-45`, `DetectorsManager.py:149-200` |
| BeadRec subscription lifecycle | `BeadRecController.py:99-110, 408-434, 478-547, 1132-1186, 1194-1294, 1717-1853` |
| Scan lifecycle signals | `CommunicationChannel.py:117-136`, `docs/scan-lifecycle.rst` |
| Layout producers / recorder consumer | `_acquisition_layout_source.py:25-33, 343-389`, `RecordingController.py:1283-1328` |
| Recorder attrs and frame accounting | `RecordingManager.py:3890-3981, 3990-4034, 4108-4137` |
| Viewer layers | `ImageWidget.py:86-103, 124-186, 194-236, 312-353`, `ImageController.py:76-134` |
| ImProcess bridge | `ModuleCommunicationChannel.py:12`, `ImProcessMainController.py:939-1001` |
| Streaming contract | `reconstructors/base.py:570-672` |
| Registry helpers | `reconstructors/__init__.py:97-168`, `registry.py:13-201` |
| Live runtime | `live/sources.py:316-468`, `live/workers.py:14-663`, `LiveReconstructionController.py:16-135` |
| RAM save path | `RecordingManager.py:1340-1358, 1506-1611, 1974-1978, 2016-2024, 3860-3869`, `MasterController.py:124-129`, `VFileCollection.py`, `MultiDataFrameController.py:49-63, 175-186`, `MemoryLiveController.py:26-119` |
| Memory limits | `imcommon/model/memory_limits.py`, `memory-budgets.md` |
| Layering rules | `imcontrol/_test/unit/test_layering_boundaries.py:83-162`, `docs/design/audit-2026-06/02-layering-boundaries.md:60-84` |
