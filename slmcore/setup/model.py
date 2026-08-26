from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
from types import MappingProxyType
from typing import Any,Mapping

from ..engine.device import SLMGeometry,SLMIdentity
from ..engine.section.geometry import (
    SectionGeometry,
    SectionSplitLayout,
    create_split_section_geometries,
    validate_config_section_layout,
)


@dataclass(frozen=True)
class SLMSectionsSetup:
    """Setup-level section layout and whether configs may change its geometry."""

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
    def from_dict(cls,data: Mapping[str,Any]) -> "SLMSectionsSetup":
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
            customizable=data.get("customizable",False),
        )


@dataclass(frozen=True)
class SLMCorrectionSetup:
    """Installed correction resources for one physical SLM.

    ``preferred_directory`` mirrors the existing host behavior: if supplied
    and it exists, it is preferred over the workspace's serial-number based
    correction directory. slmcore never creates a correction directory merely
    because corrections are configured.
    """

    wavelength_table_file: str | None = None
    preferred_directory: str | Path | None = None

    def __post_init__(self) -> None:
        table = self.wavelength_table_file
        if table is not None:
            table = str(table).strip() or None
        directory = self.preferred_directory
        if directory is not None:
            directory = str(directory).strip() or None
            if directory is not None:
                directory = str(Path(directory).expanduser())
        object.__setattr__(self,"wavelength_table_file",table)
        object.__setattr__(self,"preferred_directory",directory)

    def to_dict(self) -> dict[str,Any]:
        return {
            "wavelength_table_file":self.wavelength_table_file,
            "preferred_directory":self.preferred_directory,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any] | None) -> "SLMCorrectionSetup | None":
        if data is None:
            return None
        return cls(
            wavelength_table_file=data.get("wavelength_table_file"),
            preferred_directory=data.get("preferred_directory"),
        )


@dataclass(frozen=True)
class SLMHardwareSetup:
    """Declarative hardware binding reserved for native slmcore drivers."""

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
    def from_dict(cls,data: Mapping[str,Any] | None) -> "SLMHardwareSetup | None":
        if data is None:
            return None
        return cls(driver=str(data["driver"]),options=dict(data.get("options",{})))


@dataclass(frozen=True)
class SLMSetup:
    """Canonical slmcore description of one installed physical SLM."""

    identity: SLMIdentity
    geometry: SLMGeometry
    sections: SLMSectionsSetup
    corrections: SLMCorrectionSetup | None = None
    hardware: SLMHardwareSetup | None = None
    _section_geometries: Mapping[str,SectionGeometry] = field(
        init=False,repr=False,compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.identity,SLMIdentity):
            raise TypeError("identity must be an SLMIdentity")
        if not isinstance(self.geometry,SLMGeometry):
            raise TypeError("geometry must be an SLMGeometry")
        if not isinstance(self.sections,SLMSectionsSetup):
            raise TypeError("sections must be an SLMSectionsSetup")
        if self.corrections is not None and not isinstance(
            self.corrections,SLMCorrectionSetup,
        ):
            raise TypeError("corrections must be an SLMCorrectionSetup or None")
        if self.hardware is not None and not isinstance(self.hardware,SLMHardwareSetup):
            raise TypeError("hardware must be an SLMHardwareSetup or None")
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
            setup_section_geometries=self.section_geometries,
            section_layout_customizable=self.sections.customizable,
        )

    def to_dict(self) -> dict[str,Any]:
        return {
            "identity":self.identity.to_dict(),
            "geometry":self.geometry.to_dict(),
            "sections":self.sections.to_dict(),
            "corrections":(
                None if self.corrections is None else self.corrections.to_dict()
            ),
            "hardware":None if self.hardware is None else self.hardware.to_dict(),
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "SLMSetup":
        return cls(
            identity=SLMIdentity.from_dict(data["identity"]),
            geometry=SLMGeometry.from_dict(data["geometry"]),
            sections=SLMSectionsSetup.from_dict(data["sections"]),
            corrections=SLMCorrectionSetup.from_dict(data.get("corrections")),
            hardware=SLMHardwareSetup.from_dict(data.get("hardware")),
        )
