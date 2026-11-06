"""M3A: Session Review/Follow observation is opt-in and runtime-only.

These tests exercise Session mode policy without requiring real raw files or
launching the full ImProcess main window. Run with a Qt-capable environment.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")

from imswitch.improcess.controller.ProcessingSessionController import (
    ProcessingSessionController,
)
from imswitch.improcess.view.ProcessingSessionFrame import ProcessingSessionFrame


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)

    def connect(self, slot):
        self.calls.append(("connected", slot))


class _FakeWatcher:
    instances = []

    def __init__(self, root, parent=None):
        self.root = root
        self.sigEntryFound = _Signal()
        self.sigEntryChanged = _Signal()
        self.sigEntryRemoved = _Signal()
        self.started = False
        self.deleted = False
        self.__class__.instances.append(self)

    @property
    def running(self):
        return self.started

    def scan_once(self):
        return ["/tmp/old.zarr", "/tmp/unreadable.h5"]

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def deleteLater(self):
        self.deleted = True


def test_review_is_passive_and_follow_reconciles_baseline_once(monkeypatch):
    import imswitch.improcess.controller.ProcessingSessionController as module

    _FakeWatcher.instances = []
    monkeypatch.setattr(module, "DirectoryWatcher", _FakeWatcher)
    events = []
    session = SimpleNamespace(raw_root="/tmp", datasets=[SimpleNamespace(id="old")])

    def reconcile():
        events.append("reconcile")
        session.datasets.append(SimpleNamespace(id="new-from-review"))

    session.reconcile_all = reconcile
    fake = SimpleNamespace(
        _session=session,
        _sessionMode="review",
        _directoryWatcher=None,
        _widget=SimpleNamespace(
            setSessionMode=lambda mode: events.append(("ui", mode)),
            setStatus=lambda message: events.append(("status", message)),
        ),
        _watchPendingPaths=set(),
        _watchUnresolvedPaths=set(),
        _watchRetrySchedule=SimpleNamespace(track=lambda _path: None, clear=lambda: None),
        _watchRetryTimer=SimpleNamespace(stop=lambda: None),
        _watchNeedsFullReconcile=False,
        _watchAutoArrivals={}, _watchLapseChildren={},
        _watchReconcileTimer=SimpleNamespace(stop=lambda: None),
        _pathBelongsToResults=lambda path: False,
        _sessionKnowsRootEntry=lambda path: path == "/tmp/old.zarr",
        _render=lambda: events.append("render"),
        sigCatalogChanged=_Signal(),
        sigDatasetsAdded=_Signal(),
        _logger=SimpleNamespace(exception=lambda *args: None),
        _disableSessionAuto=lambda: events.append("auto-off"),
        _sync_auto_availability=lambda: events.append("auto-available"),
        _main=SimpleNamespace(_activeReconstructor=SimpleNamespace(result_role="derived")),
        _queue_controller=lambda: SimpleNamespace(
            setAutoEnabled=lambda enabled: events.append(("auto", enabled)),
        ),
        _admit_resolved_arrivals=lambda paths: None,
        _syncWatchRetryTimer=lambda: None,
    )
    fake._stopDirectoryWatcher = lambda: ProcessingSessionController._stopDirectoryWatcher(fake)
    fake._startDirectoryWatcher = lambda session: ProcessingSessionController._startDirectoryWatcher(fake, session)

    # Review never creates an observer or performs background scanning.
    assert fake._directoryWatcher is None
    ProcessingSessionController.setSessionMode(fake, "follow")
    assert fake._sessionMode == "follow"
    assert len(_FakeWatcher.instances) == 1
    assert fake._directoryWatcher.started
    assert events.count("reconcile") == 1
    assert fake._watchUnresolvedPaths == {"/tmp/unreadable.h5"}
    assert fake.sigDatasetsAdded.calls == [(('new-from-review',),)]
    assert "auto-off" in events
    assert ("auto", True) in events  # Monitoring now arms future Auto arrivals.
    assert not fake._watchAutoArrivals  # Nothing from the baseline is backfilled.

    # A repeated Follow selection neither restarts nor repeats reconciliation.
    ProcessingSessionController.setSessionMode(fake, "follow")
    assert len(_FakeWatcher.instances) == 1
    assert events.count("reconcile") == 1

    watcher = fake._directoryWatcher
    ProcessingSessionController.setSessionMode(fake, "review")
    assert fake._directoryWatcher is None
    assert watcher.deleted
    assert fake._sessionMode == "review"
    assert fake._watchUnresolvedPaths == set()


def test_review_ignores_stale_queued_watcher_events():
    fake = SimpleNamespace(
        _session=object(), _sessionMode="review",
        _watchPendingPaths=set(), _watchUnresolvedPaths=set(),
        _pathBelongsToResults=lambda path: False,
        _watchReconcileTimer=SimpleNamespace(start=lambda: pytest.fail("timer started")),
    )
    ProcessingSessionController._sessionPathFound(fake, "/tmp/later.h5")
    ProcessingSessionController._sessionPathChanged(fake, "/tmp/later.h5")
    ProcessingSessionController._sessionPathRemoved(fake, "/tmp/later.h5")
    assert fake._watchPendingPaths == set()


def test_auto_toggle_is_available_only_while_watching():
    calls = []
    queue = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=False),
        setAutoEnabled=lambda enabled: calls.append(("auto", enabled)),
        setProcessWhileAcquiring=lambda enabled: calls.append(("while", enabled)),
    )
    fake = SimpleNamespace(
        _session=object(), _sessionMode="review", _queue_controller=lambda: queue,
        _sync_auto_availability=lambda: None,
    )
    ProcessingSessionController.setAutoProcessingEnabled(fake, True)
    assert calls == [("auto", False), ("while", False)]
    calls.clear()
    fake._sessionMode = "follow"
    ProcessingSessionController.setAutoProcessingEnabled(fake, True)
    assert calls == [("auto", True)]


def test_session_watching_toggle_and_refresh_visibility(qtbot):
    frame = ProcessingSessionFrame()
    qtbot.addWidget(frame)
    assert not frame.watchToggle.isEnabled()
    frame.setSessionActive(True, "example")
    assert not frame.refreshBtn.isHidden()
    seen = []
    frame.sigSessionModeChanged.connect(seen.append)
    frame.watchToggle.setChecked(True)
    assert seen == ["follow"]
    frame.setSessionMode("follow")
    assert frame.watchToggle.isChecked()
    assert frame.refreshBtn.isHidden()
    frame.setSessionMode("review")
    assert not frame.watchToggle.isChecked()
    assert not frame.refreshBtn.isHidden()
    assert seen == ["follow"]  # controller update does not re-emit
    frame.setSessionActive(False)
    assert not frame.watchToggle.isEnabled()


def test_opening_session_resets_follow_without_persisting(monkeypatch, tmp_path):
    import imswitch.improcess.controller.ProcessingSessionController as module

    monkeypatch.setattr(module, "SessionResultStore", lambda *_args, **_kwargs: object())
    observed = []
    widget = SimpleNamespace(
        setSessionMode=lambda mode: observed.append(("mode", mode)),
        setSessionActive=lambda active, label="": observed.append(("active", active)),
        setStatus=lambda status: None,
    )
    session = SimpleNamespace(raw_root=tmp_path, results_root=tmp_path / "results", datasets=[])
    fake = SimpleNamespace(
        _sessionMode="follow", _session=object(), _saveFormat="tiff",
        _watchAutoArrivals={}, _watchLapseChildren={},
        _autoCalibrationSeenJobs=set(),
        _widget=widget, _stopDirectoryWatcher=lambda: observed.append(("stop",)),
        _disableSessionAuto=lambda: observed.append(("auto-off",)),
        _sync_auto_availability=lambda: observed.append(("auto-available",)),
        _loaded_result_signature=lambda: (), _rebuild_loaded_result_index=lambda: None,
        _main=SimpleNamespace(_currentDataObj=None),
        _sync_current_dataset=lambda _obj: None, _render=lambda: None,
        _followActiveReconstruction=False, _reconstruction=None,
        sigCatalogChanged=_Signal(),
    )
    ProcessingSessionController._activate_session(fake, session)
    assert fake._sessionMode == "review"
    assert fake._session is session
    assert ("mode", "review") in observed
    assert ("auto-off",) in observed
    assert ("active", True) in observed


def test_legacy_live_saving_remains_session_owned_in_review(tmp_path):
    from imswitch.improcess.controller.LiveModeController import LiveModeController

    root = tmp_path / "raw"
    root.mkdir()
    fake = SimpleNamespace(
        _session_controller=SimpleNamespace(session=SimpleNamespace(raw_root=root))
    )
    assert LiveModeController._root_has_processing_session(fake, str(root))
    assert not LiveModeController._root_has_processing_session(fake, str(tmp_path / "other"))
    fake._session_controller.session = None
    assert not LiveModeController._root_has_processing_session(fake, str(root))
