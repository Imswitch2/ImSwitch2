# SNOUTY deskew reconstructor (M12 Phase D.1)

**Status:** Plan
**Date:** 2026-05-29
**Scope:** Port the `DeskewProcessorGPU` / `DeskewProcessorCPU` pair from
[Mini_Recon](https://github.com/khoj00/Mini_Recon) into an ImProcess
`Reconstructor` plugin under
`imswitch/improcess/reconstructors/snouty/`.
**Relation to roadmap:** First member of Milestone 12 Phase D
(per-modality plugins). Specifically the "Lightsheet (SNOUTY): deskew /
deconvolution hooks" bullet.

---

## 1. Audit of the upstream code

### What the class is

A geometric deskew operator for tilted light-sheet data
(SNOUTY / OPM / MS-RESOLFT). Input: a 3D stack
`(planes, cam_y, cam_x)` from an obliquely-illuminated scan. Output: a
properly oriented 3D volume `(sample_z, sample_y, sample_x)` in voxel
space.

### Algorithm

1. Build a 3×3 affine `M` from the optical geometry
   `(c_px, alpha_deg, dy, sample_vx_size)`.
2. Transpose the stack to `(cam_y, planes, cam_x)`, subtract
   `camera_offset`, clip ≥0.
3. Scatter-add into a flat float32 canvas of size `Sz·Sy·Sx`; in
   parallel scatter a `ones` array to keep per-voxel counts.
4. Divide the data canvas by the count canvas (proper normalisation),
   apply a separable Gaussian smoother whose sigmas come from the
   geometry itself (skipped when all sigmas < 0.5 vx — dense-scan
   regime).
5. Three call paths: `process_stack` (full volume),
   `process_projections` (xy/xz/yz max-projections, no smoothing),
   `process_frame` (single plane).

### Engineering quality

* Pure CuPy on the GPU path, with a byte-identical CPU twin
  (`DeskewProcessorCPU`) using `np.bincount` +
  `scipy.ndimage.gaussian_filter`. Same constructor, same method
  signatures — already a CPU/GPU pair.
* Scatter indices computed once per `(data_shape, out_shape)` pair and
  cached — reused for both the data scatter and the counts scatter, and
  across timepoints in a timelapse.
* No Qt, no UI, no global state, no print/log side effects in the core
  class. Constructor takes a plain `dict`.
* Picked at runtime by `_build_snouty_processor(params)` from
  `cupy_available`.

### Parameters

| Key                | Unit | Meaning |
|--------------------|------|---------|
| `c_px`             | nm   | camera pixel size in sample space |
| `alpha_deg`        | deg  | light-sheet tilt |
| `dy`               | nm   | scan step between successive planes (post-restack) |
| `sample_vx_size`   | nm   | output voxel size |
| `camera_offset`    | ADU  | dark-current subtraction |
| `flip_data`        | bool | reverse plane order |
| `cycles`           | int  | number of MS-RESOLFT cycles (slow axis groups) |
| `planes_in_cycle`  | int  | planes acquired per cycle |
| `restack`          | bool | de-interlace before deskew |

Mini_Recon's defaults (used when metadata is missing): `c_px=100 nm`,
`alpha_deg=35°`, `dy=210 nm`, `sample_vx_size=200 nm`,
`camera_offset=100 ADU`, `flip_data=False`, `cycles=1`,
`planes_in_cycle=1`, `restack=True`.

### Metadata it reads from ImSwitch HDF5 attrs

| HDF5 attr | Param | Conversion |
|---|---|---|
| `Detector:<cam>:Camera pixel size` or `Detector:<cam>:Pixel size` | `c_px` | µm → nm (×1000) |
| `MS-RESOLFT_Scan:cycleStepSizeUm` | `dy` | µm → nm (×1000) |
| `MS-RESOLFT_Scan:cycleSteps` | `cycles` | int |
| `MS-RESOLFT_Scan:roSteps` | `planes_in_cycle` | int |
| `ScanStage:positive_direction` | `flip_data` | bool |

ImSwitch2 emits these keys today, so auto-detection will work end-to-end.
The widget still exposes them so the user can override.

---

## 2. Plan: `improcess/reconstructors/snouty/`

### Folder structure to add

```
imswitch/improcess/reconstructors/snouty/
├── __init__.py
├── reconstructor.py          ← SnoutyReconstructor(Reconstructor)
├── deskew_cpu.py             ← vendored from Mini_Recon, renamed file
├── deskew_gpu.py             ← vendored, optional import
├── restack.py                ← _restack_interleaved helper
├── metadata.py               ← ImSwitch-HDF5 attrs → params dict mapping
├── params_widget.py          ← Qt parameter widget
└── result.py                 ← SnoutyResult(ProcessingResult)
```

### What gets vendored vs re-derived

**Vendor verbatim (with copyright headers added):**

* `Mini_Recon/core/DeskewProcessorCPU.py` → `snouty/deskew_cpu.py`
* `Mini_Recon/core/DeskewProcessorGPU.py` → `snouty/deskew_gpu.py`
* `_restack_interleaved` from `Mini_Recon/core/core_processors.py`
  → `snouty/restack.py`

These are pure, single-file, no internal Mini_Recon imports — zero
rewrite needed. License-wise both projects share authorship, so no
friction.

**Re-derive in ImProcess style:**

* **`metadata.py`** — adapts ImSwitch's existing `DataObj.attrs` dict
  (which already carries the keys above) to the deskew param dict.
  Replaces Mini_Recon's `_snouty_params_from_metadata`, but reading
  from `data_obj.attrs` instead of reopening the file.
* **`params_widget.py`** — Qt widget exposing the nine knobs with
  metadata-derived defaults pre-filled. Same `get_values() -> dict`
  contract as `MonalisaParamsWidget`. Includes a `Device` dropdown
  (`CPU` / `GPU`) and a button that re-runs the metadata adapter
  against the current `DataObj`.
* **`result.py`** — `SnoutyResult(ProcessingResult)` wrapping the
  output volume.
  * Single timepoint: `data` shape `(Sz, Sy, Sx)`, `axis_labels =
    ["Z", "Y", "X"]`.
  * Multi-timepoint: `data` shape `(T, Sz, Sy, Sx)`, `axis_labels =
    ["T", "Z", "Y", "X"]`.
  * Three `view_modes` matching Mini_Recon's projection convention
    (XY = identity, XZ = swap Z/Y, YZ = swap X/Z; in the multi-T case
    the time axis is preserved at position 0 in each).
  * `save(path, fmt)` writes ImageJ TIFF for `fmt="tiff"`, and for
    multi-T saves one HDF5 with `t000`, `t001`, … datasets matching
    Mini_Recon's convention.
* **`reconstructor.py`** — `SnoutyReconstructor(Reconstructor)` with:
  * `id = "snouty"`, `name = "SNOUTY deskew"`,
    `file_extensions = ["hdf5", "h5", "tiff"]`.
  * `process(data_obj, params)`:
    1. Read `data_obj.data`.
    2. Optional `restack._restack_interleaved`.
    3. Pick CPU vs GPU processor based on `params["device"]` and cupy
       availability; raise a clear error if `device="GPU"` but cupy is
       absent (matching the MoNaLISA plugin's pattern).
    4. **Timelapse handling**: if `params["n_timepoints"] > 1`, run the
       processor once per timepoint (the scatter-index cache makes
       this near-free for the second-and-later timepoints since
       per-frame shape is identical), accumulate results into a 4D
       array `(T, Sz, Sy, Sx)`. Memory-budget tradeoff: process one
       timepoint at a time, then concatenate at the end. (Mini_Recon
       streams to disk; we hold in RAM and let `save()` write — the
       canonical post-processing path. If memory becomes an issue,
       switch to streaming via a temporary zarr later.)
    5. Wrap in `SnoutyResult`.
  * `make_param_widget` returns `SnoutyParamsWidget`, initialised from
    `metadata.snouty_params_from_attrs(data_obj.attrs)` when a
    `DataObj` is already selected.
  * `make_metadata_dialog` → `None` (geometry is in the param widget).
  * `make_overlay` → `None` (no pattern overlay).

### Decisions from review (2026-05-29)

1. **`cycles` / `planes_in_cycle`** — ImSwitch2 emits these. Defaults
   stay, widget exposes them, no special handling needed.
2. **Projection mode** — take the cleaner route: a separate plugin
   `snouty-projections` that calls `process_projections`. Result is
   `data=np.stack([xy, xz, yz])` with
   `axis_labels=["projection", "Y", "X"]`. Same param widget, different
   `process()` body. (Phase D.3.)
3. **Test fixture** — Mini_Recon's `Test_data/` is too large to vendor.
   Tests use a **synthetic stack** built in-test (a few hundred KB of
   numpy noise + a known set of bright voxels) so the regression has
   no on-disk dependency and runs in CI in <1 s.
4. **Timelapse support — required for Phase D.1**, not deferred. Single
   reconstructor handles both single- and multi-timepoint inputs based
   on the `n_timepoints` param (auto-detected from HDF5 attrs where
   available, user-overrideable in the widget). Memory model:
   accumulate in RAM; if a real dataset breaks that assumption we
   revisit with streaming-to-zarr.

### Phased delivery

* **D.1 — CPU Snouty plugin with timelapse.** Lands the folder above
  using only `deskew_cpu`. GPU import is guarded; selecting `device=GPU`
  raises a clear error in D.1. Synthetic regression test included.
* **D.2 — GPU path.** Add `deskew_gpu` behind `try: import cupy`.
  Selecting `device=GPU` then works when CuPy is installed.
* **D.3 — Projection-only sibling plugin** (`snouty-projections`).
  Same widget, different `process()`, returns the 3-projection stack.

D.2 and D.3 are independent and can each run as their own agent task
after D.1 is in.

### Wiring into the registry

Two lines in `imswitch/improcess/reconstructors/__init__.py`:

```python
from .snouty import SnoutyReconstructor
# in available_plugins:
'snouty': SnoutyReconstructor,
```

Users opt in via the existing `processing:` config block:

```json
"processing": {
  "reconstructors": ["snouty", "view-only"],
  "processors":     ["drift-correct"]
}
```

### Why this fits the contracts cleanly

* `ProcessingResult` is shape-agnostic — `(Sz, Sy, Sx)` and
  `(T, Sz, Sy, Sx)` are both fine.
* `view_modes` already supports orthogonal projections via axis
  permutations.
* The param widget is plugin-owned; nothing in the main view needs to
  change.
* The metadata adapter sits inside `process()` — no controller
  changes.
* Phase D plugins do **not** depend on Phase B.2 (controller rewire)
  landing first, because they only need to exist in the registry.
  The legacy Reconstruct button continues to drive MoNaLISA; this
  plugin is discoverable via `registry.reconstructors()` for any new
  call sites we add (e.g. the picker that ships with B.2).

---

## 3. Out of scope for D.1

* Multi-color alignment (Mini_Recon's `core.multicolor` path).
* Saving via a non-TIFF/HDF5 format (zarr later).
* GPU path (D.2).
* Projection-only sibling (D.3).
* Streaming-to-disk timelapse mode (revisit if RAM becomes a problem).
* Hooking SNOUTY datasets into M10's live pipeline (Phase E in the
  main M12 doc).
