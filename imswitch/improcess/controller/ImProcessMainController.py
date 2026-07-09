from collections.abc import Mapping
from typing import Any, Dict

import numpy as np

from imswitch.imcommon.controller import MainController
from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.processing_config import (
    load_processing_config,
    plugin_ids_from_config,
)
from .CommunicationChannel import CommunicationChannel
from .ImageToolbarController import ImageToolbarController
from .ImProcessMainViewController import ImProcessMainViewController
from .ResultProcessorController import ResultProcessorController
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
        self._register_startup_runtime_processors()
        self.__mainView.createStartupRuntimeAnalysisWidgets()
        self._refresh_runtime_processor_choices()

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel()

        # Bridge live results to imcontrol if enabled
        self.__commChannel.sigResultProduced.connect(self._onResultProduced)
        self.__commChannel.sigLiveResultUpdated.connect(self._onLiveResultUpdated)

        # List of Controllers for the GUI Widgets
        self.__factory = ImProcessWidgetControllerFactory(
            self.__commChannel, self.__moduleCommChannel
        )

        self.mainViewController = self.__factory.createController(
            ImProcessMainViewController, self.__mainView
        )
        self.imageToolbarController = ImageToolbarController(
            self.__commChannel,
            self.__mainView,
            self.mainViewController.reconstructionController,
        )
        self._resultProcessorControllers = {}
        # Runtime panels that publish their own results (multi-action producers
        # like Multicolor) expose sigResultProduced; we forward it to the comm
        # channel once. Tracks which panels have had that bridge connected.
        self._panelResultBridges = set()

        # Register the view's dock layout with the shared widget-state
        # persistence service so it is auto-restored at startup and auto-saved
        # at shutdown. Failures here must never block ImProcess from coming up.
        self.__guiLayoutStateAdapter = None
        try:
            from imswitch.imcommon.model import getWidgetStatePersistence

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

        # Wire producing panels unconditionally: on a fresh profile there is
        # no saved layout, so the persistence adapter hook above never fires,
        # and startup-constructed panels (projection, segmentation, PSF,
        # colocalization) would otherwise never get their
        # ResultProcessorController. Wiring is idempotent, so a later layout
        # restore re-running it is harmless.
        self._wire_runtime_result_processors()

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

    def _register_startup_runtime_processors(self) -> None:
        """Register processors required by config-enabled runtime panels."""
        from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import register_processor_by_id

        registry = get_registry()
        specs = runtime_analysis_tool_specs()
        for tool_id in self.__mainView.startupRuntimeAnalysisToolIds():
            spec = specs.get(tool_id)
            if spec is None or spec.processor_id is None:
                continue
            if (
                registry.get_processor(spec.processor_id, raise_on_missing=False)
                is not None
            ):
                continue
            try:
                plugin = register_processor_by_id(registry, spec.processor_id)
            except Exception:
                self.__logger.exception(
                    f"Failed to register startup analysis processor "
                    f"{spec.processor_id!r} for panel {tool_id!r}"
                )
                continue
            self.__logger.info(
                f"Registered startup analysis processor: "
                f"{plugin.id} ({plugin.name})"
            )

    def _refresh_runtime_processor_choices(self):
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.model.runtime_tools import (
            runtime_analysis_tool_specs,
        )

        registry = get_registry()
        loaded_processors = registry.processors()
        loaded = {processor.id for processor in loaded_processors}
        tool_specs = runtime_analysis_tool_specs()
        choices = []
        for tool_id, spec in sorted(
            tool_specs.items(),
            key=lambda item: (item[1].category, item[1].title, item[0]),
        ):
            processor_loaded = (
                spec.processor_id is None
                or spec.processor_id in loaded
            )
            widget_loaded = self.__mainView.isRuntimeAnalysisToolLoaded(tool_id)
            if processor_loaded and widget_loaded:
                continue
            choices.append((tool_id, _runtime_tool_display_title(spec)))
        self.__mainView.setAvailableRuntimeProcessors(choices)
        self.__mainView.setLoadedRuntimeProcessors(
            [
                (
                    processor.id,
                    f"{getattr(processor, 'category', 'Other')}: {processor.name}",
                )
                for processor in sorted(
                    loaded_processors,
                    key=lambda item: (
                        getattr(item, 'category', 'Other'),
                        item.name,
                        item.id,
                    ),
                )
            ]
        )

    def _restore_runtime_processor(self, processor_id: str) -> None:
        """Re-register a processor that was runtime-loaded last session.

        Called from the persistence adapter before the view recreates the
        matching widget dock. Unknown ids and processor-less tools are skipped
        silently — the widget restore handles
        them on the view side.
        """
        from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import register_processor_by_id

        spec = runtime_analysis_tool_specs().get(processor_id)
        if spec is None or spec.processor_id is None:
            return
        registry = get_registry()
        if (
            registry.get_processor(spec.processor_id, raise_on_missing=False)
            is not None
        ):
            return
        try:
            plugin = register_processor_by_id(registry, spec.processor_id)
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
        from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import register_processor_by_id

        spec = runtime_analysis_tool_specs().get(processor_id)
        if spec is None:
            self.__logger.warning(f"Unknown runtime analysis tool: {processor_id}")
            self._refresh_runtime_processor_choices()
            return
        registry = get_registry()
        is_processor = spec.processor_id is not None
        plugin = (
            registry.get_processor(spec.processor_id, raise_on_missing=False)
            if is_processor
            else None
        )
        if plugin is not None:
            self.__logger.info(f"Processor already loaded: {spec.processor_id}")
        elif is_processor:
            # Wrap register_processor_by_id: a processor whose import or
            # __init__ raises must not crash the load handler — log the
            # traceback so the user can see *why* nothing showed up.
            try:
                plugin = register_processor_by_id(registry, spec.processor_id)
                self.__logger.info(
                    f"Runtime-loaded processor: {plugin.id} ({plugin.name})"
                )
            except Exception:
                self.__logger.exception(
                    f"Failed to register processor {spec.processor_id!r}"
                )
                self._refresh_runtime_processor_choices()
                return
        dock_title = self.__mainView.ensureRuntimeAnalysisWidget(processor_id)
        if dock_title is not None:
            self.__logger.info(f"Runtime-opened analysis tool: {dock_title}")
            self._wire_runtime_result_processor(processor_id)
        self._refresh_runtime_processor_choices()

    def _wire_runtime_result_processor(self, processor_id: str) -> None:
        widget = self.__mainView.getRuntimeAnalysisWidget(processor_id)
        if widget is None:
            return
        # Single-processor panels (generic ResultProcessorWidget, and custom
        # panels like Segmentation that conform to the contract) run one
        # processor and publish through ResultProcessorController.
        if hasattr(widget, "sigRunRequested"):
            if processor_id not in self._resultProcessorControllers:
                self._resultProcessorControllers[processor_id] = self.__factory.createController(
                    ResultProcessorController,
                    widget,
                )
            self._seed_runtime_result_processor(widget)
            return
        # Multi-action producing panels (e.g. Multicolor: register + apply)
        # publish their own results via sigResultProduced; forward those to the
        # reconstruction list and feed them the current result.
        if hasattr(widget, "sigResultProduced"):
            self._wire_producing_panel(widget)

    def _wire_producing_panel(self, widget) -> None:
        """Bridge a panel that publishes its own results into the pipeline.

        Keyed by widget identity, not tool id: several tool ids can map to one
        panel (multicolor-registration and multicolor-apply share the
        Multicolor widget), and a per-id key would double-connect the forward,
        publishing every result twice.
        """
        comm_channel = self.__commChannel
        if id(widget) not in self._panelResultBridges:
            def _forward(result, name):
                comm_channel.sigResultProduced.emit(result, name)
                comm_channel.sigCurrentResultChanged.emit(result)

            widget.sigResultProduced.connect(_forward)
            if hasattr(widget, "setCurrentResult"):
                comm_channel.sigCurrentResultChanged.connect(widget.setCurrentResult)
            self._panelResultBridges.add(id(widget))
        self._seed_runtime_result_processor(widget)

    def _seed_runtime_result_processor(self, widget) -> None:
        """Populate a newly opened processor dock with the current result.

        Runtime result-processor widgets are often opened after a
        reconstruction has already been selected. Those widgets only receive
        future sigCurrentResultChanged events, so seed them explicitly with the
        active reconstruction result at wire time.
        """
        setter = getattr(widget, "setCurrentResult", None)
        if not callable(setter):
            return
        try:
            result = self.mainViewController.reconstructionController.getActiveResult()
        except Exception:
            return
        try:
            setter(result)
        except Exception:
            self.__logger.debug(
                "Could not seed runtime processor widget with current result",
                exc_info=True,
            )

    def _wire_runtime_result_processors(self) -> None:
        from imswitch.improcess.model.runtime_tools import runtime_analysis_tool_specs

        for tool_id in runtime_analysis_tool_specs().keys():
            self._wire_runtime_result_processor(tool_id)

    def _onResultProduced(self, result, name):
        """Bridge processing result to imcontrol if display is enabled."""
        self._bridgeResultToImcontrol(result, name)

    def _onLiveResultUpdated(self, result):
        """Bridge live result update to imcontrol if display is enabled."""
        self._bridgeResultToImcontrol(result, result.name if hasattr(result, 'name') else 'Live Result')

    def _bridgeResultToImcontrol(self, result, name):
        """Extract displayable image and emit to imcontrol viewer if configured."""
        if not self._processingConfigValue('live_display_in_imcontrol', False):
            return

        if not self.__moduleCommChannel.isModuleRegistered('imcontrol'):
            return

        if not hasattr(result, 'data'):
            return

        image = self._displayImageForImcontrol(result)
        if image is None:
            return

        scale = None
        if hasattr(result, 'axis_scales') and result.axis_scales:
            scale = result.axis_scales[-2:] if len(result.axis_scales) >= 2 else None

        self.__moduleCommChannel.sigLiveReconResult.emit(name, image, scale)

    def _processingConfigValue(self, key: str, default: Any = None) -> Any:
        """Read an ImProcess config value from dict-like or setup-like config."""
        config = self.__processingConfig
        if config is None:
            return default

        if isinstance(config, Mapping):
            return config.get(key, default)

        if hasattr(config, key):
            return getattr(config, key)

        catch_all = getattr(config, "_catchAll", None)
        if isinstance(catch_all, Mapping):
            processing = catch_all.get("processing", {}) or {}
            if isinstance(processing, Mapping):
                return processing.get(key, default)

        return default

    @staticmethod
    def _displayImageForImcontrol(result):
        """Return a 2D image slice suitable for ImageWidget.addStaticLayer."""
        data = np.asarray(result.data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data

        axis_labels = list(getattr(result, 'axis_labels', []) or [])
        indices = []
        for axis in range(data.ndim - 2):
            label = str(axis_labels[axis]).lower() if axis < len(axis_labels) else ""
            if label in {"t", "time", "timepoint", "timepoints"} or "time" in label:
                indices.append(max(0, data.shape[axis] - 1))
            else:
                indices.append(data.shape[axis] // 2)
        return data[tuple(indices)]

    def closeEvent(self):
        # Persist the current dock layout before tearing the controllers down,
        # so the next launch can restore it.
        if self.__guiLayoutStateAdapter is not None:
            try:
                from imswitch.imcommon.model import getWidgetStatePersistence

                getWidgetStatePersistence().saveWidgetState(
                    _GUI_LAYOUT_STATE_KEY, 'default'
                )
            except Exception as e:
                self.__logger.warning(
                    f'Failed to save ImProcess dock layout: {e}'
                )
        self.__factory.closeAllCreatedControllers()


def _runtime_tool_display_title(spec) -> str:
    category = str(getattr(spec, "category", "") or "").strip()
    title = str(getattr(spec, "title", "") or getattr(spec, "id", ""))
    return f"{category}: {title}" if category else title


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
        if controller is not None:
            try:
                controller._wire_runtime_result_processors()
            except Exception:
                pass

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
