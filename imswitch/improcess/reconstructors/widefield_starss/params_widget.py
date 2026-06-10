"""WidefieldSTARSS parameter widget."""

from pyqtgraph.parametertree import Parameter, ParameterTree
from qtpy import QtCore, QtWidgets


class WidefieldStarssParamsWidget(QtWidgets.QWidget):
    """Parameter tree for WidefieldSTARSS H/V analysis."""

    _REGION_PREVIEW_LIMIT = 500

    sigRunBatchRequested = QtCore.Signal()
    sigCancelBatchRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        params = [
            {"name": "Preset", "type": "list", "values": ["Custom", "Widefield cells", "Line PSF / split detection"], "value": "Custom"},
            {"name": "Pairing", "type": "group", "children": [
                {"name": "Current file role", "type": "list", "values": ["Auto", "H", "V"], "value": "Auto"},
                {"name": "Counterpart path", "type": "str", "value": ""},
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
                {"name": "Segmentation mode", "type": "list", "values": ["none", "otsu", "generic_otsu", "psf_peaks", "line_psf"], "value": "none"},
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
        self.batchInputEdit.setPlaceholderText("Folder containing *_h.tif / *_v.tif files")
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
        self.openBatchOutputButton = QtWidgets.QPushButton("Open output folder")
        self.batchResultsTabs = QtWidgets.QTabWidget()
        self.batchResultsTabs.setMinimumHeight(180)
        self.batchSummaryTable = self._make_result_table()
        self.batchRegionsTable = self._make_result_table()
        self.batchUnmatchedList = QtWidgets.QListWidget()
        self.batchResultsTabs.addTab(self.batchSummaryTable, "Summary")
        self.batchResultsTabs.addTab(self.batchRegionsTable, "Regions")
        self.batchResultsTabs.addTab(self.batchUnmatchedList, "Unmatched")
        self.clear_batch_results()

        self.batchInputBrowseButton.clicked.connect(self._browse_batch_input)
        self.batchOutputBrowseButton.clicked.connect(self._browse_batch_output)
        self.runBatchButton.clicked.connect(self.sigRunBatchRequested)
        self.cancelBatchButton.clicked.connect(self.sigCancelBatchRequested)
        self.batchFilterEdit.textChanged.connect(self._apply_batch_filter)
        self.clearBatchFilterButton.clicked.connect(self.batchFilterEdit.clear)
        self.openBatchOutputButton.clicked.connect(self._open_batch_output_folder)

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
        batchFilterRow.addWidget(self.openBatchOutputButton)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.browseButton)
        layout.addLayout(batchForm)
        layout.addLayout(batchButtons)
        layout.addWidget(self.batchProgress)
        layout.addWidget(self.batchStatusLabel)
        layout.addLayout(batchFilterRow)
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
        self.batchFilterEdit.clear()
        self._set_table_records(self.batchSummaryTable, [], [])
        self._set_table_records(self.batchRegionsTable, [], [])
        self.batchUnmatchedList.clear()
        self.batchResultsTabs.setTabText(0, "Summary")
        self.batchResultsTabs.setTabText(1, "Regions")
        self.batchResultsTabs.setTabText(2, "Unmatched")

    def set_batch_results(self, payload: dict[str, object]) -> None:
        summary_columns = list(payload.get("summary_columns", []))
        summary_records = list(payload.get("summary_records", []))
        region_columns = list(payload.get("region_columns", []))
        region_records = list(payload.get("region_records", []))
        unmatched_paths = list(payload.get("unmatched_paths", []))
        region_count = int(payload.get("region_count", len(region_records)) or 0)

        self._set_table_records(self.batchSummaryTable, summary_columns, summary_records)
        self._set_table_records(self.batchRegionsTable, region_columns, region_records)
        self.batchUnmatchedList.clear()
        for path in unmatched_paths:
            self.batchUnmatchedList.addItem(str(path))

        self.batchResultsTabs.setTabText(0, f"Summary ({len(summary_records)})")
        regions_label = f"Regions ({min(len(region_records), region_count)}/{region_count})"
        if region_count > len(region_records):
            regions_label = f"Regions ({len(region_records)}/{region_count})"
        self.batchResultsTabs.setTabText(1, regions_label)
        self.batchResultsTabs.setTabText(2, f"Unmatched ({len(unmatched_paths)})")
        self._apply_batch_filter(self.batchFilterEdit.text())

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

    def _make_result_table(self) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, 0)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        return table

    def _set_table_records(
        self,
        table: QtWidgets.QTableWidget,
        columns: list[str],
        records: list[dict[str, object]],
    ) -> None:
        table.setSortingEnabled(False)
        table.clear()
        table.setColumnCount(len(columns))
        table.setRowCount(len(records))
        table.setHorizontalHeaderLabels([str(column) for column in columns])
        for row_index, record in enumerate(records):
            for column_index, column in enumerate(columns):
                item = QtWidgets.QTableWidgetItem(
                    self._format_table_value(record.get(column, ""))
                )
                table.setItem(row_index, column_index, item)
        table.resizeColumnsToContents()
        table.setSortingEnabled(True)
        self._apply_batch_filter(self.batchFilterEdit.text())

    def _format_table_value(self, value: object) -> str:
        if value is None:
            return ""
        try:
            if value != value:
                return ""
        except Exception:
            pass
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    def _apply_batch_filter(self, text: str) -> None:
        query = str(text).strip().lower()
        self._filter_table(self.batchSummaryTable, query)
        self._filter_table(self.batchRegionsTable, query)
        for row in range(self.batchUnmatchedList.count()):
            item = self.batchUnmatchedList.item(row)
            item.setHidden(bool(query) and query not in item.text().lower())

    def _filter_table(self, table: QtWidgets.QTableWidget, query: str) -> None:
        for row in range(table.rowCount()):
            if not query:
                table.setRowHidden(row, False)
                continue
            match = False
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None and query in item.text().lower():
                    match = True
                    break
            table.setRowHidden(row, not match)
