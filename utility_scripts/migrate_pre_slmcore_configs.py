#!/usr/bin/env python3
"""Migrate pre-slmcore ImSwitch SLM HDF5 configs to current slmcore schema v1.

This is intentionally a one-off migration utility, not a compatibility layer.
It migrates editable configuration only. Legacy rendered component arrays,
full-SLM frames and saved CGH arrays are never copied.

Running
-------
Edit the USER CONFIGURATION block below, then run this file directly.
INPUT_PATH may point to one legacy .h5/.hdf5 file or to a folder;
folder processing is non-recursive.

Output/archive behavior
-----------------------
2 modes available.
1. Moves legacy to archive folder and leave new ones in place.
2. Leaves legacy files in place and writes migrated configs separately
See Output / archive settings in USER CONFIGURATION.

Correction policy
-----------------
Legacy correction resources are deliberately not resolved,imported or copied. 
Migrated configs always use neutral correction resources (correction_pattern=None, 
two_pi_value=255). When loading migrated config for the first time, UI will signal
correction mismatch. Select "use current" to use corrections in corrections directory
and apply them to the migrated configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import h5py
import numpy as np

try:
    from slmcore import (
        DEFAULT_REGISTRIES,
        GroupTopology,
        SectionGeometry,
        SectionPresentation,
        SLMConfigStore,
        SLMGeometry,
        SLMIdentity,
        SLMRuntime,
    )
except ImportError as error:  # pragma: no cover - user-environment diagnostic
    raise SystemExit(
        "Could not import slmcore. Run this script from an environment where the "
        "current slmcore package is installed (for example the ImSwitch/slmcore "
        "development environment)."
    ) from error


# =============================================================================
# USER CONFIGURATION -- EDIT THIS BLOCK BEFORE RUNNING
# =============================================================================

# One legacy .h5/.hdf5 file OR one folder containing legacy configs. Folder
# processing is non-recursive. This is the only input used by the script.
INPUT_PATH: str | Path = r"C:\Users\Monalisa2\Documents\ImSwitchConfig\imcontrol_slm\configs\LSH0805164"

# Canonical identity of the physical SLM. display_name deliberately stays None:
# display naming belongs to the host/setup, not to this migrated operating config.
SLM_KEY = "slm_off_on"
SERIAL_NUMBER = "LSH0805164"

# Physical SLM geometry.
SLM_WIDTH = 1272
SLM_HEIGHT = 1024
PIXEL_SIZE_UM: float | None = 12.5  # e.g. 12.5

# Canonical section geometry. Keys must match the old config section keys.
# Defaults below match even horizontal split in 2 sections with SLM size=1272x1024
SECTION_GEOMETRIES = {
    "sec_0": dict(x=0, y=0, width=636, height=1024),
    "sec_1": dict(x=636, y=0, width=636, height=1024),
}

# Output / archive settings. Keep these together:
# - archive mode: legacy moves to ARCHIVE_DIRECTORY and new config takes its path;
# - otherwise, legacy stays in place; OUTPUT_DIRECTORY="auto" -> local migrated/
#   ARCHIVE_DIRECTORY="auto" -> local archive/; explicit archive paths must be absolute.
MOVE_LEGACY_TO_ARCHIVE = True
OUTPUT_DIRECTORY: str | Path = "auto"
ARCHIVE_DIRECTORY: str | Path = "auto"
OVERWRITE_EXISTING_MIGRATED = False  # normal mode only


# Print full Python tracebacks for failed files. Useful if adapting another old pre-slmcore config variant.
PRINT_TRACEBACKS = False

# =============================================================================
# END USER CONFIGURATION
# =============================================================================






DEFAULT_OUTPUT_FOLDER_NAME = "migrated"
DEFAULT_ARCHIVE_FOLDER_NAME = "archive"

LEGACY_PATTERN_PARAM_ALIASES: dict[str, dict[str, str]] = {
    # Pattern names are already canonical in the known pre-slmcore format.
}

LEGACY_TARGET_PARAM_ALIASES: dict[str, dict[str, str]] = {
    "multi_foci": {
        "period_x": "period_x_px",
        "period_y": "period_y_px",
    },
    "multi_foci_vector": {
        "period_x": "period_x_px",
        "period_y": "period_y_px",
    },
}

LEGACY_CGH_GENERAL_GROUP = "cgh_general"
LEGACY_CGH_COMPUTATION_GROUP = "cgh_computation"
LEGACY_TARGET_SIZE_KEYS = {"target_size_x", "target_size_y"}
SUPPORTED_EXTENSIONS = {".h5", ".hdf5"}


@dataclass
class MigrationReport:
    source: Path
    destination: Path | None = None
    warnings: list[str] = field(default_factory=list)
    cgh_recomputed_sections: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(str(message))


class LegacyConfigError(ValueError):
    pass


def _decode_legacy_value(value: Any) -> Any:
    """Decode JSON-like strings used by the pre-slmcore HDF5 attribute format."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _attrs(group: h5py.Group) -> dict[str, Any]:
    return {str(key): _decode_legacy_value(value) for key, value in group.attrs.items()}


def _require_group(parent: h5py.Group, key: str, *, context: str) -> h5py.Group:
    node = parent.get(key)
    if not isinstance(node, h5py.Group):
        raise LegacyConfigError(f"Missing legacy group {context}/{key}")
    return node


def _validate_user_configuration() -> tuple[SLMIdentity, SLMGeometry, dict[str, SectionGeometry]]:
    key = str(SLM_KEY or "").strip()
    serial = str(SERIAL_NUMBER or "").strip()
    if not key or key == "CHANGE_ME":
        raise ValueError("Set SLM_KEY in the USER CONFIGURATION block before running.")
    if not serial or serial == "CHANGE_ME":
        raise ValueError("Set SERIAL_NUMBER in the USER CONFIGURATION block before running.")
    if PIXEL_SIZE_UM is None or float(PIXEL_SIZE_UM) <= 0:
        raise ValueError("Set PIXEL_SIZE_UM to the physical SLM pixel size before running.")

    identity = SLMIdentity(
        key=key,
        serial_number=serial,
        display_name=None,
    )
    geometry = SLMGeometry(
        width=int(SLM_WIDTH),
        height=int(SLM_HEIGHT),
        pixel_size_um=float(PIXEL_SIZE_UM),
    )

    if not SECTION_GEOMETRIES:
        raise ValueError("SECTION_GEOMETRIES cannot be empty.")

    sections: dict[str, SectionGeometry] = {}
    for section_key, data in SECTION_GEOMETRIES.items():
        section_key = str(section_key).strip()
        if not section_key:
            raise ValueError("SECTION_GEOMETRIES contains an empty section key.")
        sections[section_key] = SectionGeometry(
            key=section_key,
            x=int(data["x"]),
            y=int(data["y"]),
            width=int(data["width"]),
            height=int(data["height"]),
        )

    return identity, geometry, sections


def _legacy_info(file: h5py.File) -> str:
    value = file.attrs.get("info", "")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")


def _validate_legacy_file(
    file: h5py.File,
    source: Path,
    geometry: SLMGeometry,
    sections: Mapping[str, SectionGeometry],
) -> None:
    # Current slmcore files have a typed file envelope. Refuse them here so a
    # batch folder cannot accidentally be "migrated" twice.
    file_type = file.attrs.get("file_type")
    if isinstance(file_type, bytes):
        file_type = file_type.decode("utf-8", errors="replace")
    if str(file_type or "").strip() == "slm_config":
        raise LegacyConfigError("File is already a current slmcore config.")

    if "parameters" not in file or not isinstance(file["parameters"], h5py.Group):
        raise LegacyConfigError("Missing /parameters group; not a recognized pre-slmcore config.")

    parameters = file["parameters"]
    legacy_section_keys = {
        str(key)
        for key, value in parameters.items()
        if isinstance(value, h5py.Group) and str(key).startswith("sec_")
    }
    configured_keys = set(sections)
    if legacy_section_keys != configured_keys:
        raise LegacyConfigError(
            "Legacy/configured section keys differ: "
            f"legacy={sorted(legacy_section_keys)}, configured={sorted(configured_keys)}"
        )

    # Rendered arrays are not migrated, but their shapes are useful as a guard
    # against running the script with the wrong hardcoded physical geometry.
    full_slm = file.get("images/final/full_slm")
    if isinstance(full_slm, h5py.Dataset) and tuple(full_slm.shape) != geometry.shape:
        raise LegacyConfigError(
            f"Legacy full-SLM shape {tuple(full_slm.shape)} does not match configured "
            f"geometry {geometry.shape}."
        )

    for section_key, section_geometry in sections.items():
        eightbit = file.get(f"sections/{section_key}/components/eightbits")
        if isinstance(eightbit, h5py.Dataset) and tuple(eightbit.shape) != section_geometry.shape:
            raise LegacyConfigError(
                f"Legacy section {section_key} shape {tuple(eightbit.shape)} does not match "
                f"configured geometry {section_geometry.shape}."
            )


def _legacy_section_title(parameters: h5py.Group, section_key: str) -> str | None:
    tab_names = parameters.get("tab_names")
    if not isinstance(tab_names, h5py.Group) or section_key not in tab_names.attrs:
        return None
    value = _decode_legacy_value(tab_names.attrs[section_key])
    text = str(value or "").strip()
    return text or None


def _collect_pattern_topology_and_changes(
    section_group: h5py.Group,
    report: MigrationReport,
) -> tuple[tuple[str, ...], dict[tuple[str, ...], Any]]:
    patterns_group = section_group.get("patterns")
    if not isinstance(patterns_group, h5py.Group):
        return (), {}

    selected: list[str] = []
    changes: dict[tuple[str, ...], Any] = {}

    for pattern_key in patterns_group.keys():
        node = patterns_group.get(pattern_key)
        if not isinstance(node, h5py.Group):
            continue
        legacy = _attrs(node)
        active = bool(legacy.get("active", False))

        registration = DEFAULT_REGISTRIES.patterns.get(pattern_key)
        if registration is None:
            message = f"{section_group.name}: unsupported legacy pattern '{pattern_key}'"
            if active:
                raise LegacyConfigError(message + " is active; refusing silent behavior change.")
            report.warn(message + " is inactive and was skipped.")
            continue

        selected.append(pattern_key)
        changes[("patterns", pattern_key, "params", "active")] = active

        aliases = LEGACY_PATTERN_PARAM_ALIASES.get(pattern_key, {})
        consumed = {"active"}
        for old_key, value in legacy.items():
            if old_key == "active":
                continue
            new_key = aliases.get(old_key, old_key)
            if new_key in registration.params:
                changes[("patterns", pattern_key, "params", new_key)] = value
                consumed.add(old_key)

        for old_key in sorted(set(legacy) - consumed):
            report.warn(
                f"{section_group.name}/patterns/{pattern_key}: unknown parameter "
                f"'{old_key}' was skipped."
            )

    if selected:
        changes[("patterns", "active")] = True
    return tuple(selected), changes


def _collect_aberration_topology_and_changes(
    section_group: h5py.Group,
    report: MigrationReport,
) -> tuple[tuple[str, ...], dict[tuple[str, ...], Any]]:
    legacy_group = section_group.get("aberrations")
    if not isinstance(legacy_group, h5py.Group):
        return (), {}

    registration = DEFAULT_REGISTRIES.aberrations.get("zernike")
    if registration is None:
        raise LegacyConfigError("Current slmcore registry has no 'zernike' aberration model.")

    legacy = _attrs(legacy_group)
    active = bool(legacy.pop("aberrations_active", True))
    changes: dict[tuple[str, ...], Any] = {
        ("aberrations", "active"): active,
    }

    consumed: set[str] = set()
    for key, value in legacy.items():
        if key in registration.params:
            changes[("aberrations", "zernike", "params", key)] = value
            consumed.add(key)

    for key in sorted(set(legacy) - consumed):
        report.warn(
            f"{section_group.name}/aberrations: unknown coefficient '{key}' was skipped."
        )

    return ("zernike",), changes


def _collect_cgh_topology_and_changes(
    section_group: h5py.Group,
    report: MigrationReport,
) -> tuple[tuple[str, ...], dict[tuple[str, ...], Any], bool]:
    cgh_group = section_group.get("cgh")
    if not isinstance(cgh_group, h5py.Group):
        return (), {}, False

    general_group = cgh_group.get(LEGACY_CGH_GENERAL_GROUP)
    if not isinstance(general_group, h5py.Group):
        report.warn(f"{section_group.name}/cgh has no cgh_general group; CGH was skipped.")
        return (), {}, False

    general = _attrs(general_group)
    active = bool(general.get("active", False))
    target_type = general.get("target_type")
    target_type = None if target_type is None else str(target_type).strip()

    if not target_type:
        if active:
            raise LegacyConfigError(f"{section_group.name}: CGH is active but has no target_type.")
        return (), {("cgh", "active"): False}, False

    registration = DEFAULT_REGISTRIES.targets.get(target_type)
    if registration is None:
        raise LegacyConfigError(
            f"{section_group.name}: selected legacy CGH target '{target_type}' is not "
            "supported by the current slmcore registry."
        )

    target_group = cgh_group.get(target_type)
    if not isinstance(target_group, h5py.Group):
        raise LegacyConfigError(
            f"{section_group.name}: selected CGH target '{target_type}' has no parameter group."
        )

    changes: dict[tuple[str, ...], Any] = {
        ("cgh", "active"): active,
        ("cgh", "selected_target"): target_type,
    }

    legacy_target = _attrs(target_group)
    aliases = LEGACY_TARGET_PARAM_ALIASES.get(target_type, {})
    consumed: set[str] = set()
    for old_key, value in legacy_target.items():
        if old_key in LEGACY_TARGET_SIZE_KEYS:
            # Old raster implementation detail. Current slmcore resolves its own
            # exact internal raster from the semantic target parameters.
            consumed.add(old_key)
            continue
        new_key = aliases.get(old_key, old_key)
        if new_key in registration.params:
            changes[("cgh", target_type, "params", new_key)] = value
            consumed.add(old_key)

    for key in sorted(set(legacy_target) - consumed):
        report.warn(
            f"{section_group.name}/cgh/{target_type}: unknown parameter '{key}' was skipped."
        )

    algorithm = DEFAULT_REGISTRIES.algorithms.get(registration.algorithm)
    if algorithm is None:
        raise LegacyConfigError(
            f"Current slmcore registry has no algorithm '{registration.algorithm}' "
            f"required by target '{target_type}'."
        )

    computation_group = cgh_group.get(LEGACY_CGH_COMPUTATION_GROUP)
    if isinstance(computation_group, h5py.Group):
        legacy_compute = _attrs(computation_group)
        consumed_compute: set[str] = set()
        for key, value in legacy_compute.items():
            if key in algorithm.params:
                changes[("cgh", target_type, "computation", "params", key)] = value
                consumed_compute.add(key)
        for key in sorted(set(legacy_compute) - consumed_compute):
            report.warn(
                f"{section_group.name}/cgh/cgh_computation: unknown parameter "
                f"'{key}' was skipped."
            )
    else:
        report.warn(
            f"{section_group.name}/cgh has no cgh_computation group; current algorithm "
            "defaults were used."
        )

    # Report other old target groups that are not the selected target. They are
    # intentionally not imported because current configs persist selected/available
    # current-registry target state, not arbitrary retired target implementations.
    ignored_group_names = {
        LEGACY_CGH_GENERAL_GROUP,
        LEGACY_CGH_COMPUTATION_GROUP,
        target_type,
    }
    for key, node in cgh_group.items():
        if isinstance(node, h5py.Group) and key not in ignored_group_names:
            report.warn(
                f"{section_group.name}/cgh: unselected legacy target/group '{key}' was skipped."
            )

    return (target_type,), changes, active


def _collect_section_changes(
    parameters: h5py.Group,
    section_key: str,
    report: MigrationReport,
) -> tuple[dict[str, GroupTopology], dict[tuple[str, ...], Any], bool]:
    section_group = _require_group(parameters, section_key, context="/parameters")

    topology: dict[str, GroupTopology] = {}
    changes: dict[tuple[str, ...], Any] = {}

    general = section_group.get("general")
    if not isinstance(general, h5py.Group):
        raise LegacyConfigError(f"/parameters/{section_key} is missing required 'general' group.")

    legacy_general = _attrs(general)
    optics_keys = {
        "wavelength_nm",
        "pupil_radius_px",
        "center_offset_x_px",
        "center_offset_y_px",
    }
    for key in optics_keys:
        if key in legacy_general:
            changes[("optics", key)] = legacy_general[key]
    for key in sorted(set(legacy_general) - optics_keys):
        report.warn(f"{general.name}: unknown field '{key}' was skipped.")

    correction_options = section_group.get("correction_options")
    if isinstance(correction_options, h5py.Group):
        correction_values = _attrs(correction_options)
        # The legacy format had no separate master active switch. Keep the
        # current correction group active and preserve the two legacy toggles.
        changes[("corrections", "active")] = True
        for key in ("apply_correction_pattern", "apply_twopi_value"):
            if key in correction_values:
                changes[("corrections", key)] = correction_values[key]
        for key in sorted(
            set(correction_values) - {"apply_correction_pattern", "apply_twopi_value"}
        ):
            report.warn(f"{correction_options.name}: unknown field '{key}' was skipped.")

    pattern_items, pattern_changes = _collect_pattern_topology_and_changes(
        section_group, report
    )
    # The current runtime may pre-populate registry-backed groups. Make the
    # legacy file authoritative for which editable items existed, including an
    # explicitly empty selection when the legacy group was absent.
    topology["patterns"] = GroupTopology(enabled=True, item_keys=pattern_items)
    changes.update(pattern_changes)

    aberration_items, aberration_changes = _collect_aberration_topology_and_changes(
        section_group, report
    )
    topology["aberrations"] = GroupTopology(
        enabled=True, item_keys=aberration_items
    )
    changes.update(aberration_changes)

    cgh_items, cgh_changes, cgh_active = _collect_cgh_topology_and_changes(
        section_group, report
    )
    topology["cgh"] = GroupTopology(enabled=True, item_keys=cgh_items)
    changes.update(cgh_changes)

    return topology, changes, cgh_active


def _build_runtime_from_legacy(
    file: h5py.File,
    source: Path,
    report: MigrationReport,
    identity: SLMIdentity,
    geometry: SLMGeometry,
    sections: Mapping[str, SectionGeometry],
    correction_provider,
) -> SLMRuntime:
    _validate_legacy_file(file, source, geometry, sections)
    parameters = file["parameters"]

    runtime = SLMRuntime(
        identity=identity,
        geometry=geometry,
        section_geometries=sections,
        registries=DEFAULT_REGISTRIES,
        correction_provider=correction_provider,
    )

    cgh_active_sections: list[str] = []

    for section_key in sections:
        topology, changes, cgh_active = _collect_section_changes(
            parameters, section_key, report
        )
        if topology:
            runtime.apply_section_topology(section_key, topology)
        if changes:
            runtime.apply_section_patch(
                section_key,
                changes,
                use_workspace_corrections=False,
            )

        title = _legacy_section_title(parameters, section_key)
        if title is not None:
            runtime.set_section_presentation(
                section_key,
                SectionPresentation(title=title),
            )

        if cgh_active:
            cgh_active_sections.append(section_key)

    # Recompute, do not copy, active legacy CGH. This gives the new config a
    # valid current CGH session and a final_eightbit frame consistent with the
    # migrated editable state.
    for section_key in cgh_active_sections:
        job = runtime.prepare_section_base_cgh(section_key)
        result = job.run()
        transition = runtime.commit_section_cgh(section_key, result)
        if transition is None:
            raise RuntimeError(f"CGH result for {section_key} was unexpectedly rejected.")
        report.cgh_recomputed_sections.append(section_key)

    return runtime


def migrate_one(
    source: Path,
    output_directory: Path,
    *,
    overwrite: bool,
    identity: SLMIdentity,
    geometry: SLMGeometry,
    sections: Mapping[str, SectionGeometry],
    correction_provider,
) -> MigrationReport:
    source = Path(source).expanduser().resolve()
    output_directory = Path(output_directory).expanduser().resolve()
    report = MigrationReport(source=source)

    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise LegacyConfigError(f"Unsupported file extension: {source.suffix}")

    with h5py.File(source, "r") as file:
        runtime = _build_runtime_from_legacy(
            file,
            source,
            report,
            identity,
            geometry,
            sections,
            correction_provider,
        )
        info = _legacy_info(file)

    config = runtime.create_config()
    store = SLMConfigStore(output_directory, DEFAULT_REGISTRIES)
    destination = store.destination(source.name)
    if destination.resolve() == source.resolve():
        raise ValueError(
            "Destination resolves to the legacy source file. Migration never "
            "writes directly over a legacy source config."
        )

    migration_info = "Migrated from pre-slmcore ImSwitch config"
    if info.strip():
        migration_info += "\n\n" + info.strip()

    metadata = store.save(
        destination,
        config,
        info=migration_info,
        overwrite=overwrite,
    )
    report.destination = metadata.path

    # Verify the just-written file through the public current loader. Any
    # schema/registry serialization mistake should fail the migration now, not
    # later when the user first opens the config.
    loaded, warnings = store.load(metadata.path)
    if loaded.identity != config.identity or loaded.geometry != config.geometry:
        raise RuntimeError("Post-save verification changed config identity or geometry.")
    if not np.array_equal(loaded.final_eightbit, config.final_eightbit):
        raise RuntimeError("Post-save verification changed the compiled SLM frame.")
    for warning in warnings:
        report.warn(f"Current-loader warning after save: {warning}")

    return report


def _input_files(path: Path) -> tuple[Path, ...]:
    path = Path(path).expanduser().resolve()
    if path.is_file():
        return (path,)
    if not path.is_dir():
        raise FileNotFoundError(path)

    files = sorted(
        (
            item for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda item: item.name.casefold(),
    )
    if not files:
        raise FileNotFoundError(f"No .h5/.hdf5 files found in {path}")
    return tuple(files)


def _configured_input_path() -> Path:
    raw = str(INPUT_PATH or "").strip()
    if not raw or raw == "CHANGE_ME":
        raise ValueError("Set INPUT_PATH in the USER CONFIGURATION block before running.")
    return Path(INPUT_PATH).expanduser().resolve()


def _default_output_directory(input_path: Path) -> Path:
    input_path = Path(input_path).expanduser().resolve()
    parent = input_path.parent if input_path.is_file() else input_path
    return parent / DEFAULT_OUTPUT_FOLDER_NAME


def _resolve_output_directory(input_path: Path) -> Path:
    raw = str(OUTPUT_DIRECTORY or "").strip()
    if not raw or raw.casefold() == "auto":
        return _default_output_directory(input_path)
    return Path(OUTPUT_DIRECTORY).expanduser().resolve()


def _default_archive_directory(input_path: Path) -> Path:
    input_path = Path(input_path).expanduser().resolve()
    parent = input_path.parent if input_path.is_file() else input_path
    return parent / DEFAULT_ARCHIVE_FOLDER_NAME


def _resolve_archive_directory(input_path: Path) -> Path:
    raw = str(ARCHIVE_DIRECTORY or "").strip()
    if not raw:
        raise ValueError(
            'ARCHIVE_DIRECTORY must be "auto" or an explicit absolute directory path.'
        )
    if raw.casefold() == "auto":
        return _default_archive_directory(input_path)

    directory = Path(ARCHIVE_DIRECTORY).expanduser()
    if not directory.is_absolute():
        raise ValueError(
            "Explicit ARCHIVE_DIRECTORY must be a full/absolute path. "
            f"Got: {ARCHIVE_DIRECTORY!r}"
        )
    return directory.resolve()


def migrate_one_archive_and_replace(
    source: Path,
    archive_directory: Path,
    *,
    identity: SLMIdentity,
    geometry: SLMGeometry,
    sections: Mapping[str, SectionGeometry],
    correction_provider,
) -> MigrationReport:
    """Migrate safely, archive the old source, then put the new config in place."""
    source = Path(source).expanduser().resolve()
    archive_directory = Path(archive_directory).expanduser().resolve()
    archive_destination = archive_directory / source.name

    if archive_destination.resolve() == source:
        raise ValueError(
            "Archive destination resolves to the source file itself. Choose a different "
            "ARCHIVE_DIRECTORY."
        )
    if archive_destination.exists():
        raise FileExistsError(
            f"Archive already contains {archive_destination}. Existing archived legacy "
            "files are never overwritten."
        )

    # Stage on the same filesystem as the source so replacing the original path
    # with the verified current config can use an atomic os.replace().
    with tempfile.TemporaryDirectory(
        prefix=".slm_migration_",
        dir=str(source.parent),
    ) as temporary_directory:
        report = migrate_one(
            source,
            Path(temporary_directory),
            overwrite=False,
            identity=identity,
            geometry=geometry,
            sections=sections,
            correction_provider=correction_provider,
        )
        assert report.destination is not None
        staged_config = Path(report.destination)

        # Only now, after current-loader verification succeeded, touch the old
        # source. Explicit archive paths may live on another filesystem, so use
        # shutil.move for the archive operation.
        archive_directory.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(archive_destination))

        try:
            os.replace(staged_config, source)
        except Exception as replacement_error:
            # Best-effort rollback: restore the legacy source if installing the
            # verified migrated config unexpectedly fails.
            try:
                if not source.exists() and archive_destination.exists():
                    shutil.move(str(archive_destination), str(source))
            except Exception as rollback_error:
                raise RuntimeError(
                    "Failed to install migrated config AND failed to restore the "
                    f"legacy source. Migrated staging file: {staged_config}; "
                    f"archive: {archive_destination}; rollback error: {rollback_error}"
                ) from replacement_error
            raise

        report.destination = source
        return report


def _print_report(report: MigrationReport, *, archived_to: Path | None = None) -> None:
    assert report.destination is not None
    print(f"OK: {report.source.name} -> {report.destination}")
    if archived_to is not None:
        print(f"    legacy archived to: {archived_to}")
    if report.cgh_recomputed_sections:
        print("    recomputed CGH: " + ", ".join(report.cgh_recomputed_sections))
    for warning in report.warnings:
        print(f"    WARNING: {warning}")


def main() -> int:
    try:
        identity, geometry, sections = _validate_user_configuration()
        input_path = _configured_input_path()
        sources = _input_files(input_path)

        archive_mode = bool(MOVE_LEGACY_TO_ARCHIVE)
        if archive_mode:
            archive_directory = _resolve_archive_directory(input_path)
            output_directory = None
        else:
            archive_directory = None
            output_directory = _resolve_output_directory(input_path)
            output_directory.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    succeeded = 0
    failed: list[tuple[Path, Exception]] = []

    print(f"Input:  {input_path}")
    if archive_mode:
        assert archive_directory is not None
        print("Mode:   archive old config + keep migrated config in original location")
        print(f"Archive: {archive_directory}")
    else:
        assert output_directory is not None
        print("Mode:   keep legacy config + write migrated config separately")
        print(f"Output: {output_directory}")
    print(
        f"SLM:    key={identity.key!r}, serial={identity.serial_number!r}, "
        f"geometry={geometry.width}x{geometry.height}, px={geometry.pixel_size_um} um"
    )
    print()

    for source in sources:
        try:
            if archive_mode:
                assert archive_directory is not None
                report = migrate_one_archive_and_replace(
                    source,
                    archive_directory,
                    identity=identity,
                    geometry=geometry,
                    sections=sections,
                    correction_provider=None,
                )
                _print_report(
                    report,
                    archived_to=archive_directory / source.name,
                )
            else:
                assert output_directory is not None
                report = migrate_one(
                    source,
                    output_directory,
                    overwrite=bool(OVERWRITE_EXISTING_MIGRATED),
                    identity=identity,
                    geometry=geometry,
                    sections=sections,
                    correction_provider=None,
                )
                _print_report(report)
            succeeded += 1
        except Exception as error:
            failed.append((source, error))
            print(f"FAILED: {source.name}: {error}", file=sys.stderr)
            if PRINT_TRACEBACKS:
                traceback.print_exc()

    print()
    print(f"Migration complete: {succeeded} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed files:", file=sys.stderr)
        for source, error in failed:
            print(f"  - {source}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
