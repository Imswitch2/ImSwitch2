"""Profile fits and records — moved to ``imswitch.imcommon.algorithms.profile_fits``.

The Profile panel is shared with imcontrol's Line Profile panel, and imcontrol
may not depend on improcess, so the fits it uses live in the common layer. This
module keeps the historical import path working with the very same objects.
"""

from imswitch.imcommon.algorithms.profile_fits import (  # noqa: F401
    ExponentialFit,
    FitResult,
    GaussianFit,
    ProfileFit,
    TwoGaussianFit,
    _initial_two_peak_centers,
    _sort_two_gaussian_params,
    build_profile_record,
)

__all__ = [
    "ExponentialFit",
    "FitResult",
    "GaussianFit",
    "ProfileFit",
    "TwoGaussianFit",
    "build_profile_record",
]
