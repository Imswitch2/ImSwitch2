"""Canonical JSON setup files used by the standalone Qt demo."""

from __future__ import annotations

from pathlib import Path
import shutil

from slmcore import SLMDefinition,SLMStartupPreferences,load_slm_setup_file


_DEFAULT_SETUP_DIR = Path(__file__).with_name("default_setups")


def load_demo_setups(
    data_dir: str | Path,
) -> tuple[tuple[Path,SLMDefinition,SLMStartupPreferences],...]:
    """Seed writable demo setup files, then load them through slmcore."""
    setup_dir = Path(data_dir).expanduser().resolve() / "setups"
    setup_dir.mkdir(parents=True,exist_ok=True)

    loaded = []
    for source in sorted(_DEFAULT_SETUP_DIR.glob("*.json")):
        destination = setup_dir / source.name
        if not destination.exists():
            shutil.copyfile(source,destination)
        definition,hardware,preferences = load_slm_setup_file(destination)
        if hardware is not None:
            raise ValueError(
                "The simulation-only Qt demo does not consume hardware bindings."
            )
        loaded.append((destination,definition,preferences))
    return tuple(loaded)
