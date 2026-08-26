

import numpy as np
from ..engine.registry import register_pattern
from ..engine.parameters import EditorKind,ParamSpec
from ..engine.section import SectionContext

### lens phase (spherical wavefront) ###
@register_pattern(
    "lens_phase",
    params={
        "focal_mm":ParamSpec(
            225,float,step=1.0,decimals=1,
            editor=EditorKind.DOUBLE_SPIN_BOX,
        ),
    },
)
def lens_phase(context:SectionContext, focal_mm: int):
    """
    Lens phase (spherical wavefront) of a given focal length `focal_mm`.
    """
    # Convert all units to meters
    f = focal_mm * 1e-3

    wavelength = context.wavelength_nm * 1e-9
    px = context.pixel_size_um * 1e-6

    height, width = context.shape
    offset_x = context.center_offset_x_px
    offset_y = context.center_offset_y_px

    x = (np.arange(width) - width/2 - offset_x) * px
    y = (np.arange(height) - height/2 - offset_y) * px
    X, Y = np.meshgrid(x, y)
    r2 = X**2 + Y**2

    wavefront = np.sqrt(f**2 - r2) - f
    wavefront = np.abs(wavefront) + 1e-16

    k = 2 * np.pi / wavelength
    phase = -k * wavefront

    return np.exp(1j * phase)
