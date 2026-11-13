"""Compact advanced scan-metadata overrides for the MoNaLISA widget."""

from __future__ import annotations

from qtpy import QtWidgets


class ScanningAdvancedDialog(QtWidgets.QDialog):
    """Edit reconstruction-only scan metadata overrides.

    The normal path always trusts the acquisition layout/recording metadata.
    The explicit checkbox makes the exceptional recovery path visible without
    exposing sentinel zeros in the main reconstruction panel.
    """

    def __init__(self, values: dict, *, recorded_summary: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced scanning")

        self.overrideCheck = QtWidgets.QCheckBox("Override recording scan metadata")
        self.recordedLabel = QtWidgets.QLabel(
            recorded_summary or "Recorded metadata: use values stored with the data"
        )
        self.recordedLabel.setWordWrap(True)

        self.fastSteps = QtWidgets.QSpinBox()
        self.fastSteps.setRange(1, 100000)
        self.slowSteps = QtWidgets.QSpinBox()
        self.slowSteps.setRange(1, 100000)
        self.stepNm = QtWidgets.QDoubleSpinBox()
        self.stepNm.setRange(0.001, 1e7)
        self.stepNm.setDecimals(3)
        self.stepNm.setSuffix(" nm")

        fast = int(values.get("scan_steps_fast", 0) or 0)
        slow = int(values.get("scan_steps_slow", 0) or 0)
        step = float(values.get("scan_step_nm", 0.0) or 0.0)
        self.fastSteps.setValue(max(1, fast or 1))
        self.slowSteps.setValue(max(1, slow or 1))
        self.stepNm.setValue(max(0.001, step or 1.0))
        self.overrideCheck.setChecked(bool(fast or slow or step))

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Fast-axis steps", self.fastSteps)
        form.addRow("Slow-axis steps", self.slowSteps)
        form.addRow("Step size", self.stepNm)

        self.resetButton = QtWidgets.QPushButton("Use recording metadata")
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.resetButton.clicked.connect(lambda: self.overrideCheck.setChecked(False))
        self.overrideCheck.toggled.connect(self._sync_enabled)

        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(self.resetButton)
        footer.addStretch(1)
        footer.addWidget(buttons)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.recordedLabel)
        layout.addWidget(self.overrideCheck)
        layout.addLayout(form)
        layout.addLayout(footer)
        self._sync_enabled(self.overrideCheck.isChecked())

    def _sync_enabled(self, enabled: bool) -> None:
        for widget in (self.fastSteps, self.slowSteps, self.stepNm):
            widget.setEnabled(bool(enabled))

    def values(self) -> dict:
        if not self.overrideCheck.isChecked():
            return {"scan_steps_fast": 0, "scan_steps_slow": 0, "scan_step_nm": 0.0}
        return {
            "scan_steps_fast": int(self.fastSteps.value()),
            "scan_steps_slow": int(self.slowSteps.value()),
            "scan_step_nm": float(self.stepNm.value()),
        }


__all__ = ["ScanningAdvancedDialog"]
