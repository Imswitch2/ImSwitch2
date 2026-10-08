"""Generic time-resolved detector contracts and helpers."""

from .detector_contract import TimeResolvedDetectorMixin
from .processing import (
    PILEUP_RED,
    PILEUP_WARN,
    TCSPC_DIRECTIONS,
    aggregate_decay,
    background_per_bin,
    compute_gate_images,
    estimate_prepulse_background,
    intensity_from_cube,
    orient_cube,
    pileup_fraction,
    resolve_gates,
    roll_to_peak,
    subtract_background,
    validate_gates,
)
from .types import (
    GATE_REFERENCES,
    GateSpec,
    LifetimeFitConfig,
    LiveProducts,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
    copy_time_resolved_products,
)

__all__ = [
    "GATE_REFERENCES",
    "GateSpec",
    "LifetimeFitConfig",
    "LiveProducts",
    "PILEUP_RED",
    "PILEUP_WARN",
    "TCSPC_DIRECTIONS",
    "TimeResolvedDetectorMixin",
    "TimeResolvedScanConfig",
    "TimeResolvedScanProducts",
    "aggregate_decay",
    "background_per_bin",
    "compute_gate_images",
    "copy_time_resolved_products",
    "estimate_prepulse_background",
    "intensity_from_cube",
    "orient_cube",
    "pileup_fraction",
    "resolve_gates",
    "roll_to_peak",
    "subtract_background",
    "validate_gates",
]
