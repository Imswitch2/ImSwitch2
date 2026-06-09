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
        self.__mainView.sigLoadProcessorRequested.connect(self._load_runtime_processor)

        # Initialize plugin registry
        self._initialize_plugins()
        self._refresh_runtime_processor_choices()

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

            self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(
                self.__mainView, main_controller=self,
            )
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

    def _refresh_runtime_processor_choices(self):
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import (
            available_processor_choices,
            available_processor_ids,
        )

        registry = get_registry()
        loaded_processors = registry.processors()
        loaded = {processor.id for processor in loaded_processors}
        processor_ids = set(available_processor_ids())
        tool_choices = dict(available_processor_choices())
        tool_choices["roi-manager"] = "ROI manager"
        choices = []
        for tool_id, tool_name in sorted(tool_choices.items()):
            processor_loaded = tool_id not in processor_ids or tool_id in loaded
            widget_loaded = self.__mainView.isRuntimeAnalysisToolLoaded(tool_id)
            if processor_loaded and widget_loaded:
                continue
            choices.append((tool_id, tool_name))
        self.__mainView.setAvailableRuntimeProcessors(choices)
        self.__mainView.setLoadedRuntimeProcessors(
            [(processor.id, processor.name) for processor in loaded_processors]
        )

    def _restore_runtime_processor(self, processor_id: str) -> None:
        """Re-register a processor that was runtime-loaded last session.

        Called from the persistence adapter before the view recreates the
        matching widget dock. Unknown ids and processor-less tools (like
        ``roi-manager``) are silently skipped — the widget restore handles
        them on the view side.
        """
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import (
            available_processor_ids,
            register_processor_by_id,
        )

        if processor_id not in set(available_processor_ids()):
            return
        registry = get_registry()
        if registry.get_processor(processor_id) is not None:
            return
        try:
            plugin = register_processor_by_id(registry, processor_id)
        except Exception:
            self.__logger.exception(
                f"Failed to restore runtime processor {processor_id!r} from "
                f"persisted layout state"
            )
            return
        self.__logger.info(
            f"Restored runtime-loaded processor: {plugin.id} ({plugin.name})"
        )

    def _load_runtime_processor(self, processor_id: str):
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import (
            available_processor_ids,
            register_processor_by_id,
        )

        registry = get_registry()
        is_processor = processor_id in set(available_processor_ids())
        plugin = registry.get_processor(processor_id) if is_processor else None
        if plugin is not None:
            self.__logger.info(f"Processor already loaded: {processor_id}")
        elif is_processor:
            # Wrap register_processor_by_id: a processor whose import or
            # __init__ raises must not crash the load handler — log the
            # traceback so the user can see *why* nothing showed up.
            try:
                plugin = register_processor_by_id(registry, processor_id)
                self.__logger.info(
                    f"Runtime-loaded processor: {plugin.id} ({plugin.name})"
                )
            except Exception:
                self.__logger.exception(
                    f"Failed to register processor {processor_id!r}"
                )
                self._refresh_runtime_processor_choices()
                return
        dock_title = self.__mainView.ensureRuntimeAnalysisWidget(processor_id)
        if dock_title is not None:
            self.__logger.info(f"Runtime-opened analysis tool: {dock_title}")
        elif not is_processor:
            self.__logger.warning(f"Unknown runtime analysis tool: {processor_id}")
        self._refresh_runtime_processor_choices()

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

    def __init__(self, view: Any, main_controller: Any = None) -> None:
        self._view = view
        self._main_controller = main_controller

    def getWidgetState(self) -> Dict[str, Any]:
        """Return the current GUI layout state."""
        return self._view.getLayoutState()

    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """Restore GUI layout state without triggering hardware actions.

        Re-registers any processors that were runtime-loaded last session so
        their UI docks come back wired to a live registry entry. Then asks
        the view to recreate the matching widget docks (via setLayoutState
        → ensureRuntimeAnalysisWidget) before applying the DockArea state.
        """
        controller = self._main_controller
        if controller is not None and isinstance(state, dict):
            tool_ids = state.get('runtime_analysis_tool_ids', []) or []
            for tool_id in tool_ids:
                try:
                    controller._restore_runtime_processor(tool_id)
                except Exception:
                    # The hook is best-effort; the view-side widget restore
                    # below will still bring the dock back even if the
                    # processor cannot be re-registered.
                    pass
            controller._refresh_runtime_processor_choices()
        self._view.setLayoutState(state)

    def getStateSchemaVersion(self) -> int:
        """Return the GUI layout persistence schema version."""
        return 2


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
