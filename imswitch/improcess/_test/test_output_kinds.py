"""Every built-in declares what it produces, and the declaration is true.

The workflow editor 2.0 colours a port by the kind of result it carries and
checks a connection before anything runs; both need the kind *before* a run.
Processors already declared what they accept (``kinds``); this pins what they
produce (``output_kind``, carried by ``OutputSpec.kind``), what reconstructors
produce, that no built-in modifies its inputs (a cached result is handed to
later runs as an input), and that a drop-in written to the older
``output_spec(params)`` signature keeps working now that callers pass the
inputs' kinds.
"""

import copy
import sys
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model import ArrayProcessingResult, localizations_from_columns
from imswitch.improcess.model.result import ANY_KIND, PORT_KINDS, RESULT_KINDS, result_kind
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES
from imswitch.improcess.processors.base import OutputSpec, Processor, call_output_spec
from imswitch.improcess.processors.run import run_processor
from imswitch.improcess.reconstructors import builtin_reconstructor_ids
from imswitch.improcess.workflows import Workflow, bootstrap_registry, validate
from imswitch.improcess.workflows.steps import declared_kinds

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_result_kind_matrix import (  # noqa: E402
    _gaussian,
    _image_2d,
    _image_c_stack,
    _image_t_stack,
    _representative_results,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


class _Logger:
    def exception(self, *args, **kwargs):
        pass


# -- the declarations exist and agree with output_spec --------------------------------------

def test_every_built_in_processor_declares_its_output_kind(registry):
    for pid, cls in _AVAILABLE_PROCESSOR_CLASSES.items():
        assert cls.output_kind in PORT_KINDS and cls.output_kind != "unknown", pid
        spec = call_output_spec(registry.get_processor(pid), cls.default_params(), ("image",))
        assert spec.kind == cls.output_kind, pid


def test_every_built_in_reconstructor_declares_its_output_kind(registry):
    for rid in builtin_reconstructor_ids():
        plugin = registry.get_reconstructor(rid)
        assert type(plugin).output_kind in RESULT_KINDS, rid
        spec = plugin.output_spec()
        assert spec.ports == ("out",) and spec.kind == type(plugin).output_kind, rid


def test_only_the_python_step_is_declared_nondeterministic(registry):
    flagged = {pid for pid, cls in _AVAILABLE_PROCESSOR_CLASSES.items() if not cls.deterministic}
    assert flagged == {"python"}
    assert all(registry.get_reconstructor(rid).deterministic for rid in builtin_reconstructor_ids())


#: The result class each reconstructor builds; its ``kind`` must be the declared one.
#: Reconstructors need recordings of their own modality to run, so this checks the
#: declaration against the class rather than against a run (view-only also runs below).
_RECONSTRUCTOR_RESULTS = {
    "beadrec": ("imswitch.improcess.model.array_result", "ArrayProcessingResult"),
    "monalisa": ("imswitch.improcess.reconstructors.monalisa.result", "MonalisaProcessingResult"),
    "monalisa-legacy": ("imswitch.improcess.reconstructors.monalisa.result", "MonalisaProcessingResult"),
    "monalisa-lattice": ("imswitch.improcess.reconstructors.monalisa_lattice.result", "MonalisaLatticeResult"),
    "polarisation-map": ("imswitch.improcess.reconstructors.polarisation_map.reconstructor", "PolarisationMapResult"),
    "smlm-localizer": ("imswitch.improcess.model.localization_result", "LocalizationResult"),
    "snouty": ("imswitch.improcess.reconstructors.snouty.result", "SnoutyResult"),
    "snouty-projections": ("imswitch.improcess.reconstructors.snouty_projections.result", "SnoutyProjectionsResult"),
    "tiling-mosaic": ("imswitch.improcess.reconstructors.tiling.reconstructor", "TilingMosaicResult"),
    "time-lapse": ("imswitch.improcess.reconstructors.time_lapse.reconstructor", "TimeLapseResult"),
    "view-only": ("imswitch.improcess.reconstructors.view_only.reconstructor", "ViewOnlyResult"),
    "widefield-starss": ("imswitch.improcess.reconstructors.widefield_starss.result", "WidefieldStarssResult"),
}


def test_each_reconstructor_declares_the_kind_of_the_result_it_builds(registry):
    import importlib

    assert set(_RECONSTRUCTOR_RESULTS) == set(builtin_reconstructor_ids()), "a new reconstructor needs a row here"
    for rid, (module, name) in _RECONSTRUCTOR_RESULTS.items():
        result_class = getattr(importlib.import_module(module), name)
        assert getattr(result_class, "kind", "image") == type(registry.get_reconstructor(rid)).output_kind, rid


# -- a run returns what was declared, and leaves its inputs alone ---------------------------------

def _snapshot(result):
    state = {"data": np.array(np.asarray(result.data), copy=True)}
    for attribute in ("locs", "properties"):
        if getattr(result, attribute, None) is not None:
            state[attribute] = copy.deepcopy(getattr(result, attribute))
    return state


def _same(before, after) -> bool:
    for key, value in before.items():
        if key == "data":
            if not np.array_equal(value, np.asarray(after["data"]), equal_nan=True):
                return False
        elif key == "locs":
            if not all(np.array_equal(value[name], after["locs"][name]) for name in value.dtype.names):
                return False
        elif isinstance(value, dict):
            if set(value) != set(after[key]) or not all(np.array_equal(value[k], after[key][k]) for k in value):
                return False
    return True


def _drifting_cloud(n_frames=40):
    rng = np.random.default_rng(0)
    base_x, base_y = rng.uniform(0, 4000, 60), rng.uniform(0, 4000, 60)
    frames = np.repeat(np.arange(n_frames), base_x.size)
    return localizations_from_columns({
        "frame": frames,
        "x_nm": np.tile(base_x, n_frames) + frames * 4.0,
        "y_nm": np.tile(base_y, n_frames) - frames * 3.0,
        "sigma_x_nm": np.full(frames.size, 120.0),
        "sigma_y_nm": np.full(frames.size, 120.0),
        "photons": np.full(frames.size, 1000.0),
    })


def _fixture(pid, processor, tmp_path, monkeypatch):
    """``(inputs, params)`` a built-in can run on; skips only for a missing optional package."""
    params = dict(type(processor).default_params())
    if pid == "channel-merge":
        return [_image_2d(), ArrayProcessingResult("b", _gaussian(center=(4.0, 6.0)), ["Y", "X"])], params
    if pid == "deconvolve-rl":
        calibrated = ArrayProcessingResult("plane", _gaussian(), ["Y", "X"], axis_scales=[0.1, 0.1], scale_unit="um")
        return [calibrated], params
    if pid == "denoise":
        pytest.importorskip("torch", reason="denoise needs PyTorch")
    if pid == "smlm-drift-comet":
        pytest.importorskip("comet", reason="COMET drift needs the optional comet-smlm package")
    if pid in ("smlm-drift", "smlm-drift-comet"):
        from imswitch.improcess.model import LocalizationResult

        return [LocalizationResult("storm", _drifting_cloud(), pixel_size_nm=100.0)], {
            **params, "segments": 8, "render_pixel_size_nm": 10.0,
        }
    if pid == "table-to-localizations":
        from imswitch.improcess.model.points_table_result import PointsTableResult

        table = PointsTableResult("pts", np.array([[1.0, 2.0], [3.0, 4.0]]),
                                  properties={"frame": np.array([0, 1]), "brightness": np.array([100.0, 200.0])})
        return [table], {**params, "x_column": "x", "y_column": "y", "frame_column": "frame",
                         "photons_column": "brightness", "unit": "px", "pixel_size_nm": 100.0}
    if pid in ("multicolor-apply", "multicolor-registration"):
        from test_multicolor_processor import MinimalResult, _identity_alignment

        data = np.zeros((2, 3, 6), dtype=np.float32)
        data[..., 0:2], data[..., 2:4], data[..., 4:6] = 1, 2, 3
        volume = MinimalResult(name="beads", data=data, axis_labels=["Z", "Y", "X"],
                               axis_scales=[0.1, 0.1, 0.1], scale_unit="um")
        if pid == "multicolor-apply":
            from imswitch.improcess.analysis.multicolor import save_alignment

            path = tmp_path / "alignment.h5"
            save_alignment(_identity_alignment(), path)
            return [volume], {**params, "alignment_path": str(path)}
        monkeypatch.setattr(
            "imswitch.improcess.processors.multicolor_registration.processor.extract_alignment",
            lambda *args, **kwargs: _identity_alignment(),
        )
        return [volume], {**params, "x_bounds": "0,2,4,6"}
    candidates = list(_representative_results().values()) + [_image_t_stack(), _image_c_stack()]
    for candidate in candidates:
        if processor.accepts(candidate):
            count = 1 if processor.max_inputs == 1 else max(processor.min_inputs, 2)
            return [candidate] * count, params
    raise AssertionError(f"no fixture for {pid}: add one to _fixture")


@pytest.mark.parametrize("pid", sorted(_AVAILABLE_PROCESSOR_CLASSES))
def test_a_run_returns_the_declared_kind_and_does_not_modify_its_inputs(pid, registry, tmp_path, monkeypatch):
    processor = registry.get_processor(pid)
    inputs, params = _fixture(pid, processor, tmp_path, monkeypatch)
    before = [_snapshot(item) for item in inputs]
    results, failures = run_processor(processor, inputs, params, _Logger())
    assert failures == [] and results, f"{pid} did not run on its fixture: {failures}"
    declared = type(processor).output_kind
    if declared != ANY_KIND:
        assert {result_kind(result) for result in results} == {declared}, pid
    assert all(_same(snapshot, _snapshot(item)) for snapshot, item in zip(before, inputs)), f"{pid} modified its input"


def test_view_only_returns_the_declared_kind(registry, tmp_path):
    import importlib.util

    from imswitch.improcess.workflows import run

    spec = importlib.util.spec_from_file_location("synthetic", EXAMPLES / "_synthetic.py")
    synthetic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synthetic)
    recording = synthetic.write_synthetic_recording(tmp_path / "cells.h5", frames=2)
    workflow = Workflow.from_dict({"name": "v", "schema": 1, "steps": [
        {"step": "source", "id": "raw"},
        {"step": "reconstruct", "id": "rec", "reconstructor": "view-only", "inputs": ["raw"]},
    ]})
    with run(workflow, registry=registry, bindings={"raw": str(recording)}, out_dir=tmp_path) as report:
        assert result_kind(report.result("rec")) == type(registry.get_reconstructor("view-only")).output_kind


# -- drop-ins written to the older signature keep working -------------------------------------------

class _OldStyle(Processor):
    """A drop-in written to the documented ``output_spec(params)``."""

    name = "Old style"
    id = "old-style"

    @classmethod
    def default_params(cls):
        return {}

    def output_spec(self, params=None):
        return OutputSpec(ports=("a", "b"))

    @property
    def applies_to(self):
        return lambda result: True

    def apply(self, result, params):
        return result

    def make_param_widget(self, parent):
        return None


def test_an_override_without_input_specs_is_called_without_it(registry):
    plugin = _OldStyle()
    spec = call_output_spec(plugin, {}, ("image",))                  # no TypeError
    assert spec.ports == ("a", "b") and spec.kind == "unknown"
    new_style = registry.get_processor("stack-split")
    assert call_output_spec(new_style, type(new_style).default_params(), ("image",)).kind == "image"


def test_a_workflow_with_an_old_style_drop_in_still_validates(registry):
    registry.register_processor(_OldStyle())
    try:
        workflow = Workflow.from_dict({"name": "w", "schema": 1, "steps": [
            {"step": "source", "id": "raw"},
            {"step": "reconstruct", "id": "rec", "reconstructor": "view-only", "inputs": ["raw"]},
            {"step": "process", "id": "old", "processor": "old-style", "inputs": ["rec"]},
            {"step": "process", "id": "next", "processor": "filter", "inputs": ["old.a"]},
        ]})
        assert validate(workflow, registry) == []
        assert declared_kinds(workflow, registry)["old"] == "unknown"
    finally:
        registry._processors.pop("old-style", None)


# -- validate() refuses only declared mismatches -------------------------------------------------

def _chain(*processors):
    steps = [{"step": "source", "id": "raw"},
             {"step": "reconstruct", "id": "rec", "reconstructor": "view-only", "inputs": ["raw"]}]
    previous = "rec"
    for index, (pid, params) in enumerate(processors):
        steps.append({"step": "process", "id": f"p{index}", "processor": pid, "inputs": [previous], "params": params})
        previous = f"p{index}"
    return Workflow.from_dict({"name": "chain", "schema": 1, "steps": steps})


def test_a_declared_kind_the_next_step_does_not_take_is_an_issue(registry):
    issues = [str(issue) for issue in validate(_chain(("frc", {}), ("filter", {})), registry)]
    assert issues == ["p1: 'filter' does not accept curve results, which 'p0' produces (it takes image)"]
    assert validate(_chain(("segmentation", {}), ("label-morphology", {})), registry) == []
    assert any("does not accept labels" in str(i) for i in validate(_chain(("segmentation", {}), ("filter", {})), registry))


def test_an_any_kind_is_never_refused_before_the_run(registry):
    assert validate(_chain(("python", {"code": "out = data", "ports": "out"}), ("filter", {})), registry) == []


def test_every_shipped_example_still_validates(registry):
    for path in sorted(EXAMPLES.glob("*.yaml")):
        assert validate(Workflow.load(path), registry) == [], path.name


def test_the_catalogue_description_says_what_each_plugin_produces(registry):
    from imswitch.improcess.workflows.runtime import describe_registry

    described = describe_registry(registry)
    assert described["processors"]["frc"]["output_kind"] == "curve"
    assert described["processors"]["python"]["output_kind"] == "any"
    assert described["reconstructors"]["smlm-localizer"]["output_kind"] == "localization"
