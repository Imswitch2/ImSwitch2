from pathlib import Path

from qtpy import QtCore

from .basecontrollers import ImProcessWidgetController


class _WidefieldStarssBatchWorker(QtCore.QObject):
    progress = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal(str)

    def __init__(self, input_folder, output_folder, params, parent=None,
                 h_suffix="_h", v_suffix="_v"):
        super().__init__(parent)
        self._input_folder = input_folder
        self._output_folder = output_folder
        self._params = params
        self._h_suffix = h_suffix
        self._v_suffix = v_suffix
        self._cancel_requested = False

    @QtCore.Slot()
    def run(self):
        try:
            from imswitch.improcess.reconstructors.widefield_starss.analysis import (
                WidefieldStarssBatchCancelled,
                run_widefield_starss_batch_from_folder,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return

        try:
            result = run_widefield_starss_batch_from_folder(
                self._input_folder,
                params=self._params,
                progress_callback=self.progress.emit,
                cancel_callback=lambda: self._cancel_requested,
                h_suffix=self._h_suffix,
                v_suffix=self._v_suffix,
            )
            if self._cancel_requested:
                self.cancelled.emit("WFS batch cancelled before export.")
                return
            regions_path, summary_path = result.save_csv(self._output_folder)
            hdf5_path = result.save_hdf5(
                Path(self._output_folder) / "batch_widefield_starss.h5"
            )
            self.finished.emit(
                {
                    "pair_count": len(result.pairs),
                    "region_count": len(result.regions),
                    "unmatched_count": len(result.unmatched),
                    "regions_path": str(regions_path),
                    "summary_path": str(summary_path),
                    "hdf5_path": str(hdf5_path),
                    "summary_columns": list(result.summary.columns),
                    "summary_records": result.summary.to_dict(orient="records"),
                    "region_columns": list(result.regions.columns),
                    "region_records": result.regions.to_dict(orient="records"),
                    "unmatched_paths": [str(path) for path in result.unmatched],
                    "plot_payloads": result.plot_payloads(),
                }
            )
        except WidefieldStarssBatchCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))

    @QtCore.Slot()
    def cancel(self):
        self._cancel_requested = True


class WidefieldStarssBatchController(ImProcessWidgetController):
    """Owns the Widefield-STARSS batch workflow: launching the background
    worker thread, relaying its progress/results to the parameter widget and
    graph dock, and folding single-file WFS results into the accumulated
    results tables.

    Extracted from ``ImProcessMainViewController``; the coordinator passes
    itself as ``mainController`` so the active-reconstructor guard can be
    checked. All other state (the worker thread/worker references) is owned
    here.
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._wfsBatchThread = None
        self._wfsBatchWorker = None

    def runBatch(self):
        active = self._main._activeReconstructor if self._main is not None else None
        if active is None or active.id != "widefield-starss":
            return
        if self._wfsBatchThread is not None:
            self._set_wfs_batch_status("A WFS batch is already running.")
            return
        widget = getattr(self._widget, "parTree", None)
        if widget is None or not hasattr(widget, "get_values"):
            return
        params = widget.get_values()
        input_folder = params.get("batch_input_folder")
        output_folder = params.get("batch_output_folder")
        if not input_folder:
            self._set_wfs_batch_status("Choose a batch input folder.")
            return
        if not output_folder:
            self._set_wfs_batch_status("Choose a batch output folder.")
            return

        try:
            analysis_params = self._make_widefield_starss_batch_params(params)
            thread = QtCore.QThread(self._widget)
            worker = _WidefieldStarssBatchWorker(
                input_folder=input_folder,
                output_folder=output_folder,
                params=analysis_params,
                h_suffix=str(params.get("h_suffix") or "_h"),
                v_suffix=str(params.get("v_suffix") or "_v"),
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(self._on_wfs_batch_progress)
            worker.finished.connect(self._on_wfs_batch_finished)
            worker.failed.connect(self._on_wfs_batch_failed)
            worker.cancelled.connect(self._on_wfs_batch_cancelled)

            for signal in (worker.finished, worker.failed, worker.cancelled):
                signal.connect(worker.deleteLater)
                signal.connect(thread.quit)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda: self._clear_wfs_batch_refs(thread))

            self._wfsBatchThread = thread
            self._wfsBatchWorker = worker
            self._set_wfs_batch_running(True)
            self._set_wfs_batch_progress(0, 1)
            self._set_wfs_batch_status("WFS batch starting...")
            thread.start()
        except Exception as exc:
            self._logger.error(f"WidefieldSTARSS batch failed: {exc}")
            self._set_wfs_batch_status(f"Batch failed: {exc}")
            self._set_wfs_batch_running(False)

    def cancelBatch(self):
        worker = self._wfsBatchWorker
        if worker is None:
            self._set_wfs_batch_status("No WFS batch is running.")
            return
        worker.cancel()
        self._set_wfs_batch_status("Cancelling WFS batch after the current pair...")

    def _make_widefield_starss_batch_params(self, params: dict):
        from imswitch.improcess.reconstructors.widefield_starss.analysis import (
            WidefieldStarssParams,
        )

        return WidefieldStarssParams(
            convention=params.get("convention", "alternating"),
            start_frame=int(params.get("start_frame", 0)),
            n_dark=int(params.get("n_dark", 0)),
            n_off=int(params.get("n_off", 0)),
            sum_stacks=bool(params.get("sum_stacks", False)),
            split_detection=bool(params.get("split_detection", False)),
            split_y=params.get("split_y"),
            anisotropy_mode=params.get("anisotropy_mode", "stokes"),
            segmentation_mode=params.get("segmentation_mode", "none"),
            segmentation_sigma=float(params.get("segmentation_sigma", 2.0)),
            min_size=int(params.get("min_size", 200)),
            hole_size=int(params.get("hole_size", 200)),
            threshold_scale=float(params.get("threshold_scale", 1.0)),
            psf_sigma=float(params.get("psf_sigma", 2.0)),
            psf_min_distance=int(params.get("psf_min_distance", 5)),
            psf_threshold_rel=float(params.get("psf_threshold_rel", 0.1)),
            psf_radius=int(params.get("psf_radius", 3)),
            smooth_sigma=float(params.get("smooth_sigma", 2.0)),
            intensity_threshold=params.get("intensity_threshold"),
        )

    def _on_wfs_batch_progress(self, progress: dict):
        total = int(progress.get("pair_count", 1) or 1)
        completed = int(progress.get("completed", 0) or 0)
        sample_id = str(progress.get("sample_id", ""))
        state = str(progress.get("state", ""))
        if state == "processing":
            self._set_wfs_batch_progress(completed, total)
            self._set_wfs_batch_status(
                f"Processing {sample_id}: {completed + 1}/{total} pair(s)."
            )
        else:
            self._set_wfs_batch_progress(completed, total, sample_id, state)

    def _on_wfs_batch_finished(self, payload: dict):
        regions_path = Path(payload["regions_path"])
        summary_path = Path(payload["summary_path"])
        hdf5_path = Path(payload["hdf5_path"])
        message = (
            f"WFS batch complete: {payload['pair_count']} pair(s), "
            f"{payload['region_count']} region row(s), "
            f"{payload['unmatched_count']} unmatched file(s). "
            f"Saved {regions_path.name}, {summary_path.name}, {hdf5_path.name}."
        )
        self._logger.info(message)
        self._set_wfs_batch_progress(int(payload["pair_count"]), int(payload["pair_count"]) or 1)
        self._set_wfs_batch_results(payload)
        self._set_wfs_batch_graphs(payload.get("plot_payloads", []))
        self._set_wfs_batch_status(message)
        self._set_wfs_batch_running(False)

    def _on_wfs_batch_failed(self, message: str):
        self._logger.error(f"WidefieldSTARSS batch failed: {message}")
        self._set_wfs_batch_status(f"Batch failed: {message}")
        self._set_wfs_batch_running(False)

    def _on_wfs_batch_cancelled(self, message: str):
        self._logger.info(message or "WidefieldSTARSS batch cancelled")
        self._set_wfs_batch_status("WFS batch cancelled.")
        self._set_wfs_batch_running(False)

    def _clear_wfs_batch_refs(self, thread):
        if self._wfsBatchThread is thread:
            self._wfsBatchThread = None
            self._wfsBatchWorker = None

    def _set_wfs_batch_status(self, text: str) -> None:
        widget = getattr(self._widget, "parTree", None)
        setter = getattr(widget, "set_batch_status", None)
        if callable(setter):
            setter(text)
        else:
            self._logger.info(text)

    def _set_wfs_batch_running(self, running: bool) -> None:
        widget = getattr(self._widget, "parTree", None)
        setter = getattr(widget, "set_batch_running", None)
        if callable(setter):
            setter(running)

    def _set_wfs_batch_progress(
        self,
        completed: int,
        total: int,
        sample_id: str | None = None,
        state: str | None = None,
    ) -> None:
        widget = getattr(self._widget, "parTree", None)
        setter = getattr(widget, "set_batch_progress", None)
        if callable(setter):
            setter(completed, total, sample_id, state)

    def _set_wfs_batch_results(self, payload: dict) -> None:
        widget = getattr(self._widget, "parTree", None)
        setter = getattr(widget, "set_batch_results", None)
        if callable(setter):
            setter(payload)

    def appendSingleResult(self, result) -> None:
        """Append one single-file WFS result to the accumulated results tables
        in the parameter widget, alongside any batch rows."""
        analysis = getattr(result, "analysis", None)
        widget = getattr(self._widget, "parTree", None)
        appender = getattr(widget, "append_results", None)
        if analysis is None or not callable(appender):
            return
        try:
            from imswitch.improcess.reconstructors.widefield_starss.analysis import (
                single_analysis_results_payload,
            )

            params = getattr(result, "params", {}) or {}
            payload = single_analysis_results_payload(
                analysis,
                sample_id=result.name,
                h_path=params.get("source_h_path", ""),
                v_path=params.get("source_v_path", ""),
            )
            appender(payload)
        except Exception as exc:
            self._logger.warning(f"Could not append WFS single result to tables: {exc}")

    def plotMetric(self) -> None:
        widget = getattr(self._widget, "parTree", None)
        builder = getattr(widget, "build_metric_plot_payloads", None)
        if not callable(builder):
            return
        payloads = builder()
        if payloads:
            self._set_wfs_batch_graphs(payloads)
        else:
            self._set_wfs_batch_status("No accumulated WFS results to plot.")

    def _set_wfs_batch_graphs(self, plot_payloads) -> None:
        graph_widget = getattr(self._widget, "graphWidget", None)
        setter = getattr(graph_widget, "setPlotPayloads", None)
        if callable(setter):
            setter(list(plot_payloads))


# Copyright (C) 2020-2021 ImSwitch developers
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
