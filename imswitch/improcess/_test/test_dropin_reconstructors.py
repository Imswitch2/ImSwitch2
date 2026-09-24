"""Drop-in plugins can be reconstructors, added to a running ImProcess.

A ``.py`` file in the plugins folder that defines a ``Reconstructor`` subclass
is discovered like a processor plugin and joins the reconstructor enumeration,
so the registry, the Parameters-dock picker, the config editor and headless
workflows all see it. A reload is exact about what changed: an edited plugin
is swapped in, an unchanged one keeps its instance, a removed one drops out
and an active one that vanished hands over -- and nothing is touched while a
reconstruction is running. *Add plugin file...* copies a chosen file into the
folder and reloads.
"""

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
import pytest

import imswitch.improcess.plugins as plugins
import imswitch.improcess.processors as processors
import imswitch.improcess.reconstructors as reconstructors
import imswitch.improcess.plugins.user_plugins as up
from imswitch.improcess.controller.ImProcessMainController import (
    ImProcessMainController as Controller,
)
from imswitch.improcess.controller.ReconstructorManagerController import (
    ReconstructorManagerController as Manager,
)
from imswitch.improcess.model.plugin_contract import has_param_contract
from imswitch.improcess.plugins.user_plugins import (
    discover_plugins,
    discover_processor_plugins,
    install_plugin_files,
)
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.registry import PluginRegistry
from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "improcess_plugins"

_RECONSTRUCTOR_PLUGIN = textwrap.dedent(
    '''
    import numpy as np

    from imswitch.improcess.reconstructors.base import Reconstructor
    from imswitch.improcess.model.array_result import ArrayProcessingResult


    class MeanReconstructor(Reconstructor):
        name = "Mean of frames"
        id = "user.mean"
        file_extensions = ["hdf5", "tiff"]

        @classmethod
        def default_params(cls):
            return {}

        def make_param_widget(self, parent):
            from qtpy import QtWidgets

            widget = QtWidgets.QWidget(parent)
            widget.get_values = lambda: {}
            return widget

        def make_metadata_dialog(self, parent):
            return None

        def process(self, data_obj, params, context=None):
            data_obj.checkAndLoadData()
            data = np.asarray(data_obj.data, dtype=np.float32)
            return ArrayProcessingResult(
                name=f"{data_obj.name} (mean)",
                data=data.reshape(-1, *data.shape[-2:]).mean(axis=0),
                axis_labels=["Y", "X"],
            )
    '''
)

_BOTH_KINDS_PLUGIN = _RECONSTRUCTOR_PLUGIN + textwrap.dedent(
    '''

    from imswitch.improcess.processors.base import Processor


    class InvertProcessor(Processor):
        name = "Invert"
        id = "user.invert"
        category = "User"
        kinds = ("image",)

        @classmethod
        def default_params(cls):
            return {}

        @property
        def applies_to(self):
            return lambda result: getattr(result.data, "ndim", 0) >= 2

        def make_param_widget(self, parent):
            return None

        def apply(self, result, params):
            return result
    '''
)


def _write(directory, name, source):
    path = Path(directory) / name
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_user_plugins():
    """Keep the module-level user-plugin tables from leaking between tests."""
    plugins.clear_user_plugins()
    yield
    plugins.clear_user_plugins()


class _Stub(Reconstructor):
    """A concrete reconstructor for registry-level tests."""

    name = "Stub"
    id = "stub"

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        return None


def _stub(plugin_id, name=None):
    return type(
        "Stub_" + plugin_id.replace(".", "_"),
        (_Stub,),
        {"id": plugin_id, "name": name or plugin_id},
    )()


# --- discovery -------------------------------------------------------------


def test_discovers_reconstructor_plugin(tmp_path):
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)

    found = discover_plugins(str(tmp_path))

    assert set(found.reconstructors) == {"user.mean"}
    assert issubclass(found.reconstructors["user.mean"], Reconstructor)
    assert found.reconstructors["user.mean"].name == "Mean of frames"
    assert found.processors == {}
    assert found.errors == []


def test_reconstructor_only_file_is_not_an_error_for_processor_callers(tmp_path):
    """The processor-only view of the scan must not flag a reconstructor
    file as 'defines nothing'."""
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert classes == {}
    assert errors == []


def test_one_file_may_define_both_kinds(tmp_path):
    _write(tmp_path, "both.py", _BOTH_KINDS_PLUGIN)

    found = discover_plugins(str(tmp_path))

    assert set(found.reconstructors) == {"user.mean"}
    assert set(found.processors) == {"user.invert"}
    assert found.errors == []


def test_file_defining_nothing_is_reported_once(tmp_path):
    _write(tmp_path, "empty.py", "X = 1\n")

    found = discover_plugins(str(tmp_path))

    assert found.processors == {} and found.reconstructors == {}
    assert len(found.errors) == 1
    assert "No Processor subclass" in found.errors[0].message
    assert "Reconstructor subclass" in found.errors[0].message


def test_reconstructor_without_id_is_skipped(tmp_path):
    _write(
        tmp_path,
        "noid.py",
        _RECONSTRUCTOR_PLUGIN.replace('id = "user.mean"', 'id = ""'),
    )

    found = discover_plugins(str(tmp_path))

    assert found.reconstructors == {}
    assert any("has no 'id'" in e.message for e in found.errors)


def test_duplicate_reconstructor_id_keeps_the_first(tmp_path):
    _write(tmp_path, "a_mean.py", _RECONSTRUCTOR_PLUGIN)
    _write(
        tmp_path,
        "b_mean.py",
        _RECONSTRUCTOR_PLUGIN.replace('name = "Mean of frames"', 'name = "Second"'),
    )

    found = discover_plugins(str(tmp_path))

    assert found.reconstructors["user.mean"].name == "Mean of frames"
    assert any("Duplicate plugin reconstructor id" in e.message for e in found.errors)


# --- enumeration / registration --------------------------------------------


def test_loaded_reconstructor_surfaces_in_enumeration_and_registers(tmp_path):
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)

    loaded = plugins.load_user_plugins(str(tmp_path))

    assert loaded.reconstructors == ["user.mean"]
    assert loaded.processors == []
    assert loaded.errors == []
    assert loaded.ids == ["user.mean"]
    # The merged enumeration (what the config editor offers) has it; the
    # built-in list does not.
    assert "user.mean" in reconstructors.available_reconstructor_ids()
    assert "user.mean" not in reconstructors.builtin_reconstructor_ids()

    registry = PluginRegistry()
    reconstructors.register_default_reconstructors(registry, None)
    plugin = registry.get_reconstructor("user.mean")
    assert plugin.name == "Mean of frames"
    # Versioned by a digest of the file, like a drop-in processor.
    assert plugin.version.startswith("file:")


def test_user_reconstructor_active_even_under_config_filter(tmp_path):
    """Built-ins are gated by config; user reconstructors by presence."""
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    plugins.load_user_plugins(str(tmp_path))

    registry = PluginRegistry()
    reconstructors.register_default_reconstructors(registry, ["view-only"])

    assert registry.get_reconstructor("view-only", raise_on_missing=False) is not None
    assert registry.get_reconstructor("user.mean", raise_on_missing=False) is not None

    # A config that names the user id explicitly is accepted too.
    registry = PluginRegistry()
    reconstructors.register_default_reconstructors(registry, ["user.mean"])
    assert [r.id for r in registry.reconstructors()] == ["user.mean"]


def test_register_reconstructor_by_id_handles_user_plugin_and_unknown(tmp_path):
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    plugins.load_user_plugins(str(tmp_path))

    registry = PluginRegistry()
    plugin = reconstructors.register_reconstructor_by_id(registry, "user.mean")
    assert plugin.id == "user.mean"
    assert registry.get_reconstructor("user.mean") is plugin
    with pytest.raises(KeyError, match="Unknown reconstructor id"):
        reconstructors.register_reconstructor_by_id(registry, "user.nope")


def test_builtin_id_collision_keeps_builtin(tmp_path):
    _write(
        tmp_path,
        "shadow.py",
        _RECONSTRUCTOR_PLUGIN.replace('"user.mean"', '"view-only"'),
    )

    loaded = plugins.load_user_plugins(str(tmp_path))

    assert loaded.reconstructors == []
    assert any("collides with a built-in" in e.message for e in loaded.errors)
    assert reconstructors._all_reconstructor_classes()["view-only"] is ViewOnlyReconstructor


def test_processor_loader_fills_the_reconstructor_table_too(tmp_path):
    """One scan serves both tables, whichever entry point ran it."""
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)

    loaded_processors, errors = processors.load_user_plugins(str(tmp_path))

    assert loaded_processors == [] and errors == []
    assert "user.mean" in reconstructors.available_reconstructor_ids()


def test_clear_user_plugins_resets_both_tables(tmp_path):
    _write(tmp_path, "both.py", _BOTH_KINDS_PLUGIN)
    plugins.load_user_plugins(str(tmp_path))
    assert "user.mean" in reconstructors.available_reconstructor_ids()
    assert "user.invert" in processors.available_processor_ids()

    plugins.clear_user_plugins()

    assert "user.mean" not in reconstructors.available_reconstructor_ids()
    assert "user.invert" not in processors.available_processor_ids()


def test_removed_file_drops_out_on_the_next_scan(tmp_path):
    path = _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    plugins.load_user_plugins(str(tmp_path))
    path.unlink()

    loaded = plugins.load_user_plugins(str(tmp_path))

    assert loaded.reconstructors == []
    assert "user.mean" not in reconstructors.available_reconstructor_ids()


# --- registry --------------------------------------------------------------


def test_registry_unregister_reconstructor():
    registry = PluginRegistry()
    plugin = _stub("user.a")
    registry.register_reconstructor(plugin)

    assert registry.unregister_reconstructor("user.a") is plugin
    assert registry.get_reconstructor("user.a", raise_on_missing=False) is None
    assert registry.unregister_reconstructor("user.a") is None


# --- workflows -------------------------------------------------------------


def test_bootstrap_registry_sees_dropin_reconstructors(tmp_path, monkeypatch):
    from imswitch.improcess.workflows.runtime import BootstrapError, bootstrap_registry

    monkeypatch.setattr(up.dirtools.UserFileDirs, "Root", str(tmp_path))
    folder = Path(up.user_plugins_directory(create=True))
    _write(folder, "mean.py", _RECONSTRUCTOR_PLUGIN)

    registry = bootstrap_registry()
    plugin = registry.get_reconstructor("user.mean")
    assert plugin.version.startswith("file:")

    narrowed = bootstrap_registry(reconstructor_ids=["user.mean"])
    assert [r.id for r in narrowed.reconstructors()] == ["user.mean"]
    with pytest.raises(BootstrapError, match="unknown reconstructor"):
        bootstrap_registry(reconstructor_ids=["user.nope"])


# --- reload: the manager re-syncs the active reconstructor -----------------


def _manager(active, registry, monkeypatch):
    """The controller's own methods over a minimal surface."""
    monkeypatch.setattr(
        "imswitch.improcess.reconstructors.registry.get_registry", lambda: registry
    )
    installed, published = [], []
    manager = SimpleNamespace(
        _main=SimpleNamespace(_activeReconstructor=active),
        _logger=SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
        _install_reconstructor_params=lambda r: installed.append(r),
        _publishReconstructorChoices=lambda: published.append(True),
    )
    manager._select_reconstructor = lambda: Manager._select_reconstructor(manager)
    return manager, installed, published


def test_reload_keeps_an_untouched_active_reconstructor(monkeypatch):
    registry = PluginRegistry()
    builtin, user = _stub("view-only"), _stub("user.mean")
    registry.register_reconstructor(builtin)
    registry.register_reconstructor(user)
    manager, installed, published = _manager(builtin, registry, monkeypatch)

    Manager.pluginsReloaded(manager)

    assert manager._main._activeReconstructor is builtin
    assert installed == []          # no widget rebuild for an untouched one
    assert published == [True]      # the picker is refreshed regardless


def test_reload_swaps_in_the_fresh_instance_of_the_active_plugin(monkeypatch):
    registry = PluginRegistry()
    old, new = _stub("user.mean", "v1"), _stub("user.mean", "v2")
    registry.register_reconstructor(new)     # the reload re-registered it
    manager, installed, published = _manager(old, registry, monkeypatch)

    Manager.pluginsReloaded(manager)

    assert manager._main._activeReconstructor is new
    assert installed == [new]       # parameter widget rebuilt from the new code
    assert published == [True]


def test_reload_falls_back_when_the_active_plugin_was_removed(monkeypatch):
    registry = PluginRegistry()
    builtin = _stub("view-only")
    registry.register_reconstructor(builtin)
    gone = _stub("user.mean")
    manager, installed, published = _manager(gone, registry, monkeypatch)

    Manager.pluginsReloaded(manager)

    assert manager._main._activeReconstructor is builtin
    assert installed == [builtin]
    assert published == [True]


def test_reload_selects_a_reconstructor_when_none_was_active(monkeypatch):
    registry = PluginRegistry()
    user = _stub("user.mean")
    registry.register_reconstructor(user)
    manager, installed, _published = _manager(None, registry, monkeypatch)

    Manager.pluginsReloaded(manager)

    assert manager._main._activeReconstructor is user
    assert installed == [user]


def test_reload_with_an_empty_registry_leaves_nothing_active(monkeypatch):
    manager, installed, published = _manager(_stub("user.mean"), PluginRegistry(), monkeypatch)

    Manager.pluginsReloaded(manager)

    assert manager._main._activeReconstructor is None
    assert installed == []
    assert published == [True]


# --- reload: the main controller brings the registry in line ---------------


def _controller(*, running=False, live_running=False):
    """The controller's own methods over a minimal surface."""
    status, warnings, reloaded = [], [], []
    stub = SimpleNamespace()
    stub._ImProcessMainController__logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda message, *a, **k: warnings.append(message),
        exception=lambda *a, **k: None,
    )
    stub._ImProcessMainController__mainView = SimpleNamespace(
        showStatusMessage=lambda message, **k: status.append(message)
    )
    stub.mainViewController = SimpleNamespace(
        reconstructorManager=SimpleNamespace(
            isReconstructionRunning=lambda: running,
            pluginsReloaded=lambda: reloaded.append(True),
        ),
        liveModeController=SimpleNamespace(
            isLiveReconstructionRunning=lambda: live_running
        ),
    )
    stub._reconstruction_in_progress = (
        lambda: Controller._reconstruction_in_progress(stub)
    )
    stub._show_status_message = lambda m: Controller._show_status_message(stub, m)
    return stub, status, warnings, reloaded


def _registry_with(tmp_path, *, filter_ids=("view-only",)):
    """Load the folder and register as startup would."""
    plugins.load_user_plugins(str(tmp_path))
    registry = PluginRegistry()
    reconstructors.register_default_reconstructors(registry, list(filter_ids))
    return registry


def test_controller_reload_registers_new_and_unregisters_removed(tmp_path):
    path = _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    registry = _registry_with(tmp_path)
    assert {r.id for r in registry.reconstructors()} == {"view-only", "user.mean"}

    path.unlink()
    _write(
        tmp_path,
        "other.py",
        _RECONSTRUCTOR_PLUGIN.replace('"user.mean"', '"user.other"'),
    )
    loaded = plugins.load_user_plugins(str(tmp_path))
    stub, _status, _warnings, reloaded = _controller()

    assert Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)

    assert {r.id for r in registry.reconstructors()} == {"view-only", "user.other"}
    assert reloaded == [True]       # the manager re-synced the picker


def test_controller_reload_keeps_unchanged_and_swaps_edited(tmp_path):
    path = _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    registry = _registry_with(tmp_path)
    before = registry.get_reconstructor("user.mean")
    stub, _status, _warnings, _reloaded = _controller()

    # Same bytes on disk: the instance (and with it any widget state) stays.
    loaded = plugins.load_user_plugins(str(tmp_path))
    Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)
    assert registry.get_reconstructor("user.mean") is before

    # An edit is a new version: the instance is replaced by one of the new code.
    path.write_text(
        _RECONSTRUCTOR_PLUGIN.replace('name = "Mean of frames"', 'name = "Mean v2"'),
        encoding="utf-8",
    )
    loaded = plugins.load_user_plugins(str(tmp_path))
    Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)
    after = registry.get_reconstructor("user.mean")
    assert after is not before
    assert after.name == "Mean v2"
    assert after.version != before.version


def test_controller_reload_is_refused_while_a_reconstruction_runs(tmp_path):
    path = _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    registry = _registry_with(tmp_path)
    path.unlink()
    loaded = plugins.load_user_plugins(str(tmp_path))
    stub, status, warnings, reloaded = _controller(running=True)

    assert not Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)

    # Nothing touched: the job's plugin is still registered, the manager was
    # not asked to re-sync, and the user was told in the status bar.
    assert {r.id for r in registry.reconstructors()} == {"view-only", "user.mean"}
    assert reloaded == []
    assert status and "reconstruction is running" in status[0]
    assert warnings and "reconstruction is running" in warnings[0]


def test_a_live_reconstruction_counts_as_running(tmp_path):
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    registry = _registry_with(tmp_path)
    loaded = plugins.load_user_plugins(str(tmp_path))
    stub, _status, _warnings, reloaded = _controller(live_running=True)

    assert not Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)
    assert reloaded == []


def test_controller_reload_survives_a_missing_view_controller(tmp_path):
    """Before the view controller exists (or on a bare stub) the registry is
    still brought in line; only the re-sync is skipped."""
    _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    registry = PluginRegistry()
    loaded = plugins.load_user_plugins(str(tmp_path))
    stub, _status, _warnings, _reloaded = _controller()
    del stub.mainViewController

    assert Controller._reload_user_reconstructors(stub, registry, loaded.reconstructors)
    assert registry.get_reconstructor("user.mean", raise_on_missing=False) is not None


# --- Add plugin file... ------------------------------------------------------


def test_install_plugin_files_copies_and_reports(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    folder = tmp_path / "plugins"
    folder.mkdir()
    good = _write(source_dir, "mean.py", _RECONSTRUCTOR_PLUGIN)
    inert = _write(source_dir, "_draft.py", "X = 1\n")
    notes = _write(source_dir, "notes.txt", "not python\n")

    installed, skipped = install_plugin_files(
        [str(good), str(inert), str(notes)], str(folder)
    )

    assert installed == [str(folder / "mean.py")]
    assert (folder / "mean.py").read_text(encoding="utf-8") == _RECONSTRUCTOR_PLUGIN
    assert len(skipped) == 2
    assert any("underscore-prefixed" in reason for reason in skipped)
    assert any("not a .py file" in reason for reason in skipped)

    # An existing file is left alone unless the caller says otherwise.
    edited = _write(source_dir, "mean.py", _RECONSTRUCTOR_PLUGIN + "\n# edited\n")
    installed, skipped = install_plugin_files([str(edited)], str(folder))
    assert installed == [] and "not replaced" in skipped[0]
    installed, skipped = install_plugin_files(
        [str(edited)], str(folder), overwrite=lambda name: True
    )
    assert installed == [str(folder / "mean.py")] and skipped == []
    assert (folder / "mean.py").read_text(encoding="utf-8").endswith("# edited\n")

    # Picking a file that already lives in the folder is not an overwrite.
    installed, skipped = install_plugin_files(
        [str(folder / "mean.py")], str(folder), overwrite=lambda name: True
    )
    assert installed == [] and "already in the plugins folder" in skipped[0]


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def test_add_plugin_file_action_copies_and_reloads(qapp, tmp_path, monkeypatch):
    from qtpy import QtWidgets

    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    monkeypatch.setattr(up.dirtools.UserFileDirs, "Root", str(tmp_path))
    source = _write(tmp_path, "mean.py", _RECONSTRUCTOR_PLUGIN)
    picked = [str(source)]
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: (list(picked), "Python plugin (*.py)")),
    )
    messages = []
    view = QtWidgets.QMainWindow()
    view.sigReloadPluginsRequested = _Signal()
    view.showStatusMessage = lambda message, **k: messages.append(message)
    view._confirmPluginOverwrite = lambda name: True

    ImProcessMainView._addPluginFiles(view)

    target = tmp_path / "improcess_plugins" / "mean.py"
    assert target.read_text(encoding="utf-8") == _RECONSTRUCTOR_PLUGIN
    assert view.sigReloadPluginsRequested.emitted == [()]
    assert messages and "Added plugin file(s): mean.py" in messages[0]

    # Cancelling the dialog does nothing at all.
    picked.clear()
    ImProcessMainView._addPluginFiles(view)
    assert view.sigReloadPluginsRequested.emitted == [()]
    assert len(messages) == 1


# --- template + shipped example --------------------------------------------


def test_template_is_inert_but_defines_both_kinds_once_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(up.dirtools.UserFileDirs, "Root", str(tmp_path))
    directory = up.user_plugins_directory(create=True)

    # Underscore-prefixed: discovery skips it.
    found = discover_plugins(directory)
    assert found.processors == {} and found.reconstructors == {}
    assert found.errors == []

    # Renamed, as the instructions say, it is a working plugin of each kind.
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    template = Path(directory) / "_example_plugin.py"
    (copy_dir / "example_copy.py").write_text(
        template.read_text(encoding="utf-8"), encoding="utf-8"
    )
    found = discover_plugins(str(copy_dir))
    assert found.errors == []
    assert set(found.processors) == {"user.invert-example"}
    assert set(found.reconstructors) == {"user.mean-frames-example"}
    assert all(has_param_contract(cls) for cls in found.reconstructors.values())


def test_frame_average_example_reconstructs(tmp_path):
    from imswitch.improcess.model import DataObj, result_kind

    found = discover_plugins(str(_EXAMPLES_DIR))
    assert found.errors == []
    cls = found.reconstructors["example.frame-average"]
    assert has_param_contract(cls)
    assert cls.default_params() == {"frames": 0}

    path = tmp_path / "stack.h5"
    data = np.arange(4 * 6 * 8, dtype=np.uint16).reshape(4, 6, 8)
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=data)
    data_obj = DataObj("stack.h5", "CAM", path=str(path))

    out = cls().process(data_obj, {"frames": 0})
    assert result_kind(out) == "image"
    assert out.axis_labels == ["Y", "X"]
    np.testing.assert_allclose(out.data, data.mean(axis=0))

    first_two = cls().process(data_obj, {"frames": 2})
    np.testing.assert_allclose(first_two.data, data[:2].mean(axis=0))
