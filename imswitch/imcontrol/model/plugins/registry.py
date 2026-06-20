"""Device plugin registry for resolving and loading manager classes."""

import importlib
from typing import Type

from .manifest import DeviceManagerContribution
from .discovery import discover_contributions


class DuplicateContributionError(Exception):
    """Raised when a duplicate contribution is registered."""
    pass


class UnknownDeviceManagerError(RuntimeError):
    """Raised when a device manager cannot be resolved."""
    pass


class DevicePluginRegistry:
    """Registry for device manager contributions from plugins and built-ins."""
    
    def __init__(self):
        # Index by (kind, id) -> contribution
        self._by_id: dict[tuple[str, str], DeviceManagerContribution] = {}
        # Index by (kind, alias) -> contribution
        self._by_alias: dict[tuple[str, str], DeviceManagerContribution] = {}
        # Track which contributions are built-ins
        self._builtins: set[tuple[str, str]] = set()
    
    def register(
        self,
        contribution: DeviceManagerContribution,
        *,
        is_builtin: bool = False,
    ) -> None:
        """Register a device manager contribution.
        
        Args:
            contribution: The contribution to register.
            is_builtin: If True, marks this as a built-in contribution that
                cannot be overridden by plugins.
        
        Raises:
            DuplicateContributionError: If a duplicate (kind, id) or if a
                plugin tries to override a built-in.
        """
        key = (contribution.kind, contribution.id)
        
        # Check if plugin is trying to override a built-in
        if not is_builtin and key in self._builtins:
            existing = self._by_id[key]
            raise DuplicateContributionError(
                f"Cannot override built-in {contribution.kind} manager "
                f"'{contribution.id}' from plugin '{contribution.plugin_name}'"
            )
        
        # Check for duplicate ID
        if key in self._by_id:
            existing = self._by_id[key]
            raise DuplicateContributionError(
                f"Duplicate {contribution.kind} manager '{contribution.id}': "
                f"already registered by plugin '{existing.plugin_name}', "
                f"cannot register from '{contribution.plugin_name}'"
            )
        
        # Register by ID
        self._by_id[key] = contribution
        if is_builtin:
            self._builtins.add(key)
        
        # Register by aliases
        for alias in contribution.manager_name_aliases:
            alias_key = (contribution.kind, alias)
            
            # Check if alias collision with built-in
            if not is_builtin and alias_key in self._builtins:
                existing = self._by_alias.get(alias_key) or self._by_id.get(alias_key)
                if existing:
                    raise DuplicateContributionError(
                        f"Cannot use alias '{alias}' for {contribution.kind} "
                        f"manager '{contribution.id}': conflicts with built-in "
                        f"'{existing.id}'"
                    )
            
            # Aliases can map to multiple IDs only if identical contribution
            if alias_key in self._by_alias:
                existing = self._by_alias[alias_key]
                if existing != contribution:
                    raise DuplicateContributionError(
                        f"Alias '{alias}' for {contribution.kind} manager "
                        f"already resolves to '{existing.id}' from plugin "
                        f"'{existing.plugin_name}', cannot also resolve to "
                        f"'{contribution.id}' from '{contribution.plugin_name}'"
                    )
            else:
                self._by_alias[alias_key] = contribution
                if is_builtin:
                    self._builtins.add(alias_key)
    
    def resolve(
        self,
        kind: str,
        manager_name: str,
    ) -> DeviceManagerContribution | None:
        """Resolve a manager name to a contribution.
        
        Resolution order: exact ID match first, then alias match.
        
        Args:
            kind: The device kind (e.g., "detector", "laser").
            manager_name: The manager name or alias to resolve.
        
        Returns:
            The matching contribution, or None if not found.
        """
        key = (kind, manager_name)
        
        # Try ID first
        if key in self._by_id:
            return self._by_id[key]
        
        # Try alias
        if key in self._by_alias:
            return self._by_alias[key]
        
        return None
    
    def load_python_object(self, python_name: str):
        """Load a Python object from a 'module:attr' string.
        
        Args:
            python_name: String in format "module.path:ObjectName".
        
        Returns:
            The loaded object.
        """
        module_name, object_name = python_name.split(":", 1)
        module = importlib.import_module(module_name)
        return getattr(module, object_name)
    
    def load_manager_class(
        self,
        kind: str,
        manager_name: str,
        *,
        prefer_mock: bool = False,
    ) -> Type | None:
        """Resolve and load a manager class.
        
        Args:
            kind: The device kind.
            manager_name: The manager name or alias.
            prefer_mock: If True and the contribution has a mock_python_name,
                load the mock instead of the real implementation.
        
        Returns:
            The manager class, or None if not found.
        """
        contribution = self.resolve(kind, manager_name)
        if contribution is None:
            return None
        
        # Choose mock or real implementation
        if prefer_mock and contribution.mock_python_name:
            python_name = contribution.mock_python_name
        else:
            python_name = contribution.python_name
        
        return self.load_python_object(python_name)
    
    def list_contributions(
        self,
        kind: str | None = None,
    ) -> list[DeviceManagerContribution]:
        """List all registered contributions.
        
        Args:
            kind: If provided, filter to only this device kind.
        
        Returns:
            List of contributions, deduplicated by (kind, id).
        """
        contributions = list(self._by_id.values())
        
        if kind is not None:
            contributions = [c for c in contributions if c.kind == kind]
        
        return contributions
    
    def format_resolution_error(
        self,
        kind: str,
        manager_name: str,
    ) -> str:
        """Format an error message for when a manager cannot be resolved.
        
        Args:
            kind: The device kind that was requested.
            manager_name: The manager name that could not be resolved.
        
        Returns:
            A formatted error message with installed managers listed.
        """
        installed = self.list_contributions(kind=kind)
        
        lines = [
            f"Could not resolve {kind} manager '{manager_name}'.",
            f"Installed {kind} managers:",
        ]
        
        if not installed:
            lines.append(f"  (none)")
        else:
            for contrib in sorted(installed, key=lambda c: c.id):
                lines.append(f"  - {contrib.id} ({contrib.plugin_name})")
                for alias in contrib.manager_name_aliases:
                    lines.append(f"    alias: {alias}")
        
        lines.append("")
        lines.append(
            "Install the required plugin package or correct managerName in your setup."
        )
        
        return "\n".join(lines)


def build_default_registry(*, discover: bool = True) -> DevicePluginRegistry:
    """Build a registry with built-ins and optionally discovered plugins.
    
    Args:
        discover: If True, discover and register plugins from entry points.
    
    Returns:
        A configured DevicePluginRegistry.
    """
    from .builtins import BUILTIN_DEVICE_MANAGERS
    
    registry = DevicePluginRegistry()
    
    # Register built-ins first
    for contrib in BUILTIN_DEVICE_MANAGERS:
        registry.register(contrib, is_builtin=True)
    
    # Discover and register plugins
    if discover:
        contributions, errors = discover_contributions()
        
        for contrib in contributions:
            try:
                registry.register(contrib, is_builtin=False)
            except DuplicateContributionError:
                # Built-ins win on collision; silently skip
                pass
        
        # Errors are collected but not raised (one broken plugin shouldn't
        # prevent the application from starting)
        # TODO: Log discovery errors once logging is configured
    
    return registry
