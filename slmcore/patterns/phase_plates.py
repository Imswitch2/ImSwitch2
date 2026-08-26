
import numpy as np
from ..engine.parameters import EditorKind,ParamDisplayLevel,ParamSpec
from ..engine.registry import register_pattern
from ..engine.section import SectionContext

### Vortex Phase (Spiral Phase Plate) ###
@register_pattern(
    "vortex",
    params={
        "charge":ParamSpec(
            1,int,step=1,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def vortex(context:SectionContext, charge: int = 1):
    """
    Vortex phase / Spiral Phase Plate.
    charge: Topological charge (integer).
            Determines how many 2π phase wraps occur around the center.
    """
    height,width = context.shape
    offset_x = context.center_offset_x_px
    offset_y = context.center_offset_y_px

    x = np.arange(width) - width/2 - offset_x
    y = np.arange(height) - height/2 - offset_y
    X, Y = np.meshgrid(x, y)

    # Calculate angle theta (-π to π)
    theta = np.arctan2(Y, X)

    # Phase = l * theta
    phase = charge * theta
    
    return np.exp(1j * phase)


### Top Hat (Circular Phase Piston) ###
@register_pattern(
    "top_hat",
    params={
        "radius_px":ParamSpec(
            100,int,min_value=0,step=1,editor=EditorKind.SPIN_BOX,
        ),
        "phase_shift":ParamSpec(
            3.14,float,step=0.1,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def top_hat(context:SectionContext, radius_px: int, phase_shift: float =3.14):
    """
    Top Hat / Circular Piston.
    Creates a circular region with a constant phase shift relative to the background.
    radius_px: Radius of the circle in pixels.
    phase_shift: The phase height of the hat in radians (default π).
    """
    offset_x = context.center_offset_x_px
    offset_y = context.center_offset_y_px
    height,width = context.shape

    x = np.arange(width) - width/2 - offset_x
    y = np.arange(height) - height/2 - offset_y
    X, Y = np.meshgrid(x, y)

    mask = (X**2 + Y**2) <= radius_px**2

    phase = np.zeros((height, width))
    phase[mask] = phase_shift

    return np.exp(1j * phase)


### Half Moon X (Vertical Split) ###
@register_pattern(
    "half_moon_x",
    params={
        "phase_shift":ParamSpec(
            3.14,float,step=0.1,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def half_moon_x(context:SectionContext, phase_shift: float =3.14):
    """
    Half Moon X (Vertical Phase Step).
    Splits the screen horizontally: 0 phase on left, `phase_shift` on right.
    Used often for Hilbert transforms or edge detection.
    """
    
    offset_x = context.center_offset_x_px
    height,width = context.shape
    
    # Create X grid centered at offset
    x = np.arange(width) - width/2 - offset_x
    
    # Create mask where X > 0 (Right side)
    mask = x > 0
    
    # Broadcast to 2D (height, width)
    phase = np.zeros((height, width))
    phase[:, mask] = phase_shift

    return np.exp(1j * phase)


### Half Moon Y (Horizontal Split) ###
@register_pattern(
    "half_moon_y",
    params={
        "phase_shift":ParamSpec(
            3.14,float,step=0.1,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def half_moon_y(context:SectionContext, phase_shift: float =3.14):
    """
    Half Moon Y (Horizontal Phase Step).
    Splits the screen vertically: 0 phase on top, `phase_shift` on bottom.
    """
    offset_y = context.center_offset_y_px
    height,width = context.shape
    
    # Create Y grid centered at offset
    y = np.arange(height) - height/2 - offset_y
    
    # Create mask where Y > 0 (Bottom side, assuming numpy coordinates increase downwards)
    mask = y > 0
    
    # Broadcast to 2D (height, width)
    phase = np.zeros((height, width))
    phase[mask, :] = phase_shift

    return np.exp(1j * phase)
