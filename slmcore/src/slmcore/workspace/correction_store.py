from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import json
import logging
import re

import numpy as np
from PIL import Image

from ..core.engine.corrections import CorrectionProvider,ResolvedCorrections
from ..core.engine.device import SLMIdentity
from ..core.engine.section.geometry import SectionGeometry


_logger = logging.getLogger(__name__)


@dataclass
class SLMCorrectionStore(CorrectionProvider):
    """Filesystem-backed correction provider for one physical SLM."""

    identity: SLMIdentity
    directory: Path
    wavelength_table_file: str | None = None

    _image_cache: dict[Path,np.ndarray] = field(
        default_factory=dict,init=False,repr=False,
    )
    _pattern_files: dict[int,Path] | None = field(
        default=None,init=False,repr=False,
    )
    _section_pattern_cache: dict[tuple[int,SectionGeometry],np.ndarray] = field(
        default_factory=dict,init=False,repr=False,
    )
    _twopi_values: dict[int,tuple[int,str]] | None = field(
        default=None,init=False,repr=False,
    )
    _warned: set[tuple] = field(
        default_factory=set,init=False,repr=False,
    )

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    def resolve(
        self,wavelength_nm: int,geometry: SectionGeometry,
    ) -> ResolvedCorrections:
        wavelength_nm = int(wavelength_nm)
        pattern,pattern_path,pattern_wavelength = self._resolve_pattern(
            wavelength_nm,geometry,
        )
        (
            two_pi_value,twopi_wavelength,twopi_source,twopi_path,
        ) = self._resolve_twopi(wavelength_nm)
        return ResolvedCorrections(
            wavelength_nm=wavelength_nm,
            geometry=geometry,
            correction_pattern=pattern,
            two_pi_value=two_pi_value,
            source_directory=str(self.directory.resolve()),
            pattern_filename=(None if pattern_path is None else pattern_path.name),
            pattern_wavelength_nm=pattern_wavelength,
            twopi_filename=(None if twopi_path is None else twopi_path.name),
            twopi_wavelength_nm=twopi_wavelength,
            twopi_source=twopi_source,
        )

    def invalidate_cache(self) -> None:
        """Explicitly invalidate correction files and decoded array caches."""
        self._pattern_files = None
        self._image_cache.clear()
        self._section_pattern_cache.clear()
        self._twopi_values = None
        self._warned.clear()

    def _resolve_pattern(
        self,wavelength_nm: int,geometry: SectionGeometry,
    ) -> tuple[np.ndarray | None,Path | None,int | None]:
        files = self._find_pattern_files()
        if not files:
            self._warn_once(
                ("no_patterns",),
                "No correction patterns found for SLM %s in %s",
                self.identity.serial_number,self.directory,
            )
            return None,None,None

        selected_wavelength = self._closest(wavelength_nm,files)
        path = files[selected_wavelength]
        if selected_wavelength != wavelength_nm:
            self._warn_once(
                ("pattern_fallback",wavelength_nm,selected_wavelength),
                "No correction pattern at %snm for SLM %s; using %snm",
                wavelength_nm,self.identity.serial_number,selected_wavelength,
            )

        cache_key = selected_wavelength,geometry
        cached = self._section_pattern_cache.get(cache_key)
        if cached is not None:
            return cached,path,selected_wavelength

        if path not in self._image_cache:
            with Image.open(path) as source:
                image = np.array(source)
            if image.ndim != 2:
                raise ValueError(
                    f"Correction pattern must be 2D, got shape {image.shape}"
                )
            if np.any(image < 0) or np.any(image > 255):
                raise ValueError("Correction pattern values must be in [0,255]")
            image = np.array(image,copy=True)
            image.setflags(write=False)
            self._image_cache[path] = image

        pattern = self._image_cache[path][geometry.slices]
        if pattern.shape != geometry.shape:
            raise ValueError(
                f"Correction pattern section has shape {pattern.shape}; "
                f"expected {geometry.shape}"
            )
        pattern = np.array(pattern,dtype=np.float64,copy=True)
        pattern.setflags(write=False)
        self._section_pattern_cache[cache_key] = pattern
        return pattern,path,selected_wavelength

    def _resolve_twopi(
        self,wavelength_nm: int,
    ) -> tuple[int,int | None,str,Path | None]:
        values = self._load_twopi_values()
        table_path = self._wavelength_table_path()
        if not values:
            self._warn_once(
                ("no_twopi_values",),
                "No 2pi values found for SLM %s; using 255",
                self.identity.serial_number,
            )
            return 255,None,"default",None

        selected_wavelength = self._closest(wavelength_nm,values)
        value,source = values[selected_wavelength]
        if not 1 <= value <= 255:
            raise ValueError(f"2pi value must be in [1,255], got {value}")
        if selected_wavelength != wavelength_nm:
            self._warn_once(
                ("twopi_fallback",wavelength_nm,selected_wavelength),
                "No 2pi value at %snm for SLM %s; using %snm",
                wavelength_nm,self.identity.serial_number,selected_wavelength,
            )
        if source != "measurement":
            self._warn_once(
                ("twopi_source",source,selected_wavelength),
                "Using %s 2pi value for SLM %s at %snm",
                source,self.identity.serial_number,selected_wavelength,
            )
        return value,selected_wavelength,source,table_path

    def _find_pattern_files(self) -> dict[int,Path]:
        if self._pattern_files is not None:
            return self._pattern_files
        serial = re.escape(self.identity.serial_number)
        expression = re.compile(
            rf"^CAL_{serial}_(\d+)nm\.bmp$",re.IGNORECASE,
        )
        files: dict[int,Path] = {}
        if self.directory.is_dir():
            for path in self.directory.iterdir():
                match = expression.match(path.name)
                if match:
                    files[int(match.group(1))] = path
        self._pattern_files = files
        return files

    def _wavelength_table_path(self) -> Path | None:
        name = str(self.wavelength_table_file or "").strip()
        if not name or name.lower() == "none":
            return None
        path = self.directory/name
        return path if path.is_file() else None

    def _load_twopi_values(self) -> dict[int,tuple[int,str]]:
        if self._twopi_values is not None:
            return self._twopi_values
        self._twopi_values = {}
        path = self._wavelength_table_path()
        if path is None:
            if self.wavelength_table_file and str(self.wavelength_table_file).lower() != "none":
                self._warn_once(
                    ("missing_twopi_table",str(self.wavelength_table_file)),
                    "2pi wavelength table not found: %s",
                    self.directory/self.wavelength_table_file,
                )
            return self._twopi_values
        with path.open("r",encoding="utf-8") as file:
            data = json.load(file)
        self._add_twopi_entries(data,"legacy")
        self._add_twopi_entries(data.get("manufacturer",{}),"manufacturer")
        self._add_twopi_entries(data.get("measurement",{}),"measurement")
        return self._twopi_values

    def _warn_once(self,key: tuple,message: str,*args) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        _logger.warning(message,*args)

    def _add_twopi_entries(self,data: dict,source: str) -> None:
        if not isinstance(data,dict):
            return
        for key,value in data.items():
            match = re.match(r"^(\d+)nm$",str(key))
            if match:
                self._twopi_values[int(match.group(1))] = (int(value),source)

    @staticmethod
    def _closest(wavelength_nm: int,values: dict) -> int:
        return min(values,key=lambda value:(abs(value-wavelength_nm),value))


__all__ = ["SLMCorrectionStore"]
