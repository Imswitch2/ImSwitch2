from __future__ import annotations

from enum import Enum
from typing import Sequence

from qtpy import QtWidgets


class CalibrationMismatchDecision(Enum):
    KEEP = "keep"
    CLEAR = "clear"
    CANCEL = "cancel"


def _exec(dialog) -> int:
    return dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()


def confirm_destructive_change(parent,title: str,message: str) -> bool:
    result = QtWidgets.QMessageBox.question(
        parent,title,str(message),
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
        QtWidgets.QMessageBox.Cancel,
    )
    return result == QtWidgets.QMessageBox.Yes


def calibration_mismatch_decision(
    parent,
    *,
    title: str,
    message: str,
    mismatches: Sequence,
    allow_clear: bool=True,
) -> CalibrationMismatchDecision:
    dialog = QtWidgets.QMessageBox(parent)
    dialog.setIcon(QtWidgets.QMessageBox.Warning)
    dialog.setWindowTitle(str(title))
    dialog.setText(str(message))
    dialog.setInformativeText("\n".join("• " + item.summary() for item in mismatches))
    keep = dialog.addButton(
        "Keep calibration" if len(mismatches) == 1 else "Keep calibrations",
        QtWidgets.QMessageBox.AcceptRole,
    )
    clear = None
    if allow_clear:
        clear = dialog.addButton(
            "Clear calibration" if len(mismatches) == 1 else "Clear calibrations",
            QtWidgets.QMessageBox.DestructiveRole,
        )
    cancel = dialog.addButton(QtWidgets.QMessageBox.Cancel)
    dialog.setDefaultButton(cancel)
    _exec(dialog)
    clicked = dialog.clickedButton()
    if clicked is keep:
        return CalibrationMismatchDecision.KEEP
    if clear is not None and clicked is clear:
        return CalibrationMismatchDecision.CLEAR
    return CalibrationMismatchDecision.CANCEL
