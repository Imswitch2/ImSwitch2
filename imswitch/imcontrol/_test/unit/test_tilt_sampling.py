"""Experimental XY calibration density independent of the scanning area."""

import pytest

from imswitch.imcontrol.model.workflows.tilt_sampling import calibration_visits
from imswitch.imcontrol.model.workflows.spiral import SERPENTINE, SPIRAL


@pytest.mark.parametrize('pattern, expected_bounds', [
    (SERPENTINE, ((0., 400.), (0., 400.))),
    (SPIRAL, ((-200., 200.), (-200., 200.))),
])
def test_21_square_5_sample_grid_same_area(pattern, expected_bounds):
    visits = calibration_visits(21, 21, 20., 5, 5, pattern)
    assert len(visits) == 25
    assert visits[0][-2:] == (0., 0.)
    assert (min(v[2] for v in visits), max(v[2] for v in visits)) == expected_bounds[0]
    assert (min(v[3] for v in visits), max(v[3] for v in visits)) == expected_bounds[1]
    assert len({(v[2], v[3]) for v in visits}) == 25


def test_rectangle_and_even_spiral_are_supported():
    visits = calibration_visits(20, 8, 30., 6, 4, SPIRAL)
    assert len(visits) == 24
    assert max(v[2] for v in visits) - min(v[2] for v in visits) == pytest.approx(570.)
    assert max(v[3] for v in visits) - min(v[3] for v in visits) == pytest.approx(210.)


def test_reverse_uses_exact_forward_endpoint():
    forward = calibration_visits(21, 21, 20., 5, 5, SERPENTINE)
    backward = list(reversed(forward))
    assert forward[-1] == backward[0]
    assert backward[-1] == forward[0]


@pytest.mark.parametrize('values', [(1, 5), (5, 1), (0, 2)])
def test_reject_degenerate_areas(values):
    with pytest.raises(ValueError, match='at least 2'):
        calibration_visits(values[0], 21, 20., values[1], 5, SERPENTINE)
