"""Transform model protocol, kind registry, and the generic payload wrappers.

The design constraint this file exists to enforce: *what* you transform (image,
point cloud, ROI, mask) and *which model* does it (identity, rotation, affine,
polynomial, displacement field) are independent axes. Implementing them as a
matrix means every new model touches every payload wrapper, so it is not done
that way.

Every model implements exactly one required primitive -- :meth:`map_points`
plus :meth:`inverse` -- and every payload wrapper in this module is written
*once* against that primitive. A model added later gets image, ROI and mask
support for free.

Fast paths (``warp_fast``) are optimizations, never separate semantics: a model
may offer a cheaper route for its own structure, and the generic layer uses it
when available. Every fast path is required to agree with the generic path to
within interpolation tolerance, and the test suite asserts exactly that.

Coordinate convention throughout: row-major ``(..., y, x)`` point arrays of
shape ``(N, ndim)``, integer coordinates naming pixel *centres*, matching
``imcommon.algorithms.detector_transform``. Transforms are stored **forward**
(source -> target); inversion for resampling happens inside this module so no
caller has to know that ``scipy.ndimage`` wants the output -> input map.

Heavy dependencies (scipy, scikit-image) stay behind function-local imports so
importing this package remains cheap, per the package docstring in
``imcommon/algorithms/__init__.py``.
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Mapping, Sequence

import numpy as np

__all__ = [
    "TransformModel",
    "register_model",
    "model_from_params",
    "model_kinds",
    "transform_points",
    "transform_image",
    "transform_mask",
    "transform_roi",
]


class TransformModel(abc.ABC):
    """One swappable spatial mapping.

    Subclasses must implement :meth:`map_points`, :meth:`inverse`,
    :meth:`to_params` and :meth:`from_params`. Everything else has a working
    generic default; override only to go faster or to advertise structure.
    """

    #: Stable identifier used for serialization and registry lookup.
    kind: str = ""

    @property
    @abc.abstractmethod
    def ndim(self) -> int:
        """Number of spatial dimensions this model maps."""

    # -- the one required primitive -------------------------------------

    @abc.abstractmethod
    def map_points(self, points: Any) -> np.ndarray:
        """Map ``(N, ndim)`` source points to target points, forward.

        Points are ``(..., y, x)`` ordered and name pixel centres.
        """

    @abc.abstractmethod
    def inverse(self) -> "TransformModel":
        """Return the target -> source mapping.

        Models without a closed-form inverse must return a numerical one and
        report its error rather than pretending to be exact.
        """

    # -- serialization ---------------------------------------------------

    @abc.abstractmethod
    def to_params(self) -> dict[str, Any]:
        """Return JSON-safe parameters reconstructing this model."""

    @classmethod
    @abc.abstractmethod
    def from_params(cls, params: Mapping[str, Any]) -> "TransformModel":
        """Rebuild a model from :meth:`to_params` output."""

    # -- capability flags ------------------------------------------------

    def as_matrix(self) -> np.ndarray | None:
        """Return an ``(ndim+1, ndim+1)`` homogeneous matrix, or ``None``.

        ``None`` means this model genuinely cannot be expressed as an affine --
        a polynomial or a displacement field. Callers that need a matrix (the
        non-resampling display path, matrix composition) must check for
        ``None`` and degrade explicitly rather than quietly applying something
        wrong.
        """
        return None

    @property
    def is_linear(self) -> bool:
        """Whether this model is affine, i.e. :meth:`as_matrix` is not None."""
        return self.as_matrix() is not None

    @property
    def is_axis_aligned(self) -> bool:
        """Whether this maps pixel centres onto pixel centres without mixing.

        True permits a lossless resample-free implementation (``np.rot90``,
        ``np.flip``, slicing) with no interpolation at all.
        """
        return False

    @property
    def has_analytic_inverse(self) -> bool:
        """Whether :meth:`inverse` is exact rather than numerically fitted."""
        return True

    # -- optional fast path ----------------------------------------------

    def warp_fast(
        self,
        image: np.ndarray,
        out_shape: tuple[int, ...],
        *,
        order: int = 1,
        cval: float = 0.0,
    ) -> np.ndarray | None:
        """Return a resampled image via a model-specific shortcut, or ``None``.

        Returning ``None`` -- the default -- means "no shortcut, use the
        generic path". An override must produce the same result as the generic
        path to within interpolation tolerance.
        """
        return None


# ---------------------------------------------------------------------------
# Kind registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[TransformModel]] = {}


def register_model(cls: type[TransformModel]) -> type[TransformModel]:
    """Class decorator registering a model under its ``kind``."""
    kind = getattr(cls, "kind", "")
    if not kind:
        raise ValueError(f"{cls.__name__} must define a non-empty 'kind'")
    existing = _REGISTRY.get(kind)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"transform kind {kind!r} is already registered to {existing.__name__}"
        )
    _REGISTRY[kind] = cls
    return cls


def model_from_params(kind: str, params: Mapping[str, Any]) -> TransformModel:
    """Rebuild a registered model from its kind and parameters."""
    try:
        cls = _REGISTRY[str(kind)]
    except KeyError:
        raise ValueError(
            f"unknown transform kind {kind!r}; known kinds: {sorted(_REGISTRY)}"
        ) from None
    return cls.from_params(params)


def model_kinds() -> tuple[str, ...]:
    """Return every registered kind, sorted."""
    return tuple(sorted(_REGISTRY))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def as_points(points: Any, ndim: int, *, label: str = "points") -> np.ndarray:
    """Validate and normalize an ``(N, ndim)`` float point array."""
    array = np.asarray(points, dtype=np.float64)
    if array.ndim == 1 and array.size == ndim:
        array = array.reshape(1, ndim)
    if array.ndim != 2 or array.shape[1] != ndim:
        raise ValueError(
            f"{label} must have shape (N, {ndim}), got {tuple(np.shape(points))}"
        )
    return array


def _model_of(transform: Any) -> TransformModel:
    """Accept either a bare model or anything exposing ``.model``."""
    if isinstance(transform, TransformModel):
        return transform
    model = getattr(transform, "model", None)
    if isinstance(model, TransformModel):
        return model
    raise TypeError(
        f"expected a TransformModel or an object carrying one, got "
        f"{type(transform).__name__}"
    )


# ---------------------------------------------------------------------------
# Generic payload wrappers -- written once, against map_points only
# ---------------------------------------------------------------------------


def transform_points(points: Any, transform: Any) -> np.ndarray:
    """Map a point cloud through any transform.

    Points are ``(N, ndim)`` in ``(..., y, x)`` order; the result has the same
    shape.
    """
    model = _model_of(transform)
    return model.map_points(points)


def transform_image(
    image: Any,
    transform: Any,
    *,
    out_shape: Sequence[int] | None = None,
    order: int = 1,
    cval: float = 0.0,
    prefer_fast: bool = True,
) -> np.ndarray:
    """Resample an image through any transform.

    The generic implementation evaluates the *inverse* mapping on the output
    pixel grid and hands the resulting source coordinates to
    ``scipy.ndimage.map_coordinates``. That works for every model, including
    ones not yet written, which is the whole point of the design.

    The transform acts on the trailing ``model.ndim`` axes; any leading axes
    (channel, time, z on a 2-D model) are iterated, so stacks transform plane
    by plane and consistently.

    ``prefer_fast=False`` forces the generic path, which is how the test suite
    holds every fast path to the generic result.
    """
    model = _model_of(transform)
    image = np.asarray(image)
    ndim = model.ndim

    if image.ndim < ndim:
        raise ValueError(
            f"image needs at least {ndim} dimensions for this transform, "
            f"got shape {image.shape}"
        )

    spatial_shape = image.shape[-ndim:]
    leading_shape = image.shape[:-ndim]
    target_shape = (
        tuple(int(size) for size in spatial_shape)
        if out_shape is None
        else tuple(int(size) for size in out_shape)
    )
    if len(target_shape) != ndim:
        raise ValueError(
            f"out_shape must have {ndim} entries, got {tuple(target_shape)}"
        )
    if any(size <= 0 for size in target_shape):
        raise ValueError(f"out_shape must be positive, got {tuple(target_shape)}")

    if prefer_fast:
        fast = model.warp_fast(image, target_shape, order=order, cval=cval)
        if fast is not None:
            return fast

    from scipy.ndimage import map_coordinates

    # Materializing the output grid costs ndim float64 arrays of the output
    # size. Structured models avoid it through warp_fast; only genuinely
    # freeform ones (polynomial, displacement field) pay for it.
    grid = np.indices(target_shape, dtype=np.float64).reshape(ndim, -1).T
    source = model.inverse().map_points(grid)
    coords = np.ascontiguousarray(source.T).reshape(ndim, *target_shape)

    def resample(plane: np.ndarray) -> np.ndarray:
        return map_coordinates(
            plane, coords, order=order, mode="constant", cval=cval
        )

    if not leading_shape:
        return resample(image)

    planes = image.reshape(-1, *spatial_shape)
    stacked = np.stack([resample(plane) for plane in planes], axis=0)
    return stacked.reshape(*leading_shape, *target_shape)


def transform_mask(
    mask: Any,
    transform: Any,
    *,
    out_shape: Sequence[int] | None = None,
    prefer_fast: bool = True,
) -> np.ndarray:
    """Resample a label or boolean mask, without inventing intermediate values.

    Nearest-neighbour throughout (``order=0``); a boolean input stays boolean.
    """
    mask = np.asarray(mask)
    result = transform_image(
        mask,
        transform,
        out_shape=out_shape,
        order=0,
        cval=0,
        prefer_fast=prefer_fast,
    )
    return result.astype(mask.dtype, copy=False)


def transform_roi(roi: Any, transform: Any) -> Any:
    """Map an :class:`~imcommon.algorithms.roi.ROIRecord` through a transform.

    The rectangle corners are mapped and the axis-aligned bounding box of the
    result is taken. That is **lossy for anything but an axis-aligned
    transform**: a rotated rectangle is not a rectangle, and the bounding box
    is strictly larger than the true footprint. Free-form ``pixels`` members
    are mapped point-wise and rounded, which is exact up to sampling.
    """
    from imswitch.imcommon.algorithms.roi import ROIRecord

    if not isinstance(roi, ROIRecord):
        raise TypeError(f"expected an ROIRecord, got {type(roi).__name__}")

    model = _model_of(transform)
    if model.ndim != 2:
        raise ValueError(
            f"ROI transforms need a 2-D model, got ndim={model.ndim}"
        )

    row0, col0, row1, col1 = (int(value) for value in roi.bounds)
    corners = np.array(
        [[row0, col0], [row0, col1], [row1, col0], [row1, col1]], dtype=np.float64
    )
    mapped = model.map_points(corners)
    bounds = (
        int(np.floor(mapped[:, 0].min())),
        int(np.floor(mapped[:, 1].min())),
        int(np.ceil(mapped[:, 0].max())),
        int(np.ceil(mapped[:, 1].max())),
    )

    pixels = roi.pixels
    if pixels is not None and len(pixels) > 0:
        mapped_pixels = model.map_points(np.asarray(pixels, dtype=np.float64))
        pixels = tuple(
            (int(round(row)), int(round(col))) for row, col in mapped_pixels
        )

    return ROIRecord(
        name=roi.name,
        roi_type=roi.roi_type,
        bounds=bounds,
        visible=roi.visible,
        source=roi.source,
        pixels=pixels,
    )


# Kept for the wrapper registry-parametrized contract test, so a new payload
# wrapper cannot be added without the suite noticing.
PAYLOAD_WRAPPERS: dict[str, Callable[..., Any]] = {
    "points": transform_points,
    "image": transform_image,
    "mask": transform_mask,
    "roi": transform_roi,
}
