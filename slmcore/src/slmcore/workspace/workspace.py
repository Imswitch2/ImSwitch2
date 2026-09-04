from __future__ import annotations

from pathlib import Path

from ..core.engine.device import SLMIdentity
from ..core.engine.registry import SLMRegistries
from .calibration_store import SLMCalibrationStore
from .config_store import SLMConfigStore
from .correction_store import SLMCorrectionStore
from .position_reference_store import SLMPositionReferenceStore
from .fov_position_calibration_store import SLMFOVPositionCalibrationStore
from .geometry_orientation_calibration_store import SLMGeometryOrientationCalibrationStore


class SLMWorkspace:
    """Standard slmcore workspace for persisted runtime resources."""

    def __init__(
        self,
        root: str | Path,
        *,
        configs_dir: str | Path | None=None,
        corrections_dir: str | Path | None=None,
        calibrations_dir: str | Path | None=None,
        position_references_dir: str | Path | None=None,
        fov_position_calibrations_dir: str | Path | None=None,
        geometry_orientation_calibrations_dir: str | Path | None=None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.configs_root = self._resolve_override(configs_dir,"configs")
        self.corrections_root = self._resolve_override(corrections_dir,"corrections")
        self.calibrations_root = self._resolve_override(calibrations_dir,"calibrations")
        self.position_references_root = self._resolve_override(
            position_references_dir,"position_references"
        )
        self.fov_position_calibrations_root = self._resolve_override(
            fov_position_calibrations_dir,"fov_position_calibrations"
        )
        self.geometry_orientation_calibrations_root = self._resolve_override(
            geometry_orientation_calibrations_dir,"geometry_orientation_calibrations"
        )
        self._config_stores: dict[tuple[str,int],SLMConfigStore] = {}
        self._correction_stores: dict[tuple[str,str],SLMCorrectionStore] = {}
        self._calibration_store: SLMCalibrationStore | None = None
        self._position_reference_store: SLMPositionReferenceStore | None = None
        self._fov_position_calibration_store: SLMFOVPositionCalibrationStore | None = None
        self._geometry_orientation_calibration_store: SLMGeometryOrientationCalibrationStore | None = None

    @property
    def calibration_store(self) -> SLMCalibrationStore:
        if self._calibration_store is None:
            self._calibration_store = SLMCalibrationStore(self.calibrations_root)
        return self._calibration_store

    @property
    def position_reference_store(self) -> SLMPositionReferenceStore:
        if self._position_reference_store is None:
            self._position_reference_store = SLMPositionReferenceStore(
                self.position_references_root
            )
        return self._position_reference_store

    @property
    def fov_position_calibration_store(self) -> SLMFOVPositionCalibrationStore:
        if self._fov_position_calibration_store is None:
            self._fov_position_calibration_store = SLMFOVPositionCalibrationStore(
                self.fov_position_calibrations_root
            )
        return self._fov_position_calibration_store

    @property
    def geometry_orientation_calibration_store(self) -> SLMGeometryOrientationCalibrationStore:
        if self._geometry_orientation_calibration_store is None:
            self._geometry_orientation_calibration_store = SLMGeometryOrientationCalibrationStore(
                self.geometry_orientation_calibrations_root
            )
        return self._geometry_orientation_calibration_store

    def config_directory(self,identity: SLMIdentity) -> Path:
        return self.configs_root/self._serial(identity)

    def correction_directory(self,identity: SLMIdentity) -> Path:
        return self.corrections_root/self._serial(identity)

    def config_store(
        self,identity: SLMIdentity,registries: SLMRegistries,
    ) -> SLMConfigStore:
        serial = self._serial(identity)
        key = serial,id(registries)
        store = self._config_stores.get(key)
        if store is None:
            store = SLMConfigStore(self.config_directory(identity),registries)
            self._config_stores[key] = store
        return store

    def correction_store(self,identity: SLMIdentity) -> SLMCorrectionStore:
        directory = self.correction_directory(identity)
        directory.mkdir(parents=True,exist_ok=True)
        serial = self._serial(identity)
        cache_key = serial,str(directory.resolve())
        store = self._correction_stores.get(cache_key)
        if store is None:
            store = SLMCorrectionStore(
                identity=identity,
                directory=directory,
                wavelength_table_file="wavelength.json",
            )
            self._correction_stores[cache_key] = store
        return store

    def _resolve_override(
        self,value: str | Path | None,default_name: str,
    ) -> Path:
        path = Path(default_name) if value is None else Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.root/path).resolve()

    @staticmethod
    def _serial(identity: SLMIdentity) -> str:
        if not isinstance(identity,SLMIdentity):
            raise TypeError("identity must be an SLMIdentity")
        return identity.serial_number
