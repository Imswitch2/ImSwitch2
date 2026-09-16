"""The rendezvous between the tiling worker and the recording session.

Tiling owns the stage and knows when a tile's position is reached; the session
owns the scan and the writer, and advances on its own timers. Neither drives
the other, so the ordering between them has to work whichever arrives first —
and a point must never arm before its position is resolved, because nothing
downstream could tell that the stage was somewhere else.
"""

import threading

import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    _PointOutcome,
    _RecordingDispatcher,
)
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)


class _Manager:
    def __init__(self, locators=None):
        self._locators = locators or {}

    def payloadLocators(self, generation):
        return dict(self._locators)


class _Recording:
    def __init__(self, manager):
        self._master = type('M', (), {'recordingManager': manager})()


def _dispatcher(locators=None, timeout=5.0):
    return _RecordingDispatcher(None, _Recording(_Manager(locators)), timeout)


# ----------------------------------------------------------------------
# Ordering
# ----------------------------------------------------------------------


def test_the_session_asks_first_and_tiling_answers():
    """The ordinary order: the lapse reaches the point before the stage does."""
    dispatcher = _dispatcher()

    request = dispatcher.provider(0)
    assert not request.mayProceed, 'must not arm before the stage is there'

    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    outcome = dispatcher.runPoint(handed)

    assert request.mayProceed, 'the session was not released'
    assert isinstance(outcome, _PointOutcome)


def test_tiling_answers_first_and_the_session_takes_it():
    """The other order: the stage arrives before the lapse asks."""
    dispatcher = _dispatcher()

    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    dispatcher.runPoint(handed)

    request = dispatcher.provider(0)

    assert request is handed
    assert request.mayProceed


def test_a_point_is_never_released_by_an_unresolved_request():
    """A request tiling has not resolved must not let the point arm."""
    dispatcher = _dispatcher()
    request = dispatcher.provider(0)

    unresolved = PositioningRequest(0, timeout_s=5)
    dispatcher.runPoint(unresolved)

    assert not request.mayProceed


# ----------------------------------------------------------------------
# Terminals
# ----------------------------------------------------------------------


def test_a_finished_point_resolves_what_tiling_is_waiting_on():
    dispatcher = _dispatcher()
    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    outcome = dispatcher.runPoint(handed)

    dispatcher.onWriterFinalised(7)
    dispatcher.onPointFinished()

    assert outcome.wait(1) is True
    assert outcome.successful is True


def test_a_failed_point_says_so_rather_than_hanging():
    dispatcher = _dispatcher()
    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    outcome = dispatcher.runPoint(handed)

    dispatcher.onPointFailed('disk full', 7, 'writer')

    assert outcome.wait(1) is True
    assert outcome.successful is False
    assert 'disk full' in outcome.message


def test_a_point_is_not_finished_when_only_the_writer_has_drained():
    """Scan lifecycle cleanup is still running; the stage must not move yet."""
    dispatcher = _dispatcher()
    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    outcome = dispatcher.runPoint(handed)

    dispatcher.onWriterFinalised(7)

    assert outcome.wait(0.05) is False, 'released before the scan finished'

    dispatcher.onPointFinished()
    assert outcome.wait(1) is True


def test_locators_are_read_from_the_finalised_terminal():
    """Not guessed at dispatch: they only exist once the writer has drained."""
    dispatcher = _dispatcher(locators={'APDred': object()})

    assert dispatcher.locatorsForLastPoint() == {}

    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    dispatcher.runPoint(handed)
    dispatcher.onWriterFinalised(7)
    dispatcher.onPointFinished()

    assert list(dispatcher.locatorsForLastPoint()) == ['APDred']


def test_a_failed_point_reports_no_locators():
    """Nothing finalised, so there is nothing to point a manifest at."""
    dispatcher = _dispatcher(locators={'APDred': object()})
    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    dispatcher.runPoint(handed)
    dispatcher.onWriterFinalised(7)
    dispatcher.onPointFinished()

    dispatcher.runPoint(handed)
    dispatcher.onPointFailed('scan aborted', 8, 'scan')

    assert dispatcher.locatorsForLastPoint() == {}


def test_an_outcome_settles_once():
    outcome = _PointOutcome()
    outcome.resolve(True)
    outcome.resolve(False, 'late failure')

    assert outcome.successful is True
    assert outcome.message == ''


def test_the_worker_waiting_is_released_from_the_signal_thread():
    """Tiling waits on its worker; the terminal arrives on another thread."""
    dispatcher = _dispatcher()
    handed = PositioningRequest(0, timeout_s=5)
    handed.resolve()
    outcome = dispatcher.runPoint(handed)

    threading.Timer(0.02, lambda: (dispatcher.onWriterFinalised(1), dispatcher.onPointFinished())).start()

    assert outcome.wait(2) is True
    assert outcome.successful is True


# ----------------------------------------------------------------------
# Failure policy, paths and generation ownership
# ----------------------------------------------------------------------


def test_a_writer_failure_lets_the_run_continue():
    """The measurement happened; only the file did not."""
    dispatcher = _dispatcher()
    dispatcher.onPointFailed('could not serialise', 7, 'writer')

    assert dispatcher.lastFailureWasRecoverable('could not serialise') is True


@pytest.mark.parametrize('kind', ['acquisition', 'scan', 'hardware', 'unknown'])
def test_anything_but_a_writer_failure_stops_the_run(kind):
    dispatcher = _dispatcher()
    dispatcher.onPointFailed('something went wrong', 7, kind)

    assert dispatcher.lastFailureWasRecoverable('something went wrong') is False


@pytest.mark.parametrize('message', [
    'No space left on device', 'OSError: [Errno 28] No space left',
    'disk full', 'user quota exceeded',
])
def test_storage_exhaustion_is_not_carried_on_from(message):
    """The next tile would fail identically; grinding on produces only errors."""
    dispatcher = _dispatcher()
    dispatcher.onPointFailed(message, 7, 'writer')

    assert dispatcher.lastFailureWasRecoverable(message) is False


def test_locator_paths_are_recorded_relative_to_the_run_folder(tmp_path):
    """An absolute path breaks the moment the dataset is moved or archived."""
    from imswitch.imcontrol.model.managers.RecordingManager import PayloadLocator

    folder = tmp_path / 'tiling_run'
    inside = folder / 'payloads' / 'tile_000.h5'
    dispatcher = _RecordingDispatcher(
        None, _Recording(_Manager({'APDred': PayloadLocator(
            path=str(inside), detector='APDred', group='scan0/APDred',
        )})), 5.0, folder,
    )

    dispatcher.onWriterFinalised(3)

    assert dispatcher.locatorsForLastPoint()['APDred'].path == (
        'payloads/tile_000.h5'
    )


def test_a_locator_outside_the_run_folder_is_left_absolute(tmp_path):
    """Better an honest absolute path than a relative one that lies."""
    from imswitch.imcontrol.model.managers.RecordingManager import PayloadLocator

    elsewhere = tmp_path / 'somewhere_else' / 'tile.h5'
    dispatcher = _RecordingDispatcher(
        None, _Recording(_Manager({'Camera': PayloadLocator(
            path=str(elsewhere), detector='Camera',
        )})), 5.0, tmp_path / 'tiling_run',
    )

    dispatcher.onWriterFinalised(3)

    assert dispatcher.locatorsForLastPoint()['Camera'].path == str(elsewhere)


def test_each_point_reads_the_locators_of_its_own_generation():
    """Generation ownership: a point must not inherit the previous one's."""
    seen = []

    class _Tracking(_Manager):
        def payloadLocators(self, generation):
            seen.append(generation)
            return {'APDred': f'gen{generation}'}

    dispatcher = _RecordingDispatcher(None, _Recording(_Tracking()), 5.0)

    for generation in (11, 12):
        handed = PositioningRequest(0, timeout_s=5)
        handed.resolve()
        dispatcher.runPoint(handed)
        dispatcher.onWriterFinalised(generation)
        dispatcher.onPointFinished()

    assert seen == [11, 12]
    assert dispatcher.locatorsForLastPoint() == {'APDred': 'gen12'}


def test_a_cancelled_positioning_request_never_releases_the_point():
    """The operator stopped the run mid-move; nothing may be acquired."""
    dispatcher = _dispatcher()
    request = dispatcher.provider(0)

    cancelled = PositioningRequest(0, timeout_s=5)
    cancelled.cancel('operator stopped the run')
    dispatcher.runPoint(cancelled)

    assert not request.mayProceed
    assert request.settled, 'the session would otherwise poll forever'
    assert 'stopped' in request.message
