"""
MoNaLISA coefficient-to-image reassignment logic.

Extracted from ReconObj.coeffsToImage (previously in improcess/model/ReconObj.py).
This is specific to MoNaLISA SIM pattern scanning and should not be part of a generic
processing result base class.
"""

import numpy as np

from imswitch.imcommon.model import initLogger

_logger = initLogger('MonalisaCoeffsToImage')


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
    dim3Side = int(scan_params['steps'][3])  # Always timepoints
    
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
        t = int(np.floor(i / (frames / dim3Side)))
        
        slow = int(np.mod(i, frames / timepoints) / (dim0Side * dim1Side))
        mid = int(np.mod(i, dim0Side * dim1Side) / dim0Side)
        fast = np.mod(i, dim0Side)
        
        # Bidirectional scan handling (snake pattern)
        if not scan_params['unidirectional']:
            oddMidStep = np.mod(mid, 2)
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
