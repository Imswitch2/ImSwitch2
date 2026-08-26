from __future__ import annotations

from pathlib import Path

from ..calibration import SLMCalibrationStore
from ..config import SLMConfigRepository
from ..corrections import SLMCorrectionStore
from ..engine.registry import SLMRegistries
from ..setup import SLMSetup


class SLMWorkspace:
    """Standard slmcore persistence/resource workspace.

    By default slmcore owns the layout below ``root``::

        configs/<serial>/
        corrections/<serial>/
        calibrations/

    Hosts normally provide only ``root``. The optional directory overrides are
    intended for integrations with an existing filesystem layout; relative
    overrides are resolved below ``root`` and absolute overrides are used as-is.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        configs_dir: str | Path | None=None,
        corrections_dir: str | Path | None=None,
        calibrations_dir: str | Path | None=None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.configs_root = self._resolve_override(configs_dir,"configs")
        self.corrections_root = self._resolve_override(corrections_dir,"corrections")
        self.calibrations_root = self._resolve_override(calibrations_dir,"calibrations")
        self._config_repositories: dict[tuple[str,int],SLMConfigRepository] = {}
        self._correction_stores: dict[tuple[str,str],SLMCorrectionStore] = {}
        self._calibration_store: SLMCalibrationStore | None = None

    @property
    def calibration_store(self) -> SLMCalibrationStore:
        if self._calibration_store is None:
            self._calibration_store = SLMCalibrationStore(self.calibrations_root)
        return self._calibration_store

    def config_directory(self,setup: SLMSetup) -> Path:
        return self.configs_root / self._serial(setup)

    def correction_directory(self,setup: SLMSetup) -> Path:
        return self.corrections_root / self._serial(setup)

    def config_repository(
        self,setup: SLMSetup,registries: SLMRegistries,
    ) -> SLMConfigRepository:
        serial = self._serial(setup)
        key = serial,id(registries)
        repository = self._config_repositories.get(key)
        if repository is None:
            repository = SLMConfigRepository(
                self.config_directory(setup),registries,
            )
            self._config_repositories[key] = repository
        return repository

    def correction_store(self,setup: SLMSetup) -> SLMCorrectionStore:
        directory = self.correction_directory(setup)
        directory.mkdir(parents=True,exist_ok=True)
        serial = self._serial(setup)
        cache_key = serial,str(directory.resolve())
        store = self._correction_stores.get(cache_key)
        if store is None:
            store = SLMCorrectionStore(
                identity=setup.identity,
                directory=directory,
                wavelength_table_file="wavelength.json",
            )
            self._correction_stores[cache_key] = store
        return store

    def _resolve_override(
        self,value: str | Path | None,default_name: str,
    ) -> Path:
        path = Path(default_name) if value is None else Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    @staticmethod
    def _serial(setup: SLMSetup) -> str:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        return setup.identity.serial_number
