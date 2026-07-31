"""Navigating an N-dimensional dataset as a flat series of 2D display planes.

The pyqtgraph-backed data panels can only ever show one 2D plane, so anything
with more axes than ``(Y, X)`` has to be reduced before it reaches an
``ImageItem`` -- handing it a 3D array raises ``data.shape[2] must be <= 4``
from deep inside pyqtgraph's renderer, at paint time, with no hint as to which
widget supplied the data.

Rather than special-casing each rank, every non-``(Y, X)`` axis is treated as a
navigation axis and the whole navigation space is flattened into a single
frame index. A ``(T, Z, Y, X)`` stack therefore exposes ``T * Z`` frames on the
one slider the panels already have, and the historical ``(frames, Y, X)`` case
keeps behaving exactly as before.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .image_sources import default_axis_labels

ROW_LABEL = "Y"
COL_LABEL = "X"


def _shape_of(array: Any) -> tuple[int, ...]:
    shape = getattr(array, "shape", None)
    if shape is None:
        shape = np.shape(array)
    return tuple(int(size) for size in shape)


def plane_axes(shape: Sequence[int],
               axis_labels: Sequence[str] | None = None) -> tuple[int, int] | None:
    """Return the ``(row, col)`` axis indices of the 2D display plane.

    Falls back to the trailing two axes whenever the labels are missing, do not
    match the array rank, or do not name both ``Y`` and ``X`` -- which is also
    what ``default_axis_labels`` produces, since every layout it knows ends in
    ``Y, X``.
    """
    ndim = len(shape)
    if ndim < 2:
        return None

    labels = [str(label) for label in axis_labels] if axis_labels else []
    if len(labels) != ndim:
        labels = default_axis_labels(ndim)

    if ROW_LABEL in labels and COL_LABEL in labels:
        row, col = labels.index(ROW_LABEL), labels.index(COL_LABEL)
        if row != col:
            return row, col
    return ndim - 2, ndim - 1


def navigation_axes(shape: Sequence[int],
                    axis_labels: Sequence[str] | None = None) -> tuple[int, ...]:
    """Return the axes that must be indexed away to leave a single 2D plane."""
    plane = plane_axes(shape, axis_labels)
    if plane is None:
        return ()
    return tuple(axis for axis in range(len(shape)) if axis not in plane)


def plane_count(shape: Sequence[int],
                axis_labels: Sequence[str] | None = None) -> int:
    """Number of 2D planes the dataset exposes (1 for a plain 2D image)."""
    if len(shape) < 2:
        return 0
    count = 1
    for axis in navigation_axes(shape, axis_labels):
        count *= int(shape[axis])
    return int(count)


def _selector(ndim: int, nav: Sequence[int], nav_shape: Sequence[int],
              index: int) -> Any:
    """Index expression for flat plane ``index``, given resolved geometry.

    Trimmed to the shortest equivalent form so the common ``(frames, Y, X)``
    case still indexes with a bare ``int``: lazy handles (h5py/zarr/virtual)
    then read exactly one plane off disk, and the read pattern is unchanged
    from before N-dimensional support existed.
    """
    if not nav:
        return ()

    total = int(np.prod(nav_shape))
    index = int(min(max(int(index), 0), max(total - 1, 0)))

    selector: list[Any] = [slice(None)] * ndim
    for axis, coord in zip(nav, np.unravel_index(index, nav_shape)):
        selector[axis] = int(coord)

    while selector and selector[-1] == slice(None):
        selector.pop()
    if len(selector) == 1:
        return selector[0]
    return tuple(selector)


def _nav_shape(shape: Sequence[int], nav: Sequence[int]) -> list[int]:
    return [max(int(shape[axis]), 1) for axis in nav]


def plane_selector(shape: Sequence[int], index: int,
                   axis_labels: Sequence[str] | None = None) -> Any:
    """Build the index expression selecting flat plane ``index``."""
    nav = navigation_axes(shape, axis_labels)
    return _selector(len(shape), nav, _nav_shape(shape, nav), index)


def extract_plane(array: Any, index: int,
                  axis_labels: Sequence[str] | None = None) -> np.ndarray:
    """Read flat plane ``index`` out of ``array`` as a 2D array.

    ``array`` may be a lazy handle; only the selected plane is read.
    """
    shape = _shape_of(array)
    if len(shape) < 2:
        return np.asarray(array)
    return np.asarray(array[plane_selector(shape, index, axis_labels)])


def iter_planes(array: Any, axis_labels: Sequence[str] | None = None):
    """Yield every 2D plane of ``array`` in flat index order.

    Prefer this over ``extract_plane`` in a loop: the plane geometry is
    resolved once rather than per plane, and a lazy handle is still read one
    plane at a time, so a dataset larger than memory can be walked.
    """
    shape = _shape_of(array)
    if len(shape) < 2:
        return

    ndim = len(shape)
    nav = navigation_axes(shape, axis_labels)
    nav_shape = _nav_shape(shape, nav)
    for index in range(plane_count(shape, axis_labels)):
        yield np.asarray(array[_selector(ndim, nav, nav_shape, index)])


def mean_plane(array: Any, axis_labels: Sequence[str] | None = None) -> np.ndarray:
    """Average every navigation axis away, leaving a single 2D plane."""
    shape = _shape_of(array)
    if len(shape) < 2:
        return np.asarray(array, dtype=np.float32)

    nav = navigation_axes(shape, axis_labels)
    if not nav:
        return np.asarray(array, dtype=np.float32)
    return np.asarray(np.mean(np.asarray(array), axis=nav), dtype=np.float32)
