"""The shipped example drop-in plugins load and apply correctly.

Guards examples/improcess_plugins/ so the reference plugins we tell users to
copy never drift from the Processor contract.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.model import ArrayProcessingResult, result_kind
from imswitch.improcess.model.plugin_contract import check_plugin_contract
from imswitch.improcess.plugins.user_plugins import discover_processor_plugins
from imswitch.improcess.processors.base import Processor

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "improcess_plugins"


def _image(shape=(6, 8)):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(name="img", data=data, axis_labels=["Y", "X"])


def test_examples_directory_exists():
    assert _EXAMPLES_DIR.is_dir()
    assert (_EXAMPLES_DIR / "invert.py").exists()
    assert (_EXAMPLES_DIR / "gaussian_blur.py").exists()
    assert (_EXAMPLES_DIR / "photophysics_suite.py").exists()
    assert (_EXAMPLES_DIR / "lisai_restore.py").exists()


def test_example_plugins_are_discoverable_processors():
    classes, errors = discover_processor_plugins(str(_EXAMPLES_DIR))

    assert errors == []
    assert set(classes) == {
        "example.invert",
        "example.gaussian-blur",
        "photophysics_suite",
        "lisai.restore",
    }
    for processor_cls in classes.values():
        assert issubclass(processor_cls, Processor)
        assert processor_cls.kinds == ("image",)


def test_invert_example_applies():
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))
    processor = classes["example.invert"]()
    source = _image()

    out = processor.apply(source, {})

    assert result_kind(out) == "image"
    assert out.data.shape == source.data.shape
    np.testing.assert_array_equal(out.data, source.data.max() - source.data)
    # Gating: accepts an image, rejects a non-image (table) kind.
    assert processor.accepts(source)


def test_gaussian_blur_example_applies():
    pytest.importorskip("scipy")
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))
    processor = classes["example.gaussian-blur"]()
    source = _image((16, 16))

    out = processor.apply(source, {"sigma": 2.0})

    assert out.data.shape == source.data.shape
    assert out.data.dtype == source.data.dtype
    # Blurring reduces the dynamic range of a ramp image.
    assert np.ptp(out.data) < np.ptp(source.data)


def test_example_plugins_reject_non_image_kinds():
    """The example image processors must not offer themselves on a table."""
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))

    class _Table(ArrayProcessingResult):
        kind = "table"

    table = _Table(name="t", data=np.zeros((2, 3), np.float32), axis_labels=["ROI", "M"])
    for processor_cls in classes.values():
        assert not processor_cls().accepts(table)


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def _lisai_plugin():
    classes, _ = discover_processor_plugins(str(_EXAMPLES_DIR))
    processor_cls = classes["lisai.restore"]
    return sys.modules[processor_cls.__module__], processor_cls


def _fake_lisai_checkout(root: Path) -> SimpleNamespace:
    package = root / "src" / "lisai"
    package.mkdir(parents=True)
    (root / "configs").mkdir()
    return SimpleNamespace(
        origin=str(package / "__init__.py"), submodule_search_locations=[str(package)]
    )


def test_lisai_restore_reports_missing_install(monkeypatch):
    module, processor_cls = _lisai_plugin()
    monkeypatch.setattr(module, "_lisai_spec", lambda: None)

    with pytest.raises(RuntimeError, match="not installed"):
        processor_cls().apply(_image(), {"model_name": "any"})


def test_lisai_restore_refuses_unconfigured_lisai_without_prompting(monkeypatch, tmp_path):
    module, processor_cls = _lisai_plugin()
    spec = _fake_lisai_checkout(tmp_path)
    monkeypatch.setattr(module, "_lisai_spec", lambda: spec)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("LISAI setup prompted on stdin"))

    with pytest.raises(RuntimeError, match="local_config.yml"):
        processor_cls().apply(_image(), {"model_name": "any"})

    (tmp_path / "configs" / "local_config.yml").write_text("infrastructure: {}\n")
    assert module._lisai_setup_problem() is None


def test_lisai_restore_output_axes_follow_upsampling():
    module, _ = _lisai_plugin()
    source = ArrayProcessingResult(
        name="s",
        data=np.zeros((2, 5, 8, 8), np.float32),
        axis_labels=["C", "T", "Y", "X"],
        axis_scales=[1.0, 0.5, 40.0, 40.0],
        scale_unit="nm",
    )

    stack, kept = module._frame_stack(source)
    assert stack.shape == (5, 8, 8)
    assert kept == [1, 2, 3]

    labels, scales, factor = module._output_axes(source, kept, stack.shape, np.zeros((5, 16, 16)))
    assert labels == ["T", "Y", "X"]
    assert factor == 2
    assert scales == [0.5, 20.0, 20.0]

    _, scales, factor = module._output_axes(source, kept, stack.shape, np.zeros((5, 8, 8)))
    assert factor == 1
    assert scales == [0.5, 40.0, 40.0]


def test_lisai_restore_drops_frames_without_context():
    module, _ = _lisai_plugin()
    prediction = np.arange(7, dtype=np.float32)[:, None, None] * np.ones((7, 4, 4), np.float32)

    trimmed, dropped = module._trim_context_edges(prediction, 5, False, "m")
    assert dropped == 2
    assert trimmed[:, 0, 0].tolist() == [2.0, 3.0, 4.0]

    padded, dropped = module._trim_context_edges(prediction, 5, True, "m")
    assert dropped == 0 and padded.shape == prediction.shape

    same, dropped = module._trim_context_edges(prediction, None, False, "m")
    assert dropped == 0 and same is prediction

    with pytest.raises(ValueError, match="Pad edge frames"):
        module._trim_context_edges(prediction[:4], 5, False, "m")
    with pytest.raises(ValueError, match="Pad edge frames"):
        module._trim_context_edges(prediction[0], 5, False, "m")


def test_lisai_restore_widget_matches_declaration(qapp, monkeypatch):
    module, processor_cls = _lisai_plugin()
    monkeypatch.setattr(module, "_lisai_spec", lambda: None)

    assert check_plugin_contract(processor_cls) == []
