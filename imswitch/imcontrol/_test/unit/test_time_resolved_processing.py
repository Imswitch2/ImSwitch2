from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    TimeResolvedScanConfig,
    aggregate_decay,
    compute_gate_images,
    intensity_from_cube,
)


def test_gate_spec_validates_bounds_and_name():
    with pytest.raises(ValueError, match="must not be empty"):
        GateSpec("", 0.0, 1.0)
    with pytest.raises(ValueError, match="greater than"):
        GateSpec("bad", 2.0, 1.0)


def test_time_resolved_scan_config_rejects_duplicate_gate_names():
    with pytest.raises(ValueError, match="Duplicate time gate names"):
        TimeResolvedScanConfig(
            gates=(
                GateSpec("late", 1.0, 2.0),
                GateSpec("late", 2.0, 3.0),
            )
        )


def test_aggregate_decay_and_intensity_sum_expected_axes():
    cube = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)

    np.testing.assert_array_equal(aggregate_decay(cube), cube.sum(axis=(0, 1)))
    np.testing.assert_array_equal(intensity_from_cube(cube), cube.sum(axis=-1))


def test_compute_gate_images_uses_start_inclusive_stop_exclusive_bins():
    cube = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    t_axis_ns = np.array([0.5, 1.5, 2.5, 3.5], dtype=np.float32)
    gates = (
        GateSpec("first_two", 0.5, 2.5),
        GateSpec("last", 3.5, 4.5),
        GateSpec("empty", 10.0, 11.0),
    )

    images = compute_gate_images(cube, t_axis_ns, gates)

    np.testing.assert_array_equal(images["first_two"], cube[..., :2].sum(axis=-1))
    np.testing.assert_array_equal(images["last"], cube[..., 3])
    np.testing.assert_array_equal(
        images["empty"], np.zeros(cube.shape[:-1], dtype=cube.dtype)
    )


def test_compute_gate_images_validates_time_axis_length():
    cube = np.zeros((2, 3, 4), dtype=np.float32)
    with pytest.raises(ValueError, match="does not match cube bin axis"):
        compute_gate_images(cube, np.zeros(3), (GateSpec("gate", 0.0, 1.0),))
