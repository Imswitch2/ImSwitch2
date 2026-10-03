from __future__ import annotations

from typing import Any

from qtpy import QtWidgets


def _exec(dialog: QtWidgets.QDialog) -> int:
    return dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()


def request_mapping_definition(
    parent: QtWidgets.QWidget | None=None,
) -> dict[str,Any] | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Add mapping")
    dialog.resize(380,140)

    layout = QtWidgets.QVBoxLayout(dialog)
    form = QtWidgets.QFormLayout()
    name_edit = QtWidgets.QLineEdit()
    description_edit = QtWidgets.QLineEdit()

    form.addRow("Mapping name:",name_edit)
    form.addRow("Description:",description_edit)
    layout.addLayout(form)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
    )
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Add")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    while _exec(dialog) == QtWidgets.QDialog.Accepted:
        name = name_edit.text().strip()
        if not name:
            QtWidgets.QMessageBox.warning(
                dialog,"Add SLM Mapping","Mapping name is required.",
            )
            continue
        return {
            "name":name,
            "description":description_edit.text().strip(),
        }
    return None


def confirm_mapping_deletion(
    mapping_name: str,parent: QtWidgets.QWidget | None=None,
) -> bool:
    answer = QtWidgets.QMessageBox.question(
        parent,
        "Delete mapping",
        "Delete mapping '%s' and all calibration files for this mapping?" % mapping_name,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return answer == QtWidgets.QMessageBox.Yes


def confirm_calibration_deletion(
    mapping_name: str,parent: QtWidgets.QWidget | None=None,
) -> bool:
    answer = QtWidgets.QMessageBox.question(
        parent,
        "Delete calibration",
        (
            "Permanently delete the calibration for mapping '%s'?\n\n"
            "The mapping definition will be kept. This cannot be undone."
        ) % mapping_name,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return answer == QtWidgets.QMessageBox.Yes


__all__ = [
    "confirm_calibration_deletion","confirm_mapping_deletion",
    "request_mapping_definition",
]
