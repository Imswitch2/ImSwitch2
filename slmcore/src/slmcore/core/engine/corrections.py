from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol,runtime_checkable

import numpy as np

from .section.geometry import SectionGeometry


@dataclass(frozen=True)
class ResolvedCorrections:
    """Complete correction resources resolved for one wavelength/section.

    Numerical values are portable and may be persisted in an SLM config.
    Source paths and filenames are provenance only; runtimes never follow them
    when reconstructing a configuration.
    """

    wavelength_nm: int
    geometry: SectionGeometry
    correction_pattern: np.ndarray | None
    two_pi_value: int
    source_directory: str | None = None
    pattern_filename: str | None = None
    pattern_wavelength_nm: int | None = None
    twopi_filename: str | None = None
    twopi_wavelength_nm: int | None = None
    twopi_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self,"wavelength_nm",int(self.wavelength_nm))
        object.__setattr__(self,"two_pi_value",int(self.two_pi_value))
        if not 1 <= self.two_pi_value <= 255:
            raise ValueError("two_pi_value must be in [1,255]")
        pattern = self.correction_pattern
        if pattern is not None:
            pattern = np.asarray(pattern,dtype=np.float64)
            if pattern.shape != self.geometry.shape:
                raise ValueError(
                    "Correction pattern has shape %s; expected %s"
                    % (pattern.shape,self.geometry.shape)
                )
            pattern = np.array(pattern,dtype=np.float64,copy=True)
            pattern.setflags(write=False)
            object.__setattr__(self,"correction_pattern",pattern)
        for name in ("pattern_wavelength_nm","twopi_wavelength_nm"):
            value = getattr(self,name)
            if value is not None:
                object.__setattr__(self,name,int(value))
        for name in (
            "source_directory","pattern_filename","twopi_filename","twopi_source",
        ):
            value = getattr(self,name)
            if value is not None:
                text = str(value).strip()
                object.__setattr__(self,name,text or None)

    @classmethod
    def defaults(
        cls,wavelength_nm: int,geometry: SectionGeometry,
    ) -> "ResolvedCorrections":
        return cls(
            wavelength_nm=int(wavelength_nm),
            geometry=geometry,
            correction_pattern=None,
            two_pi_value=255,
            twopi_source="default",
        )

    def to_dict(self) -> dict:
        pattern = self.correction_pattern
        return {
            "wavelength_nm":self.wavelength_nm,
            "geometry":{
                "key":self.geometry.key,"x":self.geometry.x,"y":self.geometry.y,
                "width":self.geometry.width,"height":self.geometry.height,
            },
            "correction_pattern":(
                None if pattern is None else np.array(pattern,copy=True)
            ),
            "two_pi_value":self.two_pi_value,
            "source_directory":self.source_directory,
            "pattern_filename":self.pattern_filename,
            "pattern_wavelength_nm":self.pattern_wavelength_nm,
            "twopi_filename":self.twopi_filename,
            "twopi_wavelength_nm":self.twopi_wavelength_nm,
            "twopi_source":self.twopi_source,
        }

    @classmethod
    def from_dict(cls,data) -> "ResolvedCorrections":
        if not isinstance(data,dict):
            raise ValueError("correction_snapshot must be a dictionary")
        return cls(
            wavelength_nm=int(data["wavelength_nm"]),
            geometry=SectionGeometry(**dict(data["geometry"])),
            correction_pattern=data.get("correction_pattern"),
            two_pi_value=int(data["two_pi_value"]),
            source_directory=data.get("source_directory"),
            pattern_filename=data.get("pattern_filename"),
            pattern_wavelength_nm=data.get("pattern_wavelength_nm"),
            twopi_filename=data.get("twopi_filename"),
            twopi_wavelength_nm=data.get("twopi_wavelength_nm"),
            twopi_source=data.get("twopi_source"),
        )

    def numerically_equal(
        self,
        other: "ResolvedCorrections",
        *,
        compare_pattern: bool=True,
        compare_twopi: bool=True,
    ) -> bool:
        if not isinstance(other,ResolvedCorrections):
            return False
        if compare_twopi and self.two_pi_value != other.two_pi_value:
            return False
        if compare_pattern:
            first,second = self.correction_pattern,other.correction_pattern
            if first is None or second is None:
                if first is not None or second is not None:
                    return False
            elif not np.array_equal(first,second):
                return False
        return True


@runtime_checkable
class CorrectionProvider(Protocol):
    """Resolve correction resources available in the current environment."""

    def resolve(
        self,wavelength_nm: int,geometry: SectionGeometry,
    ) -> ResolvedCorrections:
        ...


class CorrectionSourceInvalidatedError(RuntimeError):
    """Raised when a pinned saved snapshot no longer matches runtime context."""


__all__ = [
    "CorrectionProvider",
    "CorrectionSourceInvalidatedError",
    "ResolvedCorrections",
]
