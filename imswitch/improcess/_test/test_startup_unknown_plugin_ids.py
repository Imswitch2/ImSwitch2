"""ImProcess starts when the setup names a plugin id this computer lacks.

A setup can list a drop-in plugin that lives on another computer, a typo, or
a plugin from a newer ImSwitch. Startup used to raise ``KeyError`` from
``register_default_*`` and ImProcess did not open at all. The controller now
skips unknown ids with one warning each; the ``register_*`` functions stay
strict on purpose (``test_dropin_reconstructors.py``).
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from imswitch.improcess import plugins as plugins_package
from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
from imswitch.improcess.model.processing_config import plugin_ids_from_config
from imswitch.improcess.processors import (
    _USER_PROCESSOR_CLASSES, _all_processor_classes, register_default_processors,
)
from imswitch.improcess.reconstructors import register_default_reconstructors
from imswitch.improcess.reconstructors.registry import get_registry


class _Logger:
    def __init__(self):
        self.infos, self.warnings = [], []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def debug(self, *args, **kwargs):
        pass


@pytest.fixture
def start(monkeypatch):
    """``start(config)`` runs the real ``_initialize_plugins`` on a stand-in
    controller and returns ``(registry, logger)``."""
    monkeypatch.setattr(
        plugins_package, "load_user_plugins",
        lambda *a, **k: SimpleNamespace(ids=[], processors=[], reconstructors=[], errors=[]),
    )
    registry = get_registry()

    def run(config):
        registry.clear()
        logger = _Logger()
        controller = SimpleNamespace(
            _ImProcessMainController__processingConfig=config,
            _ImProcessMainController__logger=logger,
        )
        controller._known_plugin_ids = ImProcessMainController._known_plugin_ids.__get__(controller)
        ImProcessMainController._initialize_plugins(controller)
        return registry, logger

    yield run
    registry.clear()


def _ids(registry):
    return (
        sorted(p.id for p in registry.reconstructors()),
        sorted(p.id for p in registry.processors()),
    )


def test_unknown_ids_are_skipped_with_a_warning_each_and_the_rest_register(start):
    registry, logger = start({
        "reconstructors": ["view-only", "nope"],
        "processors": ["drift-correct", "nope2"],
    })
    assert _ids(registry) == (["view-only"], ["drift-correct"])
    assert len(logger.warnings) == 2
    first, second = logger.warnings
    assert "processing.reconstructors" in first and "'nope'" in first
    assert "view-only" in first, "the available ids are named"
    assert "processing.processors" in second and "'nope2'" in second
    assert "drift-correct" in second


def test_only_unknown_reconstructors_fall_back_to_the_standalone_default(start):
    registry, logger = start({"reconstructors": ["nope"], "processors": ["drift-correct"]})
    default = plugin_ids_from_config({})[0]
    assert _ids(registry)[0] == sorted(default)
    assert any("processing.reconstructors" in w and "'nope'" in w for w in logger.warnings)
    assert any("default" in w and str(default) in w for w in logger.warnings)


def test_unknown_processors_only_leave_the_processors_empty(start):
    registry, logger = start({"reconstructors": ["view-only"], "processors": ["nope"]})
    assert _ids(registry) == (["view-only"], [])
    assert len(logger.warnings) == 1


def test_an_empty_reconstructor_list_is_not_replaced_by_the_default(start):
    registry, logger = start({"reconstructors": [], "processors": []})
    assert _ids(registry) == ([], [])
    assert logger.warnings == []


def test_known_ids_and_standalone_defaults_log_no_warning(start):
    for config in ({"reconstructors": ["view-only", "monalisa"], "processors": ["resize"]}, {}):
        registry, logger = start(config)
        assert logger.warnings == []
        assert registry.reconstructors()


def test_a_listed_id_that_is_not_a_list_or_not_a_string_does_not_raise(start):
    registry, logger = start({"reconstructors": "view-only", "processors": [["x"], 3, "resize"]})
    assert _ids(registry)[0] == ["view-only"], "the default stands in for an unusable list"
    assert _ids(registry)[1] == ["resize"]
    assert any("must be a list" in w for w in logger.warnings)
    assert sum("processing.processors" in w for w in logger.warnings) == 2


def test_a_drop_in_processor_in_the_user_table_counts_as_known(start, monkeypatch):
    builtin = _all_processor_classes()["drift-correct"]
    monkeypatch.setitem(_USER_PROCESSOR_CLASSES, "user.copy", builtin)
    _, logger = start({"reconstructors": ["view-only"], "processors": ["user.copy"]})
    assert logger.warnings == []


def test_the_register_functions_stay_strict():
    registry = get_registry()
    with pytest.raises(KeyError):
        register_default_reconstructors(registry, ["nope"])
    with pytest.raises(KeyError):
        register_default_processors(registry, ["nope"])


# ── the order of the list decides what ImProcess starts with ───────────────
# The config editor's "Starts with" combo writes the chosen reconstructor
# first, and adds no key of its own: this is the ImProcess side of that
# contract. The registry keeps registration order, registration follows the
# setup's list, the manager activates the first registered one, and the
# Parameters-dock picker opens on the active one.
def _startup_surface(qapp):
    """The real manager methods over a real combo, as ``initActiveReconstructor`` runs them."""
    from PyQt5.QtWidgets import QComboBox

    from imswitch.improcess.controller.ReconstructorManagerController import (
        ReconstructorManagerController as Manager,
    )
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    view = SimpleNamespace(_activeReconstructorCombo=QComboBox(),
                           _updateParametersDockTitle=lambda: None)
    view.setReconstructorChoices = ImProcessMainView.setReconstructorChoices.__get__(view)
    manager = SimpleNamespace(
        _main=SimpleNamespace(_activeReconstructor=None, _currentDataObj=None),
        _widget=view, _logger=_Logger(),
        _install_reconstructor_params=lambda reconstructor: None,
    )
    for name in ("initActiveReconstructor", "_select_reconstructor",
                 "_publishReconstructorChoices", "_offerable",
                 "_accepts_current_source", "_reopen_path_for"):
        setattr(manager, name, getattr(Manager, name).__get__(manager))
    return manager, view._activeReconstructorCombo


@pytest.mark.parametrize("listed", [["view-only", "monalisa"], ["monalisa", "view-only"]])
def test_the_first_reconstructor_listed_is_the_one_active_at_startup(start, qapp, listed):
    registry, logger = start({"reconstructors": listed, "processors": []})
    assert [p.id for p in registry.reconstructors()] == listed, "registration follows the list"
    assert logger.warnings == []

    manager, picker = _startup_surface(qapp)
    manager.initActiveReconstructor()
    assert manager._main._activeReconstructor.id == listed[0]
    assert [picker.itemData(i) for i in range(picker.count())] == listed
    assert picker.currentData() == listed[0], "the picker opens on the active one"


def test_an_unknown_first_id_hands_the_start_to_the_first_one_this_computer_has(start, qapp):
    registry, _ = start({"reconstructors": ["nope", "monalisa", "view-only"], "processors": []})
    manager, picker = _startup_surface(qapp)
    manager.initActiveReconstructor()
    assert manager._main._activeReconstructor.id == "monalisa"
    assert picker.currentData() == "monalisa"
