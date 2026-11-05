"""M3B: arrival-based Auto is independent of catalog refresh/backfill.

These are Qt/controller contract checks; run with qtpy/pytest-qt on the
ImSwitch Windows environment. Pure readiness tests remain separate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")

from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController
from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController
from imswitch.improcess.model.processing_queue import ExecutionRequirement
from imswitch.improcess.view.ProcessingSessionFrame import ProcessingSessionFrame


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def test_only_new_paths_while_auto_on_are_admitted(tmp_path):
    root = tmp_path
    earlier = root / "earlier.zarr"
    later = root / "later.zarr"
    emitted = Signal()
    queue = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=False),
        captureAutoRecipe=lambda: {
            "reconstructor_id": "monalisa",
            "params": {"localization_mode": "Auto"},
            "execution_requirement": "finite",
        },
    )
    session = SimpleNamespace(datasets=[])
    fake = SimpleNamespace(
        _sessionMode="follow", _session=session,
        _watchAutoArrivals={}, _watchUnresolvedPaths=set(), _watchPendingPaths=set(),
        _watchRetrySchedule=SimpleNamespace(track=lambda _path: None),
        _syncWatchRetryTimer=lambda: None,
        _queue_controller=lambda: queue,
        _pathBelongsToResults=lambda path: False,
        _sessionKnowsRootEntry=lambda path: False,
        _watchReconcileTimer=SimpleNamespace(start=lambda: None),
        sigAutoAcquisitionsAdmitted=emitted,
        _widget=SimpleNamespace(setStatus=lambda msg: None),
    )
    ProcessingSessionController._sessionPathFound(fake, str(earlier))
    queue.autoPolicy.enabled = True
    ProcessingSessionController._sessionPathFound(fake, str(later))
    queue.autoPolicy.enabled = False
    assert str(earlier) not in fake._watchAutoArrivals
    assert str(later) in fake._watchAutoArrivals
    # Metadata may resolve long after the operator disables Auto. The
    # admission decision is from the original discovery instant.
    session.datasets.append(SimpleNamespace(
        id="later", primary_path=str(later), source_refs=[],
    ))
    ProcessingSessionController._admit_resolved_arrivals(fake, [str(earlier), str(later)])
    assert len(emitted.calls) == 1
    assert emitted.calls[0][0] == ("later",)
    assert emitted.calls[0][1]["execution_requirement"] == "finite"
    ProcessingSessionController._admit_resolved_arrivals(fake, [str(later)])
    assert len(emitted.calls) == 1


def test_enabling_auto_does_not_enqueue_existing_catalog():
    seen = []
    fake = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=False),
        _processWhileAcquiring=False,
        enqueueAutoEligible=lambda *args: seen.append("BACKFILL"),
        _emit_changed=lambda: seen.append("refresh"),
    )
    ProcessingQueueController.setAutoEnabled(fake, True)
    assert seen == ["refresh"]
    assert fake.autoPolicy.enabled is True
    assert fake._processWhileAcquiring is False


def test_recipe_records_growing_policy_but_finished_job_is_finite():
    reconstructor = SimpleNamespace(id="robust", result_role="derived")
    fake = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=True),
        _processWhileAcquiring=True,
        _main=SimpleNamespace(_activeReconstructor=reconstructor),
        _snapshot_params=lambda recon: {"quality": "robust"},
    )
    recipe = ProcessingQueueController.captureAutoRecipe(fake)
    assert recipe["reconstructor_id"] == "robust"
    assert recipe["execution_requirement"] == ExecutionRequirement.FOLLOW.value
    fake._processWhileAcquiring = False
    recipe2 = ProcessingQueueController.captureAutoRecipe(fake)
    assert recipe2["execution_requirement"] == ExecutionRequirement.FINITE.value
    assert recipe["execution_requirement"] == ExecutionRequirement.FOLLOW.value


def test_old_auto_exclusion_not_shown_in_session_list(qtbot):
    frame = ProcessingSessionFrame()
    qtbot.addWidget(frame)
    frame.setSessionActive(True, "demo")
    assert frame.datasetList.columnCount() == 5
    assert not any("ignore" in action.text().lower()
                   for action in frame.actionsMenu.actions())
    frame.setSessionMode("review")
    assert not frame.refreshBtn.isHidden()
    frame.setSessionMode("follow")
    assert frame.refreshBtn.isHidden()


def test_open_session_starts_passive_without_startup_choice(tmp_path):
    candidate = SimpleNamespace(raw_root=tmp_path, results_root=tmp_path / "results")
    activated = []
    fake = SimpleNamespace(
        _session=None,
        _prepare_session=lambda *_args: candidate,
        _activate_session=lambda session: activated.append(session),
        # A legacy UI prompt must never be required to open a Session.
        _widget=SimpleNamespace(requestSessionWatchingChoice=lambda: pytest.fail("unexpected startup prompt")),
    )
    assert ProcessingSessionController.openSession(fake, tmp_path, tmp_path / "results")
    assert activated == [candidate]


def test_auto_availability_refreshes_without_dataset_selection(qtbot):
    from imswitch.improcess.view.ImProcessMainView import BtnFrame

    footer = BtnFrame()
    qtbot.addWidget(footer)
    footer.setContext("session")
    footer.setSessionActive(True)
    footer.setSelectionCount("session", 0)
    footer.setAutoProcessingAvailable(False)
    assert not footer.autoProcessCheck.isEnabled()
    footer.setAutoProcessingAvailable(True)
    assert footer.autoProcessCheck.isEnabled()
    footer.setAutoProcessingAvailable(False)
    assert not footer.autoProcessCheck.isEnabled()


def test_watching_status_and_switch_are_separate(qtbot):
    frame = ProcessingSessionFrame()
    qtbot.addWidget(frame)
    frame.setSessionActive(True, "demo")
    assert frame.watchStatusLabel.text() == "Monitor"
    assert not frame.watchToggle.isChecked()
    assert not frame.refreshBtn.isHidden()
    assert "palette(mid)" in frame.watchStatusDot.styleSheet()
    # Refresh belongs between the monitoring switch and Actions.
    header = frame.layout().itemAt(0).layout()
    assert header.indexOf(frame.watchingControls) < header.indexOf(frame.refreshBtn)
    assert header.indexOf(frame.refreshBtn) < header.indexOf(frame.actionsBtn)

    frame.watchToggle.setChecked(True)
    frame.setSessionMode("follow")
    assert frame.watchStatusLabel.text() == "Monitor"  # observer not yet confirmed
    assert "palette(mid)" in frame.watchStatusDot.styleSheet()
    frame.setWatchingActive(True)
    assert frame.watchStatusLabel.text() == "Monitor"
    assert "#27865d" in frame.watchStatusDot.styleSheet()
    assert frame.refreshBtn.isHidden()

    frame.setSessionMode("review")
    assert frame.watchStatusLabel.text() == "Monitor"
    assert "palette(mid)" in frame.watchStatusDot.styleSheet()
    assert not frame.watchToggle.isChecked()
    assert not frame.refreshBtn.isHidden()


class _TestTimer:
    def __init__(self):
        self.active = False
        self.starts = 0

    def isActive(self):
        return self.active

    def start(self):
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False



def _fake_watch_controller(tmp_path, *, auto_enabled=True):
    """Exercise real Session slots without creating the complete ImSwitch GUI."""
    from imswitch.improcess.live.discovery_retry import UnresolvedDiscoveryRetry

    emitted = Signal()
    queue = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=auto_enabled),
        captureAutoRecipe=lambda: {"reconstructor_id": "monalisa", "params": {"x": 1}},
    )
    session = SimpleNamespace(datasets=[])
    def reconcile_path(path, *, reconcile_saved_results=False):
        # A nested metadata file appears later; the top-level .zarr directory
        # need not change its stat signature when that happens.
        if not (tmp_path / "delayed.zarr" / "nested" / ".zattrs").exists():
            return False
        if not session.datasets:
            session.datasets.append(SimpleNamespace(
                id="ready", primary_path=str(path), source_refs=[],
            ))
        return True
    session.reconcile_path = reconcile_path
    session.reconcile_all = lambda: None

    fake = SimpleNamespace(
        _sessionMode="follow", _session=session,
        _directoryWatcher=object(), _watchAutoArrivals={},
        _watchUnresolvedPaths=set(), _watchPendingPaths=set(),
        _watchLapseChildren={}, _watchNeedsFullReconcile=False,
        _watchRetrySchedule=UnresolvedDiscoveryRetry(),
        _watchRetryTimer=_TestTimer(), _watchReconcileTimer=_TestTimer(),
        _queue_controller=lambda: queue,
        _pathBelongsToResults=lambda _path: False,
        _sessionKnowsRootEntry=lambda path: any(
            str(dataset.primary_path) == str(path) for dataset in session.datasets
        ),
        _render=lambda: None,
        _logger=SimpleNamespace(exception=lambda *_args: None),
        _widget=SimpleNamespace(setStatus=lambda _message: None),
        sigCatalogChanged=Signal(), sigDatasetsAdded=Signal(),
        sigAutoAcquisitionsAdmitted=emitted,
    )
    fake._syncWatchRetryTimer = lambda: ProcessingSessionController._syncWatchRetryTimer(fake)
    fake._sessionPathRemoved = lambda path: ProcessingSessionController._sessionPathRemoved(fake, path)
    fake._admit_resolved_arrivals = lambda paths: ProcessingSessionController._admit_resolved_arrivals(fake, paths)
    return fake, queue, session, emitted


def test_nested_zarr_becomes_visible_without_root_stat_change(tmp_path):
    import os

    store = tmp_path / "delayed.zarr"
    nested = store / "nested"
    nested.mkdir(parents=True)
    root_stat = store.stat()
    fake, _queue, session, emitted = _fake_watch_controller(tmp_path)
    ProcessingSessionController._sessionPathFound(fake, str(store))
    assert fake._watchRetryTimer.active
    ProcessingSessionController._flushWatcherReconcile(fake)
    assert not session.datasets
    assert str(store) in fake._watchUnresolvedPaths
    assert str(store) in fake._watchAutoArrivals

    (nested / ".zattrs").write_text("{}")
    # The update is nested, not an observable top-level .zarr stat change.
    os.utime(store, ns=(root_stat.st_atime_ns, root_stat.st_mtime_ns))
    assert store.stat().st_mtime_ns == root_stat.st_mtime_ns
    fake._watchRetrySchedule.track(str(store), now=0)  # already tracked; idempotent
    fake._watchRetrySchedule._entries[str(store)] = (0, 3.0)
    ProcessingSessionController._retryUnresolvedPaths(fake)
    assert str(store) in fake._watchPendingPaths
    ProcessingSessionController._flushWatcherReconcile(fake)

    assert [dataset.id for dataset in session.datasets] == ["ready"]
    assert fake.sigDatasetsAdded.calls == [(("ready",),)]
    assert len(emitted.calls) == 1
    assert emitted.calls[0][0] == ("ready",)
    assert str(store) not in fake._watchUnresolvedPaths
    assert not fake._watchRetrySchedule
    assert not fake._watchRetryTimer.active
    ProcessingSessionController._retryUnresolvedPaths(fake)
    assert len(emitted.calls) == 1


def test_retry_never_backfills_recording_discovered_before_auto_on(tmp_path):
    store = tmp_path / "delayed.zarr"
    (store / "nested").mkdir(parents=True)
    fake, queue, _session, emitted = _fake_watch_controller(tmp_path, auto_enabled=False)
    ProcessingSessionController._sessionPathFound(fake, str(store))
    queue.autoPolicy.enabled = True
    (store / "nested" / ".zattrs").write_text("{}")
    fake._watchRetrySchedule._entries[str(store)] = (0, 3.0)
    ProcessingSessionController._retryUnresolvedPaths(fake)
    ProcessingSessionController._flushWatcherReconcile(fake)
    assert not emitted.calls


def test_full_reconcile_does_not_drop_other_unresolved_zarr(tmp_path):
    store = tmp_path / "delayed.zarr"
    (store / "nested").mkdir(parents=True)
    fake, _queue, _session, _emitted = _fake_watch_controller(tmp_path)
    ProcessingSessionController._sessionPathFound(fake, str(store))
    assert str(store) in fake._watchAutoArrivals
    fake._watchNeedsFullReconcile = True  # another entry was removed
    ProcessingSessionController._flushWatcherReconcile(fake)
    assert str(store) in fake._watchUnresolvedPaths
    assert str(store) in fake._watchAutoArrivals
    assert fake._watchRetrySchedule
    assert fake._watchRetryTimer.active


def test_stopping_monitor_cancels_all_discovery_retries(tmp_path):
    store = tmp_path / "delayed.zarr"
    store.mkdir()
    fake, _queue, _session, _emitted = _fake_watch_controller(tmp_path)
    ProcessingSessionController._sessionPathFound(fake, str(store))
    fake._directoryWatcher = SimpleNamespace(stop=lambda: None, deleteLater=lambda: None)
    fake._widget.setWatchingActive = lambda _active: None
    ProcessingSessionController._stopDirectoryWatcher(fake)
    assert not fake._watchRetryTimer.active
    assert not fake._watchRetrySchedule
    assert not fake._watchUnresolvedPaths
