from __future__ import annotations

from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping

from ..core.engine.device import SLMGeometry,SLMIdentity
from ..core.engine.section.geometry import (
    SectionGeometry,
    SectionSplitLayout,
    create_split_section_geometries,
    validate_config_section_layout,
)


@dataclass(frozen=True)
class SLMSectionsDefinition:
    """Definition-level section layout and whether configs may change its geometry."""

    layout: SectionSplitLayout
    customizable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.layout,SectionSplitLayout):
            raise TypeError("layout must be a SectionSplitLayout")
        if not isinstance(self.customizable,bool):
            raise TypeError("customizable must be a boolean")

    @property
    def section_count(self) -> int:
        return self.layout.n_sections

    def to_dict(self) -> dict[str,Any]:
        return {
            "layout":{
                "n_sections":self.layout.n_sections,
                "axis":self.layout.axis,
                "mode":self.layout.mode,
                "sizes":None if self.layout.sizes is None else list(self.layout.sizes),
                "key_prefix":self.layout.key_prefix,
            },
            "customizable":self.customizable,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "SLMSectionsDefinition":
        if not isinstance(data,Mapping):
            raise TypeError("sections must be a mapping")
        layout_data = data.get("layout",data)
        if not isinstance(layout_data,Mapping):
            raise TypeError("sections layout must be a mapping")
        sizes = layout_data.get("sizes")
        return cls(
            layout=SectionSplitLayout(
                n_sections=int(layout_data["n_sections"]),
                axis=str(layout_data.get("axis","x")),
                mode=str(layout_data.get("mode","even")),
                sizes=None if sizes is None else tuple(int(value) for value in sizes),
                key_prefix=str(layout_data.get("key_prefix","sec_")),
            ),
            customizable=bool(data.get("customizable",False)),
        )


@dataclass(frozen=True)
class SLMHardwareConfig:
    """Optional declarative binding consumed by an external hardware layer."""

    driver: str
    options: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        driver = str(self.driver or "").strip()
        if not driver:
            raise ValueError("hardware driver cannot be empty")
        object.__setattr__(self,"driver",driver)
        object.__setattr__(self,"options",MappingProxyType(dict(self.options or {})))

    def to_dict(self) -> dict[str,Any]:
        return {"driver":self.driver,"options":dict(self.options)}

    @classmethod
    def from_dict(cls,data: Mapping[str,Any] | None) -> "SLMHardwareConfig | None":
        if data is None:
            return None
        if not isinstance(data,Mapping):
            raise TypeError("hardware must be a mapping or null")
        return cls(driver=str(data["driver"]),options=dict(data.get("options",{})))


@dataclass(frozen=True)
class SLMDefinition:
    """Canonical portable definition of one physical SLM.

    The definition contains identity, geometry and section layout only. Hardware
    binding and startup/session preferences are intentionally separate concerns.
    Filesystem locations are also excluded; persistent resources are resolved by
    :class:`SLMWorkspace` from the physical serial number.
    """

    identity: SLMIdentity
    geometry: SLMGeometry
    sections: SLMSectionsDefinition
    _section_geometries: Mapping[str,SectionGeometry] = field(
        init=False,repr=False,compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.identity,SLMIdentity):
            raise TypeError("identity must be an SLMIdentity")
        if not isinstance(self.geometry,SLMGeometry):
            raise TypeError("geometry must be an SLMGeometry")
        if not isinstance(self.sections,SLMSectionsDefinition):
            raise TypeError("sections must be an SLMSectionsDefinition")
        geometries = create_split_section_geometries(
            self.geometry,self.sections.layout,
        )
        object.__setattr__(
            self,"_section_geometries",MappingProxyType(dict(geometries)),
        )

    @property
    def section_geometries(self) -> Mapping[str,SectionGeometry]:
        return self._section_geometries

    @property
    def section_count(self) -> int:
        return self.sections.section_count

    def validate_layout(
        self,
        config_geometry: SLMGeometry,
        section_geometries: Mapping[str,SectionGeometry],
    ):
        if len(section_geometries) != self.section_count:
            raise ValueError("Changing section count is not supported")
        return validate_config_section_layout(
            physical_geometry=self.geometry,
            config_geometry=config_geometry,
            config_section_geometries=section_geometries,
            definition_section_geometries=self.section_geometries,
            section_layout_customizable=self.sections.customizable,
        )

    def to_dict(self) -> dict[str,Any]:
        return {
            "identity":self.identity.to_dict(),
            "geometry":self.geometry.to_dict(),
            "sections":self.sections.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,data: Mapping[str,Any],*,key: str | None=None,
    ) -> "SLMDefinition":
        if not isinstance(data,Mapping):
            raise TypeError("definition must be a mapping")
        identity_data = data.get("identity")
        if not isinstance(identity_data,Mapping):
            raise TypeError("definition.identity must be a mapping")
        return cls(
            identity=SLMIdentity.from_dict(identity_data,key=key),
            geometry=SLMGeometry.from_dict(data["geometry"]),
            sections=SLMSectionsDefinition.from_dict(data["sections"]),
        )
