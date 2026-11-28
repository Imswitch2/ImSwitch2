"""Declared input contracts: core dims, required attributes, metadata that travels.

A plugin may say which axes one call consumes (``core_dims_in``) and which
attributes it needs; a wrong input is then refused with a message that says
why *before* any pixel is processed. Metadata travels with the data: a
Python step's reshape keeps the recording's geometry, a deskew says what it
made, the next reconstructor reads it.
"""

import os

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.core_dims import (  # noqa: E402
    CoreDimsError,
    match_core_dims,
    wants_iteration,
    without_reserved,
)
from imswitch.improcess.model.result_source import data_obj_from_result  # noqa: E402
from imswitch.improcess.processors.python_step.context import build_namespace, run_script  # noqa: E402
from imswitch.improcess.reconstructors.base import Reconstructor  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    RunError,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    validate,
)

SNOUTY = {"device": "CPU", "c_px": 100.0, "alpha_deg": 30.0, "dy": 210.0,
          "sample_vx_size": 200.0, "camera_offset": 100.0, "n_timepoints": 1}


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _image(shape, axes, *, attrs=None, seed=0):
    data = np.random.default_rng(seed).random(shape).astype(np.float32) * 100 + 101
    result = ArrayProcessingResult("img", data, list(axes))
    result.attrs = dict(attrs or {})
    return result


# -- matching ------------------------------------------------------------------

def test_the_core_dims_are_the_trailing_axes_and_the_rest_is_looped():
    match = match_core_dims(["T", "C", "Y", "X"], (3, 2, 8, 8), ("Y", "X"))
    assert match.loop_labels == ("T", "C") and match.loop_shape == (3, 2) and match.count == 6
    assert match.core_labels == ("Y", "X") and match.iterates


def test_alternatives_and_wildcards_match_and_labels_compare_case_insensitively():
    assert match_core_dims(["t", "frame", "y", "x"], (2, 5, 4, 4), ("Z|Frame", "Y", "X")).loop_labels == ("t",)
    assert match_core_dims(["T", "Anything", "Y", "X"], (2, 5, 4, 4), ("*", "Y", "X")).loop_shape == (2,)


def test_the_core_rank_runs_as_before_whatever_the_labels():
    # A raw file's labels are often a guess: the old one-call contract stands.
    match = match_core_dims(["C", "Y", "X"], (5, 4, 4), ("Z", "Y", "X"))
    assert not match.iterates and match.core_labels == ("C", "Y", "X")
    assert not match_core_dims(["A", "B"], (2, 2), None).iterates


def test_too_few_axes_or_the_wrong_trailing_axes_are_refused_with_the_reason():
    with pytest.raises(CoreDimsError, match="needs at least 3 axes"):
        match_core_dims(["Y", "X"], (4, 4), ("Z", "Y", "X"))
    with pytest.raises(CoreDimsError, match=r"\(X, C\) of \(T, Y, X, C\).*not last"):
        match_core_dims(["T", "Y", "X", "C"], (2, 4, 4, 3), ("Y", "X"))


def test_the_reserved_iterate_parameter():
    assert wants_iteration({}) and not wants_iteration({"_iterate": False})
    assert not wants_iteration({"_iterate": "false"})
    assert without_reserved({"_iterate": False, "a": 1}) == {"a": 1}


# -- the reconstructor contract ----------------------------------------------------

class _Needy(Reconstructor):
    name = "needy"
    id = "needy"
    core_dims_in = ("Z", "Y", "X")
    required_attrs = ("alpha_deg",)

    @classmethod
    def default_params(cls):
        return {}

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        raise AssertionError("never reached")


def test_check_input_names_every_problem():
    needy = _Needy()
    problems = needy.check_input(data_obj_from_result(_image((2, 4, 4, 3), "TYXC")), {})
    assert len(problems) == 2
    assert "last 3 axes" in problems[0] and "alpha_deg" in problems[1]
    assert needy.check_input(data_obj_from_result(_image((2, 4, 4, 4), "TZYX", attrs={"alpha_deg": 30})), {}) == []
    assert needy.check_input(data_obj_from_result(_image((4, 4, 4), "ZYX")), {"alpha_deg": 30}) == []
    assert needy.check_input(None, {}) == []                        # nothing known statically


def test_a_plugin_without_a_declaration_checks_nothing():
    class Plain(_Needy):
        core_dims_in = None
        required_attrs = ()

    assert Plain().check_input(data_obj_from_result(_image((2, 4, 4, 3), "TYXC")), {}) == []


# -- SNOUTY and MoNaLISA ------------------------------------------------------------

def test_snouty_declares_its_core_dims_and_marks_what_it_made(registry):
    snouty = registry.get_reconstructor("snouty")
    assert snouty.core_dims_in[1:] == ("Y", "X") and snouty.core_dims_out == ("Z", "Y", "X")
    skewed = _image((8, 16, 16), "ZYX", attrs={
        "Detector:Cam:Camera pixel size": 0.1, "AcquisitionLayout:json": "{}", "sample": "beads",
    })
    result = snouty.process(data_obj_from_result(skewed), dict(SNOUTY))
    assert result.attrs["geometry"] == "deskewed"
    assert result.attrs["pixel_size_nm"] == 200.0 and result.attrs["voxel_size_nm"] == [200.0] * 3
    assert result.attrs["sample"] == "beads"                       # other metadata kept
    assert "Detector:Cam:Camera pixel size" not in result.attrs     # no longer the grid
    assert "AcquisitionLayout:json" not in result.attrs             # nor the raw layout


def test_snouty_refuses_to_split_timepoints_twice(registry):
    snouty = registry.get_reconstructor("snouty")
    four_d = data_obj_from_result(_image((2, 8, 16, 16), "TZYX"))
    assert snouty.check_input(four_d, dict(SNOUTY)) == []
    problems = snouty.check_input(four_d, {**SNOUTY, "n_timepoints": 2})
    assert problems and "n_timepoints to 1" in problems[0]
    three_d = data_obj_from_result(_image((8, 16, 16), "ZYX"))
    assert snouty.check_input(three_d, {**SNOUTY, "n_timepoints": 2}) == []   # the legacy layout


def test_the_3d_snouty_path_is_unchanged(registry, tmp_path):
    raw = np.random.default_rng(3).random((8, 16, 16)).astype(np.float32) * 100 + 101
    path = tmp_path / "raw.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("data", data=raw)
    from imswitch.improcess.model import DataObj

    snouty = registry.get_reconstructor("snouty")
    from_file = snouty.process(DataObj("raw", "data", path=str(path)), {**SNOUTY, "n_timepoints": 2})
    assert from_file.data.shape[0] == 2 and from_file.axis_labels == ["T", "Z", "Y", "X"]


def test_monalisa_reads_a_default_pixel_size_from_the_attributes(registry):
    for plugin_id in ("monalisa", "monalisa-lattice"):
        plugin = registry.get_reconstructor(plugin_id)
        assert plugin.core_dims_in == ("*", "Y", "X")
        defaults = type(plugin).default_params()
        data_obj = data_obj_from_result(_image((4, 8, 8), "ZYX", attrs={"pixel_size_nm": 200.0}))
        assert plugin.prepare_params(data_obj, dict(defaults))["pixel_size_nm"] == 200.0
        explicit = {**defaults, "pixel_size_nm": 65.0}
        assert plugin.prepare_params(data_obj, explicit)["pixel_size_nm"] == 65.0     # explicit wins


# -- the Python step's attrs ------------------------------------------------------------

def test_a_python_reshape_keeps_the_input_attributes():
    skewed = _image((4, 8, 16), "ZYX", attrs={"alpha_deg": 30.0, "geometry": "skewed"})
    code = ('out = make_result(data.reshape(4, 8, 2, 8).transpose(2, 0, 1, 3), '
            'axes=["T", "Z", "Y", "X"], attrs={"timepoints": 2})\n')
    (result,), _ = run_script(code, [skewed], ("out",))
    assert result.axis_labels == ["T", "Z", "Y", "X"]
    assert result.attrs == {"alpha_deg": 30.0, "geometry": "skewed", "timepoints": 2}


def test_inherit_attrs_false_starts_from_the_given_attributes_alone():
    skewed = _image((4, 8, 8), "ZYX", attrs={"alpha_deg": 30.0})
    (result,), _ = run_script('out = make_result(data, attrs={"a": 1}, inherit_attrs=False)\n', [skewed], ("out",))
    assert result.attrs == {"a": 1}
    (plain,), _ = run_script("out = data * 2\n", [skewed], ("out",))
    assert plain.attrs == {"alpha_deg": 30.0}                      # a plain array inherits too


def test_the_code_sees_the_input_attributes_as_a_copy():
    skewed = _image((4, 8, 8), "ZYX", attrs={"alpha_deg": 30.0})
    namespace = build_namespace([skewed])
    assert namespace["attrs"] == {"alpha_deg": 30.0}
    namespace["attrs"]["alpha_deg"] = 0
    assert skewed.attrs["alpha_deg"] == 30.0


def test_attrs_must_be_a_dict():
    from imswitch.improcess.processors.python_step.context import ScriptError

    with pytest.raises(ScriptError, match="attrs must be a dict"):
        run_script("out = make_result(data, attrs=[1])\n", [_image((4, 8, 8), "ZYX")], ("out",))


# -- in a workflow ------------------------------------------------------------------------

def _chain(raw, code, snouty=None):
    return Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("shape", "python", {"code": code}, inputs=["view"]),
        Reconstruct("deskew", "snouty", dict(snouty or SNOUTY), inputs=["shape"]),
    ])


def _raw(tmp_path, shape=(8, 16, 16)):
    path = tmp_path / "raw.h5"
    with h5py.File(path, "w") as handle:
        dataset = handle.create_dataset("data", data=np.ones(shape, np.float32) * 150)
        dataset.attrs["axes"] = "ZYX"
    return path


def test_the_reserved_iterate_key_is_not_an_unknown_parameter(registry, tmp_path):
    wf = _chain(_raw(tmp_path), "out = data\n", {**SNOUTY, "_iterate": False})
    assert validate(wf, registry) == []


def test_a_wrongly_shaped_input_fails_before_the_deskew_with_the_reason(registry, tmp_path):
    # (T, Y, X, C): the stack's axes are not last.
    code = 'out = make_result(np.stack([data[0]] * 3, -1)[None], axes=["T", "Y", "X", "C"])\n'
    with pytest.raises(RunError) as caught:
        run(_chain(_raw(tmp_path), code), registry=registry, out_dir=tmp_path)
    report = caught.value.report
    assert report.failed_step == "deskew"
    assert "last 3 axes" in report.error and "(Y, X, C)" in report.error
