from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceFailureKind,
    DeviceId,
    DeviceRuntimeMode,
    DeviceStatus,
)
from imswitch.imcontrol.view.widgets.HardwareStatusWidget import HardwareStatusWidget


def _status(kind, name, connection, mode, *, summary=None, failure_kind=None):
    return DeviceStatus(
        device_id=DeviceId(kind, name),
        manager_name=f"{name}Manager",
        connection=connection,
        mode=mode,
        summary=summary,
        failure_kind=failure_kind,
    )


def test_hardware_status_widget_renders_status_and_details(qtbot):
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
    ]

    widget.setStatuses(statuses)

    assert widget.table.rowCount() == 2
    assert widget.table.item(0, 0).text() == "Camera"
    assert widget.table.item(0, 2).text() == "CONNECTED"
    assert widget.table.item(1, 3).text() == "MOCK"

    widget.table.selectRow(1)
    assert widget.detailDevice.text().startswith("488")
    assert widget.detailFailure.text() == "connection_error"
    assert "mock fallback" in widget.detailMessage.text().lower()


def test_hardware_status_widget_preserves_selected_device_across_refresh(qtbot):
    widget = HardwareStatusWidget(None)
    qtbot.addWidget(widget)

    camera = _status(
        "detector", "Camera", DeviceConnectionState.UNKNOWN,
        DeviceRuntimeMode.REAL,
    )
    laser = _status(
        "laser", "488", DeviceConnectionState.UNKNOWN,
        DeviceRuntimeMode.MOCK,
    )
    widget.setStatuses([camera, laser])
    widget.table.selectRow(1)

    widget.setStatuses([laser, camera])

    selected = widget.table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert widget.table.item(selected[0].row(), 0).text() == "488"
