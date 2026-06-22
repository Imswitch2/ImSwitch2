"""Tests for the Phase 1 shortcut catalog system with action IDs."""
import pytest
from imswitch.imcommon.model import shortcut, generateShortcuts, getBoundShortcuts, ShortcutScope

pytestmark = pytest.mark.nohardware


class FakeWidget:
    """Fake widget with various shortcut declarations for testing."""
    
    @shortcut(actionId="test.action1", defaultKey="Ctrl+A",
              displayName="Test Action 1", initiallyBound=True)
    def action1(self):
        pass
    
    @shortcut(actionId="test.action2", defaultKey="Ctrl+B",
              displayName="Test Action 2", initiallyBound=False)
    def action2(self):
        """This is catalog-only (not initially bound)."""
        pass
    
    @shortcut("Ctrl+C", "Test Legacy")
    def legacyAction(self):
        """Legacy positional form should still work."""
        pass
    
    @shortcut(actionId="test.noKey", defaultKey=None,
              displayName="No Key Action", initiallyBound=True)
    def noKeyAction(self):
        """Even with initiallyBound=True, no key means not bound."""
        pass


class DuplicateWidget:
    """Widget with a duplicate action ID for testing error detection."""
    
    @shortcut(actionId="test.duplicate", defaultKey="Ctrl+D",
              displayName="Duplicate 1", initiallyBound=True)
    def dup1(self):
        pass
    
    @shortcut(actionId="test.duplicate", defaultKey="Ctrl+E",
              displayName="Duplicate 2", initiallyBound=True)
    def dup2(self):
        pass


def test_generate_shortcuts_returns_actionid_dict():
    """Test that generateShortcuts returns a dict keyed by actionId."""
    widget = FakeWidget()
    shortcuts = generateShortcuts([widget])
    
    # Should be keyed by actionId
    assert "test.action1" in shortcuts
    assert "test.action2" in shortcuts
    assert "test.noKey" in shortcuts
    
    # Legacy should get a derived actionId (class.method)
    assert "FakeWidget.legacyAction" in shortcuts
    
    # Check ShortcutAction structure
    action1 = shortcuts["test.action1"]
    assert action1.actionId == "test.action1"
    assert action1.defaultKeySequence == "Ctrl+A"
    assert action1.displayName == "Test Action 1"
    assert action1.initiallyBound is True
    assert callable(action1.callback)


def test_catalog_only_action_in_catalog_not_in_bound():
    """Test that catalog-only actions appear in full catalog but not bound subset."""
    widget = FakeWidget()
    shortcuts = generateShortcuts([widget])
    boundShortcuts = getBoundShortcuts(shortcuts)
    
    # Catalog-only action (initiallyBound=False) should be in catalog
    assert "test.action2" in shortcuts
    
    # But NOT in bound shortcuts
    assert "test.action2" not in boundShortcuts
    
    # Regular bound action should be in both
    assert "test.action1" in shortcuts
    assert "test.action1" in boundShortcuts


def test_no_key_action_not_bound():
    """Test that actions with no defaultKey are not in bound subset."""
    widget = FakeWidget()
    shortcuts = generateShortcuts([widget])
    boundShortcuts = getBoundShortcuts(shortcuts)
    
    # Action with no key should be in catalog
    assert "test.noKey" in shortcuts
    
    # But NOT in bound shortcuts (even though initiallyBound=True)
    assert "test.noKey" not in boundShortcuts


def test_legacy_positional_form_works():
    """Test that legacy @shortcut("Ctrl+C", "Name") still works."""
    widget = FakeWidget()
    shortcuts = generateShortcuts([widget])
    
    # Should have derived actionId from class.method
    legacy_id = "FakeWidget.legacyAction"
    assert legacy_id in shortcuts
    
    action = shortcuts[legacy_id]
    assert action.defaultKeySequence == "Ctrl+C"
    assert action.displayName == "Test Legacy"
    assert action.initiallyBound is True  # Legacy defaults to True


def test_duplicate_actionid_raises_error():
    """Test that duplicate action IDs raise a clear error."""
    widget = DuplicateWidget()
    
    with pytest.raises(RuntimeError, match="Duplicate shortcut actionId.*test.duplicate"):
        generateShortcuts([widget])


def test_bound_subset_matches_expected():
    """Test that the bound subset contains exactly the expected actions."""
    widget = FakeWidget()
    shortcuts = generateShortcuts([widget])
    boundShortcuts = getBoundShortcuts(shortcuts)
    
    # Should have exactly 2 bound actions:
    # - test.action1 (initiallyBound=True, has key)
    # - FakeWidget.legacyAction (legacy, has key)
    assert len(boundShortcuts) == 2
    assert "test.action1" in boundShortcuts
    assert "FakeWidget.legacyAction" in boundShortcuts


def test_scope_enum_exists():
    """Test that ShortcutScope enum is available."""
    assert hasattr(ShortcutScope, 'Application')
    assert hasattr(ShortcutScope, 'Window')
    assert hasattr(ShortcutScope, 'WidgetLocal')
    assert hasattr(ShortcutScope, 'PressRelease')


def test_grbl_catalog_only_not_bound():
    """Test that GRBL manager shortcuts are catalogued but NOT bound.
    
    This is critical: Phase 1 must not change which shortcuts are actually active.
    GRBL shortcuts should appear in the catalog but must NOT be in the bound subset.
    """
    # Mock GRBL manager with catalog-only shortcuts
    class MockGRBLManager:
        @shortcut(actionId="grbl.moveUp", defaultKey="Up",
                  displayName="Move up", initiallyBound=False)
        def key_moveXup(self):
            pass
        
        @shortcut(actionId="grbl.moveDown", defaultKey="Down",
                  displayName="Move down", initiallyBound=False)
        def key_moveXdown(self):
            pass
    
    # Mock widget with normal bound shortcuts
    class MockWidget:
        @shortcut(actionId="widget.action", defaultKey="Ctrl+W",
                  displayName="Widget Action", initiallyBound=True)
        def action(self):
            pass
    
    widget = MockWidget()
    manager = MockGRBLManager()
    
    # Collect from both
    allShortcuts = generateShortcuts([widget, manager])
    boundShortcuts = getBoundShortcuts(allShortcuts)
    
    # All 3 actions should be in the catalog
    assert "grbl.moveUp" in allShortcuts
    assert "grbl.moveDown" in allShortcuts
    assert "widget.action" in allShortcuts
    
    # Only the widget action should be bound
    assert "widget.action" in boundShortcuts
    assert "grbl.moveUp" not in boundShortcuts
    assert "grbl.moveDown" not in boundShortcuts
    
    # Verify the bound subset has exactly 1 action
    assert len(boundShortcuts) == 1
