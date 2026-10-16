"""COMET drift correction: LocalizationResult -> drift-corrected result.

Runs the optional ``comet-smlm`` package (all-pairs, GPU-optional drift
estimation); the result type is the one ``smlm-drift`` produces.
"""

from .processor import (
    INSTALL_HINT,
    TARGET_SIGMA_NM,
    CometParamWidget,
    SmlmCometDriftProcessor,
    comet_installed,
    comet_problem,
    recheck_comet,
)

__all__ = [
    "INSTALL_HINT",
    "TARGET_SIGMA_NM",
    "CometParamWidget",
    "SmlmCometDriftProcessor",
    "comet_installed",
    "comet_problem",
    "recheck_comet",
]
