from __future__ import annotations

from dataclasses import dataclass


from ..calibration.geometry import calibration_geometry_mismatches
from ..config import SLMConfig
from ..corrections import SLMCorrectionStore
from ..engine.registry import DEFAULT_REGISTRIES,SLMRegistries
from ..engine.runtime import SLMRuntime
from ..setup import SLMSetup


@dataclass(frozen=True)
class StartupRuntime:
    runtime: SLMRuntime
    config_path: str | None
    warnings: tuple[str, ...] = ()


class SLMRuntimeFactory:
    """Construct and validate runtimes for one canonical SLM setup."""

    def __init__(
        self,
        *,
        setup: SLMSetup,
        registries: SLMRegistries | None=None,
        correction_store: SLMCorrectionStore | None=None,
        config_repository=None,
    ) -> None:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        
        registries = DEFAULT_REGISTRIES if registries is None else registries
        if not isinstance(registries,SLMRegistries):
            raise TypeError("registries must be SLMRegistries or None")

        self.setup = setup
        self.registries = registries
        self.correction_store = correction_store
        self.config_repository = config_repository

    def create_default(self) -> SLMRuntime:
        return SLMRuntime(
            identity=self.setup.identity,
            geometry=self.setup.geometry,
            section_geometries=self.setup.section_geometries,
            registries=self.registries,
            correction_store=self.correction_store,
        )

    def validate_config(self,config: SLMConfig):
        if config.identity != self.setup.identity:
            raise ValueError("SLM config identity does not match the physical SLM")
        return self.setup.validate_layout(
            config.geometry,
            {key:section.geometry for key,section in config.sections.items()},
        )

    def validate_calibration_geometry(self,config: SLMConfig) -> None:
        mismatches = calibration_geometry_mismatches(
            (key,section.geometry,section.calibration)
            for key,section in config.sections.items()
        )
        if mismatches:
            details = "; ".join(item.summary() for item in mismatches)
            raise ValueError(
                "Config calibration geometry does not match its section layout: "
                + details
            )

    def create_from_config(self,config: SLMConfig) -> SLMRuntime:
        self.validate_config(config)
        return SLMRuntime.from_config(
            config,
            registries=self.registries,
            correction_store=self.correction_store,
        )


    def create_layout_replacement(
        self,
        runtime: SLMRuntime,
        section_geometries,
        *,
        clear_calibration_sections=(),
        topologies_by_section=None,
        presentations=None,
    ) -> SLMRuntime:
        from ..config import SLM_CONFIG_SCHEMA_VERSION,SLMConfig
        import numpy as np

        self.setup.validate_layout(
            runtime.geometry,section_geometries,
        )
        clear = set(clear_calibration_sections or ())
        current_config = runtime.create_config()
        sections = {}
        for key,geometry in section_geometries.items():
            try:
                section = current_config.sections[key].clone(self.registries)
            except KeyError as error:
                raise ValueError(
                    "Changing section identity/count is not supported"
                ) from error
            section.geometry = geometry
            section.cgh_session = None
            if key in clear:
                section.calibration = None
            sections[key] = section

        config = SLMConfig(
            schema_version=SLM_CONFIG_SCHEMA_VERSION,
            identity=runtime.identity,
            geometry=runtime.geometry,
            sections=sections,
            final_eightbit=np.zeros(runtime.geometry.shape,dtype=np.uint8),
        )
        replacement = self.create_from_config(config)
        for section_key,topologies in dict(topologies_by_section or {}).items():
            replacement.apply_section_topology(section_key,topologies)
        for section_key,presentation in dict(presentations or {}).items():
            replacement.set_section_presentation(section_key,presentation)
        return replacement

    def create_startup(self,startup_name: str | None) -> StartupRuntime:
        name = str(startup_name or "").strip()
        if not name:
            return StartupRuntime(self.create_default(),None)
        repository = self.config_repository
        if repository is None:
            return StartupRuntime(
                self.create_default(),None,
                ("Startup config was requested but configuration storage is not configured.",),
            )
        try:
            config,warnings = repository.load(name)
            self.validate_config(config)
            # Startup is deliberately conservative: no modal override exists.
            self.validate_calibration_geometry(config)
            runtime = self.create_from_config(config)
            return StartupRuntime(
                runtime=runtime,
                config_path=str(repository.resolve(name)),
                warnings=tuple(_warning_text(item) for item in warnings),
            )
        except Exception as error:
            return StartupRuntime(
                runtime=self.create_default(),
                config_path=None,
                warnings=("Startup config '%s' was not loaded: %s" % (name,error),),
            )


def _warning_text(warning) -> str:
    path = ".".join(getattr(warning,"path",()) or ())
    message = str(getattr(warning,"message",warning))
    return "%s: %s" % (path,message) if path else message
