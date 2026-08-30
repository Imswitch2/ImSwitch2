from __future__ import annotations

from qtpy import QtCore, QtWidgets

from .basewidgets import Widget


_KIND_LABELS = {
    "detector": "Detector",
    "laser": "Laser",
    "positioner": "Positioner",
    "rotator": "Rotator",
    "flip_mirror": "Flip mirror",
    "rs232": "RS232",
    "slm": "SLM",
    "daq": "DAQ",
    "pulse_generator": "Pulse generator",
    "trigger_scope": "TriggerScope",
    "stand": "Microscope stand",
}


class HardwareStatusWidget(Widget):
    """Global read-only hardware status window.

    This widget is intentionally independent of ``availableWidgets`` so it is
    available for every ImControl setup through the Tools menu.
    """

    sigRefreshRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle("Hardware status")
        self.resize(900, 520)

        self._statuses = []

        self.refreshButton = QtWidgets.QPushButton("Refresh")
        self.refreshButton.clicked.connect(self.sigRefreshRequested)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Device", "Type", "Connection", "Mode", "Manager", "Summary"]
        )
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._updateDetails)

        detailsGroup = QtWidgets.QGroupBox("Details")
        detailsLayout = QtWidgets.QFormLayout(detailsGroup)
        self.detailDevice = QtWidgets.QLabel("—")
        self.detailManager = QtWidgets.QLabel("—")
        self.detailFailure = QtWidgets.QLabel("—")
        self.detailMessage = QtWidgets.QLabel("—")
        self.detailMessage.setWordWrap(True)
        detailsLayout.addRow("Device", self.detailDevice)
        detailsLayout.addRow("Manager", self.detailManager)
        detailsLayout.addRow("Failure", self.detailFailure)
        detailsLayout.addRow("Message", self.detailMessage)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel(
            "Passive runtime status only; Refresh does not probe hardware."
        ))
        top.addStretch(1)
        top.addWidget(self.refreshButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(detailsGroup)

    def setStatuses(self, statuses) -> None:
        selected_id = None
        selected_rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if selected_rows:
            row = selected_rows[0].row()
            if 0 <= row < len(self._statuses):
                selected_id = self._statuses[row].device_id

        self._statuses = list(statuses)
        self.table.setRowCount(len(self._statuses))

        selected_row = None
        for row, status in enumerate(self._statuses):
            values = (
                status.name,
                _KIND_LABELS.get(status.kind, status.kind),
                status.connection.value.upper(),
                status.mode.value.upper(),
                status.manager_name,
                status.summary or "",
            )
            for col, value in enumerate(values):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))
            if selected_id is not None and status.device_id == selected_id:
                selected_row = row

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        if selected_row is not None:
            self.table.selectRow(selected_row)
        elif self._statuses:
            self.table.selectRow(0)
        else:
            self._clearDetails()

    def _clearDetails(self) -> None:
        self.detailDevice.setText("—")
        self.detailManager.setText("—")
        self.detailFailure.setText("—")
        self.detailMessage.setText("—")

    def _updateDetails(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            self._clearDetails()
            return
        row = rows[0].row()
        if not (0 <= row < len(self._statuses)):
            self._clearDetails()
            return

        status = self._statuses[row]
        self.detailDevice.setText(f"{status.name} ({_KIND_LABELS.get(status.kind, status.kind)})")
        self.detailManager.setText(status.manager_name)
        self.detailFailure.setText(
            status.failure_kind.value if status.failure_kind is not None else "—"
        )
        message_parts = [part for part in (status.summary, status.details) if part]
        self.detailMessage.setText("\n".join(message_parts) if message_parts else "—")
