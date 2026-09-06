"""Every built-in plugin's headless defaults, codec and version stamp.

``default_params()`` is what a workflow starts from; it is pinned to the
widget so the two cannot drift. The codec must round-trip those defaults
losslessly, and the registry must stamp a version nobody had to remember.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.plugin_versions import imswitch_version, plugin_version  # noqa: E402
from imswitch.improcess.model.provenance import encode_strict  # noqa: E402
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES  # noqa: E402
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES  # noqa: E402
from imswitch.improcess.reconstructors.registry import PluginRegistry  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _plugins():
    for pid, cls in sorted(_AVAILABLE_PROCESSOR_CLASSES.items()):
        yield f"processor:{pid}", cls
    for pid, cls in sorted(_AVAILABLE_RECONSTRUCTOR_CLASSES.items()):
        yield f"reconstructor:{pid}", cls


_IDS = [key for key, _cls in _plugins()]
_CLASSES = [cls for _key, cls in _plugins()]


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_default_params_match_the_widget(cls, qapp):
    plugin = cls()
    parent = QtWidgets.QWidget()
    widget = plugin.make_param_widget(parent)
    defaults = cls.default_params()
    assert isinstance(defaults, dict)
    if widget is None:
        # A plugin driven from a widget it does not own (legacy MoNaLISA).
        return
    getter = getattr(widget, "get_values", None)
    assert callable(getter), f"{cls.__name__}: widget has no get_values()"
    from_widget = getter()
    volatile = set(getattr(cls, "default_params_volatile", ()))
    assert set(from_widget) == set(defaults), f"{cls.__name__}: keys differ"
    for key in defaults:
        if key in volatile:
            continue
        assert from_widget[key] == defaults[key], f"{cls.__name__}: {key!r} differs"


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_defaults_are_lossless_and_round_trip_through_the_codec(cls):
    plugin = cls()
    defaults = cls.default_params()
    encode_strict(defaults)                             # raises if not lossless
    encoded, reasons = plugin.encode_params(defaults)
    assert reasons == []
    assert plugin.decode_params(encoded) == defaults
    assert plugin.migrate_params(encoded, cls.params_version) == encoded
    assert isinstance(cls.params_version, int) and cls.params_version >= 1


def test_the_registry_stamps_a_version_from_the_distribution():
    registry = PluginRegistry()
    for cls in _CLASSES:
        plugin = cls()
        if cls in _AVAILABLE_PROCESSOR_CLASSES.values():
            registry.register_processor(plugin)
        else:
            registry.register_reconstructor(plugin)
        assert plugin.version == imswitch_version()
        assert plugin.version not in ("", "unknown")


def test_a_drop_in_plugin_is_versioned_by_its_file_digest(tmp_path, monkeypatch):
    from imswitch.improcess.plugins import user_plugins

    monkeypatch.setattr(user_plugins, "user_plugins_directory", lambda create=True: str(tmp_path))
    source = tmp_path / "mine.py"
    source.write_text(
        "from imswitch.improcess.processors.base import Processor\n"
        "class Mine(Processor):\n"
        "    id = 'user.mine'\n"
        "    name = 'Mine'\n"
        "    @property\n"
        "    def applies_to(self):\n"
        "        return lambda r: True\n"
        "    def make_param_widget(self, parent):\n"
        "        return None\n"
        "    def apply(self, result, params):\n"
        "        return result\n"
    )
    module = user_plugins._load_module_from_path(str(source))
    first = plugin_version(module.Mine)
    assert first.startswith("file:")
    source.write_text(source.read_text() + "\n# edited\n")
    module2 = user_plugins._load_module_from_path(str(source))
    assert plugin_version(module2.Mine) != first


def test_a_plugin_codec_is_used_by_the_recorder_and_its_failure_is_survived():
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.provenance import output_node
    from imswitch.improcess.processors.base import Processor, normalize_processor_output
    import numpy as np

    class _Custom(Processor):
        id = "t.custom"
        name = "Custom"

        @property
        def applies_to(self):
            return lambda r: True

        def make_param_widget(self, parent):
            return None

        def apply(self, result, params):
            return ArrayProcessingResult("out", np.zeros((2, 2)), ["Y", "X"])

        def encode_params(self, params):
            return {"handle": params["handle"].name}, []

    class _Handle:
        name = "gaussian-3"

    source = ArrayProcessingResult("src", np.zeros((2, 2)), ["Y", "X"])
    out = normalize_processor_output(_Custom().apply(source, {}), source, _Custom(), {"handle": _Handle()})[0]
    node = output_node(out)
    assert node["params"] == {"handle": "gaussian-3"} and node["replayable"] is True

    class _Broken(_Custom):
        id = "t.broken"

        def encode_params(self, params):
            raise RuntimeError("codec bug")

    out = normalize_processor_output(_Broken().apply(source, {}), source, _Broken(), {"x": 1})[0]
    node = output_node(out)
    assert node["params"] == {"x": 1}
    assert any("codec failed" in reason for reason in node["reasons"])
