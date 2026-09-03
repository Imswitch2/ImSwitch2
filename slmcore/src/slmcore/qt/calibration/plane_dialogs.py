from __future__ import annotations

from typing import Any

from qtpy import QtCore,QtGui,QtWidgets


def _exec(dialog: QtWidgets.QDialog) -> int:
    return dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()


def request_plane_definition(
    parent: QtWidgets.QWidget | None=None,
    *,
    detectors: tuple[str,...]=(),
    current_detector: str | None=None,
) -> dict[str, Any] | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Add plane")
    dialog.resize(380,190)

    layout = QtWidgets.QVBoxLayout(dialog)
    form = QtWidgets.QFormLayout()
    name_edit = QtWidgets.QLineEdit()
    detector_combo = QtWidgets.QComboBox()
    detector_combo.setEditable(not bool(detectors))
    for detector in detectors:
        name = str(detector).strip()
        if name:
            detector_combo.addItem(name,name)
    if current_detector:
        index = detector_combo.findData(str(current_detector))
        if index < 0:
            index = detector_combo.findText(str(current_detector))
        if index >= 0:
            detector_combo.setCurrentIndex(index)
    pixel_size_edit = QtWidgets.QLineEdit()
    validator = QtGui.QDoubleValidator(pixel_size_edit)
    validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
    validator.setLocale(QtCore.QLocale(QtCore.QLocale.C))
    validator.setBottom(0.0)
    pixel_size_edit.setValidator(validator)
    description_edit = QtWidgets.QLineEdit()

    form.addRow("Plane name:",name_edit)
    form.addRow("Detector name:",detector_combo)
    form.addRow("Detector pixel size (um):",pixel_size_edit)
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
        try:
            name = name_edit.text().strip()
            detector = detector_combo.currentText().strip()
            if not name:
                raise ValueError("Plane name is required.")
            if not detector:
                raise ValueError("Detector name is required.")
            text = pixel_size_edit.text().strip()
            if not text:
                raise ValueError("Detector pixel size is required.")
            pixel_size = float(text)
            if pixel_size <= 0.0:
                raise ValueError("Detector pixel size must be > 0.")
            return {
                "name":name,
                "detector_name":detector,
                "detector_pixel_size_um":pixel_size,
                "description":description_edit.text().strip(),
            }
        except ValueError as error:
            QtWidgets.QMessageBox.warning(dialog,"Add SLM Plane",str(error))
    return None


def confirm_plane_deletion(
    plane_name: str,parent: QtWidgets.QWidget | None=None,
) -> bool:
    answer = QtWidgets.QMessageBox.question(
        parent,
        "Delete plane",
        "Delete plane '%s' and all calibration files for this plane?" % plane_name,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return answer == QtWidgets.QMessageBox.Yes


def confirm_calibration_deletion(
    plane_name: str,parent: QtWidgets.QWidget | None=None,
) -> bool:
    answer = QtWidgets.QMessageBox.question(
        parent,
        "Delete calibration",
        (
            "Permanently delete the calibration for plane '%s'?\n\n"
            "The plane definition will be kept. This cannot be undone."
        ) % plane_name,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return answer == QtWidgets.QMessageBox.Yes


__all__ = [
    "confirm_calibration_deletion","confirm_plane_deletion",
    "request_plane_definition",
]
