from qtpy import QtCore

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
    DeviceSection,
    DeviceStatus,
    HardwareComponentStatus,
    HardwareDeviceId,
    HardwareStatus,
)
from imswitch.imcontrol.view.widgets.HardwareStatusWidget import (
    HardwareStatusWidget, _category_health, _health,
)


def _status(category, name, connection, mode, *, summary=None, failure_kind=None,
            section=DeviceSection.DEVICES):
    return HardwareStatus(
        hardware_id=HardwareDeviceId(category, name),
        name=name,
        category=category,
        section=section,
        connection=connection,
        mode=mode,
        summary=summary,
        failure_kind=failure_kind,
        manager_names=(f"{name}Manager",),
    )


def _find_child(widget, name):
    for top_index in range(widget.tree.topLevelItemCount()):
        parent = widget.tree.topLevelItem(top_index)
        for child_index in range(parent.childCount()):
            child = parent.child(child_index)
            if child.text(0).startswith(name):
                return child
    raise AssertionError(f"No hardware row named {name!r}")


def _find_category(widget, label):
    for index in range(widget.tree.topLevelItemCount()):
        item = widget.tree.topLevelItem(index)
        if item.text(0).startswith(label):
            return item
    raise AssertionError(f"No category named {label!r}")


def test_hardware_status_widget_groups_statuses_and_renders_details(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    statuses = [
        _status(
            "detector", "Camera", DeviceConnectionState.CONNECTED,
            DeviceRuntimeMode.REAL,
        ),
        _status(
            "laser", "488", DeviceConnectionState.ERROR,
            DeviceRuntimeMode.MOCK,
            summary="Using mock fallback after hardware connection failure",
            failure_kind=DeviceFailureKind.CONNECTION_ERROR,
        ),
        _status(
            "positioner", "Mock Z", DeviceConnectionState.NOT_APPLICABLE,
            DeviceRuntimeMode.MOCK,
            summary="Mock positioner configured",
        ),
    ]

    widget.setStatuses(statuses)

    assert widget.tree.topLevelItemCount() == 3
    assert "1 connected" in _find_category(widget, "Detectors").text(0)
    assert "1 issue" in _find_category(widget, "Lasers").text(0)
    assert "1 mock" in _find_category(widget, "Positioners").text(0)

    laser_item = _find_child(widget, "488")
    widget.tree.setCurrentItem(laser_item)
    assert widget.detailDevice.text() == "488"
    assert widget.detailHealth.text() == "Connection issue"
    assert widget.detailFailure.text() == "connection error"
    assert "mock fallback" in widget.detailMessage.text().lower()


def test_hardware_status_widget_preserves_selected_device_across_refresh(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    camera = _status(
        "detector", "Camera", DeviceConnectionState.UNKNOWN,
        DeviceRuntimeMode.REAL,
    )
    laser = _status(
        "laser", "488", DeviceConnectionState.NOT_APPLICABLE,
        DeviceRuntimeMode.MOCK,
    )
    widget.setStatuses([camera, laser])
    widget.tree.setCurrentItem(_find_child(widget, "488"))

    widget.setStatuses([laser, camera])

    selected = widget.tree.selectedItems()
    assert len(selected) == 1
    assert selected[0].text(0).startswith("488")


def test_health_colors_reserve_orange_for_mixed_groups_only():
    connected = _status(
        "laser", "Good", DeviceConnectionState.CONNECTED, DeviceRuntimeMode.REAL
    )
    issue = _status(
        "laser", "Bad", DeviceConnectionState.ERROR, DeviceRuntimeMode.REAL
    )
    unknown = _status(
        "laser", "Unknown", DeviceConnectionState.UNKNOWN, DeviceRuntimeMode.REAL
    )
    mock = _status(
        "laser", "Mock", DeviceConnectionState.NOT_APPLICABLE, DeviceRuntimeMode.MOCK
    )

    assert _health(unknown) == "neutral"
    assert _health(mock) == "neutral"
    assert _category_health([connected, unknown]) == "ok"
    assert _category_health([connected, issue]) == "mixed"
    assert _category_health([issue, unknown]) == "issue"
    assert _category_health([unknown, mock]) == "neutral"


def test_cross_category_component_is_projected_into_its_capability_group(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    z_status = DeviceStatus(
        device_id=DeviceId("positioner", "Objective Z"),
        manager_name="LeicaDMIZPositionerManager",
        connection=DeviceConnectionState.CONNECTED,
        mode=DeviceRuntimeMode.REAL,
        summary="Leica DMI Z connected",
    )
    leica = HardwareStatus(
        hardware_id=HardwareDeviceId("stand", "leica:COM10"),
        name="Leica stand",
        category="stand",
        section=DeviceSection.DEVICES,
        connection=DeviceConnectionState.CONNECTED,
        mode=DeviceRuntimeMode.REAL,
        manager_names=("LeicaDMIStandManager", "LeicaDMIZPositionerManager"),
        components=(
            HardwareComponentStatus(
                device_id=z_status.device_id,
                status=z_status,
            ),
        ),
    )

    widget.setStatuses([leica])

    assert widget.tree.topLevelItemCount() == 2
    assert "1 connected" in _find_category(widget, "Positioners").text(0)
    assert "1 connected" in _find_category(widget, "Microscope stands").text(0)

    z_item = _find_child(widget, "Objective Z")
    assert "Connected · via Leica stand" in z_item.text(0)
    widget.tree.setCurrentItem(z_item)
    assert widget.detailDevice.text() == "Objective Z"
    assert widget.detailVia.text() == "Leica stand"
    assert widget.detailManagers.text() == "LeicaDMIZPositionerManager"


def test_same_category_components_remain_collapsed_in_physical_row(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    channel = DeviceStatus(
        device_id=DeviceId("laser", "365 LED"),
        manager_name="CoolLEDLaserManager",
        connection=DeviceConnectionState.CONNECTED,
        mode=DeviceRuntimeMode.REAL,
    )
    coolled = HardwareStatus(
        hardware_id=HardwareDeviceId("laser", "coolled:COM10"),
        name="CoolLED controller",
        category="laser",
        section=DeviceSection.DEVICES,
        connection=DeviceConnectionState.CONNECTED,
        mode=DeviceRuntimeMode.REAL,
        manager_names=("CoolLEDLaserManager",),
        components=(
            HardwareComponentStatus(device_id=channel.device_id, status=channel),
        ),
    )

    widget.setStatuses([coolled])

    assert widget.tree.topLevelItemCount() == 1
    lasers = _find_category(widget, "Lasers")
    assert lasers.childCount() == 1
    assert lasers.child(0).text(0).startswith("CoolLED controller")


def test_reconnect_button_is_opt_in_per_physical_device(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    hardware_id = HardwareDeviceId("laser", "coolled:COM10")
    coolled = HardwareStatus(
        hardware_id=hardware_id,
        name="CoolLED controller",
        category="laser",
        section=DeviceSection.DEVICES,
        connection=DeviceConnectionState.ERROR,
        mode=DeviceRuntimeMode.MOCK,
        manager_names=("CoolLEDLaserManager",),
    )

    emitted = []
    widget.sigReconnectRequested.connect(emitted.append)
    widget.setStatuses([coolled])
    assert widget.reconnectButton.isEnabled() is False

    widget.setStatuses([coolled], reconnectableHardwareIds=(hardware_id,))
    assert widget.reconnectButton.isEnabled() is True
    qtbot.mouseClick(widget.reconnectButton, QtCore.Qt.LeftButton)
    assert emitted == [hardware_id]

    widget.setReconnectBusy(True, "Reconnecting device…")
    assert widget.reconnectButton.isEnabled() is False
    assert "Reconnecting" in widget.operationLabel.text()
