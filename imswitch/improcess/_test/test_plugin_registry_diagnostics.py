"""
Tests for plugin registry diagnostics (Task D — audit report 11).

Verifies that:
1. Known plugin IDs resolve to the correct built-in plugins
2. Unknown plugin IDs raise clear diagnostics listing available IDs
3. No silent drops when a plugin ID is not found
"""
import pytest


def test_get_reconstructor_known_id():
    """get_reconstructor returns the correct plugin for a known ID."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import register_default_reconstructors
    
    registry = PluginRegistry()
    register_default_reconstructors(registry)
    
    # Verify a known reconstructor resolves correctly
    plugin = registry.get_reconstructor('view-only')
    assert plugin is not None
    assert plugin.id == 'view-only'


def test_get_reconstructor_unknown_id_raises():
    """get_reconstructor raises KeyError with diagnostic for unknown ID."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import register_default_reconstructors
    
    registry = PluginRegistry()
    register_default_reconstructors(registry)
    
    # Verify unknown ID raises KeyError with helpful message
    with pytest.raises(KeyError) as exc_info:
        registry.get_reconstructor('nonexistent-plugin')
    
    error_msg = str(exc_info.value)
    # Error should mention the missing plugin ID
    assert 'nonexistent-plugin' in error_msg
    # Error should list available plugins
    assert 'Available reconstructors:' in error_msg
    assert 'view-only' in error_msg


def test_get_reconstructor_unknown_id_with_raise_false():
    """get_reconstructor returns None when raise_on_missing=False."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import register_default_reconstructors
    
    registry = PluginRegistry()
    register_default_reconstructors(registry)
    
    # Verify legacy behavior (return None) when explicitly requested
    plugin = registry.get_reconstructor('nonexistent-plugin', raise_on_missing=False)
    assert plugin is None


def test_get_processor_known_id():
    """get_processor returns the correct plugin for a known ID."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import register_default_processors
    
    registry = PluginRegistry()
    register_default_processors(registry)
    
    # Verify a known processor resolves correctly
    plugin = registry.get_processor('projection')
    assert plugin is not None
    assert plugin.id == 'projection'


def test_get_processor_unknown_id_raises():
    """get_processor raises KeyError with diagnostic for unknown ID."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import register_default_processors
    
    registry = PluginRegistry()
    register_default_processors(registry)
    
    # Verify unknown ID raises KeyError with helpful message
    with pytest.raises(KeyError) as exc_info:
        registry.get_processor('unknown-processor')
    
    error_msg = str(exc_info.value)
    # Error should mention the missing plugin ID
    assert 'unknown-processor' in error_msg
    # Error should list available plugins
    assert 'Available processors:' in error_msg
    assert 'projection' in error_msg


def test_get_processor_unknown_id_with_raise_false():
    """get_processor returns None when raise_on_missing=False."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import register_default_processors
    
    registry = PluginRegistry()
    register_default_processors(registry)
    
    # Verify legacy behavior (return None) when explicitly requested
    plugin = registry.get_processor('unknown-processor', raise_on_missing=False)
    assert plugin is None


def test_all_builtin_reconstructors_resolve():
    """All built-in reconstructor IDs can be successfully retrieved."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import (
        register_default_reconstructors,
        available_reconstructor_ids,
    )
    
    registry = PluginRegistry()
    register_default_reconstructors(registry)
    
    # Verify every advertised ID resolves
    for reconstructor_id in available_reconstructor_ids():
        plugin = registry.get_reconstructor(reconstructor_id)
        assert plugin is not None
        assert plugin.id == reconstructor_id


def test_register_default_reconstructors_preserves_requested_order():
    """Explicit setup order controls the active/default reconstructor order."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import register_default_reconstructors

    registry = PluginRegistry()
    register_default_reconstructors(registry, ["widefield-starss", "view-only"])

    assert [plugin.id for plugin in registry.reconstructors()] == [
        "widefield-starss",
        "view-only",
    ]


def test_register_default_reconstructors_unknown_id_raises():
    """Explicit reconstructor config should fail loudly on stale/typo IDs."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.reconstructors import register_default_reconstructors

    registry = PluginRegistry()
    with pytest.raises(KeyError) as exc_info:
        register_default_reconstructors(registry, ["view-only", "typo"])

    error_msg = str(exc_info.value)
    assert "typo" in error_msg
    assert "Available reconstructors:" in error_msg
    assert registry.reconstructors() == []


def test_all_builtin_processors_resolve():
    """All built-in processor IDs can be successfully retrieved."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import (
        register_default_processors,
        available_processor_ids,
    )
    
    registry = PluginRegistry()
    register_default_processors(registry)
    
    # Verify every advertised ID resolves
    for processor_id in available_processor_ids():
        plugin = registry.get_processor(processor_id)
        assert plugin is not None
        assert plugin.id == processor_id


def test_register_default_processors_preserves_requested_order():
    """Explicit setup order controls processor registration order."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import register_default_processors

    registry = PluginRegistry()
    register_default_processors(registry, ["projection", "drift-correct"])

    assert [plugin.id for plugin in registry.processors()] == [
        "projection",
        "drift-correct",
    ]


def test_register_default_processors_unknown_id_raises():
    """Explicit processor config should fail loudly on stale/typo IDs."""
    from imswitch.improcess.reconstructors.registry import PluginRegistry
    from imswitch.improcess.processors import register_default_processors

    registry = PluginRegistry()
    with pytest.raises(KeyError) as exc_info:
        register_default_processors(registry, ["projection", "typo"])

    error_msg = str(exc_info.value)
    assert "typo" in error_msg
    assert "Available processors:" in error_msg
    assert registry.processors() == []


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
