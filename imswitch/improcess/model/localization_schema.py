"""Canonical single-molecule localization schema (single source of truth).

Every SMLM component in ImProcess — the localizer, the in-house renderer,
table processors, and the napari-storm export — reads and writes the same
structured record described here. Keeping the column contract in one module
means the phases can evolve independently without re-deriving field names or
units in five places.

Design decisions (see docs/design/plans/smlm-localization-port.md, section 7):

* **Units are nanometres internally.** Positions and sigmas are stored in nm;
  the acquisition pixel size lives in the owning
  :class:`~imswitch.improcess.model.localization_result.LocalizationResult`
  metadata, not in the table. A *pixel* view is derived on demand for the
  napari-storm boundary (which is pixel-native), via
  :func:`to_napari_storm_recarray`.
* **The schema is 3D-ready.** ``z_nm`` and ``sigma_z_nm`` always exist; a
  2D localizer simply leaves them at zero. ``dims`` on the result records
  whether the z columns are meaningful.

The field order mirrors napari-storm's ``LOCS_DTYPE`` (frame, x, y, z,
sigma_x, sigma_y, sigma_z, photons) so the export is a rename + unit
conversion rather than a reshape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

#: Ordered canonical column names. Do not reorder — downstream code and the
#: napari-storm export rely on positional correspondence with ``LOCS_DTYPE``.
LOCALIZATION_COLUMNS: tuple[str, ...] = (
    "frame",
    "x_nm",
    "y_nm",
    "z_nm",
    "sigma_x_nm",
    "sigma_y_nm",
    "sigma_z_nm",
    "photons",
)

#: Structured dtype for a localization table. ``frame`` is an int index into
#: the source stack; every spatial quantity is float32 nanometres; ``photons``
#: is the fitted integrated intensity.
LOCALIZATION_DTYPE: np.dtype = np.dtype(
    [
        ("frame", "i4"),
        ("x_nm", "f4"),
        ("y_nm", "f4"),
        ("z_nm", "f4"),
        ("sigma_x_nm", "f4"),
        ("sigma_y_nm", "f4"),
        ("sigma_z_nm", "f4"),
        ("photons", "f4"),
    ]
)

#: Columns that carry the lateral position — the minimum a caller must supply.
REQUIRED_INPUT_COLUMNS: tuple[str, ...] = ("x_nm", "y_nm")

#: napari-storm pixel-native schema (``ns_constants.LOCS_DTYPE``). The export
#: maps the canonical nm columns onto these names after dividing by the pixel
#: size. Kept here so the contract at the boundary is documented in one place.
NAPARI_STORM_DTYPE: np.dtype = np.dtype(
    [
        ("frame_number", "i4"),
        ("x_pos_pixels", "f4"),
        ("y_pos_pixels", "f4"),
        ("z_pos_pixels", "f4"),
        ("sigma_x_pixels", "f4"),
        ("sigma_y_pixels", "f4"),
        ("sigma_z_pixels", "f4"),
        ("photon_count", "f4"),
    ]
)

#: Positional map canonical column -> napari-storm column.
_CANONICAL_TO_NAPARI_STORM: tuple[tuple[str, str], ...] = tuple(
    zip(LOCALIZATION_COLUMNS, NAPARI_STORM_DTYPE.names)
)


def empty_localizations(count: int = 0) -> np.recarray:
    """Return a zero-filled localization recarray with ``count`` rows."""
    if count < 0:
        raise ValueError("count must be non-negative")
    return np.zeros(int(count), dtype=LOCALIZATION_DTYPE).view(np.recarray)


def localizations_from_columns(columns: Mapping[str, Any]) -> np.recarray:
    """Build a canonical recarray from a mapping of column name -> 1D array.

    ``x_nm`` and ``y_nm`` are mandatory. Missing optional columns are filled
    with sensible defaults: ``z``/``sigma_z`` -> 0, ``sigma_x``/``sigma_y``
    -> 0, ``photons`` -> the intensity if supplied else 1, ``frame`` -> 0.
    Unknown keys raise, so a typo can't silently vanish.
    """
    unknown = set(columns) - set(LOCALIZATION_COLUMNS)
    if unknown:
        raise KeyError(
            f"Unknown localization column(s): {sorted(unknown)}; "
            f"valid columns are {list(LOCALIZATION_COLUMNS)}"
        )
    for required in REQUIRED_INPUT_COLUMNS:
        if required not in columns:
            raise KeyError(f"Missing required localization column {required!r}")

    lengths = {name: len(np.asarray(values)) for name, values in columns.items()}
    count = next(iter(lengths.values()))
    if any(length != count for length in lengths.values()):
        raise ValueError(f"Localization columns have mismatched lengths: {lengths}")

    locs = empty_localizations(count)
    for name in LOCALIZATION_COLUMNS:
        if name in columns:
            locs[name] = np.asarray(columns[name])
    return locs


def as_localizations(array: Any) -> np.recarray:
    """Coerce ``array`` to a canonical localization recarray, validating dtype.

    Accepts an existing structured array/recarray whose fields are a superset
    of the canonical columns (extra fields are dropped), reordering to the
    canonical field order. Raises if any canonical column is absent.
    """
    structured = np.asarray(array)
    if structured.dtype.names is None:
        raise TypeError(
            "Localization data must be a structured/record array with named "
            f"fields; got plain dtype {structured.dtype!r}"
        )
    missing = [name for name in LOCALIZATION_COLUMNS if name not in structured.dtype.names]
    if missing:
        raise KeyError(f"Localization array missing column(s): {missing}")
    out = empty_localizations(structured.shape[0])
    for name in LOCALIZATION_COLUMNS:
        out[name] = structured[name]
    return out


def validate_localizations(array: Any) -> np.recarray:
    """Return ``array`` as a recarray if it already matches the exact schema.

    Unlike :func:`as_localizations` this does not copy or reorder — it asserts
    the dtype is exactly :data:`LOCALIZATION_DTYPE`. Use for cheap invariant
    checks on data that should already be canonical.
    """
    structured = np.asarray(array)
    if structured.dtype != LOCALIZATION_DTYPE:
        raise TypeError(
            f"Expected localization dtype {LOCALIZATION_DTYPE!r}, got "
            f"{structured.dtype!r}"
        )
    return structured.view(np.recarray)


def to_napari_storm_recarray(
    locs: Any,
    pixel_size_nm: float,
    *,
    z_pixel_size_nm: float | None = None,
) -> np.recarray:
    """Project canonical (nm) localizations onto napari-storm's pixel schema.

    Positions and lateral sigmas are divided by ``pixel_size_nm``; the z
    columns use ``z_pixel_size_nm`` when given (astigmatism 3D often has a
    different axial sampling) and fall back to ``pixel_size_nm`` otherwise.
    ``frame`` and ``photons`` pass through unchanged.
    """
    if pixel_size_nm <= 0:
        raise ValueError("pixel_size_nm must be positive")
    z_scale = float(z_pixel_size_nm) if z_pixel_size_nm else float(pixel_size_nm)

    canonical = as_localizations(locs)
    out = np.zeros(canonical.shape[0], dtype=NAPARI_STORM_DTYPE).view(np.recarray)
    lateral = float(pixel_size_nm)
    scale = {
        "x_nm": lateral,
        "y_nm": lateral,
        "z_nm": z_scale,
        "sigma_x_nm": lateral,
        "sigma_y_nm": lateral,
        "sigma_z_nm": z_scale,
    }
    for canonical_name, storm_name in _CANONICAL_TO_NAPARI_STORM:
        values = canonical[canonical_name]
        divisor = scale.get(canonical_name)
        out[storm_name] = values / divisor if divisor else values
    return out


__all__ = [
    "LOCALIZATION_COLUMNS",
    "LOCALIZATION_DTYPE",
    "REQUIRED_INPUT_COLUMNS",
    "NAPARI_STORM_DTYPE",
    "empty_localizations",
    "localizations_from_columns",
    "as_localizations",
    "validate_localizations",
    "to_napari_storm_recarray",
]
