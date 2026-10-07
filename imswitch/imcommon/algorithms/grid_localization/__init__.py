"""Reusable 2-D lattice localization backend.

The numerical implementation mirrors the proven SLMcore localization core so
ImControl uLenses and transform calibration use the same crop, detection,
finite-topology, stagger, affine-refinement and FFT-fallback semantics without
introducing a runtime dependency on slmcore.
"""

from .api import localize_lattice, localize_spots
from .detection import detect_spots
from .lattice import infer_lattice_shape, make_lattice_model, rectangular_lattice_indices
from .model import DetectedSpots, LatticeModel, LatticeRegistration, LatticeRegistrationOptions, SpotDetectionOptions
from .registration import register_lattice
from .parameters import LOCALIZATION_DEFAULTS, LOCALIZATION_PARAMETER_INFO

__all__ = [
    "DetectedSpots", "LatticeModel", "LatticeRegistration",
    "LatticeRegistrationOptions", "SpotDetectionOptions",
    "detect_spots", "infer_lattice_shape", "localize_lattice",
    "localize_spots", "make_lattice_model", "rectangular_lattice_indices",
    "register_lattice", "LOCALIZATION_DEFAULTS", "LOCALIZATION_PARAMETER_INFO",
]
