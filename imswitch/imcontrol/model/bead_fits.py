"""Back-compat shim: bead-fit models moved to ``imcommon.algorithms``.

The parametric bead-image fit models are hardware-agnostic numpy code, so they
now live in :mod:`imswitch.imcommon.algorithms.bead_fits` where ImProcess (which
must not import from imcontrol) can reuse them. This module re-exports them so
existing ``imswitch.imcontrol.model.bead_fits`` imports keep working.
"""

from imswitch.imcommon.algorithms.bead_fits import (  # noqa: F401
    FIT_MODELS,
    DonutR2Gaussian,
    FitModel,
    FitResult,
    Gaussian2D,
    Sine2D,
)

__all__ = [
    "FIT_MODELS",
    "DonutR2Gaussian",
    "FitModel",
    "FitResult",
    "Gaussian2D",
    "Sine2D",
]
