"""
GPU-based deskew processor stub (arrives in Phase D.2).

The import is guarded by try/except in the reconstructor, so missing CuPy
won't break imports. Instantiating this class raises a clear error until D.2.
"""

try:
    import cupy as cp
    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False


class DeskewProcessorGPU:
    """GPU deskew processor (stub for Phase D.1)."""
    
    def __init__(self, args: dict) -> None:
        if not CUPY_AVAILABLE:
            raise RuntimeError(
                "GPU deskew requires CuPy. Install with: pip install cupy-cuda12x"
            )
        raise RuntimeError(
            "SNOUTY GPU deskew arrives in Phase D.2. Use device='CPU' for now."
        )
    
    def process_stack(self, stack):
        raise NotImplementedError("GPU deskew not yet implemented")
    
    def process_projections(self, stack):
        raise NotImplementedError("GPU deskew not yet implemented")
    
    def process_frame(self, frame):
        raise NotImplementedError("GPU deskew not yet implemented")


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
