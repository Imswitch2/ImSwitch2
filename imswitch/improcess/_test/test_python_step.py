"""The Python step: the script context's rules (Qt-free), the processor, and
the step inside a workflow."""

import ast
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.python_step import context as python_context
from imswitch.improcess.processors.python_step.context import (
    DEFAULT_CODE,
    DEFAULT_PORTS,
    ScriptError,
    build_namespace,
    parse_ports,
    run_script,
)


def _stack(shape=(12, 4, 4), labels=("Z", "Y", "X"), scales=None, unit="px", name="rec"):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult(
        name, data, list(labels), axis_scales=scales, scale_unit=unit,
    )


def _slice_stack(count=12, size=4):
    """Plane ``i`` holds the value ``i`` everywhere: slices are easy to name."""
    data = np.broadcast_to(np.arange(count, dtype=np.float32)[:, None, None], (count, size, size))
    return ArrayProcessingResult("rec", np.array(data), ["Z", "Y", "X"])


# -- the context is Qt-free ---------------------------------------------------

def test_the_context_module_imports_nothing_from_qt():
    tree = ast.parse(Path(python_context.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not [name for name in imported if name.split(".")[0] in {"qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6"}]


# -- ports --------------------------------------------------------------------

def test_default_ports_and_template():
    assert DEFAULT_PORTS == "out"
    assert DEFAULT_CODE.rstrip().splitlines()[-1] == 'outputs = {"out": data}'
    assert DEFAULT_CODE.lstrip().startswith("#")
    compile(DEFAULT_CODE, "<template>", "exec")


@pytest.mark.parametrize("text, expected", [
    ("", ("out",)),
    (None, ("out",)),
    ("   ", ("out",)),
    (" , ,", ("out",)),
    ("a", ("a",)),
    ("a, b", ("a", "b")),
    ("  a ,b,, c-1 , d_2", ("a", "b", "c-1", "d_2")),
])
def test_parse_ports_splits_strips_and_drops_empties(text, expected):
    assert parse_ports(text) == expected


@pytest.mark.parametrize("text, named", [
    ("a b", "a b"),
    ("a, b.c", "b.c"),
    ("ok, spl/it", "spl/it"),
    ("é", "é"),
])
def test_parse_ports_refuses_a_bad_name_and_names_it(text, named):
    with pytest.raises(ValueError, match=named):
        parse_ports(text)


def test_parse_ports_refuses_a_duplicate_and_names_it():
    with pytest.raises(ValueError, match="'a'"):
        parse_ports("a, b, a")


# -- the namespace ------------------------------------------------------------

def test_the_namespace_has_exactly_the_documented_names():
    first = _stack(scales=[2.0, 0.5, 0.5], unit="um")
    second = _stack(name="other")
    namespace = build_namespace([first, second])
    assert set(namespace) == {
        "np", "data", "inputs", "axes", "scales", "unit", "axis", "results",
        "make_result", "make_labels", "outputs", "out",
    }
    assert namespace["np"] is np
    assert np.array_equal(namespace["data"], first.data)
    assert [a.shape for a in namespace["inputs"]] == [(12, 4, 4), (12, 4, 4)]
    assert namespace["axes"] == ["Z", "Y", "X"]
    assert namespace["scales"] == [2.0, 0.5, 0.5]
    assert namespace["unit"] == "um"
    assert namespace["results"] == [first, second]
    assert namespace["outputs"] is None and namespace["out"] is None


def test_data_is_materialised_from_a_lazy_source():
    class Lazy:
        shape = (3, 2, 2)
        ndim = 3

        def __array__(self, dtype=None, copy=None):
            return np.ones(self.shape, dtype=dtype or np.float32)

    lazy = ArrayProcessingResult("lazy", Lazy(), ["Z", "Y", "X"])
    data = build_namespace([lazy])["data"]
    assert isinstance(data, np.ndarray) and data.shape == (3, 2, 2)


def test_axis_finds_a_label_or_passes_an_index_through():
    axis = build_namespace([_stack()])["axis"]
    assert axis("Z") == 0 and axis("X") == 2
    assert axis("y") == 1
    assert axis(1) == 1 and axis(np.int64(2)) == 2
    assert axis(-1) == 2
    for bad in ("T", 3, -4, 1.5, True, None):
        with pytest.raises(ValueError, match="Z, Y, X"):
            axis(bad)


# -- outputs ------------------------------------------------------------------

def test_a_bare_out_becomes_the_single_port():
    (result,), printed = run_script("out = data * 2", [_stack()], ("out",))
    assert printed == ""
    assert np.array_equal(result.data, _stack().data * 2)
    assert result.name == "rec (out)"
    assert result.axis_labels == ["Z", "Y", "X"]
    assert result.metadata == {"operation": "python", "port": "out"}


def test_a_bare_array_in_outputs_is_accepted_for_the_single_port_only():
    (result,), _ = run_script("outputs = data + 1", [_stack()], ("out",))
    assert np.array_equal(result.data, _stack().data + 1)
    with pytest.raises(ScriptError, match="must be a dict") as caught:
        run_script("outputs = data", [_stack()], ("a", "b"))
    assert caught.value.line is None


def test_out_fills_a_single_port_of_any_name_but_not_several():
    (result,), _ = run_script("out = data", [_stack()], ("only",))
    assert result.metadata["port"] == "only"
    with pytest.raises(ScriptError, match="single output port"):
        run_script("out = data", [_stack()], ("a", "b"))


def test_dict_outputs_come_back_in_the_declared_order():
    results, _ = run_script(
        'outputs = {"b": data + 1, "a": data + 2}', [_stack()], ("a", "b"),
    )
    assert [r.metadata["port"] for r in results] == ["a", "b"]
    assert np.array_equal(results[0].data, _stack().data + 2)
    assert np.array_equal(results[1].data, _stack().data + 1)
    assert [r.name for r in results] == ["rec (a)", "rec (b)"]


def test_setting_neither_outputs_nor_out_is_an_error():
    with pytest.raises(ScriptError, match="neither 'outputs' nor 'out'"):
        run_script("x = 1", [_stack()], ("out",))


def test_missing_and_extra_ports_are_named_with_the_declared_list():
    with pytest.raises(ScriptError) as missing:
        run_script('outputs = {"a": data}', [_stack()], ("a", "b"))
    assert "missing b" in str(missing.value) and "(a, b)" in str(missing.value)
    with pytest.raises(ScriptError) as extra:
        run_script('outputs = {"a": data, "c": data}', [_stack()], ("a",))
    assert "not declared: c" in str(extra.value) and "(a)" in str(extra.value)
    with pytest.raises(ScriptError) as both:
        run_script('outputs = {"c": data}', [_stack()], ("a", "b"))
    assert "missing a, b" in str(both.value) and "not declared: c" in str(both.value)


def test_a_different_dimensionality_needs_make_result():
    with pytest.raises(ScriptError) as caught:
        run_script('outputs = {"out": data.max(axis=0)}', [_stack()], ("out",))
    message = str(caught.value)
    assert "'out'" in message and "2 dimensions" in message and "3 (Z, Y, X)" in message
    assert "make_result" in message

    (result,), _ = run_script(
        'outputs = {"out": make_result(data.max(axis=0), axes=["Y", "X"], name="mip")}',
        [_stack(scales=[2.0, 0.5, 0.25], unit="um")], ("out",),
    )
    assert result.axis_labels == ["Y", "X"]
    assert result.data.shape == (4, 4)
    assert result.name == "mip"
    # the calibration of the axes that survived comes along; the unit always does
    assert result.axis_scales == [0.5, 0.25] and result.scale_unit == "um"


def test_make_result_scales_override_and_axes_must_match_the_array():
    (result,), _ = run_script(
        'out = make_result(data[0], axes=["Y", "X"], scales=[1.5, 2.5])', [_stack()], ("out",),
    )
    assert result.axis_scales == [1.5, 2.5]
    with pytest.raises(ScriptError, match="name 3 dimensions but the array has 2"):
        run_script('out = make_result(data[0], axes=["Z", "Y", "X"])', [_stack()], ("out",))
    with pytest.raises(ScriptError, match="1 scales for 2 dimensions"):
        run_script('out = make_result(data[0], axes=["Y", "X"], scales=[1.0])', [_stack()], ("out",))


def test_the_same_dimensionality_inherits_axes_scales_and_unit():
    (result,), _ = run_script(
        "out = data[::2]", [_stack(scales=[2.0, 0.5, 0.5], unit="um")], ("out",),
    )
    assert result.data.shape == (6, 4, 4)
    assert result.axis_labels == ["Z", "Y", "X"]
    assert result.axis_scales == [2.0, 0.5, 0.5] and result.scale_unit == "um"
    low, high = result.display_levels
    assert low <= float(result.data.min()) and high >= float(result.data.max())


def test_an_output_that_is_not_a_numeric_array_is_refused():
    for value in ('"text"', "{1: 2}", "object()"):
        with pytest.raises(ScriptError, match="'out' must be a numeric array"):
            run_script(f"out = {value}", [_stack()], ("out",))


def test_make_labels_builds_a_labels_result():
    from imswitch.improcess.model.result import result_kind

    (result,), _ = run_script(
        'out = make_labels((data > 100).astype(np.uint8), name="mask")', [_stack()], ("out",),
    )
    assert result_kind(result) == "labels"
    assert result.name == "mask"
    assert result.axis_labels == ["Z", "Y", "X"]
    assert result.data.dtype.kind in "iu" and set(np.unique(result.data)) <= {0, 1}

    (whole,), _ = run_script("out = make_labels(data // 50)", [_stack()], ("out",))
    assert whole.data.dtype.kind in "iu" and whole.name == "rec (out)"
    with pytest.raises(ScriptError, match="whole numbers"):
        run_script("out = make_labels(data / 7)", [_stack()], ("out",))


# -- stdout and errors --------------------------------------------------------

def test_print_and_stderr_output_is_captured_in_order():
    code = 'import sys\nprint("one")\nprint("two", file=sys.stderr)\nout = data\n'
    (result,), printed = run_script(code, [_stack()], ("out",))
    assert printed == "one\ntwo\n"
    assert result.metadata["port"] == "out"


def test_a_name_error_reports_its_line():
    code = "x = 1\ny = x + undefined_name\nout = y\n"
    with pytest.raises(ScriptError) as caught:
        run_script(code, [_stack()], ("out",))
    error = caught.value
    assert error.line == 2
    assert str(error) == "line 2: NameError: name 'undefined_name' is not defined"
    assert "\n" not in str(error)
    assert 'File "<python step>", line 2, in <module>' in error.traceback_text
    assert "y = x + undefined_name" in error.traceback_text
    assert error.traceback_text.splitlines()[-1] == "NameError: name 'undefined_name' is not defined"


def test_a_syntax_error_reports_its_own_line():
    code = "a = 1\nb = 2\nc = (3 +\n"
    with pytest.raises(ScriptError) as caught:
        run_script(code, [_stack()], ("out",))
    assert caught.value.line is not None and caught.value.line >= 3
    assert str(caught.value).startswith(f"line {caught.value.line}: SyntaxError: ")
    assert "SyntaxError" in caught.value.traceback_text

    with pytest.raises(ScriptError) as plain:
        run_script("x = 1\ndef f(:\n    pass\n", [_stack()], ("out",))
    assert plain.value.line == 2


def test_the_traceback_keeps_only_the_scripts_own_frames():
    code = "def pick(i):\n    return np.take(data, [100], axis=0)\nout = pick(0)\n"
    with pytest.raises(ScriptError) as caught:
        run_script(code, [_stack()], ("out",))
    error = caught.value
    assert error.line == 2          # the innermost frame of the user's code
    frames = [line for line in error.traceback_text.splitlines() if line.lstrip().startswith("File ")]
    assert len(frames) == 2 and all("<python step>" in line for line in frames)
    assert "numpy" not in error.traceback_text
    assert str(error).startswith("line 2: IndexError")


def test_exit_is_an_error_not_the_end_of_the_program():
    with pytest.raises(ScriptError, match="line 2: SystemExit: the code called exit"):
        run_script("x = 1\nraise SystemExit(0)\n", [_stack()], ("out",))


def test_a_named_step_shows_in_the_traceback_frames():
    with pytest.raises(ScriptError) as caught:
        run_script("1 / 0", [_stack()], ("out",), step_name="split")
    assert "<python step 'split'>" in caught.value.traceback_text
    assert caught.value.line == 1


def test_running_needs_an_input():
    with pytest.raises(ScriptError, match="at least one input"):
        run_script("out = 1", [], ("out",))


def test_the_original_exception_is_kept_for_the_log():
    with pytest.raises(ScriptError) as caught:
        run_script("raise KeyError('k')", [_stack()], ("out",))
    assert isinstance(caught.value.__cause__, KeyError)


# -- the example in the design ------------------------------------------------

INTERLEAVE_CODE = """\
ax = axis("Z")
group = (np.arange(data.shape[ax]) // 3) % 2
outputs = {
    "a": np.take(data, np.flatnonzero(group == 0), axis=ax),
    "b": np.take(data, np.flatnonzero(group == 1), axis=ax),
}
"""


def test_three_slices_at_a_time_alternate_between_two_outputs():
    (a, b), _ = run_script(INTERLEAVE_CODE, [_slice_stack()], ("a", "b"))
    assert a.data.shape == (6, 4, 4) and b.data.shape == (6, 4, 4)
    assert a.data[:, 0, 0].tolist() == [0, 1, 2, 6, 7, 8]
    assert b.data[:, 0, 0].tolist() == [3, 4, 5, 9, 10, 11]
    assert a.axis_labels == b.axis_labels == ["Z", "Y", "X"]
    assert (a.name, b.name) == ("rec (a)", "rec (b)")
