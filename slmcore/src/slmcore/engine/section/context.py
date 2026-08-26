from __future__ import annotations


from dataclasses import dataclass

from copy import deepcopy
from .geometry import SectionGeometry
from ...calibration import SLMSectionCalibration

@dataclass(frozen=True)
class SectionContext:
    """
    Detached snapshot of the runtime inputs required to compute one SLM section.

    A context is created from the current runtime and section state, but must not
    retain mutable references to them. In particular, ``calibration`` is copied
    during construction, so later runtime calibration changes do not affect an
    existing context.

    SectionContext can therefore be safely passed to delayed, threaded, or
    otherwise asynchronous computations.
    """

    geometry: SectionGeometry
    pixel_size_um: float
    wavelength_nm: int
    pupil_radius_px: int
    center_offset_x_px: int
    center_offset_y_px: int
    calibration: SLMSectionCalibration | None = None

    def __post_init__(self) -> None:
        if self.pixel_size_um <= 0:
            raise ValueError("pixel_size_um must be > 0")
       
        if self.calibration is not None:
             # store copy to prevent accidental mutation
            object.__setattr__(
                self,"calibration",self.calibration.copy()
            )

    @property
    def shape(self) -> tuple[int, int]:
        return self.geometry.shape