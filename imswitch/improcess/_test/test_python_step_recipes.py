"""The Python step recipes that ship with ImSwitch: each does what it says, each
is delivered to the panel and as a workflow file, and none can drift from its twin."""

import ast
import importlib.util
import os
from pathlib import Path

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model import result_kind  # noqa: E402
from imswitch.improcess.model.curve_result import CurveResult  # noqa: E402
from imswitch.improcess.model.snippets import parse_snippet  # noqa: E402
from imswitch.improcess.processors.python_step.context import parse_ports, run_script  # noqa: E402
from imswitch.improcess.workflows import Workflow, bootstrap_registry, run, validate  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SNIPPETS = REPO / "imswitch" / "_data" / "user_defaults" / "improcess_snippets"
EXAMPLES = REPO / "examples" / "improcess_workflows"

RECIPES = [
    "autocrop", "best_focus", "crosstalk", "despeckle",
    "normalize_frames", "ratio_mask", "signal_trace", "snake_mosaic", "temporal_bin",
]


def _tool():
    spec = importlib.util.spec_from_file_location("make_recipes", REPO / "tools" / "make_python_recipe_workflows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recipe(name):
    """``(code, ports)`` of a shipped recipe, split as the panel splits it."""
    return parse_snippet((SNIPPETS / f"{name}.py").read_text(encoding="utf-8"))


def _result(array, labels, scales=None, unit="px", name="rec"):
    return ArrayProcessingResult(name, np.asarray(array), list(labels), axis_scales=scales, scale_unit=unit)


def _run(name, result):
    code, ports = _recipe(name)
    return run_script(code, [result], parse_ports(ports))


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# -- what ships ---------------------------------------------------------------

def test_exactly_these_recipes_ship():
    assert sorted(path.stem for path in SNIPPETS.glob("*.py")) == RECIPES
    assert _tool().recipes() == RECIPES


@pytest.mark.parametrize("name", RECIPES)
def test_a_recipe_says_what_it_does_and_why_it_is_a_script(name):
    text = (SNIPPETS / f"{name}.py").read_text(encoding="utf-8")
    code, ports = _recipe(name)
    assert text.splitlines()[0].startswith("# ports:") and parse_ports(ports)
    comments = []
    for line in code.splitlines():
        if not line.startswith("#"):
            break
        comments.append(line)
    assert len(comments) >= 3, "a recipe opens with what it does"
    assert any(line.startswith("# Why a script:") for line in comments), "and why no processor does it"
    compile(code, name, "exec")


@pytest.mark.parametrize("name", RECIPES)
def test_a_recipe_asks_for_nothing_beyond_numpy_and_scipy(name):
    tree = ast.parse(_recipe(name)[0])
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"scipy"}                              # numpy is in the namespace already


# -- what each one does ------------------------------------------------------------

def test_normalize_frames_gives_every_plane_the_same_level():
    base = np.random.default_rng(0).uniform(50, 100, (8, 8)).astype(np.float32)
    stack = np.stack([base * (1 + 0.2 * i) for i in range(5)]).astype(np.float32)
    (out,), printed = _run("normalize_frames", _result(stack, "TYX"))
    assert np.allclose(out.data, out.data[0], rtol=1e-4)
    assert "plane levels ran from" in printed
    # a plane that is all zeros stays zeros rather than turning into NaN
    stack[2] = 0
    (out,), _ = _run("normalize_frames", _result(stack, "TYX"))
    assert np.isfinite(out.data).all() and not out.data[2].any()


def test_temporal_bin_averages_groups_and_stretches_the_frame_spacing():
    frames = np.broadcast_to(np.arange(12, dtype=np.float32)[:, None, None], (12, 4, 4)).copy()
    (out,), printed = _run("temporal_bin", _result(frames, "TYX", scales=[0.5, 0.1, 0.1], unit="um"))
    assert out.data[:, 0, 0].tolist() == [1.5, 5.5, 9.5]
    assert out.axis_scales == [2.0, 0.1, 0.1] and out.axis_labels == ["T", "Y", "X"] and out.scale_unit == "um"
    assert "12 frames -> 3 (dropped 0)" in printed
    (out,), printed = _run("temporal_bin", _result(frames[:10], "TYX"))        # a remainder is dropped, and said
    assert out.data.shape == (2, 4, 4) and "dropped 2" in printed


def test_best_focus_picks_the_sharpest_plane_says_which_and_draws_the_scores():
    from scipy import ndimage

    noise = np.random.default_rng(0).random((32, 32)).astype(np.float32)
    planes = np.stack([ndimage.gaussian_filter(noise, sigma=abs(i - 3) * 1.5 + 0.01) for i in range(5)])
    (out, sharpness), printed = _run("best_focus", _result(planes, "ZYX", scales=[2.0, 0.1, 0.1], name="stack"))
    assert np.array_equal(out.data, planes[3])
    assert out.name == "stack (plane 3)" and out.axis_labels == ["Y", "X"] and out.axis_scales == [0.1, 0.1]
    assert "sharpest plane: 3" in printed
    # the score of every plane comes out as a curve along Z, calibrated like the stack, peaking at the focus
    assert isinstance(sharpness, CurveResult) and sharpness.name == "stack (sharpness)"
    assert sharpness.axis_labels == ["Z"] and sharpness.axis_scales == [2.0] and sharpness.x_label() == "Z"
    assert sharpness.data.shape == (5,) and int(np.argmax(sharpness.data)) == 3
    np.testing.assert_allclose(sharpness.x_values, [0.0, 2.0, 4.0, 6.0, 8.0])
    with pytest.raises(Exception, match="expects a \\(plane, Y, X\\) stack"):
        _run("best_focus", _result(np.zeros((2, 3, 8, 8)), "TZYX"))


def _disk(size, centre, radius, height):
    yy, xx = np.mgrid[:size, :size]
    return np.where((yy - centre[0]) ** 2 + (xx - centre[1]) ** 2 <= radius ** 2, height, 0.0)


def test_signal_trace_follows_signal_that_moves_and_fades_where_a_fixed_region_loses_it():
    """A disk drifts across the frame while its brightness falls by a fifth each frame."""
    size, radius = 64, 6
    centres = [(32, 12 + 8 * i) for i in range(6)]
    heights = [100.0 * 0.8 ** i for i in range(6)]
    frames = np.stack([20.0 + _disk(size, c, radius, h) for c, h in zip(centres, heights)]).astype(np.float32)
    (level, area), printed = _run("signal_trace", _result(frames, "TYX", scales=[0.5, 0.1, 0.1], unit="um", name="cells"))

    pixels = int(_disk(size, centres[0], radius, 1).sum())
    assert isinstance(level, CurveResult) and isinstance(area, CurveResult)
    assert level.name == "cells (level)" and area.name == "cells (area)"
    np.testing.assert_allclose(level.data, [0.8 ** i for i in range(6)], rtol=1e-5)
    assert area.data.tolist() == [pixels] * 6
    # the frame axis is the curve's x, calibrated, and not captioned in the pixel size's unit
    assert level.axis_labels == ["T"] and level.axis_scales == [0.5] and level.x_label() == "T (s)"
    assert "signal area: %d px in the first frame, %d px in the last" % (pixels, pixels) in printed
    # what a region drawn on the first frame would have measured: nothing, once the disk has left it
    fixed = frames[:, 28:37, 6:19].mean(axis=(1, 2))
    assert fixed[-1] < 0.1 * fixed[0] + 20 and level.data[-1] > 0.3


def test_signal_trace_says_when_it_cannot_compare_and_what_it_expects():
    flat = np.full((4, 16, 16), 7.0, np.float32)
    with pytest.raises(Exception, match="the first frame has no signal above its background"):
        _run("signal_trace", _result(flat, "TYX"))
    with pytest.raises(Exception, match="expects a \\(frame, Y, X\\) recording"):
        _run("signal_trace", _result(np.zeros((8, 8)), "YX"))
    # a frame that loses all its signal is a gap in the curve, not an error or a made-up number
    frames = np.stack([20.0 + _disk(32, (16, 16), 4, 50.0), np.full((32, 32), 20.0)]).astype(np.float32)
    (level, area), _ = _run("signal_trace", _result(frames, "TYX"))
    assert level.data[0] == 1.0 and np.isnan(level.data[1]) and area.data[1] == 0


def test_signal_trace_on_integer_data_gives_the_same_answer_as_on_float_data():
    """The background subtraction goes negative on uint16 unless the recipe converts first."""
    rng = np.random.default_rng(3)
    counts = (rng.normal(300, 5, (5, 24, 24)) + np.stack([_disk(24, (12, 12), 4, 400.0 - 40 * i) for i in range(5)])).astype(np.uint16)
    (a_level, a_area), _ = _run("signal_trace", _result(counts, "TYX"))
    (f_level, f_area), _ = _run("signal_trace", _result(counts.astype(np.float32), "TYX"))
    assert np.allclose(a_level.data, f_level.data) and np.array_equal(a_area.data, f_area.data)


def test_snake_mosaic_puts_every_other_row_back_in_order():
    tiles = np.stack([np.full((3, 3), i, np.float32) for i in range(12)])
    (out,), printed = _run("snake_mosaic", _result(tiles, "TYX", scales=[1.0, 0.2, 0.2]))
    assert out.data.shape == (9, 12) and out.axis_labels == ["Y", "X"] and out.axis_scales == [0.2, 0.2]
    # one tile per cell of the 3 x 4 grid; the middle row was scanned right to left
    assert out.data[::3, ::3].astype(int).tolist() == [[0, 1, 2, 3], [7, 6, 5, 4], [8, 9, 10, 11]]
    assert "12 tiles" in printed
    with pytest.raises(Exception, match="expected 12 tiles"):
        _run("snake_mosaic", _result(tiles[:5], "TYX"))


def test_snake_mosaic_as_a_raster_scan_keeps_the_order_and_never_touches_its_input():
    code, ports = _recipe("snake_mosaic")
    code = code.replace("snake = True", "snake = False")
    tiles = np.stack([np.full((3, 3), i, np.float32) for i in range(12)])
    before = tiles.copy()
    (out,), _ = run_script(code, [_result(tiles, "TYX")], parse_ports(ports))
    assert out.data[::3, ::3].astype(int).tolist() == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
    _run("snake_mosaic", _result(tiles, "TYX"))
    assert np.array_equal(tiles, before)


def test_ratio_mask_divides_where_it_can_and_marks_where_it_did():
    rng = np.random.default_rng(1)
    bottom = rng.uniform(10, 100, (16, 16)).astype(np.float32)
    bottom[:4] = 0.5                                                     # a dim band
    pair = np.stack([2 * bottom, bottom])
    (ratio, valid), printed = _run("ratio_mask", _result(pair, "CYX", scales=[1, 0.2, 0.2], unit="um"))
    used = valid.data.astype(bool)
    assert not used[:4].any() and used[4:].all()
    assert np.allclose(ratio.data[used], 2.0) and np.isnan(ratio.data[~used]).all()
    assert ratio.axis_labels == valid.axis_labels == ["Y", "X"] and ratio.axis_scales == [0.2, 0.2]
    assert valid.kind == "labels" and ratio.kind == "image"
    assert "75.0% of pixels used; median ratio 2.000" in printed


def test_despeckle_replaces_the_hot_pixels_and_nothing_else():
    rng = np.random.default_rng(3)
    noisy = (100 + rng.normal(0, 1, (3, 32, 32))).astype(np.float32)
    spiked = noisy.copy()
    spots = [(0, 5, 5), (1, 20, 7), (2, 30, 30)]
    for spot in spots:
        spiked[spot] += 500
    (out,), printed = _run("despeckle", _result(spiked, "TYX"))
    changed = out.data != spiked
    assert {tuple(int(i) for i in index) for index in np.argwhere(changed)} == set(spots)
    assert all(abs(out.data[spot] - 100) < 8 for spot in spots)           # back to the neighbourhood's level
    assert "replaced 3 of 3072 pixels" in printed
    (clean,), printed = _run("despeckle", _result(noisy, "TYX"))          # nothing to remove: unchanged
    assert np.array_equal(clean.data, noisy) and "replaced 0 of" in printed
    (flat,), printed = _run("despeckle", _result(np.full((2, 8, 8), 5.0, np.float32), "TYX"))
    assert np.array_equal(flat.data, np.full((2, 8, 8), 5.0)) and "replaced 0 of" in printed


def test_autocrop_keeps_the_bright_region_plus_a_margin_and_its_calibration():
    image = np.zeros((3, 64, 80), np.float32)
    image[:, 20:30, 40:55] = 10
    (out,), printed = _run("autocrop", _result(image, "TYX", scales=[1.0, 0.1, 0.1]))
    assert out.data.shape == (3, 10 + 16, 15 + 16)                        # the region plus 8 pixels each side
    assert np.array_equal(out.data[:, 8:18, 8:23], image[:, 20:30, 40:55])
    assert out.axis_labels == ["T", "Y", "X"] and out.axis_scales == [1.0, 0.1, 0.1]
    assert "cropped to rows 12:38, columns 32:63 of (64, 80)" in printed
    near_edge = np.zeros((1, 20, 20), np.float32)
    near_edge[0, 1:4, 1:4] = 5
    (out,), _ = _run("autocrop", _result(near_edge, "TYX"))
    assert out.data.shape == (1, 12, 12)                                  # the margin stops at the image edge
    with pytest.raises(Exception, match="nothing to crop to"):
        _run("autocrop", _result(np.ones((2, 8, 8), np.float32), "TYX"))


def test_crosstalk_recovers_the_true_channels_and_does_not_leave_negative_counts():
    rng = np.random.default_rng(2)
    true0 = rng.uniform(0, 100, (8, 8)).astype(np.float32)
    true1 = rng.uniform(0, 100, (8, 8)).astype(np.float32)
    measured = np.stack([true0, true1 + 0.15 * true0])
    (out,), _ = _run("crosstalk", _result(measured, "CYX"))
    assert np.allclose(out.data[0], true0, atol=1e-3) and np.allclose(out.data[1], true1, atol=1e-3)
    assert out.data.dtype == np.float32 and out.axis_labels == ["C", "Y", "X"]
    # noise that makes the corrected channel dip below zero is clipped, not kept as a negative count
    dim = np.stack([np.full((4, 4), 100.0, np.float32), np.full((4, 4), 5.0, np.float32)])
    (out,), _ = _run("crosstalk", _result(dim, "CYX"))
    assert out.data.min() == 0.0
    with pytest.raises(Exception, match="the matrix is for 2 channels, the data has 3"):
        _run("crosstalk", _result(np.ones((3, 4, 4), np.float32), "CYX"))


@pytest.mark.parametrize("name, shape, labels", [
    ("normalize_frames", (6, 16, 16), "TYX"),
    ("temporal_bin", (8, 16, 16), "TYX"),
    ("despeckle", (3, 16, 16), "TYX"),
    ("crosstalk", (2, 16, 16), "CYX"),
])
def test_integer_data_gives_the_same_answer_as_the_float_data(name, shape, labels):
    """uint16 arithmetic wraps around instead of going negative: a recipe must convert first."""
    rng = np.random.default_rng(5)
    counts = rng.integers(0, 4000, shape).astype(np.uint16)
    (as_int,), _ = _run(name, _result(counts, labels))
    (as_float,), _ = _run(name, _result(counts.astype(np.float32), labels))
    assert np.allclose(as_int.data, as_float.data)


def test_ratio_mask_on_integer_data_gives_the_same_answer_as_on_float_data():
    rng = np.random.default_rng(6)
    pair = rng.integers(1, 4000, (2, 16, 16)).astype(np.uint16)
    (a_ratio, a_valid), _ = _run("ratio_mask", _result(pair, "CYX"))
    (f_ratio, f_valid), _ = _run("ratio_mask", _result(pair.astype(np.float32), "CYX"))
    assert np.allclose(a_ratio.data, f_ratio.data, equal_nan=True) and np.array_equal(a_valid.data, f_valid.data)


# -- delivery -----------------------------------------------------------------------

def test_the_recipes_reach_a_users_snippet_list_with_nothing_set_up(tmp_path, monkeypatch):
    from imswitch.imcommon.model import dirtools
    from imswitch.improcess.model import snippets

    report = dirtools.syncUserDefaults(dirtools.DataFileDirs.UserDefaults, tmp_path, {})
    copied = {Path(path).as_posix() for path in report.copied}
    assert {f"improcess_snippets/{name}.py" for name in RECIPES} <= copied
    monkeypatch.setattr(dirtools.UserFileDirs, "Root", str(tmp_path))
    assert snippets.list_snippets() == RECIPES                            # what Load snippet... offers
    for name in RECIPES:
        assert snippets.load_snippet(name) == _recipe(name)


def test_a_recipe_loaded_into_the_panel_runs_there(qapp):
    from imswitch.improcess.processors.python_step import PythonStepProcessor

    widget = PythonStepProcessor().make_param_widget(None)
    code, ports = _recipe("temporal_bin")
    widget.set_values({"code": code, "ports": ports})
    values = widget.get_values()
    assert values == {"code": code, "ports": ports}
    frames = np.arange(8, dtype=np.float32).reshape(8, 1, 1) * np.ones((8, 2, 2), np.float32)
    output = PythonStepProcessor().apply(_result(frames, "TYX"), values)
    assert output.keys == ("out",) and output.results[0].data[:, 0, 0].tolist() == [1.5, 5.5]


# -- the workflow files --------------------------------------------------------------

def test_each_workflow_file_is_what_the_tool_makes_from_its_snippet():
    tool = _tool()
    for name in RECIPES:
        assert (EXAMPLES / f"python_step_{name}.yaml").read_text(encoding="utf-8") == tool.workflow_text(name), (
            f"python_step_{name}.yaml is out of date: run python tools/make_python_recipe_workflows.py"
        )
    shipped = {path.name for path in EXAMPLES.glob("python_step_*.yaml")}
    assert shipped == {f"python_step_{name}.yaml" for name in RECIPES} | {"python_step_interleave.yaml"}


def test_the_tool_reports_a_stale_file_and_fixes_it(tmp_path, monkeypatch):
    tool = _tool()
    monkeypatch.setattr(tool, "EXAMPLES", tmp_path)
    assert tool.main(["--check"]) == 1                                    # none exist yet
    assert tool.main([]) == 0 and tool.main(["--check"]) == 0
    stale = tmp_path / "python_step_autocrop.yaml"
    stale.write_text(stale.read_text().replace("margin = 8", "margin = 9"))
    assert tool.main(["--check"]) == 1
    assert tool.main([]) == 0 and tool.main(["--check"]) == 0


def _synthetic(tmp_path, frames):
    spec = importlib.util.spec_from_file_location("synthetic", EXAMPLES / "_synthetic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_synthetic_recording(tmp_path / "cells.h5", frames=frames)


@pytest.mark.parametrize("name, shapes", [
    ("autocrop", {"out": None}),
    ("best_focus", {"out": (96, 96), "sharpness": (12,)}),
    ("crosstalk", {"out": (2, 96, 96)}),
    ("despeckle", {"out": (12, 96, 96)}),
    ("normalize_frames", {"out": (12, 96, 96)}),
    ("ratio_mask", {"ratio": (96, 96), "valid": (96, 96)}),
    ("signal_trace", {"level": (12,), "area": (12,)}),
    ("snake_mosaic", {"out": (3 * 96, 4 * 96)}),
    ("temporal_bin", {"out": (3, 96, 96)}),
])
def test_each_workflow_validates_and_runs_on_the_synthetic_recording(registry, tmp_path, name, shapes):
    tool = _tool()
    curves = set(tool.CURVE_PORTS.get(name, ()))
    recording = _synthetic(tmp_path, tool.TRY_FRAMES.get(name, tool.DEFAULT_TRY_FRAMES))
    workflow = Workflow.load(EXAMPLES / f"python_step_{name}.yaml")
    assert validate(workflow, registry) == []
    with run(workflow, registry=registry, bindings={"raw": str(recording)}, out_dir=tmp_path / "out") as report:
        assert report.ok
        for port, shape in shapes.items():
            produced = report.result(f"script.{port}")
            if shape is not None:
                assert produced.data.shape == shape, port
            # the tool saves exactly the curve ports as CSV: if the script's idea of what is a
            # curve and the tool's drift apart, a curve would be saved as a TIFF and fail
            assert (result_kind(produced) == "curve") == (port in curves), port
        written = [receipt.primary.name for receipt in report.receipts]
    assert len(written) == len(shapes) and all((tmp_path / "out" / file).exists() for file in written)
    assert sorted(file for file in written if file.endswith(".csv")) == sorted(
        f"cells_{name}_{port}.csv" for port in curves
    )
    for port in curves:
        table = np.loadtxt(tmp_path / "out" / f"cells_{name}_{port}.csv", delimiter=",", skiprows=1)
        assert table.shape == (shapes[port][0], 2) and np.array_equal(table[:, 0], np.arange(shapes[port][0]))


def test_a_workflow_file_runs_over_a_folder_and_each_file_gets_its_own_crop(registry, tmp_path):
    """The point of a recipe in a workflow: one file in, the same script over every recording."""
    from imswitch.improcess.workflows import bindings_for_inputs, run_over

    paths = []
    for index, (rows, cols) in enumerate([((10, 20), (10, 20)), ((30, 50), (40, 70))]):
        path = tmp_path / f"cells{index}.h5"
        image = np.zeros((2, 96, 96), np.float32)
        image[:, rows[0]:rows[1], cols[0]:cols[1]] = 10
        with h5py.File(str(path), "w") as handle:
            handle.create_dataset("data", data=image)
        paths.append(path)
    workflow = Workflow.load(EXAMPLES / "python_step_autocrop.yaml")
    batch, reports = run_over(
        workflow, bindings_for_inputs(workflow, paths), registry=registry, out_dir=tmp_path / "out", keep_reports=True,
    )
    assert batch.ok
    shapes = [report.result("script.out").data.shape for report in reports]
    assert shapes == [(2, 10 + 16, 10 + 16), (2, 20 + 16, 30 + 16)]
    for report in reports:
        report.close()


# -- the documentation -----------------------------------------------------------------

def test_every_recipe_is_in_the_docs_and_in_the_examples_readme():
    docs = (REPO / "docs" / "improcess.rst").read_text(encoding="utf-8")
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    for name in RECIPES:
        assert f"improcess_snippets/{name}.py" in docs, f"{name} is not shown in docs/improcess.rst"
        assert f"python_step_{name}.yaml" in readme, f"{name} has no row in the examples README"
