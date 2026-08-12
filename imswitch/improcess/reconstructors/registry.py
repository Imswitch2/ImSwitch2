"""Plugin registry for ImProcess reconstructors and processors."""

from typing import TYPE_CHECKING

from imswitch.imcommon.model import initLogger

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj
    from imswitch.improcess.reconstructors.base import Reconstructor
    from imswitch.improcess.processors.base import Processor


class PluginRegistry:
    """
    Central registry for reconstructor and processor plugins.
    
    Populated at startup based on config (or standalone defaults). Controllers
    retrieve plugins from the registry, never import them directly.
    """
    
    def __init__(self):
        self.__logger = initLogger(self)
        self._reconstructors: dict[str, 'Reconstructor'] = {}
        self._processors: dict[str, 'Processor'] = {}
    
    def register_reconstructor(self, plugin: 'Reconstructor') -> None:
        """Register a reconstructor plugin instance."""
        if plugin.id in self._reconstructors:
            self.__logger.warning(f"Reconstructor '{plugin.id}' already registered, replacing")
        self._reconstructors[plugin.id] = plugin
        self.__logger.debug(f"Registered reconstructor: {plugin.id} ({plugin.name})")
    
    def register_processor(self, plugin: 'Processor') -> None:
        """Register a processor plugin instance."""
        if plugin.id in self._processors:
            self.__logger.warning(f"Processor '{plugin.id}' already registered, replacing")
        self._processors[plugin.id] = plugin
        self.__logger.debug(f"Registered processor: {plugin.id} ({plugin.name})")

    def clear(self) -> None:
        """Remove all registered plugins before applying a new setup config."""
        self._reconstructors.clear()
        self._processors.clear()
    
    def reconstructors(self) -> list['Reconstructor']:
        """Return all registered reconstructors."""
        return list(self._reconstructors.values())
    
    def processors(self) -> list['Processor']:
        """Return all registered processors."""
        return list(self._processors.values())
    
    def get_reconstructor(self, plugin_id: str, raise_on_missing: bool = True) -> 'Reconstructor | None':
        """
        Retrieve a reconstructor by ID.
        
        Args:
            plugin_id: The reconstructor plugin ID to look up
            raise_on_missing: If True (default), raise KeyError when ID not found.
                            If False, return None (legacy behavior).
        
        Returns:
            The reconstructor instance, or None if not found and raise_on_missing=False
        
        Raises:
            KeyError: If plugin_id is not registered and raise_on_missing=True
        """
        plugin = self._reconstructors.get(plugin_id)
        if plugin is None and raise_on_missing:
            available = sorted(self._reconstructors.keys())
            raise KeyError(
                f"Reconstructor plugin '{plugin_id}' is not registered. "
                f"Available reconstructors: {available}"
            )
        return plugin
    
    def get_processor(self, plugin_id: str, raise_on_missing: bool = True) -> 'Processor | None':
        """
        Retrieve a processor by ID.
        
        Args:
            plugin_id: The processor plugin ID to look up
            raise_on_missing: If True (default), raise KeyError when ID not found.
                            If False, return None (legacy behavior).
        
        Returns:
            The processor instance, or None if not found and raise_on_missing=False
        
        Raises:
            KeyError: If plugin_id is not registered and raise_on_missing=True
        """
        plugin = self._processors.get(plugin_id)
        if plugin is None and raise_on_missing:
            available = sorted(self._processors.keys())
            raise KeyError(
                f"Processor plugin '{plugin_id}' is not registered. "
                f"Available processors: {available}"
            )
        return plugin
    
    def auto_select_reconstructor(self, data_obj: 'DataObj') -> 'Reconstructor':
        """
        Pick a default reconstructor based on data_obj metadata.
        
        Heuristics:
        1. Check data_obj.attrs for a "modality" tag (e.g., "monalisa", "sted")
        2. Check file extension against reconstructor.file_extensions
        3. Fall back to "view-only" if available, else first registered reconstructor
        
        Args:
            data_obj: The data object to select a reconstructor for
        
        Returns:
            The best-match reconstructor
        """
        # Heuristic 1: the modality the recording declared. The acquisition
        # layout is the contract that carries it now; the bare "modality" attr
        # remains for sources written before that. DataObj.attrs is None for
        # plain TIFFs and may be missing on synthetic objects, so guard both.
        modality_tag = ""
        try:
            resolved = getattr(data_obj, "acquisition_layout", None)
            recorded = getattr(getattr(resolved, "layout", None), "modality", None)
            modality_tag = str(recorded or "").lower()
        except Exception:
            # A source that cannot resolve its layout still gets the older
            # heuristics; picking a reconstructor must never be fatal.
            pass
        if not modality_tag:
            attrs = getattr(data_obj, "attrs", None) or {}
            try:
                modality_tag = str(attrs.get("modality", "")).lower()
            except AttributeError:
                # attrs is something we cannot .get() on (e.g. h5py
                # AttributeManager in edge cases). Fall through to extensions.
                pass
        if modality_tag and modality_tag in self._reconstructors:
            self.__logger.debug(f"Auto-selected reconstructor '{modality_tag}' from modality tag")
            return self._reconstructors[modality_tag]

        # Heuristic 2: file extension match. The DataObj attribute is dataPath,
        # not filePath, and it may be a str rather than a Path.
        from pathlib import Path
        data_path = getattr(data_obj, "dataPath", None)
        file_ext = Path(data_path).suffix.lstrip(".").lower() if data_path else ""
        for recon in self._reconstructors.values():
            if file_ext in recon.file_extensions:
                self.__logger.debug(
                    f"Auto-selected reconstructor '{recon.id}' from file extension '{file_ext}'"
                )
                return recon
        
        # Heuristic 3: fall back to view-only
        if "view-only" in self._reconstructors:
            self.__logger.debug("Auto-selected 'view-only' reconstructor (fallback)")
            return self._reconstructors["view-only"]
        
        # Last resort: first registered reconstructor
        if self._reconstructors:
            fallback = list(self._reconstructors.values())[0]
            self.__logger.warning(
                f"No suitable reconstructor found, falling back to '{fallback.id}'"
            )
            return fallback
        
        raise RuntimeError("No reconstructors registered in PluginRegistry")


# Global singleton instance (populated at module startup)
_registry = None


def get_registry() -> PluginRegistry:
    """Get the global plugin registry singleton."""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


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
