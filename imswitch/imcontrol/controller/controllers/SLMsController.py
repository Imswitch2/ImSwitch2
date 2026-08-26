from __future__ import annotations

import os
import re
import traceback
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from slmcore import (
    SLMCorrectionSetup,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMWorkspace,
    SLMWorkspaceLayout,
    SectionSplitLayout,
)
from slmcore.calibration import get_default_active_planes,set_default_active_plane
from slmcore.host import SLMDeviceProvider,SLMHostServices
from slmcore.measurement import create_image_measurement
from slmcore.qt import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,SectionsDisplayMode,
    SLMControlMode,SLMQtSession,SLMQtSessionFactory,SLMQtSessionGroup,
)

from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)
from imswitch.imcommon.model import dirtools,initLogger, signaltools
from imswitch.imcontrol.model import configfiletools,getWidgetStatePersistence


class SLMsController(StatefulComponentMixin,ImConWidgetController):
    """Thin ImSwitch adapter around reusable slmcore SLM application control."""

    componentName = "SLMs"
    setupModeDisplayName = "SLM"
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = "spatial_light_modulator"
    setupModeApplyPriority = SetupModeApplyPriority.MULTI_SPATIAL_LIGHT_MODULATOR
    setupModeHardwareCritical = True

    def __init__(self,*args: Any,**kwargs: Any) -> None:
        super().__init__(*args,**kwargs)
        self.__logger = initLogger(self)
        self._slm_names: dict[str,str] = {}
        self._slm_infos: dict[str,Any] = {}
        self._slm_qt_sessions: dict[str,SLMQtSession] = {}
        self._interaction_settings = DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS

        self._initialize_storage()
        self._slm_qt_factory = SLMQtSessionFactory(
            workspace=self._workspace,
        )
        self._slm_qt_group = SLMQtSessionGroup()
        self._slm_qt_group.sigControlModeChanged.connect(
            self._widget.set_control_mode,
        )
        self._slm_qt_group.sigControlModeAvailabilityChanged.connect(
            self._widget.set_control_mode_change_enabled,
        )
        self._widget.sigControlModeRequested.connect(
            self.on_control_mode_requested,
        )
        self._initialize_slms()
        if self._slm_names:
            getWidgetStatePersistence().register("SLMs",self)

    # ------------------------------------------------------------------
    # ImSwitch unified component state
    # ------------------------------------------------------------------

    def getComponentState(self) -> dict:
        slms = {}
        for slm_key,slm_name in self._slm_names.items():
            session = self._slm_qt_session(slm_key)
            control_mode = session.control_mode
            path = (
                session.fast_config_path
                if control_mode is SLMControlMode.FAST_CONFIG
                else session.current_config_path
            )
            slms[slm_key] = {
                "slmName":slm_name,
                "configPath":path,
                "configName":os.path.basename(path) if path else None,
                "controlMode":control_mode.value,
                "config":({} if not path else {"path":path}),
            }
        return {"slms":slms}

    def applyComponentState(
        self,state: dict,*,applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        if applyMode == ComponentStateApplyMode.STARTUP_RESTORE:
            return []
        if applyMode != ComponentStateApplyMode.SETUP_MODE_APPLY:
            return [f"Unsupported SLMs apply mode: {applyMode}"]
        return self._apply_setup_mode_state_to_sessions(state)

    def describeComponentState(self,state: dict) -> list[str]:
        slms = (state or {}).get("slms") or {}
        if not slms:
            return ["  no SLM state"]

        summaries = []
        for slm_key,slm_state in sorted(slms.items(),key=lambda item:str(item[0])):
            slm_name = slm_state.get("slmName") or slm_key
            config = (
                slm_state.get("configName")
                or slm_state.get("configPath")
                or "None"
            )
            mode = slm_state.get("controlMode")
            suffix = f" ({mode})" if mode else ""
            summaries.append(f"  {slm_name}: {self._fmt(config)}{suffix}")
        return summaries

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None=None,
    ) -> list[dict]:
        return []

    def getSetupModeState(self):
        return self.getComponentState()

    def applySetupModeState(self,state):
        return self.applyComponentState(
            state,
            applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
        )

    def _apply_setup_mode_state_to_sessions(self,state: dict) -> list[str]:
        warnings = []
        if not isinstance(state,dict):
            return ["Saved SLMs state is not a dictionary."]
        saved_slms = state.get("slms",{})
        if not isinstance(saved_slms,dict):
            return ["Saved SLM entries are not a dictionary."]

        for saved_key,slm_state in saved_slms.items():
            if not isinstance(slm_state,dict):
                warnings.append(f'SLM "{saved_key}" saved state is not a dictionary.')
                continue
            slm_key = saved_key
            if slm_key not in self._slm_names:
                saved_name = slm_state.get("slmName")
                slm_key = next((
                    key for key,name in self._slm_names.items()
                    if name == saved_name
                ),None)
            if slm_key not in self._slm_names:
                warnings.append(
                    f'SLM "{saved_key}" ({slm_state.get("slmName")}) is not available.'
                )
                continue

            path = self._resolve_setup_mode_config_path(slm_key,slm_state)
            if path is None:
                name = slm_state.get("configName") or slm_state.get("configPath")
                warnings.append(
                    f'SLM "{self._slm_names[slm_key]}" config "{name}" is not available.'
                )
                continue

            qt_session = self._slm_qt_session(slm_key)
            try:
                control_mode = self._saved_control_mode(slm_state)
            except ValueError as error:
                warnings.append(
                    f'SLM "{self._slm_names[slm_key]}" saved control mode is invalid: {error}.'
                )
                continue
            if control_mode is not None and qt_session.control_mode is not control_mode:
                if not self._slm_qt_group.set_control_mode(control_mode):
                    warnings.append(
                        f'Could not switch SLMs to "{control_mode.value}" mode.'
                    )
                    continue

            load_ok = (
                qt_session.activate_compiled_config(path)
                if qt_session.control_mode is SLMControlMode.FAST_CONFIG
                else qt_session.load_config(
                    path,
                    confirm_layout_change=False,
                    calibration_mismatch_policy="reject",
                    show_error=False,
                )
            )
            if not load_ok:
                warnings.append(
                    f'Failed to load SLM "{self._slm_names[slm_key]}" config "{path}".'
                )
                continue
            loaded = (
                qt_session.fast_config_path
                if qt_session.control_mode is SLMControlMode.FAST_CONFIG
                else qt_session.current_config_path
            )
            if not loaded:
                warnings.append(
                    f'SLM "{self._slm_names[slm_key]}" config "{path}" may not have loaded.'
                )
            elif (
                os.path.normcase(os.path.abspath(str(loaded)))
                != os.path.normcase(os.path.abspath(str(path)))
            ):
                warnings.append(
                    f'SLM "{self._slm_names[slm_key]}" loaded "{loaded}" instead of "{path}".'
                )
        return warnings

    @staticmethod
    def _saved_control_mode(slm_state: Mapping[str,Any]) -> SLMControlMode | None:
        mode = slm_state.get("controlMode")
        if mode is None:
            mode = slm_state.get("control_mode")
        config = slm_state.get("config",{})
        if mode is None and isinstance(config,Mapping):
            mode = config.get("controlMode") or config.get("control_mode")
        if mode is None:
            return None
        return SLMControlMode.normalize(mode)

    def _resolve_setup_mode_config_path(
        self,slm_key: str,slm_state: Mapping[str,Any],
    ) -> str | None:
        config_path = slm_state.get("configPath")
        config_name = slm_state.get("configName")
        config = slm_state.get("config",{})
        if isinstance(config,Mapping):
            config_path = config_path or config.get("path")
            if config_path and config_name is None:
                config_name = os.path.basename(str(config_path))
        candidates = []
        if config_path:
            candidates.append(Path(str(config_path)))
        if config_name:
            repository = self._slm_qt_session(slm_key).config_repository
            if repository is not None:
                candidates.append(repository.resolve(config_name))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    # ------------------------------------------------------------------
    # Construction / host adapters
    # ------------------------------------------------------------------

    def _initialize_storage(self) -> None:
        self._slm_dir = os.path.join(dirtools.UserFileDirs.Root,"imcontrol_slm")
        self._workspace = SLMWorkspace(
            self._slm_dir,
            layout=SLMWorkspaceLayout(
                configs="configs",
                corrections="Corrections",
                calibrations="calibrations",
                preferences="preferences.json",
            ),
        )

    def _initialize_slms(self) -> None:
        for slm_name,slm_manager in self._master.slmsManager:
            try:
                self._initialize_slm(slm_name,slm_manager)
            except Exception as error:
                self.__logger.error(traceback.format_exc())
                self._widget.show_error(
                    "SLM initialization failed",
                    f"Could not initialize '{slm_name}':\n{error}",
                )

    def _initialize_slm(self,slm_name: str,slm_manager: Any) -> None:
        """Prepare one SLM completely, then atomically commit it to ImSwitch."""
        slm_info = slm_manager.slmInfo
        if slm_info is None:
            raise ValueError(f"SLM '{slm_name}' has no slmInfo")
        slm_key = self._make_slm_key(slm_name)
        if slm_key in self._slm_names:
            raise KeyError(f"SLM key {slm_key!r} is already initialized")

        # Prepare: no ImSwitch UI or controller registries are mutated here.
        setup = self._create_setup(slm_key,slm_info)
        host_services = self._create_host_services(
            slm_name=slm_name,
            slm_info=slm_info,
            slm_manager=slm_manager,
        )
        qt_session,panel = self._slm_qt_factory.create(
            setup=setup,
            host_services=host_services,
            display_name=slm_name,
            interaction_settings=self._interaction_settings,
            auto_upload_frame=True,
        )

        # Commit only after reusable construction has completed successfully.
        try:
            self._widget.add_slm(
                slm_key=slm_key,slm_name=slm_name,panel=panel,
            )
            self._slm_names[slm_key] = slm_name
            self._slm_infos[slm_key] = slm_info
            self._slm_qt_sessions[slm_key] = qt_session
            self._slm_qt_group.add_session(qt_session,key=slm_key)
            self._install_session_signals(slm_key,qt_session)
        except Exception:
            self._slm_qt_group.remove_session(slm_key)
            self._widget.remove_slm(slm_key)
            self._slm_names.pop(slm_key,None)
            self._slm_infos.pop(slm_key,None)
            self._slm_qt_sessions.pop(slm_key,None)
            try:
                qt_session.dispose()
            finally:
                panel.deleteLater()
            raise

        # Device initialization is a recoverable runtime side effect. It occurs
        # only after the SLM is fully mounted and registered with the host.
        qt_session.initialize_device(show_error=False)

    def closeEvent(self):
        self._dispose_slm_sessions()
        parent_close = getattr(super(),"closeEvent",None)
        if callable(parent_close):
            return parent_close()
        return True

    def __del__(self) -> None:
        try:
            self._dispose_slm_sessions()
        except Exception:
            pass

    def _dispose_slm_sessions(self) -> None:
        for slm_key,qt_session in tuple(self._slm_qt_sessions.items()):
            try:
                self._slm_qt_group.remove_session(slm_key)
            except Exception:
                pass
            try:
                qt_session.dispose()
            except Exception:
                self.__logger.error(traceback.format_exc())
        self._slm_qt_sessions.clear()

    def on_control_mode_requested(self,mode: Any) -> None:
        if not self._slm_qt_group.set_control_mode(mode):
            self._widget.set_control_mode(self._slm_qt_group.control_mode)

    def _create_host_services(
        self,
        *,
        slm_name: str,
        slm_info: Any,
        slm_manager: Any,
    ) -> SLMHostServices:
        return SLMHostServices.from_callbacks(
            device=self._create_device_provider(slm_name,slm_manager),
            measurement_provider=_SlmMeasurementProvider(
                master=self._master,comm_channel=self._commChannel,
            ),
            get_startup_config=lambda name=slm_name,info=slm_info:
                self._get_startup_config_for(name,info),
            set_startup_config=lambda value,name=slm_name,info=slm_info:
                self._set_startup_config_for(name,info,value),
            get_default_plane=lambda section,name=slm_name,info=slm_info:
                self._get_default_active_plane_for(name,info,section),
            set_default_plane=lambda section,plane,name=slm_name,info=slm_info:
                self._set_default_active_plane_for(name,info,section,plane),
            get_section_display_mode=lambda name=slm_name,info=slm_info:
                self._get_section_view_mode_for(name,info),
            set_section_display_mode=lambda value,name=slm_name,info=slm_info:
                self._set_section_view_mode_for(name,info,value),
        )

    def _install_session_signals(
        self,slm_key: str,qt_session: SLMQtSession,
    ) -> None:
        qt_session.sigError.connect(
            lambda title,error,key=slm_key:
                self._on_session_error(key,title,error)
        )
        qt_session.sigInteractionSettingsChanged.connect(
            self.on_interaction_settings_changed,
        )

    def _create_setup(self,slm_key: str,slm_info: Any) -> SLMSetup:
        serial_number = getattr(slm_info,"serial_number",None)
        if serial_number is None or not str(serial_number).strip():
            raise ValueError(
                f"SLM {slm_key!r} must define a non-empty serial_number"
            )

        geometry = SLMGeometry(
            width=int(slm_info.width),
            height=int(slm_info.height),
            pixel_size_um=float(slm_info.pixelSize),
        )
        layout_info = self._read_setup_section_layout(slm_info)
        n_sections = int(getattr(slm_info,"nSections",None) or 1)
        layout = SectionSplitLayout(
            n_sections=n_sections,
            axis=str(self._layout_value(layout_info,"splitAxis",default="x") or "x"),
            mode=str(self._layout_value(layout_info,"layoutMode",default="even") or "even"),
            sizes=(
                None if self._layout_value(layout_info,"layoutSizes",default=None) is None
                else tuple(int(value) for value in self._layout_value(layout_info,"layoutSizes"))
            ),
        )
        return SLMSetup(
            identity=SLMIdentity(
                key=slm_key,
                serial_number=str(serial_number),
            ),
            geometry=geometry,
            sections=SLMSectionsSetup(
                layout=layout,
                customizable=self._layout_bool(
                    layout_info,"customizable",default=False,
                ),
            ),
            corrections=SLMCorrectionSetup(
                preferred_directory=getattr(
                    slm_info,"correctionPatternsDir",None,
                ),
                wavelength_table_file=getattr(
                    slm_info,"wavelengthTableFile",None,
                ),
            ),
        )

    @staticmethod
    def _read_setup_section_layout(slm_info: Any) -> Any:
        value = getattr(slm_info,"sectionLayout",None)
        if value is not None:
            return value
        properties = getattr(slm_info,"managerProperties",None) or {}
        return properties.get("sectionLayout") if isinstance(properties,Mapping) else None

    @classmethod
    def _layout_bool(cls,layout_info: Any,name: str,*,default=False) -> bool:
        value = cls._layout_value(layout_info,name,default=default)
        if isinstance(value,str):
            return value.strip().lower() in ("1","true","yes","on")
        return bool(value)

    @staticmethod
    def _layout_value(layout_info: Any,name: str,*,default=None):
        if layout_info is None:
            return default
        value = (
            layout_info.get(name,default)
            if isinstance(layout_info,Mapping)
            else getattr(layout_info,name,default)
        )
        return default if value is None else value

    # ------------------------------------------------------------------
    # setupInfo-backed preferences
    # ------------------------------------------------------------------

    def _get_setup_slm_info_by_name(
        self,slm_name: str,fallback: Any | None=None,
    ):
        slm_info = (getattr(self._setupInfo,"slms",{}) or {}).get(slm_name)
        if slm_info is None:
            slm_info = fallback
        if slm_info is None:
            raise KeyError(f"Could not find SLM '{slm_name}' in setup info")
        return slm_info

    def _manager_properties_for(
        self,slm_name: str,fallback: Any | None=None,
    ):
        slm_info = self._get_setup_slm_info_by_name(slm_name,fallback)
        properties = getattr(slm_info,"managerProperties",None)
        if properties is None:
            properties = {}
            object.__setattr__(slm_info,"managerProperties",properties)
        return properties

    def _get_default_active_plane_for(
        self,slm_name: str,slm_info: Any,section_key: str,
    ):
        return get_default_active_planes(
            self._manager_properties_for(slm_name,slm_info)
        ).get(section_key)

    def _set_default_active_plane_for(
        self,
        slm_name: str,
        slm_info: Any,
        section_key: str,
        plane_name: str | None,
    ) -> None:
        set_default_active_plane(
            self._manager_properties_for(slm_name,slm_info),
            section_key,
            plane_name,
        )
        self._save_setup_info()

    def _get_startup_config_for(
        self,slm_name: str,slm_info: Any,
    ) -> str | None:
        return self._manager_properties_for(slm_name,slm_info).get("startConfig")

    def _set_startup_config_for(
        self,slm_name: str,slm_info: Any,filename: str | None,
    ) -> None:
        properties = self._manager_properties_for(slm_name,slm_info)
        if filename:
            properties["startConfig"] = str(filename)
        else:
            properties.pop("startConfig",None)
        self._save_setup_info()

    def _get_section_view_mode_for(self,slm_name: str,slm_info: Any):
        return getattr(
            self._get_setup_slm_info_by_name(slm_name,slm_info),
            "sectionViewMode",
            None,
        )

    def _set_section_view_mode_for(
        self,slm_name: str,slm_info: Any,value: Any,
    ) -> None:
        mode = SectionsDisplayMode.normalize(value)
        setup_slm_info = self._get_setup_slm_info_by_name(slm_name,slm_info)
        if getattr(setup_slm_info,"sectionViewMode",None) == mode.value:
            return
        object.__setattr__(setup_slm_info,"sectionViewMode",mode.value)
        self._save_setup_info()

    def _save_setup_info(self) -> None:
        configfiletools.saveSetupInfo(
            configfiletools.loadOptions()[0],self._setupInfo,
        )

    # ------------------------------------------------------------------
    # Shared Qt policy / session lookup
    # ------------------------------------------------------------------

    def on_interaction_settings_changed(self,settings: Any) -> None:
        self._interaction_settings = settings
        for qt_session in tuple(self._slm_qt_sessions.values()):
            qt_session.set_interaction_settings(settings)

    def _slm_qt_session(self,slm_key: str) -> SLMQtSession:
        try:
            return self._slm_qt_sessions[slm_key]
        except KeyError as error:
            raise RuntimeError(
                f"SLM Qt session is not initialized for {slm_key!r}"
            ) from error

    def _on_session_error(
        self,slm_key: str,title: str,error: Any,
    ) -> None:
        trace = getattr(error,"traceback_text",None)
        detail = str(trace) if trace else f"{title}: {error}"
        self.__logger.error(f"[{slm_key}] {detail}")

    def _create_device_provider(
        self,slm_name: str,slm_manager: Any,
    ) -> SLMDeviceProvider:
        requires_connection = bool(
            getattr(slm_manager,"requires_device_connection",False)
        )

        def upload(frame):
            return self._master.slmsManager.execOn(
                slm_name,lambda manager:manager.upload_pattern(frame),
            )

        def connect():
            return self._master.slmsManager.execOn(
                slm_name,lambda manager:manager.connect_to_device(),
            )

        def disconnect():
            return self._master.slmsManager.execOn(
                slm_name,lambda manager:manager.close_device(),
            )

        return SLMDeviceProvider(
            upload_frame=upload,
            connect=(connect if requires_connection else None),
            disconnect=(disconnect if requires_connection else None),
            requires_explicit_connection=requires_connection,
        )

    @staticmethod
    def _make_slm_key(name: str) -> str:
        key = re.sub(r"[^0-9a-zA-Z_]+","_",name.strip())
        key = key.strip("_").lower()
        if not key:
            raise ValueError(f"Cannot derive an SLM key from {name!r}")
        return key

    def _fmt(self,value):
        if value is None:
            return "None"
        if isinstance(value,bool):
            return "ON" if bool(value) else "OFF"
        if isinstance(value,float):
            return f"{value:.4g}"
        if isinstance(value,(list,tuple)):
            return "[" + ", ".join(self._fmt(item) for item in value) + "]"
        return str(value)


CURRENT_DETECTOR_LABEL = "Current detector"

class _SlmMeasurementProvider:
    """Adapt ImSwitch detector signals to slmcore's measurement capability."""

    def __init__(
        self,
        *,
        master: Any,
        comm_channel: Any,
        additional_sources: Callable[[],Sequence[str]] | None=None,
        preferred_fallback: Callable[[str,Sequence[str]],str | None] | None=None,
        timeout_ms: int=1000,
    ) -> None:
        self._master = master
        self._comm_channel = comm_channel
        self._additional_sources = additional_sources
        self._preferred_fallback = preferred_fallback
        self._timeout_ms = int(timeout_ms)

    def available_sources(self,section_key: str) -> tuple[str,...]:
        del section_key
        names = []
        manager = getattr(self._master,"detectorsManager",None)
        if manager is not None:
            for method_name in (
                "getAllDeviceNames","getAllDetectorNames","getDeviceNames",
            ):
                method = getattr(manager,method_name,None)
                if not callable(method):
                    continue
                try:
                    values = method()
                except Exception:
                    continue
                if values is not None:
                    names.extend(str(value) for value in values)
                    break
            if not names:
                try:
                    names.extend(str(item[0]) for item in manager)
                except Exception:
                    pass

        callback = self._additional_sources
        if callable(callback):
            try:
                names.extend(str(value) for value in callback())
            except Exception:
                pass

        ordered = []
        seen = set()
        for value in names:
            name = str(value).strip()
            if name and name not in seen:
                ordered.append(name)
                seen.add(name)
        if not ordered:
            ordered.append(CURRENT_DETECTOR_LABEL)
        return tuple(ordered)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        available = tuple(str(value) for value in available)
        manager = getattr(self._master,"detectorsManager",None)
        if manager is not None:
            for method_name in (
                "getCurrentDetectorName","getCurrentDeviceName",
                "getCurrentDetector",
            ):
                method = getattr(manager,method_name,None)
                if not callable(method):
                    continue
                try:
                    value = method()
                except Exception:
                    continue
                name = self._detector_name(value)
                if name and (not available or name in available):
                    return name

        callback = self._preferred_fallback
        if callable(callback):
            try:
                name = callback(section_key,available)
            except Exception:
                name = None
            if name and (not available or str(name) in available):
                return str(name)

        if CURRENT_DETECTOR_LABEL in available:
            return CURRENT_DETECTOR_LABEL
        return available[0] if available else None

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[Any],None],
        on_error: Callable[[Exception],None],
    ):
        source = str(source or "").strip()
        if not source:
            raise ValueError("Select a detector before acquisition.")

        def handle_image(
            detector=None,image=None,is_current_detector=None,timeout=False,
        ):
            if timeout:
                on_error(TimeoutError(f"No image received from {source}."))
                return False
            if image is None or not self._matches(
                source,detector,bool(is_current_detector),section_key,
            ):
                return False
            try:
                emitted_name = self._detector_name(detector)
                measurement = create_image_measurement(
                    image,
                    source="detector",
                    detector=(
                        emitted_name
                        if source == CURRENT_DETECTOR_LABEL and emitted_name
                        else source
                    ),
                    metadata=metadata,
                )
            except Exception as error:
                on_error(error)
                return True
            on_result(measurement)
            return True

        def wrapper_slot(detector,image,_init,_scale,is_current_detector):
            return handle_image(
                detector=detector,
                image=image,
                is_current_detector=is_current_detector,
            )

        wrapper_slot._timeout_handler = (
            lambda timeout=False:handle_image(timeout=timeout)
        )
        return signaltools.OneShotConnection(
            signal=self._comm_channel.sigUpdateImage,
            slot=wrapper_slot,
            timeout_ms=self._timeout_ms,
            notify_timeout=True,
            wait_for_success=True,
        )

    def _matches(
        self,
        requested: str,
        emitted_detector: Any,
        is_current_detector: bool,
        section_key: str,
    ) -> bool:
        if requested == CURRENT_DETECTOR_LABEL:
            return bool(is_current_detector)
        emitted = self._detector_name(emitted_detector)
        if emitted:
            return emitted == requested
        if not is_current_detector:
            return False
        available = self.available_sources(section_key)
        return self.preferred_source(section_key,available) == requested

    @staticmethod
    def _detector_name(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value,str):
            text = value.strip()
            return text or None
        for attr in ("name","detectorName","deviceName"):
            item = getattr(value,attr,None)
            if item is not None and str(item).strip():
                return str(item).strip()
        text = str(value).strip()
        return text or None


__all__ = ["SLMsController","CURRENT_DETECTOR_LABEL"]
