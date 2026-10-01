"""The Q-08 preflight: what a batch would do before it does it.

A-13 refuses `clippable` and worse by default. This dialog is the only way to
opt in, and it opts in *per row* — the user sees which ROI on which result is
in question and what would be lost, rather than accepting a single yes that
covers cases they never saw.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.algorithms.spatial_frame import (
    OPT_IN_MEASURABLE,
    is_auto_measurable,
)

#: What each verdict means, in the terms the decision is actually made in.
VERDICT_NOTES = {
    "exact": "same plane and calibration",
    "pixel-compatible": "same pixels, different calibration — "
    "calibrated values will use this result's scale",
    "clippable": "different extent — the ROI will be cut to the overlap",
    "registered": "related only through a transform; measuring would "
    "reproject, which is a separate, explicit action",
    "incompatible": "not the same plane or not the same pixel grid",
}


class ROIPreflightDialog(QtWidgets.QDialog):
    """Per-row opt-in for a batch that is not automatically measurable."""

    def __init__(self, entries, parent=None):
        """``entries`` are ``(result_name, roi_name, verdict)`` triples."""
        super().__init__(parent)
        self.setWindowTitle("Measure across results — preflight")
        self._entries = list(entries)

        self.table = QtWidgets.QTableWidget(len(self._entries), 4, self)
        self.table.setHorizontalHeaderLabels(["Measure", "Result", "ROI", "Verdict"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, (result_name, roi_name, verdict) in enumerate(self._entries):
            check = QtWidgets.QTableWidgetItem("")
            check.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            auto = is_auto_measurable(verdict)
            optional = verdict in OPT_IN_MEASURABLE
            check.setCheckState(QtCore.Qt.Checked if auto else QtCore.Qt.Unchecked)
            if not auto and not optional:
                # Never opt-in-able: shown so the count adds up, but the box
                # cannot be ticked. Hiding the row would leave the user
                # wondering which ROIs went missing.
                check.setFlags(QtCore.Qt.ItemIsEnabled)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(result_name)))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(roi_name)))
            verdict_item = QtWidgets.QTableWidgetItem(str(verdict))
            verdict_item.setToolTip(VERDICT_NOTES.get(verdict, ""))
            self.table.setItem(row, 3, verdict_item)

        note = QtWidgets.QLabel(
            "Rows that are not an exact or pixel-compatible match are unticked "
            "by default. Tick one to measure it anyway; the choice is recorded "
            "with the row."
        )
        note.setWordWrap(True)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

    def accepted_entries(self) -> list[tuple[str, str, str]]:
        """The ticked rows, as ``(result_name, roi_name, verdict)``."""
        out = []
        for row, entry in enumerate(self._entries):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == QtCore.Qt.Checked:
                out.append(entry)
        return out

    @classmethod
    def confirm(cls, entries, parent=None):
        """Ticked rows, or None if the user cancelled."""
        dialog = cls(entries, parent)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            return dialog.accepted_entries()
        return None


__all__ = ["ROIPreflightDialog", "VERDICT_NOTES"]
