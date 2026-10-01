"""Drop-in analysis plugin discovery (Picasso-style), slice 1.

A user drops a .py defining a Processor subclass into the plugins directory; at
startup it is discovered, registered and surfaced in every enumeration exactly
like a built-in. Loading is tolerant: a broken file is skipped, never crashing
startup.
"""

import textwrap

import pytest

import imswitch.improcess.plugins as plugins
import imswitch.improcess.processors as processors
from imswitch.improcess.plugins.user_plugins import (
    discover_processor_plugins,
    user_plugins_directory,
)
from imswitch.improcess.reconstructors.registry import PluginRegistry


_VALID_PLUGIN = textwrap.dedent(
    '''
    from imswitch.improcess.processors.base import Processor
    from imswitch.improcess.model.array_result import ArrayProcessingResult


    class InvertProcessor(Processor):
        name = "Invert"
        id = "user.invert"
        category = "User"
        kinds = ("image",)

        @property
        def applies_to(self):
            return lambda result: getattr(result.data, "ndim", 0) >= 2

        def make_param_widget(self, parent):
            from qtpy import QtWidgets

            widget = QtWidgets.QWidget(parent)
            widget.get_values = lambda: {}
            return widget

        def apply(self, result, params):
            return ArrayProcessingResult(
                name=f"{result.name} (inverted)",
                data=result.data.max() - result.data,
                axis_labels=list(result.axis_labels),
            )
    '''
)


def _write(directory, name, source):
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_user_plugins():
    """Keep the module-level user-plugin tables from leaking between tests."""
    plugins.clear_user_plugins()
    yield
    plugins.clear_user_plugins()


# --- discovery -------------------------------------------------------------


def test_discovers_valid_processor(tmp_path):
    _write(tmp_path, "invert.py", _VALID_PLUGIN)

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert set(classes) == {"user.invert"}
    assert classes["user.invert"].name == "Invert"
    assert errors == []


def test_broken_plugin_is_skipped_not_raised(tmp_path):
    _write(tmp_path, "invert.py", _VALID_PLUGIN)
    _write(tmp_path, "broken.py", "this is not valid python !!!\n")

    classes, errors = discover_processor_plugins(str(tmp_path))

    # The good plugin still loads; the broken one is recorded, not raised.
    assert set(classes) == {"user.invert"}
    assert len(errors) == 1
    assert errors[0].path.endswith("broken.py")


def test_underscore_files_are_ignored(tmp_path):
    _write(tmp_path, "_private.py", "raise RuntimeError('must not import')\n")

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert classes == {}
    assert errors == []


def test_file_without_processor_is_reported(tmp_path):
    _write(tmp_path, "empty.py", "X = 1\n")

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert classes == {}
    assert len(errors) == 1
    assert "No Processor subclass" in errors[0].message


def test_processor_without_id_is_skipped(tmp_path):
    _write(
        tmp_path,
        "noid.py",
        textwrap.dedent(
            """
            from imswitch.improcess.processors.base import Processor

            class NoId(Processor):
                id = ""
                @property
                def applies_to(self):
                    return lambda r: True
                def make_param_widget(self, parent):
                    return None
                def apply(self, result, params):
                    return result
            """
        ),
    )

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert classes == {}
    assert any("has no 'id'" in e.message for e in errors)


def test_imported_processor_is_not_re_collected(tmp_path):
    """A plugin importing a built-in Processor must not re-register it: only
    classes *defined in* the plugin module count."""
    _write(
        tmp_path,
        "reimport.py",
        "from imswitch.improcess.processors.segmentation import SegmentationProcessor\n",
    )

    classes, errors = discover_processor_plugins(str(tmp_path))

    assert classes == {}
    assert len(errors) == 1  # "no Processor subclass defined here"


# --- registration / enumeration integration --------------------------------


def test_loaded_plugin_surfaces_in_enumeration_and_registers(tmp_path):
    _write(tmp_path, "invert.py", _VALID_PLUGIN)

    loaded, errors = processors.load_user_plugins(str(tmp_path))
    assert loaded == ["user.invert"]

    specs = {pid: (name, cat) for pid, name, cat in processors.available_processor_specs()}
    assert specs["user.invert"] == ("Invert", "User")
    assert "user.invert" in processors.available_processor_ids()

    registry = PluginRegistry()
    processors.register_default_processors(registry, None)
    assert registry.get_processor("user.invert", raise_on_missing=False) is not None


def test_user_plugin_active_even_under_config_filter(tmp_path):
    """Built-ins are gated by config; user plugins are gated by presence."""
    _write(tmp_path, "invert.py", _VALID_PLUGIN)
    processors.load_user_plugins(str(tmp_path))

    registry = PluginRegistry()
    processors.register_default_processors(registry, ["projection"])  # excludes user id

    assert registry.get_processor("projection", raise_on_missing=False) is not None
    assert registry.get_processor("user.invert", raise_on_missing=False) is not None


def test_register_processor_by_id_handles_user_plugin(tmp_path):
    _write(tmp_path, "invert.py", _VALID_PLUGIN)
    processors.load_user_plugins(str(tmp_path))

    registry = PluginRegistry()
    plugin = processors.register_processor_by_id(registry, "user.invert")
    assert plugin.id == "user.invert"


def test_builtin_id_collision_keeps_builtin(tmp_path):
    """A user plugin that reuses a built-in id is rejected; the built-in wins."""
    _write(
        tmp_path,
        "shadow.py",
        _VALID_PLUGIN.replace('"user.invert"', '"segmentation"').replace(
            "InvertProcessor", "ShadowProcessor"
        ),
    )

    loaded, errors = processors.load_user_plugins(str(tmp_path))

    assert loaded == []  # not loaded
    assert any("collides with a built-in" in e.message for e in errors)
    # The enumeration's 'segmentation' entry is still the built-in.
    specs = {pid: name for pid, name, _cat in processors.available_processor_specs()}
    assert specs["segmentation"] == "Segmentation"


def test_clear_user_plugins_resets_enumeration(tmp_path):
    _write(tmp_path, "invert.py", _VALID_PLUGIN)
    processors.load_user_plugins(str(tmp_path))
    assert "user.invert" in processors.available_processor_ids()

    processors.clear_user_plugins()
    assert "user.invert" not in processors.available_processor_ids()


# --- directory + template --------------------------------------------------


def test_user_plugins_directory_creates_inert_template(tmp_path, monkeypatch):
    import imswitch.improcess.plugins.user_plugins as up

    monkeypatch.setattr(up.dirtools.UserFileDirs, "Root", str(tmp_path))
    directory = up.user_plugins_directory(create=True)

    template = tmp_path / "improcess_plugins" / "_example_plugin.py"
    assert template.exists()

    # The template is underscore-prefixed, so discovery skips it (it never
    # registers the example processor by merely existing).
    classes, errors = discover_processor_plugins(directory)
    assert classes == {}
    assert errors == []
