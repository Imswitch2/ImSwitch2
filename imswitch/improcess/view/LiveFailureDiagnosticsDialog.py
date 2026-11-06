"""Temporary Session diagnostic tool (not saved in user preferences)."""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.execution import live_diagnostics as diag


class LiveFailureDiagnosticsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live reconstruction failure diagnostics")
        self.setMinimumWidth(540)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Record live worker/queue lifecycle events, then optionally inject one "
            "error into the next live-following job. The subsequent finite retry "
            "is never injected and is logged in the same file."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Log only (no injected failure)", "off")
        self.mode.addItem("Fail during processor begin()", "begin")
        self.mode.addItem("Fail during reader polling", "reader")
        self.mode.addItem("Fail on first T1 push (after completed T0)", "after_t0")
        selected = self.mode.findData(diag.state()["armed"])
        if selected >= 0:
            self.mode.setCurrentIndex(selected)
        form.addRow("Next live job:", self.mode)
        layout.addLayout(form)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.path = QtWidgets.QLineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("No diagnostic log created yet")
        layout.addWidget(self.path)

        controls = QtWidgets.QHBoxLayout()
        self.applyButton = QtWidgets.QPushButton("Start log / Apply")
        self.applyButton.setToolTip("Create a log if needed and arm one error for the next live job")
        self.stopButton = QtWidgets.QPushButton("Stop logging")
        self.copyButton = QtWidgets.QPushButton("Copy path")
        self.openButton = QtWidgets.QPushButton("Open folder")
        for button in (self.applyButton, self.stopButton, self.copyButton, self.openButton):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.applyButton.clicked.connect(self._apply)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.stopButton.clicked.connect(self._stop)
        self.copyButton.clicked.connect(self._copy)
        self.openButton.clicked.connect(self._open)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _apply(self):
        try:
            diag.start()
            diag.arm(self.mode.currentData())
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Diagnostics unavailable", str(exc))
        self._refresh()

    def _mode_changed(self, _index):
        # Once the log exists, changing the selection arms the next live job
        # immediately. The one-shot fault is not part of Session settings.
        if diag.state()["active"]:
            diag.arm(self.mode.currentData())
        self._refresh()

    def _stop(self):
        diag.stop()
        self._refresh()

    def _copy(self):
        value = diag.state()["path"]
        if value:
            QtWidgets.QApplication.clipboard().setText(value)

    def _open(self):
        value = diag.state()["path"]
        if value:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(value).parent)))

    def _refresh(self):
        current = diag.state()
        # A consumed one-shot fault should not leave a stale selection that
        # misleadingly looks armed for the next acquisition.
        if current["active"]:
            index = self.mode.findData(current["armed"])
            if index >= 0 and index != self.mode.currentIndex():
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(index)
                self.mode.blockSignals(False)
        self.path.setText(current["path"])
        self.copyButton.setEnabled(bool(current["path"]))
        self.openButton.setEnabled(bool(current["path"]))
        self.stopButton.setEnabled(current["active"])
        if current["active"]:
            status = "Logging active; " + (
                f"armed once: {current['armed']}" if current["armed"] != "off"
                else "no fault armed"
            )
        else:
            status = "Logging stopped"
        if current["last_claim"]:
            status += f". Last injected job: {current['last_claim']}"
        self.status.setText(status)
