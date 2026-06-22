"""Generic time-resolved detector contracts and helpers."""

from .detector_contract import TimeResolvedDetectorMixin
from .processing import (
    aggregate_decay,
    compute_gate_images,
    intensity_from_cube,
    validate_gates,
)
from .types import (
    GateSpec,
    LifetimeFitConfig,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
    copy_time_resolved_products,
)

__all__ = [
    "GateSpec",
    "LifetimeFitConfig",
    "TimeResolvedDetectorMixin",
    "TimeResolvedScanConfig",
    "TimeResolvedScanProducts",
    "aggregate_decay",
    "compute_gate_images",
    "copy_time_resolved_products",
    "intensity_from_cube",
    "validate_gates",
]
