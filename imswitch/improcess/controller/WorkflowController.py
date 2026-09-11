"""Export the current result as a workflow, or run a workflow file.

Two thin hooks over :mod:`imswitch.improcess.workflows`. Export needs no
disk: the result in memory carries its provenance graph, which is the
workflow. Run happens on a worker thread and publishes each result the
workflow produced through ``sigResultProduced``, the same way every
reconstructor and processor does, so they land in the reconstruction list.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger


class _RunWorker(QtCore.QObject):
    sigFinished = QtCore.Signal(object)     # RunReport
    sigFailed = QtCore.Signal(str, object)  # message, RunReport or None

    def __init__(self, workflow, registry, out_dir, parent=None):
        super().__init__(parent)
        self._workflow = workflow
        self._registry = registry
        self._out_dir = out_dir
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the run to stop at the next step boundary (thread-safe: a flag)."""
        self._cancelled = True

    def _should_stop(self) -> bool:
        if self._cancelled:
            return True
        thread = QtCore.QThread.currentThread()
        return bool(thread is not None and thread.isInterruptionRequested())

    @QtCore.Slot()
    def run(self) -> None:
        from imswitch.improcess.workflows.runner import RunError, run

        try:
            report = run(
                self._workflow, registry=self._registry, out_dir=self._out_dir,
                cancel=self._should_stop,
            )
        except RunError as exc:
            self.sigFailed.emit(str(exc), getattr(exc, "report", None))
            return
        except Exception as exc:  # noqa: BLE001
            self.sigFailed.emit(str(exc), None)
            return
        self.sigFinished.emit(report)


#: Workflow threads that did not stop within the shutdown wait; referenced
#: here so they are not destroyed while running, released when they finish.
_ORPHANED_RUNS: list = []


class WorkflowController(QtCore.QObject):
    def __init__(self, commChannel, mainView, reconstructionController, *,
                 processing_config=None, parent=None):
        super().__init__(parent)
        self._logger = initLogger(self)
        self._commChannel = commChannel
        self._mainView = mainView
        self._reconstructionController = reconstructionController
        self._config = dict(processing_config or {})
        self._thread = None
        self._worker = None
        # Source handles behind published results: one entry per run,
        # ``(result uids it published, handles)``. Released when every one of
        # those results has left the reconstruction list, or at shutdown.
        self._retained: list[tuple[frozenset, list]] = []
        # Callables returning result uids some other owner still reads from
        # (napari endpoint sessions hold lazy layers over a result's data);
        # a retained source is released only when none of them holds it.
        self._holders: list = []
        for name, slot in (("sigExportWorkflowRequested", self.exportWorkflow),
                           ("sigRunWorkflowRequested", self.runWorkflow)):
            signal = getattr(mainView, name, None)
            if signal is not None:
                signal.connect(slot)
        changed = getattr(commChannel, "sigResultsChanged", None)
        if changed is not None:
            changed.connect(self.releaseUnusedSources)
        closing = getattr(mainView, "sigClosing", None)
        if closing is not None:
            closing.connect(self.shutdown)

    # -- export ---------------------------------------------------------------

    def exportWorkflow(self, path=None):
        """Write the current result's provenance as a workflow file."""
        from imswitch.improcess.model.provenance import graph_of
        from imswitch.improcess.model.save_protocol import ProvenanceDocument
        from imswitch.improcess.workflows.replay import ReplayError, workflow_from_provenance
        from imswitch.improcess.workflows.runtime import bootstrap_registry

        result = self._reconstructionController.getActiveResult()
        if result is None:
            self._status("No result selected.")
            return None
        graph = graph_of(result)
        if graph is None:
            self._status(f"'{getattr(result, 'name', 'result')}' carries no provenance to export.")
            return None
        try:
            replay = workflow_from_provenance(
                ProvenanceDocument(graph=graph), registry=bootstrap_registry(self._config),
                name=f"workflow-{_slug(getattr(result, 'name', 'result'))}",
            )
        except ReplayError as exc:
            self._status(f"Cannot export a runnable workflow: {exc.reasons[0] if exc.reasons else exc}")
            self._logger.warning("Workflow export refused:\n%s", exc)
            return None
        if path is None:
            path, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self._mainView, "Export workflow", f"{replay.workflow.name}.yaml",
                "Workflow (*.yaml *.yml *.json)",
            )
            if not path:
                return None
        written = replay.workflow.save(Path(path))
        for warning in replay.warnings:
            self._logger.warning("Workflow export: %s", warning)
        self._status(f"Wrote workflow {written.name} ({len(replay.workflow.steps)} steps).")
        return written

    # -- run --------------------------------------------------------------------

    def runWorkflow(self, path=None, out_dir=None):
        """Run a workflow file on a worker thread; results are published."""
        from imswitch.improcess.workflows.runtime import bootstrap_registry
        from imswitch.improcess.workflows.steps import Workflow, WorkflowError, validate

        if self._thread is not None:
            self._status("A workflow is already running.")
            return False
        if path is None:
            path, _filter = QtWidgets.QFileDialog.getOpenFileName(
                self._mainView, "Run workflow", "", "Workflow (*.yaml *.yml *.json)",
            )
            if not path:
                return False
        try:
            workflow = Workflow.load(path)
        except (WorkflowError, OSError, ValueError) as exc:
            self._status(f"Could not read workflow: {exc}")
            return False
        registry = bootstrap_registry(self._config)
        issues = validate(workflow, registry)
        if issues:
            self._status(f"Workflow invalid: {issues[0]}")
            self._logger.warning("Workflow invalid:\n  %s", "\n  ".join(str(i) for i in issues))
            return False
        if out_dir is None:
            out_dir = QtWidgets.QFileDialog.getExistingDirectory(self._mainView, "Output directory for saves")
            if not out_dir:
                return False

        self._thread = QtCore.QThread()
        self._worker = _RunWorker(workflow, registry, Path(out_dir))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.sigFinished.connect(self._onFinished)
        self._worker.sigFailed.connect(self._onFailed)
        for signal in (self._worker.sigFinished, self._worker.sigFailed):
            signal.connect(self._thread.quit)
        self._thread.finished.connect(self._clear)
        self._status(f"Running workflow {workflow.name}…")
        self._thread.start()
        return True

    @QtCore.Slot(object)
    def _onFinished(self, report) -> None:
        published = self._publish(report)
        files = [str(f) for receipt in report.receipts for f in receipt.files]
        self._status(
            f"Workflow {report.workflow} done: {published} result(s) added"
            + (f", wrote {len(files)} file(s)" if files else "")
        )
        self._retain(report)

    @QtCore.Slot(str, object)
    def _onFailed(self, message: str, report) -> None:
        published = self._publish(report) if report is not None else 0
        self._logger.error("Workflow failed: %s", message)
        self._status(f"Workflow failed: {message}" + (f" ({published} partial result(s) added)" if published else ""))
        if report is not None:
            self._retain(report)

    def _retain(self, report) -> None:
        """Keep the sources the published results may still read from.

        A view-only result is a lazy view over the file it came from; closing
        the report would close that handle under a result that is now in the
        reconstruction list. The handles are transferred to this controller
        and released once none of the results this run published is loaded
        any more (:meth:`releaseUnusedSources`), or at shutdown.
        """
        from imswitch.improcess.model.result import ProcessingResult

        uids = frozenset(
            str(getattr(result, "result_uid", "") or id(result))
            for result in report.results.values() if isinstance(result, ProcessingResult)
        )
        handles = report.detach_sources()
        if handles:
            self._retained.append((uids, handles))

    def releaseUnusedSources(self) -> int:
        """Close the handles of runs whose published results are all gone.

        Connected to the comm channel's ``sigResultsChanged``; returns how
        many runs' handles were released.
        """
        held = self._heldResultUids()
        if held is None:
            return 0
        keep, released = [], 0
        for uids, handles in self._retained:
            if uids & held:
                keep.append((uids, handles))
                continue
            self._closeHandles(handles)
            released += 1
        self._retained = keep
        return released

    def addHolder(self, holder, changed=None) -> None:
        """Register a zero-argument callable returning result uids still in use.

        The napari endpoint controller registers its sessions' result uids,
        so a source is not closed under a layer that still reads from it.
        ``changed`` is a signal (or anything with ``connect``) the holder
        emits when what it holds changes; a release check runs on it, so a
        source whose last reader was an endpoint session is closed when
        that session closes, not at the next unrelated list event.
        """
        self._holders.append(holder)
        connect = getattr(changed, "connect", None)
        if callable(connect):
            connect(self.releaseUnusedSources)

    def shutdown(self, wait_ms: int = 5000) -> bool:
        """Stop a running workflow and release the retained source handles.

        Cancellation is requested (the runner checks between steps), the
        thread is joined for at most ``wait_ms``, and only then are handles
        closed: closing them under a running step would fail that step in
        the middle of a read. Handles a registered holder still leases (an
        export that outlived the endpoint controller's own wait) stay open.
        Returns False when the run did not stop in time; the thread and
        worker are then parked so they are not destroyed while running, and
        every handle stays open.
        """
        stopped = self.cancelRun(wait_ms)
        if not stopped:
            self._logger.warning("A workflow is still running after %d ms; its sources stay open", wait_ms)
            return False
        held = self._heldResultUids() or set()
        keep = []
        for uids, handles in self._retained:
            if uids & held:
                keep.append((uids, handles))
                self._logger.warning("Sources of a workflow run stay open: %s still reads them", sorted(uids & held))
                continue
            self._closeHandles(handles)
        self._retained = keep
        return True

    def cancelRun(self, wait_ms: int = 5000) -> bool:
        """Ask a running workflow to stop and wait for its thread; True when idle.

        ``thread.quit()`` is called here directly: the worker's completion
        signal would queue it onto this (GUI) thread, which is blocked in
        ``wait()``, so relying on it would deadlock until the timeout.
        """
        thread, worker = self._thread, self._worker
        if thread is None:
            return True
        if worker is not None:
            worker.cancel()
        try:
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(int(wait_ms)):
                # Keep both alive beyond this controller: destroying a
                # running QThread takes the process down with it.
                _ORPHANED_RUNS.append((thread, worker))
                return False
        except Exception:
            self._logger.debug("Could not join the workflow thread", exc_info=True)
            return False
        return True

    def _heldResultUids(self):
        """Uids of every result still in use: loaded in the list (with their
        lineage, since a derived result may read through its parent's
        handle) plus whatever registered holders report."""
        getter = getattr(self._commChannel, "getAllResults", None)
        if not callable(getter):
            return None
        try:
            items = getter()
        except Exception:
            return None
        uids = set()
        for item in items or []:
            result = item[1] if isinstance(item, tuple) and len(item) == 2 else item
            if result is not None:
                uids.add(str(getattr(result, "result_uid", "") or id(result)))
                uids.update(str(u) for u in (getattr(result, "lineage", ()) or ()))
        for holder in self._holders:
            try:
                uids.update(str(u) for u in (holder() or ()))
            except Exception:
                self._logger.debug("A result holder failed; keeping sources open", exc_info=True)
                return None
        return uids

    def _closeHandles(self, handles) -> None:
        from imswitch.improcess.workflows.sources import close_source

        for handle in handles:
            try:
                close_source(handle)
            except Exception:
                self._logger.debug("Could not close a retained source", exc_info=True)

    def _publish(self, report) -> int:
        from imswitch.improcess.model.result import ProcessingResult

        count = 0
        for key, result in report.results.items():
            if isinstance(result, ProcessingResult):
                self._commChannel.sigResultProduced.emit(result, f"{report.workflow}:{key}")
                count += 1
        return count

    def _clear(self) -> None:
        entry = (self._thread, self._worker)
        if entry in _ORPHANED_RUNS:
            _ORPHANED_RUNS.remove(entry)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def _status(self, message: str) -> None:
        show = getattr(self._mainView, "showStatusMessage", None)
        if callable(show):
            show(message)
        self._logger.info(message)


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_\-]+", "-", str(text)).strip("-") or "result"


__all__ = ["WorkflowController"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
