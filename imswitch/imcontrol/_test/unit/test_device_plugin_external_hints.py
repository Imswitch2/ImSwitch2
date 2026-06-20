"""Tests for the Phase 8 extraction safety net: install hints for managers that
live in (or were extracted to) external plugin packages."""

from imswitch.imcontrol.model.plugins.external import (
    ExternalManagerHint,
    lookup_external_hint,
)
from imswitch.imcontrol.model.plugins.registry import build_default_registry


def test_install_command_with_and_without_extra():
    assert ExternalManagerHint("imswitch-device-x").install_command() == (
        "pip install imswitch-device-x"
    )
    assert ExternalManagerHint(
        "imswitch-device-x", extra="hardware"
    ).install_command() == "pip install imswitch-device-x[hardware]"


def test_lookup_known_and_unknown():
    hint = lookup_external_hint("detector", "zhinst.lockin-demod")
    assert hint is not None
    assert hint.package == "imswitch-zhinst-devices"
    # Legacy class-name and alias resolve to the same package.
    assert lookup_external_hint("detector", "ZhinstLockinDetectorManager") is hint
    assert lookup_external_hint("detector", "ZurichLockinDetectorManager") is hint
    # Wrong kind / unknown name -> no hint.
    assert lookup_external_hint("laser", "zhinst.lockin-demod") is None
    assert lookup_external_hint("detector", "NoSuchManager") is None


def test_resolution_error_includes_install_hint_for_known_external():
    registry = build_default_registry(discover=False)
    message = registry.format_resolution_error("detector", "zhinst.lockin-demod")
    assert "imswitch-zhinst-devices" in message
    assert "pip install imswitch-zhinst-devices[hardware]" in message


def test_resolution_error_has_no_hint_for_unknown_manager():
    registry = build_default_registry(discover=False)
    message = registry.format_resolution_error("detector", "TotallyUnknownCam")
    assert "pip install" not in message
    # Still lists installed managers and the generic guidance.
    assert "Installed detector managers:" in message
