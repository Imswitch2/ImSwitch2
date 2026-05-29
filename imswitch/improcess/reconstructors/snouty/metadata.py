"""
ImSwitch HDF5 metadata → SNOUTY deskew parameters.

Extracts geometry and scan parameters from DataObj.attrs (the HDF5 attributes
dict) and returns a dict consumable by DeskewProcessor constructors.
"""

import re
from typing import Any


# Defaults from Mini_Recon
DEFAULT_PARAMS = {
    "c_px": 100.0,              # nm
    "alpha_deg": 35.0,          # degrees
    "dy": 210.0,                # nm
    "sample_vx_size": 200.0,    # nm
    "camera_offset": 100.0,     # ADU
    "flip_data": False,
    "cycles": 1,
    "planes_in_cycle": 1,
    "restack": True,
}


def snouty_params_from_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """
    Extract SNOUTY deskew parameters from ImSwitch HDF5 attributes.
    
    Args:
        attrs: DataObj.attrs dict (HDF5 root attributes)
    
    Returns:
        Parameter dict with keys: c_px, alpha_deg, dy, sample_vx_size,
        camera_offset, flip_data, cycles, planes_in_cycle, restack.
        Missing keys are filled with Mini_Recon defaults.
    
    Examples:
        >>> attrs = {"Detector:Cam:Camera pixel size": 0.108}
        >>> snouty_params_from_attrs(attrs)
        {'c_px': 108.0, 'alpha_deg': 35.0, ...}
    """
    params = DEFAULT_PARAMS.copy()
    
    # Camera pixel size: search for "Detector:*:Camera pixel size" or "Detector:*:Pixel size"
    # Convert µm → nm (×1000)
    c_px_key_pattern = re.compile(r"^Detector:[^:]+:(Camera pixel size|Pixel size)$", re.IGNORECASE)
    for key, value in attrs.items():
        if c_px_key_pattern.match(key):
            try:
                params["c_px"] = float(value) * 1000.0  # µm → nm
                break
            except (TypeError, ValueError):
                pass
    
    # Scan step (dy): MS-RESOLFT_Scan:cycleStepSizeUm
    # Convert µm → nm (×1000)
    dy_val = attrs.get("MS-RESOLFT_Scan:cycleStepSizeUm")
    if dy_val is not None:
        try:
            params["dy"] = float(dy_val) * 1000.0
        except (TypeError, ValueError):
            pass
    
    # MS-RESOLFT cycles
    cycles_val = attrs.get("MS-RESOLFT_Scan:cycleSteps")
    if cycles_val is not None:
        try:
            params["cycles"] = int(cycles_val)
        except (TypeError, ValueError):
            pass
    
    # Planes in cycle
    planes_val = attrs.get("MS-RESOLFT_Scan:roSteps")
    if planes_val is not None:
        try:
            params["planes_in_cycle"] = int(planes_val)
        except (TypeError, ValueError):
            pass
    
    # Flip data: ScanStage:positive_direction
    flip_val = attrs.get("ScanStage:positive_direction")
    if flip_val is not None:
        try:
            # Convert scalar or array to bool
            import numpy as np
            params["flip_data"] = bool(np.asarray(flip_val).flat[0])
        except (TypeError, ValueError, IndexError):
            pass
    
    return params


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
