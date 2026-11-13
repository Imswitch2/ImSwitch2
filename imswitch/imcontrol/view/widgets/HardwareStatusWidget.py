from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceRelationKind,
    DeviceRuntimeMode,
)
from .basewidgets import Widget


_CATEGORY_LABELS = {
    "laser": "Lasers",
    "detector": "Detectors",
    "positioner": "Positioners",
    "rotator": "Rotators",
    "flip_mirror": "Flip mirrors",
    "slm": "SLMs",
    "stand": "Microscope stands",
    "instrument": "Instruments",
    "infrastructure": "Infrastructure",
}

_CATEGORY_ORDER = {
    "laser": 0,
    "detector": 1,
    "positioner": 2,
    "rotator": 3,
    "flip_mirror": 4,
    "slm": 5,
    "stand": 6,
    "instrument": 7,
    "infrastructure": 100,
}

_HEALTH_COLORS = {
    "ok": "#2e7d32",
    "issue": "#c62828",
    "mixed": "#ef6c00",
    "neutral": "#757575",
}

_HEALTH_LABELS = {
    "ok": "Connected",
    "issue": "Connection issue",
    "neutral": "Status unavailable / not applicable",
}


def _isNotConnected(status) -> bool:
    """Intentionally not connected (a transient instrument): not an issue."""
    return (
        status.mode is DeviceRuntimeMode.ABSENT
        and status.connection is DeviceConnectionState.DISCONNECTED
    )


def _health(status) -> str:
    if _isNotConnected(status):
        return "neutral"
    if status.connection in {
        DeviceConnectionState.ERROR,
        DeviceConnectionState.DISCONNECTED,
    }:
        return "issue"
    if status.mode is DeviceRuntimeMode.MOCK:
        return "neutral"
    if status.connection is DeviceConnectionState.CONNECTED:
        return "ok"
    # UNKNOWN is deliberately neutral at device level. It means ImSwitch has
    # no verified connection fact, not that the device is in a warning state.
    return "neutral"


def _connection_text(status) -> str:
    if status.connection is DeviceConnectionState.NOT_APPLICABLE:
        return "-"
    return status.connection.value.replace("_", " ").title()


def _dot_icon(health: str, size: int = 10) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(size + 4, size + 4)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(_HEALTH_COLORS[health]))
    painter.drawEllipse(2, 2, size, size)
    painter.end()
    return QtGui.QIcon(pixmap)


def _category_health(statuses) -> str:
    healths = {_health(status) for status in statuses}
    # Orange is reserved for a mixed group: at least one verified healthy
    # device and at least one known issue. Neutral children do not degrade a
    # healthy group.
    if "ok" in healths and "issue" in healths:
        return "mixed"
    if "issue" in healths:
        return "issue"
    if "ok" in healths:
        return "ok"
    return "neutral"


def _category_summary(category, statuses) -> str:
    counts = defaultdict(int)
    for status in statuses:
        health = _health(status)
        if health == "neutral":
            if status.mode is DeviceRuntimeMode.MOCK:
                counts["mock"] += 1
            elif _isNotConnected(status):
                counts["absent"] += 1
            else:
                counts["unavailable"] += 1
        else:
            counts[health] += 1

    noun = "resource" if category == "infrastructure" else "device"
    parts = [f"{len(statuses)} {noun}{'s' if len(statuses) != 1 else ''}"]
    labels = (
        ("ok", "connected"),
        ("mock", "mock"),
        ("absent", "not connected"),
        ("issue", "issue"),
        ("unavailable", "status unavailable"),
    )
    for key, label in labels:
        count = counts[key]
        if count:
            parts.append(f"{count} {label}")
    return " · ".join(parts)


@dataclass(frozen=True)
class _StatusRow:
    key: object
    hardware_id: object
    name: str
    category: str
    connection: DeviceConnectionState
    mode: DeviceRuntimeMode
    summary: str | None
    details: str | None
    failure_kind: object | None
    manager_names: tuple[str, ...]
    components: tuple = ()
    dependencies: tuple = ()
    via: str | None = None

    @classmethod
    def forHardware(cls, status):
        return cls(
            key=("hardware", status.hardware_id),
            hardware_id=status.hardware_id,
            name=status.name,
            category=status.category,
            connection=status.connection,
            mode=status.mode,
            summary=status.summary,
            details=status.details,
            failure_kind=status.failure_kind,
            manager_names=status.manager_names,
            components=status.components,
            dependencies=status.dependencies,
        )

    @classmethod
    def forComponent(cls, parent, component):
        status = component.status
        return cls(
            key=("component", parent.hardware_id, component.device_id),
            hardware_id=parent.hardware_id,
            name=component.name,
            category=component.category,
            connection=status.connection,
            mode=status.mode,
            summary=status.summary,
            details=status.details,
            failure_kind=status.failure_kind,
            manager_names=(status.manager_name,),
            dependencies=parent.dependencies,
            via=parent.name,
        )


class HardwareStatusWidget(Widget):
    """Global hardware status with opt-in physical-device lifecycle actions."""

    sigRefreshRequested = QtCore.Signal()
    sigReconnectRequested = QtCore.Signal(object)
    sigConnectRequested = QtCore.Signal(object)
    sigDisconnectRequested = QtCore.Signal(object)
    sigProbeRequested = QtCore.Signal(object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle("Hardware status")
        self.resize(760, 620)

        self._statuses = []
        self._statusByKey = {}
        self._reconnectableHardwareIds = set()
        self._connectableHardwareIds = set()
        self._disconnectableHardwareIds = set()
        self._probeableHardwareIds = set()
        self._reconnectBusy = False

        self.operationLabel = QtWidgets.QLabel("")
        self.operationLabel.setWordWrap(True)
        self.reconnectButton = QtWidgets.QPushButton("Reconnect")
        self.reconnectButton.setEnabled(False)
        self.reconnectButton.clicked.connect(self._requestReconnect)
        self.connectButton = QtWidgets.QPushButton("Connect")
        self.connectButton.setEnabled(False)
        self.connectButton.clicked.connect(self._requestConnect)
        self.disconnectButton = QtWidgets.QPushButton("Disconnect")
        self.disconnectButton.setEnabled(False)
        self.disconnectButton.clicked.connect(self._requestDisconnect)
        self.probeButton = QtWidgets.QPushButton("Check")
        self.probeButton.setEnabled(False)
        self.probeButton.setToolTip("Ask the device whether it is still there (nothing is replaced).")
        self.probeButton.clicked.connect(self._requestProbe)
        self.refreshButton = QtWidgets.QPushButton("Refresh")
        self.refreshButton.clicked.connect(self.sigRefreshRequested)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(20)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._updateDetails)
        self.tree.setUniformRowHeights(True)

        detailsGroup = QtWidgets.QGroupBox("Details")
        detailsLayout = QtWidgets.QFormLayout(detailsGroup)
        self.detailDevice = QtWidgets.QLabel("—")
        self.detailHealth = QtWidgets.QLabel("—")
        self.detailConnection = QtWidgets.QLabel("—")
        self.detailMode = QtWidgets.QLabel("—")
        self.detailManagers = QtWidgets.QLabel("—")
        self.detailVia = QtWidgets.QLabel("—")
        self.detailComponents = QtWidgets.QLabel("—")
        self.detailDependencies = QtWidgets.QLabel("—")
        self.detailDependencies.setWordWrap(True)
        self.detailFailure = QtWidgets.QLabel("—")
        self.detailMessage = QtWidgets.QLabel("—")
        self.detailMessage.setWordWrap(True)
        detailsLayout.addRow("Device", self.detailDevice)
        detailsLayout.addRow("Health", self.detailHealth)
        detailsLayout.addRow("Connection", self.detailConnection)
        detailsLayout.addRow("Mode", self.detailMode)
        detailsLayout.addRow("Manager(s)", self.detailManagers)
        detailsLayout.addRow("Via", self.detailVia)
        detailsLayout.addRow("Components", self.detailComponents)
        detailsLayout.addRow("Dependencies", self.detailDependencies)
        detailsLayout.addRow("Failure", self.detailFailure)
        detailsLayout.addRow("Message", self.detailMessage)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.operationLabel, 1)
        top.addWidget(self.probeButton)
        top.addWidget(self.connectButton)
        top.addWidget(self.disconnectButton)
        top.addWidget(self.reconnectButton)
        top.addWidget(self.refreshButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(detailsGroup)

    def setStatuses(self, statuses, reconnectableHardwareIds=(),
                    connectableHardwareIds=(), disconnectableHardwareIds=(),
                    probeableHardwareIds=()) -> None:
        selected_key = self._selectedStatusKey()
        self._reconnectableHardwareIds = set(reconnectableHardwareIds)
        self._connectableHardwareIds = set(connectableHardwareIds)
        self._disconnectableHardwareIds = set(disconnectableHardwareIds)
        self._probeableHardwareIds = set(probeableHardwareIds)
        expanded = self._expandedCategories()

        self._statuses = list(statuses)
        rows = []
        for status in self._statuses:
            rows.append(_StatusRow.forHardware(status))
            for component in status.components:
                # Same-category components are implementation details of one
                # physical device (e.g. CoolLED channels). Cross-category
                # components are useful capabilities in their own UI group
                # (e.g. Leica objective Z under Positioners).
                if component.category != status.category:
                    rows.append(_StatusRow.forComponent(status, component))

        self._statusByKey = {row.key: row for row in rows}
        self.tree.clear()

        grouped = defaultdict(list)
        for row in rows:
            grouped[row.category].append(row)

        first_child = None
        selected_item = None
        for category in sorted(
            grouped,
            key=lambda key: (_CATEGORY_ORDER.get(key, 50), _CATEGORY_LABELS.get(key, key)),
        ):
            category_statuses = sorted(
                grouped[category], key=lambda status: status.name.casefold()
            )
            header = QtWidgets.QTreeWidgetItem(self.tree)
            header.setIcon(0, _dot_icon(_category_health(category_statuses)))
            header.setText(
                0,
                f"{_CATEGORY_LABELS.get(category, category.title())}    "
                f"{_category_summary(category, category_statuses)}",
            )
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            header.setFlags(header.flags() & ~QtCore.Qt.ItemIsSelectable)
            header.setExpanded(expanded.get(category, True))

            for status in category_statuses:
                child = QtWidgets.QTreeWidgetItem(header)
                child.setIcon(0, _dot_icon(_health(status)))
                suffix = self._rowSuffix(status)
                child.setText(0, f"{status.name}{suffix}")
                child.setData(0, QtCore.Qt.UserRole, status.key)
                child.setToolTip(0, self._tooltipFor(status))
                if first_child is None:
                    first_child = child
                if selected_key is not None and status.key == selected_key:
                    selected_item = child

        target = selected_item or first_child
        if target is not None:
            self.tree.setCurrentItem(target)
        else:
            self._clearDetails()
        self._updateReconnectButton()

    def setReconnectBusy(self, busy: bool, message: str | None = None) -> None:
        """One lifecycle operation at a time: all action buttons are
        disabled while one runs; ``message`` is shown beside them."""
        self._reconnectBusy = bool(busy)
        self.operationLabel.setText(message or "")
        self._updateReconnectButton()

    def _requestReconnect(self) -> None:
        status = self._statusByKey.get(self._selectedStatusKey())
        if status is None or self._reconnectBusy:
            return
        if status.hardware_id not in self._reconnectableHardwareIds:
            return
        self.sigReconnectRequested.emit(status.hardware_id)

    def _requestConnect(self) -> None:
        status = self._statusByKey.get(self._selectedStatusKey())
        if status is None or self._reconnectBusy:
            return
        if status.hardware_id not in self._connectableHardwareIds:
            return
        self.sigConnectRequested.emit(status.hardware_id)

    def _requestDisconnect(self) -> None:
        status = self._statusByKey.get(self._selectedStatusKey())
        if status is None or self._reconnectBusy:
            return
        if status.hardware_id not in self._disconnectableHardwareIds:
            return
        self.sigDisconnectRequested.emit(status.hardware_id)

    def _requestProbe(self) -> None:
        status = self._statusByKey.get(self._selectedStatusKey())
        if status is None or self._reconnectBusy:
            return
        if status.hardware_id not in self._probeableHardwareIds:
            return
        self.sigProbeRequested.emit(status.hardware_id)

    def _updateReconnectButton(self) -> None:
        status = self._statusByKey.get(self._selectedStatusKey())
        absent = status is not None and status.mode is DeviceRuntimeMode.ABSENT
        canProbe = (
            status is not None
            and status.hardware_id in self._probeableHardwareIds
            and not absent
        )
        self.probeButton.setVisible(status is not None
                                    and status.hardware_id in self._probeableHardwareIds)
        self.probeButton.setEnabled(bool(canProbe and not self._reconnectBusy))
        canConnect = (
            status is not None
            and status.hardware_id in self._connectableHardwareIds
            and status.connection is not DeviceConnectionState.CONNECTED
        )
        canDisconnect = (
            status is not None
            and status.hardware_id in self._disconnectableHardwareIds
            and not absent
        )
        # Connect / Disconnect appear only for devices that offer them.
        offersConnect = status is not None and (
            status.hardware_id in self._connectableHardwareIds
            or status.hardware_id in self._disconnectableHardwareIds
        )
        self.connectButton.setVisible(offersConnect)
        self.disconnectButton.setVisible(offersConnect)
        self.connectButton.setEnabled(bool(canConnect and not self._reconnectBusy))
        self.disconnectButton.setEnabled(bool(canDisconnect and not self._reconnectBusy))
        self._updateReconnectOnlyButton(status, absent)

    def _updateReconnectOnlyButton(self, status, absent) -> None:
        supported = (
            status is not None
            and status.hardware_id in self._reconnectableHardwareIds
            and not absent  # nothing to reconnect: use Connect
        )
        self.reconnectButton.setEnabled(bool(supported and not self._reconnectBusy))
        if supported:
            self.reconnectButton.setToolTip(
                "Reconnect this physical device using its lifecycle adapter."
            )
        else:
            self.reconnectButton.setToolTip(
                "Reconnect is not available for this device yet."
            )

    def _expandedCategories(self):
        result = {}
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            text = item.text(0)
            for category, label in _CATEGORY_LABELS.items():
                if text.startswith(label):
                    result[category] = item.isExpanded()
                    break
        return result

    def _selectedStatusKey(self):
        selected = self.tree.selectedItems()
        if not selected:
            return None
        return selected[0].data(0, QtCore.Qt.UserRole)

    @staticmethod
    def _rowSuffix(status) -> str:
        health = _health(status)
        if health == "neutral" and status.mode is DeviceRuntimeMode.MOCK:
            label = "Mock"
        elif _isNotConnected(status):
            label = "Not connected"
        elif status.connection is DeviceConnectionState.UNKNOWN:
            label = "Status unavailable"
        elif health == "issue":
            label = "Connection issue"
        elif status.via is not None and status.connection is DeviceConnectionState.CONNECTED:
            label = "Connected"
        else:
            label = ""

        if status.via is not None:
            if label:
                return f"    {label} · via {status.via}"
            return f"    via {status.via}"
        return f"    {label}" if label else ""

    def _tooltipFor(self, status) -> str:
        parts = ["Not connected" if _isNotConnected(status) else _HEALTH_LABELS[_health(status)]]
        if status.via:
            parts.append(f"Via {status.via}")
        if status.summary:
            parts.append(status.summary)
        if status.details:
            parts.append(status.details)
        return "\n".join(parts)

    def _clearDetails(self) -> None:
        for label in (
            self.detailDevice,
            self.detailHealth,
            self.detailConnection,
            self.detailMode,
            self.detailManagers,
            self.detailVia,
            self.detailComponents,
            self.detailDependencies,
            self.detailFailure,
            self.detailMessage,
        ):
            label.setText("—")

    def _updateDetails(self) -> None:
        status_key = self._selectedStatusKey()
        status = self._statusByKey.get(status_key)
        if status is None:
            self._clearDetails()
            self._updateReconnectButton()
            return

        health = _health(status)
        self.detailDevice.setText(status.name)
        self.detailHealth.setText(
            "Not connected" if _isNotConnected(status) else _HEALTH_LABELS[health]
        )
        self.detailHealth.setStyleSheet(f"color: {_HEALTH_COLORS[health]}; font-weight: 600;")
        self.detailConnection.setText(_connection_text(status))
        self.detailMode.setText(status.mode.value.upper())
        self.detailManagers.setText(", ".join(status.manager_names) or "—")
        self.detailVia.setText(status.via or "—")
        self.detailComponents.setText(
            ", ".join(component.name for component in status.components) or "—"
        )
        self.detailDependencies.setText(self._formatDependencies(status.dependencies))
        self.detailFailure.setText(
            status.failure_kind.value.replace("_", " ")
            if status.failure_kind is not None
            else "—"
        )
        message_parts = [part for part in (status.summary, status.details) if part]
        self.detailMessage.setText("\n".join(message_parts) if message_parts else "—")
        self._updateReconnectButton()

    @staticmethod
    def _formatDependencies(dependencies) -> str:
        if not dependencies:
            return "—"
        relation_labels = {
            DeviceRelationKind.USES_TRANSPORT: "Transport",
            DeviceRelationKind.USES_CONTROL_BACKEND: "Control backend",
            DeviceRelationKind.COMPONENT_OF: "Component of",
        }
        lines = []
        for dependency in dependencies:
            state = (
                "-"
                if dependency.status.connection is DeviceConnectionState.NOT_APPLICABLE
                else dependency.status.connection.value.replace("_", " ").title()
            )
            lines.append(
                f"{relation_labels.get(dependency.kind, dependency.kind.value)}: "
                f"{dependency.name} — {state} / {dependency.status.mode.value.upper()}"
            )
        return "\n".join(lines)
