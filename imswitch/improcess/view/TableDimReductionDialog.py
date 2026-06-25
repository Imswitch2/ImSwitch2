"""Dialog for PCA / UMAP dimensionality reduction of results-table columns.

Kept separate from the results table so multi-column selection and reducer
options don't crowd the table panel. Produces a ``spec`` dict consumable by
:func:`imswitch.improcess.model.table_plots.build_plot_payloads`.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets


class TableDimReductionDialog(QtWidgets.QDialog):
    """Pick columns + method (PCA/UMAP) and build a reduction plot spec."""

    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dimensionality reduction")
        self.setMinimumWidth(320)

        self.columnList = QtWidgets.QListWidget()
        self.columnList.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for column in columns:
            item = QtWidgets.QListWidgetItem(str(column))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked)
            self.columnList.addItem(item)

        self.methodCombo = QtWidgets.QComboBox()
        self.methodCombo.addItems(["PCA", "UMAP"])
        self.methodCombo.currentTextChanged.connect(self._update_umap_enabled)

        self.standardizeCheck = QtWidgets.QCheckBox("Standardize columns (z-score)")
        self.standardizeCheck.setChecked(True)
        self.standardizeCheck.setToolTip(
            "Scale each column to zero mean / unit variance before reducing so "
            "large-range columns don't dominate."
        )

        self.neighborsSpin = QtWidgets.QSpinBox()
        self.neighborsSpin.setRange(2, 200)
        self.neighborsSpin.setValue(15)
        self.minDistSpin = QtWidgets.QDoubleSpinBox()
        self.minDistSpin.setRange(0.0, 1.0)
        self.minDistSpin.setDecimals(2)
        self.minDistSpin.setSingleStep(0.05)
        self.minDistSpin.setValue(0.1)

        selectButtons = QtWidgets.QHBoxLayout()
        self.selectAllButton = QtWidgets.QPushButton("All")
        self.selectNoneButton = QtWidgets.QPushButton("None")
        self.selectAllButton.clicked.connect(lambda: self._set_all(QtCore.Qt.Checked))
        self.selectNoneButton.clicked.connect(lambda: self._set_all(QtCore.Qt.Unchecked))
        selectButtons.addWidget(QtWidgets.QLabel("Columns"))
        selectButtons.addStretch()
        selectButtons.addWidget(self.selectAllButton)
        selectButtons.addWidget(self.selectNoneButton)

        form = QtWidgets.QFormLayout()
        form.addRow("Method", self.methodCombo)
        form.addRow("", self.standardizeCheck)
        form.addRow("UMAP neighbors", self.neighborsSpin)
        form.addRow("UMAP min dist", self.minDistSpin)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(selectButtons)
        layout.addWidget(self.columnList, 1)
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        self._update_umap_enabled(self.methodCombo.currentText())

    def _set_all(self, state) -> None:
        for row in range(self.columnList.count()):
            self.columnList.item(row).setCheckState(state)

    def _update_umap_enabled(self, method: str) -> None:
        is_umap = method == "UMAP"
        self.neighborsSpin.setEnabled(is_umap)
        self.minDistSpin.setEnabled(is_umap)

    def _checked_columns(self) -> list[str]:
        return [
            self.columnList.item(row).text()
            for row in range(self.columnList.count())
            if self.columnList.item(row).checkState() == QtCore.Qt.Checked
        ]

    def selected_spec(self) -> dict:
        """Build the plot spec from the current selections."""
        columns = self._checked_columns()
        if self.methodCombo.currentText() == "UMAP":
            return {
                "kind": "umap",
                "columns": columns,
                "standardize": self.standardizeCheck.isChecked(),
                "n_neighbors": self.neighborsSpin.value(),
                "min_dist": self.minDistSpin.value(),
            }
        return {
            "kind": "pca",
            "columns": columns,
            "standardize": self.standardizeCheck.isChecked(),
        }

    @classmethod
    def get_spec(cls, columns, parent=None) -> dict | None:
        """Modal helper: return the chosen spec, or ``None`` if cancelled."""
        dialog = cls(columns, parent=parent)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.selected_spec()
