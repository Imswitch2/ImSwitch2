import numpy as np
import pytest

from imswitch.imcommon.algorithms.transforms.correspondence.grid_correspondence import (
    GridCorrespondence,
    GridOrdering,
    available_grid_orderings,
    grid_ordering_permutation,
    resolve_grid_correspondence,
)


def _indices(nx=5, ny=4):
    return np.asarray([(i, j) for j in range(ny) for i in range(nx)], dtype=np.int64)


def _points(indices):
    logical = np.asarray(indices, dtype=np.float64)
    linear = np.array([[0.3, 12.0], [10.0, 0.6]])
    return logical @ linear.T + np.array([40.0, 70.0])


def test_non_square_grid_only_exposes_shape_preserving_orderings():
    options = available_grid_orderings(_indices(5, 4))
    assert options == (
        GridOrdering.CANONICAL,
        GridOrdering.FLIP_X,
        GridOrdering.FLIP_Y,
        GridOrdering.ROTATE_180,
    )


def test_staggered_grid_rejects_motif_changing_symmetries():
    indices = _indices(5, 4)
    logical = np.asarray(indices, dtype=np.float64)
    logical[:, 0] += (indices[:, 1] % 2) * 0.5
    logical -= np.mean(logical, axis=0, keepdims=True)

    options = available_grid_orderings(indices, logical)
    assert options == (GridOrdering.CANONICAL, GridOrdering.ROTATE_180)


@pytest.mark.parametrize(
    "target_ordering, expected_reindexing",
    [
        (GridOrdering.CANONICAL, GridOrdering.CANONICAL),
        (GridOrdering.FLIP_X, GridOrdering.FLIP_X),
        (GridOrdering.FLIP_Y, GridOrdering.FLIP_Y),
        (GridOrdering.ROTATE_180, GridOrdering.ROTATE_180),
        (GridOrdering.TRANSPOSE, GridOrdering.TRANSPOSE),
        (GridOrdering.ROTATE_90_CW, GridOrdering.ROTATE_90_CCW),
        (GridOrdering.ROTATE_90_CCW, GridOrdering.ROTATE_90_CW),
        (GridOrdering.ANTI_TRANSPOSE, GridOrdering.ANTI_TRANSPOSE),
    ],
)
def test_auto_resolves_independent_square_grid_indexing(
    target_ordering, expected_reindexing
):
    indices = _indices(5, 5)
    source = _points(indices)
    permutation = grid_ordering_permutation(indices, target_ordering)
    target = source[permutation] + np.array([2.0, -3.0])

    moving, reference, resolution = resolve_grid_correspondence(
        source, target, indices, correspondence=GridCorrespondence.AUTO
    )

    assert resolution.resolved is expected_reindexing
    assert not resolution.reflection
    assert resolution.pair_count == len(indices)
    np.testing.assert_allclose(reference - moving, [2.0, -3.0], atol=1e-10)


def test_manual_override_keeps_only_genuinely_measured_pairs():
    indices = _indices(5, 4)
    source = _points(indices)
    rotate = grid_ordering_permutation(indices, GridOrdering.ROTATE_180)
    target = source[rotate] + np.array([1.0, 4.0])
    source_mask = np.ones(len(indices), dtype=bool)
    target_mask = np.ones(len(indices), dtype=bool)
    source_mask[[1, 7]] = False
    target_mask[[rotate[3], rotate[7]]] = False

    moving, reference, resolution = resolve_grid_correspondence(
        source,
        target,
        indices,
        source_matched=source_mask,
        target_matched=target_mask,
        correspondence="rotate_180",
    )

    assert resolution.requested is GridCorrespondence.ROTATE_180
    assert resolution.resolved is GridOrdering.ROTATE_180
    assert resolution.pair_count == 17
    assert len(moving) == len(reference) == 17
    np.testing.assert_allclose(reference - moving, [1.0, 4.0], atol=1e-10)


def test_invalid_manual_transpose_is_rejected_for_non_square_grid():
    indices = _indices(5, 4)
    points = _points(indices)
    with pytest.raises(ValueError, match="not valid"):
        resolve_grid_correspondence(
            points, points, indices, correspondence="transpose"
        )
