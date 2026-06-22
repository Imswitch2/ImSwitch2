# Rust Port Pilot Targets

This note describes two contained ImSwitch hot-path candidates for a first Rust
experiment. The intent is not to rewrite subsystems, but to create small
PyO3/maturin extension modules with Python fallbacks, golden-output tests, and
benchmarks that show whether Rust is worth integrating more deeply.

## 1. Galvo Scan Curve Generation

### Task Description

The Galvo scan designer is a good first integration pilot because it is
deterministic and hardware-independent: scan parameters and setup metadata go in,
analog voltage arrays and scan metadata come out. The current implementation is
centralized in `imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py`,
especially `make_signal()` and the private helpers that generate smooth
turnaround curves, step axes, padding, and metadata. A Rust implementation can
start by replacing the numeric waveform kernel while preserving the existing
Python class API.

This is primarily a correctness and integration pilot. It may not produce the
largest runtime speed-up, because scans are built before acquisition and the
current Python code already uses NumPy and SciPy for much of the heavy work.
That is acceptable for a first target: the value is that it exercises typed
array transfer, Rust packaging, optional fallback behavior, and strict numerical
comparison in a low-risk path.

The Rust function should initially mirror one complete path: smooth fast-axis
generation plus step slow-axis generation for normal XY galvo scans. Once the
extension passes parity tests, expand to mock/repeat/timelapse axes and higher
dimensions. The existing Python implementation remains the oracle until the
Rust path is proven equivalent.

### Pseudo Code

```python
# Python wrapper in GalvoScanDesigner.py
try:
    from imswitch_rust_scan import make_galvo_signal as _make_galvo_signal_rust
except ImportError:
    _make_galvo_signal_rust = None

def make_signal(self, parameterDict, setupInfo):
    prepared = _prepare_galvo_inputs(parameterDict, setupInfo)
    if _make_galvo_signal_rust is not None and _rust_supported(prepared):
        sig_dict, axis_positions, scan_info = _make_galvo_signal_rust(prepared)
        return sig_dict, axis_positions, scan_info
    return self._make_signal_python(parameterDict, setupInfo)
```

```rust
// Rust-side shape, simplified
fn make_galvo_signal(input: GalvoInput) -> PyResult<GalvoOutput> {
    let fast_curve = build_smooth_fast_axis(&input);
    let slow_curve = build_step_axis(&input, fast_curve.period_samples);
    let padded = zero_pad_and_align(vec![fast_curve.samples, slow_curve.samples]);
    let scan_info = build_scan_info(&input, &padded);
    Ok(GalvoOutput { signals: padded, scan_info })
}
```

### Current Code References

- `imswitch/imcontrol/model/signaldesigners/GalvoScanDesigner.py`
  - `make_signal()` orchestrates axis selection, unit conversion, waveform
    generation, padding, and `ScanInfoContract`.
  - `__d2scan_poly()`, `__generate_smooth_multid2()`, `__init_positioning()`,
    `__final_positioning()`, and `__add_start_end()` are the smooth curve core.
  - `__generate_step_scan()`, `__repeat_dlower()`, and `__zero_padding()` handle
    non-fast axes and alignment.
- `scripts/diagnostics/test-galvoscandesigner.py` contains many scan-order
  examples useful for golden-output fixture generation.
- `imswitch/imcontrol/_test/unit/test_galvo_jerk_limit.py` documents desired
  jerk-limit behavior, but is currently skipped and should be rewritten against
  the real `ScanManagerBase.makeFullScan()` entry point before relying on it.

### Acceptance Criteria

- For selected XY, XYZ, and mock-axis fixtures, Rust and Python produce matching
  signal lengths, metadata fields, min/max voltages, and waveform values within
  a defined tolerance.
- The extension is optional: missing Rust module falls back to the current Python
  implementation without changing behavior.
- Benchmarks report waveform build time and output size for small, medium, and
  large scans.

## 2. On-the-Fly Camera Frame Compression

### Task Description

Recording compression is a stronger speed-focused Rust candidate than etSTED
controller code, but the first target should be a native batch compressor rather
than a replacement for all HDF5/TIFF/Zarr writing. The current recording path in
`imswitch/imcontrol/model/managers/RecordingManager.py` already moves disk I/O
off the acquisition thread via `WriterThread`, batches frames with
`WRITE_BATCH_FRAMES`, and writes through `HDF5Storer`, `ZarrStorer`, or
`TiffStorer`. HDF5 currently uses native `gzip` plus shuffle for disk streaming.

Rust will not automatically beat native HDF5 gzip, because gzip compression is
already implemented in C. The useful experiment is whether a Rust extension can
compress frame batches faster with a low-latency codec such as LZ4 or Zstd at a
low compression level, while releasing the GIL and avoiding extra copies. The
pilot should first benchmark a standalone function:
`uint16/float32 frames -> compressed byte chunks + metadata`. Integration can
then follow as either a new experimental save format, a Zarr-compatible chunk
codec path, or a sidecar chunk stream used only for throughput testing.

This target directly measures acquisition pressure: how quickly the writer can
drain the queue, how much disk bandwidth is saved, and whether frame acquisition
stalls when compression is enabled.

### Pseudo Code

```python
# WriterThread remains the owner of I/O and batching.
def _flush_batch(self, detectorName):
    batch = np.concatenate(self._batches[detectorName], axis=0)
    if self._storer.supports_native_chunks:
        chunk = rust_compress_frames(
            batch,
            codec="lz4",
            dtype=str(batch.dtype),
            shape=batch.shape,
        )
        self._storer.writeCompressedChunk(detectorName, chunk)
    else:
        self._storer.writeFrames(detectorName, batch)
```

```rust
fn compress_frames(
    py: Python,
    frames: PyReadonlyArrayDyn<u8>,
    codec: Codec,
    dtype: String,
    shape: Vec<usize>,
) -> PyResult<CompressedChunk> {
    py.allow_threads(|| {
        let bytes = frames.as_slice()?;
        let compressed = codec.compress(bytes)?;
        Ok(CompressedChunk { dtype, shape, compressed })
    })
}
```

### Current Code References

- `imswitch/imcontrol/model/managers/RecordingManager.py`
  - `WRITER_QUEUE_MAXSIZE` and `WRITE_BATCH_FRAMES` define producer/consumer
    pressure and batch size.
  - `WriterThread.run()`, `_flush_batch()`, `enqueue_frames()`, `finish()`, and
    `abort()` own the off-thread recording pipeline.
  - `HDF5Storer._createDetectorGroup()` enables `compression=self.compression`
    and `shuffle=True` for disk streaming.
  - `HDF5Storer.writeFrames()` appends batches to an extendable dataset.
  - `ZarrStorer.writeFrames()` and `TiffStorer.writeFrames()` are comparison
    points for non-HDF5 behavior.
- `imswitch/imcontrol/_test/unit/test_recording.py`
  - Existing tests cover compression being enabled, writer order preservation,
    backpressure behavior, abort behavior, and dtype preservation.

### Acceptance Criteria

- Benchmark current HDF5 gzip, HDF5 lzf or no compression, and Rust LZ4/Zstd on
  the same frame stacks.
- Report input throughput in MB/s, compressed output size, CPU time, and writer
  queue stall behavior.
- Preserve frame order, dtype, shape, and abort semantics.
- Keep existing HDF5/TIFF/Zarr writers unchanged until the Rust chunk path shows
  a clear throughput or latency advantage.
