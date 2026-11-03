import numpy as np

from imswitch.improcess.reconstructors.monalisa.calibration import (
    PatternLocalization, ScanOrientation,
)


def test_orientation_literals_round_trip_and_transform_indices():
    index = np.array([[0, 0], [1, 0], [0, 2]])
    for literal in ("+x+y", "-x+y", "+y-x", "-y-x"):
        orientation = ScanOrientation.from_literal(literal)
        assert orientation.to_literal() == literal
        transformed = orientation.orient_index(index)
        assert transformed.shape == index.shape


def test_manual_x_stagger_is_a_two_point_motif():
    localization = PatternLocalization.manual_grid(10.0, 8.0, 2.0, 3.0, stagger_axis="x", stagger=0.5)
    x, y = localization.points_in_frame(30, 30)
    # Rows at y=3 and y=11 alternate by half the x period.
    row0 = np.sort(x[np.isclose(y, 3.0)])
    row1 = np.sort(x[np.isclose(y, 11.0)])
    assert row0.size > 1 and row1.size > 1
    assert np.isclose((row1[0] - row0[0]) % 10.0, 5.0)
