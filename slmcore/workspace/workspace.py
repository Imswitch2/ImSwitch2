from __future__ import annotations

from pathlib import Path
from ..calibration import SLMCalibrationStore
from ..config import SLMConfigRepository
from ..corrections import SLMCorrectionStore
from ..engine.registry import SLMRegistries
from ..host import SLMHostServices
from ..setup import SLMSetup
from .layout import SLMWorkspaceLayout
from .preferences import SLMPreferenceStore


class SLMWorkspace:
    """Reusable persistence and installed-resource context for SLM setups."""

    def __init__(
        self,
        root: str | Path,
        *,
        layout: SLMWorkspaceLayout | None=None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.layout = layout or SLMWorkspaceLayout()
        if not isinstance(self.layout,SLMWorkspaceLayout):
            raise TypeError("layout must be an SLMWorkspaceLayout")
        self._config_repositories: dict[tuple[str,int],SLMConfigRepository] = {}
        self._correction_stores: dict[tuple[str,str,str | None],SLMCorrectionStore] = {}
        self._calibration_store: SLMCalibrationStore | None = None
        self._preference_store: SLMPreferenceStore | None = None

    @property
    def calibration_store(self) -> SLMCalibrationStore:
        if self._calibration_store is None:
            self._calibration_store = SLMCalibrationStore(
                self._resolve(self.layout.calibrations),
            )
        return self._calibration_store

    @property
    def preference_store(self) -> SLMPreferenceStore:
        if self._preference_store is None:
            self._preference_store = SLMPreferenceStore(
                self._resolve(self.layout.preferences),
            )
        return self._preference_store

    def config_directory(self,setup: SLMSetup) -> Path:
        serial = self._serial(setup)
        return self._resolve(self.layout.configs) / serial

    def correction_directory(self,setup: SLMSetup) -> Path | None:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        correction_setup = setup.corrections
        if correction_setup is None:
            return None
        preferred = correction_setup.preferred_directory
        if preferred:
            path = Path(preferred).expanduser()
            if path.is_dir():
                return path
        default = self._resolve(self.layout.corrections) / setup.identity.serial_number
        return default if default.is_dir() else None

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

    def correction_store(self,setup: SLMSetup) -> SLMCorrectionStore | None:
        directory = self.correction_directory(setup)
        if directory is None or setup.corrections is None:
            return None
        table = setup.corrections.wavelength_table_file
        cache_key = (
            setup.identity.serial_number,
            str(directory.resolve()),
            table,
        )
        store = self._correction_stores.get(cache_key)
        if store is None:
            store = SLMCorrectionStore(
                identity=setup.identity,
                directory=directory,
                wavelength_table_file=table,
            )
            self._correction_stores[cache_key] = store
        return store

    def default_host_services(self,setup: SLMSetup) -> SLMHostServices:
        preferences = self.preference_store
        return SLMHostServices(
            configuration_preferences=preferences.configuration_preferences(setup),
            calibration_preferences=preferences.calibration_preferences(setup),
            section_view_preferences=preferences.section_view_preferences(setup),
        )

    def _resolve(self,path: str | Path) -> Path:
        value = Path(path).expanduser()
        return value if value.is_absolute() else self.root / value

    @staticmethod
    def _serial(setup: SLMSetup) -> str:
        if not isinstance(setup,SLMSetup):
            raise TypeError("setup must be an SLMSetup")
        return setup.identity.serial_number
