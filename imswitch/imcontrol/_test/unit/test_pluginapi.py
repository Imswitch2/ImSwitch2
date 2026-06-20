"""Tests for the public plugin API surface."""

import pytest


def test_public_api_imports():
    """Test that all public API symbols can be imported."""
    from imswitch.pluginapi import (
        DeviceInfo,
        DetectorInfo,
        LaserInfo,
        PositionerInfo,
        RS232Info,
        DetectorManager,
        DetectorAction,
        DetectorNumberParameter,
        DetectorListParameter,
        LaserManager,
        PositionerManager,
        RotatorManager,
    )
    
    # Verify they are classes (or in the case of DetectorAction, likely an Enum)
    assert DeviceInfo is not None
    assert DetectorInfo is not None
    assert LaserInfo is not None
    assert PositionerInfo is not None
    assert RS232Info is not None
    assert DetectorManager is not None
    assert DetectorAction is not None
    assert DetectorNumberParameter is not None
    assert DetectorListParameter is not None
    assert LaserManager is not None
    assert PositionerManager is not None
    assert RotatorManager is not None


def test_public_api_all_list():
    """Test that __all__ is correctly defined."""
    import imswitch.pluginapi as pluginapi
    
    assert hasattr(pluginapi, "__all__")
    expected = {
        "DeviceInfo",
        "DetectorInfo",
        "LaserInfo",
        "PositionerInfo",
        "RS232Info",
        "DetectorManager",
        "DetectorAction",
        "DetectorParameter",
        "DetectorNumberParameter",
        "DetectorListParameter",
        "LaserManager",
        "PositionerManager",
        "RotatorManager",
    }
    assert set(pluginapi.__all__) == expected
