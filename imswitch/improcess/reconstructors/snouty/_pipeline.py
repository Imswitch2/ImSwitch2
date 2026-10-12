"""Shared deskew pipeline scaffolding for the SNOUTY reconstructors.

The full-volume (:mod:`...snouty`) and projections-only
(:mod:`...snouty_projections`) plugins share an identical front half: load the
raw stack, optionally de-interlace it (MS-RESOLFT), pick a CPU/GPU processor,
then split the stack into timepoints and run a per-timepoint loop with GPU
memory cleanup between iterations. Only the inner per-timepoint operation and
the result type differ. This module hosts that shared scaffolding so each
reconstructor's ``process()`` stays small.
"""

from typing import TYPE_CHECKING, Callable, List

import numpy as np

from .deskew_cpu import DeskewProcessorCPU
from .metadata import recorded_snouty_geometry
from .restack import restack_interleaved

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


def _validate_geometry(params: dict) -> None:
    """Reject non-positive or nonsensical deskew geometry parameters.

    ``c_px`` (camera pixel size), ``dy`` (scan step), and ``sample_vx_size``
    (output voxel size) must be positive; ``sample_vx_size`` is also a divisor
    in the transform. ``alpha_deg`` (lightsheet tilt) must lie strictly between
    0 and 90 degrees, otherwise the deskew shear collapses or inverts.
    """
    for key in ('c_px', 'dy', 'sample_vx_size'):
        value = params[key]
        if not np.isfinite(value) or value <= 0:
            raise ValueError(
                f"SNOUTY geometry parameter {key!r} must be a positive number, "
                f"got {value!r}"
            )

    alpha = params['alpha_deg']
    if not np.isfinite(alpha) or not (0 < alpha < 90):
        raise ValueError(
            f"SNOUTY geometry parameter 'alpha_deg' must be between 0 and 90 "
            f"degrees (exclusive), got {alpha!r}"
        )


def load_restack_deskew_timelapse(
    data_obj: 'DataObj',
    params: dict,
    *,
    per_timepoint_fn: Callable,
    logger=None,
) -> List:
    """Load, restack, select a deskew processor, and run the per-timepoint loop.

    Args:
        data_obj: DataObj containing raw scan data (3D: planes, cam_y, cam_x).
        params: Parameter dict (see SnoutyParamsWidget.get_values()).
        per_timepoint_fn: Callable invoked once per timepoint as
            ``per_timepoint_fn(processor, tp_stack, use_gpu, cp)`` and returning
            that timepoint's output array. ``cp`` is the CuPy module when the GPU
            path is active, otherwise ``None``.
        logger: Optional logger for progress messages.

    Returns:
        A list with one entry per timepoint (the per_timepoint_fn return values).
        Callers stack the list along axis 0 for multi-timepoint (4D) output or
        use the single element directly for single-timepoint (3D) output.
    """
    # Open the source. Data already in memory is used as is. Otherwise a
    # source with a lazy path (HDF5, zarr) is read one timepoint at a time,
    # so a timelapse larger than RAM reconstructs; only a source without one
    # is decoded whole.
    preloaded = data_obj.dataLoaded
    stack = None
    handle = None
    try:
        if preloaded:
            stack = data_obj.data
        else:
            data_obj.checkAndOpenData()
            handle = data_obj.data_handle
            if handle is None or not data_obj.sourceHasLazyPath():
                data_obj.checkAndLoadData()
                stack = data_obj.data
                handle = None
        shape = tuple(stack.shape) if stack is not None else tuple(handle.shape)

        # Validate data shape
        if len(shape) != 3:
            raise ValueError(
                f'Expected 3D data (planes, cam_y, cam_x), got shape {shape}'
            )

        cycles = params.get('cycles', 1)
        planes_in_cycle = params.get('planes_in_cycle', 1)
        n_timepoints = params.get('n_timepoints', 1)
        recorded = recorded_snouty_geometry(data_obj)
        if recorded is not None:
            cycles, planes_in_cycle, recorded_timepoints = recorded
            if recorded_timepoints is not None:
                n_timepoints = recorded_timepoints
            if logger is not None:
                logger.info(
                    f'Using the recorded acquisition layout: {cycles} cycles x '
                    f'{planes_in_cycle} planes/cycle x {n_timepoints} timepoint(s)'
                )

        restack = bool(params.get('restack', True)) and (cycles > 1 or planes_in_cycle > 1)
        if restack and logger is not None:
            logger.info(
                f'Restacking {cycles} cycles × {planes_in_cycle} planes/cycle '
                f'in each of {n_timepoints} timepoint(s)'
            )

        return _run_timepoints(
            stack, handle, shape, params, n_timepoints, cycles, planes_in_cycle,
            restack, per_timepoint_fn, logger,
        )
    finally:
        if not preloaded:
            data_obj.checkAndUnloadData()


def _run_timepoints(
    stack, handle, shape, params, n_timepoints, cycles, planes_in_cycle,
    restack, per_timepoint_fn, logger,
) -> List:
    # Pick processor (CPU or GPU)
    device = params.get('device', 'CPU').upper()
    use_gpu = False
    cp = None
    if device == 'GPU':
        try:
            from .deskew_gpu import DeskewProcessorGPU, cupy_available
            if not cupy_available():
                raise RuntimeError(
                    "CuPy not available. Install with: pip install cupy-cuda12x"
                )
            import cupy as cp
            processor_class = DeskewProcessorGPU
            use_gpu = True
            if logger is not None:
                logger.info('GPU deskew enabled (CuPy detected)')
        except (ImportError, RuntimeError) as e:
            raise RuntimeError(
                f'GPU deskew unavailable: {e}. Use device="CPU" or install CuPy.'
            ) from e
    else:
        processor_class = DeskewProcessorCPU

    # Validate geometry before constructing the processor so a bad value fails
    # with a clear message instead of an opaque downstream error (e.g. division
    # by zero on a non-positive voxel size, or an empty/degenerate output canvas).
    _validate_geometry(params)

    # Build processor config (subset of params that constructor expects)
    processor_params = {
        'c_px': params['c_px'],
        'alpha_deg': params['alpha_deg'],
        'dy': params['dy'],
        'sample_vx_size': params['sample_vx_size'],
        'camera_offset': params.get('camera_offset', 0.0),
        'flip_data': params.get('flip_data', False),
    }
    processor = processor_class(processor_params)

    # Split into timepoints along axis 0 and process each. np.array_split
    # accepts an uneven division and hands back timepoints of different
    # depths, which deskews into silently inconsistent volumes.
    if n_timepoints < 1:
        raise ValueError(f'Timepoints must be at least 1, got {n_timepoints}')
    n_frames = int(shape[0])
    if n_frames % n_timepoints:
        raise ValueError(
            f'SNOUTY needs an equal number of planes per timepoint: '
            f'{n_frames} frames does not divide into {n_timepoints} '
            f'timepoint(s)'
        )
    if logger is not None:
        logger.info(f'Processing {n_timepoints} timepoint(s) with {device} deskew...')
    planes_per_timepoint = n_frames // n_timepoints
    outputs = []
    for t in range(n_timepoints):
        if n_timepoints > 1 and logger is not None:
            logger.info(f'  Timepoint {t+1}/{n_timepoints}...')
        start = t * planes_per_timepoint
        stop = start + planes_per_timepoint
        if stack is not None:
            tp_stack = stack[start:stop]
        else:
            tp_stack = np.asarray(handle[start:stop])
        if restack:
            # Every timepoint is restacked on its own, which is what
            # restack_interleaved does for a whole stream anyway.
            tp_stack = restack_interleaved(
                tp_stack, cycles, planes_in_cycle, timepoints=1
            )
        outputs.append(per_timepoint_fn(processor, tp_stack, use_gpu, cp))
        del tp_stack
        # Free GPU memory between timepoints
        if use_gpu:
            try:
                cp.get_default_memory_pool().free_all_blocks()
            except Exception:
                pass  # graceful degradation if memory management fails
    return outputs


def stack_timepoints(volumes: List[np.ndarray]) -> np.ndarray:
    """One volume as is, several stacked on a new leading axis.

    Releases each per-timepoint array as it is copied, so a long timelapse
    peaks at the result plus one timepoint rather than twice the result.
    """
    if len(volumes) == 1:
        return np.asarray(volumes[0])
    first = np.asarray(volumes[0])
    result = np.empty((len(volumes),) + first.shape, dtype=first.dtype)
    for index in range(len(volumes)):
        result[index] = volumes[index]
        volumes[index] = None
    return result


def display_levels(data: np.ndarray, max_samples: int = 4_000_000) -> tuple:
    """``(1st, 99.9th)`` percentile display range from an evenly strided sample.

    A percentile over a multi-gigabyte timelapse sorts a copy of it; a few
    million strided voxels give the same levels for the viewer.
    """
    data = np.asarray(data)
    stride = int(np.ceil((data.size / max_samples) ** (1.0 / max(data.ndim, 1))))
    stride = max(1, stride)
    sample = data[(slice(None, None, stride),) * data.ndim]
    return float(np.percentile(sample, 1)), float(np.percentile(sample, 99.9))


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
