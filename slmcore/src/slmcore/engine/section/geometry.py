from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..device import SLMGeometry


@dataclass(frozen=True)
class SectionGeometry:
    key: str
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("Section key cannot be empty")
        if self.x < 0 or self.y < 0:
            raise ValueError("Section x and y must be >= 0")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Section width and height must be > 0")

    @property
    def shape(self) -> tuple[int, int]:
        return self.height,self.width

    @property
    def slices(self) -> tuple[slice, slice]:
        return (
            slice(self.y,self.y + self.height),
            slice(self.x,self.x + self.width),
        )


@dataclass(frozen=True)
class SectionSplitLayout:
    """Fixed-count split layout requested for one physical SLM."""

    n_sections: int
    axis: str = "x"
    mode: str = "even"
    sizes: tuple[int, ...] | None = None
    key_prefix: str = "sec_"

    def __post_init__(self) -> None:
        if isinstance(self.n_sections,bool) or not isinstance(
            self.n_sections,int,
        ):
            raise TypeError("n_sections must be an integer")
        if self.n_sections <= 0:
            raise ValueError("n_sections must be > 0")

        axis = str(self.axis).strip().lower()
        if axis not in ("x","y"):
            raise ValueError("axis must be either 'x' or 'y'")
        object.__setattr__(self,"axis",axis)

        mode = str(self.mode).strip().lower()
        if mode not in ("even","manual"):
            raise ValueError("mode must be either 'even' or 'manual'")
        object.__setattr__(self,"mode",mode)

        if not self.key_prefix:
            raise ValueError("key_prefix cannot be empty")

        sizes = self.sizes
        if sizes is None:
            if mode == "manual":
                raise ValueError("manual layout requires sizes")
            return

        normalized = tuple(int(size) for size in sizes)
        if len(normalized) != self.n_sections:
            raise ValueError("layout size count must match n_sections")
        if any(size <= 0 for size in normalized):
            raise ValueError("layout sizes must be positive")
        object.__setattr__(self,"sizes",normalized)


@dataclass(frozen=True)
class SectionLayoutSignature:
    """Canonical split-layout identity independent from runtime objects."""

    axis: str
    sizes: tuple[int, ...]
    section_keys: tuple[str, ...]


def split_slm_geometry(
    geometry: SLMGeometry,
    n_sections: int,
    axis: str="x",
    key_prefix: str="sec_",
) -> dict[str, SectionGeometry]:
    """Split an SLM into contiguous sections covering its complete geometry."""

    if isinstance(n_sections,bool) or not isinstance(n_sections,int):
        raise TypeError("n_sections must be an integer")
    if n_sections <= 0:
        raise ValueError("n_sections must be > 0")
    if not key_prefix:
        raise ValueError("key_prefix cannot be empty")

    axis = str(axis).strip().lower()
    if axis not in ("x","y"):
        raise ValueError("axis must be either 'x' or 'y'")

    total_size = geometry.width if axis == "x" else geometry.height
    if n_sections > total_size:
        raise ValueError(
            f"Cannot split {total_size} pixels into {n_sections} sections"
        )

    base_size,remainder = divmod(total_size,n_sections)
    sections = {}
    offset = 0

    for index in range(n_sections):
        section_size = base_size + (1 if index < remainder else 0)
        key = f"{key_prefix}{index}"

        if axis == "x":
            section = SectionGeometry(
                key=key,x=offset,y=0,
                width=section_size,height=geometry.height,
            )
        else:
            section = SectionGeometry(
                key=key,x=0,y=offset,
                width=geometry.width,height=section_size,
            )

        sections[key] = section
        offset += section_size

    return sections


def create_split_section_geometries(
    geometry: SLMGeometry,
    layout: SectionSplitLayout,
) -> dict[str, SectionGeometry]:
    """Create contiguous split sections covering the supplied SLM geometry."""

    if not isinstance(layout,SectionSplitLayout):
        raise TypeError("layout must be a SectionSplitLayout")

    if layout.mode == "even":
        sections = split_slm_geometry(
            geometry,
            layout.n_sections,
            axis=layout.axis,
            key_prefix=layout.key_prefix,
        )
    else:
        sections = _manual_split_slm_geometry(geometry,layout)

    validate_split_section_geometries(
        geometry,sections,n_sections=layout.n_sections,axis=layout.axis,
    )
    return sections


def split_layout_signature(
    geometry: SLMGeometry,
    section_geometries: Mapping[str,SectionGeometry],
    *,
    n_sections: int | None=None,
    axis: str | None=None,
) -> SectionLayoutSignature:
    """Validate and return the canonical identity of a fixed split layout."""

    return validate_split_section_geometries(
        geometry,section_geometries,n_sections=n_sections,axis=axis,
    )


def validate_split_section_geometries(
    geometry: SLMGeometry,
    section_geometries: Mapping[str,SectionGeometry],
    *,
    n_sections: int | None=None,
    axis: str | None=None,
) -> SectionLayoutSignature:
    """Validate a fixed-count X/Y split with exact full-SLM coverage."""

    sections = dict(section_geometries)
    if not sections:
        raise ValueError("Section layout must contain at least one section")

    if n_sections is not None:
        if isinstance(n_sections,bool) or not isinstance(n_sections,int):
            raise TypeError("n_sections must be an integer")
    if n_sections is not None and len(sections) != n_sections:
        raise ValueError(
            f"Section layout has {len(sections)} section(s); "
            f"expected {n_sections}"
        )

    axis = None if axis is None else str(axis).strip().lower()
    if axis is not None and axis not in ("x","y"):
        raise ValueError("axis must be either 'x' or 'y'")

    for key,section in sections.items():
        if key != section.key:
            raise ValueError(
                f"Section mapping key '{key}' does not match "
                f"geometry key '{section.key}'"
            )
        if (
            section.x + section.width > geometry.width
            or section.y + section.height > geometry.height
        ):
            raise ValueError(f"Section '{key}' exceeds the SLM geometry")

    valid_axes = []
    candidate_axes = (axis,) if axis is not None else ("x","y")
    for candidate_axis in candidate_axes:
        try:
            valid_axes.append(
                _split_axis_signature(geometry,sections,candidate_axis)
            )
        except ValueError:
            if axis is not None:
                raise

    if not valid_axes:
        raise ValueError(
            "Section layout must be contiguous, non-overlapping, and cover "
            "the full physical SLM along X or Y"
        )

    return valid_axes[0]


def validate_config_section_layout(
    *,
    physical_geometry: SLMGeometry,
    config_geometry: SLMGeometry,
    config_section_geometries: Mapping[str,SectionGeometry],
    setup_section_geometries: Mapping[str,SectionGeometry],
    section_layout_customizable: bool,
) -> SectionLayoutSignature:
    """Validate setup-vs-config authority for one complete SLM config."""

    if config_geometry != physical_geometry:
        raise ValueError("SLM config geometry does not match the physical SLM")

    setup_signature = validate_split_section_geometries(
        physical_geometry,setup_section_geometries,
    )
    config_signature = validate_split_section_geometries(
        physical_geometry,
        config_section_geometries,
        n_sections=len(setup_section_geometries),
    )

    if (
        not bool(section_layout_customizable)
        and dict(config_section_geometries) != dict(setup_section_geometries)
    ):
        raise ValueError(
            "SLM config section layout does not match the setup-defined layout"
        )

    del setup_signature
    return config_signature


def _manual_split_slm_geometry(
    geometry: SLMGeometry,
    layout: SectionSplitLayout,
) -> dict[str, SectionGeometry]:
    sizes = tuple(layout.sizes or ())
    total_size = geometry.width if layout.axis == "x" else geometry.height
    if sum(sizes) != total_size:
        raise ValueError(
            f"Manual {layout.axis.upper()} section sizes sum to {sum(sizes)}; "
            f"expected {total_size}"
        )

    sections = {}
    offset = 0
    for index,section_size in enumerate(sizes):
        key = f"{layout.key_prefix}{index}"
        if layout.axis == "x":
            section = SectionGeometry(
                key=key,x=offset,y=0,
                width=section_size,height=geometry.height,
            )
        else:
            section = SectionGeometry(
                key=key,x=0,y=offset,
                width=geometry.width,height=section_size,
            )
        sections[key] = section
        offset += section_size
    return sections


def _split_axis_signature(
    geometry: SLMGeometry,
    sections: Mapping[str,SectionGeometry],
    axis: str,
) -> SectionLayoutSignature:
    ordered = sorted(
        sections.items(),
        key=lambda item:item[1].x if axis == "x" else item[1].y,
    )
    offset = 0
    sizes = []
    keys = []
    total_size = geometry.width if axis == "x" else geometry.height

    for key,section in ordered:
        if axis == "x":
            if section.y != 0 or section.height != geometry.height:
                raise ValueError("X split sections must span the full height")
            if section.x != offset:
                raise ValueError("X split sections must be contiguous")
            section_size = section.width
        else:
            if section.x != 0 or section.width != geometry.width:
                raise ValueError("Y split sections must span the full width")
            if section.y != offset:
                raise ValueError("Y split sections must be contiguous")
            section_size = section.height

        sizes.append(section_size)
        keys.append(key)
        offset += section_size

    if offset != total_size:
        raise ValueError(
            f"{axis.upper()} split sections cover {offset} pixels; "
            f"expected {total_size}"
        )

    return SectionLayoutSignature(
        axis=axis,sizes=tuple(sizes),section_keys=tuple(keys),
    )
