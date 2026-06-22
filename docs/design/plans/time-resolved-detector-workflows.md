# Time-Resolved Detector Workflows

Status: Implementation in progress

Implemented so far:

- Generic `imswitch.imcontrol.model.timeresolved` dataclasses, processing
  helpers, and detector contract.
- `SwabianTimeTaggerManager` side-channel products for final/live
  time-resolved scan data.
- Workflow facade support through `facade.time_resolved`.
- Scan workflow adapter through `facade.scan.run_once()`.
- Detector-neutral photon-arrival, gated-STED, and tau-STED workflows.
- HDF5/NPZ/TIFF output for workflow products.
- Default user scripts under
  `imswitch/_data/user_defaults/scripts/timeresolved/`.

Still pending:

- Hardware validation on the Swabian rig.
- Multidimensional Swabian output beyond 2D `Y, X, tcspc_bin` products.
- A second real or simulated time-tagger backend to prove backend portability.

## Goal

Add opt-in workflows for time-resolved photon-counting acquisitions while keeping
the current detector and widget defaults unchanged.

The immediate driver is `SwabianTimeTaggerManager`, which already produces
per-pixel TCSPC histograms internally. The design should also make it cheap to
add another time tagger card later by implementing the same generic contract.

Target workflows:

1. Record binned photon arrival times per scan pixel.
2. Compute time-gated STED images from configurable TCSPC windows.
3. Compute tau STED / FLIM-STED lifetime products from the same acquisition.

## Current State

Relevant files:

- `imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py`
- `imswitch/imcontrol/model/workflows/facade.py`
- `imswitch/imcontrol/model/workflows/mock_facade.py`
- `imswitch/imcontrol/model/workflows/widefield_starss.py`
- `imswitch/imcontrol/controller/controllers/FLIMHistController.py`
- `imswitch/imcontrol/view/widgets/FLIMHistWidget.py`
- `docs/devices/detectors.rst`

The Swabian manager currently:

- connects to `NidaqManager.sigScanBuilt`, `sigScanStarted`, and `sigScanDone`
  so FLIM acquisition follows the normal scan lifecycle;
- creates a `TimeTagger.Flim` measurement with virtual pixel begin/end channels
  generated from the physical line clock;
- polls `Flim.getCurrentFrame()` in `_TTFlimWorker`;
- reshapes the returned frame to `(Ny, Nx, n_bins)`;
- immediately reduces that cube to intensity, per-pixel lifetime, aggregated
  decay, and global tau;
- exposes the lifetime image as the normal detector display/chunk frame.

The important missing capability is not acquisition. The binned per-pixel photon
arrival cube already exists inside `_TTFlimWorker._poll_frame()`. It is just not
retained, exposed, or saved.

## Problems To Solve

1. The raw per-pixel TCSPC cube is local to the worker and disappears after each
   poll.
2. Workflows have no generic way to ask a detector for final time-resolved
   products.
3. Gated images are not computed from TCSPC windows.
4. Tau/lifetime products are coupled to the Swabian manager instead of a shared
   processing layer.
5. `RecordingManager` assumes a detector stream is a stack of 2D frames. It is
   not currently a good home for `(Y, X, bins)` products.
6. Swabian currently handles 2D `Nx * Ny` scans well, but does not fully preserve
   `n_linesteps` or higher scan dimensions the way APD/PMT managers do.
7. The setup template exposes `click_trigger`, `start_trigger`, and
   `line_trigger`, but the manager constructor only reads `trigger_levels`.

## Design Principles

1. Keep default LiveView, `getLatestFrame()`, and `getChunk()` behavior stable.
   Existing FLIM display and recording should still see the 2D lifetime image.
2. Put advanced outputs on an explicit side-channel, not in the normal detector
   frame stream.
3. Make workflows depend on a generic time-resolved detector contract, not on
   `SwabianTimeTaggerManager`.
4. Keep vendor managers responsible for acquisition and scan alignment.
5. Keep reusable processing, gates, fitting, and saving in shared workflow/model
   code.
6. Make all units explicit in field names: `_ns`, `_ps`, `bin`, or `counts`.
7. Make expensive products opt-in. Capturing a full TCSPC cube should never be
   default.

## Proposed Generic Contract

Add a generic time-resolved detector contract under:

`imswitch/imcontrol/model/timeresolved/`

Suggested files:

- `types.py`
- `processing.py`
- `hdf5.py`
- `detector_contract.py`

### Types

```python
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class GateSpec:
    name: str
    start_ns: float
    stop_ns: float


@dataclass(frozen=True)
class LifetimeFitConfig:
    method: str = "moment"
    min_counts_per_pixel: int = 20
    laser_rep_rate_mhz: float | None = None


@dataclass(frozen=True)
class TimeResolvedScanConfig:
    capture_cube: bool = False
    gates: tuple[GateSpec, ...] = ()
    fit: LifetimeFitConfig = field(default_factory=LifetimeFitConfig)
    include_live_products: bool = False
    max_retained_products: int = 1


@dataclass
class TimeResolvedScanProducts:
    cube_counts: np.ndarray | None
    cube_axes: tuple[str, ...]
    t_axis_ns: np.ndarray
    intensity: np.ndarray
    lifetime_ns: np.ndarray | None
    gate_images: dict[str, np.ndarray]
    decay_counts: np.ndarray
    global_tau_ns: float
    metadata: dict[str, Any]
    is_final: bool
```

### Manager Contract

Use either an ABC or a mixin. A mixin is probably easier to introduce without
disturbing the existing detector inheritance tree.

```python
class TimeResolvedDetectorMixin:
    def timeResolvedCapabilities(self) -> dict:
        raise NotImplementedError

    def configureTimeResolvedProducts(self, config: TimeResolvedScanConfig) -> None:
        raise NotImplementedError

    def waitForFinalTimeResolvedProducts(
        self,
        timeout_s: float | None = None,
    ) -> TimeResolvedScanProducts:
        raise NotImplementedError

    def getLastTimeResolvedProducts(
        self,
        *,
        copy: bool = True,
    ) -> TimeResolvedScanProducts | None:
        raise NotImplementedError

    def clearTimeResolvedProducts(self) -> None:
        raise NotImplementedError
```

Capabilities should be declarative and cheap to query:

```python
{
    "time_axis": "tcspc",
    "supports_binned_cube": True,
    "supports_raw_tags": False,
    "supports_software_gates": True,
    "supports_hardware_gates": False,
    "supports_lifetime_fit": True,
    "native_cube_axes": ("y", "x", "tcspc_bin"),
    "vendor": "Swabian Instruments",
    "model": "Time Tagger"
}
```

## Shared Processing Helpers

Move vendor-independent processing out of `SwabianTimeTaggerManager.py` over
time.

Initial helpers:

```python
def aggregate_decay(cube_counts: np.ndarray) -> np.ndarray:
    ...


def compute_gate_images(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    gates: tuple[GateSpec, ...],
) -> dict[str, np.ndarray]:
    ...


def fit_lifetime_image(
    cube_counts: np.ndarray,
    t_axis_ns: np.ndarray,
    fit: LifetimeFitConfig,
) -> tuple[np.ndarray, np.ndarray, float, dict]:
    """Return intensity, lifetime_ns, global_tau_ns, fit_metadata."""
    ...
```

The Swabian manager can initially keep its current optimized cached fitting
implementation. The shared helper can be introduced with tests first, then used
by the manager once behavior is proven equivalent.

## Swabian Implementation

`SwabianTimeTaggerManager` becomes the first implementation of
`TimeResolvedDetectorMixin`.

### Minimal Changes

1. Add state:

```python
self._tr_config = TimeResolvedScanConfig()
self._tr_last_products = None
self._tr_final_event = threading.Event()
self._tr_lock = threading.Lock()
```

2. Clear `_tr_final_event` in `initiateScan()`.
3. In `_TTFlimWorker._emit_frame()`, optionally build
   `TimeResolvedScanProducts`.
4. Retain the final product when `is_final` is true.
5. Retain live products only when `include_live_products=True`.
6. Set `_tr_final_event` after the final product is stored.
7. Preserve current `sigFrameReady` behavior for LiveView and `FLIMHistWidget`.

### Product Creation

For each worker poll:

1. `cube_counts = cube`
2. `intensity = cube.sum(axis=2)`
3. `decay_counts = aggregate_decay(cube)`
4. `gate_images = compute_gate_images(cube, t_axis_ns, config.gates)`
5. `lifetime_ns = existing lifetime output converted to ns`
6. metadata includes:
   - detector name
   - backend name
   - click/start/line channels
   - trigger levels
   - `n_bins`
   - `binwidth_ps`
   - `t0_ps`
   - `fit_method`
   - `laser_rep_rate_mhz`
   - `min_counts_per_pixel`
   - `scan_info`
   - `peak_bin`
   - `peak_time_ns`

If `capture_cube=False`, store `cube_counts=None` but still allow gates,
intensity, lifetime, and decay to be saved. Gates require access to the cube
during processing, but do not require retaining the cube.

### Fixes To Include

1. Constructor should honor direct manager properties:

```python
self._click_trigger = float(
    props.get("click_trigger", tl.get(str(self._click_ch), 0.5))
)
```

and same for `start_trigger` and `line_trigger`.

2. Align docs/comments on lifetime units. The display/chunk image currently
   contains ns after `_on_frame_ready()`, not seconds.

3. Add explicit constraints or support for `n_linesteps` and higher dimensions.
   Recommended first step: reject unsupported non-2D Swabian time-resolved
   product capture with a clear error. Recommended second step: match APD/PMT
   output conventions.

## Future Time Tagger Backends

A new time tagger manager should only need to implement the generic contract.

Examples:

- `PicoQuantTimeHarpManager`
- `PicoQuantHydraHarpManager`
- `BeckerHicklSPCManager`
- `QutoolsQuTAGManager`
- `FpgaTimeTaggerManager`

Backend options:

1. Native per-pixel histograms from the vendor API.
2. Raw tag streaming plus ImSwitch-side pixel assignment and binning.
3. Hardware gates computed on-card, with optional software gates from retained
   cube data.

The workflow layer should not care which option is used. It only consumes
`TimeResolvedScanProducts`.

## Workflow Facade

Extend `MicroscopeFacade` with a generic time-resolved facade:

```python
@dataclass
class MicroscopeFacade:
    ...
    time_resolved: Optional[TimeResolvedDetectorFacade] = None
```

Facade methods:

```python
class TimeResolvedDetectorFacade:
    def configure(self, config: TimeResolvedScanConfig) -> None:
        ...

    def wait_for_final(self, timeout_s: float | None = None) -> TimeResolvedScanProducts:
        ...

    def get_last(self, copy: bool = True) -> TimeResolvedScanProducts | None:
        ...

    def clear(self) -> None:
        ...

    def capabilities(self) -> dict:
        ...
```

`build_facade_from_master()` gains:

```python
time_resolved_detector_name: Optional[str] = None
```

If provided, the builder verifies that the detector implements the contract.
This avoids Swabian-specific workflow code.

## Workflows

Add workflows under:

`imswitch/imcontrol/model/workflows/time_resolved.py`

or split into:

- `time_resolved_photon_bins.py`
- `gated_sted.py`
- `tau_sted.py`

### Shared Parameters

```python
@dataclass
class TimeResolvedAcquisitionParams:
    capture_cube: bool = False
    gates: tuple[GateSpec, ...] = ()
    fit: LifetimeFitConfig = field(default_factory=LifetimeFitConfig)
    save_hdf5: bool = True
    measurements_root: Path | str | None = None
    measurement_name_addition: str = ""
    final_timeout_s: float = 60.0
```

### Photon Bins Workflow

Purpose: record binned photon arrival histograms per pixel.

Sequence:

1. Configure `facade.time_resolved` with `capture_cube=True`.
2. Trigger a normal scan through the existing scan path.
3. Wait for final products.
4. Save HDF5.
5. Return products and output path.

This should be the lowest-level workflow and the first implementation target.

### Gated STED Workflow

Purpose: compute one or more time-gated STED images.

Example gates:

```python
gates = (
    GateSpec("early", 0.5, 2.5),
    GateSpec("late", 2.5, 8.0),
)
```

Sequence:

1. Configure gates.
2. Run STED scan using existing laser/scan setup.
3. Wait for products.
4. Save `/gates/<name>` datasets.
5. Optionally save the cube.

The STED laser timing remains controlled by the scan/TTL configuration. The
workflow computes detection gates from photon arrival times.

### Tau STED Workflow

Purpose: compute lifetime/tau products for a STED scan.

Sequence:

1. Configure fit method and optional cube capture.
2. Run STED scan.
3. Wait for products.
4. Save intensity, lifetime image, global decay, global tau, fit metadata.

This workflow should use the same lifetime fit config as the generic contract.
It should not call Swabian-specific fit methods directly.

## HDF5 Schema

Use workflow-owned HDF5 output at first. Do not extend `RecordingManager` until
the generic product contract is stable.

Suggested layout:

```text
<file>.h5
  attrs/
    imswitch_version
    created_unix_s
    workflow_name
    backend
    detector_name

  scan/
    metadata attrs copied from scan_info_dict

  time_resolved/
    t_axis_ns                  (B,)
    decay_counts               (B,)
    intensity                  (Y, X) or (..., Y, X)
    lifetime_ns                (Y, X) or (..., Y, X), optional
    cube_counts                (Y, X, B) or (..., Y, X, B), optional
      attrs: axes = ["y", "x", "tcspc_bin"]

  gates/
    <gate_name>                (Y, X) or (..., Y, X)
      attrs:
        start_ns
        stop_ns

  fit/
    attrs:
      method
      min_counts_per_pixel
      laser_rep_rate_mhz
      peak_bin
      peak_time_ns
      global_tau_ns
```

Compression:

- Use gzip or lzf for `cube_counts`.
- Use integer dtype for counts when possible.
- Keep `lifetime_ns` as `float32`.

## Example Script

Add an example under:

`imswitch/_data/user_defaults/scripts/timeresolved/`

Example:

```python
from imswitch.imcontrol.model.workflows import (
    GateSpec,
    TimeResolvedPhotonBinsParams,
    TimeResolvedPhotonBinsWorkflow,
)

facade = api.imcontrol.buildWorkflowFacade(
    time_resolved_detector_name="FLIM",
)

params = TimeResolvedPhotonBinsParams(
    capture_cube=True,
    gates=(
        GateSpec("early", 0.5, 2.5),
        GateSpec("late", 2.5, 8.0),
    ),
)

workflow = TimeResolvedPhotonBinsWorkflow(facade, params)
result = workflow.run()
print(result.output_path)
```

The exact scan trigger call still needs to match the selected scan surface. The
first version can support the same `scanWorkflow` path used by existing
recording workflows.

## Tests

### Unit Tests

Add tests under `imswitch/imcontrol/_test/unit/`.

1. `test_time_resolved_processing.py`
   - gate bin selection with exact edge behavior
   - aggregate decay
   - lifetime fit helper sanity checks
   - invalid gate names / reversed windows

2. `test_swabian_time_resolved_contract.py`
   - configure products
   - final event is set when final products arrive
   - `capture_cube=False` does not retain cube
   - gates are computed from cube
   - trigger direct properties are honored

3. `test_time_resolved_workflows.py`
   - mock facade returns canned products
   - photon-bin workflow saves expected HDF5 layout
   - gated STED workflow saves gate datasets
   - tau STED workflow saves lifetime and fit metadata

4. `test_microscope_facade.py`
   - `build_mock_facade()` includes a mock time-resolved detector
   - real facade builder rejects detectors without the contract when
     `time_resolved_detector_name` is provided

### Hardware Verification

Manual checks on the Swabian rig:

1. Existing FLIM live view still works without workflow product capture.
2. Existing `FLIMHistWidget` lifetime distribution and decay modes still work.
3. Photon-bin workflow saves a non-empty `(Y, X, bins)` cube.
4. Sum over `cube_counts` equals saved intensity within dtype/casting limits.
5. Gate images equal the sum of selected bins.
6. Late-gate STED image changes as expected when gate limits move.
7. Tau STED lifetime image remains stable across repeated scans of a known dye.

## Phasing

### Phase 0 - Contract And Processing

- Add generic dataclasses.
- Add processing helpers.
- Add pure unit tests.
- No Swabian behavior change yet.

### Phase 1 - Swabian Side-Channel

- Implement `TimeResolvedDetectorMixin` on `SwabianTimeTaggerManager`.
- Retain final products only when configured.
- Add final-product wait event.
- Fix direct trigger property initialization.
- Keep normal detector frames unchanged.

### Phase 2 - Facade And Mock

- Add `TimeResolvedDetectorFacade`.
- Add `time_resolved` field to `MicroscopeFacade`.
- Add `time_resolved_detector_name` to `build_facade_from_master()`.
- Add mock time-resolved detector and tests.

### Phase 3 - Photon Bins Workflow

- Add photon-bin workflow and params.
- Add HDF5 writer.
- Add example script.
- Add tests for output schema.

### Phase 4 - Gated STED Workflow

- Add gate-oriented workflow wrapper.
- Save gate images and gate metadata.
- Optionally save cube.
- Add tests with synthetic cubes.

### Phase 5 - Tau STED Workflow

- Add tau/lifetime-oriented workflow wrapper.
- Save lifetime, intensity, decay, fit metadata, and optional cube.
- Add tests.

### Phase 6 - Multidimensional Scans

- Decide and implement generic axes for `n_linesteps`, Z stacks, and other
  outer dimensions.
- Align Swabian output conventions with APD/PMT where practical.
- Update HDF5 axes metadata and tests.

### Phase 7 - Future Backends

- Implement a second mock or real backend using the same contract.
- Verify workflows require no changes.
- Move any Swabian-specific processing assumptions into backend metadata.

## Open Questions

1. Should gates be edge-inclusive on the start and edge-exclusive on the stop?
   Recommendation: `start_ns <= t < stop_ns`.
2. Should `cube_counts` be retained as `uint16`, `uint32`, or the vendor dtype?
   Recommendation: keep vendor dtype unless overflow risk is known; HDF5 attrs
   should state count dtype and any cast.
3. Should lifetime fitting eventually be entirely shared, or should managers
   remain allowed to provide optimized vendor-specific fits?
   Recommendation: shared API, backend may override implementation.
4. How should workflows trigger scans from scripts without depending on a
   widget controller?
   Recommendation: first support existing `scanWorkflow` facade path, then add a
   scan facade if a clean model-only scan runner emerges.
5. Should raw tag streaming be part of this first contract?
   Recommendation: no. Add it later as a separate optional capability.

## Non-Goals

- Do not replace `FLIMHistWidget`.
- Do not change default detector output from 2D lifetime image to a 3D cube.
- Do not require the Recording widget to understand TCSPC cubes in the first
  implementation.
- Do not make photon-bin recording default.
- Do not add vendor-specific workflows named after Swabian.

## Expected User-Facing Outcome

A script or workflow can opt into advanced time-resolved products:

```python
facade.time_resolved.configure(
    TimeResolvedScanConfig(
        capture_cube=True,
        gates=(GateSpec("late", 3.0, 8.0),),
    )
)
```

and then receive the same product object regardless of whether the detector is a
Swabian Time Tagger or a future supported time tagger card.
