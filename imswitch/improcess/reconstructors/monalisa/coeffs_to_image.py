"""
MoNaLISA coefficient-to-image reassignment logic.

Extracted from ReconObj.coeffsToImage (previously in improcess/model/ReconObj.py).
This is specific to MoNaLISA SIM pattern scanning and should not be part of a generic
processing result base class.
"""

from dataclasses import dataclass

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    iter_recorded_coordinates,
)

_logger = initLogger('MonalisaCoeffsToImage')


@dataclass(frozen=True)
class LayoutPlacement:
    """Where every stored frame's coefficient block belongs in the output.

    Built from the resolved acquisition layout, so serpentine traversal,
    detector gating and line-step interleaving are already resolved into
    logical coordinates and this module never re-derives them from the frame
    count.
    """

    slots: tuple[tuple[int, int, int, int], ...]
    """``(t, z, y, x)`` output coordinate per stored frame, in storage order."""

    timepoints: int
    slices: int
    rows: int
    cols: int
    condition_labels: tuple[str, ...]
    n_conditions: int
    n_time: int

    @property
    def folds_condition_into_time(self) -> bool:
        return self.n_conditions > 1


def _loops_by_kind(layout: AcquisitionLayout) -> dict:
    return {loop.kind: loop for loop in layout.event_loops}


def linestep_conditions_interleave_per_line(layout: AcquisitionLayout | None) -> bool:
    """True when every line-step condition repeats each fast-axis line.

    The fast-Gauss paths (offline and live) consume one interleaved stack per
    timepoint -- ``[line 0 / condition 0][line 0 / condition 1][line 1 / ...``
    -- and de-interleave it by ``n_linesteps``. That is the chronology the
    Advanced producer and its legacy adapter both record: the ``condition``
    loop sits immediately outside ``scan_x``. A layout with no condition loop
    trivially qualifies. Any other placement of the condition loop -- one
    complete image per condition, say -- is a frame order those paths cannot
    reassemble and must be declined rather than reshaped.
    """
    if layout is None:
        return False
    kinds = [loop.kind for loop in layout.event_loops]
    if 'condition' not in kinds:
        return True
    if 'scan_x' not in kinds:
        return False
    fast = kinds.index('scan_x')
    return fast > 0 and kinds[fast - 1] == 'condition'


def placement_from_layout(layout: AcquisitionLayout | None) -> LayoutPlacement | None:
    """Resolve output coordinates for every stored frame, or ``None``.

    ``None`` means the layout does not describe a MoNaLISA-style scan and the
    caller must fall back to its scan-parameter arithmetic.
    """
    if layout is None or layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return None
    loops = _loops_by_kind(layout)
    fast, slow = loops.get('scan_x'), loops.get('scan_y')
    if fast is None or slow is None:
        return None

    depth = loops.get('scan_z')
    condition = loops.get('condition')
    time = loops.get('time')
    n_conditions = condition.count if condition is not None else 1
    n_time = time.count if time is not None else 1

    # A negative physical direction means the logical index runs against the
    # stage, so flip it to keep the reconstructed image in physical order.
    flip = {
        loop.id: loop
        for loop in layout.event_loops
        if loop.direction == -1
    }

    def coordinate(coordinates, loop):
        if loop is None:
            return 0
        value = coordinates[loop.id]
        return loop.count - 1 - value if loop.id in flip else value

    slots = []
    for coordinates in iter_recorded_coordinates(layout):
        condition_index = coordinate(coordinates, condition)
        time_index = coordinate(coordinates, time)
        slots.append(
            (
                # Conditions of one timepoint stay adjacent, so the compatibility
                # T axis reads as time-major.
                time_index * n_conditions + condition_index,
                coordinate(coordinates, depth),
                coordinate(coordinates, slow),
                coordinate(coordinates, fast),
            )
        )

    labels = tuple(condition.labels) if condition is not None and condition.labels else ()
    if condition is not None and not labels:
        labels = tuple(f'condition_{index}' for index in range(condition.count))
    return LayoutPlacement(
        slots=tuple(slots),
        timepoints=n_time * n_conditions,
        slices=depth.count if depth is not None else 1,
        rows=slow.count,
        cols=fast.count,
        condition_labels=labels,
        n_conditions=n_conditions,
        n_time=n_time,
    )


def coeffs_to_image_from_placement(
    coeffs: np.ndarray, placement: LayoutPlacement
) -> np.ndarray:
    """Reassemble one base using recorded coordinates instead of frame order.

    Each scan position contributes a ``gridRows x gridCols`` block of foci that
    is interleaved at a stride equal to the scan-step count, exactly as the
    scan-parameter path does; only the source of the coordinates differs.
    """
    if coeffs.shape[0] != len(placement.slots):
        raise ValueError(
            f'Coefficient frame count ({coeffs.shape[0]}) does not match the '
            f'{len(placement.slots)} frames the acquisition layout records'
        )
    image = np.zeros(
        [
            placement.timepoints,
            placement.slices,
            placement.rows * coeffs.shape[1],
            placement.cols * coeffs.shape[2],
        ],
        dtype=np.float32,
    )
    for index, (t, z, y, x) in enumerate(placement.slots):
        image[t, z, y::placement.rows, x::placement.cols] = coeffs[index]
    return image


def coeffs_to_image(coeffs: np.ndarray, scan_params: dict, axis_labels: dict[str, str]) -> np.ndarray:
    """
    Reshape one base's worth of MoNaLISA coefficients into an image.

    Caller responsibility: ``SignalExtractor.extractSignal`` returns a 4D array
    ``(numBases, numFrames, gridRows, gridCols)``.  Callers must iterate the
    leading Base axis and pass each 3D slice to this function separately —
    the function intentionally handles one base at a time.

    Args:
        coeffs: 3D coefficient array for a single base
                ``(numFrames, gridRows, gridCols)``.
        scan_params: Scan metadata dict with keys:
            - 'dimensions': list of 4 dimension names in scan order (e.g., ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'])
            - 'directions': list of 3 direction strings ('pos' or 'neg')
            - 'steps': list of 4 step counts as strings
            - 'n_linesteps': optional number of conditions acquired per scan
              line. Values greater than one mean the recorded frame order is
              ``line 0 / condition 0, line 0 / condition 1, ...`` rather than
              one complete image per condition.
            - 'unidirectional': bool (True for snake scan, False for raster)
        axis_labels: Dict mapping semantic names to dimension names, e.g.:
            - 'r_l_text': 'Right-Left'
            - 'u_d_text': 'Up-Down'
            - 'b_f_text': 'Back-Front'
            - 'timepoints_text': 'Timepoints'
            - 'p_text': 'pos'
            - 'n_text': 'neg'
    
    Returns:
        4D array (timepoints, slices, rows, cols) where each pixel is the reconstructed value
        from the corresponding scan position
    """
    frames = coeffs.shape[0]
    dim0Side = int(scan_params['steps'][0])
    dim1Side = int(scan_params['steps'][1])
    dim2Side = int(scan_params['steps'][2])
    dim3Side = int(scan_params['steps'][3])  # Output T (timepoints/conditions)
    num_linesteps = int(scan_params.get('n_linesteps', 1))
    if num_linesteps < 1:
        raise ValueError('n_linesteps must be at least 1')
    if dim3Side % num_linesteps != 0:
        raise ValueError(
            f'Time/condition steps ({dim3Side}) must be divisible by '
            f'n_linesteps ({num_linesteps})'
        )
    
    if frames != dim0Side * dim1Side * dim2Side * dim3Side:
        _logger.error(f'Wrong dimensional data: {frames} frames != {dim0Side}*{dim1Side}*{dim2Side}*{dim3Side}')
        raise ValueError(f'Coefficient frame count ({frames}) does not match scan dimensions')
    
    timepoints = int(
        scan_params['steps'][scan_params['dimensions'].index(axis_labels['timepoints_text'])]
    )
    slices = int(scan_params['steps'][scan_params['dimensions'].index(axis_labels['b_f_text'])])
    sqRows = int(scan_params['steps'][scan_params['dimensions'].index(axis_labels['u_d_text'])])
    sqCols = int(scan_params['steps'][scan_params['dimensions'].index(axis_labels['r_l_text'])])
    
    im = np.zeros(
        [timepoints, slices, sqRows * coeffs.shape[1], sqCols * coeffs.shape[2]],
        dtype=np.float32
    )
    
    for i in range(coeffs.shape[0]):
        # Advanced scans repeat every physical line once per condition.  The
        # line-step axis therefore sits between the fast and middle spatial
        # axes in the recorded stream:
        #   [line0/A][line0/B][line1/A][line1/B] ...
        # Keep the public result shape unchanged by folding real timepoints and
        # line-step conditions into the output T axis.
        fast = i % dim0Side
        expanded_line = i // dim0Side
        linestep = expanded_line % num_linesteps
        mid = (expanded_line // num_linesteps) % dim1Side
        slow = (expanded_line // (num_linesteps * dim1Side)) % dim2Side
        acquisition = expanded_line // (num_linesteps * dim1Side * dim2Side)
        t = acquisition * num_linesteps + linestep
        
        # Bidirectional scan handling (snake pattern)
        if not scan_params['unidirectional']:
            # Each repeated line is a separate fast-axis traversal, so its
            # line-step index participates in the alternating direction.
            oddMidStep = np.mod(mid * num_linesteps + linestep, 2)
            fast = (1 - oddMidStep) * fast + oddMidStep * (dim0Side - 1 - fast)
        
        # Direction handling (positive vs negative)
        neg = (int(scan_params['directions'][0] == axis_labels['n_text']),
               int(scan_params['directions'][1] == axis_labels['n_text']),
               int(scan_params['directions'][2] == axis_labels['n_text']))
        
        fast = (1 - neg[0]) * fast + neg[0] * (dim0Side - 1 - fast)
        mid = (1 - neg[1]) * mid + neg[1] * (dim1Side - 1 - mid)
        slow = (1 - neg[2]) * slow + neg[2] * (dim2Side - 1 - slow)
        
        # Place dimensions in correct row/col/slice
        if scan_params['dimensions'][0] == axis_labels['r_l_text']:
            if scan_params['dimensions'][1] == axis_labels['u_d_text']:
                c = fast
                pc = dim0Side
                r = mid
                pr = dim1Side
                s = slow
            else:
                c = fast
                pc = dim0Side
                r = slow
                pr = dim2Side
                s = mid
        elif scan_params['dimensions'][0] == axis_labels['u_d_text']:
            if scan_params['dimensions'][1] == axis_labels['r_l_text']:
                c = mid
                pc = dim1Side
                r = fast
                pr = dim0Side
                s = slow
            else:
                c = slow
                pc = dim2Side
                r = fast
                pr = dim0Side
                s = mid
        else:
            if scan_params['dimensions'][1] == axis_labels['r_l_text']:
                c = mid
                pc = dim1Side
                r = slow
                pr = dim2Side
                s = fast
            else:
                c = slow
                pc = dim2Side
                r = mid
                pr = dim1Side
                s = fast
        
        # Add grid of coefficients at the computed position
        im[t, s, r::pr, c::pc] = coeffs[i]

    return im


def reconstruct_images_from_coeffs(
    coeffs: np.ndarray,
    scan_params: dict,
    axis_labels: dict[str, str],
    placement: LayoutPlacement | None = None,
) -> np.ndarray:
    """Reassemble a full 6D MoNaLISA image stack from per-base coefficients.

    Args:
        coeffs: 5D array ``(Dataset, Base, numFrames, gridRows, gridCols)``.
            ``SignalExtractor.extractSignal`` returns the per-dataset 4D
            ``(Base, numFrames, gridRows, gridCols)``; stack those along a new
            leading Dataset axis before calling this.
        scan_params: Scan metadata dict (see :func:`coeffs_to_image`).
        axis_labels: Semantic-name → dimension-name map (see
            :func:`coeffs_to_image`).

    Returns:
        6D array ``(Dataset, Base, T, Z, Y, X)``.
    """
    datasets = coeffs.shape[0]
    bases = coeffs.shape[1]
    if placement is not None:
        return np.array(
            [
                [
                    coeffs_to_image_from_placement(coeffs[ds, b], placement)
                    for b in range(bases)
                ]
                for ds in range(datasets)
            ]
        )
    return np.array(
        [
            [
                coeffs_to_image(coeffs[ds, b], scan_params, axis_labels)
                for b in range(bases)
            ]
            for ds in range(datasets)
        ]
    )


def output_pixel_size_nm(
    scan_params: dict,
    axis_labels: dict[str, str],
) -> tuple[float, float] | None:
    """Compute the reconstructed ``(y_nm, x_nm)`` pixel pitch.

    The reconstructed pitch *is* the scan step size. :func:`coeffs_to_image`
    lays the output out as ``(sqRows * gridRows, sqCols * gridCols)`` pixels,
    interleaving each scan position's ``gridRows x gridCols`` focus block at a
    stride equal to the scan-step count (``im[r::pr, c::pc] = coeffs[i]`` with
    ``pr = sqRows``). Walking one pixel along an axis therefore advances the
    scan position by exactly one step, and the ``gridRows`` foci tile adjacent
    illumination periods (period = ``sqRows * step_size``) seamlessly — so the
    physical pitch is ``step_size`` everywhere, independent of the focus count.
    Returns ``None`` if the scan params don't carry the needed step sizes.
    """
    try:
        ud_index = scan_params['dimensions'].index(axis_labels['u_d_text'])
        rl_index = scan_params['dimensions'].index(axis_labels['r_l_text'])
        step_y_nm = float(scan_params['step_sizes'][ud_index])
        step_x_nm = float(scan_params['step_sizes'][rl_index])
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    return (step_y_nm, step_x_nm)


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
