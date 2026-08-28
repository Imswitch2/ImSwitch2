"""
Analytically defined phase patterns (gratings, lenses, etc.).
"""
import numpy as np
from ..engine.parameters import (
    EditorKind,
    METRIC_UNIT,
    ParamDisplayLevel,
    ParamSpec,
    PeriodDisplacementConverter,
    SLM_UNIT,
)
from ..engine.registry import register_pattern
from ..engine.section import SectionContext


_LINEAR_PHASE_STEP_BY_UNIT = {
    SLM_UNIT:1,
    METRIC_UNIT:0.01,
}
_LINEAR_PHASE_DECIMALS_BY_UNIT = {
    SLM_UNIT:0,
    METRIC_UNIT:3,
}


### binary grating ###
@register_pattern(
    "binary_grating",
    params={
        "period_x":ParamSpec(
            0,int,step=1,editor=EditorKind.SPIN_BOX,
        ),
        "period_y":ParamSpec(
            0,int,step=1,editor=EditorKind.SPIN_BOX,
        ),
        "phase_offset":ParamSpec(
            0,float,step=0.1,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
        "duty_x":ParamSpec(
            0.5,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
        "duty_y":ParamSpec(
            0.5,float,min_value=0.0,max_value=1.0,step=0.05,decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def binary_grating(
    context: SectionContext, 
    period_x: int, 
    period_y: int,
    phase_offset: int= 0,
    duty_x: float = 0.5,
    duty_y: float = 0.5
    ):
    """
    1D/2D binary phase grating.
    Produces 0/π phase stripes along X and/or Y directions.
    """
    height,width = context.shape
    x = np.arange(width)
    y = np.arange(height)

    if period_x > 0:
        pattern_x = ((x % period_x) < (period_x * duty_x)).astype(float)
    else:
        pattern_x = np.ones(width)

    if period_y > 0:
        pattern_y = ((y % period_y) < (period_y * duty_y)).astype(float)
    else:
        pattern_y = np.ones(height)

    phase = (pattern_y[:, None] * pattern_x[None, :]) * np.pi
    phase += phase_offset
    return np.exp(1j * phase)

### sinusoidal grating ###
@register_pattern(
    "sinusoidal_grating",
    params={
        "period_x":ParamSpec(
            0,int,step=1,editor=EditorKind.SPIN_BOX,
        ),
        "period_y":ParamSpec(
            0,int,step=1,editor=EditorKind.SPIN_BOX,
        ),
        "power_x":ParamSpec(
            1,int,step=1,editor=EditorKind.SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
        "power_y":ParamSpec(
            1,int,step=1,editor=EditorKind.SPIN_BOX,
            display_level=ParamDisplayLevel.ADVANCED,
        ),
    },
)
def sinusoidal_grating(
    context: SectionContext, 
    period_x: int, 
    period_y: int,
    power_x: int = 1,
    power_y: int = 1
    ):
    """
    1D/2D sinusoidal phase grating.
    """
    height,width = context.shape
    x = np.arange(width)
    y = np.arange(height)
    X, Y = np.meshgrid(x, y)


    if period_x == 0 and period_y == 0:
        phase = np.zeros(shape=(height,width))
        return np.exp(1j*phase)
    
    phase = np.ones(shape=(height,width))
    if period_x != 0:
        phase *= np.sin(2 * np.pi * X / period_x) ** power_x

    if period_y != 0:
        phase *= np.sin(2 * np.pi * Y / period_y) ** power_y

    return np.exp(1j*phase)


@register_pattern(
    "linear_phase",
    params={
        "period_x":ParamSpec(
            0,int,
            step_by_unit=_LINEAR_PHASE_STEP_BY_UNIT,
            decimals_by_unit=_LINEAR_PHASE_DECIMALS_BY_UNIT,
            converter=PeriodDisplacementConverter(axis="x"),
            editor=EditorKind.SPIN_BOX,
            converted_label = "Displacement X (um)",
        ),
        "period_y":ParamSpec(
            0,int,
            step_by_unit=_LINEAR_PHASE_STEP_BY_UNIT,
            decimals_by_unit=_LINEAR_PHASE_DECIMALS_BY_UNIT,
            converter=PeriodDisplacementConverter(axis="y"),
            editor=EditorKind.SPIN_BOX,
            converted_label = "Displacement Y (um)",
        ),
    },
)
def linear_phase(context: SectionContext, period_x: int, period_y: int):
    """
    Linear (blazed) phase ramp.
    Periods define the number of pixels for a 2π phase ramp.
    """
    height,width = context.shape
    x = np.arange(width)
    y = np.arange(height)
    X, Y = np.meshgrid(x, y)
    
    phase = np.zeros(shape=(height,width))
    if period_x!=0:
        phase += 2 * np.pi * (X / period_x)
    if period_y != 0:
        phase += 2 * np.pi * (Y / period_y)

    phase = np.mod(phase, 2 * np.pi)
    return np.exp(1j * phase)
