"""Unit tests for trimRasterLengthForFirmwareBoundary.

Background: the TriggerScope firmware's RASTER_SCAN axis loop is INCLUSIVE
(``while (abs(pos) <= abs(lenV))``), so when the scan length is an exact
multiple of the step size, the firmware visits length/step + 1 positions
(both endpoints) -- one MORE pixel than round(length/step) pixels, which is
what the rest of the system (BeadRec reconstruction dims, GUI step count)
expects. E.g. a 4 um scan at exactly 200 nm/pixel (20.0 pixels, an exact
ratio) came back with 21 frames; nudging to 201 nm/pixel (not an exact
divisor) "fixed" it by accident, because the loop then overshoots the
inclusive threshold one increment earlier and stops at 20.

trimRasterLengthForFirmwareBoundary shrinks the length sent to firmware by
half a step so the boundary lands safely between the intended last position
and the next one, regardless of which side of an exact ratio floating-point
rounding falls on.
"""
import math

from imswitch.imcontrol.controller.controllers.TriggerScopeRasterController import (
    trimRasterLengthForFirmwareBoundary,
)


def _firmware_position_count(length, stepSize, trimmedLength):
    """Re-implement the firmware's inclusive while-loop in Python to count how
    many positions it would actually visit for a given (trimmed) length."""
    pos = 0.0
    count = 0
    # Safety bound so a bug here can't hang the test suite.
    for _ in range(100000):
        if abs(pos) > abs(trimmedLength):
            break
        count += 1
        pos += stepSize
    return count


def test_exact_divisor_produces_intended_pixel_count_not_one_extra():
    """The user's reported case: 4000 nm length, 200 nm/pixel -> 20 pixels,
    not 21."""
    length, stepSize = 4000.0, 200.0
    intended = round(length / stepSize)
    trimmed = trimRasterLengthForFirmwareBoundary(length, stepSize)

    assert _firmware_position_count(length, stepSize, trimmed) == intended == 20
    # Confirm the untrimmed length is the one that used to overshoot to 21,
    # i.e. this test would have failed before the fix.
    assert _firmware_position_count(length, stepSize, length) == intended + 1


def test_non_exact_divisor_still_matches_intended_count():
    """The user's workaround (201 nm) must also still give exactly 20 with the
    fix applied, not 19 or 21."""
    length, stepSize = 4000.0, 201.0
    intended = round(length / stepSize)
    trimmed = trimRasterLengthForFirmwareBoundary(length, stepSize)

    assert _firmware_position_count(length, stepSize, trimmed) == intended == 20


def test_trim_is_half_a_step():
    assert trimRasterLengthForFirmwareBoundary(4000.0, 200.0) == 3900.0
    assert math.isclose(
        trimRasterLengthForFirmwareBoundary(4000.0, 201.0), 3899.5
    )


def test_zero_step_size_is_left_unchanged():
    """A trivial (non-scanning) axis has stepSize == 0; must not divide by
    zero or otherwise touch the length."""
    assert trimRasterLengthForFirmwareBoundary(0.0, 0.0) == 0.0
    assert trimRasterLengthForFirmwareBoundary(123.0, 0.0) == 123.0


def test_various_pixel_counts_all_land_on_intended_count():
    """Sweep several (length, stepSize) pairs, including exact and non-exact
    divisors, and confirm the trimmed length always yields round(length/step)
    positions."""
    cases = [
        (4000.0, 200.0),   # exact: 20.0
        (4000.0, 100.0),   # exact: 40.0
        (1000.0, 50.0),    # exact: 20.0
        (4000.0, 199.0),   # non-exact, just under
        (4000.0, 201.0),   # non-exact, just over
        (3333.0, 111.0),   # exact: 30.0
        (10.0, 3.0),       # non-exact: 3.33...
    ]
    for length, stepSize in cases:
        intended = round(length / stepSize)
        trimmed = trimRasterLengthForFirmwareBoundary(length, stepSize)
        actual = _firmware_position_count(length, stepSize, trimmed)
        assert actual == intended, (
            f'length={length} stepSize={stepSize}: expected {intended} '
            f'positions, firmware loop would visit {actual}'
        )
