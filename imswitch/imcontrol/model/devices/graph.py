from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .status import (
    DeviceConnectionState, DeviceFailureKind, DeviceId, DeviceRuntimeMode, DeviceStatus,
)


class DeviceRole(str, Enum):
    """How a configured manager participates in the user-facing device model."""

    PRIMARY = "primary"
    COMPONENT = "component"
    RESOURCE = "resource"


class DeviceRelationKind(str, Enum):
    """Relationship types used by the runtime device graph."""

    COMPONENT_OF = "component_of"
    USES_TRANSPORT = "uses_transport"
    USES_CONTROL_BACKEND = "uses_control_backend"


class DeviceSection(str, Enum):
    """Top-level presentation section for a user-facing hardware node."""

    DEVICES = "devices"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True, order=True)
class HardwareDeviceId:
    """Stable identity for one user-meaningful physical device/infrastructure node."""

    category: str
    key: str


GraphNodeId = DeviceId | HardwareDeviceId


@dataclass(frozen=True)
class DeviceDependencySpec:
    """Dependency declared by one configured manager/device."""

    kind: DeviceRelationKind
    target: GraphNodeId
    label: str | None = None


@dataclass(frozen=True)
class DeviceDescriptorSpec:
    """Optional manager-provided description of its physical-device relationship.

    The supervisor owns the configured manager identity. Managers only declare
    the unusual part: whether they are a component/resource, which physical
    device they belong to, and which lower-level resources they use.
    """

    role: DeviceRole = DeviceRole.PRIMARY
    hardware_id: HardwareDeviceId | None = None
    display_name: str | None = None
    category: str | None = None
    section: DeviceSection = DeviceSection.DEVICES
    dependencies: tuple[DeviceDependencySpec, ...] = ()


@dataclass(frozen=True)
class DeviceDescriptor:
    """Resolved descriptor for one configured manager endpoint."""

    source_device_id: DeviceId
    hardware_id: HardwareDeviceId
    display_name: str
    category: str
    role: DeviceRole
    section: DeviceSection
    manager_name: str


@dataclass(frozen=True)
class DeviceRelation:
    source: GraphNodeId
    kind: DeviceRelationKind
    target: GraphNodeId
    label: str | None = None


@dataclass(frozen=True)
class DeviceGraph:
    """Normalized runtime map from configured managers to physical hardware."""

    descriptors: tuple[DeviceDescriptor, ...]
    relations: tuple[DeviceRelation, ...]

    def descriptorsForHardware(self, hardware_id: HardwareDeviceId) -> tuple[DeviceDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self.descriptors
            if descriptor.hardware_id == hardware_id
        )

    def relationsFrom(self, node_id: GraphNodeId) -> tuple[DeviceRelation, ...]:
        return tuple(relation for relation in self.relations if relation.source == node_id)


@dataclass(frozen=True)
class HardwareDependencyStatus:
    kind: DeviceRelationKind
    name: str
    status: DeviceStatus


@dataclass(frozen=True)
class HardwareComponentStatus:
    """Cached status for one logical capability of a physical device."""

    device_id: DeviceId
    status: DeviceStatus

    @property
    def name(self) -> str:
        return self.device_id.name

    @property
    def category(self) -> str:
        return self.device_id.kind


@dataclass(frozen=True)
class HardwareStatus:
    """Supervisor-resolved status for one user-facing hardware node."""

    hardware_id: HardwareDeviceId
    name: str
    category: str
    section: DeviceSection
    connection: DeviceConnectionState
    mode: DeviceRuntimeMode
    summary: str | None = None
    details: str | None = None
    failure_kind: DeviceFailureKind | None = None
    manager_names: tuple[str, ...] = ()
    components: tuple[HardwareComponentStatus, ...] = ()
    dependencies: tuple[HardwareDependencyStatus, ...] = ()


@runtime_checkable
class DeviceDescriptorProvider(Protocol):
    def getDeviceDescriptorSpec(self) -> DeviceDescriptorSpec:
        """Return cached/config-derived device relationships; never perform I/O."""
        ...


def sharedRs232ComponentSpec(
    *, category: str, family: str, display_name: str, rs232_name: str
) -> DeviceDescriptorSpec:
    """Descriptor helper for logical channels of one serial physical device."""
    hardware_id = HardwareDeviceId(category=category, key=f"{family}:{rs232_name}")
    return DeviceDescriptorSpec(
        role=DeviceRole.COMPONENT,
        hardware_id=hardware_id,
        display_name=display_name,
        category=category,
        dependencies=(
            DeviceDependencySpec(
                kind=DeviceRelationKind.USES_TRANSPORT,
                target=DeviceId("rs232", str(rs232_name)),
                label=str(rs232_name),
            ),
        ),
    )


def rs232BackedPrimarySpec(
    *, category: str, rs232_name: str, display_name: str | None = None,
    hardware_id: HardwareDeviceId | None = None,
) -> DeviceDescriptorSpec:
    """Descriptor helper for one physical device using a generic RS232 transport."""
    return DeviceDescriptorSpec(
        role=DeviceRole.PRIMARY,
        hardware_id=hardware_id,
        display_name=display_name,
        category=category,
        dependencies=(
            DeviceDependencySpec(
                kind=DeviceRelationKind.USES_TRANSPORT,
                target=DeviceId("rs232", str(rs232_name)),
                label=str(rs232_name),
            ),
        ),
    )
