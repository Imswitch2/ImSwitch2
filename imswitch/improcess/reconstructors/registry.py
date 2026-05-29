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
    
    def reconstructors(self) -> list['Reconstructor']:
        """Return all registered reconstructors."""
        return list(self._reconstructors.values())
    
    def processors(self) -> list['Processor']:
        """Return all registered processors."""
        return list(self._processors.values())
    
    def get_reconstructor(self, plugin_id: str) -> 'Reconstructor | None':
        """Retrieve a reconstructor by ID, or None if not found."""
        return self._reconstructors.get(plugin_id)
    
    def get_processor(self, plugin_id: str) -> 'Processor | None':
        """Retrieve a processor by ID, or None if not found."""
        return self._processors.get(plugin_id)
    
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
        # Heuristic 1: modality tag in attrs
        modality_tag = data_obj.attrs.get("modality", "").lower()
        if modality_tag and modality_tag in self._reconstructors:
            self.__logger.debug(f"Auto-selected reconstructor '{modality_tag}' from modality tag")
            return self._reconstructors[modality_tag]
        
        # Heuristic 2: file extension match
        file_ext = data_obj.filePath.suffix.lstrip(".").lower() if data_obj.filePath else ""
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
