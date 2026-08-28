from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any,Mapping

from ..calibration.geometry import (
    CalibrationGeometryMismatch,
    config_calibration_geometry_mismatches,
)
from ..config.loading import SLMConfigLoadReport
from ..config.model import SLMConfig
from ..config.repository import SLMConfigRepository
from ..config.store import SLMConfigMetadata
from ..engine.runtime import SLMRuntime
from ..engine.section import split_layout_signature
from .runtime_factory import SLMRuntimeFactory


class CalibrationMismatchPolicy(str,Enum):
    """Headless policy for incompatible stored calibration geometry."""

    REJECT = "reject"
    KEEP = "keep"
    CLEAR = "clear"

    @classmethod
    def normalize(cls,value) -> "CalibrationMismatchPolicy":
        if isinstance(value,cls):
            return value
        text = str(value or cls.REJECT.value).strip().lower()
        try:
            return cls(text)
        except ValueError as error:
            raise ValueError(
                "Unknown calibration mismatch policy %r" % value
            ) from error


@dataclass(frozen=True)
class PreparedConfigLoad:
    """Validated, side-effect-free description of one requested config load."""

    path: str
    config: SLMConfig
    metadata: SLMConfigMetadata
    warnings: tuple[Any,...]
    layout_changed: bool
    calibration_mismatches: tuple[CalibrationGeometryMismatch,...]
    runtime_layout_signature: Any

    def __post_init__(self) -> None:
        object.__setattr__(self,"warnings",tuple(self.warnings))
        object.__setattr__(
            self,"calibration_mismatches",tuple(self.calibration_mismatches),
        )


@dataclass(frozen=True)
class ConfigLoadCommit:
    """Core result produced before a session publishes/announces a config load."""

    runtime: SLMRuntime
    runtime_replaced: bool
    report: SLMConfigLoadReport | None
    failed_section_snapshots: Mapping[str,Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_section_snapshots",
            MappingProxyType(dict(self.failed_section_snapshots)),
        )


@dataclass(frozen=True)
class ConfigLoadOutcome:
    """Authoritative application result of a committed config load."""

    path: str
    metadata: SLMConfigMetadata
    report: SLMConfigLoadReport | None
    runtime_replaced: bool
    failed_section_snapshots: Mapping[str,Any]
    warnings: tuple[Any,...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_section_snapshots",
            MappingProxyType(dict(self.failed_section_snapshots)),
        )
        object.__setattr__(self,"warnings",tuple(self.warnings))

    @property
    def frame_changed(self) -> bool:
        return bool(
            self.runtime_replaced
            or (self.report is not None and self.report.frame_changed)
        )


class SLMConfigurationService:
    """Toolkit-independent config persistence and runtime restoration service."""

    def __init__(
        self,
        *,
        repository: SLMConfigRepository,
        runtime_factory: SLMRuntimeFactory,
    ) -> None:
        if not isinstance(repository,SLMConfigRepository):
            raise TypeError("repository must be an SLMConfigRepository")
        if not isinstance(runtime_factory,SLMRuntimeFactory):
            raise TypeError("runtime_factory must be an SLMRuntimeFactory")
        self.repository = repository
        self.runtime_factory = runtime_factory

    @property
    def directory(self):
        return self.repository.directory

    def prepare_load(self,runtime: SLMRuntime,path: str) -> PreparedConfigLoad:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        config,warnings = self.repository.load(path)
        self.runtime_factory.validate_config(config)
        current_signature = _runtime_layout_signature(runtime)
        config_signature = self.runtime_factory.setup.validate_layout(
            config.geometry,
            {key:section.geometry for key,section in config.sections.items()},
        )
        resolved = str(self.repository.resolve(path))
        return PreparedConfigLoad(
            path=resolved,
            config=config,
            metadata=self.repository.read_metadata(resolved),
            warnings=tuple(warnings),
            layout_changed=config_signature != current_signature,
            calibration_mismatches=config_calibration_geometry_mismatches(config),
            runtime_layout_signature=current_signature,
        )

    def commit_load(
        self,
        runtime: SLMRuntime,
        prepared: PreparedConfigLoad,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        require_complete: bool=False,
    ) -> ConfigLoadCommit:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        if not isinstance(prepared,PreparedConfigLoad):
            raise TypeError("prepared must be a PreparedConfigLoad")
        if _runtime_layout_signature(runtime) != prepared.runtime_layout_signature:
            raise RuntimeError(
                "Runtime layout changed after the config load was prepared; prepare it again"
            )

        policy = CalibrationMismatchPolicy.normalize(calibration_mismatch_policy)
        config = prepared.config
        mismatches = prepared.calibration_mismatches
        if mismatches:
            if policy is CalibrationMismatchPolicy.REJECT:
                raise ValueError(
                    "Config calibration geometry is incompatible with its section layout: "
                    + "; ".join(item.summary() for item in mismatches)
                )
            if policy is CalibrationMismatchPolicy.CLEAR:
                config = _config_with_cleared_calibrations(
                    config,
                    (item.section_key for item in mismatches),
                    self.repository.registries,
                )

        if prepared.layout_changed:
            replacement = self.runtime_factory.create_from_config(config)
            return ConfigLoadCommit(
                runtime=replacement,
                runtime_replaced=True,
                report=None,
                failed_section_snapshots={},
            )

        report = runtime.load_config(
            config,require_complete=bool(require_complete),
        )
        failed_snapshots = {
            key:runtime.get_section_snapshot(key)
            for key in report.failed_sections
        }
        return ConfigLoadCommit(
            runtime=runtime,
            runtime_replaced=False,
            report=report,
            failed_section_snapshots=failed_snapshots,
        )

    def list(self):
        return self.repository.list()

    def save_runtime(
        self,
        runtime: SLMRuntime,
        name: str,
        info: str="",
        *,
        overwrite: bool=False,
    ) -> SLMConfigMetadata:
        return self.repository.save(
            self.repository.destination(name),
            runtime.create_config(),
            info,
            overwrite=overwrite,
        )

    def compare_runtime(self,runtime: SLMRuntime,path: str):
        return self.repository.compare(path,runtime.create_config())

    def read_metadata(self,path: str) -> SLMConfigMetadata:
        return self.repository.read_metadata(path)

    def read_compiled_frame(self,path: str):
        return self.repository.read_compiled_frame(path)

    def inspect(self,path: str):
        return self.repository.inspect(path)

    def rename(self,source: str,new_name: str,*,overwrite: bool=False):
        return self.repository.rename(source,new_name,overwrite=overwrite)

    def duplicate(self,source: str,new_name: str,*,overwrite: bool=False):
        return self.repository.duplicate(source,new_name,overwrite=overwrite)

    def delete(self,path: str) -> None:
        self.repository.delete(path)


def _runtime_layout_signature(runtime: SLMRuntime):
    return split_layout_signature(
        runtime.geometry,
        {key:runtime.get_section_geometry(key) for key in runtime.section_keys},
    )


def _config_with_cleared_calibrations(config: SLMConfig,keys,registries) -> SLMConfig:
    clear = set(keys)
    sections = {}
    for key,section in config.sections.items():
        clone = section.clone(registries)
        if key in clear:
            clone.calibration = None
        sections[key] = clone
    return SLMConfig(
        schema_version=config.schema_version,
        identity=config.identity,
        geometry=config.geometry,
        sections=sections,
        final_eightbit=config.final_eightbit,
    )


__all__ = [
    "CalibrationMismatchPolicy",
    "ConfigLoadCommit",
    "ConfigLoadOutcome",
    "PreparedConfigLoad",
    "SLMConfigurationService",
]
