"""Dataset source location and path-normalization helpers.

This module is intentionally about locating a readable source, not parsing the
image inside it.  OME/Zarr/TIFF metadata resolution builds on top of this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


LOCATOR_FILE = "file"
LOCATOR_DIRECTORY = "directory"


@dataclass(frozen=True)
class SourceSpec:
    """One supported family of input sources."""

    id: str
    suffixes: tuple[str, ...]
    locator: str
    label: str


@dataclass(frozen=True)
class ResolvedDatasetSource:
    """A user-selected path normalized to the dataset source root."""

    path: Path
    spec: SourceSpec
    original_path: Path

    @property
    def format_id(self) -> str:
        return self.spec.id


HDF5_SPEC = SourceSpec(
    id="hdf5",
    suffixes=(".h5", ".hdf5", ".hdf"),
    locator=LOCATOR_FILE,
    label="HDF5",
)
TIFF_SPEC = SourceSpec(
    id="tiff",
    suffixes=(".ome.tiff", ".ome.tif", ".tiff", ".tif"),
    locator=LOCATOR_FILE,
    label="TIFF / OME-TIFF",
)
ZARR_SPEC = SourceSpec(
    id="zarr",
    suffixes=(".zarr",),
    locator=LOCATOR_DIRECTORY,
    label="Zarr / OME-Zarr",
)

SOURCE_SPECS: tuple[SourceSpec, ...] = (HDF5_SPEC, TIFF_SPEC, ZARR_SPEC)
_SPECS_BY_ID = {spec.id: spec for spec in SOURCE_SPECS}
_EXTENSION_ALIASES = {
    "h5": "hdf5",
    "hdf": "hdf5",
    "hdf5": "hdf5",
    "tif": "tiff",
    "tiff": "tiff",
    "ome.tif": "tiff",
    "ome.tiff": "tiff",
    "zarr": "zarr",
}


def _normalized_extension(extension: str | None) -> str | None:
    if extension is None:
        return None
    text = str(extension).strip().lower()
    while text.startswith("."):
        text = text[1:]
    return text or None


def spec_for_extension(extension: str | None) -> SourceSpec | None:
    normalized = _normalized_extension(extension)
    if normalized is None:
        return None
    spec_id = _EXTENSION_ALIASES.get(normalized)
    if spec_id is None:
        return None
    return _SPECS_BY_ID[spec_id]


def specs_for_extensions(extensions: Iterable[str] | None) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for extension in extensions or ():
        spec = spec_for_extension(extension)
        if spec is not None and spec.id not in seen:
            specs.append(spec)
            seen.add(spec.id)
    return specs


def specs_for_reconstructor(reconstructor) -> list[SourceSpec]:
    return specs_for_extensions(getattr(reconstructor, "file_extensions", None))


def preferred_source_spec(
    specs: Sequence[SourceSpec] | None,
    preferred_extension: str | None = None,
) -> SourceSpec:
    available = list(specs or SOURCE_SPECS)
    preferred = spec_for_extension(preferred_extension)
    if preferred is not None and any(spec.id == preferred.id for spec in available):
        return preferred
    if available:
        return available[0]
    return HDF5_SPEC


def _matches_suffix(path: Path, spec: SourceSpec) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in spec.suffixes)


def _zarr_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if _matches_suffix(candidate, ZARR_SPEC):
            return candidate
    return None


def has_zarr_ancestor(path: str | Path) -> bool:
    return _zarr_ancestor(Path(path)) is not None


def resolve_dataset_source(
    path: str | Path,
    *,
    allowed_specs: Sequence[SourceSpec] | None = None,
) -> ResolvedDatasetSource:
    """Normalize ``path`` to a supported dataset source root.

    A path inside ``*.zarr`` is treated as the store root. This catches common
    file-dialog mistakes where the user enters the Zarr directory and selects a
    chunk file such as ``data/0.0.0``.
    """

    original = Path(path)
    allowed = list(allowed_specs or SOURCE_SPECS)
    allowed_ids = {spec.id for spec in allowed}

    zarr_root = _zarr_ancestor(original)
    if zarr_root is not None and ZARR_SPEC.id in allowed_ids:
        return ResolvedDatasetSource(zarr_root, ZARR_SPEC, original)

    for spec in allowed:
        if _matches_suffix(original, spec):
            return ResolvedDatasetSource(original, spec, original)

    suffix = original.suffix or original.name
    raise ValueError(f'Unsupported file extension "{suffix}"')


def normalize_dataset_path(
    path: str | Path,
    *,
    allowed_specs: Sequence[SourceSpec] | None = None,
) -> str:
    return str(resolve_dataset_source(path, allowed_specs=allowed_specs).path)


def file_dialog_filter(specs: Sequence[SourceSpec] | None) -> str:
    file_specs = [spec for spec in (specs or SOURCE_SPECS) if spec.locator == LOCATOR_FILE]
    patterns = [f"*{suffix}" for spec in file_specs for suffix in spec.suffixes]
    if not patterns:
        return ""
    return f"Supported image files ({' '.join(patterns)})"
