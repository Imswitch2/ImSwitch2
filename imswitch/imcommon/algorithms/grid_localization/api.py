"""Public convenience API for reusable spot and lattice localization."""

from __future__ import annotations


import numpy as np

from .detection import detect_spots
from .lattice import (
    infer_lattice_shape,
    make_lattice_model,
    rectangular_lattice_indices,
)
from .registration import register_lattice
from .model import (
    DetectedSpots,
    LatticeRegistration,
    LatticeRegistrationOptions,
    SpotDetectionOptions,
)


def localize_spots(
    image: np.ndarray,
    *,
    detection_options: SpotDetectionOptions=SpotDetectionOptions(),
    expected_period_px: float | tuple[float, float] | None=None,
    crop_coord=None,
) -> DetectedSpots:
    """Return an unordered set of localized spots from one image."""
    return detect_spots(
        image,detection_options,
        expected_period_px=expected_period_px,
        crop_coord=crop_coord,
    )


def localize_lattice(
    image: np.ndarray,
    lattice_indices: np.ndarray | None,
    *,
    stagger: float | None=None,
    basis_offsets=None,
    detection_options: SpotDetectionOptions=SpotDetectionOptions(),
    registration_options: LatticeRegistrationOptions=LatticeRegistrationOptions(),
    previous: LatticeRegistration | None=None,
    crop_coord=None,
    initial_linear=None,
    initial_translation=None,
) -> LatticeRegistration:
    """Detect spots and robustly register a finite 2D lattice.

    Pass ``lattice_indices=None`` to infer the finite X/Y point counts from the
    image before stagger/affine registration.
    """


    if previous is not None and crop_coord is None:
        crop_coord = previous.detections.crop_coord

    # --------------------------------------------------------------
    # 1. Spot detection
    # --------------------------------------------------------------

    detections = detect_spots(
        image,
        detection_options,
        expected_period_px=registration_options.expected_period_px,
        crop_coord=crop_coord,
    )


    # --------------------------------------------------------------
    # 2. Lattice model
    # --------------------------------------------------------------

    size_inference = None
    if lattice_indices is None:
        (count_x,count_y),size_inference = infer_lattice_shape(detections)
        lattice_indices = rectangular_lattice_indices(count_x,count_y)

    model = make_lattice_model(
        lattice_indices,
        stagger=stagger,
        basis_offsets=basis_offsets,
    )
    grid = np.asarray(model.lattice_indices)
    count_x = int(np.unique(grid[0]).size)
    count_y = int(np.unique(grid[1]).size)
    merged_diagnostics = dict(model.diagnostics)
    merged_diagnostics.update({
        "lattice_count_x":count_x,
        "lattice_count_y":count_y,
        "lattice_size_source":"image" if size_inference is not None else "provided",
        "lattice_size_inference":size_inference,
    })
    model = type(model)(
        lattice_indices=model.lattice_indices,
        logical_positions=model.logical_positions,
        representation=model.representation,
        diagnostics=merged_diagnostics,
    )


    # --------------------------------------------------------------
    # 3. Registration
    # --------------------------------------------------------------

    result = register_lattice(
        detections,
        model,
        registration_options,
        previous=previous,
        initial_linear=initial_linear,
        initial_translation=initial_translation,
    )


    return result