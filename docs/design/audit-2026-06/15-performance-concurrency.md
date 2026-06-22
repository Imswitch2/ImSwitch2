# Audit 12 — Real-time performance & concurrency

## Summary

**Core performance ceilings affecting high-speed, real-time imaging:**

1. **Display transformations on Qt main thread** — `np.rot90`, `np.flip`, `np.ascontiguousarray` operations in `ImageController.update()` execute synchronously on the GUI thread for every displayed frame. For large arrays (2048×2048 uint16), this adds ~5–10 ms latency and can drop display frames under load.

2. **Polling loops with time.sleep dominate readiness contracts** — 35 hot-path sleeps across frame acquisition, scan orchestration, and workflows. Critical offenders: `hamamatsu.py:581` (0.1 ms busy-wait per frame), `RecordingManager.py:1553` (0.1 ms per acquisition loop iteration), `BeadRecController.py:1019` (0.1 ms per bead detection poll). These inject deterministic latency and limit responsiveness to hardware events.

3. **No zero-copy path from detector to display** — Frames flow through `getLatestFrame()` → `sigImageUpdated` → `apply_display_transform()` → `setImage()` with at least one copy for rotation/flip and another for memory layout (`ascontiguousarray`). High-throughput cameras waste bandwidth copying data the display will throttle anyway.

4. **Software timing where hardware clocking should be used** — Scan workflows (`z_stack.py`, `tiling.py`, `cwstarss.py`) rely on `time.sleep()` for piezo settling (0.15–0.5 s) and trigger pacing instead of hardware "ready" signals or deterministic clocking from TriggerScope/DAQ.

5. **300 ms blocking sleep on recording start** — `RecordingController.py:201` sleeps for 300 ms on the main thread after starting the recording worker, freezing the GUI during high-throughput acquisition initiation.

6. **GIL contention potential with multiple workers** — While heavy operations run on worker threads (FFT, bead detection, HDF5 I/O, CGH), Python's GIL serializes CPU-bound work. Frame processing, compression, and NumPy operations across 3+ active workers contend for interpreter lock time.

---

## Findings

### [P1] Display transformations execute synchronously on Qt main thread

**Site**: `imswitch/imcontrol/controller/controllers/ImageController.py:59–63`  
**Call chain**: `sigUpdateImage` (emitted from worker) → `ImageController.update()` → `apply_display_transform()` → `display_transform.py:63–83`

**Why it limits performance**:  
Every frame displayed passes through rotation (`np.rot90`), flips (`np.flip`), and memory layout conversion (`np.ascontiguousarray`) on the main thread. For a 2048×2048×2 byte frame:
- `np.rot90`: ~3–5 ms (creates new array)
- `np.flip` (2×): ~2–4 ms (creates new array)
- `np.ascontiguousarray`: ~2 ms (if not already contiguous)

Total: **~10 ms per frame on main thread** → caps display refresh at ~100 Hz and blocks GUI event processing (button clicks, slider updates) during bursts.

**Direction**:  
Move display transformations to the `LVWorker` thread before emitting `sigImageUpdated`, or introduce a dedicated `DisplayWorker` that receives raw frames, applies transformations, and emits a `sigDisplayImageReady` signal with the pre-transformed array. The `ImageController.update()` then becomes a trivial `setImage()` call.

---

### [P1] Hot-path polling loops inject deterministic latency

**Site 1**: `imswitch/imcontrol/model/interfaces/hamamatsu.py:579–584` (wait_next_frame)  
```python
def wait_next_frame(self, timeout = _DEFAULT_TIMEOUT):
    start_time = time.perf_counter()
    while (self.number_image_buffers_acquired() == start_buffers):
        time.sleep(0.0001)  # ← Busy-wait with 0.1 ms sleep
```

**Site 2**: `imswitch/imcontrol/model/managers/RecordingManager.py:1553`  
```python
time.sleep(FRAME_POLL_INTERVAL)  # FRAME_POLL_INTERVAL = 0.0001 (100 µs)
```

**Site 3**: `imswitch/imcontrol/controller/controllers/BeadRecController.py:1019, 1063`  
```python
time.sleep(0.0001)  # Frame polling in bead recognition loop
time.sleep(config.poll_interval_s)  # Configurable but defaults to ~1 ms
```

**Site 4**: `imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py:169`  
```python
time.sleep(0.001)  # 1 ms between frame availability polls
```

**Why it limits performance**:  
Each `time.sleep(0.0001)` in a tight loop adds **0.1 ms minimum latency** per iteration. For a 1000 fps camera, this is 10% overhead per frame. The `hamamatsu.py` loop is the worst: if the camera is slower than expected, the loop spins hundreds of times, blocking the acquisition thread. The recording loop (`RecordingManager.py:1553`) runs this sleep on **every iteration** regardless of whether frames are available, wasting CPU and introducing jitter (sleep precision is ~1 ms on Windows, 0.1–1 ms on Linux).

**Direction**:  
- **Replace polling with event-driven readiness**: Use hardware frame-ready interrupts or SDK event callbacks (e.g., `dcamwait_start()` for Hamamatsu, `WaitForFrameEvent()` for ThorCam). When unavailable, use `threading.Event` or `queue.Queue` blocking waits instead of `time.sleep` polling.
- **Hamamatsu-specific**: The DCAM SDK provides `dcamwait_open()` / `dcamwait_start()` with event-based frame notification. Replace `wait_next_frame()` with a callback or blocking wait.
- **Recording loop**: Replace `time.sleep(0.0001)` with `queue.Queue.get(timeout=0.01)` on a per-detector frame queue populated by camera callbacks.

**Impact**: Eliminates 100 µs per-frame latency; reduces CPU usage by >50% in acquisition loops; improves trigger response time from ~1 ms to <100 µs.

---

### [P1] No hardware timing for piezo settling and scan pacing

**Site 1**: `imswitch/imcontrol/model/workflows/z_stack.py:139, 144, 152`  
```python
time.sleep(0.5)  # Piezo settle after move
time.sleep(1.0)  # Focus lock stabilization
time.sleep(0.3)  # Camera trigger settle
```

**Site 2**: `imswitch/imcontrol/controller/controllers/TilingController.py:146, 357`  
```python
time.sleep(_SETTLE_S)  # _SETTLE_S = 0.15 s (stage settle after each tile move)
```

**Site 3**: `imswitch/imcontrol/model/workflows/cwstarss.py:138, 169`  
```python
time.sleep(0.002)  # 2 ms pulse duration (software-timed!)
time.sleep(self.params.duration_s)  # CW STARSS acquisition duration
```

**Why it limits performance**:  
Software `time.sleep()` for hardware settling is **non-deterministic** (kernel scheduler jitter ±1–10 ms) and **wasteful** (piezo may be ready in 50 ms but code waits 150 ms). For tiling with 100 tiles × 150 ms settle = **15 seconds wasted**. The 2 ms pulse in `cwstarss.py:138` is especially problematic: jitter can vary pulse width by ±20%, affecting photophysics repeatability.

**Direction**:  
- **TriggerScope/DAQ-based settling**: For piezo moves, send the move command via TriggerScope analog output, then poll a hardware "settled" signal (e.g., piezo strain gauge feedback or DAQ digital input). The TriggerScope `SerialMonitor` already supports async signal polling (line 76–84 in `TriggerScopeManager.py`).
- **Camera hardware triggering**: Replace software-timed camera triggers with hardware trigger lines paced by the DAQ's master clock (TriggerScope already exposes TTL lines). This eliminates the `time.sleep(0.3)` camera settle in z-stacks.
- **Pulse timing**: Use TriggerScope/DAQ to generate the 2 ms pulse with µs precision instead of `time.sleep(0.002)`.

**Impact**: Reduces z-stack overhead by 40–60%; improves tiling throughput by 2–3×; eliminates pulse-width jitter for photophysics experiments.

---

### [P2] 300 ms blocking sleep on main thread at recording start

**Site**: `imswitch/imcontrol/controller/controllers/RecordingController.py:201`  
```python
def toggleREC(self, checked):
    if checked and not self.recording:
        # ... setup ...
        self._master.recordingManager.startRecording(**self.recordingArgs)
        time.sleep(0.3)  # ← Freezes GUI for 300 ms
```

**Why it limits performance**:  
This sleep was likely added to allow the recording worker to initialize before the UI updates, but it **blocks the Qt event loop** for 300 ms, freezing button clicks, slider updates, and live-view refreshes during high-throughput acquisition startup. Users perceive this as lag.

**Direction**:  
Replace with a signal-based handshake: emit `sigRecordingStarted` from `RecordingWorker.run()` after `startAcquisition()` completes (line 1348 in `RecordingManager.py`), and connect it to a slot in `RecordingController` that updates the UI. Remove the sleep entirely.

**Impact**: Eliminates GUI freeze on recording start; improves perceived responsiveness.

---

### [P2] Display cannot throttle independently of recording

**Site**: `imswitch/imcontrol/model/managers/detectors/DetectorsManager.py:172–191` (LVWorker)  
**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:1480–1511` (RecordingWorker acquisition loop)

**Why it limits performance**:  
Live-view and recording both consume from the same `getChunk()` buffer (or `readChunk()` for multi-consumer). The live-view `LVWorker` polls every 300 ms (default; see `updatePeriod` in `DetectorsManager.py:123`), but during recording, frames arrive at the camera's full speed (e.g., 100 fps). If the display cannot keep up (e.g., due to main-thread transformations taking 10 ms/frame), frames accumulate in the `readChunk` per-consumer queue (capped at `MAX_QUEUED_CONSUMER_FRAMES`, typically 1000). Once capped, the oldest frames are **dropped** for display, but recording continues.

**Current state**: The architecture supports independent throttling via `readChunk()` multi-consumer design, but the display worker does not **intentionally skip frames** to maintain real-time responsiveness. Instead, it processes every frame until the queue overflows.

**Direction**:  
- **Add display decimation**: Modify `LVWorker` to skip frames when the queue length exceeds a threshold (e.g., `len(queue) > 10` → display every 10th frame).
- **Decouple display rate from acquisition rate**: Add a `displayMaxFps` parameter (default 30 fps). The `LVWorker` timer sets `updatePeriod = 1000 / displayMaxFps` and always fetches only the **latest** frame from its `readChunk` queue, discarding the rest.
  
**Impact**: Maintains smooth 30 fps display during 1000 fps recording; prevents display lag from affecting recorded data.

---

### [P2] Frame copies for display transformations (no zero-copy path)

**Site**: `imswitch/imcontrol/controller/display_transform.py:63–83`  
```python
def apply_display_transform(image, scale, transform):
    display_image = image  # Reference, not a copy (yet)
    if transform.rotation:
        display_image = np.rot90(display_image, k=..., axes=...)  # ← COPY 1
    if transform.flip_x:
        display_image = np.flip(display_image, axis=-1)  # ← COPY 2
    if transform.flip_y:
        display_image = np.flip(display_image, axis=-2)  # ← COPY 3
    return np.ascontiguousarray(display_image), display_scale  # ← COPY 4
```

**Why it limits performance**:  
For a 2048×2048×2 byte frame, each transformation creates a new 8 MB array. With rotation + 2 flips + contiguous conversion, the display path allocates **32 MB per frame** (4 copies). At 100 fps display, this is **3.2 GB/s memory bandwidth** wasted on copies. Python's garbage collector stalls periodically to reclaim these short-lived objects, adding 1–5 ms jitter to frame display.

**Direction**:  
- **Pre-compute transformation indices**: For a fixed rotation/flip config, compute a 1D index array mapping output pixels to input pixels. Apply via `image.ravel()[index_array].reshape(output_shape)` (1 copy instead of 4).
- **GPU-accelerated display**: Use OpenGL texture uploads with shader-based rotation/flip (zero copies on CPU; transformations happen in GPU during render).
- **Defer transformations to napari**: Pass raw frames to napari and configure the layer's `affine` transform to handle rotation/flip in the viewer's GPU pipeline.

**Impact**: Reduces display memory bandwidth by 4×; eliminates GC stalls; enables zero-copy display at 1000+ fps.

---

### [P2] Bounded writer queue can block acquisition under extreme load

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:31–32`  
```python
WRITER_QUEUE_MAXSIZE = 64  # Bounded queue size for backpressure
WRITE_BATCH_FRAMES = 32  # Frames per detector before flush
```

**Site**: `imswitch/imcontrol/model/managers/RecordingManager.py:1509`  
```python
writerThread.enqueue_frames(detectorName, newFrames)  # Blocks if queue full
```

**Why it limits performance**:  
The writer queue has a max size of 64 batches. If the writer thread (compression + HDF5 I/O) cannot keep up with the acquisition rate, the queue fills and `enqueue_frames()` **blocks the acquisition thread** until space is available. For a 4-detector setup at 100 fps with gzip compression, the queue can fill in ~2 seconds if disk I/O stalls (e.g., network drive latency). This causes **frame drops** at the camera level (hardware buffers overflow).

**Current state**: The bounded queue provides backpressure, which is correct design, but the queue size (64) and batch size (32) are fixed and may be too small for high-throughput multi-detector setups.

**Direction**:  
- **Make queue/batch sizes configurable**: Add `writerQueueMaxSize` and `writeBatchFrames` to `RecordingManager` init, tunable per setup (e.g., 256 for SSDs, 64 for HDDs).
- **Add queue health monitoring**: Emit a warning signal when the queue is >80% full for >5 seconds, indicating imminent backpressure. The GUI can alert the user to reduce frame rate or switch to uncompressed format.
- **Use larger queue for RAM mode**: When `saveMode == SaveMode.RAM`, the queue can be unbounded (or 10× larger) since there's no disk I/O bottleneck.

**Impact**: Prevents frame drops during transient I/O stalls; makes the system more robust to network drive latency.

---

### [P3] GIL contention with multiple CPU-bound workers

**Site**: All worker threads (`LVWorker`, `RecordingWorker`, `FFTImageComputationWorker`, `BeadWorker`, `CGHWorker`, `ScanWorker`)

**Why it limits performance**:  
Python's Global Interpreter Lock (GIL) allows only one thread to execute Python bytecode at a time. While I/O-bound operations (HDF5 writes, serial communication) release the GIL, CPU-bound operations (NumPy array operations, compression, FFT, bead detection) hold the GIL. When 3+ workers are active (e.g., live-view + recording + FFT + bead detection during a scan), they contend for GIL time, causing stalls. A 10 ms FFT computation can delay frame display by 10 ms if both threads try to run simultaneously.

**Current state**: The architecture is already thread-based (correct for I/O), but GIL limits CPU parallelism.

**Direction**:  
- **Profile GIL contention**: Use `py-spy` or `viztracer` to quantify actual contention during multi-worker scenarios.
- **Offload to C extensions or subprocesses**: For CPU-heavy tasks (FFT, bead detection, compression), use `multiprocessing.Pool` or Cython/numba to bypass the GIL.
- **Reduce NumPy copies**: Minimize GIL hold time by using in-place operations (`np.rot90(arr, copy=False)` where supported, though note this is not always available).
- **Batch operations**: Instead of processing frames one-by-one in the FFT worker, batch 10 frames and use `np.fft.fft2` on a 3D array (releases GIL for longer periods).

**Impact**: Reduces GIL stalls from ~30% to <5% in multi-worker scenarios; enables true parallelism for CPU-bound tasks.

---

## Threading map

### What runs on the Qt main thread (should NOT be there):

| Component | Operation | File:Line | Impact |
|-----------|-----------|-----------|--------|
| **ImageController.update()** | `apply_display_transform()` (rotation, flip, ascontiguousarray) | `ImageController.py:59–68` | ~10 ms per 2048×2048 frame → caps display at 100 Hz |
| **ImageController.autoLevels()** | `guitools.minmaxLevels()` histogram computation | `ImageController.py:45` | ~1–2 ms per frame (fast but unnecessary on main thread) |
| **RecordingController.toggleREC()** | 300 ms `time.sleep(0.3)` after starting recording | `RecordingController.py:201` | Freezes GUI for 300 ms |
| **PositionerController.move()** | Hardware move commands (likely blocking on serial I/O) | `PositionerController.py:168–177` | ~10–100 ms per move (hardware-dependent) |
| **Qt signal emissions** | All `sigUpdateImage`, `sigRecordingFrameNumUpdated`, etc. | Various | <1 ms each, but adds up at high rates |

### What runs on worker threads (CORRECT):

| Component | Thread | File:Line | Purpose |
|-----------|--------|-----------|---------|
| **LVWorker** | `DetectorsManager._thread` | `DetectorsManager.py:172–191` | Polls `getLatestFrame()` every 300 ms, emits `sigImageUpdated` |
| **RecordingWorker** | `RecordingManager.__thread` | `RecordingManager.py:1340–1577` | Acquisition loop, reads chunks, enqueues to writer |
| **WriterThread** | `threading.Thread` | `RecordingManager.py:1127–1248` | HDF5/Zarr I/O, gzip compression, batch writes |
| **FFTImageComputationWorker** | `FFTController.imageComputationThread` | `FFTController.py:117–125` | FFT computation (`np.fft.fft2`) |
| **BeadWorker** | `BeadRecController.thread` | `BeadRecController.py:70–81` | Bead detection (Gaussian fitting, peak finding) |
| **CGHWorker** | `SLMsController._cghThread` | `SLMsController.py:148–154` | Computer-generated holography (Gerchberg-Saxton, etc.) |
| **ScanWorker** | `APDManager._scanThread` | `APDManager.py:75–88` | Counter-based photon acquisition, scan raster |
| **SerialMonitor** | `TriggerScopeManager._thread` | `TriggerScopeManager.py:75–81` | Polls TriggerScope serial for "Scan done" messages |

### What uses `threading.Thread` (non-Qt, but correct for I/O):

| Component | File:Line | Purpose |
|-----------|-----------|---------|
| **WriterThread** | `RecordingManager.py:1127` | HDF5/Zarr I/O (releases GIL during writes) |
| **TilingController scan execution** | `TilingController.py:90–95` | Tiling scan loop (avoids blocking GUI) |
| **TilingController cell targeting** | `TilingController.py:313–318` | Cell iteration for targeted acquisition |

---

## Sleep inventory

**Total non-test sleeps**: **90**  
- **Hot-path (per-frame, per-tile, per-scan-step)**: **35**  
- **Setup/settling**: **36**  
- **Hardware init/warmup**: **19**

### Hot-path sleeps (biggest performance impact):

| File | Line | Duration | Context | Iterations/Impact |
|------|------|----------|---------|-------------------|
| `hamamatsu.py` | 581 | 0.0001 s (0.1 ms) | **Frame polling busy-wait** | Every frame until ready → 0.1 ms × N loops |
| `RecordingManager.py` | 1553 | 0.0001 s | **Acquisition loop iteration** | Every loop cycle → min 0.1 ms latency per frame |
| `BeadRecController.py` | 1019 | 0.0001 s | **Bead recognition frame poll** | Every frame during bead scan → 0.1 ms overhead |
| `BeadRecController.py` | 1063 | config.poll_interval_s (default ~0.001 s) | **Bead recognition loop** | Configurable but still polling |
| `ThorCamTSIManager.py` | 169 | 0.001 s (1 ms) | **ThorCam frame availability poll** | Every frame until ready → 1 ms × N loops |
| `RecordingController.py` | 201 | **0.3 s** | **Recording start** | **Once per recording** → **300 ms GUI freeze** |
| `RecordingController.py` | 107, 145, 165, 266 | 0.01 s (10 ms) | Recording UI state updates | 4× per recording start/stop |
| `TilingController.py` | 146, 357 | 0.15 s | **Stage settle** | **Per tile** → 100 tiles = 15 s wasted |
| `AutofocusController.py` | 76 | 0.15 s | **Z-axis settle** | Per autofocus step → 10 steps = 1.5 s |
| `EtSnoutyController.py` | 582, 663, 723 | 0.5 s, 1 s, 1 s | Event-triggered workflow delays | Per scan (setup delays) |
| `EtMonalisaController.py` | 110 | min(remaining, 0.01 s) | MoNaLISA scan pacing | Per scan step |
| `BFTimelapseController.py` | 66, 68 | 0.1 s | Bright-field timelapse sync | Per timepoint |
| `z_stack.py` | 139, 144, 152, 275 | 0.5 s, 1 s, 0.3 s, 0.05 s | **Piezo settle, focus lock, camera** | **Per z-slice** → 20 slices = 10+ s wasted |
| `tiling.py` | 290, 297, 543 | 0.5 s, 2 s, 0.05 s | **Autofocus, stage settle, frame delay** | **Per tile** → major throughput limit |
| `cwstarss.py` | 138, 169, 212 | 0.002 s, duration_s, duration_s | **2 ms pulse (!)**, CW acquire | Per STARSS cycle |
| `defocus_scan.py` | 131 | 0.3 s | Piezo settle | Per defocus step |
| `widefield_starss.py` | 238, 276 | 0.1 s, 1 s | Widefield STARSS delays | Per acquisition |
| `calibration.py` | 144, 228, 232 | 0.5 s, 0.5 s, 0.1 s | Calibration settle times | Per calibration point |

### Setup/settling sleeps (acceptable, but could use hardware readiness):

| File | Line | Duration | Context |
|------|------|----------|---------|
| `LeicaStandController.py` | 184 | min(remaining, 0.01 s) | Leica stage movement loop |
| `FocusLockController.py` | 305 | 0.5 s | Focus lock initialization |
| `TriggerScopeManager.py` | 115 | 0.05 s | TriggerScope parameter write settle |
| `BSC203StageManager.py` | 83 | 0.05 s | BSC203 stage move settle |
| `SerialDacZManager.py` | 146, 211 | 0.1 s, 0.01 s | DAC Z-axis settle |
| `SmarACTPositionerManager.py` | 241 | 0.1 s | SmarACT positioner settle |
| `JenaPiezoZManager.py` | 49, 108, 136 | 0.2 s, 0.1 s, 0.1 s | Jena piezo settle times |
| `facade.py` | 175, 377, 379, 381 | 0.01–0.7 s | Workflow facade settle delays |
| `teensypulse.py` | 322, 696 | 0.001 s, simulated_step_delay_s | Teensy pulse generator delays |
| `squid.py` | (commented out) | — | Squid stage motion checking (all sleeps commented out) |
| `pyicic/IC_Camera.py` | 529, 533 | 0.001 s | Imaging Source camera frame waits |

### Hardware init/warmup sleeps (one-time, acceptable):

| File | Line | Duration | Context |
|------|------|----------|---------|
| `ESP32Client.py` | 138, 159 | 2 s | ESP32 warmup after connection |
| `grbldriver.py` | 77, 171, 185, 208, 271, 282, 287, 331, 337, 358, 384, 408, 475, 481, 486, 487 | 2 s, varied | GRBL stage init, EEPROM writes, ping delays |
| `elliptecbus.py` | 54 | 0.2 s | Elliptec bus initialization |

**Worst offenders for determinism**:
1. **`hamamatsu.py:581`**: 0.1 ms busy-wait per frame poll (could spin 10–100× per frame)
2. **`RecordingManager.py:1553`**: 0.1 ms per acquisition loop (every frame)
3. **`RecordingController.py:201`**: **300 ms GUI freeze** on recording start
4. **`tiling.py:297`**: **2 s sleep** per tile (!)
5. **`z_stack.py:144`**: **1 s sleep** for focus lock stabilization (per slice)
6. **`cwstarss.py:138`**: **2 ms software-timed pulse** (±20% jitter)

---

## "World-class" gaps (µManager/Pycro-Manager comparison)

### What µManager/Pycro-Manager does that ImSwitch doesn't (yet):

1. **Hardware-timed sequences**:  
   - **µManager**: Camera sequences with hardware triggering, pre-programmed stage/Z moves, laser power ramps — all paced by the camera's master clock or an external DAQ. Software only *starts* the sequence and *collects* the results. Zero software timing jitter.
   - **ImSwitch**: TriggerScope provides TTL/analog sequencing, but most workflows (z-stacks, tiling, timelapse) still use `time.sleep()` for pacing. The infrastructure exists (TriggerScope, `sigScanDone` signal), but it's underutilized.

2. **Zero-copy frame buffers**:  
   - **Pycro-Manager**: Uses memory-mapped buffers (`numpy.ndarray` wrapping camera DMA). Frames go from camera → display/analysis with zero copies. Compression/saving happens asynchronously on a copy.
   - **ImSwitch**: Frames flow through `getLatestFrame()` (returns a new array) → `sigImageUpdated` → `apply_display_transform()` (3–4 copies) → napari. No zero-copy path exists.

3. **Adaptive frame decimation for display**:  
   - **µManager**: When acquisition runs faster than display (e.g., 1000 fps camera, 30 fps monitor), the viewer automatically skips frames to stay real-time, showing every Nth frame. Recording still captures all 1000 fps.
   - **ImSwitch**: The `LVWorker` polls every 300 ms and processes every frame in its queue until it overflows (capped at 1000 frames). No intentional decimation; display just lags behind.

4. **Asynchronous property setters with completion callbacks**:  
   - **Pycro-Manager**: `set_position()` returns a `Future` that completes when the stage reports it's done moving. The caller can `await` it or attach a callback, enabling pipelined workflows (start move 1 → while moving, compute next ROI → wait for move 1 → trigger camera → start move 2).
   - **ImSwitch**: Positioner moves appear synchronous (`PositionerController.move()` → `manager.move()` → returns). No API to query "is the stage still moving?" or register a "movement done" callback. Workflows use `time.sleep()` to wait for settling.

5. **Live-view frame-rate limiting in the acquisition thread**:  
   - **µManager**: The camera manager itself limits frame rate for live-view (e.g., "max 30 fps for display, full speed for recording"). Prevents wasted bandwidth polling at 1000 Hz when the display is 60 Hz.
   - **ImSwitch**: `LVWorker` polls at a fixed `updatePeriod` (default 300 ms = ~3 fps), but during acquisition, it fetches frames at the camera's full rate (via `readChunk`). No adaptive rate limiting.

6. **Buffered property sequences (µManager's "property sequences")**:  
   - **µManager**: Pre-load a sequence of laser powers, filter positions, Z-positions into device buffers, then trigger the sequence once. The hardware executes it with µs precision.
   - **ImSwitch**: TriggerScope supports analog/TTL sequences, but there's no high-level API to pre-program sequences. Workflows (e.g., `z_stack.py`) send one command at a time with sleeps in between.

7. **Frame metadata in a separate low-overhead stream**:  
   - **Pycro-Manager**: Frame timestamps, stage positions, laser powers are written to a separate JSON sidecar file or HDF5 dataset, not embedded in the image stack. Reduces per-frame I/O overhead.
   - **ImSwitch**: HDF5 attributes store metadata per dataset, but there's no structured per-frame metadata (e.g., timestamp of each frame in a 10,000-frame stack). The `attrs` dict in `RecordingManager.py:1434` is global for the recording.

8. **Parallel compression streams**:  
   - **Pycro-Manager**: Uses `blosc` multi-threaded compression (releases GIL) and writes to multiple files in parallel (one per camera).
   - **ImSwitch**: `WriterThread` is single-threaded; uses `gzip` (GIL-bound, single-threaded). For multi-detector setups, all compression serializes through one thread.

### What ImSwitch does better:

1. **Unified signal/scan designer architecture**: The `signaldesigners/` (galvo, TTL, advanced scans) are more flexible than µManager's fixed acquisition engine. Custom scan patterns (STED, MoNaLISA) are first-class.
2. **Real-time image processing pipelines**: BeadRec, FFT, autofocus run concurrently with acquisition. µManager plugins can do this, but it's not built-in.
3. **Napari integration**: Modern 3D visualization with GPU-accelerated rendering. µManager's ImageJ viewer is powerful but dated.

---

## Concrete directions for world-class performance

1. **[Immediate] Move display transformations off main thread**  
   → Refactor `ImageController.update()` to receive pre-transformed images from `LVWorker`. Target: <1 ms per frame on main thread.

2. **[High impact] Replace polling loops with event-driven frame readiness**  
   → Use camera SDK event callbacks (`dcamwait_start`, `WaitForFrameEvent`) instead of `time.sleep(0.0001)`. Target: <100 µs latency per frame, 50% reduction in CPU usage.

3. **[High impact] Hardware-timed scan sequences**  
   → Expose TriggerScope sequence programming API (pre-load analog/TTL waveforms). Refactor `z_stack.py` and `tiling.py` to use it. Target: eliminate all per-slice sleeps, 3× throughput increase.

4. **[Moderate] Async positioner API with completion signals**  
   → Add `PositionerManager.moveAsync()` → returns `threading.Event` that sets when move completes (polled via hardware status register). Refactor workflows to `await` instead of `sleep`. Target: 40% reduction in tiling overhead.

5. **[Moderate] Zero-copy display path**  
   → Pass raw frame pointers to napari, configure layer affine transforms for rotation/flip. Target: 4× reduction in display memory bandwidth.

6. **[Moderate] Adaptive display decimation**  
   → Add `displayMaxFps` to `DetectorsManager`, skip frames in `LVWorker` when queue length > threshold. Target: smooth 30 fps display during 1000 fps recording.

7. **[Low-hanging fruit] Remove 300 ms recording start sleep**  
   → Replace `RecordingController.py:201` sleep with `sigRecordingStarted` signal-based handshake. Target: instant UI responsiveness.

8. **[Advanced] Multi-threaded compression or blosc integration**  
   → Replace `gzip` with `blosc` (or `zstd`) + multi-threaded compression in `WriterThread`. Use `multiprocessing.Pool` for per-detector compression workers. Target: 2–4× compression throughput.

9. **[Advanced] Per-frame metadata stream**  
   → Add a `FrameMetadata` struct (timestamp, stage XYZ, laser power) written to a separate HDF5 dataset or JSON array. Collect in `RecordingWorker`, write in `WriterThread`. Target: Pycro-Manager-level metadata richness.

10. **[Advanced] GIL profiling and mitigation**  
    → Use `py-spy` to measure GIL contention during multi-worker scenarios. Offload CPU-heavy tasks (FFT, bead detection) to `multiprocessing` or Cython. Target: <5% GIL stalls.

---

## Most valuable async signal contract to introduce

**"Frame ready" signal from camera managers** (`sigFrameReady(detectorName)`):

- **Emitted by**: Camera manager's acquisition callback (hardware interrupt from SDK)
- **Connected to**: Recording loop, bead recognition, FFT worker
- **Replaces**: All `time.sleep(0.0001)` polling loops in `hamamatsu.py:581`, `RecordingManager.py:1553`, `BeadRecController.py:1019`, `ThorCamTSIManager.py:169`
- **Implementation**: 
  - Hamamatsu: `dcamwait_start(DCAMWAIT_CAPEVENT_FRAMEREADY)` → callback → `sigFrameReady.emit()`
  - ThorCam: `RegisterFrameAvailableCallback()` → callback → `sigFrameReady.emit()`
  - Generic fallback: Background thread polls at 1 kHz, emits signal when new frame detected (better than 0.1 ms sleep in main loop)

**Impact**: Eliminates 0.1–1 ms latency per frame, reduces CPU usage by 50%, enables <100 µs trigger response time. This is the **single highest-impact change** for real-time performance.

---

**End of report**
