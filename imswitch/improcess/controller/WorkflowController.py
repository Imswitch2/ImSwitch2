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

    @QtCore.Slot()
    def run(self) -> None:
        from imswitch.improcess.workflows.runner import RunError, run

        try:
            report = run(self._workflow, registry=self._registry, out_dir=self._out_dir)
        except RunError as exc:
            self.sigFailed.emit(str(exc), getattr(exc, "report", None))
            return
        except Exception as exc:  # noqa: BLE001
            self.sigFailed.emit(str(exc), None)
            return
        self.sigFinished.emit(report)


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
        loaded = self._loadedResultUids()
        if loaded is None:
            return 0
        keep, released = [], 0
        for uids, handles in self._retained:
            if uids & loaded:
                keep.append((uids, handles))
                continue
            self._closeHandles(handles)
            released += 1
        self._retained = keep
        return released

    def shutdown(self) -> None:
        """Release every retained source handle (window closing)."""
        for _uids, handles in self._retained:
            self._closeHandles(handles)
        self._retained = []

    def _loadedResultUids(self):
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
