from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
from typing import TYPE_CHECKING
import json
import logging
import re

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from ..engine.section.geometry import SectionGeometry
    from ..engine.device import SLMIdentity
    from ..engine.section.components import OpticsState,CorrectionsState


_logger = logging.getLogger(__name__)


@dataclass
class SLMCorrectionStore:
    identity: SLMIdentity
    directory: Path
    wavelength_table_file: str | None = None

    _image_cache: dict[Path, np.ndarray] = field(
        default_factory=dict,init=False,repr=False)
    _pattern_files: dict[int, Path] | None = field(
        default=None,init=False,repr=False)
    _section_pattern_cache: dict[tuple[int, SectionGeometry], np.ndarray] = field(
        default_factory=dict,init=False,repr=False)
    _twopi_values: dict[int, tuple[int, str]] | None = field(
        default=None,init=False,repr=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)


    def get_pattern(
        self,
        wavelength_nm: int,
        geometry: SectionGeometry,
    ) -> np.ndarray | None:
        cache_key = int(wavelength_nm),geometry
        cached = self._section_pattern_cache.get(cache_key)
        if cached is not None:
            return cached

        files = self._find_pattern_files()

        if not files:
            _logger.warning(
                "No correction patterns found for SLM %s in %s",
                self.identity.serial_number,self.directory,
            )
            return None

        selected_wavelength = self._closest(wavelength_nm,files)
        path = files[selected_wavelength]

        if selected_wavelength != wavelength_nm:
            _logger.warning(
                "No correction pattern at %snm for SLM %s; using %snm",
                wavelength_nm,self.identity.serial_number,selected_wavelength,
            )

        if path not in self._image_cache:
            with Image.open(path) as source:
                image = np.array(source)

            if image.ndim != 2:
                raise ValueError(
                    f"Correction pattern must be 2D, got shape {image.shape}"
                )

            if np.any(image < 0) or np.any(image > 255):
                raise ValueError( "Correction pattern values must be in [0,255]")

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
        return pattern

    def invalidate_cache(self) -> None:
        """Explicitly invalidate correction files and decoded array caches."""
        self._pattern_files = None
        self._image_cache.clear()
        self._section_pattern_cache.clear()
        self._twopi_values = None

    def get_twopi_value(self,wavelength_nm: int) -> int:
        values = self._load_twopi_values()

        if not values:
            _logger.warning(
                "No 2pi values found for SLM %s; using 255",
                self.identity.serial_number,
            )
            return 255

        selected_wavelength = self._closest(wavelength_nm,values)
        value,source = values[selected_wavelength]
        if not 1 <= value <= 255:
            raise ValueError( f"2pi value must be in [1,255], got {value}")
        if selected_wavelength != wavelength_nm:
            _logger.warning(
                "No 2pi value at %snm for SLM %s; using %snm",
                wavelength_nm,self.identity.serial_number,selected_wavelength,
            )

        if source != "measurement":
            _logger.warning(
                "Using %s 2pi value for SLM %s at %snm",
                source,self.identity.serial_number,selected_wavelength,
            )

        return value

    def _find_pattern_files(self) -> dict[int, Path]:
        if self._pattern_files is not None:
            return self._pattern_files

        serial = re.escape(self.identity.serial_number)
        expression = re.compile(
            rf"^CAL_{serial}_(\d+)nm\.bmp$",re.IGNORECASE)

        files = {}

        if not self.directory.is_dir():
            self._pattern_files = files
            return files

        for path in self.directory.iterdir():
            match = expression.match(path.name)
            if match:
                files[int(match.group(1))] = path

        self._pattern_files = files
        return files

    def _load_twopi_values(self) -> dict[int, tuple[int, str]]:
        if self._twopi_values is not None:
            return self._twopi_values

        self._twopi_values = {}

        if not self.wavelength_table_file:
            return self._twopi_values

        if self.wavelength_table_file.lower() == "none":
            return self._twopi_values

        path = self.directory/self.wavelength_table_file

        if not path.is_file():
            _logger.warning("2pi wavelength table not found: %s",path)
            return self._twopi_values

        with path.open("r",encoding="utf-8") as file:
            data = json.load(file)

        # Legacy root-level values.
        self._add_twopi_entries(data,"legacy")

        # Manufacturer values override legacy values.
        self._add_twopi_entries(
            data.get("manufacturer",{}),"manufacturer")

        # Measurements have highest priority at a given wavelength.
        self._add_twopi_entries(
            data.get("measurement",{}),"measurement")

        return self._twopi_values

    def _add_twopi_entries(self,data: dict,source: str) -> None:
        if not isinstance(data,dict):
            return

        for key,value in data.items():
            match = re.match(r"^(\d+)nm$",str(key))
            if match:
                self._twopi_values[int(match.group(1))] = (
                    int(value),source)

    @staticmethod
    def _closest(wavelength_nm: int,values: dict) -> int:
        return min(
            values,
            key=lambda value: (
                abs(value-wavelength_nm),value,
            ),
        )