from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..core.calibration.geometry import (
    CalibrationGeometryMismatch,
    config_calibration_geometry_mismatches,
)
from ..core.config.loading import SLMConfigLoadReport
from ..core.config.model import SLMConfig
from ..core.engine.corrections import ResolvedCorrections
from ..core.engine.runtime import SLMRuntime
from ..core.engine.section import split_layout_signature
from ..workspace.config_store import SLMConfigMetadata,SLMConfigStore
from .runtime_factory import SLMRuntimeFactory


class CalibrationMismatchPolicy(str,Enum):
    REJECT = "reject"
    KEEP = "keep"
    CLEAR = "clear"

    @classmethod
    def normalize(cls,value) -> "CalibrationMismatchPolicy":
        if isinstance(value,cls):
            return value
        try:
            return cls(str(value or cls.REJECT.value).strip().lower())
        except ValueError as error:
            raise ValueError(f"Unknown calibration mismatch policy {value!r}") from error


class CorrectionMismatchPolicy(str,Enum):
    REJECT = "reject"
    USE_CURRENT = "use_current"
    USE_SAVED = "use_saved"

    @classmethod
    def normalize(cls,value) -> "CorrectionMismatchPolicy":
        if isinstance(value,cls):
            return value
        try:
            return cls(str(value or cls.REJECT.value).strip().lower())
        except ValueError as error:
            raise ValueError(f"Unknown correction mismatch policy {value!r}") from error


@dataclass(frozen=True)
class CorrectionMismatch:
    section_key: str
    pattern_changed: bool
    two_pi_changed: bool
    saved: ResolvedCorrections
    current: ResolvedCorrections | None
    current_error: str | None = None

    def summary(self) -> str:
        details = []
        if self.pattern_changed:
            details.append("correction pattern")
        if self.two_pi_changed:
            details.append("2pi value")
        if self.current_error:
            details.append(f"current corrections unavailable: {self.current_error}")
        return f"{self.section_key}: " + ", ".join(details or ("corrections differ",))


@dataclass(frozen=True)
class PreparedConfigLoad:
    path: str
    config: SLMConfig
    metadata: SLMConfigMetadata
    warnings: tuple[Any,...]
    layout_changed: bool
    calibration_mismatches: tuple[CalibrationGeometryMismatch,...]
    correction_mismatches: tuple[CorrectionMismatch,...]
    runtime_layout_signature: Any

    def __post_init__(self) -> None:
        object.__setattr__(self,"warnings",tuple(self.warnings))
        object.__setattr__(self,"calibration_mismatches",tuple(self.calibration_mismatches))
        object.__setattr__(self,"correction_mismatches",tuple(self.correction_mismatches))


@dataclass(frozen=True)
class ConfigLoadCommit:
    runtime: SLMRuntime
    runtime_replaced: bool
    report: SLMConfigLoadReport | None
    failed_section_snapshots: Mapping[str,Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"failed_section_snapshots",MappingProxyType(dict(self.failed_section_snapshots)),
        )


@dataclass(frozen=True)
class ConfigLoadOutcome:
    path: str
    metadata: SLMConfigMetadata
    report: SLMConfigLoadReport | None
    runtime_replaced: bool
    failed_section_snapshots: Mapping[str,Any]
    warnings: tuple[Any,...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"failed_section_snapshots",MappingProxyType(dict(self.failed_section_snapshots)),
        )
        object.__setattr__(self,"warnings",tuple(self.warnings))

    @property
    def frame_changed(self) -> bool:
        return bool(
            self.runtime_replaced
            or (self.report is not None and self.report.frame_changed)
        )


@dataclass(frozen=True)
class StartupRuntime:
    runtime: SLMRuntime
    config_path: str | None
    warnings: tuple[str,...] = ()


class SLMConfigurationService:
    """Toolkit-independent config persistence and runtime restoration service."""

    def __init__(
        self,*,store: SLMConfigStore,runtime_factory: SLMRuntimeFactory,
    ) -> None:
        if not isinstance(store,SLMConfigStore):
            raise TypeError("store must be an SLMConfigStore")
        if not isinstance(runtime_factory,SLMRuntimeFactory):
            raise TypeError("runtime_factory must be an SLMRuntimeFactory")
        self.store = store
        self.runtime_factory = runtime_factory

    @property
    def directory(self):
        return self.store.directory

    def resolve(self,path_or_name) -> str:
        return str(self.store.resolve(path_or_name))

    def prepare_load(self,runtime: SLMRuntime,path: str) -> PreparedConfigLoad:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        config,warnings = self.store.load(path)
        self.runtime_factory.validate_config(config)
        current_signature = _runtime_layout_signature(runtime)
        config_signature = self.runtime_factory.setup.validate_layout(
            config.geometry,
            {key:section.geometry for key,section in config.sections.items()},
        )
        resolved = str(self.store.resolve(path))
        return PreparedConfigLoad(
            path=resolved,
            config=config,
            metadata=self.store.read_metadata(resolved),
            warnings=tuple(warnings),
            layout_changed=config_signature != current_signature,
            calibration_mismatches=config_calibration_geometry_mismatches(config),
            correction_mismatches=self._correction_mismatches(config),
            runtime_layout_signature=current_signature,
        )

    def commit_load(
        self,
        runtime: SLMRuntime,
        prepared: PreparedConfigLoad,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=CalibrationMismatchPolicy.REJECT,
        correction_mismatch_policy: CorrectionMismatchPolicy | str=CorrectionMismatchPolicy.REJECT,
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

        calibration_policy = CalibrationMismatchPolicy.normalize(
            calibration_mismatch_policy,
        )
        correction_policy = CorrectionMismatchPolicy.normalize(
            correction_mismatch_policy,
        )
        config = prepared.config

        if prepared.calibration_mismatches:
            if calibration_policy is CalibrationMismatchPolicy.REJECT:
                raise ValueError(
                    "Config calibration geometry is incompatible with its section layout: "
                    + "; ".join(item.summary() for item in prepared.calibration_mismatches)
                )
            if calibration_policy is CalibrationMismatchPolicy.CLEAR:
                config = _config_with_cleared_calibrations(
                    config,
                    (item.section_key for item in prepared.calibration_mismatches),
                    self.store.registries,
                )

        saved_sections: set[str] = set()
        mismatches = prepared.correction_mismatches
        if mismatches:
            if correction_policy is CorrectionMismatchPolicy.REJECT:
                raise ValueError(
                    "Saved corrections differ from current workspace corrections: "
                    + "; ".join(item.summary() for item in mismatches)
                )
            if correction_policy is CorrectionMismatchPolicy.USE_CURRENT:
                unavailable = [item for item in mismatches if item.current is None]
                if unavailable:
                    raise ValueError(
                        "Current workspace corrections are unavailable: "
                        + "; ".join(item.summary() for item in unavailable)
                    )
            else:
                saved_sections = {item.section_key for item in mismatches}

        if prepared.layout_changed:
            replacement = self.runtime_factory.create_from_config(
                config,saved_correction_sections=saved_sections,
            )
            return ConfigLoadCommit(
                runtime=replacement,
                runtime_replaced=True,
                report=None,
                failed_section_snapshots={},
            )

        report = runtime.load_config(
            config,
            require_complete=bool(require_complete),
            saved_correction_sections=saved_sections,
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

    def create_startup(self,startup_name: str | None) -> StartupRuntime:
        name = str(startup_name or "").strip()
        if not name:
            return StartupRuntime(self.runtime_factory.create_default(),None)
        runtime = self.runtime_factory.create_default()
        try:
            prepared = self.prepare_load(runtime,name)
            commit = self.commit_load(
                runtime,
                prepared,
                calibration_mismatch_policy=CalibrationMismatchPolicy.REJECT,
                correction_mismatch_policy=CorrectionMismatchPolicy.REJECT,
                require_complete=True,
            )
            return StartupRuntime(
                runtime=commit.runtime,
                config_path=prepared.path,
                warnings=tuple(_warning_text(item) for item in prepared.warnings),
            )
        except Exception as error:
            return StartupRuntime(
                runtime=self.runtime_factory.create_default(),
                config_path=None,
                warnings=(f"Startup config '{name}' was not loaded: {error}",),
            )

    def list(self):
        return self.store.list()

    def save_runtime(
        self,runtime: SLMRuntime,name: str,info: str="",*,overwrite: bool=False,
    ) -> SLMConfigMetadata:
        return self.store.save(
            self.store.destination(name),runtime.create_config(),info,overwrite=overwrite,
        )

    def compare_runtime(self,runtime: SLMRuntime,path: str):
        return self.store.compare(path,runtime.create_config())

    def read_metadata(self,path: str) -> SLMConfigMetadata:
        return self.store.read_metadata(path)

    def read_compiled_frame(self,path: str):
        return self.store.read_compiled_frame(path)

    def inspect(self,path: str):
        return self.store.inspect(path)

    def rename(self,source: str,new_name: str,*,overwrite: bool=False):
        return self.store.rename(source,new_name,overwrite=overwrite)

    def duplicate(self,source: str,new_name: str,*,overwrite: bool=False):
        return self.store.duplicate(source,new_name,overwrite=overwrite)

    def delete(self,path: str) -> None:
        self.store.delete(path)

    def _correction_mismatches(self,config: SLMConfig) -> tuple[CorrectionMismatch,...]:
        mismatches = []
        for key,section in config.sections.items():
            corrections = section.state.corrections
            compare_pattern = bool(
                corrections.active and corrections.apply_correction_pattern
            )
            compare_twopi = bool(
                corrections.active and corrections.apply_twopi_value
            )
            if not compare_pattern and not compare_twopi:
                continue
            saved = section.correction_snapshot
            try:
                current = self.runtime_factory.resolve_corrections(
                    int(section.state.optics.wavelength_nm),section.geometry,
                )
                pattern_changed = compare_pattern and not _patterns_equal(
                    saved.correction_pattern,current.correction_pattern,
                )
                two_pi_changed = compare_twopi and (
                    saved.two_pi_value != current.two_pi_value
                )
                error = None
            except Exception as exc:
                current = None
                pattern_changed = compare_pattern
                two_pi_changed = compare_twopi
                error = str(exc)
            if pattern_changed or two_pi_changed:
                mismatches.append(CorrectionMismatch(
                    section_key=key,
                    pattern_changed=pattern_changed,
                    two_pi_changed=two_pi_changed,
                    saved=saved,
                    current=current,
                    current_error=error,
                ))
        return tuple(mismatches)


def _patterns_equal(first,second) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return bool(np.array_equal(first,second))


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


def _warning_text(warning) -> str:
    path = ".".join(getattr(warning,"path",()) or ())
    message = str(getattr(warning,"message",warning))
    return f"{path}: {message}" if path else message


__all__ = [
    "CalibrationMismatchPolicy",
    "ConfigLoadCommit",
    "ConfigLoadOutcome",
    "CorrectionMismatch",
    "CorrectionMismatchPolicy",
    "PreparedConfigLoad",
    "SLMConfigurationService",
    "StartupRuntime",
]
