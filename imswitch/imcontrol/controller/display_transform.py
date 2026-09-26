from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class DisplayTransform:
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False

    @property
    def is_identity(self) -> bool:
        return self.rotation == 0 and not self.flip_x and not self.flip_y


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalise_rotation(value: Any) -> int:
    try:
        rotation = int(float(value))
    except (TypeError, ValueError):
        return 0

    rotation %= 360
    if rotation not in {0, 90, 180, 270}:
        return 0
    return rotation


def display_transform_from_properties(
    manager_properties: Mapping[str, Any] | None,
) -> DisplayTransform:
    """Read the display-only detector orientation from managerProperties."""
    if not manager_properties:
        return DisplayTransform()

    nested = manager_properties.get("displayTransform")
    if isinstance(nested, Mapping):
        return DisplayTransform(
            rotation=_normalise_rotation(nested.get("rotation", 0)),
            flip_x=_as_bool(nested.get("flipX", nested.get("flip_x", False))),
            flip_y=_as_bool(nested.get("flipY", nested.get("flip_y", False))),
        )

    return DisplayTransform(
        rotation=_normalise_rotation(manager_properties.get("displayRotation", 0)),
        flip_x=_as_bool(manager_properties.get("displayFlipX", False)),
        flip_y=_as_bool(manager_properties.get("displayFlipY", False)),
    )


def apply_display_transform(
    image: np.ndarray,
    scale: Sequence[float] | None,
    transform: DisplayTransform,
) -> tuple[np.ndarray, Sequence[float] | None]:
    """Apply a detector display transform over the last two image axes."""
    if transform.is_identity or image.ndim < 2:
        return image, scale

    display_image = image
    display_scale = list(scale) if scale is not None else None

    if transform.rotation:
        display_image = np.rot90(
            display_image, k=transform.rotation // 90, axes=(-2, -1)
        )
        if (
            transform.rotation in {90, 270}
            and display_scale is not None
            and len(display_scale) >= 2
        ):
            display_scale[-2], display_scale[-1] = display_scale[-1], display_scale[-2]

    if transform.flip_x:
        display_image = np.flip(display_image, axis=-1)
    if transform.flip_y:
        display_image = np.flip(display_image, axis=-2)

    return np.ascontiguousarray(display_image), display_scale


def raw_to_display_point(
    row: float,
    col: float,
    raw_shape: Sequence[int],
    transform: DisplayTransform,
) -> tuple[float, float]:
    """Where raw pixel ``(row, col)`` lands in the displayed image.

    Coordinates are continuous over the last two axes, with pixel centres at
    integers, so a point anywhere inside a pixel maps consistently. The steps
    are those of :func:`apply_display_transform`: rotate (``np.rot90``, one
    quarter turn sends ``(r, c)`` of an ``H x W`` image to ``(W-1-c, r)``),
    then flip X, then flip Y.
    """
    height, width = int(raw_shape[-2]), int(raw_shape[-1])
    r, c = float(row), float(col)
    for _ in range(transform.rotation // 90):
        r, c = width - 1 - c, r
        height, width = width, height
    if transform.flip_x:
        c = width - 1 - c
    if transform.flip_y:
        r = height - 1 - r
    return r, c


def display_to_raw_point(
    row: float,
    col: float,
    raw_shape: Sequence[int],
    transform: DisplayTransform,
) -> tuple[float, float]:
    """The raw pixel coordinate shown at displayed ``(row, col)``.

    The inverse of :func:`raw_to_display_point`, for the same ``raw_shape``
    (the image's shape *before* the transform).
    """
    height, width = int(raw_shape[-2]), int(raw_shape[-1])
    turns = transform.rotation // 90
    display_height, display_width = (
        (width, height) if turns % 2 else (height, width)
    )
    r, c = float(row), float(col)
    if transform.flip_y:
        r = display_height - 1 - r
    if transform.flip_x:
        c = display_width - 1 - c
    # Undo the quarter turns, last first: (r', c') of the turned image came
    # from (c', w - 1 - r') of the (h, w) image before that turn.
    shapes = []
    h, w = height, width
    for _ in range(turns):
        shapes.append((h, w))
        h, w = w, h
    for h, w in reversed(shapes):
        r, c = c, w - 1 - r
    return r, c
