"""Manager catalog service for the config editor.

This module provides a pure-Python (no Qt dependencies) service for discovering
and cataloging device managers from the plugin registry and legacy filesystem
scanning. It serves as the bridge between the registry system and the config
editor GUI.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from imswitch.imcontrol.model.plugins.setup_metadata import (
    CATEGORY_METADATA,
    KIND_METADATA,
    kind_to_category,
)


# Compatibility exports for existing callers.  The model-owned setup metadata
# is the single source of truth.
KIND_TO_CATEGORY = kind_to_category()
CATEGORY_TO_KIND = {category: metadata.kind for category, metadata in CATEGORY_METADATA.items()}
CATEGORY_TO_DIR = {
    category: metadata.legacy_manager_directory
    for category, metadata in CATEGORY_METADATA.items()
}


@dataclass(frozen=True)
class ManagerInfo:
    """Information about a device manager for the config editor."""
    
    manager_name: str  # contribution id == setup-file managerName
    category: str  # editor category (plural), e.g. "detectors"
    kind: str  # registry kind (singular), e.g. "detector"
    display_name: str
    aliases: tuple[str, ...]
    plugin_name: Optional[str]
    source_package: Optional[str]
    docs_url: Optional[str]
    supported_platforms: tuple[str, ...]
    setup_templates: tuple[str, ...]
    is_builtin: bool
    from_registry: bool  # True from registry, False from legacy scan
    properties_schema: Optional[dict] = None  # resolved managerProperties JSON Schema


@dataclass(frozen=True)
class CoreManagerCoverage:
    """Registry-coverage result for one in-tree selectable manager.

    ``unregistered`` is deliberately distinct from ``legacy_only``.  The
    former is a migration item for a runtime-plugin-capable kind; the latter is
    intentional because that kind still has a bespoke runtime loader.
    """

    manager: ManagerInfo
    status: str  # "registered" | "unregistered" | "legacy_only"


class ManagerCatalog:
    """Catalog of available device managers for the config editor."""
    
    def __init__(self, infos: list[ManagerInfo]):
        self._infos = list(infos)
        # Build indices for fast lookup
        self._by_name: dict[str, ManagerInfo] = {}
        self._by_alias: dict[str, ManagerInfo] = {}
        self._by_category: dict[str, list[ManagerInfo]] = {}
        
        for info in self._infos:
            # Index by manager name (id)
            self._by_name[info.manager_name] = info
            
            # Index by aliases
            for alias in info.aliases:
                # If alias collides, keep the first one (built-ins win)
                if alias not in self._by_alias:
                    self._by_alias[alias] = info
            
            # Index by category
            self._by_category.setdefault(info.category, []).append(info)
        
        # Sort category lists by display name
        for cat_list in self._by_category.values():
            cat_list.sort(key=lambda x: x.display_name.lower())
    
    def managers(self) -> list[ManagerInfo]:
        """Return all manager infos."""
        return list(self._infos)
    
    def by_category(self) -> dict[str, list[ManagerInfo]]:
        """Return managers grouped by category."""
        return {k: list(v) for k, v in self._by_category.items()}
    
    def get(self, manager_name: str) -> Optional[ManagerInfo]:
        """Get manager info by name or alias.
        
        Resolution order: exact name match first, then alias match.
        """
        # Try exact name match first
        if manager_name in self._by_name:
            return self._by_name[manager_name]
        
        # Try alias match
        if manager_name in self._by_alias:
            return self._by_alias[manager_name]
        
        return None
    
    def category_for(self, manager_name: str) -> Optional[str]:
        """Get the editor category for a manager name or alias."""
        info = self.get(manager_name)
        return info.category if info else None
    
    def display_name(self, manager_name: str) -> str:
        """Get the display name for a manager, falling back to the name itself."""
        info = self.get(manager_name)
        return info.display_name if info else manager_name
    
    def all_manager_names(self) -> list[str]:
        """Return all manager names (ids) sorted alphabetically."""
        return sorted(self._by_name.keys())


def build_catalog(
    registry=None,
    *,
    include_legacy_scan: bool = True,
    managers_root: Optional[Path] = None,
    include_generated_schemas: bool = False,
    schemas_root: Optional[Path] = None,
) -> ManagerCatalog:
    """Build a manager catalog from the registry and optional legacy scan.
    
    Args:
        registry: A DevicePluginRegistry instance. If None, builds the default
            registry with discovery enabled.
        include_legacy_scan: If True, also scan the filesystem for managers not
            in the registry (preserves legacy behavior).
        managers_root: Root directory for manager scanning. If None, resolves
            from the package structure.
        include_generated_schemas: If True, a manager without a schema of its
            own gets the generated one from package data (``schemas/managers``)
            through the validator's ``schema_for`` -- a registered manager only
            when it is implemented by the core class the schema describes.
            Off by default: the editor turns any ``properties_schema`` into form
            fields on the spot, and until it preserves omitted and aliased
            keys on Apply (Phase 3 of the schema-extraction plan) that would
            rewrite setup files. The CLI validator always resolves generated
            schemas and does not go through this flag.
        schemas_root: Where to read generated schemas from (tests).
    
    Returns:
        A populated ManagerCatalog instance.
    """
    logger = logging.getLogger(__name__)

    def schema_for(kind, name, contribution):
        """The same resolver the validator uses; the flag is its gate."""
        from imswitch.imcontrol.model.plugins.validation import schema_for as _schema_for

        try:
            return _schema_for(kind, name, contribution,
                               schemas_root=schemas_root, generated=include_generated_schemas)
        except Exception as e:  # a broken plugin schema must not take the catalog down
            logger.debug(f"Could not resolve schema for {name}: {e}")
            return None
    
    # Build or use provided registry
    if registry is None:
        try:
            from imswitch.imcontrol.model.plugins.registry import build_default_registry
            registry = build_default_registry(discover=True)
        except ImportError as e:
            logger.warning(f"Could not import registry: {e}")
            # Create catalog with only legacy scan
            if include_legacy_scan:
                return _catalog_from_legacy_scan(managers_root)
            else:
                return ManagerCatalog([])
    
    infos: list[ManagerInfo] = []
    seen_names: set[str] = set()
    
    # Process registry contributions
    contributions = registry.list_contributions()
    for contrib in contributions:
        # Map kind to category
        metadata = KIND_METADATA.get(contrib.kind)
        if metadata is None:
            # Skip unknown kinds gracefully
            logger.debug(
                f"Skipping contribution {contrib.id} with unmapped kind '{contrib.kind}'"
            )
            continue
        category = metadata.editor_category
        
        # Determine if builtin (from imswitch-core plugin)
        is_builtin = contrib.plugin_name == "imswitch-core"
        
        # The contribution's own schema, else -- when asked for -- the
        # generated one, if the contribution runs the core class it describes.
        schema = schema_for(contrib.kind, contrib.id, contrib)
        
        info = ManagerInfo(
            manager_name=contrib.id,
            category=category,
            kind=contrib.kind,
            display_name=contrib.display_name,
            aliases=contrib.manager_name_aliases,
            plugin_name=contrib.plugin_name,
            source_package=contrib.source_package,
            docs_url=contrib.docs_url,
            supported_platforms=contrib.supported_platforms,
            setup_templates=contrib.setup_templates,
            is_builtin=is_builtin,
            from_registry=True,
            properties_schema=schema,
        )
        infos.append(info)
        seen_names.add(contrib.id)
        logger.debug(
            f"Cataloged {category} manager: {contrib.id} "
            f"(from {'builtin' if is_builtin else contrib.plugin_name})"
        )
    
    # Add legacy filesystem scan results
    if include_legacy_scan:
        legacy_infos = _scan_legacy_managers(managers_root, seen_names)
        if include_generated_schemas:
            legacy_infos = [
                ManagerInfo(**{**info.__dict__,
                               "properties_schema": schema_for(info.kind, info.manager_name, None)})
                for info in legacy_infos
            ]
        infos.extend(legacy_infos)
        if legacy_infos:
            logger.info(
                f"Added {len(legacy_infos)} manager(s) from legacy filesystem scan"
            )
    
    return ManagerCatalog(infos)


def audit_core_manager_coverage(
    registry=None,
    *,
    managers_root: Optional[Path] = None,
) -> list[CoreManagerCoverage]:
    """Classify each in-tree selectable manager against the core registry.

    This intentionally uses the same static scanner as the catalog's legacy
    fallback.  It is a migration guard, not runtime discovery: a newly added
    manager must be registered or explicitly reviewed as legacy-only instead
    of quietly becoming editor-visible through a filename convention.
    """
    if registry is None:
        from imswitch.imcontrol.model.plugins.registry import build_default_registry

        # Installed third-party plugins are irrelevant to coverage of the core
        # source tree and could make a local audit environment-dependent.
        registry = build_default_registry(discover=False)

    coverage: list[CoreManagerCoverage] = []
    for manager in _scan_legacy_managers(managers_root, set()):
        metadata = KIND_METADATA[manager.kind]
        if registry.resolve(manager.kind, manager.manager_name) is not None:
            status = "registered"
        elif metadata.supports_external_plugins:
            status = "unregistered"
        else:
            status = "legacy_only"
        coverage.append(CoreManagerCoverage(manager=manager, status=status))
    return sorted(coverage, key=lambda item: (item.manager.kind, item.manager.manager_name))


def _scan_legacy_managers(
    managers_root: Optional[Path],
    seen_names: set[str],
) -> list[ManagerInfo]:
    """Scan filesystem for managers not already in the registry.
    
    Args:
        managers_root: Root directory for scanning. If None, resolves from package.
        seen_names: Set of manager names already cataloged from registry.
    
    Returns:
        List of ManagerInfo instances for discovered managers.
    """
    logger = logging.getLogger(__name__)
    
    # Resolve managers root
    if managers_root is None:
        try:
            import imswitch.imcontrol.model.managers as managers_pkg
            managers_root = Path(managers_pkg.__file__).parent
        except (ImportError, AttributeError):
            logger.warning("Could not resolve managers package for legacy scan")
            return []
    
    if not managers_root.is_dir():
        logger.warning(f"Managers root does not exist: {managers_root}")
        return []
    
    # Base manager class names to skip. ``RS232Manager`` is deliberately not
    # here: it is the base of the RS232 managers *and* the manager shipped
    # setups select by that name, so it must be offered.
    base_managers = {
        "DetectorManager",
        "LaserManager",
        "PositionerManager",
        "RotatorManager",
        "FlipMirrorManager",
        "PulseGeneratorManager",
        "StandManager",
        "SLMManager",
    }
    
    infos: list[ManagerInfo] = []
    
    # Scan each category directory
    for category, dir_name in CATEGORY_TO_DIR.items():
        cat_dir = managers_root / dir_name
        if not cat_dir.is_dir():
            continue
        
        for f in cat_dir.glob("*Manager.py"):
            stem = f.stem
            
            # Skip base classes, private modules, and already-seen managers
            if stem in base_managers or stem.startswith("_") or stem in seen_names:
                continue
            
            # Resolve kind from category
            kind = CATEGORY_TO_KIND.get(category, "unknown")
            
            info = ManagerInfo(
                manager_name=stem,
                category=category,
                kind=kind,
                display_name=stem,  # No fancy display name for legacy
                aliases=(),
                plugin_name=None,
                source_package=None,
                docs_url=None,
                supported_platforms=(),
                setup_templates=(),
                is_builtin=False,
                from_registry=False,
            )
            infos.append(info)
            logger.debug(f"Legacy scan found {category} manager: {stem}")
    
    return infos


def _catalog_from_legacy_scan(managers_root: Optional[Path]) -> ManagerCatalog:
    """Build catalog entirely from legacy filesystem scan (registry unavailable)."""
    infos = _scan_legacy_managers(managers_root, set())
    return ManagerCatalog(infos)
