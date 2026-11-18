"""ImProcess-owned, detector-scoped MoNaLISA calibration defaults.

Only an explicit Save action writes to the selected setup's
``processing.monalisa.detectorDefaults``. Legacy detector ``managerProperties``
remain *read-only* fallback for existing microscope setups; neither ImControl
nor raw acquisition owns or snapshots these reconstruction preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile

from .footprint_config import FitFootprint
from .scan_frame import ORIENTATIONS

# Legacy keys remain public for existing callers and read-only migration.
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
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def valid_orientation(value) -> str | None:
    return value if isinstance(value, str) and value in ORIENTATIONS else None


def validate_footprint(mode, shape, size) -> tuple[str, str, int] | None:
    """Read the pre-shared, odd-integer Local footprint format."""
    if mode not in ("fixed", "from_lattice") or shape not in ("square", "circle"):
        return None
    try:
        number = int(size)
    except (ValueError, TypeError, OverflowError):
        return None
    if isinstance(size, bool) or number != size or number < 3 or number > 255 or number % 2 != 1:
        return None
    return mode, shape, number


def current_setup_path() -> Path | None:
    """Use ImProcess's existing, lazy setup-selection adapter."""
    from imswitch.improcess.model.processing_config import selected_setup_path
    return selected_setup_path()


def _read_setup(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("Setup is not a JSON object")
    return document


def _known_detector(document: dict, detector: str) -> bool:
    detectors = document.get("detectors")
    return (isinstance(detectors, dict) and detector in detectors
            and isinstance(detectors[detector], dict))


def _legacy_properties(document: dict, detector: str) -> dict:
    if not _known_detector(document, detector):
        return {}
    props = document["detectors"][detector].get("managerProperties")
    return props if isinstance(props, dict) else {}


def _processing_defaults(document: dict, detector: str) -> dict:
    """Return a detector's processing-owned values; never create sections on read."""
    processing = document.get("processing")
    monalisa = processing.get("monalisa") if isinstance(processing, dict) else None
    defaults = monalisa.get("detectorDefaults") if isinstance(monalisa, dict) else None
    entry = defaults.get(detector) if isinstance(defaults, dict) else None
    return entry if isinstance(entry, dict) else {}


def _valid_fit(value) -> FitFootprint | None:
    if not isinstance(value, dict):
        return None
    try:
        return FitFootprint(
            shape=value["shape"],
            mode=value.get("mode", "from_lattice"),
            size_px=float(value.get("sizePx", 9)),
            sigma_factor=float(value.get("sigmaFactor", 2.5)),
        ).validate()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _legacy_fit(props: dict) -> FitFootprint | None:
    if SHARED_FOOTPRINT_SHAPE_KEY in props:
        result = _valid_fit({
            "shape": props[SHARED_FOOTPRINT_SHAPE_KEY],
            "mode": props.get(SHARED_FOOTPRINT_MODE_KEY, "from_lattice"),
            "sizePx": props.get(SHARED_FOOTPRINT_SIZE_KEY, 9),
            "sigmaFactor": props.get(SHARED_FOOTPRINT_SIGMA_KEY, 2.5),
        })
        if result is not None:
            return result
    old = validate_footprint(
        props.get(FOOTPRINT_MODE_KEY), props.get(FOOTPRINT_SHAPE_KEY),
        props.get(FOOTPRINT_SIZE_KEY),
    )
    return FitFootprint(old[1], old[0], float(old[2])) if old else None


def _fit_mapping(fit: FitFootprint) -> dict:
    return {
        "shape": fit.shape, "mode": fit.mode,
        "sizePx": fit.size_px, "sigmaFactor": fit.sigma_factor,
    }


def _read_from_document(document: dict, detector: str) -> DetectorSetupDefaults:
    if not _known_detector(document, detector):
        return DetectorSetupDefaults(detector)
    prefs = _processing_defaults(document, detector)
    legacy = _legacy_properties(document, detector)
    fit = _valid_fit(prefs.get("footprint")) or _legacy_fit(legacy)
    fwhm = positive_fwhm(prefs.get("spotFwhmNm"))
    if fwhm is None:
        fwhm = positive_fwhm(legacy.get(FWHM_KEY))
    orientation = valid_orientation(prefs.get("scanOrientation"))
    if orientation is None:
        orientation = valid_orientation(legacy.get(ORIENTATION_KEY))
    estimator = prefs.get("estimator")
    if estimator not in ("local", "joint"):
        estimator = legacy.get(ESTIMATOR_KEY)
    return DetectorSetupDefaults(
        detector=detector, fwhm_nm=fwhm, orientation=orientation,
        footprint_mode=fit.mode if fit else None,
        footprint_shape=fit.shape if fit else None,
        footprint_size=fit.size_px if fit else None,
        footprint_sigma_factor=fit.sigma_factor if fit else 2.5,
        estimator=estimator if estimator in ("local", "joint") else None,
    )


def startup_detector_id(*, path: Path | None = None) -> str:
    """Resolve a single detector, or a unique detector with a calibration.

    Never infer a detector when two or more are calibrated; a recording's
    detector identity takes precedence once data are selected.
    """
    setup_path = path if path is not None else current_setup_path()
    if setup_path is None:
        return "unknown"
    try:
        document = _read_setup(Path(setup_path))
        detectors = document.get("detectors")
        if not isinstance(detectors, dict):
            return "unknown"
        candidates = [name for name in detectors
                      if isinstance(name, str) and name and _known_detector(document, name)]
        configured = []
        for name in candidates:
            saved = _read_from_document(document, name)
            if (saved.fwhm_nm is not None or saved.orientation is not None
                    or saved.estimator is not None or saved.footprint_mode is not None):
                configured.append(name)
        if len(configured) == 1:
            return configured[0]
        if not configured and len(candidates) == 1:
            return candidates[0]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return "unknown"


def read_detector_defaults(detector: str, *, path: Path | None = None) -> DetectorSetupDefaults:
    detector = str(detector or "")
    setup_path = path if path is not None else current_setup_path()
    if not detector or detector == "unknown" or setup_path is None:
        return DetectorSetupDefaults(detector)
    try:
        return _read_from_document(_read_setup(Path(setup_path)), detector)
    except (OSError, ValueError, json.JSONDecodeError):
        return DetectorSetupDefaults(detector)


def save_detector_footprint(
    detector: str, mode: str, shape: str, size: float, *,
    sigma_factor: float = 2.5, path: Path | None = None,
) -> Path:
    """Save estimator-independent footprint in ImProcess setup preferences."""
    try:
        fit = FitFootprint(shape, mode, float(size), float(sigma_factor)).validate()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid fitting footprint preference") from exc
    return _save_processing_preferences(detector, {"footprint": _fit_mapping(fit)}, path=path)


def save_detector_fitting(
    detector: str, estimator: str, mode: str, shape: str, size: float, *,
    sigma_factor: float = 2.5, path: Path | None = None,
) -> Path:
    """Save the Gaussian estimator and footprint as one atomic preference."""
    if estimator not in ("local", "joint"):
        raise ValueError(f"Invalid Gaussian estimator: {estimator!r}")
    try:
        fit = FitFootprint(shape, mode, float(size), float(sigma_factor)).validate()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid fitting footprint preference") from exc
    return _save_processing_preferences(
        detector, {"estimator": estimator, "footprint": _fit_mapping(fit)}, path=path,
    )


def save_detector_default(detector: str, key: str, value, *, path: Path | None = None) -> Path:
    """Save one validated processing preference; legacy key names for UI compatibility."""
    if key == FWHM_KEY:
        validated = positive_fwhm(value)
        field = "spotFwhmNm"
    elif key == ORIENTATION_KEY:
        validated = valid_orientation(value)
        field = "scanOrientation"
    else:
        raise ValueError(f"Unsupported MoNaLISA setup key: {key!r}")
    if validated is None:
        raise ValueError(f"Invalid {key} value: {value!r}")
    return _save_processing_preferences(detector, {field: validated}, path=path)


def _save_processing_preferences(detector: str, values: dict, *, path: Path | None = None) -> Path:
    if not isinstance(detector, str) or not detector or detector == "unknown":
        raise ValueError("Select data with an identified detector before saving a setup default")
    setup_path = Path(path) if path is not None else current_setup_path()
    if setup_path is None or not setup_path.is_file():
        raise ValueError("No selected setup is available")
    document = _read_setup(setup_path)
    if not _known_detector(document, detector):
        raise ValueError(f"Detector {detector!r} is not defined in the selected setup")

    # Fail closed on malformed sections rather than overwriting unrelated data.
    processing = document.setdefault("processing", {})
    if not isinstance(processing, dict):
        raise ValueError("Setup processing section must be an object")
    monalisa = processing.setdefault("monalisa", {})
    if not isinstance(monalisa, dict):
        raise ValueError("Setup processing.monalisa section must be an object")
    mapping = monalisa.setdefault("detectorDefaults", {})
    if not isinstance(mapping, dict):
        raise ValueError("Setup processing.monalisa.detectorDefaults must be an object")
    prefs = mapping.setdefault(detector, {})
    if not isinstance(prefs, dict):
        raise ValueError(f"Processing defaults for {detector!r} must be an object")

    # Non-destructive, on-save migration: carry *valid* old preferences to the
    # new location, without mutating legacy managerProperties or other detectors.
    legacy = _legacy_properties(document, detector)
    old_fwhm = positive_fwhm(legacy.get(FWHM_KEY))
    old_orientation = valid_orientation(legacy.get(ORIENTATION_KEY))
    old_estimator = legacy.get(ESTIMATOR_KEY)
    old_fit = _legacy_fit(legacy)
    if old_fwhm is not None:
        prefs.setdefault("spotFwhmNm", old_fwhm)
    if old_orientation is not None:
        prefs.setdefault("scanOrientation", old_orientation)
    if old_estimator in ("local", "joint"):
        prefs.setdefault("estimator", old_estimator)
    if old_fit is not None:
        prefs.setdefault("footprint", _fit_mapping(old_fit))
    prefs.update(values)

    # Replace atomically without round-tripping SetupInfo (which may drop
    # unrelated/unknown properties). No change to disk on validation failure.
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
