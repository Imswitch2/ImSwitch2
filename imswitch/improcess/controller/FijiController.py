"""Plugins > Fiji: start Fiji, send results to it, take its images back.

Fiji runs in its own process (:mod:`imswitch.improcess.fiji.session`). Every
call to the session is synchronous and can take seconds, so all of them run on
ONE worker thread, strictly in order; everything that touches a widget, a
result list or a signal runs on the GUI thread. The worker hands each job's
outcome back through a queued signal.

This module imports no ``imagej``: only the pure Fiji modules and Qt. Whether
Fiji can be started is decided by
:func:`~imswitch.improcess.fiji.preferences.availability`.
"""

from __future__ import annotations

import itertools
import sys
import threading
from dataclasses import replace
from typing import Callable

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.fiji.convert import (
    PROP_RESULT_UID,
    NotSendable,
    import_fiji_image,
    plan_send,
)
from imswitch.improcess.fiji.preferences import (
    FijiPreferences,
    availability,
    launch_config,
    load_fiji_preferences,
    save_fiji_preferences,
)
from imswitch.improcess.fiji.protocol import FijiLinkError, HostError
from imswitch.improcess.fiji.tracker import GRID_POLICY_VERSION, grid_preserved, history_for
from imswitch.improcess.fiji.session import (
    STOP_TIMEOUT_S,
    FijiDied,
    FijiSession,
    FijiStartError,
)
from imswitch.improcess.model.napari_import import NotImportable
from imswitch.improcess.view.FijiImportDialog import FijiImportDialog

#: Time allowed, on top of the session's own stop bound, for the worker to
#: finish a stop and for its thread to exit when ImProcess closes. The stop
#: itself is bounded by STOP_TIMEOUT_S; what remains is removing the temp
#: files and the thread's event loop returning, which take milliseconds.
#: 5 s is far above that and keeps closing under ~15 s in the worst case.
CLOSE_MARGIN_S = 5.0

_MS_PER_S = 1000

#: Worker threads that outlived the close wait. Kept referenced here so a
#: running QThread is never destroyed under its worker.
_ORPHANED_THREADS: list = []

_STATUS_TEXT = {
    "stopped": "Fiji: not running",
    "starting": "Fiji: starting…",
    "dead": "Fiji: stopped unexpectedly",
    "failed": "Fiji: failed to start",
}


class _FijiWorker(QtCore.QObject):
    """Runs jobs one at a time on its thread and reports each outcome."""

    sigRun = QtCore.Signal(int, str, object)       # ticket, tag, callable
    sigDone = QtCore.Signal(int, str, object)      # ticket, tag, value
    sigFailed = QtCore.Signal(int, str, object)    # ticket, tag, the exception

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sigRun.connect(self.run)

    @QtCore.Slot(int, str, object)
    def run(self, ticket: int, tag: str, job: Callable) -> None:
        try:
            value = job()
        except Exception as exc:  # noqa: BLE001 - reported to the GUI thread
            self.sigFailed.emit(ticket, tag, exc)
            return
        self.sigDone.emit(ticket, tag, value)


def _inline_runner(tag, job, on_done, on_failed):
    """Run ``job`` here and now: the runner the unit tests inject."""
    try:
        value = job()
    except Exception as exc:  # noqa: BLE001
        on_failed(exc)
    else:
        on_done(value)


def resolve_source(image_id, desc, sent_ids, sent_results: dict, results_by_uid: dict):
    """The ImProcess result a Fiji image comes from: sent, else lineage hint, else None."""
    if image_id in sent_ids and sent_results.get(image_id) is not None:
        return sent_results[image_id]
    uid = desc.properties.get(PROP_RESULT_UID, "") if desc is not None else ""
    return results_by_uid.get(uid) if uid else None


def sent_record_for(image_id, desc, sent: dict):
    """The send an image belongs to: its own, else the send its lineage hint names.

    A duplicate made in Fiji carries the sent image's uid hint but has no send
    of its own; borrowing that send's axes lets it come back in the source's
    axis order instead of ImageJ's. It only shapes the axis order --
    ``to_snapshot`` still checks that the extra axes are size 1, and the grid
    is decided separately.
    """
    if image_id in sent:
        return sent[image_id]
    uid = desc.properties.get(PROP_RESULT_UID, "") if desc is not None else ""
    return next((record for record in sent.values() if uid and record.result_uid == uid), None)


class FijiController(QtCore.QObject):
    """Owns the Plugins > Fiji menu, the session and its worker thread."""

    def __init__(
        self,
        commChannel,
        mainView,
        reconstructionController,
        *,
        preferences_path=None,
        session_factory=FijiSession,
        job_runner=None,
        find_spec=None,
        import_dialog_factory=FijiImportDialog,
        parent=None,
    ):
        super().__init__(parent)
        self._logger = initLogger(self)
        self._commChannel = commChannel
        self._mainView = mainView
        self._reconstructionController = reconstructionController
        self._preferencesPath = preferences_path
        self._sessionFactory = session_factory
        self._findSpec = find_spec
        self._dialogFactory = import_dialog_factory
        self._jobRunner = job_runner or self._runThreaded
        self._session = None
        self._starting = False
        self._sentResults: dict = {}
        self._imported: set = set()
        self._thread = None
        self._worker = None
        self._pending: dict = {}
        self._tickets = itertools.count(1)
        self._closed = False
        self._menu = None

        menu_getter = getattr(mainView, "fijiMenu", None)
        if callable(menu_getter):
            self._menu = menu_getter()
            if self._menu is not None:
                self._menu.aboutToShow.connect(self.rebuildMenu)
        closing = getattr(mainView, "sigClosing", None)
        if closing is not None:
            closing.connect(self.close)

    # -- state ---------------------------------------------------------------

    def preferences(self) -> FijiPreferences:
        return load_fiji_preferences(self._preferencesPath)

    def _availability(self, prefs=None):
        kwargs = {"find_spec": self._findSpec} if self._findSpec is not None else {}
        return availability(prefs or self.preferences(), **kwargs)

    def state(self) -> str:
        """``stopped``, ``starting``, ``ready``, ``failed`` or ``dead``."""
        if self._starting:
            return "starting"
        return self._session.state if self._session is not None else "stopped"

    def statusLine(self) -> str:
        state = self.state()
        if state == "ready":
            ping = self._session.ping_info
            return f"Fiji: ImageJ {ping.imagej} (Java {ping.java})" if ping else "Fiji: ready"
        return _STATUS_TEXT.get(state, f"Fiji: {state}")

    # -- menu ----------------------------------------------------------------

    def rebuildMenu(self) -> None:
        menu = self._menu
        if menu is None:
            return
        menu.clear()
        state = self.state()
        ready = state == "ready"
        running = state in ("starting", "ready")
        ok, reason = self._availability()
        result = self._currentResult()

        status = menu.addAction(self.statusLine())
        status.setEnabled(False)
        menu.addSeparator()

        start = menu.addAction("Start Fiji")
        start.setEnabled(ok and not running)
        start.setToolTip(reason if not ok else "Start Fiji in its own process")
        start.triggered.connect(lambda _checked=False: self.startFiji())

        send = menu.addAction("Send to Fiji")
        send.setEnabled(ok and result is not None)
        send.setToolTip(
            reason if not ok else
            "Select a result first" if result is None else
            f"Open {result.name} in Fiji (starts Fiji if needed)"
        )
        send.triggered.connect(lambda _checked=False: self.sendCurrent())

        choose = menu.addAction("Import from Fiji…")
        choose.setEnabled(ready)
        choose.setToolTip("Choose which open Fiji images to import" if ready else "Start Fiji first")
        choose.triggered.connect(lambda _checked=False: self.importFromFiji())

        new = menu.addAction("Import new images")
        new.setEnabled(ready)
        new.setToolTip(
            "Import every open Fiji image that was not sent from ImProcess and not imported yet"
            if ready else "Start Fiji first"
        )
        new.triggered.connect(lambda _checked=False: self.importNewImages())

        stop = menu.addAction("Stop Fiji")
        stop.setEnabled(running)
        stop.setToolTip("Close Fiji and its process" if running else "Fiji is not running")
        stop.triggered.connect(lambda _checked=False: self.stopFiji())

        menu.addSeparator()
        location = menu.addAction("Fiji location…")
        location.setToolTip(f"Currently: {self.preferences().fijiDir or 'not set'}")
        location.triggered.connect(lambda _checked=False: self.chooseLocation())

    # -- jobs ----------------------------------------------------------------

    def _submit(self, tag: str, job: Callable, on_done: Callable, on_failed: Callable | None = None) -> None:
        self._jobRunner(tag, job, on_done, on_failed or self._onFailed)

    def _runThreaded(self, tag, job, on_done, on_failed) -> None:
        if self._worker is None:
            self._startThread()
        ticket = next(self._tickets)
        self._pending[ticket] = (on_done, on_failed)
        self._worker.sigRun.emit(ticket, tag, job)

    def _startThread(self) -> None:
        self._thread = QtCore.QThread()
        self._worker = _FijiWorker()
        self._worker.moveToThread(self._thread)
        self._worker.sigDone.connect(self._onWorkerDone)
        self._worker.sigFailed.connect(self._onWorkerFailed)
        self._thread.start()

    @QtCore.Slot(int, str, object)
    def _onWorkerDone(self, ticket: int, tag: str, value) -> None:
        callbacks = self._pending.pop(ticket, None)
        if callbacks is not None:
            self._safely(callbacks[0], value)

    @QtCore.Slot(int, str, object)
    def _onWorkerFailed(self, ticket: int, tag: str, error) -> None:
        callbacks = self._pending.pop(ticket, None)
        if callbacks is not None:
            self._safely(callbacks[1], error)

    def _safely(self, callback, argument) -> None:
        try:
            callback(argument)
        except Exception:  # noqa: BLE001 - one bad callback must not stop the queue
            self._logger.exception("A Fiji job's follow-up failed")

    def _ensureReady(self) -> FijiSession:
        """Worker thread: a ready session, starting a fresh host when there is none."""
        session = self._session
        if session is not None and session.state == "ready":
            return session
        if session is not None:
            session.stop()                    # tidy a dead or failed one before replacing it
        prefs = self.preferences()
        config = launch_config(prefs)
        session = self._sessionFactory(config, python=prefs.fijiPython or sys.executable)
        self._session = session
        session.start()
        return session

    def _markStarting(self) -> None:
        """GUI thread: a start is about to be queued. Image ids do not survive a new host."""
        if self.state() != "ready":
            self._starting = True
            self._sentResults.clear()
            self._imported.clear()

    # -- start / stop ----------------------------------------------------------

    def startFiji(self) -> None:
        ok, reason = self._availability()
        if not ok:
            self._status(reason)
            return
        if self.state() in ("starting", "ready"):
            return
        self._markStarting()
        self._submit("start", self._ensureReady, self._onStarted, self._onFailed)

    def _onStarted(self, session) -> None:
        self._starting = False
        self._status(self.statusLine())

    def stopFiji(self) -> None:
        def job():
            if self._session is not None:
                self._session.stop()

        self._submit("stop", job, self._onStopped)

    def _onStopped(self, _value) -> None:
        self._starting = False
        self._sentResults.clear()
        self._imported.clear()
        self._status("Fiji stopped.")

    # -- sending ------------------------------------------------------------------

    def sendCurrent(self, result=None) -> None:
        result = result if result is not None else self._currentResult()
        if result is None:
            self._status("No result selected.")
            return
        ok, reason = self._availability()
        if not ok:
            self._status(reason)
            return
        try:
            plan = plan_send(result)
        except NotSendable as exc:
            self._status(f"{result.name} cannot be sent to Fiji: {exc}")
            return
        self._markStarting()

        def job():
            return self._ensureReady().send(plan)

        def done(image_id):
            self._starting = False
            self._sentResults[image_id] = result
            self._status(f"Sent {result.name} to Fiji (image {image_id}).")

        self._submit("send", job, done, self._onFailed)

    # -- importing ----------------------------------------------------------------

    def importFromFiji(self) -> None:
        """List the open images, let the user choose, import the chosen."""
        self._submit("list", lambda: self._session.list_images(), self._onListedForChoice)

    def importNewImages(self) -> None:
        self._submit("list", lambda: self._session.list_images(), self._onListedForNew)

    def _newIds(self, descriptions) -> list:
        sent = set(self._session.sent) if self._session is not None else set()
        return [d.image_id for d in descriptions if d.image_id not in sent and d.image_id not in self._imported]

    def _onListedForNew(self, descriptions) -> None:
        ids = self._newIds(descriptions)
        if not ids:
            self._status("No new images in Fiji.")
            return
        self._importIds(ids)

    def _onListedForChoice(self, descriptions) -> None:
        ids = self._chooseImages(descriptions)
        if ids:
            self._importIds(ids)

    def _chooseImages(self, descriptions) -> list:
        """Open the chooser (modal) and return the image ids the user picked."""
        session = self._session
        dialog = self._dialogFactory(
            descriptions, sent=dict(session.sent), imported=set(self._imported),
            source_names=self._sourceNames(descriptions), parent=self._dialogParent(),
        )

        def refresh():
            def done(fresh):
                try:
                    dialog.setDescriptions(
                        fresh, sent=dict(session.sent), imported=set(self._imported),
                        source_names=self._sourceNames(fresh),
                    )
                except RuntimeError:       # the dialog closed while the list was on its way
                    pass

            self._submit("list", lambda: self._session.list_images(), done)

        dialog.sigRefreshRequested.connect(refresh)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return []
        return dialog.selectedIds()

    def _sourceNames(self, descriptions) -> dict:
        """Image id -> name of the ImProcess result it comes from, None when unknown."""
        by_uid = self._resultsByUid()
        sent_ids = set(self._session.sent) if self._session is not None else set()
        names = {}
        for desc in descriptions:
            source = resolve_source(desc.image_id, desc, sent_ids, self._sentResults, by_uid)
            names[desc.image_id] = getattr(source, "name", None) if source is not None else None
        return names

    def _resultsByUid(self) -> dict:
        return {uid: r for r in self._allResults() if (uid := getattr(r, "result_uid", ""))}

    def _importIds(self, ids) -> None:
        results_by_uid = self._resultsByUid()
        sent_results = dict(self._sentResults)
        for image_id in ids:
            self._submit(
                "import", self._importJob(image_id, sent_results, results_by_uid),
                self._onImported, lambda exc, i=image_id: self._onImportFailed(i, exc),
            )

    def _importJob(self, image_id, sent_results, results_by_uid):
        def job():
            session = self._session
            data, desc = session.fetch(image_id)
            source = resolve_source(image_id, desc, session.sent, sent_results, results_by_uid)
            ping = session.ping_info
            # The host's log is cumulative (never cleared here), so every
            # import reads the whole of what Fiji did since it started.
            log = session.events()
            history = history_for(image_id, log["events"], log["recorder"], sent_ids=set(session.sent))
            preserves, reason = grid_preserved(history)
            details = {"imagej": ping.imagej, "java": ping.java} if ping else {}
            details.update({
                "fiji_history": history.to_details(),
                "grid_policy": GRID_POLICY_VERSION,
                "grid_policy_reason": reason,
            })
            imported = import_fiji_image(
                desc, data, source,
                sent=sent_record_for(image_id, desc, session.sent),
                preserves_grid=preserves,
                details=details,
                history_details=history.to_details(),
            )
            return image_id, imported

        return job

    def _onImported(self, value) -> None:
        image_id, imported = value
        result = imported.result
        if result is None:
            self._status(f"Fiji image {image_id} produced nothing to import.")
            return
        self._commChannel.sigResultProduced.emit(result, result.name)
        self._imported.add(image_id)
        fiji = result.metadata.get("fiji") or {}
        notes = len(fiji.get("dropped") or ())
        suffix = f" ({notes} metadata notes; see Metadata)" if notes else ""
        history = fiji.get("history")
        if history is None:
            self._status(f"Imported {result.name} from Fiji{suffix}.")
            return
        steps = len(history["steps"])
        self._status(
            f"Imported {result.name} from Fiji \N{EM DASH} {steps} Fiji step{'' if steps == 1 else 's'}, "
            f"history {history['completeness']}{suffix}."
        )

    def _onImportFailed(self, image_id, error) -> None:
        if isinstance(error, (NotImportable, HostError)) and not isinstance(error, FijiDied):
            self._logger.warning("Could not import Fiji image %s: %s", image_id, error)
            self._status(f"Could not import Fiji image {image_id}: {error}")
            return
        self._onFailed(error)

    # -- failures --------------------------------------------------------------------

    def _onFailed(self, error) -> None:
        self._starting = False
        if isinstance(error, FijiStartError):
            self._logger.warning("Fiji could not be started: %s", error)
            QtWidgets.QMessageBox.warning(self._dialogParent(), "Fiji", str(error))
            self._status("Fiji failed to start.")
        elif isinstance(error, FijiDied):
            self._sentResults.clear()
            self._imported.clear()
            self._logger.warning("Fiji stopped unexpectedly: %s", error)
            self._status("Fiji stopped unexpectedly.")
        elif isinstance(error, (FijiLinkError, NotImportable)):
            self._logger.warning("Fiji: %s", error)
            self._status(f"Fiji: {error}")
        else:
            self._logger.error("Fiji job failed", exc_info=error)
            self._status(f"Fiji: {type(error).__name__}: {error}")

    # -- location --------------------------------------------------------------------

    def chooseLocation(self) -> None:
        prefs = self.preferences()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self._dialogParent(), "Choose the Fiji folder", prefs.fijiDir or ""
        )
        if not chosen:
            return
        try:
            config = launch_config(replace(prefs, fijiDir=chosen))
        except FijiLinkError as exc:
            QtWidgets.QMessageBox.warning(self._dialogParent(), "Fiji location", str(exc))
            return
        try:
            save_fiji_preferences(replace(prefs, fijiDir=str(config.fiji_dir)), self._preferencesPath)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self._dialogParent(), "Fiji location", f"The location could not be saved: {exc}"
            )
            return
        message = f"Fiji location set to {config.fiji_dir}."
        if self.state() in ("starting", "ready"):
            message += " It takes effect the next time Fiji is started."
        self._status(message)

    # -- closing ---------------------------------------------------------------------

    def close(self) -> None:
        """Stop the session and the worker thread, bounded; never leave a host behind."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        thread, worker = self._thread, self._worker
        if thread is None:
            if self._session is not None:
                self._session.stop()
            return
        stopped = threading.Event()

        def job():
            try:
                if self._session is not None:
                    self._session.stop()
            finally:
                stopped.set()

        worker.sigRun.emit(0, "close", job)
        stopped.wait(STOP_TIMEOUT_S + CLOSE_MARGIN_S)
        thread.quit()
        if not thread.wait(int(CLOSE_MARGIN_S * _MS_PER_S)):
            self._logger.warning("The Fiji worker thread is still running after closing; leaving it to finish")
            _ORPHANED_THREADS.append((thread, worker))
        self._thread = self._worker = None

    # -- helpers ---------------------------------------------------------------------

    def _dialogParent(self):
        return self._mainView if isinstance(self._mainView, QtWidgets.QWidget) else None

    def _currentResult(self):
        getter = getattr(self._reconstructionController, "getActiveResult", None)
        return getter() if callable(getter) else None

    def _allResults(self) -> list:
        try:
            items = self._commChannel.getAllResults()
        except Exception:  # noqa: BLE001
            items = []
        results = [item[1] if isinstance(item, tuple) and len(item) == 2 else item for item in items]
        return [r for r in results if r is not None]

    def _status(self, message: str) -> None:
        show = getattr(self._mainView, "showStatusMessage", None)
        if callable(show):
            show(message)
        self._logger.info(message)


__all__ = ["FijiController", "CLOSE_MARGIN_S", "resolve_source", "sent_record_for"]
