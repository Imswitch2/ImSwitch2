"""Canonical JSON setup files used by the standalone Qt demo."""

from __future__ import annotations

from pathlib import Path
from slmcore import SLMDefinition,SLMStartupPreferences,load_slm_setup_file


_DEFAULT_SETUP_DIR = Path(__file__).with_name("default_setups")


def load_demo_setups(
) -> tuple[tuple[SLMDefinition, SLMStartupPreferences], ...]:
    """Load the bundled demo setup files directly."""
    loaded: list[tuple[SLMDefinition, SLMStartupPreferences]] = []

    for source in sorted(_DEFAULT_SETUP_DIR.glob("*.json")):
        definition, _hardware, preferences = load_slm_setup_file(source)
        loaded.append((definition, preferences))

    return tuple(loaded)