from __future__ import annotations

import numpy as np

from ..core.calibration.geometry import calibration_geometry_mismatches
from ..core.config import SLM_CONFIG_SCHEMA_VERSION,SLMConfig
from ..core.engine.corrections import CorrectionProvider,ResolvedCorrections
from ..core.engine.registry import DEFAULT_REGISTRIES,SLMRegistries
from ..core.engine.runtime import SLMRuntime
from ..core.engine.section.geometry import SectionGeometry
from ..setup import SLMDefinition


class SLMRuntimeFactory:
    """Construct and validate runtimes for one canonical SLM definition."""

    def __init__(
        self,
        *,
        definition: SLMDefinition,
        registries: SLMRegistries | None=None,
        correction_provider: CorrectionProvider | None=None,
    ) -> None:
        if not isinstance(definition,SLMDefinition):
            raise TypeError("definition must be an SLMDefinition")
        registries = DEFAULT_REGISTRIES if registries is None else registries
        if not isinstance(registries,SLMRegistries):
            raise TypeError("registries must be SLMRegistries or None")
        self.definition = definition
        self.registries = registries
        self.correction_provider = correction_provider

    def create_default(self) -> SLMRuntime:
        return SLMRuntime(
            identity=self.definition.identity,
            geometry=self.definition.geometry,
            section_geometries=self.definition.section_geometries,
            registries=self.registries,
            correction_provider=self.correction_provider,
        )

    def validate_config(self,config: SLMConfig):
        if config.identity != self.definition.identity:
            raise ValueError("SLM config identity does not match the physical SLM")
        return self.definition.validate_layout(
            config.geometry,
            {key:section.geometry for key,section in config.sections.items()},
        )

    def validate_calibration_geometry(self,config: SLMConfig) -> None:
        mismatches = calibration_geometry_mismatches(
            (key,section.geometry,section.calibration)
            for key,section in config.sections.items()
        )
        if mismatches:
            raise ValueError(
                "Config calibration geometry does not match its section layout: "
                + "; ".join(item.summary() for item in mismatches)
            )

    def create_from_config(
        self,config: SLMConfig,*,saved_correction_sections=(),
    ) -> SLMRuntime:
        self.validate_config(config)
        return SLMRuntime.from_config(
            config,
            registries=self.registries,
            correction_provider=self.correction_provider,
            saved_correction_sections=saved_correction_sections,
        )

    def resolve_corrections(
        self,wavelength_nm: int,geometry: SectionGeometry,
    ) -> ResolvedCorrections:
        provider = self.correction_provider
        if provider is None:
            return ResolvedCorrections.defaults(wavelength_nm,geometry)
        return provider.resolve(wavelength_nm,geometry)

    def create_layout_replacement(
        self,
        runtime: SLMRuntime,
        section_geometries,
        *,
        clear_calibration_sections=(),
        topologies_by_section=None,
        presentations=None,
    ) -> SLMRuntime:
        self.definition.validate_layout(runtime.geometry,section_geometries)
        clear = set(clear_calibration_sections or ())
        current_config = runtime.create_config()
        sections = {}
        for key,geometry in section_geometries.items():
            try:
                section = current_config.sections[key].clone(self.registries)
            except KeyError as error:
                raise ValueError("Changing section identity/count is not supported") from error
            section.geometry = geometry
            section.cgh_session = None
            section.correction_snapshot = self.resolve_corrections(
                int(section.state.optics.wavelength_nm),geometry,
            )
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


__all__ = ["SLMRuntimeFactory"]
