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


class ResultsTableWidget(QtWidgets.QWidget):
    """Reusable results table widget that renders columns + list-of-dict records, supports filtering, and exports to CSV."""

    def __init__(self, *args, show_filter=True, show_csv=True, **kwargs):
        super().__init__(*args, **kwargs)
        
        self._columns: list[str] = []
        self._records: list[dict] = []
        self._current_filter = ""
        
        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        
        if show_filter:
            self.filterEdit = QtWidgets.QLineEdit()
            self.filterEdit.setPlaceholderText("Filter...")
            self.filterEdit.textChanged.connect(self._on_filter_changed)
            layout.addWidget(self.filterEdit)
        
        self.table = self._make_result_table()
        layout.addWidget(self.table)
        
        if show_csv:
            self.csvButton = QtWidgets.QPushButton("Save CSV...")
            self.csvButton.clicked.connect(self._on_save_csv)
            layout.addWidget(self.csvButton)
    
    def _make_result_table(self) -> QtWidgets.QTableWidget:
        """Creates a QTableWidget configured exactly like the reference implementation."""
        table = QtWidgets.QTableWidget(0, 0)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        return table
    
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
