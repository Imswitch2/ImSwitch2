"""Back-compat shim: bead recognition/reconstruction moved to imcommon.algorithms.

``bead_recognition`` is hardware-agnostic numpy/scipy/skimage code (bead centre
detection, raster reconstruction, ROI means, donut analysis, model fitting), so
it now lives in :mod:`imswitch.imcommon.algorithms.bead_recognition` where
ImProcess (which must not import from imcontrol) can reuse it for an offline
BeadRec reconstructor. This module re-exports the public API so existing
``imswitch.imcontrol.model.bead_recognition`` imports keep working unchanged.
"""

from imswitch.imcommon.algorithms.bead_recognition import *  # noqa: F401,F403
from imswitch.imcommon.algorithms.bead_recognition import (  # noqa: F401
    BeadAcquisitionConfig,
    BeadAnalysisParameters,
    BeadRecResultRecord,
    BeadWorkerUpdate,
    CenterDetectionResult,
    DonutAnalysisResult,
    ReconstructionUpdate,
    RoiBounds,
    analyze_donut,
    append_roi_means,
    create_reconstruction_buffer,
    find_bead_center,
    find_center_donut,
    find_center_foci,
    fit_bead,
    mean_intensity_in_roi,
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)
