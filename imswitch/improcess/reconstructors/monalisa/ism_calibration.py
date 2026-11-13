"""One-shot ISM shift calibration from raw camera frames, without fitting.

Keep this independent of reconstruction/preset orchestration. The caller supplies
an already selected localization and a *resolved* scan orientation; the recorded
acquisition layout determines which frames form an in-plane scan. The shift
measurement itself is exactly the one used by Auto ISM in the reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .calibration import PatternLocalization
from .pipeline import complete_step
from .placement import OutputRaster, lock_step_to_lattice
from .reassignment import DEFAULT_OFFSETS, ShiftFactor, measure_shift_factor
from .scan_frame import orient_recorded_index, orient_step_columns, raster_scan_index


@dataclass(frozen=True)
class ISMShiftEstimate:
    factor: ShiftFactor
    orientation: str
    scanned_axes: tuple[str, ...]
    stack_number: int
    num_stacks: int


def _scan_geometry(frames, attrs, resolved_layout, params, orientation):
    """Use the same scan-source precedence as the lattice reconstructor."""
    from imswitch.improcess.reconstructors.monalisa_lattice.scan import (
        layout_scan, recorded_scan,
    )

    num_frames = frames.shape[0]
    steps_fast = int(params.get("scan_steps_fast", 0))
    steps_slow = int(params.get("scan_steps_slow", 0))
    typed_nm = float(params.get("scan_step_nm", 0.0))
    pixel_nm = float(params.get("pixel_size_nm", 0.0))

    layout = (
        layout_scan(resolved_layout, num_frames)
        if steps_fast <= 0 and steps_slow <= 0 else None
    )
    if layout is not None:
        step_nm = tuple(
            typed_nm if typed_nm > 0 and n > 1 else recorded
            for n, recorded in zip(layout.counts, layout.step_nm)
        )
        step = None
        aspect = None
        if pixel_nm > 0:
            if not any(v is not None and v > 0 for v in step_nm):
                raise ValueError("Camera pixel size is known, but scan step is missing")
            step = np.full((2, 2), np.nan, dtype=float)
            for axis, value in enumerate(step_nm):
                if value is not None and value > 0:
                    step[:, axis] = 0.0
                    step[axis, axis] = value / pixel_nm
        elif all(value is not None and value > 0 for value in step_nm):
            aspect = float(step_nm[0] / step_nm[1])
        return layout, step, aspect

    raster = recorded_scan(attrs, num_frames, steps_fast, steps_slow, typed_nm)
    step = None
    if pixel_nm > 0:
        if raster.step_nm is None:
            raise ValueError("Camera pixel size is known, but scan step is missing")
        step = np.diag([raster.step_nm / pixel_nm] * 2)
    return raster, step, None


def _sampled_stack(frames, scan, localization):
    """Select the in-plane stack with most central-pixel modulation."""
    rows, cols = frames.shape[1:]
    # A positive ``margin`` on points_in_frame() *extends* the bounds; it
    # does not exclude edge foci.  Use the same detector-offset reach as
    # measure_shift_factor() before directly indexing raw camera pixels.
    x, y = localization.points_in_frame(rows, cols)
    cx, cy = np.rint(x).astype(int), np.rint(y).astype(int)
    reach = max(max(abs(dx), abs(dy)) for dx, dy in DEFAULT_OFFSETS)
    safe = (
        (cx - reach >= 0) & (cx + reach < cols)
        & (cy - reach >= 0) & (cy + reach < rows)
    )
    cx, cy = cx[safe], cy[safe]
    if cx.size == 0:
        raise ValueError(
            "Localization has no foci with the required detector-offset "
            "margin inside the camera frame"
        )
    if hasattr(scan, "frames_of"):
        indexes = [scan.frames_of(i) for i in range(scan.num_stacks)]
    else:
        n = scan.frames_per_stack
        indexes = [np.arange(i * n, (i + 1) * n) for i in range(scan.num_stacks)]
    scores = []
    for indices in indexes:
        if len(indices) < 2:
            scores.append(-np.inf)
            continue
        samples = np.asarray(frames[indices[:, None], cy[None, :], cx[None, :]], dtype=np.float64)
        scores.append(float(np.mean(samples.var(axis=0))))
    selected = int(np.argmax(scores))
    return selected, indexes[selected]


def estimate_ism_shift(
    frames: np.ndarray,
    attrs: dict,
    resolved_layout,
    params: dict,
    localization: PatternLocalization,
    orientation: str,
) -> ISMShiftEstimate:
    """Return the raw-camera ISM factor; never run Gaussian extraction.

    Auto orientation must be resolved separately, rather than silently assuming
    ``+x+y``. Frame layout, step completion and 2% lattice-step locking match
    the reconstruction path. This function does not update calibration state.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3 or frames.shape[0] < 2:
        raise ValueError(f"Expected a nonempty (frames, rows, cols) scan, got {frames.shape}")
    if not orientation:
        raise ValueError(
            "ISM shift estimation needs a resolved scan orientation. "
            "Choose Recorded, Setup, Locked or Manual in Scanning, "
            "or use a previously resolved Auto orientation."
        )
    from .calibration import ScanOrientation
    orientation = ScanOrientation.from_literal(orientation).to_literal()
    scan, step, aspect = _scan_geometry(frames, attrs, resolved_layout, params, orientation)
    stack_number, frame_ids = _sampled_stack(frames, scan, localization)
    stack = np.asarray(frames[frame_ids])
    if hasattr(scan, "frames_of"):
        index = orient_recorded_index(scan.index[frame_ids], orientation)
        step = orient_step_columns(step, orientation)
        if aspect is not None and orientation[1] == "y":
            aspect = 1.0 / aspect
    else:
        index = raster_scan_index(scan.num_fast, scan.num_slow, orientation)
    lattice = localization.lattice
    actual_step, _ = complete_step(step, lattice, index, aspect)
    actual_step, _ = lock_step_to_lattice(
        lattice.matrix, actual_step, 0.02, scan_index=index,
    )
    raster = OutputRaster.covering(stack.shape[1:], actual_step, lattice.offset)
    x, y = localization.points_in_frame(*stack.shape[1:], margin=3.0)
    offsets = index @ actual_step.T
    factor = measure_shift_factor(stack, x, y, offsets, raster)
    scanned = tuple("xy"[axis] for axis in (0, 1) if np.unique(index[:, axis]).size > 1)
    return ISMShiftEstimate(
        factor=factor, orientation=orientation, scanned_axes=scanned,
        stack_number=stack_number, num_stacks=int(scan.num_stacks),
    )
