from __future__ import annotations

from collections import defaultdict

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


def _health(status) -> str:
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
            else:
                counts["unavailable"] += 1
        else:
            counts[health] += 1

    noun = "resource" if category == "infrastructure" else "device"
    parts = [f"{len(statuses)} {noun}{'s' if len(statuses) != 1 else ''}"]
    labels = (
        ("ok", "connected"),
        ("mock", "mock"),
        ("issue", "issue"),
        ("unavailable", "status unavailable"),
    )
    for key, label in labels:
        count = counts[key]
        if count:
            parts.append(f"{count} {label}")
    return " · ".join(parts)


class HardwareStatusWidget(Widget):
    """Global read-only hardware status grouped by user-meaningful device."""

    sigRefreshRequested = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle("Hardware status")
        self.resize(760, 620)

        self._statuses = []
        self._statusByHardwareId = {}

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
        detailsLayout.addRow("Components", self.detailComponents)
        detailsLayout.addRow("Dependencies", self.detailDependencies)
        detailsLayout.addRow("Failure", self.detailFailure)
        detailsLayout.addRow("Message", self.detailMessage)

        top = QtWidgets.QHBoxLayout()
        top.addStretch(1)
        top.addWidget(self.refreshButton)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(detailsGroup)

    def setStatuses(self, statuses) -> None:
        selected_id = self._selectedHardwareId()
        expanded = self._expandedCategories()

        self._statuses = list(statuses)
        self._statusByHardwareId = {
            status.hardware_id: status for status in self._statuses
        }
        self.tree.clear()

        grouped = defaultdict(list)
        for status in self._statuses:
            grouped[status.category].append(status)

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
                suffix = ""
                if _health(status) == "neutral" and status.mode is DeviceRuntimeMode.MOCK:
                    suffix = "    Mock"
                elif status.connection is DeviceConnectionState.UNKNOWN:
                    suffix = "    Status unavailable"
                elif _health(status) == "issue":
                    suffix = "    Connection issue"
                child.setText(0, f"{status.name}{suffix}")
                child.setData(0, QtCore.Qt.UserRole, status.hardware_id)
                child.setToolTip(0, self._tooltipFor(status))
                if first_child is None:
                    first_child = child
                if selected_id is not None and status.hardware_id == selected_id:
                    selected_item = child

        target = selected_item or first_child
        if target is not None:
            self.tree.setCurrentItem(target)
        else:
            self._clearDetails()

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

    def _selectedHardwareId(self):
        selected = self.tree.selectedItems()
        if not selected:
            return None
        return selected[0].data(0, QtCore.Qt.UserRole)

    def _tooltipFor(self, status) -> str:
        parts = [_HEALTH_LABELS[_health(status)]]
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
            self.detailComponents,
            self.detailDependencies,
            self.detailFailure,
            self.detailMessage,
        ):
            label.setText("—")

    def _updateDetails(self) -> None:
        hardware_id = self._selectedHardwareId()
        status = self._statusByHardwareId.get(hardware_id)
        if status is None:
            self._clearDetails()
            return

        health = _health(status)
        self.detailDevice.setText(status.name)
        self.detailHealth.setText(_HEALTH_LABELS[health])
        self.detailHealth.setStyleSheet(f"color: {_HEALTH_COLORS[health]}; font-weight: 600;")
        self.detailConnection.setText(_connection_text(status))
        self.detailMode.setText(status.mode.value.upper())
        self.detailManagers.setText(", ".join(status.manager_names) or "—")
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
