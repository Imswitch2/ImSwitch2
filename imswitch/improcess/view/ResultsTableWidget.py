import csv
import io
from qtpy import QtWidgets, QtCore


def merge_columns(existing: list[str], incoming) -> list[str]:
    """Returns a NEW list: existing columns in order, then any incoming columns not already present (stringified, deduped)."""
    result = list(existing)
    for column in incoming:
        column = str(column)
        if column not in result:
            result.append(column)
    return result


def format_table_value(value) -> str:
    """None→""; NaN→"" (guard `value != value`); float→f"{v:.6g}"; else str(value)."""
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


def records_to_csv(columns: list[str], records: list[dict]) -> str:
    """CSV text with header row = columns and one row per record using format_table_value(record.get(col, ""))."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    for record in records:
        row = [format_table_value(record.get(col, "")) for col in columns]
        writer.writerow(row)
    return output.getvalue()


def _coerce_cell(text: str):
    """Best-effort restore of a CSV cell's type: ""→None, int, float, else str.

    Keeps numeric columns numeric so a reloaded table can be plotted, while
    leaving genuine text (sample ids, paths) untouched.
    """
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def records_from_csv(text: str) -> tuple[list[str], list[dict]]:
    """Inverse of :func:`records_to_csv`: header row → columns, rows → records.

    Numeric-looking cells are coerced back to int/float; blanks become ``None``.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    columns = [str(column) for column in rows[0]]
    records = []
    for row in rows[1:]:
        record = {
            column: (_coerce_cell(row[index]) if index < len(row) else None)
            for index, column in enumerate(columns)
        }
        records.append(record)
    return columns, records


class ResultsTableWidget(QtWidgets.QWidget):
    """Reusable results table widget that renders columns + list-of-dict records, supports filtering, and exports to CSV."""

    #: Emitted with a plot ``spec`` dict (see ``model.table_plots``) when the
    #: user requests a plot from the optional plot-control row.
    sigPlotRequested = QtCore.Signal(dict)

    _PLOT_TYPES = (
        ("Histogram", "histogram"),
        ("Line (X→Y)", "line"),
        ("Scatter (X→Y)", "scatter"),
        ("2D histogram", "hist2d"),
        ("PCA / UMAP…", "reduce"),
    )

    def __init__(self, *args, show_filter=True, show_csv=True, show_plot=False, **kwargs):
        super().__init__(*args, **kwargs)

        self._columns: list[str] = []
        self._records: list[dict] = []
        self._current_filter = ""
        self._show_plot = show_plot

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        if show_filter:
            self.filterEdit = QtWidgets.QLineEdit()
            self.filterEdit.setPlaceholderText("Filter...")
            self.filterEdit.textChanged.connect(self._on_filter_changed)
            layout.addWidget(self.filterEdit)

        self.table = self._make_result_table()
        layout.addWidget(self.table)

        if show_plot:
            layout.addLayout(self._make_plot_controls())

        if show_csv:
            self.csvButton = QtWidgets.QPushButton("Save CSV...")
            self.csvButton.clicked.connect(self._on_save_csv)
            self.loadCsvButton = QtWidgets.QPushButton("Load CSV...")
            self.loadCsvButton.clicked.connect(self._on_load_csv)
            csvRow = QtWidgets.QHBoxLayout()
            csvRow.setContentsMargins(0, 0, 0, 0)
            csvRow.addWidget(self.csvButton)
            csvRow.addWidget(self.loadCsvButton)
            layout.addLayout(csvRow)
    
    def _make_result_table(self) -> QtWidgets.QTableWidget:
        """Creates a QTableWidget configured exactly like the reference implementation."""
        table = QtWidgets.QTableWidget(0, 0)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        return table
    
    def get_columns(self) -> list[str]:
        """Current column names (copy)."""
        return list(self._columns)

    def get_records(self) -> list[dict]:
        """Current records (copy of the list; dicts are shared by reference)."""
        return list(self._records)

    def numeric_columns(self) -> list[str]:
        """Columns holding at least one finite numeric value, in display order."""
        from imswitch.improcess.model.table_plots import numeric_columns
        return numeric_columns(self._columns, self._records)

    def set_records(self, columns, records):
        """Replace state, repaint table, re-apply current filter."""
        self._columns = [str(col) for col in columns]
        self._records = list(records)
        self._repaint_table()
        self.apply_filter(self._current_filter)
    
    def append_records(self, columns, records):
        """Merge columns, extend records, repaint, re-apply filter."""
        self._columns = merge_columns(self._columns, columns)
        self._records.extend(records)
        self._repaint_table()
        self.apply_filter(self._current_filter)
    
    def clear_records(self):
        """Empty state + repaint."""
        self._columns = []
        self._records = []
        self._repaint_table()
    
    def apply_filter(self, query: str):
        """Public; hides non-matching rows (case-insensitive substring across all columns)."""
        self._current_filter = query
        query_lower = str(query).strip().lower()
        
        for row in range(self.table.rowCount()):
            if not query_lower:
                self.table.setRowHidden(row, False)
                continue
            match = False
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None and query_lower in item.text().lower():
                    match = True
                    break
            self.table.setRowHidden(row, not match)
    
    def to_csv(self, path):
        """Write records_to_csv(self._columns, self._records) to path."""
        csv_text = records_to_csv(self._columns, self._records)
        with open(path, "w", encoding="utf-8") as f:
            f.write(csv_text)

    def from_csv(self, path):
        """Replace the table contents with the records parsed from a CSV file."""
        with open(path, "r", encoding="utf-8") as f:
            columns, records = records_from_csv(f.read())
        self.set_records(columns, records)
    
    def _repaint_table(self):
        """Repaint helper mirrors _set_table_records."""
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(self._columns))
        self.table.setRowCount(len(self._records))
        self.table.setHorizontalHeaderLabels([str(column) for column in self._columns])
        
        for row_index, record in enumerate(self._records):
            for column_index, column in enumerate(self._columns):
                item = QtWidgets.QTableWidgetItem(
                    format_table_value(record.get(column, ""))
                )
                self.table.setItem(row_index, column_index, item)
        
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

        if self._show_plot:
            self._refresh_plot_controls()

    def _make_plot_controls(self) -> QtWidgets.QHBoxLayout:
        self.plotTypeCombo = QtWidgets.QComboBox()
        for label, _kind in self._PLOT_TYPES:
            self.plotTypeCombo.addItem(label)
        self.plotTypeCombo.setToolTip("What to plot from the table into the Graph panel")
        self.plotXCombo = QtWidgets.QComboBox()
        self.plotYCombo = QtWidgets.QComboBox()
        self.plotXLabel = QtWidgets.QLabel("X")
        self.plotYLabel = QtWidgets.QLabel("Y")
        self.plotButton = QtWidgets.QPushButton("Plot")
        self.plotButton.setEnabled(False)

        self.plotTypeCombo.currentIndexChanged.connect(self._update_plot_controls)
        self.plotButton.clicked.connect(self._on_plot_clicked)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QtWidgets.QLabel("Plot"))
        row.addWidget(self.plotTypeCombo, 2)
        row.addWidget(self.plotXLabel)
        row.addWidget(self.plotXCombo, 1)
        row.addWidget(self.plotYLabel)
        row.addWidget(self.plotYCombo, 1)
        row.addWidget(self.plotButton)
        return row

    def _current_plot_kind(self) -> str:
        index = self.plotTypeCombo.currentIndex()
        if 0 <= index < len(self._PLOT_TYPES):
            return self._PLOT_TYPES[index][1]
        return "histogram"

    def _refresh_plot_controls(self) -> None:
        numeric = self.numeric_columns()
        for combo in (self.plotXCombo, self.plotYCombo):
            previous = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(numeric)
            if previous and combo.findText(previous) >= 0:
                combo.setCurrentText(previous)
            combo.blockSignals(False)
        # Default Y to a different column than X when possible.
        if self.plotYCombo.count() > 1 and self.plotYCombo.currentText() == self.plotXCombo.currentText():
            self.plotYCombo.setCurrentIndex(1)
        self.plotButton.setEnabled(bool(numeric))
        self._update_plot_controls()

    def _update_plot_controls(self, *_args) -> None:
        kind = self._current_plot_kind()
        needs_xy = kind in ("line", "scatter", "hist2d")
        needs_x = kind == "histogram"
        is_reduce = kind == "reduce"
        self.plotXLabel.setText("Column" if needs_x else "X")
        self.plotXCombo.setVisible(not is_reduce)
        self.plotXLabel.setVisible(not is_reduce)
        self.plotYCombo.setVisible(needs_xy)
        self.plotYLabel.setVisible(needs_xy)

    def _on_plot_clicked(self) -> None:
        kind = self._current_plot_kind()
        if kind == "reduce":
            self._request_dim_reduction()
            return
        x = self.plotXCombo.currentText()
        y = self.plotYCombo.currentText()
        if kind == "histogram":
            spec = {"kind": "histogram", "column": x}
        else:
            spec = {"kind": kind, "x_column": x, "y_column": y}
        self.sigPlotRequested.emit(spec)

    def _request_dim_reduction(self) -> None:
        from imswitch.improcess.view.TableDimReductionDialog import (
            TableDimReductionDialog,
        )

        spec = TableDimReductionDialog.get_spec(self.numeric_columns(), parent=self)
        if spec is not None:
            self.sigPlotRequested.emit(spec)

    def _on_filter_changed(self, text: str):
        """Internal filter QLineEdit calls this on textChanged."""
        self.apply_filter(text)
    
    def _on_save_csv(self):
        """Save-CSV button → QFileDialog.getSaveFileName → to_csv."""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save CSV",
            "",
            "CSV (*.csv)"
        )
        if path:
            self.to_csv(path)

    def _on_load_csv(self):
        """Load-CSV button → QFileDialog.getOpenFileName → from_csv (replaces)."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load CSV",
            "",
            "CSV (*.csv)"
        )
        if path:
            self.from_csv(path)
