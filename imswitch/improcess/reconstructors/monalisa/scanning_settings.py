"""Compact advanced scan-metadata overrides for the MoNaLISA widget."""

from __future__ import annotations

from qtpy import QtWidgets

from .scan_overrides import scan_step_overrides


class ScanningAdvancedDialog(QtWidgets.QDialog):
    """Reconstruction-only overrides; zero leaves each value to the recording.

    X and Y share a manual pitch by default. Unlink Y only for an asymmetric
    lateral scan. A Z spacing override never changes the recorded frame order.
    """

    _KEYS = (
        "scan_steps_x", "scan_steps_y", "scan_steps_z", "scan_step_nm",
        "scan_steps_fast", "scan_steps_slow",
        "scan_step_x_nm", "scan_step_y_nm", "scan_step_z_nm",
    )

    def __init__(self, values: dict, *, recorded_summary: str = "",
                 recorded_axes: dict | None = None, fast_axis: str = "x", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced scanning")
        self.overrideCheck = QtWidgets.QCheckBox("Override recording metadata")
        self.recordedLabel = QtWidgets.QLabel(
            recorded_summary or "Recording metadata: unavailable"
        )
        self.recordedLabel.setWordWrap(True)

        def count_spin():
            control = QtWidgets.QSpinBox()
            control.setRange(0, 100000)
            control.setSpecialValueText("—")
            control.setValue(0)
            return control

        def step_spin():
            control = QtWidgets.QDoubleSpinBox()
            control.setRange(0.0, 1e7)
            control.setDecimals(3)
            control.setSuffix(" nm")
            control.setSpecialValueText("—")
            control.setValue(0.0)
            return control

        # Old workflows named chronological axes. Translate only for display,
        # using the selected recording's order (legacy convention: X first).
        values = dict(values)
        if not (values.get("scan_steps_x") or values.get("scan_steps_y")):
            slow_axis = "y" if fast_axis == "x" else "x"
            values[f"scan_steps_{fast_axis}"] = values.get("scan_steps_fast", 0)
            values[f"scan_steps_{slow_axis}"] = values.get("scan_steps_slow", 0)
        self.xPositions = count_spin()
        self.yPositions = count_spin()
        self.zPlanes = count_spin()
        self.xStepNm = step_spin()
        self.yStepNm = step_spin()
        self.zStepNm = step_spin()
        self.linkXY = QtWidgets.QCheckBox("Use same step for X and Y")
        self._controls = {
            "scan_steps_x": self.xPositions,
            "scan_steps_y": self.yPositions,
            "scan_steps_z": self.zPlanes,
            "scan_step_x_nm": self.xStepNm,
            "scan_step_y_nm": self.yStepNm,
            "scan_step_z_nm": self.zStepNm,
        }
        # Read back the spin-box values to compare at the UI's precision.
        recorded_axes = recorded_axes or {}
        self._metadata_values = {}
        self._initial_overrides = {}
        for axis, step in zip("xyz", scan_step_overrides(values)):
            info = recorded_axes.get(axis) or {}
            for key, metadata, override in (
                (f"scan_steps_{axis}", info.get("count"), values.get(f"scan_steps_{axis}")),
                (f"scan_step_{axis}_nm", info.get("step_nm"), step),
            ):
                control = self._controls[key]
                control.setValue(metadata or 0)
                self._metadata_values[key] = control.value()
                control.setValue(override or 0)
                self._initial_overrides[key] = control.value()
        active = any(self._initial_overrides.values())
        self.overrideCheck.setChecked(active)
        self._display_values({key: self._initial_overrides[key] or value
                              for key, value in self._metadata_values.items()})
        self.linkXY.toggled.connect(self._sync_enabled)
        self.xStepNm.valueChanged.connect(self._sync_linked_step)

        form = QtWidgets.QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addWidget(QtWidgets.QLabel("Axis"), 0, 0)
        form.addWidget(QtWidgets.QLabel("Positions"), 0, 1)
        form.addWidget(QtWidgets.QLabel("Step (nm)"), 0, 2)
        for row, (axis, positions, step) in enumerate((
            ("X", self.xPositions, self.xStepNm),
            ("Y", self.yPositions, self.yStepNm),
            ("Z", self.zPlanes, self.zStepNm),
        ), 1):
            form.addWidget(QtWidgets.QLabel(axis), row, 0)
            form.addWidget(positions, row, 1)
            form.addWidget(step, row, 2)
        form.addWidget(self.linkXY, 4, 1, 1, 2)
        for control in (self.xStepNm, self.yStepNm, self.zStepNm):
            control.setToolTip("Scan step in nm. A dash uses recording metadata if available.")
        self.xPositions.setToolTip("Stage X positions; 1 means not scanned. Without a usable acquisition layout, X-fast order is assumed.")
        self.yPositions.setToolTip("Stage Y positions; 1 means not scanned")
        self.zPlanes.setToolTip("Z planes; 1 means no Z stack")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.overrideCheck.toggled.connect(self._switch_source)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.recordedLabel)
        layout.addWidget(self.overrideCheck)
        layout.addLayout(form)
        self.sourceLabel = QtWidgets.QLabel()
        self.sourceLabel.setWordWrap(True)
        layout.addWidget(self.sourceLabel)
        layout.addWidget(buttons)
        self._sync_enabled()

    def _display_values(self, values: dict) -> None:
        # Loading a source must never cause the old X/Y link to overwrite Y.
        for key, control in self._controls.items():
            blocked = control.blockSignals(True)
            control.setValue(values[key])
            control.blockSignals(blocked)
        blocked = self.linkXY.blockSignals(True)
        self.linkXY.setChecked(self.xStepNm.value() == self.yStepNm.value())
        self.linkXY.blockSignals(blocked)

    def _switch_source(self, enabled: bool) -> None:
        if not enabled:
            # Switching back to metadata explicitly discards the dialog's
            # manual draft. Cancel still leaves the caller's settings intact.
            self._initial_overrides = dict.fromkeys(self._controls, 0)
            self._display_values(self._metadata_values)
        self._sync_enabled()

    def _sync_linked_step(self, *_args) -> None:
        if self.overrideCheck.isChecked() and self.linkXY.isChecked():
            self.yStepNm.setValue(self.xStepNm.value())

    def _sync_enabled(self, *_args) -> None:
        self._sync_linked_step()
        enabled = self.overrideCheck.isChecked()
        for control in self._controls.values():
            control.setEnabled(enabled)
        self.linkXY.setEnabled(enabled)
        self.yStepNm.setEnabled(enabled and not self.linkXY.isChecked())
        self.sourceLabel.setText(
            "Manual override: edit the values to change the scan. "
            "Clear the checkbox to reset to recording metadata."
            if enabled else "Using recording metadata. A dash means unavailable or not applicable."
        )

    def values(self) -> dict:
        result = {key: 0 if "steps" in key else 0.0 for key in self._KEYS}
        if self.overrideCheck.isChecked():
            for key, control in self._controls.items():
                value = control.value()
                # Keep existing explicit overrides, even if they happen to
                # match this recording. Do not turn inherited values into
                # overrides merely by opening the editor or enabling it.
                if value != self._metadata_values[key] or value == self._initial_overrides[key]:
                    result[key] = value
        return result


__all__ = ["ScanningAdvancedDialog"]
