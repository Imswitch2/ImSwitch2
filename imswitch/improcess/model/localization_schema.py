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

#: Ordered canonical column names. Do not reorder, and append only — the first
#: eight correspond *positionally* to napari-storm's ``LOCS_DTYPE`` and the
#: export relies on it.
#:
#: Two different widths live here on purpose, because they are different
#: physical quantities and conflating them is a real hazard:
#:
#: * ``sigma_*_nm`` — the **PSF width**, i.e. how broad the spot was on the
#:   camera. Useful for rejecting bad fits.
#: * ``lp_*_nm`` — the **localization precision**, i.e. how well the emitter's
#:   position is known. This is what an SMLM reconstruction should be *rendered*
#:   with; drawing molecules at the PSF width merely reproduces a
#:   diffraction-limited image.
#:
#: Picasso calls these ``sx``/``sy`` and ``lpx``/``lpy``; ThunderSTORM calls
#: them ``sigma`` and ``uncertainty``. Both are carried through import.
LOCALIZATION_COLUMNS: tuple[str, ...] = (
    "frame",
    "x_nm",
    "y_nm",
    "z_nm",
    "sigma_x_nm",
    "sigma_y_nm",
    "sigma_z_nm",
    "photons",
    "lp_x_nm",
    "lp_y_nm",
    "lp_z_nm",
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
        ("lp_x_nm", "f4"),
        ("lp_y_nm", "f4"),
        ("lp_z_nm", "f4"),
    ]
)

#: Columns that carry the lateral position — the minimum a caller must supply.
REQUIRED_INPUT_COLUMNS: tuple[str, ...] = ("x_nm", "y_nm")

#: Columns added after the schema was first written. Tables saved by earlier
#: versions do not have them, so :func:`as_localizations` fills them with zeros
#: instead of refusing the file.
OPTIONAL_LOCALIZATION_COLUMNS: tuple[str, ...] = ("lp_x_nm", "lp_y_nm", "lp_z_nm")

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

#: Column names a napari-storm ``LocalizationTable`` needs that never vary.
_NAPARI_STORM_BASE_KWARGS: dict[str, Any] = {
    "position_columns": {"x": "x_nm", "y": "y_nm", "z": "z_nm"},
    "position_scale_nm": 1.0,
    "photon_column": "photons",
}

#: Width columns to declare, keyed by whether precision is available.
_NAPARI_STORM_SIGMA_COLUMNS: dict[str, dict[str, str]] = {
    "precision": {"x": "lp_x_nm", "y": "lp_y_nm", "z": "lp_z_nm"},
    "psf": {"x": "sigma_x_nm", "y": "sigma_y_nm", "z": "sigma_z_nm"},
}


def napari_storm_table_kwargs(locs: Any = None) -> dict[str, Any]:
    """Declare our canonical schema to a napari-storm ``LocalizationTable``.

    Since napari-storm 1.0 the sigma and photon columns are declarable
    alongside the position ones, so the *viewer* reads our nm recarray in
    place — no rename, no unit conversion, no copy. Deliberately not the same
    path as :func:`to_napari_storm_recarray`, which stays for the Picasso/HDF5
    *export*, a pixel-native file format. Display reads nm; export writes
    pixels.

    ``position_scale_nm=1.0`` says our columns are already nanometres, and
    napari-storm's ``sigma_scale_nm`` follows it, so widths need no separate
    declaration.

    Which width is declared is the interesting part. A reconstruction should
    render each molecule as a Gaussian of its **localization precision**, so
    ``lp_*_nm`` is preferred; rendering at the PSF width would only reproduce a
    diffraction-limited image. Tables with no precision — anything localized
    before it was computed, or imported from a format that omits it — fall back
    to ``sigma_*_nm`` so they still draw.

    Args:
        locs: Canonical recarray to inspect. ``None`` assumes precision is
            present, which is the right default for a table we just produced.
    """
    kwargs = dict(_NAPARI_STORM_BASE_KWARGS)
    kwargs["sigma_columns"] = dict(
        _NAPARI_STORM_SIGMA_COLUMNS["psf" if _lacks_precision(locs) else "precision"]
    )
    return kwargs


def _lacks_precision(locs: Any) -> bool:
    """True when ``locs`` carries no usable lateral precision."""
    if locs is None:
        return False
    names = getattr(np.asarray(locs).dtype, "names", None) or ()
    if "lp_x_nm" not in names or "lp_y_nm" not in names:
        return True
    if not len(locs):
        return False
    # A zero-filled column is indistinguishable from "not measured", and
    # napari-storm refuses a width column with nothing usable in it.
    return not (np.any(locs["lp_x_nm"] > 0) and np.any(locs["lp_y_nm"] > 0))


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
    canonical field order. Raises if any canonical column is absent, except
    those in :data:`OPTIONAL_LOCALIZATION_COLUMNS`, which are zero-filled so
    that tables written before those columns existed still load.
    """
    structured = np.asarray(array)
    if structured.dtype.names is None:
        raise TypeError(
            "Localization data must be a structured/record array with named "
            f"fields; got plain dtype {structured.dtype!r}"
        )
    missing = [
        name
        for name in LOCALIZATION_COLUMNS
        if name not in structured.dtype.names
        and name not in OPTIONAL_LOCALIZATION_COLUMNS
    ]
    if missing:
        raise KeyError(f"Localization array missing column(s): {missing}")
    out = empty_localizations(structured.shape[0])
    for name in LOCALIZATION_COLUMNS:
        if name in structured.dtype.names:
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
    "OPTIONAL_LOCALIZATION_COLUMNS",
    "NAPARI_STORM_DTYPE",
    "napari_storm_table_kwargs",
    "empty_localizations",
    "localizations_from_columns",
    "as_localizations",
    "validate_localizations",
    "to_napari_storm_recarray",
]
