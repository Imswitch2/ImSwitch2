"""Shared helpers for pulling a 2-D (Y, X) plane out of an N-D result.

Several ImProcess processors (colocalization, FRC, PSF/bead resolution and
segmentation) operate on a single two-dimensional image, but their inputs are
arbitrary N-D :class:`ProcessingResult` arrays (e.g. ``["C", "Y", "X"]`` or
``["Dataset", "Base", "T", "Z", "Y", "X"]``).

All of these processors share one convention, centralised here:

* **The last two axes are the spatial ``(Y, X)`` plane.** Every other axis is
  "non-spatial" and must be collapsed to a single index before analysis.
* When collapsing, an optional *compare axis* is sliced to a caller-supplied
  index; every remaining non-spatial axis is sliced to ``0``.

This module also exposes :func:`validate_axes`, which enforces the (previously
implicit and unchecked) contract that ``len(result.axis_labels)`` matches
``result.data.ndim``. Callers that index ``axis_labels`` positionally rely on
this; segmentation deliberately tolerates a mismatch and uses
:func:`axis_labels_for_data` to synthesise default labels instead.
"""

from typing import Sequence

import numpy as np

from imswitch.improcess.model.result import ProcessingResult

__all__ = [
    "validate_axes",
    "resolve_axis",
    "extract_2d_plane",
    "axis_labels_for_data",
]


def validate_axes(result: ProcessingResult) -> None:
    """Raise ``ValueError`` if axis labels do not describe every data axis.

    The "last two axes are (Y, X)" convention only holds when each array axis
    has a corresponding label, so processors that index ``axis_labels``
    positionally must be able to trust this invariant.
    """
    data = np.asarray(result.data)
    labels = list(getattr(result, "axis_labels", []) or [])
    if len(labels) != data.ndim:
        raise ValueError(
            f"axis_labels {labels} does not match data with {data.ndim} "
            f"dimension(s) (shape {data.shape}); expected one label per axis"
        )


def resolve_axis(
    result: ProcessingResult,
    candidates: Sequence[str],
    *,
    requested: str | None = None,
    min_size: int = 2,
    error_message: str | None = None,
) -> str:
    """Resolve the label of a non-spatial axis to compare planes along.

    Args:
        result: Input result; ``data`` and ``axis_labels`` must be consistent.
        candidates: Preferred axis labels, tried in priority order (e.g.
            ``("C", "T", "Z")``). The first one that exists and has at least
            ``min_size`` planes wins.
        requested: An explicit user choice. ``None`` or ``"Auto"`` triggers
            automatic resolution; any other value is validated and returned.
        min_size: Minimum number of planes an axis must have to qualify.
        error_message: Raised when no axis qualifies.

    Returns:
        The chosen axis label.
    """
    validate_axes(result)
    labels = result.axis_labels
    shape = np.asarray(result.data).shape

    if requested is not None and requested != "Auto":
        if requested not in labels:
            raise ValueError(f"Requested compare axis {requested!r} not in {labels}")
        if shape[labels.index(requested)] < min_size:
            raise ValueError(
                f"Compare axis {requested!r} needs at least {min_size} planes"
            )
        return requested

    for candidate in candidates:
        if candidate in labels and shape[labels.index(candidate)] >= min_size:
            return candidate
    # Fall back to the first non-spatial axis large enough to compare.
    for axis, size in enumerate(shape[:-2]):
        if size >= min_size:
            return labels[axis]
    raise ValueError(error_message or f"No axis has at least {min_size} planes to compare")


def extract_2d_plane(
    result: ProcessingResult,
    *,
    compare_axis: str | None = None,
    index: int = 0,
) -> np.ndarray:
    """Slice a single ``(Y, X)`` plane out of an N-D result.

    The last two axes are treated as spatial and kept whole. ``compare_axis``
    (if given) is sliced to ``index``; every other non-spatial axis is sliced
    to ``0``. A 2-D input is returned unchanged.

    Args:
        result: Input result; ``data`` and ``axis_labels`` must be consistent.
        compare_axis: Label of the axis to index with ``index``, or ``None`` to
            collapse every non-spatial axis to ``0``.
        index: Index along ``compare_axis``.

    Returns:
        A 2-D ``numpy`` array.
    """
    data = np.asarray(result.data)
    if data.ndim < 2:
        raise ValueError(
            f"Cannot extract a 2D plane from shape {data.shape}; need at least 2 dimensions"
        )
    if data.ndim == 2:
        return data

    if compare_axis is not None:
        validate_axes(result)
        compare_index = result.axis_labels.index(compare_axis)
    else:
        compare_index = None

    indexer = []
    for axis, size in enumerate(data.shape):
        if axis >= data.ndim - 2:  # spatial (Y, X)
            indexer.append(slice(None))
        elif axis == compare_index:
            if index < 0 or index >= size:
                raise ValueError(
                    f"Plane index {index} out of range for axis "
                    f"{compare_axis!r} with size {size}"
                )
            indexer.append(index)
        else:
            indexer.append(0)

    plane = np.asarray(data[tuple(indexer)])
    if plane.ndim != 2:
        raise ValueError(f"Could not extract a 2D plane from shape {data.shape}")
    return plane


def axis_labels_for_data(result: ProcessingResult, data: np.ndarray) -> list[str]:
    """Return per-axis labels for ``data``, synthesising defaults if needed.

    Unlike :func:`validate_axes`, this tolerates a label/ndim mismatch: when
    the result's ``axis_labels`` do not cover every axis, sensible defaults
    (``..., T, Z, C, Y, X``) are returned so callers can still name non-spatial
    axes. Used by processors that accept loosely-labelled inputs.
    """
    labels = list(getattr(result, "axis_labels", []) or [])
    if len(labels) == data.ndim:
        return [str(label) for label in labels]
    defaults = ["T", "Z", "C", "Y", "X"]
    if data.ndim <= len(defaults):
        return defaults[-data.ndim:]
    extra = [f"D{i}" for i in range(data.ndim - len(defaults))]
    return [*extra, *defaults]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
