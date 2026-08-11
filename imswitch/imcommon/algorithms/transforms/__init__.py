"""Shared spatial transform module.

One place to say how one image space maps onto another -- create the mapping
(calibrate), persist it, load it, and apply it -- used by both ``imcontrol`` and
``improcess``.

Conventions, once, for everything below:

* Points are ``(N, ndim)`` arrays in row-major ``(..., y, x)`` order.
* Integer coordinates name pixel **centres**.
* Transforms are stored **forward**: source frame -> target frame. Inversion
  for resampling happens inside :func:`transform_image`, so no caller has to
  know that ``scipy.ndimage`` wants the output -> input map.

The design in one line: every model implements ``map_points`` and ``inverse``,
and every payload wrapper (image, points, ROI, mask) is written once on top of
that pair, so adding a model gets all of them for free.

See ``docs/image-transform-module-plan.md`` for the full design and the
migration path for the five transform implementations this replaces.
"""

from .base import (
    PAYLOAD_WRAPPERS,
    TransformModel,
    model_from_params,
    model_kinds,
    register_model,
    transform_image,
    transform_mask,
    transform_points,
    transform_roi,
)
from .estimate import (
    AffineFit,
    EstimatorSpec,
    ModelFit,
    estimate_affine,
    estimator_names,
    estimator_specs,
    fit_transform,
)
from .io import load, load_h5, load_json, save, save_h5, save_json
from .model import AcquisitionContext, ResidualStats, SpatialTransform
from .models import (
    AffineTransform,
    ComposedTransform,
    IdentityTransform,
    Rotation90Transform,
)

__all__ = [
    # protocol + registry
    "TransformModel",
    "register_model",
    "model_from_params",
    "model_kinds",
    # payload wrappers
    "transform_points",
    "transform_image",
    "transform_mask",
    "transform_roi",
    "PAYLOAD_WRAPPERS",
    # models
    "AffineTransform",
    "ComposedTransform",
    "IdentityTransform",
    "Rotation90Transform",
    # record
    "SpatialTransform",
    "ResidualStats",
    "AcquisitionContext",
    # fitting
    "fit_transform",
    "estimate_affine",
    "estimator_specs",
    "estimator_names",
    "EstimatorSpec",
    "ModelFit",
    "AffineFit",
    # persistence
    "save",
    "load",
    "save_json",
    "load_json",
    "save_h5",
    "load_h5",
]
