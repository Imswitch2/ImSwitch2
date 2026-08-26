"""Centered array fitting helpers used by CGH computation backends."""

from __future__ import annotations



import numpy as np


def fit_array_centered(
    array: np.ndarray,
    target_shape: tuple[int, int],
    pad_mode: str = "wrap",
):
    """Center-pad or center-crop a 2D array to ``target_shape``.

    Padding preserves the historical centering behavior: the smaller half is added
    before the array and the larger half after it when the difference is odd.
    Cropping starts at ``difference // 2``. Axes are processed independently.

    Returns
    -------
    fitted, cropped
        The fitted array and a flag indicating whether any cropping occurred.
    """
    result = np.asarray(array)
    target_shape = _validate_shape(target_shape)

    if result.ndim != 2:
        raise ValueError(f"array must be 2D, got shape {result.shape}")

    cropped = False

    for axis, target_size in enumerate(target_shape):
        current_size = result.shape[axis]

        if current_size < target_size:
            before = (target_size - current_size) // 2
            after = target_size - current_size - before
            pad_width = [(0, 0), (0, 0)]
            pad_width[axis] = (before, after)
            result = np.pad(result, pad_width, mode=pad_mode)

        elif current_size > target_size:
            start = (current_size - target_size) // 2
            slices = [slice(None), slice(None)]
            slices[axis] = slice(start, start + target_size)
            result = result[tuple(slices)]
            cropped = True

    return result, cropped


def _validate_shape(shape: tuple[int, int]) -> tuple[int, int]:
    """Return a validated positive ``(height, width)`` shape."""
    if shape is None or len(shape) != 2:
        raise ValueError(f"target_shape must be (height, width), got {shape}")

    height, width = int(shape[0]), int(shape[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"target_shape must be positive, got {shape}")
    return height, width
