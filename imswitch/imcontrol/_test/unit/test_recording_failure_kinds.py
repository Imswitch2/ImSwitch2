"""Typed recording failures, and where a session's data actually landed.

Both exist for a caller that runs many recordings in one go — a tiling run —
and has to decide, per point, whether to carry on and where to point its
manifest. Neither decision can be made from a message string.
"""

import pytest

from imswitch.imcontrol.model.managers.RecordingManager import (
    FailureKind,
    PayloadLocator,
)


# ----------------------------------------------------------------------
# Failure kinds
# ----------------------------------------------------------------------


def test_only_a_writer_failure_is_recoverable():
    """The measurement happened; the file did not. Everything else is data."""
    assert FailureKind.WRITER.recoverable

    for kind in (FailureKind.ACQUISITION, FailureKind.SCAN,
                 FailureKind.HARDWARE):
        assert not kind.recoverable, f'{kind} must stop the run'


def test_an_unclassified_failure_is_treated_as_unrecoverable():
    """The safe reading: the alternative is continuing past the unknown."""
    assert not FailureKind.UNKNOWN.recoverable


def test_every_kind_survives_a_round_trip_through_its_value():
    """The signal carries a string, so the enum has to come back from one."""
    for kind in FailureKind:
        assert FailureKind(kind.value) is kind


# ----------------------------------------------------------------------
# Payload locators
# ----------------------------------------------------------------------


def test_a_locator_is_not_complete_until_it_says_so():
    """Dispatch is not finalisation; a manifest must not point at either yet."""
    locator = PayloadLocator(path='/data/tile.h5', detector='APDred')

    assert locator.complete is False


def test_a_locator_carries_what_a_filename_cannot():
    """Grouped containers need the group; a path alone does not find the data."""
    locator = PayloadLocator(
        path='/data/tile_000.h5', detector='APDred', group='scan0/APDred',
        axes='CZYX', stored_axes='TCZYX', shape=(2, 21, 273, 273),
        stored_shape=(1, 2, 21, 273, 273),
        generation=17, complete=True,
    )

    payload = locator.asdict()

    assert payload['group'] == 'scan0/APDred'
    assert payload['axes'] == 'CZYX'
    assert payload['stored_axes'] == 'TCZYX'
    assert payload['shape'] == [2, 21, 273, 273]
    assert payload['stored_shape'] == [1, 2, 21, 273, 273]
    assert payload['generation'] == 17
    assert payload['complete'] is True


def test_a_locator_is_hashable_so_it_can_be_compared_between_points():
    """Frozen: a run keeps one per tile and must not mutate them afterwards."""
    locator = PayloadLocator(path='/data/a.h5', detector='Camera')

    with pytest.raises(Exception):
        locator.path = '/data/b.h5'
