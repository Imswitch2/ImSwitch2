from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model import ProcessingResult
from imswitch.improcess.processors._extraction import (
    extract_2d_plane,
    resolve_axis,
    validate_axes,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_validate_axes_raises_on_mismatched_labels():
    result = MinimalResult(
        name="bad",
        data=np.zeros((2, 4, 4), dtype=float),
        axis_labels=["Y", "X"],
    )

    with pytest.raises(ValueError, match="does not match data"):
        validate_axes(result)


def test_validate_axes_accepts_matching_labels():
    result = MinimalResult(
        name="ok",
        data=np.zeros((2, 4, 4), dtype=float),
        axis_labels=["C", "Y", "X"],
    )

    validate_axes(result)  # does not raise


def test_extract_2d_plane_slices_compare_axis_and_collapses_rest():
    data = np.arange(2 * 3 * 4 * 5, dtype=float).reshape(2, 3, 4, 5)
    result = MinimalResult(name="stack", data=data, axis_labels=["T", "C", "Y", "X"])

    plane = extract_2d_plane(result, compare_axis="C", index=2)

    assert plane.shape == (4, 5)
    np.testing.assert_array_equal(plane, data[0, 2])


def test_resolve_axis_prefers_candidate_with_enough_planes():
    data = np.zeros((1, 3, 8, 8), dtype=float)
    result = MinimalResult(name="stack", data=data, axis_labels=["T", "C", "Y", "X"])

    assert resolve_axis(result, ("C", "T", "Z")) == "C"
