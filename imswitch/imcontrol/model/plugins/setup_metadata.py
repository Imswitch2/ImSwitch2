"""Canonical metadata for device-manager setup sections.

This module is deliberately declarative: catalog discovery, setup validation,
and the configuration editor can agree on the setup-file shape without
importing a hardware manager.  UI code may add colours and labels, but must not
own another kind-to-section mapping.
"""

from dataclasses import dataclass

#: The package the in-tree managers live in; ``MultiManager`` imports an
#: unregistered one as ``<this>.<legacy_manager_directory>.<ManagerName>``.
CORE_MANAGERS_PACKAGE = "imswitch.imcontrol.model.managers"


@dataclass(frozen=True)
class SetupKindMetadata:
    """How one device-manager kind appears in a setup file and the editor."""

    kind: str
    editor_category: str
    setup_section: str
    legacy_manager_directory: str
    supports_external_plugins: bool


_SETUP_KINDS: tuple[SetupKindMetadata, ...] = (
    SetupKindMetadata("detector", "detectors", "detectors", "detectors", True),
    SetupKindMetadata("laser", "lasers", "lasers", "lasers", True),
    SetupKindMetadata("positioner", "positioners", "positioners", "positioners", True),
    SetupKindMetadata("rotator", "rotators", "rotators", "rotators", True),
    SetupKindMetadata("rs232", "rs232devices", "rs232devices", "rs232", True),
    SetupKindMetadata("slm", "slms", "slms", "slms", True),
    SetupKindMetadata("flip_mirror", "flipMirrors", "flipMirrors", "flipMirrors", True),
    SetupKindMetadata("stand", "stands", "microscopeStand", "stands", True),
    # Pulse generators still use a bespoke loader.  They are catalogued for
    # existing core setups, but third-party manifests must not promise support.
    SetupKindMetadata("pulse_generator", "pulsegen", "pulsegen", "pulsegen", False),
)

KIND_METADATA: dict[str, SetupKindMetadata] = {
    metadata.kind: metadata for metadata in _SETUP_KINDS
}
CATEGORY_METADATA: dict[str, SetupKindMetadata] = {
    metadata.editor_category: metadata for metadata in _SETUP_KINDS
}
SETUP_SECTION_METADATA: dict[str, SetupKindMetadata] = {
    metadata.setup_section: metadata
    for metadata in _SETUP_KINDS
    if metadata.supports_external_plugins
}


def setup_kinds() -> tuple[SetupKindMetadata, ...]:
    """Return all supported setup kinds in deterministic display order."""
    return _SETUP_KINDS


def registry_backed_setup_kinds() -> tuple[SetupKindMetadata, ...]:
    """Return kinds whose runtime loader supports external registry plugins."""
    return tuple(metadata for metadata in _SETUP_KINDS if metadata.supports_external_plugins)


def kind_to_category() -> dict[str, str]:
    """Return the plugin-kind to editor-category mapping."""
    return {metadata.kind: metadata.editor_category for metadata in _SETUP_KINDS}


def setup_section_to_kind() -> dict[str, str]:
    """Return registry-backed setup-file section names and their kinds."""
    return {
        metadata.setup_section: metadata.kind
        for metadata in registry_backed_setup_kinds()
    }


def daq_device_categories() -> tuple[str, ...]:
    """Return setup sections that may declare DAQ channels."""
    return tuple(metadata.editor_category for metadata in _SETUP_KINDS)
