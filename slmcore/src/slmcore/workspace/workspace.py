from __future__ import annotations

from pathlib import Path

from ..core.engine.device import SLMIdentity
from ..core.engine.registry import SLMRegistries
from .calibration_store import SLMCalibrationStore
from .config_store import SLMConfigStore
from .correction_store import SLMCorrectionStore
from .fov_mapping_correction_store import FOVMappingCorrectionStore
from .geometry_orientation_calibration_store import SLMGeometryOrientationCalibrationStore
from .mapping_store import MappingStore
from .observation_plane_store import ObservationPlaneStore
from .observation_registration_store import ObservationRegistrationStore
from .position_reference_store import PositionReferenceStore


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
        fov_mapping_corrections_dir: str | Path | None=None,
        geometry_orientation_calibrations_dir: str | Path | None=None,
        observation_planes_dir: str | Path | None=None,
        observation_registration_calibrations_dir: str | Path | None=None,
        mappings_dir: str | Path | None=None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.configs_root = self._resolve_override(configs_dir,"configs")
        self.corrections_root = self._resolve_override(corrections_dir,"corrections")
        self.calibrations_root = self._resolve_override(calibrations_dir,"calibrations")
        self.position_references_root = self._resolve_override(
            position_references_dir,"position_references"
        )
        self.fov_mapping_corrections_root = self._resolve_override(
            fov_mapping_corrections_dir,"fov_mapping_corrections"
        )
        self.geometry_orientation_calibrations_root = self._resolve_override(
            geometry_orientation_calibrations_dir,"geometry_orientation_calibrations"
        )
        self.observation_planes_root = self._resolve_override(observation_planes_dir,".")
        self.observation_registration_calibrations_root = self._resolve_override(
            observation_registration_calibrations_dir,"observation_registration_calibrations"
        )
        self.mappings_root = self._resolve_override(mappings_dir,".")
        self._config_stores: dict[tuple[str,int],SLMConfigStore] = {}
        self._correction_stores: dict[tuple[str,str],SLMCorrectionStore] = {}
        self._calibration_store: SLMCalibrationStore | None = None
        self._fov_mapping_correction_store: FOVMappingCorrectionStore | None = None
        self._geometry_orientation_calibration_store: SLMGeometryOrientationCalibrationStore | None = None
        self._observation_plane_store: ObservationPlaneStore | None = None
        self._observation_registration_store: ObservationRegistrationStore | None = None
        self._position_reference_store: PositionReferenceStore | None = None
        self._mapping_store: MappingStore | None = None

    @property
    def calibration_store(self) -> SLMCalibrationStore:
        if self._calibration_store is None:
            self._calibration_store = SLMCalibrationStore(self.calibrations_root)
        return self._calibration_store


    @property
    def mapping_store(self) -> MappingStore:
        if self._mapping_store is None:
            self._mapping_store = MappingStore(self.mappings_root)
        return self._mapping_store

    @property
    def fov_mapping_correction_store(self) -> FOVMappingCorrectionStore:
        if self._fov_mapping_correction_store is None:
            self._fov_mapping_correction_store = FOVMappingCorrectionStore(
                self.fov_mapping_corrections_root
            )
        return self._fov_mapping_correction_store

    @property
    def observation_plane_store(self) -> ObservationPlaneStore:
        if self._observation_plane_store is None:
            self._observation_plane_store = ObservationPlaneStore(self.observation_planes_root)
        return self._observation_plane_store

    @property
    def position_reference_store(self) -> PositionReferenceStore:
        if self._position_reference_store is None:
            self._position_reference_store = PositionReferenceStore(
                self.position_references_root
            )
        return self._position_reference_store

    @property
    def observation_registration_store(self) -> ObservationRegistrationStore:
        if self._observation_registration_store is None:
            self._observation_registration_store = ObservationRegistrationStore(
                self.observation_registration_calibrations_root
            )
        return self._observation_registration_store

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
