from typing import Any, Dict

from imswitch.imcommon.controller import MainController
from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.processing_config import (
    load_processing_config,
    plugin_ids_from_config,
)
from .CommunicationChannel import CommunicationChannel
from .ImProcessMainViewController import ImProcessMainViewController
from .basecontrollers import ImProcessWidgetControllerFactory


# Registry key for ImProcess's dock layout state. Distinct from imcontrol's
# 'GuiLayout' so both modules can persist their layouts side by side when
# enabled together.
_GUI_LAYOUT_STATE_KEY = 'ImProcessGuiLayout'


class ImProcessMainController(MainController):
    def __init__(self, mainView, moduleCommChannel, processingConfig=None):
        self.__mainView = mainView
        self.__moduleCommChannel = moduleCommChannel
        self.__logger = initLogger(self, tryInheritParent=False)
        self.__processingConfig = processingConfig

        # Connect view signals
        self.__mainView.sigClosing.connect(self.closeEvent)

        # Initialize plugin registry
        self._initialize_plugins()

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel()

        # List of Controllers for the GUI Widgets
        self.__factory = ImProcessWidgetControllerFactory(
            self.__commChannel, self.__moduleCommChannel
        )

        self.mainViewController = self.__factory.createController(
            ImProcessMainViewController, self.__mainView
        )

        # Register the view's dock layout with the shared widget-state
        # persistence service so it is auto-restored at startup and auto-saved
        # at shutdown. Failures here must never block ImProcess from coming up.
        self.__guiLayoutStateAdapter = None
        try:
            from imswitch.imcontrol.model import getWidgetStatePersistence

            self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(self.__mainView)
            persistence = getWidgetStatePersistence()
            persistence.register(_GUI_LAYOUT_STATE_KEY, self.__guiLayoutStateAdapter)
            try:
                persistence.loadWidgetState(_GUI_LAYOUT_STATE_KEY, 'default')
            except Exception as e:
                self.__logger.warning(
                    f'Failed to restore ImProcess dock layout: {e}'
                )
        except Exception as e:
            self.__logger.debug(
                f'Widget-state persistence unavailable for ImProcess layout: {e}'
            )
    
    def _initialize_plugins(self):
        """
        Initialize the plugin registry based on config or standalone defaults.
        
        Order of precedence:
        1. If setup.json has a "processing" block, load specified plugins
        2. Otherwise, load standalone defaults (view-only + universal processors)
        """
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.reconstructors import register_default_reconstructors
        from imswitch.improcess.processors import register_default_processors
        
        registry = get_registry()
        registry.clear()
        
        # Check if we have a setup configuration.
        #
        # The `processing` block is an ImProcess-specific addition. It is not a
        # typed field on SetupInfo, so it lands in `_catchAll` (because the
        # dataclass uses `@dataclass_json(undefined=Undefined.INCLUDE)`).
        processing_config = self.__processingConfig
        if processing_config is None:
            processing_config = load_processing_config(self.__logger)
        
        reconstructor_ids, processor_ids, has_plugin_config = plugin_ids_from_config(
            processing_config
        )
        if has_plugin_config:
            # Config-driven mode
            self.__logger.info(
                f"Loading plugins from config: reconstructors={reconstructor_ids}, "
                f"processors={processor_ids}"
            )
        else:
            # Standalone defaults
            self.__logger.info("Using standalone defaults: view-only + drift-correct")
        
        # Register only the plugins requested by config / standalone defaults.
        register_default_reconstructors(registry, reconstructor_ids)
        register_default_processors(registry, processor_ids)
        
        self.__logger.info(
            f"Plugin registry initialized: "
            f"{len(registry.reconstructors())} reconstructors, "
            f"{len(registry.processors())} processors"
        )

    def closeEvent(self):
        # Persist the current dock layout before tearing the controllers down,
        # so the next launch can restore it.
        if self.__guiLayoutStateAdapter is not None:
            try:
                from imswitch.imcontrol.model import getWidgetStatePersistence

                getWidgetStatePersistence().saveWidgetState(
                    _GUI_LAYOUT_STATE_KEY, 'default'
                )
            except Exception as e:
                self.__logger.warning(
                    f'Failed to save ImProcess dock layout: {e}'
                )
        self.__factory.closeAllCreatedControllers()


class _GuiLayoutStateAdapter:
    """Persistence adapter for ImProcess's passive dock layout state."""

    def __init__(self, view: Any) -> None:
        self._view = view

    def getWidgetState(self) -> Dict[str, Any]:
        """Return the current GUI layout state."""
        return self._view.getLayoutState()

    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """Restore GUI layout state without triggering hardware actions."""
        self._view.setLayoutState(state)

    def getStateSchemaVersion(self) -> int:
        """Return the GUI layout persistence schema version."""
        return 1


# Copyright (C) 2020-2021 ImSwitch developers
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
