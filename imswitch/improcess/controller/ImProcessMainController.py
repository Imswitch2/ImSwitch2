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
from .GraphController import GraphController
from .ImageToolbarController import ImageToolbarController
from .ImProcessMainViewController import ImProcessMainViewController
from .ResultProcessorController import ResultProcessorController
from .basecontrollers import ImProcessWidgetControllerFactory


# Registry key for ImProcess's dock layout state. Distinct from imcontrol's
# 'GuiLayout' so both modules can persist their layouts side by side when
# enabled together.
_GUI_LAYOUT_STATE_KEY = 'ImProcessGuiLayout'
_ROI_MANAGER_STATE_KEY = 'ImProcessROIManager'


class ImProcessMainController(MainController):
    def __init__(self, mainView, moduleCommChannel, processingConfig=None):
        self.__mainView = mainView
        self.__moduleCommChannel = moduleCommChannel
        self.__logger = initLogger(self, tryInheritParent=False)
        self.__processingConfig = processingConfig

        # Connect view signals
        self.__mainView.sigClosing.connect(self.closeEvent)
        self.__mainView.sigLoadProcessorRequested.connect(self._load_runtime_processor)
        if hasattr(self.__mainView, 'sigReloadPluginsRequested'):
            self.__mainView.sigReloadPluginsRequested.connect(self._reload_user_plugins)

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
        self.napariEndpointController = None
        try:
            from .NapariEndpointController import NapariEndpointController

            self.napariEndpointController = NapariEndpointController(
                self.__commChannel,
                self.__mainView,
                self.mainViewController.reconstructionController,
                processing_config=getattr(self, '_processingConfigLoaded', None),
            )
        except Exception:
            # Endpoints are an extra on top of the viewer; a broken npe2
            # environment must not keep ImProcess from starting.
            self.__logger.exception("Could not set up napari endpoints")
        self.workflowController = None
        try:
            from .WorkflowController import WorkflowController

            self.workflowController = WorkflowController(
                self.__commChannel,
                self.__mainView,
                self.mainViewController.reconstructionController,
                processing_config=getattr(self, '_processingConfigLoaded', None),
            )
            if self.napariEndpointController is not None:
                # An endpoint session's layers read a result's (lazy) data;
                # the workflow controller must not close a source under them.
                self.workflowController.addHolder(
                    self.napariEndpointController.heldResultUids,
                    changed=self.napariEndpointController.sigSessionsChanged,
                )
        except Exception:
            self.__logger.exception("Could not set up workflow export/run")
        self._resultProcessorControllers = {}
        # Runtime panels that publish their own results (multi-action producers
        # like Multicolor) expose sigResultProduced; we forward it to the comm
        # channel once. Tracks which panels have had that bridge connected.
        self._panelResultBridges = set()
        # Measurement panels re-measuring on every result change; tracked by
        # widget identity so reopening a dock cannot double-connect.
        self._resultFollowers = set()
        # ROI manager panels already given their shortcuts and autosave hook.
        self._roiManagerPanels = set()

        # Configurable keyboard shortcuts (shared imcommon ShortcutManager,
        # Fiji-parity defaults, per-user JSON overrides). Never let shortcut
        # wiring block ImProcess startup.
        self._shortcutManager = None
        try:
            self._setup_shortcuts()
        except Exception:
            self.__logger.exception("Could not set up keyboard shortcuts")

        # Register the view's dock layout with the shared widget-state
        # persistence service so it is auto-restored at startup and auto-saved
        # at shutdown. Failures here must never block ImProcess from coming up.
        self.__guiLayoutStateAdapter = None
        # Assigned before the try so `_wire_runtime_result_processor` can look
        # for it whether or not persistence is available at all.
        self.__roiManagerStateAdapter = None
        try:
            from imswitch.imcommon.model import getWidgetStatePersistence

            self.__guiLayoutStateAdapter = _GuiLayoutStateAdapter(
                self.__mainView, main_controller=self,
            )
            persistence = getWidgetStatePersistence()
            persistence.register(_GUI_LAYOUT_STATE_KEY, self.__guiLayoutStateAdapter)
            # Owned here, not by the panel: the ROI manager is runtime-loaded
            # and is usually absent when startup state is restored, so a
            # widget-registered adapter would silently drop the saved sets
            # (A-09).
            self.__roiManagerStateAdapter = _ROIManagerStateAdapter(
                self.__mainView, logger=self.__logger,
            )
            persistence.register(
                _ROI_MANAGER_STATE_KEY, self.__roiManagerStateAdapter
            )
            try:
                persistence.loadWidgetState(_GUI_LAYOUT_STATE_KEY, 'default')
            except Exception as e:
                self.__logger.warning(
                    f'Failed to restore ImProcess dock layout: {e}'
                )
            try:
                persistence.loadWidgetState(_ROI_MANAGER_STATE_KEY, 'default')
            except Exception as e:
                self.__logger.warning(f'Failed to restore ImProcess ROI sets: {e}')
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
        from imswitch.improcess.processors import (
            load_user_plugins,
            register_default_processors,
        )

        registry = get_registry()
        registry.clear()

        # Discover user drop-in analysis plugins first, so they are available to
        # register_default_processors and every enumeration below. Tolerant: a
        # broken plugin is logged and skipped, never blocking startup.
        loaded_plugins, plugin_errors = load_user_plugins()
        if loaded_plugins:
            self.__logger.info(
                f"Discovered {len(loaded_plugins)} user analysis plugin(s): "
                f"{loaded_plugins}"
            )
        for error in plugin_errors:
            self.__logger.warning(
                f"Skipped analysis plugin {error.path}: {error.message.splitlines()[-1]}"
            )

        # Check if we have a setup configuration.
        #
        # The `processing` block is an ImProcess-specific addition. It is not a
        # typed field on SetupInfo, so it lands in `_catchAll` (because the
        # dataclass uses `@dataclass_json(undefined=Undefined.INCLUDE)`).
        processing_config = self.__processingConfig
        if processing_config is None:
            processing_config = load_processing_config(self.__logger)
        # Kept for controllers built later that read their own keys from the
        # same block (napari endpoints).
        self._processingConfigLoaded = dict(processing_config or {})

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

    def _setup_shortcuts(self) -> None:
        """Wire configurable keyboard shortcuts (same machinery as imcontrol)."""
        from imswitch.imcommon.controller.ShortcutManager import ShortcutManager
        from imswitch.improcess.controller.shortcuts import (
            load_shortcut_overrides,
            register_improcess_shortcuts,
        )

        if not hasattr(self.__mainView, 'shortcutsMenu'):
            return
        self._shortcutManager = ShortcutManager()
        register_improcess_shortcuts(self._shortcutManager, self.__mainView)
        self._shortcutManager.loadConfigOverrides(load_shortcut_overrides())
        self._shortcutManager.computeEffectiveBindings()
        self._shortcutManager.build(self.__mainView.shortcutsMenu, self.__mainView)
        self.__mainView.updateActionShortcutDisplays(
            self._shortcutManager.getEffectiveBindings()
        )
        self.__mainView.sigConfigureShortcuts.connect(self._openShortcutEditor)
        # Only the visible module tab's shortcut set may be live: all tabs
        # share one top-level window, so a hidden module's Window-scoped
        # bindings would otherwise stay active and collide with ours.
        if hasattr(self.__mainView, 'sigModuleVisibilityChanged'):
            self.__mainView.sigModuleVisibilityChanged.connect(
                self._shortcutManager.setBindingsEnabled
            )
            self._shortcutManager.setBindingsEnabled(self.__mainView.isVisible())

    def _openShortcutEditor(self) -> None:
        from imswitch.imcommon.view.ShortcutEditorDialog import ShortcutEditorDialog
        from imswitch.improcess.controller.shortcuts import save_shortcut_overrides

        if self._shortcutManager is None:
            return
        dialog = ShortcutEditorDialog(
            self.__mainView,
            self._shortcutManager,
            persistCallback=save_shortcut_overrides,
        )
        dialog.exec_()
        self.__mainView.updateActionShortcutDisplays(
            self._shortcutManager.getEffectiveBindings()
        )

    def _refresh_runtime_processor_choices(self):
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.model.runtime_tools import (
            runtime_analysis_tool_specs,
        )

        registry = get_registry()
        loaded_processors = registry.processors()
        loaded = {processor.id for processor in loaded_processors}

        def loadable_choices(tool_specs):
            choices = []
            seen_widgets = set()
            for tool_id, spec in sorted(
                tool_specs.items(),
                key=lambda item: (item[1].category, item[1].title, item[0]),
            ):
                # Collapse tools that share one widget into a single entry: the
                # multicolor registration + apply processors both open the
                # Multicolor panel, so without this they show up twice. First
                # (sorted) id wins.
                if spec.attribute in seen_widgets:
                    continue
                seen_widgets.add(spec.attribute)
                processor_loaded = (
                    spec.processor_id is None
                    or spec.processor_id in loaded
                )
                widget_loaded = self.__mainView.isRuntimeAnalysisToolLoaded(tool_id)
                if processor_loaded and widget_loaded:
                    continue
                choices.append((tool_id, _runtime_tool_display_title(spec)))
            return choices

        # Origin-split combos: built-in tools stay in the Tools toolbar, drop-in
        # plugins get the Plugins toolbar. This method is the single refresh
        # path for both (startup wiring and the plugin-reload handler call it).
        self.__mainView.setAvailableRuntimeProcessors(
            loadable_choices(runtime_analysis_tool_specs(origin="builtin"))
        )
        if hasattr(self.__mainView, 'setAvailablePluginTools'):
            user_specs = runtime_analysis_tool_specs(origin="user")
            self.__mainView.setAvailablePluginTools(
                loadable_choices(user_specs),
                placeholder=(
                    'All plugins loaded' if user_specs else 'No plugins installed'
                ),
            )
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

    def _reload_user_plugins(self) -> None:
        """Re-scan the drop-in plugins folder and refresh the tool list.

        Newly added plugins appear in the 'Load tool' combo; removed ones drop
        out. Re-registering with fresh instances means an edited plugin's new
        code is used the next time its panel is opened (an already-open panel
        keeps the instance it was built with until closed and reopened).
        """
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.processors import (
            load_user_plugins,
            register_processor_by_id,
        )

        loaded, errors = load_user_plugins()
        self.__logger.info(f"Reloaded drop-in analysis plugins: {loaded}")
        for error in errors:
            self.__logger.warning(
                f"Skipped analysis plugin {error.path}: "
                f"{error.message.splitlines()[-1]}"
            )

        registry = get_registry()
        for processor_id in loaded:
            try:
                register_processor_by_id(registry, processor_id)
            except Exception:
                self.__logger.exception(
                    f"Failed to (re)register plugin processor {processor_id!r}"
                )

        self._refresh_runtime_processor_choices()

    def _wire_runtime_result_processor(self, processor_id: str) -> None:
        widget = self.__mainView.getRuntimeAnalysisWidget(processor_id)
        if widget is None:
            return
        if processor_id == "roi-manager":
            # The panel exists now; hand it the state that was restored before
            # it did (A-09). Idempotent — the stash is cleared once applied.
            adapter = getattr(self, '_ImProcessMainController__roiManagerStateAdapter', None)
            if adapter is not None:
                adapter.applyStashTo(widget)
            self._wire_roi_manager_panel(widget)
        if processor_id == "graph":
            if self.mainViewController.graphController is None:
                self.mainViewController.graphController = self.__factory.createController(
                    GraphController,
                    widget,
                )
            # The panel is usually opened after a result is already selected,
            # and GraphController only renders on future selection changes —
            # seed it so the graph is populated the moment it appears.
            self._seed_graph_controller()
            return
        if processor_id == "metadata":
            if self.mainViewController.metadataController is None:
                from .MetadataController import MetadataController

                self.mainViewController.metadataController = (
                    self.__factory.createController(MetadataController, widget)
                )
            # Like Graph, the panel is usually opened after a file is already
            # loaded, and the controller only sees *future* data changes —
            # seed it so the metadata is there the moment the dock appears.
            self._seed_metadata_controller()
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
            return
        # Measurement panels (Profile, ROI stats, ROI manager) neither run a
        # processor nor publish results, so neither branch above reaches them
        # — and they read their pixels from the viewer, which switching
        # reconstruction silently changes underneath them.
        self._wire_result_follower(widget)

    def _wire_roi_manager_panel(self, widget) -> None:
        """Undo/redo shortcuts and crash-recovery autosave for the ROI panel.

        Both are wired when the panel is built, not at startup: it is
        runtime-loaded, so binding earlier would bind to nothing.
        """
        if id(widget) in self._roiManagerPanels:
            return
        self._roiManagerPanels.add(id(widget))

        if self._shortcutManager is not None:
            try:
                from .shortcuts import register_roi_manager_shortcuts

                register_roi_manager_shortcuts(
                    self._shortcutManager, widget, owner=self.__mainView
                )
            except Exception:
                self.__logger.debug(
                    "Could not register ROI manager shortcuts", exc_info=True
                )

        signal = getattr(widget, "sigStateChanged", None)
        if signal is not None:
            signal.connect(self._autosaveROIState)

    def _autosaveROIState(self) -> None:
        """Persist the ROI sets between shutdowns, so a crash costs seconds.

        Straight into the existing state store — the same place shutdown
        writes — rather than a recovery file of its own, so there is one
        payload and no question of which is newer (C-13/A-25).
        """
        try:
            from imswitch.imcommon.model import getWidgetStatePersistence

            getWidgetStatePersistence().saveWidgetState(
                _ROI_MANAGER_STATE_KEY, 'default'
            )
        except Exception:
            self.__logger.debug('Could not autosave ROI sets', exc_info=True)

    def _wire_result_follower(self, widget) -> None:
        """Have a panel recompute when the selected result changes.

        A measurement panel that keeps showing numbers from the previous
        reconstruction is worse than one showing none: nothing on screen says
        which result the values belong to.
        """
        setter = getattr(type(widget), "setCurrentResult", None)
        if not callable(setter) or id(widget) in self._resultFollowers:
            return
        self.__commChannel.sigCurrentResultChanged.connect(widget.setCurrentResult)
        # A follower that can list results must also hear about the *set*
        # changing, not only the selection (C-11): loading a reconstruction
        # while the panel is open would otherwise leave its picker showing the
        # results that existed when it was opened.
        available = getattr(type(widget), "setAvailableResults", None)
        if callable(available):
            self.__commChannel.sigResultsChanged.connect(
                lambda w=widget: self._seed_runtime_result_processor(w)
            )
        self._resultFollowers.add(id(widget))
        self._seed_runtime_result_processor(widget)

    def _seed_graph_controller(self) -> None:
        """Render the active result's plot payloads into a freshly opened Graph."""
        controller = self.mainViewController.graphController
        if controller is None:
            return
        try:
            result = self.mainViewController.reconstructionController.getActiveResult()
        except Exception:
            return
        try:
            controller.currentResultChanged(result)
        except Exception:
            self.__logger.debug(
                "Could not seed the Graph panel with the current result",
                exc_info=True,
            )

    def _seed_metadata_controller(self) -> None:
        """Show the already-loaded file's metadata in a freshly opened panel."""
        controller = self.mainViewController.metadataController
        if controller is None:
            return
        data_obj = getattr(self.mainViewController, '_currentDataObj', None)
        if data_obj is None:
            return
        try:
            controller.currentDataChanged(data_obj)
        except Exception:
            self.__logger.debug(
                "Could not seed the Metadata panel with the current data",
                exc_info=True,
            )

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
            self._panelResultBridges.add(id(widget))
        # Publishing and following are not alternatives. The ROI manager does
        # both — it produces a label image *and* measures whatever result is
        # selected — and routing it here used to cost it the follower wiring
        # entirely, so its across-results list went stale the moment it gained
        # a publish path. `_wire_result_follower` is idempotent.
        self._wire_result_follower(widget)

    def _seed_runtime_result_processor(self, widget) -> None:
        """Populate a newly opened processor dock with the loaded results.

        Runtime result-processor widgets are often opened after a
        reconstruction has already been selected. Those widgets only receive
        future sigCurrentResultChanged / sigResultsChanged events, so seed
        them explicitly at wire time — a multi-input panel opened after the
        reconstructions were loaded would otherwise show an empty picker.
        """
        # Looked up on the class: a Qt widget that does not define the method
        # answers a plain instance getattr by raising, not by returning None.
        available = getattr(type(widget), "setAvailableResults", None)
        if callable(available):
            available = available.__get__(widget)
            try:
                available(
                    self.__commChannel.getAllResults(),
                    self.__commChannel.getSelectedResults(),
                )
            except Exception:
                self.__logger.debug(
                    "Could not seed runtime processor widget with the result set",
                    exc_info=True,
                )
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
        self._routeResultToAnalysisPanels(result)
        self._bridgeResultToImcontrol(result, name)

    def _routeResultToAnalysisPanels(self, result) -> None:
        """Show non-image results in the panels that can actually render them.

        Table- and curve-kind results carry nothing the reconstruction viewer
        can draw, so producing one used to leave the user staring at a cleared
        canvas. Route their rows to the shared Results dock and reveal the
        Graph dock for curves, gated on ``kind`` so ordinary image results
        (many of which also expose plot payloads) never steal focus.

        The two are not exclusive. An analysis that fits something produces
        both a curve and the parameters of the fit; a result may therefore
        render into the Graph *and* contribute rows, and one that says so via
        ``publishes_table_rows`` gets both. Rows stay opt-in outside
        ``kind == "table"`` because a localization result's ``table_records()``
        can run to six figures.
        """
        from imswitch.improcess.model.result import result_kind

        kind = result_kind(result)
        if kind == "table" or getattr(result, "publishes_table_rows", False):
            self._appendResultTableRecords(result)
        if kind == "curve" and self._resultHasPlotPayloads(result):
            try:
                self.__mainView.raiseDockByTitle('Graph')
            except Exception:
                self.__logger.debug(
                    "Could not reveal the Graph dock", exc_info=True
                )

    def _appendResultTableRecords(self, result) -> None:
        try:
            records = list(result.table_records())
            if not records:
                return
            columns = list(result.table_columns()) or list(records[0].keys())
        except Exception:
            self.__logger.exception(
                "Could not read table records from %s",
                getattr(result, "name", type(result).__name__),
            )
            return
        try:
            self.__mainView.appendResultTableRecords(columns, records)
        except Exception:
            self.__logger.exception("Could not append result rows to the Results table")

    @staticmethod
    def _resultHasPlotPayloads(result) -> bool:
        payloads = getattr(result, "plot_payloads", None)
        if not callable(payloads):
            return False
        try:
            return bool(payloads())
        except Exception:
            return False

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
        self.__factory.closeAllCreatedControllers(waitTimeoutS=5)


def _runtime_tool_display_title(spec) -> str:
    category = str(getattr(spec, "category", "") or "").strip()
    title = str(getattr(spec, "title", "") or getattr(spec, "id", ""))
    return f"{category}: {title}" if category else title


class _ROIManagerStateAdapter:
    """Persistence for the ROI manager's sets, owned by the controller (A-09).

    The panel is runtime-loaded, so at startup it usually does not exist yet.
    Two behaviours follow, and both matter:

    * a restore that arrives before the panel is **stashed**, and applied when
      the panel is next built;
    * a save that cannot read a panel falls back to the **last state known** —
      whether that came from a restore this session or from the last
      successful read. Returning an empty state there is the silent data loss
      this class exists to prevent, and there are two ways to reach it: never
      opening the panel, and *closing* it, which leaves the attribute pointing
      at a destroyed C++ object whose every method raises.

    It deliberately does not force the panel open.
    """

    def __init__(self, view: Any, logger: Any = None) -> None:
        self._view = view
        self._logger = logger
        self._stash: Dict[str, Any] | None = None
        # The last state anyone knew about: a restore, or the last time the
        # panel could be read. This is what a closed panel falls back to.
        self._lastKnown: Dict[str, Any] | None = None

    def _panel(self):
        return getattr(self._view, 'roiManagerWidget', None)

    def _fallback(self) -> Dict[str, Any]:
        """What to save when the panel cannot be asked."""
        return dict(self._lastKnown or self._stash or {})

    def getWidgetState(self) -> Dict[str, Any]:
        panel = self._panel()
        if panel is None:
            return self._fallback()
        try:
            payload = panel.roiState()
        except Exception:
            # A destroyed panel raises from every method, including this one.
            # Saving what it last said beats saving nothing.
            if self._logger is not None:
                self._logger.debug('Could not read ROI manager state', exc_info=True)
            return self._fallback()
        self._lastKnown = payload

        from imswitch.imcommon.model import dirtools
        from imswitch.improcess.model.roi_persistence import (
            should_spill,
            spill_marker,
            write_spill,
        )

        if not should_spill(payload):
            return payload
        try:
            write_spill(payload, dirtools.UserFileDirs.Root)
        except Exception:
            # Falling back to the state store is slow but correct; failing to
            # save at all because a file could not be written is not.
            if self._logger is not None:
                self._logger.warning(
                    'Could not write the ROI spill file; keeping the sets in '
                    'the state store instead',
                    exc_info=True,
                )
            return payload
        return spill_marker(payload)

    def setWidgetState(self, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict) or not state:
            return
        from imswitch.imcommon.model import dirtools
        from imswitch.improcess.model.roi_persistence import ROIStateError, unpack

        try:
            sets, active, options, _dropped = unpack(state, dirtools.UserFileDirs.Root)
        except ROIStateError as exc:
            if self._logger is not None:
                self._logger.warning(f'Could not restore ROI sets: {exc}')
            return

        from imswitch.improcess.model.roi_persistence import sets_payload

        payload = sets_payload(sets, active, options)
        self._lastKnown = payload
        panel = self._panel()
        if panel is None:
            # Stashed, applied when the panel is next built.
            self._stash = payload
            return
        try:
            panel.setRoiState(payload)
        except Exception:
            if self._logger is not None:
                self._logger.warning('Could not apply ROI sets', exc_info=True)

    def applyStashTo(self, panel) -> None:
        """Hand a freshly built panel the state that arrived before it existed."""
        if self._stash is None or panel is None:
            return
        try:
            panel.setRoiState(self._stash)
        except Exception:
            if self._logger is not None:
                self._logger.warning('Could not apply stashed ROI sets', exc_info=True)
        else:
            self._stash = None

    def getStateSchemaVersion(self) -> int:
        return 1


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
