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
        for name, slot in (("sigExportWorkflowRequested", self.exportWorkflow),
                           ("sigRunWorkflowRequested", self.runWorkflow)):
            signal = getattr(mainView, name, None)
            if signal is not None:
                signal.connect(slot)

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
        report.close()

    @QtCore.Slot(str, object)
    def _onFailed(self, message: str, report) -> None:
        published = self._publish(report) if report is not None else 0
        self._logger.error("Workflow failed: %s", message)
        self._status(f"Workflow failed: {message}" + (f" ({published} partial result(s) added)" if published else ""))
        if report is not None:
            report.close()

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
