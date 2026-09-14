"""Reading localization tables produced by other SMLM software.

Pure functions: a path in, a
:class:`~imswitch.improcess.model.localization_result.LocalizationResult` out.
No Qt, no registry, so every format can be tested headlessly. The mirror image
of :mod:`~imswitch.improcess.analysis.smlm_export`.

Supported today:

* **ThunderSTORM CSV** — the de-facto interchange format.
* **Picasso HDF5** — via :func:`read_picasso_hdf5`, re-exported here so callers
  have one import site for "read someone else's localizations".
* **Generic CSV** — anything else, given an explicit column mapping.

Conventions that differ between tools, and are handled here rather than left
to bite later:

* **Frame numbering.** ThunderSTORM counts from 1, Picasso and ImProcess from
  0. Off-by-one in a frame index is invisible until it is correlated with the
  source stack.
* **Units.** ThunderSTORM declares them per column in the header
  (``x [nm]``, ``sigma [px]``), so they are read from the file rather than
  assumed. A file in pixels cannot be converted without a pixel size, and
  says so instead of guessing.
* **Width versus precision.** ``sigma``/``sigma1``/``sigma2`` are PSF widths;
  ``uncertainty`` is localization precision. They land in different columns.
  See :mod:`~imswitch.improcess.model.localization_schema`.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import (
    LOCALIZATION_COLUMNS,
    localizations_from_columns,
)

# Re-exported so "read a foreign localization file" has one import site.
from .smlm_export import read_picasso_hdf5

#: Used when a nanometre-native file carries no pixel size and the caller
#: supplies none. The value only affects the preview histogram's bin floor and
#: the Picasso export scale, never the coordinates — but it *is* a guess, so
#: results built this way carry ``metadata["pixel_size_assumed"] = True`` and
#: nothing downstream should report it as measured.
ASSUMED_PIXEL_SIZE_NM = 100.0

#: ``name [unit]`` as ThunderSTORM writes it.
_HEADER_UNIT = re.compile(r"^\s*(?P<name>.*?)\s*\[\s*(?P<unit>[^\]]*)\s*\]\s*$")

#: Length units we can convert to nanometres. ``px`` needs a pixel size.
_LENGTH_UNITS_NM: dict[str, float] = {
    "nm": 1.0,
    "nanometer": 1.0,
    "nanometre": 1.0,
    "um": 1000.0,
    "µm": 1000.0,
    "μm": 1000.0,
    "micron": 1000.0,
    "micrometer": 1000.0,
    "m": 1e9,
}
_PIXEL_UNITS = frozenset({"px", "pixel", "pixels"})

#: Units offered by the import dialog, in the order it lists them.
LENGTH_UNIT_CHOICES: tuple[str, ...] = ("nm", "um", "px", "m")

#: ThunderSTORM column -> canonical column(s). Split by kind because only the
#: length-valued ones get a unit conversion.
_TS_POSITION = {"x": ("x_nm",), "y": ("y_nm",), "z": ("z_nm",)}
_TS_WIDTH = {
    # Isotropic PSF: one width for both lateral axes.
    "sigma": ("sigma_x_nm", "sigma_y_nm"),
    # Elliptical PSF: the two principal axes. See _SIGMA12_NOTE.
    "sigma1": ("sigma_x_nm",),
    "sigma2": ("sigma_y_nm",),
}
_TS_PRECISION = {
    "uncertainty": ("lp_x_nm", "lp_y_nm"),
    "uncertainty_xy": ("lp_x_nm", "lp_y_nm"),
    "uncertainty_z": ("lp_z_nm",),
}
_TS_UNITLESS = {"intensity": "photons"}

_SIGMA12_NOTE = (
    "ThunderSTORM's sigma1/sigma2 are the principal axes of an elliptical "
    "PSF. For the axis-aligned astigmatic fits used for 3D they correspond to "
    "the x and y widths and are read as such, but ThunderSTORM does not "
    "guarantee that ordering in general."
)


class LocalizationImportError(ValueError):
    """A localization file could not be read as one."""


def _split_unit(header: str) -> tuple[str, str | None]:
    """``'x [nm]'`` -> ``('x', 'nm')``; ``'id'`` -> ``('id', None)``."""
    text = header.strip().strip('"').strip()
    match = _HEADER_UNIT.match(text)
    if match is None:
        return text.lower().replace(" ", "_"), None
    name = match.group("name").strip().lower().replace(" ", "_")
    return name, match.group("unit").strip().lower()


def _length_scale_nm(unit: str | None, pixel_size_nm: float | None, column: str) -> float:
    """Multiplier turning ``unit`` into nanometres."""
    if unit is None:
        # No declared unit: nanometres is the only defensible reading, since
        # every tool that omits units writes nm.
        return 1.0
    if unit in _LENGTH_UNITS_NM:
        return _LENGTH_UNITS_NM[unit]
    if unit in _PIXEL_UNITS:
        if not pixel_size_nm:
            raise LocalizationImportError(
                f"column {column!r} is in pixels, so reading it needs a pixel "
                f"size; pass pixel_size_nm"
            )
        return float(pixel_size_nm)
    raise LocalizationImportError(f"column {column!r} has unknown unit {unit!r}")


def _detect_delimiter(path: Path) -> str:
    """Pick the delimiter from the header line.

    Determined explicitly rather than by :class:`csv.Sniffer`, because a
    ThunderSTORM header contains spaces inside its unit brackets
    (``"x [nm]"``) and sniffers readily mistake those for the separator.
    """
    try:
        with open(path, newline="") as handle:
            header = handle.readline()
    except OSError as exc:
        raise LocalizationImportError(f"could not read {path.name}: {exc}") from exc
    if not header.strip():
        raise LocalizationImportError(f"{path.name} is empty")
    counts = {candidate: header.count(candidate) for candidate in (",", "\t", ";")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else ","


def _read_csv_columns(path: Path) -> tuple[list[str], dict[str, np.ndarray]]:
    """Read a delimited text file into ``(headers, {header: float array})``.

    Uses pandas when available — a multi-million-row table is why — and falls
    back to the standard library so a missing optional dependency degrades to
    slow rather than broken. Both paths must agree; a test asserts it.
    """
    delimiter = _detect_delimiter(path)
    try:
        import pandas as pd
    except ImportError:
        return _read_csv_columns_stdlib(path, delimiter)

    frame = pd.read_csv(path, sep=delimiter)
    headers = [str(column) for column in frame.columns]
    return headers, {
        header: frame[column].to_numpy(dtype=np.float64, na_value=np.nan)
        for header, column in zip(headers, frame.columns)
    }


def _read_csv_columns_stdlib(
    path: Path, delimiter: str
) -> tuple[list[str], dict[str, np.ndarray]]:
    with open(path, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            headers = [cell.strip().strip('"') for cell in next(reader)]
        except StopIteration:
            raise LocalizationImportError(f"{path.name} is empty") from None
        rows = [row for row in reader if row]

    values: dict[str, np.ndarray] = {}
    for index, header in enumerate(headers):
        column = np.empty(len(rows), dtype=np.float64)
        for row_index, row in enumerate(rows):
            try:
                column[row_index] = float(row[index])
            except (IndexError, ValueError):
                column[row_index] = np.nan
        values[header] = column
    return headers, values


#: Column names other tools plausibly use, per canonical column, best first.
#: Only consulted to *pre-fill* an import dialog — a guess the user then
#: confirms — never to read a file unattended.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "x_nm": ("x", "x_nm", "x_pos", "xpos", "pos_x", "posx", "x_position", "xcenter"),
    "y_nm": ("y", "y_nm", "y_pos", "ypos", "pos_y", "posy", "y_position", "ycenter"),
    "z_nm": ("z", "z_nm", "z_pos", "zpos", "pos_z", "posz", "z_position"),
    "frame": ("frame", "frame_number", "framenumber", "t", "slice", "image_id"),
    "photons": ("photons", "intensity", "photon_count", "n_photons", "counts"),
    "sigma_x_nm": ("sigma_x", "sigmax", "sx", "sigma1", "width_x", "sigma"),
    "sigma_y_nm": ("sigma_y", "sigmay", "sy", "sigma2", "width_y", "sigma"),
    "sigma_z_nm": ("sigma_z", "sigmaz", "sz", "width_z"),
    "lp_x_nm": ("uncertainty_xy", "uncertainty", "lpx", "lp_x", "precision_x"),
    "lp_y_nm": ("uncertainty_xy", "uncertainty", "lpy", "lp_y", "precision_y"),
    "lp_z_nm": ("uncertainty_z", "lpz", "lp_z", "precision_z"),
}

#: Aliases that legitimately serve two canonical columns at once — an
#: isotropic width, or one uncertainty quoted for both lateral axes.
_SHARED_ALIASES = frozenset({"sigma", "uncertainty", "uncertainty_xy"})


def read_csv_headers(path: Path | str) -> list[str]:
    """The header row of a delimited text file, verbatim.

    For populating an import dialog without reading the whole table, which may
    be millions of rows.
    """
    path = Path(path)
    delimiter = _detect_delimiter(path)
    with open(path, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            return [cell.strip().strip('"') for cell in next(reader)]
        except StopIteration:
            raise LocalizationImportError(f"{path.name} is empty") from None


def guess_column_mapping(headers: list[str]) -> dict[str, str]:
    """Best-effort canonical column -> header, for pre-filling a dialog.

    Deliberately incomplete rather than creative: a column it cannot place is
    left out for the user to set, because a wrong guess that looks right is
    worse than an obvious blank.
    """
    normalized = {}
    for header in headers:
        key, _unit = _split_unit(header)
        normalized.setdefault(key, header)

    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            header = normalized.get(alias)
            if header is None:
                continue
            if header in claimed and alias not in _SHARED_ALIASES:
                continue
            mapping[canonical] = header
            claimed.add(header)
            break
    return mapping


def guess_length_unit(headers: list[str]) -> str:
    """The unit a file declares for its coordinates, defaulting to nm."""
    for header in headers:
        key, unit = _split_unit(header)
        if key in ("x", "y", "x_nm", "y_nm", "xpos", "ypos") and unit:
            return unit
    return "nm"


def read_thunderstorm_csv(
    path: Path | str,
    *,
    pixel_size_nm: float | None = None,
    name: str | None = None,
) -> LocalizationResult:
    """Read a ThunderSTORM CSV export.

    Units are taken from the header (``x [nm]``, ``sigma [px]``), so a file in
    pixels is converted with ``pixel_size_nm`` and one in nanometres needs no
    pixel size at all. Frames are converted from ThunderSTORM's 1-based
    numbering to the 0-based index the rest of ImProcess uses.

    Args:
        path: The ``.csv`` file.
        pixel_size_nm: Camera pixel size. Required only when some column is
            expressed in pixels; otherwise recorded for the preview and the
            Picasso export, and assumed when absent (see
            :data:`ASSUMED_PIXEL_SIZE_NM`).
        name: Result name; defaults to the file stem.
    """
    path = Path(path)
    headers, raw = _read_csv_columns(path)
    if not headers:
        raise LocalizationImportError(f"{path.name} has no header row")

    columns: dict[str, np.ndarray] = {}
    seen: set[str] = set()
    used_sigma12 = False

    for header in headers:
        key, unit = _split_unit(header)
        values = raw[header]
        seen.add(key)

        if key in _TS_POSITION or key in _TS_WIDTH or key in _TS_PRECISION:
            scale = _length_scale_nm(unit, pixel_size_nm, key)
            targets = (
                _TS_POSITION.get(key) or _TS_WIDTH.get(key) or _TS_PRECISION[key]
            )
            if key in ("sigma1", "sigma2"):
                used_sigma12 = True
            for target in targets:
                columns[target] = (values * scale).astype(np.float32)
        elif key in _TS_UNITLESS:
            columns[_TS_UNITLESS[key]] = values.astype(np.float32)
        elif key == "frame":
            # ThunderSTORM counts frames from 1; we count from 0.
            frames = np.nan_to_num(values, nan=1.0) - 1.0
            columns["frame"] = np.clip(frames, 0, None).astype(np.int32)
        # id/offset/bkgstd and anything else are dropped on purpose.

    missing = {"x_nm", "y_nm"} - set(columns)
    if missing:
        raise LocalizationImportError(
            f"{path.name} does not look like a ThunderSTORM export: no "
            f"{sorted(missing)} column (found {sorted(seen)})"
        )

    has_z = "z_nm" in columns and np.any(columns["z_nm"] != 0)
    metadata: dict[str, Any] = {"import_format": "thunderstorm-csv"}
    if used_sigma12:
        metadata["sigma_source"] = "sigma1/sigma2"
        metadata["sigma_note"] = _SIGMA12_NOTE

    return _build_result(
        path,
        columns,
        pixel_size_nm=pixel_size_nm,
        name=name,
        dims="3D" if has_z else "2D",
        metadata=metadata,
    )


def read_generic_csv(
    path: Path | str,
    mapping: Mapping[str, str],
    *,
    pixel_size_nm: float | None = None,
    unit: str = "nm",
    frame_base: int = 0,
    name: str | None = None,
) -> LocalizationResult:
    """Read an arbitrary CSV given an explicit column mapping.

    The escape hatch that keeps the supported-format list from becoming a
    treadmill, and what the import dialog will drive.

    Args:
        path: The delimited text file.
        mapping: canonical column -> column name in the file, e.g.
            ``{"x_nm": "xpos", "y_nm": "ypos"}``. Unknown canonical names
            raise rather than being ignored.
        pixel_size_nm: Needed when ``unit`` is pixels.
        unit: Length unit of every spatial column in the file.
        frame_base: The file's first frame number, subtracted from ``frame``.
        name: Result name; defaults to the file stem.
    """
    path = Path(path)
    unknown = set(mapping) - set(LOCALIZATION_COLUMNS)
    if unknown:
        raise LocalizationImportError(
            f"Unknown canonical column(s) {sorted(unknown)}; valid names are "
            f"{list(LOCALIZATION_COLUMNS)}"
        )
    for required in ("x_nm", "y_nm"):
        if required not in mapping:
            raise LocalizationImportError(f"mapping must provide {required!r}")

    _, raw = _read_csv_columns(path)
    scale = _length_scale_nm(unit.strip().lower(), pixel_size_nm, "spatial columns")

    columns: dict[str, np.ndarray] = {}
    for canonical, source in mapping.items():
        if source not in raw:
            raise LocalizationImportError(f"{path.name} has no column {source!r}")
        values = raw[source]
        if canonical == "frame":
            columns["frame"] = np.clip(
                np.nan_to_num(values, nan=float(frame_base)) - frame_base, 0, None
            ).astype(np.int32)
        elif canonical == "photons":
            columns["photons"] = values.astype(np.float32)
        else:
            columns[canonical] = (values * scale).astype(np.float32)

    has_z = "z_nm" in columns and np.any(columns["z_nm"] != 0)
    return _build_result(
        path,
        columns,
        pixel_size_nm=pixel_size_nm,
        name=name,
        dims="3D" if has_z else "2D",
        metadata={"import_format": "generic-csv"},
    )


def _build_result(
    path: Path,
    columns: dict[str, np.ndarray],
    *,
    pixel_size_nm: float | None,
    name: str | None,
    dims: str,
    metadata: dict[str, Any],
) -> LocalizationResult:
    """Assemble a result, recording whether the pixel size was a guess."""
    if not pixel_size_nm:
        pixel_size_nm = ASSUMED_PIXEL_SIZE_NM
        metadata = {**metadata, "pixel_size_assumed": True}

    locs = localizations_from_columns(columns)
    return LocalizationResult(
        name=name or path.stem,
        locs=locs,
        pixel_size_nm=float(pixel_size_nm),
        dims=dims,
        source_name=path.name,
        metadata=metadata,
    )


def sniff_localization_format(path: Path | str) -> str | None:
    """Identify a localization file by content, or ``None`` if it is not one.

    Content rather than suffix, because ``.hdf5`` is equally an image stack
    and ``.csv`` is equally a results table from something else.
    """
    path = Path(path)
    if not path.is_file():
        return None
    suffix = path.suffix.lower()

    if suffix in (".h5", ".hdf5", ".hdf"):
        try:
            import h5py

            with h5py.File(str(path), "r") as h5:
                return "picasso-hdf5" if "locs" in h5 else None
        except Exception:
            return None

    if suffix in (".csv", ".txt", ".tsv"):
        try:
            with open(path, newline="") as handle:
                header = handle.readline()
        except OSError:
            return None
        if not header.strip():
            return None
        keys = {_split_unit(cell)[0] for cell in header.split(_detect_delimiter(path))}
        # ThunderSTORM always writes a frame column beside the coordinates;
        # requiring it keeps an arbitrary x/y table from being read with
        # ThunderSTORM's 1-based frame convention applied to it.
        if {"x", "y", "frame"} <= keys:
            return "thunderstorm-csv"
        return "generic-csv"

    return None


def read_localizations(
    path: Path | str,
    *,
    pixel_size_nm: float | None = None,
    name: str | None = None,
) -> LocalizationResult:
    """Read any recognised localization file.

    Raises :class:`LocalizationImportError` when the format is not recognised,
    or when it is a generic CSV, which needs a column mapping that only
    :func:`read_generic_csv` can be given.
    """
    path = Path(path)
    fmt = sniff_localization_format(path)
    if fmt == "picasso-hdf5":
        return read_picasso_hdf5(path, pixel_size_nm=pixel_size_nm, name=name)
    if fmt == "thunderstorm-csv":
        return read_thunderstorm_csv(path, pixel_size_nm=pixel_size_nm, name=name)
    if fmt == "generic-csv":
        raise LocalizationImportError(
            f"{path.name} looks like a table but not a recognised localization "
            f"format; import it with an explicit column mapping"
        )
    raise LocalizationImportError(f"{path.name} is not a localization file")


__all__ = [
    "ASSUMED_PIXEL_SIZE_NM",
    "LENGTH_UNIT_CHOICES",
    "LocalizationImportError",
    "guess_column_mapping",
    "guess_length_unit",
    "read_csv_headers",
    "read_generic_csv",
    "read_localizations",
    "read_picasso_hdf5",
    "read_thunderstorm_csv",
    "sniff_localization_format",
]
