# ImProcess GPU backend (pyclesperanto)

Status: **Proposed — revision 1, 2026-10-10, for Lenny's review.** Nothing
implemented. One branch, `feat/gpu-backend`, carries this plan and every phase.

## Motivation

ImProcess's pixel processors run on scipy and numpy on the CPU. On a 3D stack a
Gaussian blur takes hundreds of milliseconds and a median filter on one
4-megapixel frame takes over a second, so a chain of steps over a time series
is a coffee break. [pyclesperanto](https://github.com/clEsperanto/pyclesperanto)
(BSD-3) runs the same classical operations on any GPU through OpenCL, CUDA or
Metal — including the Apple GPU in the laptop this plan was measured on, which
no CUDA-only library reaches.

Measured on 2026-10-10, Apple M2 (OpenCL 1.2, 10 compute units, 5.4 GB
visible), pyclesperanto 0.22.0, scipy 1.17, best of three, float32, transfers
included in the GPU time:

| operation | array | scipy / numpy | pyclesperanto | ratio |
|---|---|---|---|---|
| Gaussian σ=2, 3D | 64×512×512 | 365 ms | 69 ms | 5.3× |
| Gaussian σ=2, 2D | 2048² | 60 ms | 13 ms | 4.6× |
| median, 5×5 box | 2048² | 1234 ms | 28 ms | 44× |
| mean, 5×5 box | 2048² | 40 ms | 6 ms | 7× |
| max projection along Z | 200×512×512 | 6 ms | 18 ms | 0.3× |
| connected-component labels, 3D | 64×512×512 | 67 ms | 69 ms | 1× |
| transfer to GPU and back | 64 MB | 7 ms | | |

Two readings. Neighbourhood filters gain a lot, the median most of all.
Reductions and labelling do not: a max projection is memory-bound and the
transfer costs more than numpy's pass. So this is not "move everything to the
GPU"; it is a backend for the operations that benefit, chosen per operation on
evidence, with the CPU path kept as the reference.

## What this is and is not

**It is an accelerator of operations ImProcess already defines.** A step still
records `filter` with `method=median, radius=2`. The backend changes where the
arithmetic ran, never what operation was asked for. A result from the GPU must
equal the CPU result within a stated, tested tolerance for that operation, or
the operation is not eligible.

**It is not a new processor.** No generic "run any of pyclesperanto's 369
functions" step in this effort; that would be a second vocabulary beside the
processors, and it would not carry `param_spec`, kinds, ROI restriction or the
contract tests. If wanted later it is a separate plan.

**It is not a hard dependency.** pyclesperanto is optional, installed by a
`gpu` extra, imported lazily. Without it, or without a usable device, every
processor runs exactly as today and says so once in the log.

## Facts about pyclesperanto that shape the design

Verified on 2026-10-10.

- **Versions and backends.** 0.22.0 (April 2026) added CUDA and Metal beside
  OpenCL; 0.24.0 (August 2026) split the compute backends into extras
  (`pyclesperanto[opencl|cuda|metal]`). The OpenCL backend remains the default
  and the only one on every platform.
- **0.24.0 does not load on macOS 15.7.** Its `macosx_11_0_arm64` wheels (both
  OpenCL and Metal) reference a libc++ symbol (`std::__hash_memory`) this
  macOS does not have, so `import pyclesperanto` warns "No backend installed".
  0.22.0 loads and runs. No upstream issue reports this yet. The extra pins
  `pyclesperanto>=0.22,!=0.24.0` on macOS until a fixed release exists, and the
  startup probe reports a backend that fails to load as "installed but
  unusable", naming the error, rather than as "not installed".
- **Border handling is clamp-to-edge**, scipy's `mode="nearest"`. ImProcess's
  filters use scipy's default `reflect`. Measured: with the CPU side in
  `nearest` mode, the median box is bit-identical to scipy and Gaussian and mean
  agree to 3·10⁻⁷ on data in [0, 1). With `reflect` they differ by up to 0.4 at
  the borders. The design pads on the host (`np.pad(mode="symmetric")`, which
  is scipy's `reflect`) by the kernel's half-width before the transfer and
  crops after, so **existing results do not change** and equality holds to the
  edge. The pad is one extra copy; at 2048² and radius 2 it is noise.
- **Resampling conventions differ.** `cle.scale(interpolate=True)` samples a
  different grid from `ndimage.zoom` (not a pure shift; 0.37 max difference on
  [0, 1) data after excluding borders). Resize is therefore not eligible until
  it is expressed through `cle.affine_transform` with the exact `ndimage.zoom`
  mapping, and that equality is tested. It is a later phase, not phase 1.
- **Output dtype is float32** for every float operation; integer input is
  converted. `apply_filter` already casts to float32, so the filters match.
  Operations that must keep an integer dtype (labels) use the label functions,
  which return `uint32`.
- **Device memory is queryable** (`Device.info` reports global memory size and
  the maximum single buffer, 1024 MB here). Nothing is guessed about what fits.
- **Kernels compile on first use** (the first 3D Gaussian took 711 ms, the
  second 71 ms) and are cached per process.
- **Calls are synchronous and not interruptible**; a run can only be cancelled
  between calls.

## Design

### The backend seam

New module `imswitch/improcess/model/compute.py` (Qt-free):

```python
class ComputeBackend(Protocol):
    name: str                      # "cpu" | "gpu"
    def gaussian(self, data, sigma_yx, *, spatial_axes) -> np.ndarray: ...
    def mean_box(self, data, radius, *, spatial_axes) -> np.ndarray: ...
    def median_box(self, data, radius, *, spatial_axes) -> np.ndarray: ...
    # grows by one method per eligible operation, never by "run function X"

class CpuBackend:        # the scipy code moved here verbatim from the processors
class ClesperantoBackend: # pads, pushes, runs, pulls, crops; one method per op

def active_backend() -> ComputeBackend
def describe() -> ComputeDescription   # backend, device name, library version, or why cpu
```

A processor that has an eligible operation calls `active_backend().median_box(...)`
instead of `ndimage.median_filter(...)`. Its parameters, `param_spec`, widget
and tests are untouched. The CPU backend is the existing code, so with no GPU
configured every processor is byte-for-byte what it is today — the same
argument that made the drop-in plugin tables safe.

The seam is deliberately narrow: a method per operation with ImProcess's own
argument vocabulary (radius in pixels, sigma per axis, spatial axes). The GPU
backend translates to pyclesperanto's names (`radius_x`, `sigma_y`) inside. If
pyclesperanto renames something, one method changes.

Non-spatial axes (T, C, Z when filtering in-plane) are looped on the host, one
plane or one chunk per call, so a 2D filter over a stack stays a 2D filter and
the GPU buffer never exceeds the device's reported maximum buffer size.

### Choosing the backend: a per-machine preference, not a per-run parameter

**Preferences → Compute device…** in ImProcess, kept in `improcess_options.json`
beside the default folders (`compute.backend`: `"cpu"` or `"gpu"`, default
`"cpu"`; `compute.device`: a device name from the probe, or empty for
pyclesperanto's choice). The dialog lists what the probe found: backends,
devices with their memory, the library version, or the reason nothing is
usable. Opt-in, because today's results must not change without a decision.

Why not a `backend` parameter on each processor: a parameter is part of the
recorded operation and of every workflow form, and "which GPU this laptop has"
is not part of the operation. It is the same reasoning that put the default
folders in the options file rather than the widget state. Workflows stay
portable: a file written on the rig runs on the laptop.

### Provenance: the footprint records what ran

What *is* part of the record is what arithmetic produced the pixels. The run
path (`run_processor`) publishes the active backend for the duration of a run,
and `record_step` adds to every step a `compute` block:

```yaml
compute: {backend: gpu, device: "Apple M2", library: "pyclesperanto 0.22.0"}
```

or `{backend: cpu}`. It is written by the one place every output passes
through, like the rest of the footprint, so no processor can forget it. A
reviewer reading a result's provenance sees the operation, its parameters and
the device that computed it. Replay compares operations and parameters, so a
GPU result replays on a CPU-only machine; the two footprints differ in the
`compute` block and nowhere else, and the equivalence tests bound how far the
pixels differ.

### Fallback is silent in the result and loud in the log

If `compute.backend` is `gpu` but the package is missing, no backend loads, or
no device exists, ImProcess logs one warning at startup with the reason, runs
on the CPU, and records `compute: {backend: cpu, requested: gpu, reason: ...}`
on every step. A run is never refused for lack of an accelerator: unlike COMET,
the GPU here is not the method. The Compute device dialog shows the same
reason.

A GPU call that fails mid-run (out of memory, a driver error) fails that run
with pyclesperanto's message. It does not retry on the CPU behind the user's
back, because a step that silently took twice as long on a different device is
exactly the kind of thing the footprint exists to prevent; the message says to
switch the preference or reduce the chunk.

### Memory and cancellation

The memory budget that already governs processors
(`memory.processingWorkingSetMB`, see `memory-budgets.md`) governs the host
side unchanged. The device side has its own hard numbers from the probe:
`Global Memory Size` and `Maximum Buffer Size`. A call's working set is the
input chunk plus the padded copy plus the output, in float32; the chunk
(planes per call) is the largest whole number of planes whose working set fits
the maximum buffer size, derived, not configured. `checkpoint()` runs between
chunks, so Cancel lands within one chunk.

### Equivalence policy (the gate for every operation)

An operation is eligible only with a test that runs the CPU and GPU backends
on the same fixtures and holds them to one of three classes, named
`gpu-equivalence-v1` in the test module with this rationale:

| class | bound | why |
|---|---|---|
| exact | `array_equal` | order-independent integer or selection results: median box, max/min projections, labels |
| float32 rounding | `abs(diff) ≤ 4·ε₃₂·max(abs(reference))` | separable kernels sum in a different order; measured 2.5 ε₃₂ for Gaussian and mean, so 4 ε₃₂ leaves one rounding step of margin |
| not eligible | — | a different algorithm or sampling grid: rolling ball, watershed split, `ndimage.zoom` until the affine mapping is proven |

The CPU backend is always the reference. A new pyclesperanto release that moves
an operation out of its class turns CI red on the equivalence test; that is the
contract check, as the COMET tests are for `comet-smlm`.

## Phases

Each phase is one or more commits on `feat/gpu-backend`, each leaving the suite
green and the CPU behaviour byte-identical.

- **P-0 Seam and probe.** `compute.py` with `CpuBackend` holding the moved
  scipy code; `ClesperantoBackend` with the probe (`describe()`), the padding
  helper and the chunking derivation, but no operation yet; the `gpu` extra;
  the `compute` block in `record_step`; the preference in
  `improcess_options.json` and the Compute device dialog; the startup log line.
  Tests: probe reports "not installed", "installed but unusable" (the 0.24
  macOS case, simulated) and "available"; footprint carries the block; the
  fallback records `requested`/`reason`; options file round-trips unknown keys.
- **P-1 Filters.** `gaussian`, `mean_box`, `median_box` on the backend;
  `FilterProcessor` and `apply_filter` call it (unsharp derives from the
  Gaussian as today). Equivalence tests: median exact, Gaussian and mean in the
  rounding class, border included, 2D and per-plane over a 4D stack, uint16
  input, ROI `mask` and `crop` modes. This phase alone delivers the 44× median.
- **P-2 Background and segmentation.** `subtract-background`'s Gaussian mode
  and `segmentation`'s Otsu threshold (cle `threshold_otsu`; its histogram
  binning must be tested against skimage's — it is eligible only if the
  threshold value agrees exactly on the fixtures, otherwise it stays CPU).
  Rolling ball stays CPU (different algorithm).
- **P-3 Label morphology.** `erode`, `dilate`, `open`, `close` of label masks
  and `remove_small_labels`, each gated by an exact-equality test against the
  skimage path. `fill-holes` and `watershed-split` have no equivalent
  (`binary_fill_holes`, `distance_transform_edt` are absent) and stay CPU.
- **P-4 Projections.** Only if a measurement shows a gain: the table above says
  a lone max projection loses to numpy. The case to measure is a projection as
  the second step of a chain whose first step left the data on the device;
  that needs the backend to keep arrays resident between steps, which is a
  design change (results would hold device arrays) and is out of scope unless
  P-1 shows the transfer dominating real chains.
- **P-5 Resize.** `cle.affine_transform` with the explicit `ndimage.zoom`
  grid mapping for the bilinear case, gated by the rounding-class test; other
  interpolation orders stay CPU.
- **Docs and changelog** with each phase: `docs/improcess.rst` gains a "GPU
  backend" section (what is accelerated, the preference, what the footprint
  records, the macOS 0.24 note); `installation.rst` gains the extra.

## Open questions for Lenny

1. **Preference versus parameter.** The plan argues for a per-machine
   preference with the footprint recording what ran. If you would rather see
   `backend` in every workflow file, say so before P-0; it changes the
   contract tests for every touched processor.
2. **Border policy.** Host-side padding keeps today's `reflect` results
   unchanged at a copy's cost. The alternative is to declare `nearest` the
   border mode for both backends, which drops the pad but changes border
   pixels of existing CPU results (a changelog entry and a version bump on the
   filter's `params_version`). The plan takes padding.
3. **Report the 0.24 macOS wheel upstream?** It is a mis-tagged wheel; a
   report with the dlopen error would help them and us. Your call whether to
   file it.
4. **Chains on the device (P-4).** Keeping results resident on the GPU between
   steps is where the real end-to-end gain lies for projections and short
   chains, and it is also where results stop being plain arrays. Not in this
   effort unless you want it scoped now.

## Non-goals

- pyclesperanto inside reconstructors (MoNaLISA, SNOUTY, SMLM): their hot paths
  are FFTs and fits, not neighbourhood filters, and `deconvolve_fft` /
  `convolve_fft` exist but are untested here; a separate evaluation.
- Deconvolution: parked with RedLionfish.
- napari rendering: unaffected.
- CUDA-specific paths: the OpenCL backend covers NVIDIA too; CUDA is a later
  measurement, not a design branch.
