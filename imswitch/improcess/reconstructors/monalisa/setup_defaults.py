"""Detector-scoped MoNaLISA setup defaults, independent of the session registry.

Only explicit user actions write the currently selected ImControl setup JSON.
Reconstruction may use recordings without importing ImControl at module import time.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from dataclasses import dataclass

from .scan_frame import ORIENTATIONS
from .footprint_config import FitFootprint

FWHM_KEY = "monalisaSpotFwhmNm"
ORIENTATION_KEY = "monalisaScanOrientation"
FOOTPRINT_MODE_KEY = "monalisaLocalFootprintMode"
FOOTPRINT_SHAPE_KEY = "monalisaLocalFootprintShape"
FOOTPRINT_SIZE_KEY = "monalisaLocalFootprintSizePx"
SHARED_FOOTPRINT_MODE_KEY = "monalisaFitFootprintMode"
SHARED_FOOTPRINT_SHAPE_KEY = "monalisaFitFootprintShape"
SHARED_FOOTPRINT_SIZE_KEY = "monalisaFitFootprintSizePx"
SHARED_FOOTPRINT_SIGMA_KEY = "monalisaFitFootprintSigmaFactor"
ESTIMATOR_KEY = "monalisaFitEstimator"


def validate_footprint(mode, shape, size) -> tuple[str, str, int] | None:
    if mode not in ("fixed", "from_lattice") or shape not in ("square", "circle"):
        return None
    try:
        number = int(size)
    except (ValueError, TypeError, OverflowError):
        return None
    if isinstance(size, bool) or number != size or number < 3 or number > 255 or number % 2 != 1:
        return None
    return mode, shape, number


@dataclass(frozen=True)
class DetectorSetupDefaults:
    detector: str
    fwhm_nm: float | None = None
    orientation: str | None = None
    footprint_mode: str | None = None
    footprint_shape: str | None = None
    footprint_size: float | None = None
    footprint_sigma_factor: float = 2.5
    estimator: str | None = None


def positive_fwhm(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def valid_orientation(value) -> str | None:
    value = str(value or "")
    return value if value in ORIENTATIONS else None


def current_setup_path() -> Path | None:
    """Use ImControl's active setup selection, never a guessed setup file."""
    try:
        from imswitch.imcontrol.model import configfiletools
        options, _ = configfiletools.loadOptions()
        name = str(getattr(options, "setupFileName", "") or "")
        if not name or Path(name).name != name:
            return None
        return Path(configfiletools.getSetupFilePath(name))
    except (ImportError, OSError, AttributeError):
        return None


def _read_setup(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("ImControl setup is not a JSON object")
    return document


def _detector_properties(document: dict, detector: str, *, create: bool) -> dict | None:
    detectors = document.get("detectors")
    if not isinstance(detectors, dict) or detector not in detectors:
        return None
    entry = detectors[detector]
    if not isinstance(entry, dict):
        return None
    props = entry.get("managerProperties")
    if props is None and create:
        props = {}
        entry["managerProperties"] = props
    return props if isinstance(props, dict) else None


def read_detector_defaults(detector: str, *, path: Path | None = None) -> DetectorSetupDefaults:
    detector = str(detector or "")
    setup_path = path if path is not None else current_setup_path()
    if not detector or detector == "unknown" or setup_path is None:
        return DetectorSetupDefaults(detector)
    try:
        props = _detector_properties(_read_setup(Path(setup_path)), detector, create=False)
    except (OSError, ValueError, json.JSONDecodeError):
        props = None
    footprint = None
    if props and SHARED_FOOTPRINT_SHAPE_KEY in props:
        try:
            footprint = FitFootprint(
                shape=str(props[SHARED_FOOTPRINT_SHAPE_KEY]),
                mode=str(props.get(SHARED_FOOTPRINT_MODE_KEY, "from_lattice")),
                size_px=float(props.get(SHARED_FOOTPRINT_SIZE_KEY, 9)),
                sigma_factor=float(props.get(SHARED_FOOTPRINT_SIGMA_KEY, 2.5)),
            ).validate()
        except (ValueError, TypeError):
            pass
    if footprint is None and props:
        old = validate_footprint(
            props.get(FOOTPRINT_MODE_KEY), props.get(FOOTPRINT_SHAPE_KEY),
            props.get(FOOTPRINT_SIZE_KEY),
        )
        if old is not None:
            footprint = FitFootprint(old[1], old[0], float(old[2]))
    return DetectorSetupDefaults(
        detector=detector,
        fwhm_nm=positive_fwhm(props.get(FWHM_KEY)) if props else None,
        orientation=valid_orientation(props.get(ORIENTATION_KEY)) if props else None,
        footprint_mode=footprint.mode if footprint else None,
        footprint_shape=footprint.shape if footprint else None,
        footprint_size=footprint.size_px if footprint else None,
        footprint_sigma_factor=footprint.sigma_factor if footprint else 2.5,
        estimator=(props.get(ESTIMATOR_KEY) if props and props.get(ESTIMATOR_KEY) in ("local", "joint") else None),
    )


def save_detector_footprint(
    detector: str, mode: str, shape: str, size: float, *,
    sigma_factor: float = 2.5, path: Path | None = None,
) -> Path:
    """Save one estimator-independent footprint preference atomically.

    The older Local-only setup keys remain readable for compatibility, but
    new saves use shared property keys.  Nothing changes until Save is clicked.
    """
    try:
        fit = FitFootprint(shape, mode, float(size), float(sigma_factor)).validate()
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid fitting footprint preference") from exc
    return _save_detector_properties(detector, {
        SHARED_FOOTPRINT_SHAPE_KEY: fit.shape,
        SHARED_FOOTPRINT_MODE_KEY: fit.mode,
        SHARED_FOOTPRINT_SIZE_KEY: fit.size_px,
        SHARED_FOOTPRINT_SIGMA_KEY: fit.sigma_factor,
    }, path=path)



def save_detector_fitting(
    detector: str, estimator: str, mode: str, shape: str, size: float, *,
    sigma_factor: float = 2.5, path: Path | None = None,
) -> Path:
    """Atomically save estimator and footprint as one detector fitting default."""
    if estimator not in ("local", "joint"):
        raise ValueError(f"Invalid Gaussian estimator: {estimator!r}")
    try:
        fit = FitFootprint(shape, mode, float(size), float(sigma_factor)).validate()
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid fitting footprint preference") from exc
    return _save_detector_properties(detector, {
        ESTIMATOR_KEY: estimator,
        SHARED_FOOTPRINT_SHAPE_KEY: fit.shape,
        SHARED_FOOTPRINT_MODE_KEY: fit.mode,
        SHARED_FOOTPRINT_SIZE_KEY: fit.size_px,
        SHARED_FOOTPRINT_SIGMA_KEY: fit.sigma_factor,
    }, path=path)


def save_detector_default(detector: str, key: str, value, *, path: Path | None = None) -> Path:
    """Atomically change exactly one property of a known detector in a setup.

    Unknown detectors/setups fail closed instead of silently creating hardware
    entries. A recorded data source never selects a different setup file.
    """
    if key not in (FWHM_KEY, ORIENTATION_KEY):
        raise ValueError(f"Unsupported MoNaLISA setup key: {key!r}")
    if key == FWHM_KEY:
        validated = positive_fwhm(value)
    else:
        validated = valid_orientation(value)
    if validated is None:
        raise ValueError(f"Invalid {key} value: {value!r}")
    return _save_detector_properties(detector, {key: validated}, path=path)


def _save_detector_properties(detector: str, values: dict, *, path: Path | None = None) -> Path:
    if not detector or detector == "unknown":
        raise ValueError("Select data with an identified detector before saving a setup default")
    setup_path = Path(path) if path is not None else current_setup_path()
    if setup_path is None or not setup_path.is_file():
        raise ValueError("No selected ImControl hardware setup is available")
    document = _read_setup(setup_path)
    props = _detector_properties(document, detector, create=True)
    if props is None:
        raise ValueError(f"Detector {detector!r} is not defined in the selected setup")
    props.update(values)
    # Save atomically; do not serialize the SetupInfo dataclass, which can
    # reformat or drop unknown fields from carefully maintained setups.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=setup_path.parent,
            prefix=f".{setup_path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(document, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, setup_path.stat().st_mode)
        os.replace(temp_path, setup_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return setup_path
