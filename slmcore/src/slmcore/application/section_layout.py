from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any,Mapping

from ..core.calibration.geometry import (
    CalibrationGeometryMismatch,
    calibration_geometry_mismatches,
)
from ..core.engine.runtime import SLMRuntime
from ..core.engine.section.geometry import (
    SectionGeometry,SectionSplitLayout,create_split_section_geometries,
    split_layout_signature,
)
from .configuration import CalibrationMismatchPolicy,CorrectionMismatchPolicy
from .runtime_factory import SLMRuntimeFactory


@dataclass(frozen=True)
class PreparedSectionLayoutChange:
    """Validated, side-effect-free description of one section-layout change."""

    layout: SectionSplitLayout
    section_geometries: Mapping[str,SectionGeometry]
    calibration_mismatches: tuple[CalibrationGeometryMismatch,...]
    saved_correction_sections: tuple[str,...]
    runtime_layout_signature: Any
    requested_layout_signature: Any

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"section_geometries",MappingProxyType(dict(self.section_geometries)),
        )
        object.__setattr__(
            self,"calibration_mismatches",tuple(self.calibration_mismatches),
        )
        object.__setattr__(
            self,"saved_correction_sections",tuple(self.saved_correction_sections),
        )

    @property
    def changed(self) -> bool:
        return self.requested_layout_signature != self.runtime_layout_signature


class SLMSectionLayoutService:
    """Toolkit-independent planning for editable SLM section layouts."""

    def __init__(self,*,runtime_factory: SLMRuntimeFactory) -> None:
        if not isinstance(runtime_factory,SLMRuntimeFactory):
            raise TypeError("runtime_factory must be an SLMRuntimeFactory")
        self.runtime_factory = runtime_factory

    @property
    def customizable(self) -> bool:
        return bool(self.runtime_factory.setup.sections.customizable)

    def prepare(
        self,runtime: SLMRuntime,layout: SectionSplitLayout,
    ) -> PreparedSectionLayoutChange:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        if not self.customizable:
            raise ValueError("This setup does not allow section layout editing.")
        if not isinstance(layout,SectionSplitLayout):
            raise TypeError("layout must be a SectionSplitLayout")

        setup = self.runtime_factory.setup
        if layout.n_sections != setup.section_count:
            raise ValueError("Changing section count is not supported")
        section_geometries = create_split_section_geometries(
            runtime.geometry,layout,
        )
        requested_signature = setup.validate_layout(
            runtime.geometry,section_geometries,
        )
        current_signature = split_layout_signature(
            runtime.geometry,
            {key:runtime.get_section_geometry(key) for key in runtime.section_keys},
        )
        mismatches = ()
        if requested_signature != current_signature:
            mismatches = calibration_geometry_mismatches(
                (
                    key,section_geometries[key],
                    runtime.get_section_calibration_copy(key),
                )
                for key in runtime.section_keys
            )
        saved_correction_sections = tuple(
            key for key in runtime.saved_correction_sections
            if runtime.get_section_geometry(key) != section_geometries[key]
        )
        return PreparedSectionLayoutChange(
            layout=layout,
            section_geometries=section_geometries,
            calibration_mismatches=mismatches,
            saved_correction_sections=saved_correction_sections,
            runtime_layout_signature=current_signature,
            requested_layout_signature=requested_signature,
        )

    def create_replacement(
        self,
        runtime: SLMRuntime,
        prepared: PreparedSectionLayoutChange,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        correction_mismatch_policy: CorrectionMismatchPolicy | str=(
            CorrectionMismatchPolicy.REJECT
        ),
        topologies_by_section: Mapping[str,Any] | None=None,
        presentations: Mapping[str,Any] | None=None,
    ) -> SLMRuntime | None:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        if not isinstance(prepared,PreparedSectionLayoutChange):
            raise TypeError("prepared must be a PreparedSectionLayoutChange")
        current_signature = split_layout_signature(
            runtime.geometry,
            {key:runtime.get_section_geometry(key) for key in runtime.section_keys},
        )
        if current_signature != prepared.runtime_layout_signature:
            raise RuntimeError(
                "Runtime layout changed after the section layout was prepared; prepare it again"
            )
        if not prepared.changed:
            return None

        policy = CalibrationMismatchPolicy.normalize(calibration_mismatch_policy)
        mismatches = prepared.calibration_mismatches
        if mismatches and policy is CalibrationMismatchPolicy.REJECT:
            raise ValueError(
                "Section layout is incompatible with current calibration geometry: "
                + "; ".join(item.summary() for item in mismatches)
            )
        clear_sections = (
            tuple(item.section_key for item in mismatches)
            if policy is CalibrationMismatchPolicy.CLEAR else ()
        )
        correction_policy = CorrectionMismatchPolicy.normalize(
            correction_mismatch_policy,
        )
        if (
            prepared.saved_correction_sections
            and correction_policy is not CorrectionMismatchPolicy.USE_CURRENT
        ):
            raise ValueError(
                "Changing section geometry invalidates saved correction snapshots; "
                "switch to current workspace corrections or cancel the layout change."
            )
        return self.runtime_factory.create_layout_replacement(
            runtime,prepared.section_geometries,
            clear_calibration_sections=clear_sections,
            topologies_by_section=topologies_by_section,
            presentations=presentations,
        )


__all__ = ["PreparedSectionLayoutChange","SLMSectionLayoutService"]
