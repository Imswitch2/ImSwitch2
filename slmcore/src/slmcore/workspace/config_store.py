"""HDF5 persistence and file management for complete SLM configurations."""

from __future__ import annotations

import os
import pprint
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Mapping,Sequence

import h5py
import numpy as np

from ..core.engine.registry import SLMRegistries
from ..core.engine.state import ConfigWarning
from ..core.engine.device import SLMGeometry,SLMIdentity
from ._hdf5 import read_value,write_value
from ..core.config.model import SLM_CONFIG_SCHEMA_VERSION,SLMCompiledFrame,SLMConfig


PathLike = str | os.PathLike

SLM_CONFIG_FILE_TYPE = "slm_config"
CONFIG_GROUP_NAME = "config"

_FILE_TYPE_ATTR = "file_type"
_CREATED_AT_ATTR = "created_at"
_INFO_ATTR = "info"
_SUPPORTED_EXTENSIONS = (".h5",".hdf5")
_MAX_DIFF_LINES = 500


@dataclass(frozen=True)
class SLMConfigMetadata:
    """Lightweight information readable without reconstructing a runtime."""

    path: Path
    created_at: str = ""
    info: str = ""
    schema_version: int | None = None
    slm_key: str | None = None
    serial_number: str | None = None
    section_keys: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class SLMConfigInspection:
    """Human-readable inspection result for one complete configuration."""

    metadata: SLMConfigMetadata
    summary: str
    warnings: tuple[ConfigWarning, ...] = ()


class _ConfigFileStore:
    """Save, load, inspect and manage complete SLM configurations."""

    def save(
        self,
        path: PathLike,
        config: SLMConfig,
        info: str | None=None,
        *,
        overwrite: bool=True,
    ) -> SLMConfigMetadata:
        """Atomically save a complete SLM configuration.

        Parameters
        ----------
        overwrite:
            Keep the historical store behavior by default. User-facing
            ``Save as`` operations should pass ``False`` explicitly.
        """
        path = self._normalize_path(path)
        self._validate_save_path(path,overwrite=overwrite)

        if not isinstance(config,SLMConfig):
            raise TypeError(
                f"config must be an SLMConfig, got "
                f"{type(config).__name__}"
            )

        if config.schema_version != SLM_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Cannot save SLM config schema version "
                f"{config.schema_version}; expected "
                f"{SLM_CONFIG_SCHEMA_VERSION}"
            )

        if info is not None and not isinstance(info,str):
            raise TypeError(
                f"info must be a string or None, got "
                f"{type(info).__name__}"
            )

        temporary_path = self._create_temporary_path(path)

        try:
            self._write_file(temporary_path,config,info)
            os.replace(str(temporary_path),str(path))
        except Exception:
            self._remove_temporary_file(temporary_path)
            raise

        return self.read_metadata(path)

    def load(
        self,
        path: PathLike,
        registries: SLMRegistries,
    ) -> tuple[SLMConfig, tuple[ConfigWarning, ...]]:
        """Load and reconstruct a complete SLM configuration."""
        path = self._normalize_path(path)
        self._validate_load_path(path)

        raw = self._read_config_dict(path)
        return SLMConfig.from_dict(raw,registries)

    def read_metadata(self,path: PathLike) -> SLMConfigMetadata:
        """Read envelope and identity metadata without loading large arrays."""
        path = self._normalize_path(path)
        self._validate_load_path(path)

        with h5py.File(str(path),"r") as file:
            self._validate_file_type(
                file.attrs.get(_FILE_TYPE_ATTR),path,
            )
            if CONFIG_GROUP_NAME not in file:
                raise ValueError(
                    f"SLM config file '{path}' is missing "
                    f"'/{CONFIG_GROUP_NAME}'"
                )

            config_group = file[CONFIG_GROUP_NAME]
            if not isinstance(config_group,h5py.Group):
                raise ValueError(
                    f"SLM config file '{path}' contains a non-group "
                    f"'/{CONFIG_GROUP_NAME}' value"
                )

            created_at = self._attribute_text(
                file.attrs.get(_CREATED_AT_ATTR,""),
                _CREATED_AT_ATTR,
            )
            info = self._attribute_text(
                file.attrs.get(_INFO_ATTR,""),
                _INFO_ATTR,
            )

            schema_version = None
            if (
                "schema_version" in config_group
                or "schema_version" in config_group.attrs
            ):
                schema_version = int(read_value(
                    config_group,"schema_version",
                ))

            identity = {}
            if "identity" in config_group:
                value = read_value(config_group,"identity")
                if isinstance(value,Mapping):
                    identity = value

            section_keys = ()
            sections_node = config_group.get("sections")
            if isinstance(sections_node,h5py.Group):
                section_keys = tuple(str(key) for key in sections_node.keys())

        serial = identity.get("serial_number")
        return SLMConfigMetadata(
            path=path,
            created_at=created_at,
            info=info,
            schema_version=schema_version,
            slm_key=(
                None if identity.get("key") is None
                else str(identity.get("key"))
            ),
            serial_number=None if serial is None else str(serial),
            section_keys=section_keys,
        )

    def read_compiled_frame(self,path: PathLike) -> SLMCompiledFrame:
        """Read only the persisted identity, geometry and final uint8 frame.

        This intentionally bypasses registry-aware section reconstruction and
        config migration. Fast activation therefore accepts only current
        complete HDF5 configs that already contain ``final_eightbit``.
        """
        path = self._normalize_path(path)
        self._validate_load_path(path)

        with h5py.File(str(path),"r") as file:
            self._validate_file_type(file.attrs.get(_FILE_TYPE_ATTR),path)
            config_group = file.get(CONFIG_GROUP_NAME)
            if not isinstance(config_group,h5py.Group):
                raise ValueError(
                    "SLM config file '%s' is missing '/%s'"
                    % (path,CONFIG_GROUP_NAME)
                )

            if "schema_version" not in config_group and (
                "schema_version" not in config_group.attrs
            ):
                raise ValueError("SLM config is missing schema_version")
            version = int(read_value(config_group,"schema_version"))
            if version != SLM_CONFIG_SCHEMA_VERSION:
                raise ValueError(
                    "Fast config activation requires schema version %d; got %d"
                    % (SLM_CONFIG_SCHEMA_VERSION,version)
                )

            for name in ("identity","geometry","final_eightbit"):
                if name not in config_group and name not in config_group.attrs:
                    raise ValueError(
                        "SLM config file '%s' is missing '/%s/%s'"
                        % (path,CONFIG_GROUP_NAME,name)
                    )

            identity = SLMIdentity.from_dict(read_value(config_group,"identity"))
            geometry = SLMGeometry.from_dict(read_value(config_group,"geometry"))
            frame = np.asarray(read_value(config_group,"final_eightbit"))

        return SLMCompiledFrame(
            identity=identity,
            geometry=geometry,
            final_eightbit=frame,
        )

    def list_configs(self,directory: PathLike) -> tuple[SLMConfigMetadata, ...]:
        """Return valid complete configs in deterministic filename order."""
        directory = self._normalize_directory(directory)
        metadata = []

        for path in sorted(
            (
                item for item in directory.iterdir()
                if item.is_file()
                and item.suffix.lower() in _SUPPORTED_EXTENSIONS
            ),
            key=lambda item:item.name.casefold(),
        ):
            try:
                metadata.append(self.read_metadata(path))
            except (OSError,ValueError,TypeError):
                # A config directory may contain unrelated or damaged HDF5
                # files. They are deliberately excluded from the selector.
                continue

        return tuple(metadata)

    def inspect(
        self,
        path: PathLike,
        registries: SLMRegistries,
    ) -> SLMConfigInspection:
        """Load one config and return a bounded human-readable summary."""
        metadata = self.read_metadata(path)
        config,warnings = self.load(metadata.path,registries)
        summary_data = self._summarize_value(config.to_dict())
        summary = pprint.pformat(
            summary_data,width=100,sort_dicts=False,compact=False,
        )
        return SLMConfigInspection(
            metadata=metadata,summary=summary,warnings=warnings,
        )

    def compare(
        self,
        path: PathLike,
        config: SLMConfig,
        registries: SLMRegistries,
    ) -> str:
        """Return a concise path-oriented difference against a saved config."""
        if not isinstance(config,SLMConfig):
            raise TypeError(
                f"config must be an SLMConfig, got "
                f"{type(config).__name__}"
            )

        saved,_warnings = self.load(path,registries)
        lines: list[str] = []
        self._append_diff(
            saved.to_dict(),config.to_dict(),(),lines,
        )

        if not lines:
            return "No changes."
        if len(lines) > _MAX_DIFF_LINES:
            omitted = len(lines) - _MAX_DIFF_LINES
            lines = lines[:_MAX_DIFF_LINES]
            lines.append(f"... {omitted} additional change(s) omitted")
        return "\n".join(lines)

    def rename(
        self,
        source: PathLike,
        destination: PathLike,
        *,
        overwrite: bool=False,
    ) -> SLMConfigMetadata:
        """Rename one validated complete config file."""
        source = self._normalize_path(source)
        destination = self._normalize_path(destination)
        self.read_metadata(source)

        if source.absolute() == destination.absolute():
            return self.read_metadata(source)

        self._validate_save_path(destination,overwrite=overwrite)
        if overwrite:
            os.replace(str(source),str(destination))
        else:
            source.rename(destination)
        return self.read_metadata(destination)

    def duplicate(
        self,
        source: PathLike,
        destination: PathLike,
        *,
        overwrite: bool=False,
    ) -> SLMConfigMetadata:
        """Copy one validated complete config, preserving file metadata."""
        source = self._normalize_path(source)
        destination = self._normalize_path(destination)
        self.read_metadata(source)
        self._validate_save_path(destination,overwrite=overwrite)
        shutil.copy2(str(source),str(destination))
        return self.read_metadata(destination)

    def delete(self,path: PathLike) -> None:
        """Delete one validated complete config file."""
        path = self._normalize_path(path)
        self.read_metadata(path)
        path.unlink()

    # ---- writing ---- #

    def _write_file(
        self,
        path: Path,
        config: SLMConfig,
        info: str | None,
    ) -> None:
        """Write the HDF5 envelope and serialized config."""
        with h5py.File(str(path),"w") as file:
            file.attrs[_FILE_TYPE_ATTR] = SLM_CONFIG_FILE_TYPE
            file.attrs[_CREATED_AT_ATTR] = self._created_at()

            if info is not None:
                file.attrs[_INFO_ATTR] = info

            write_value(file,CONFIG_GROUP_NAME,config.to_dict())

    def _create_temporary_path(self,path: Path) -> Path:
        """Create a unique sibling path for an atomic save."""
        descriptor,temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        os.close(descriptor)
        return Path(temporary_name)

    def _remove_temporary_file(self,path: Path) -> None:
        """Remove a temporary file without masking the original error."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # ---- reading ---- #

    def _read_config_dict(self,path: Path) -> dict[str, Any]:
        """Read and validate the HDF5 envelope and config dictionary."""
        with h5py.File(str(path),"r") as file:
            self._validate_file_type(
                file.attrs.get(_FILE_TYPE_ATTR),path,
            )

            if CONFIG_GROUP_NAME not in file:
                raise ValueError(
                    f"SLM config file '{path}' is missing "
                    f"'/{CONFIG_GROUP_NAME}'"
                )

            raw = read_value(file,CONFIG_GROUP_NAME)

        if not isinstance(raw,dict):
            raise ValueError(
                f"SLM config file '{path}' contains a non-mapping "
                f"'/{CONFIG_GROUP_NAME}' value"
            )

        return raw

    def _validate_file_type(self,value: object,path: Path) -> None:
        """Reject files that are not complete SLM config files."""
        if value is None:
            raise ValueError(
                f"HDF5 file '{path}' is missing root attribute "
                f"'{_FILE_TYPE_ATTR}'"
            )

        value = self._attribute_text(value,_FILE_TYPE_ATTR)
        if value != SLM_CONFIG_FILE_TYPE:
            raise ValueError(
                f"Unsupported HDF5 file type {value!r} in '{path}'; "
                f"expected {SLM_CONFIG_FILE_TYPE!r}"
            )

    # ---- path helpers ---- #

    def _normalize_path(self,path: PathLike) -> Path:
        """Normalize a filesystem path without resolving symlinks."""
        try:
            normalized = Path(path).expanduser()
        except TypeError as error:
            raise TypeError(
                f"path must be a string or path-like object, got "
                f"{type(path).__name__}"
            ) from error

        if not normalized.name:
            raise ValueError("Config path must include a file name")

        return normalized

    def _normalize_directory(self,directory: PathLike) -> Path:
        try:
            normalized = Path(directory).expanduser()
        except TypeError as error:
            raise TypeError(
                f"directory must be a string or path-like object, got "
                f"{type(directory).__name__}"
            ) from error
        if not normalized.exists():
            raise FileNotFoundError(
                f"SLM config directory does not exist: '{normalized}'"
            )
        if not normalized.is_dir():
            raise NotADirectoryError(
                f"SLM config directory is not a directory: '{normalized}'"
            )
        return normalized

    def _validate_save_path(self,path: Path,*,overwrite: bool) -> None:
        if path.exists() and path.is_dir():
            raise IsADirectoryError(
                f"SLM config path is a directory: '{path}'"
            )
        if path.exists() and not overwrite:
            raise FileExistsError(
                f"SLM config file already exists: '{path}'"
            )
        if not path.parent.exists():
            raise FileNotFoundError(
                f"SLM config directory does not exist: "
                f"'{path.parent}'"
            )
        if not path.parent.is_dir():
            raise NotADirectoryError(
                f"SLM config parent is not a directory: "
                f"'{path.parent}'"
            )

    def _validate_load_path(self,path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(
                f"SLM config file does not exist: '{path}'"
            )
        if not path.is_file():
            raise IsADirectoryError(
                f"SLM config path is not a file: '{path}'"
            )

    # ---- summaries and differences ---- #

    def _summarize_value(self,value: Any) -> Any:
        if isinstance(value,np.ndarray):
            summary = {
                "type":"ndarray",
                "shape":tuple(int(item) for item in value.shape),
                "dtype":str(value.dtype),
            }
            if value.size and value.dtype.kind in "biufc":
                summary["min"] = self._scalar(value.min())
                summary["max"] = self._scalar(value.max())
            return summary
        if isinstance(value,Mapping):
            return {
                str(key):self._summarize_value(item)
                for key,item in value.items()
            }
        if isinstance(value,(list,tuple)):
            return [self._summarize_value(item) for item in value]
        return self._scalar(value)

    def _append_diff(
        self,
        old: Any,
        new: Any,
        path: tuple[str, ...],
        lines: list[str],
    ) -> None:
        if isinstance(old,np.ndarray) or isinstance(new,np.ndarray):
            if not isinstance(old,np.ndarray) or not isinstance(new,np.ndarray):
                lines.append(
                    f"{self._format_path(path)}: "
                    f"{self._short_value(old)} -> {self._short_value(new)}"
                )
                return
            if np.array_equal(old,new):
                return
            lines.append(
                f"{self._format_path(path)}: array changed "
                f"{old.shape}/{old.dtype} -> {new.shape}/{new.dtype}"
            )
            return

        if isinstance(old,Mapping) and isinstance(new,Mapping):
            old_keys = list(old)
            new_keys = list(new)
            for key in old_keys:
                if key not in new:
                    lines.append(
                        f"{self._format_path(path + (str(key),))}: removed"
                    )
            for key in new_keys:
                if key not in old:
                    lines.append(
                        f"{self._format_path(path + (str(key),))}: added "
                        f"{self._short_value(new[key])}"
                    )
                else:
                    self._append_diff(
                        old[key],new[key],path + (str(key),),lines,
                    )
            return

        if (
            isinstance(old,(list,tuple))
            and isinstance(new,(list,tuple))
        ):
            if len(old) != len(new):
                lines.append(
                    f"{self._format_path(path)}: length "
                    f"{len(old)} -> {len(new)}"
                )
            for index,(old_item,new_item) in enumerate(zip(old,new)):
                self._append_diff(
                    old_item,new_item,path + (str(index),),lines,
                )
            return

        if self._scalar(old) != self._scalar(new):
            lines.append(
                f"{self._format_path(path)}: "
                f"{self._short_value(old)} -> {self._short_value(new)}"
            )

    @staticmethod
    def _format_path(path: Sequence[str]) -> str:
        return ".".join(path) if path else "<root>"

    def _short_value(self,value: Any) -> str:
        if isinstance(value,np.ndarray):
            return f"array(shape={value.shape}, dtype={value.dtype})"
        text = repr(self._summarize_value(value))
        return text if len(text) <= 160 else text[:157] + "..."

    @staticmethod
    def _scalar(value: Any) -> Any:
        return value.item() if isinstance(value,np.generic) else value

    @staticmethod
    def _attribute_text(value: object,name: str) -> str:
        if isinstance(value,bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"HDF5 attribute '{name}' is not valid UTF-8"
                ) from error
        if isinstance(value,str):
            return value
        if value is None:
            return ""
        return str(value)

    def _created_at(self) -> str:
        """Return the current UTC timestamp for file metadata."""
        return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CONFIG_GROUP_NAME",
    "SLM_CONFIG_FILE_TYPE",
    "SLMConfigInspection",
    "SLMConfigMetadata",
    "SLMConfigStore",
]


class SLMConfigStore:
    """Directory-bound persistence store for complete SLM configurations."""

    def __init__(self,directory: PathLike,registries: SLMRegistries) -> None:
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.registries = registries
        self._files = _ConfigFileStore()

    def resolve(self,path_or_name: Any) -> Path:
        path = Path(path_or_name).expanduser()
        if not path.is_absolute():
            path = self.directory / path
        return path

    def destination(self,name: str) -> Path:
        filename = os.path.basename(str(name or "").strip())
        if not filename:
            raise ValueError("Config name cannot be empty")
        if Path(filename).suffix.lower() not in _SUPPORTED_EXTENSIONS:
            filename += ".h5"
        return self.directory / filename

    def list(self) -> tuple[SLMConfigMetadata,...]:
        return self._files.list_configs(self.directory)

    def load(self,path_or_name):
        return self._files.load(self.resolve(path_or_name),self.registries)

    def read_metadata(self,path_or_name) -> SLMConfigMetadata:
        return self._files.read_metadata(self.resolve(path_or_name))

    def read_compiled_frame(self,path_or_name) -> SLMCompiledFrame:
        return self._files.read_compiled_frame(self.resolve(path_or_name))

    def inspect(self,path_or_name) -> SLMConfigInspection:
        return self._files.inspect(self.resolve(path_or_name),self.registries)

    def compare(self,path_or_name,config: SLMConfig):
        return self._files.compare(self.resolve(path_or_name),config,self.registries)

    def save(self,name_or_path,config: SLMConfig,info: str="",*,overwrite: bool=False):
        path = self.resolve(name_or_path)
        if path.parent == self.directory and not path.suffix:
            path = self.destination(path.name)
        return self._files.save(path,config,info,overwrite=overwrite)

    def rename(self,source,new_name: str,*,overwrite: bool=False):
        return self._files.rename(
            self.resolve(source),self.destination(new_name),overwrite=overwrite,
        )

    def duplicate(self,source,new_name: str,*,overwrite: bool=False):
        return self._files.duplicate(
            self.resolve(source),self.destination(new_name),overwrite=overwrite,
        )

    def delete(self,path_or_name) -> None:
        self._files.delete(self.resolve(path_or_name))
