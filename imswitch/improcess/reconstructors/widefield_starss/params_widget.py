"""WidefieldSTARSS parameter widget."""

import numpy as np
from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.reconstructors.widefield_starss.analysis.pipeline import (
    VISIBLE_SEGMENTATION_MODES,
)


class WidefieldStarssParamsWidget(QtWidgets.QWidget):
    """Parameter tree for WidefieldSTARSS H/V analysis."""

    _REGION_PREVIEW_LIMIT = 500
    _PREFERRED_METRICS = (
        "ellipticity",
        "area_pixels",
        "anisotropy_direct",
        "H_total_mean_signal",
        "V_total_mean_signal",
    )
    _NON_METRIC_COLUMNS = frozenset(
        {"pair_index", "label", "sample_id", "source_h_path", "source_v_path",
         "anisotropy_mode", "fit_success"}
    )
    _SEGMENTATION_METRICS = frozenset(
        {"area_pixels", "area_superpixels", "area_superpixels_upper",
         "area_superpixels_lower", "centroid_y", "centroid_x",
         "bbox_min_y", "bbox_min_x", "bbox_max_y", "bbox_max_x",
         "width_pixels", "height_pixels", "ellipticity"}
    )
    _PROCESSED_METRIC_KEYWORDS = (
        "anisotropy", "x_direct", "x_fit", "fit_", "dolp", "aolp",
        "_s0", "_s1", "_s2",
    )
    _METRIC_CATEGORIES = ("Segmentation", "Processed", "Raw")
    _ERROR_AUTO = "(auto)"
    _ERROR_NONE = "(none)"
    _X_CELL_INDEX = "(cell index)"

    sigRunBatchRequested = QtCore.Signal()
    sigCancelBatchRequested = QtCore.Signal()
    sigPlotMetricRequested = QtCore.Signal()
    #: (columns, records, spec) for a generic table plot request; carries the
    #: full accumulated records (not the truncated table preview).
    sigTablePlotRequested = QtCore.Signal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        params = [
            {"name": "Preset", "type": "list", "values": ["Custom", "Widefield cells", "Line PSF / split detection"], "value": "Custom"},
            {"name": "Pairing", "type": "group", "children": [
                {"name": "Current file role", "type": "list", "values": ["Auto", "H", "V"], "value": "Auto"},
                {"name": "Counterpart path", "type": "str", "value": ""},
                {"name": "H suffix", "type": "str", "value": "_h",
                 "tip": "Filename ending that marks the H stack (case-insensitive)"},
                {"name": "V suffix", "type": "str", "value": "_v",
                 "tip": "Filename ending that marks the V stack (case-insensitive)"},
            ]},
            {"name": "Loading", "type": "group", "children": [
                {"name": "Convention", "type": "list", "values": ["alternating", "block"], "value": "alternating"},
                {"name": "Start frame", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Dark frames", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Off/background frames", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Sum stacks", "type": "bool", "value": False},
            ]},
            {"name": "Analysis", "type": "group", "children": [
                {"name": "Split detection", "type": "bool", "value": False},
                {"name": "Split Y", "type": "int", "value": 0, "limits": (0, 1000000)},
                {"name": "Anisotropy mode", "type": "list", "values": ["stokes", "direct_0_90"], "value": "stokes"},
                {"name": "Segmentation mode", "type": "list", "values": list(VISIBLE_SEGMENTATION_MODES), "value": "none"},
                {"name": "Smooth sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "Intensity threshold", "type": "float", "value": 0.0},
                {"name": "Use intensity threshold", "type": "bool", "value": False},
            ]},
            {"name": "Segmentation", "type": "group", "children": [
                {"name": "Sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "Min size", "type": "int", "value": 200, "limits": (0, 1000000)},
                {"name": "Hole size", "type": "int", "value": 200, "limits": (0, 1000000)},
                {"name": "Threshold scale", "type": "float", "value": 1.0, "limits": (0.0, 1000.0)},
                {"name": "PSF sigma", "type": "float", "value": 2.0, "limits": (0.0, 1000.0)},
                {"name": "PSF min distance", "type": "int", "value": 5, "limits": (1, 1000000)},
                {"name": "PSF threshold rel", "type": "float", "value": 0.1, "limits": (0.0, 1.0)},
                {"name": "PSF radius", "type": "int", "value": 3, "limits": (1, 1000000)},
            ]},
        ]

        self.p = Parameter.create(name="params", type="group", children=params)
        self.p.param("Preset").sigValueChanged.connect(self._preset_changed)
        self.tree = ParameterTree(showHeader=False)
        self.tree.setParameters(self.p, showTop=False)

        self.browseButton = QtWidgets.QPushButton("Browse counterpart...")
        self.browseButton.clicked.connect(self._browse_counterpart)
        self.batchInputEdit = QtWidgets.QLineEdit()
        self.batchInputEdit.setPlaceholderText("Folder with H/V tiff pairs (suffixes set under Pairing)")
        self.batchOutputEdit = QtWidgets.QLineEdit()
        self.batchOutputEdit.setPlaceholderText("Output folder for batch CSV/HDF5")
        self.batchInputBrowseButton = QtWidgets.QPushButton("Browse batch input...")
        self.batchOutputBrowseButton = QtWidgets.QPushButton("Browse batch output...")
        self.runBatchButton = QtWidgets.QPushButton("Run WFS batch")
        self.cancelBatchButton = QtWidgets.QPushButton("Cancel")
        self.cancelBatchButton.setEnabled(False)
        self.batchProgress = QtWidgets.QProgressBar()
        self.batchProgress.setRange(0, 1)
        self.batchProgress.setValue(0)
        self.batchStatusLabel = QtWidgets.QLabel("Batch: choose folders, then run.")
        self.batchStatusLabel.setWordWrap(True)
        self.batchStatusLabel.setStyleSheet("color:#888; font-size:8pt;")
        self.batchFilterEdit = QtWidgets.QLineEdit()
        self.batchFilterEdit.setPlaceholderText("Filter batch results...")
        self.clearBatchFilterButton = QtWidgets.QPushButton("Clear filter")
        self.clearResultsButton = QtWidgets.QPushButton("Clear results")
        self.openBatchOutputButton = QtWidgets.QPushButton("Open output folder")
        self.metricCombo = QtWidgets.QComboBox()
        self.metricCombo.setToolTip(
            "Region metric to plot from the accumulated results (histogram + Y axis)"
        )
        self.metricErrorCombo = QtWidgets.QComboBox()
        self.metricErrorCombo.setToolTip(
            "Error column for Y error bars. (auto) picks the metric's own "
            "*_se / *_frame_se column when one exists."
        )
        self.metricXCombo = QtWidgets.QComboBox()
        self.metricXCombo.setToolTip(
            "X axis: cell index within each file, or a second metric for a "
            "metric-vs-metric scatter (e.g. anisotropy vs area)"
        )
        self.plotMetricButton = QtWidgets.QPushButton("Plot metric")
        self.plotMetricButton.setEnabled(False)
        self.batchResultsTabs = QtWidgets.QTabWidget()
        self.batchResultsTabs.setMinimumHeight(180)
        from imswitch.improcess.view.ResultsTableWidget import ResultsTableWidget
        self.batchSummaryTable = ResultsTableWidget(
            show_filter=False, show_csv=True, show_plot=True
        )
        self.batchRegionsTable = ResultsTableWidget(
            show_filter=False, show_csv=True, show_plot=True
        )
        self.batchSummaryTable.sigPlotRequested.connect(
            lambda spec: self.sigTablePlotRequested.emit(
                list(self._summary_columns), list(self._summary_records), spec
            )
        )
        self.batchRegionsTable.sigPlotRequested.connect(
            lambda spec: self.sigTablePlotRequested.emit(
                list(self._region_columns), list(self._region_records), spec
            )
        )
        self.batchUnmatchedList = QtWidgets.QListWidget()
        self.batchResultsTabs.addTab(self.batchSummaryTable, "Summary")
        self.batchResultsTabs.addTab(self.batchRegionsTable, "Regions")
        self.batchResultsTabs.addTab(self.batchUnmatchedList, "Unmatched")

        self._summary_columns: list[str] = []
        self._summary_records: list[dict] = []
        self._region_columns: list[str] = []
        self._region_records: list[dict] = []
        self._unmatched_paths: list[str] = []
        self.clear_batch_results()

        self.batchInputBrowseButton.clicked.connect(self._browse_batch_input)
        self.batchOutputBrowseButton.clicked.connect(self._browse_batch_output)
        self.runBatchButton.clicked.connect(self.sigRunBatchRequested)
        self.cancelBatchButton.clicked.connect(self.sigCancelBatchRequested)
        self.batchFilterEdit.textChanged.connect(self._apply_batch_filter)
        self.clearBatchFilterButton.clicked.connect(self.batchFilterEdit.clear)
        self.clearResultsButton.clicked.connect(self.clear_batch_results)
        self.openBatchOutputButton.clicked.connect(self._open_batch_output_folder)
        self.plotMetricButton.clicked.connect(self.sigPlotMetricRequested)

        batchForm = QtWidgets.QFormLayout()
        batchForm.addRow("Input folder", self.batchInputEdit)
        batchForm.addRow("Output folder", self.batchOutputEdit)

        batchButtons = QtWidgets.QHBoxLayout()
        batchButtons.addWidget(self.batchInputBrowseButton)
        batchButtons.addWidget(self.batchOutputBrowseButton)
        batchButtons.addWidget(self.runBatchButton)
        batchButtons.addWidget(self.cancelBatchButton)
        batchButtons.addStretch()

        batchFilterRow = QtWidgets.QHBoxLayout()
        batchFilterRow.addWidget(self.batchFilterEdit)
        batchFilterRow.addWidget(self.clearBatchFilterButton)
        batchFilterRow.addWidget(self.clearResultsButton)
        batchFilterRow.addWidget(self.openBatchOutputButton)

        plotMetricRow = QtWidgets.QHBoxLayout()
        plotMetricRow.addWidget(QtWidgets.QLabel("Metric"))
        plotMetricRow.addWidget(self.metricCombo, 2)
        plotMetricRow.addWidget(QtWidgets.QLabel("Error"))
        plotMetricRow.addWidget(self.metricErrorCombo, 1)
        plotMetricRow.addWidget(QtWidgets.QLabel("X"))
        plotMetricRow.addWidget(self.metricXCombo, 1)
        plotMetricRow.addWidget(self.plotMetricButton)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.browseButton)
        layout.addLayout(batchForm)
        layout.addLayout(batchButtons)
        layout.addWidget(self.batchProgress)
        layout.addWidget(self.batchStatusLabel)
        layout.addLayout(batchFilterRow)
        layout.addLayout(plotMetricRow)
        layout.addWidget(self.batchResultsTabs)
        self.setLayout(layout)

    def get_values(self) -> dict:
        pairing = self.p.param("Pairing")
        loading = self.p.param("Loading")
        analysis = self.p.param("Analysis")
        segmentation = self.p.param("Segmentation")

        split_y = int(analysis.param("Split Y").value())
        counterpart_path = str(pairing.param("Counterpart path").value()).strip()

        return {
            "current_role": pairing.param("Current file role").value(),
            "counterpart_path": counterpart_path or None,
            "h_suffix": str(pairing.param("H suffix").value()).strip() or "_h",
            "v_suffix": str(pairing.param("V suffix").value()).strip() or "_v",
            "convention": loading.param("Convention").value(),
            "start_frame": int(loading.param("Start frame").value()),
            "n_dark": int(loading.param("Dark frames").value()),
            "n_off": int(loading.param("Off/background frames").value()),
            "sum_stacks": bool(loading.param("Sum stacks").value()),
            "split_detection": bool(analysis.param("Split detection").value()),
            "split_y": split_y if split_y > 0 else None,
            "anisotropy_mode": analysis.param("Anisotropy mode").value(),
            "segmentation_mode": analysis.param("Segmentation mode").value(),
            "smooth_sigma": float(analysis.param("Smooth sigma").value()),
            "intensity_threshold": (
                float(analysis.param("Intensity threshold").value())
                if bool(analysis.param("Use intensity threshold").value())
                else None
            ),
            "segmentation_sigma": float(segmentation.param("Sigma").value()),
            "min_size": int(segmentation.param("Min size").value()),
            "hole_size": int(segmentation.param("Hole size").value()),
            "threshold_scale": float(segmentation.param("Threshold scale").value()),
            "psf_sigma": float(segmentation.param("PSF sigma").value()),
            "psf_min_distance": int(segmentation.param("PSF min distance").value()),
            "psf_threshold_rel": float(segmentation.param("PSF threshold rel").value()),
            "psf_radius": int(segmentation.param("PSF radius").value()),
            "batch_input_folder": self.batchInputEdit.text().strip() or None,
            "batch_output_folder": self.batchOutputEdit.text().strip() or None,
        }

    def _browse_counterpart(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select WidefieldSTARSS H/V counterpart",
            "",
            "TIFF files (*.tif *.tiff);;All files (*)",
        )
        if path:
            self.p.param("Pairing").param("Counterpart path").setValue(path)

    def set_batch_status(self, text: str) -> None:
        self.batchStatusLabel.setText(str(text))

    def set_batch_running(self, running: bool) -> None:
        self.runBatchButton.setEnabled(not running)
        self.cancelBatchButton.setEnabled(running)
        self.batchInputBrowseButton.setEnabled(not running)
        self.batchOutputBrowseButton.setEnabled(not running)

    def set_batch_progress(
        self,
        completed: int,
        total: int,
        sample_id: str | None = None,
        state: str | None = None,
    ) -> None:
        total = max(int(total), 1)
        completed = max(0, min(int(completed), total))
        self.batchProgress.setRange(0, total)
        self.batchProgress.setValue(completed)
        if sample_id:
            action = "Completed" if state == "completed" else "Processing"
            self.set_batch_status(
                f"{action} {sample_id}: {completed}/{total} pair(s)."
            )

    def clear_batch_results(self) -> None:
        self._summary_columns = []
        self._summary_records = []
        self._region_columns = []
        self._region_records = []
        self._unmatched_paths = []
        self.batchFilterEdit.clear()
        self.metricCombo.clear()
        self.metricErrorCombo.clear()
        self.metricXCombo.clear()
        self.plotMetricButton.setEnabled(False)
        self._refresh_results_views()

    def set_batch_results(self, payload: dict[str, object]) -> None:
        self.append_results(payload)

    def append_results(self, payload: dict[str, object]) -> None:
        """Append one run's records (batch or single file) to the results tables.

        ``pair_index`` values are offset by the number of already-accumulated
        summary rows so every accumulated run keeps a unique file index.
        """
        offset = len(self._summary_records)
        summary_records = [dict(record) for record in payload.get("summary_records", [])]
        region_records = [dict(record) for record in payload.get("region_records", [])]
        for record in summary_records + region_records:
            try:
                record["pair_index"] = int(record.get("pair_index", 0) or 0) + offset
            except (TypeError, ValueError):
                record["pair_index"] = offset

        from imswitch.improcess.view.ResultsTableWidget import merge_columns
        self._summary_columns = merge_columns(self._summary_columns, payload.get("summary_columns", []))
        self._region_columns = merge_columns(self._region_columns, payload.get("region_columns", []))
        self._summary_records.extend(summary_records)
        self._region_records.extend(region_records)
        self._unmatched_paths.extend(str(path) for path in payload.get("unmatched_paths", []))
        self._refresh_results_views()

    def _refresh_results_views(self) -> None:
        self.batchSummaryTable.set_records(self._summary_columns, self._summary_records)
        shown_regions = self._region_records[: self._REGION_PREVIEW_LIMIT]
        self.batchRegionsTable.set_records(self._region_columns, shown_regions)
        self.batchUnmatchedList.clear()
        for path in self._unmatched_paths:
            self.batchUnmatchedList.addItem(path)

        region_count = len(self._region_records)
        self.batchResultsTabs.setTabText(0, f"Summary ({len(self._summary_records)})")
        self.batchResultsTabs.setTabText(1, f"Regions ({len(shown_regions)}/{region_count})")
        self.batchResultsTabs.setTabText(2, f"Unmatched ({len(self._unmatched_paths)})")
        self._refresh_metric_choices()
        self._apply_batch_filter(self.batchFilterEdit.text())

    def _metric_category(self, column: str) -> str:
        if column in self._SEGMENTATION_METRICS:
            return "Segmentation"
        lowered = column.lower()
        if any(keyword in lowered for keyword in self._PROCESSED_METRIC_KEYWORDS):
            return "Processed"
        return "Raw"

    def _numeric_metric_columns(self) -> list[str]:
        numeric_columns = []
        for column in self._region_columns:
            if column in self._NON_METRIC_COLUMNS:
                continue
            for record in self._region_records:
                value = record.get(column)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_columns.append(column)
                    break
        ordered = [m for m in self._PREFERRED_METRICS if m in numeric_columns]
        ordered += [c for c in numeric_columns if c not in ordered]
        return ordered

    def _populate_metric_combo(
        self,
        combo: QtWidgets.QComboBox,
        categorized: dict[str, list[str]],
        special_entries: tuple[str, ...] = (),
    ) -> None:
        previous = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(list(special_entries))
        for category in self._METRIC_CATEGORIES:
            columns = categorized.get(category, [])
            if not columns:
                continue
            combo.addItem(f"— {category} —")
            header = combo.model().item(combo.count() - 1)
            header.setFlags(QtCore.Qt.NoItemFlags)
            combo.addItems(columns)
        if previous and combo.findText(previous) >= 0:
            combo.setCurrentText(previous)
        elif special_entries:
            combo.setCurrentIndex(0)
        else:
            # Skip the leading category header so a metric is selected.
            combo.setCurrentIndex(1 if combo.count() > 1 else 0)
        combo.blockSignals(False)

    def _refresh_metric_choices(self) -> None:
        ordered = self._numeric_metric_columns()
        categorized: dict[str, list[str]] = {c: [] for c in self._METRIC_CATEGORIES}
        for column in ordered:
            categorized[self._metric_category(column)].append(column)

        self._populate_metric_combo(self.metricCombo, categorized)
        self._populate_metric_combo(
            self.metricErrorCombo, categorized, (self._ERROR_AUTO, self._ERROR_NONE)
        )
        self._populate_metric_combo(
            self.metricXCombo, categorized, (self._X_CELL_INDEX,)
        )
        self.plotMetricButton.setEnabled(bool(ordered))

    def _resolve_error_metric(self, metric: str) -> str | None:
        choice = str(self.metricErrorCombo.currentText()).strip()
        if choice == self._ERROR_NONE:
            return None
        if choice and choice != self._ERROR_AUTO:
            return choice
        for candidate in (f"{metric}_frame_se", f"{metric}_se", f"{metric}_mean_se"):
            if candidate in self._region_columns:
                return candidate
        return None

    @staticmethod
    def _record_number(record: dict, column: str | None) -> float:
        if not column:
            return np.nan
        value = record.get(column)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return np.nan

    def build_metric_plot_payloads(self) -> list[PlotPayload]:
        """Histogram and per-cell / metric-vs-metric plots of the selected
        region metric, built from all accumulated results (batch and single
        runs). Error bars come from the resolved error column, if any."""
        metric = str(self.metricCombo.currentText()).strip()
        if not metric or metric not in self._region_columns or not self._region_records:
            return []
        error_metric = self._resolve_error_metric(metric)
        x_choice = str(self.metricXCombo.currentText()).strip()
        x_metric = x_choice if x_choice in self._region_columns else None

        groups: dict[int, dict[str, object]] = {}
        for record in self._region_records:
            value = self._record_number(record, metric)
            if not np.isfinite(value):
                continue
            pair_index = int(record.get("pair_index", 0) or 0)
            group = groups.setdefault(
                pair_index,
                {
                    "sample_id": str(record.get("sample_id", pair_index)),
                    "y": [], "err": [], "x": [],
                },
            )
            group["y"].append(value)
            group["err"].append(self._record_number(record, error_metric))
            group["x"].append(self._record_number(record, x_metric))
        if not groups:
            return []

        scatter_series = []
        all_values = []
        for pair_index in sorted(groups):
            group = groups[pair_index]
            y = np.asarray(group["y"], dtype=float)
            all_values.append(y)
            if x_metric is not None:
                x = np.asarray(group["x"], dtype=float)
            else:
                x = np.arange(1, y.size + 1, dtype=float)
            style = {}
            if error_metric is not None:
                style["y_err"] = np.asarray(group["err"], dtype=float)
            scatter_series.append(
                PlotSeries(
                    name=f"{pair_index}: {group['sample_id']}",
                    x=x,
                    y=y,
                    kind="scatter",
                    style=style,
                )
            )

        if x_metric is not None:
            scatter_title = f"{metric} vs {x_metric}"
            scatter_x_label = x_metric
        else:
            scatter_title = f"{metric} per cell (by file)"
            scatter_x_label = "Cell index within file"
        if error_metric is not None:
            scatter_title += f" ± {error_metric}"

        return [
            PlotPayload(
                title=f"{metric} histogram",
                x_label=metric,
                y_label="Cell count",
                series=[
                    PlotSeries(
                        name="All cells",
                        y=np.concatenate(all_values),
                        kind="histogram",
                        style={"bins": 50},
                    )
                ],
            ),
            PlotPayload(
                title=scatter_title,
                x_label=scatter_x_label,
                y_label=metric,
                series=scatter_series,
            ),
        ]

    def _browse_batch_input(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select WidefieldSTARSS Batch Input Folder",
            "",
        )
        if path:
            self.batchInputEdit.setText(path)

    def _browse_batch_output(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select WidefieldSTARSS Batch Output Folder",
            "",
        )
        if path:
            self.batchOutputEdit.setText(path)

    def _open_batch_output_folder(self) -> None:
        output_folder = self.batchOutputEdit.text().strip()
        if not output_folder:
            self.set_batch_status("Choose a batch output folder first.")
            return
        try:
            from imswitch.imcommon.model.ostools import openFolderInOS

            openFolderInOS(output_folder)
        except Exception as exc:
            self.set_batch_status(f"Could not open output folder: {exc}")

    def _preset_changed(self, _param, value) -> None:
        if value == "Widefield cells":
            self._apply_values(
                {
                    "Loading": {
                        "Convention": "alternating",
                        "Start frame": 20,
                        "Sum stacks": False,
                    },
                    "Analysis": {
                        "Split detection": False,
                        "Anisotropy mode": "stokes",
                        "Segmentation mode": "otsu",
                        "Smooth sigma": 10.0,
                        "Use intensity threshold": False,
                    },
                    "Segmentation": {
                        "Sigma": 2.0,
                        "Min size": 200,
                        "Hole size": 200,
                        "Threshold scale": 1.0,
                    },
                }
            )
        elif value == "Line PSF / split detection":
            self._apply_values(
                {
                    "Loading": {
                        "Convention": "block",
                        "Start frame": 0,
                        "Sum stacks": False,
                    },
                    "Analysis": {
                        "Split detection": True,
                        "Anisotropy mode": "stokes",
                        "Segmentation mode": "line_psf",
                        "Smooth sigma": 1.0,
                        "Use intensity threshold": False,
                    },
                    "Segmentation": {
                        "Sigma": 1.0,
                        "Min size": 5,
                        "Hole size": 5,
                        "Threshold scale": 1.0,
                        "PSF sigma": 1.0,
                    },
                }
            )

    def _apply_values(self, values: dict[str, dict[str, object]]) -> None:
        for group_name, group_values in values.items():
            group = self.p.param(group_name)
            for name, value in group_values.items():
                group.param(name).setValue(value)

    def _apply_batch_filter(self, text: str) -> None:
        query = str(text).strip().lower()
        self.batchSummaryTable.apply_filter(query)
        self.batchRegionsTable.apply_filter(query)
        for row in range(self.batchUnmatchedList.count()):
            item = self.batchUnmatchedList.item(row)
            item.setHidden(bool(query) and query not in item.text().lower())
