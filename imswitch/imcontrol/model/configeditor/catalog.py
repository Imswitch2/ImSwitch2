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


# Authoritative mapping from registry kind to editor category
KIND_TO_CATEGORY = {
    "detector": "detectors",
    "laser": "lasers",
    "positioner": "positioners",
    "rotator": "rotators",
    "rs232": "rs232devices",
    "slm": "slms",
    "flip_mirror": "flipMirrors",
    "stand": "stands",
    "pulse_generator": "pulsegen",
}

# Reverse mapping for category to kind
CATEGORY_TO_KIND = {v: k for k, v in KIND_TO_CATEGORY.items()}

# Editor category to manager directory name
CATEGORY_TO_DIR = {
    "detectors": "detectors",
    "lasers": "lasers",
    "positioners": "positioners",
    "rotators": "rotators",
    "rs232devices": "rs232",
    "slms": "slms",
    "flipMirrors": "flipMirrors",
    "pulsegen": "pulsegen",
    "stands": "stands",
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
) -> ManagerCatalog:
    """Build a manager catalog from the registry and optional legacy scan.
    
    Args:
        registry: A DevicePluginRegistry instance. If None, builds the default
            registry with discovery enabled.
        include_legacy_scan: If True, also scan the filesystem for managers not
            in the registry (preserves legacy behavior).
        managers_root: Root directory for manager scanning. If None, resolves
            from the package structure.
    
    Returns:
        A populated ManagerCatalog instance.
    """
    logger = logging.getLogger(__name__)
    
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
        category = KIND_TO_CATEGORY.get(contrib.kind)
        if category is None:
            # Skip unknown kinds gracefully
            logger.debug(
                f"Skipping contribution {contrib.id} with unmapped kind '{contrib.kind}'"
            )
            continue
        
        # Determine if builtin (from imswitch-core plugin)
        is_builtin = contrib.plugin_name == "imswitch-core"
        
        # Resolve schema if available
        schema = None
        try:
            from imswitch.imcontrol.model.plugins.validation import resolve_schema
            schema = resolve_schema(contrib)
        except (ImportError, Exception) as e:
            logger.debug(f"Could not resolve schema for {contrib.id}: {e}")
        
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
        infos.extend(legacy_infos)
        if legacy_infos:
            logger.info(
                f"Added {len(legacy_infos)} manager(s) from legacy filesystem scan"
            )
    
    return ManagerCatalog(infos)


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
    
    # Base manager class names to skip
    base_managers = {
        "DetectorManager",
        "LaserManager",
        "PositionerManager",
        "RotatorManager",
        "FlipMirrorManager",
        "PulseGeneratorManager",
        "StandManager",
        "RS232Manager",
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
