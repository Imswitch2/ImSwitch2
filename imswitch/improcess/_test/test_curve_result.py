"""A one-dimensional result is a curve: drawn in the Graph panel, saved as a table.

The Python step's 1-D outputs used to become image results with nothing to
show -- a blank graph, no image, and a TIFF save that failed on "a TIFF page
needs two axes". These tests pin what a curve does instead, at each place it
travels: the result itself, the Graph widget, the save formats, the Python
step that produces one, and the controller that reveals the Graph dock.
"""

import json
import os
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController  # noqa: E402
from imswitch.improcess.model import result_kind  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.curve_result import CurveResult  # noqa: E402
from imswitch.improcess.model.save_protocol import UnsupportedSaveFormat, companion_json_path  # noqa: E402
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES  # noqa: E402
from imswitch.improcess.processors.python_step import PythonStepProcessor  # noqa: E402
from imswitch.improcess.processors.python_step.context import ScriptError, run_script  # noqa: E402
from imswitch.improcess.view.GraphWidget import GraphWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _stack(frames=6, size=4, scales=None, unit="px"):
    """Frame ``i`` is the constant ``i``: a per-frame mean is ``0, 1, 2, ...``."""
    data = np.broadcast_to(np.arange(frames, dtype=np.float32)[:, None, None], (frames, size, size))
    return ArrayProcessingResult("rec", np.array(data), ["Frame", "Y", "X"], axis_scales=scales, scale_unit=unit)


def _curve(values=(1.0, 4.0, 9.0, 16.0), label="Frame", scale=1.0, unit="px", name="trace"):
    return CurveResult(name, np.array(values), [label], axis_scales=[scale], scale_unit=unit)


# -- the result ---------------------------------------------------------------

def test_a_curve_is_one_dimensional_and_says_so_in_its_kind():
    curve = _curve()
    assert result_kind(curve) == "curve"
    assert curve.data.shape == (4,)
    for bad in (np.zeros((3, 3)), np.float32(2.0)):
        with pytest.raises(ValueError, match="one-dimensional"):
            CurveResult("bad", bad, ["Frame"])


def test_x_is_the_sample_index_times_the_axis_scale():
    np.testing.assert_allclose(_curve(scale=0.5).x_values, [0.0, 0.5, 1.0, 1.5])
    np.testing.assert_allclose(_curve().x_values, [0, 1, 2, 3])


@pytest.mark.parametrize(
    "label, scale, unit, expected",
    [
        ("Z", 0.25, "um", "Z (um)"),          # a calibrated spatial axis carries the pixel unit
        ("Z", 1.0, "um", "Z"),                # ...but a scale of 1 is no calibration
        ("Z", 0.25, "px", "Z"),               # ...and "px" is not a unit to caption with
        ("Frame", 0.25, "um", "Frame"),       # a frame axis is not measured in micrometres
        ("T", 0.5, "um", "T (s)"),            # time is seconds, as the OME writer assumes
        ("Index", 2.0, "um", "Index"),
    ],
)
def test_the_x_axis_is_captioned_with_a_unit_only_when_the_axis_has_one(label, scale, unit, expected):
    assert _curve(label=label, scale=scale, unit=unit).x_label() == expected


def test_no_processor_is_offered_a_curve():
    """The kind gate that keeps FRC curves out of image processors holds for this one too."""
    for cls in _AVAILABLE_PROCESSOR_CLASSES.values():
        assert not cls().accepts(_curve()), cls.id


# -- the Graph panel ----------------------------------------------------------

def test_the_payload_is_one_line_over_the_axis():
    (payload,) = _curve(values=(2.0, 4.0, 8.0), label="T", scale=0.5, unit="um", name="bleach").plot_payloads()
    assert payload.title == "bleach" and payload.x_label == "T (s)"
    (series,) = payload.series
    assert series.kind == "line" and series.name == "bleach"
    np.testing.assert_allclose(series.x, [0.0, 0.5, 1.0])
    np.testing.assert_allclose(series.y, [2.0, 4.0, 8.0])


def test_the_graph_widget_draws_the_curve(qapp):
    graph = GraphWidget()
    graph.setPlotPayloads(_curve(values=(3.0, 1.0, 2.0)).plot_payloads())
    drawn = [item for item in graph.plot.plotItem.items if hasattr(item, "xData")]
    assert len(drawn) == 1
    np.testing.assert_allclose(drawn[0].yData, [3.0, 1.0, 2.0])
    np.testing.assert_allclose(drawn[0].xData, [0.0, 1.0, 2.0])


def test_producing_a_curve_reveals_the_graph_dock_and_not_the_results_table():
    shown = SimpleNamespace(appended=[], raised=[])
    shown.appendResultTableRecords = lambda columns, records: shown.appended.append(records)
    shown.raiseDockByTitle = lambda title: shown.raised.append(title) or True
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = shown
    controller._ImProcessMainController__logger = SimpleNamespace(debug=lambda *a, **k: None, exception=lambda *a, **k: None)

    controller._routeResultToAnalysisPanels(_curve())

    assert shown.raised == ["Graph"] and shown.appended == []


# -- saving -------------------------------------------------------------------

def test_a_curve_saves_as_csv_with_the_axis_beside_the_values(tmp_path):
    curve = _curve(values=(1.0, 4.0, 9.0), label="Z", scale=0.5, unit="um", name="focus score")
    receipt = curve.save(tmp_path / "trace.csv")

    lines = (tmp_path / "trace.csv").read_text().splitlines()
    assert lines[0] == "Z [um],focus score"
    np.testing.assert_allclose(np.loadtxt(tmp_path / "trace.csv", delimiter=",", skiprows=1), [[0.0, 1.0], [0.5, 4.0], [1.0, 9.0]])
    # the provenance goes in a listed companion, as it does for the FRC curve
    assert companion_json_path(tmp_path / "trace.csv") in receipt.files
    assert "schema" in json.loads(companion_json_path(tmp_path / "trace.csv").read_text())


def test_a_name_with_a_comma_does_not_split_the_csv_header(tmp_path):
    _curve(name='mean, "bright" pixels').save(tmp_path / "t.csv")
    assert (tmp_path / "t.csv").read_text().splitlines()[0] == 'Frame,"mean, ""bright"" pixels"'


def test_a_curve_saves_as_hdf5_with_its_axis_and_values(tmp_path):
    _curve(values=(5.0, 6.0, 7.0), label="Z", scale=0.5, unit="um").save(tmp_path / "trace.h5")
    with h5py.File(tmp_path / "trace.h5", "r") as handle:
        np.testing.assert_allclose(handle["data"][...], [5.0, 6.0, 7.0])
        assert handle["data"].attrs["axis_labels"] == "Z"
        assert handle["data"].attrs["scale_unit"] == "um"


def test_a_curve_saves_as_zarr(tmp_path):
    zarr = pytest.importorskip("zarr")
    _curve(values=(5.0, 6.0, 7.0)).save(tmp_path / "trace.ome.zarr")
    np.testing.assert_allclose(zarr.open_group(str(tmp_path / "trace.ome.zarr"), mode="r")["0"][...], [5.0, 6.0, 7.0])


def test_a_curve_is_refused_as_tiff_before_anything_is_written(tmp_path):
    with pytest.raises(UnsupportedSaveFormat, match="supports csv, hdf5, zarr"):
        _curve().save(tmp_path / "trace.ome.tif")
    assert list(tmp_path.iterdir()) == []


# -- the Python step makes them -----------------------------------------------

def test_a_one_dimensional_output_is_a_curve_along_the_named_axis():
    (result,), _ = run_script(
        'out = make_result(data.mean(axis=(1, 2)), axes=["Frame"], scales=[0.5], name="mean")',
        [_stack()], ("out",),
    )
    assert isinstance(result, CurveResult) and result_kind(result) == "curve"
    assert result.name == "mean" and result.axis_labels == ["Frame"] and result.axis_scales == [0.5]
    np.testing.assert_allclose(result.data, np.arange(6))


def test_a_curve_keeps_the_calibration_of_the_axis_it_shares_with_the_input():
    (result,), _ = run_script(
        'out = make_result(data.mean(axis=(1, 2)), axes=["Frame"])', [_stack(scales=[2.0, 0.5, 0.5], unit="um")], ("out",),
    )
    assert result.axis_scales == [2.0]


def test_a_one_dimensional_output_from_a_one_dimensional_input_needs_no_axes():
    reference = _curve(label="T", scale=0.25)
    (result,), _ = run_script("out = data / data.max()", [reference], ("out",))
    assert isinstance(result, CurveResult)
    assert result.axis_labels == ["T"] and result.axis_scales == [0.25]


def test_a_curve_is_one_of_several_outputs_beside_an_image():
    code = (
        'outputs = {"image": make_result(data[0], axes=["Y", "X"]), '
        '"trace": make_result(data.mean(axis=(1, 2)), axes=["Frame"])}'
    )
    ports = ("image", "trace")
    (image, trace), _ = run_script(code, [_stack()], ports)
    assert result_kind(image) == "image" and result_kind(trace) == "curve"


def test_the_step_hint_for_a_one_dimensional_output_shows_the_call_to_make():
    with pytest.raises(ScriptError) as caught:
        run_script("out = data.mean(axis=(1, 2))", [_stack()], ("out",))
    message = str(caught.value)
    assert "1 dimensions but the input has 3 (Frame, Y, X)" in message
    assert 'make_result(array, axes=["Frame"])' in message


def test_a_single_number_is_not_an_output_and_the_message_says_what_to_do():
    with pytest.raises(ScriptError) as caught:
        run_script("out = float(data.mean())", [_stack()], ("out",))
    message = str(caught.value)
    assert "'out' is a single number" in message and "print()" in message


def test_the_step_applies_to_a_stack_and_returns_a_curve_through_the_processor():
    params = {"code": 'out = make_result(data.mean(axis=(1, 2)), axes=["Frame"])', "ports": "out"}
    output = PythonStepProcessor().apply(_stack(), params)
    (result,) = output.results
    assert isinstance(result, CurveResult)
    np.testing.assert_allclose(result.data, np.arange(6))


def test_a_workflow_runs_a_script_to_a_curve_and_saves_it_as_csv(tmp_path):
    from imswitch.improcess.workflows import Process, Reconstruct, Save, Source, Workflow, bootstrap_registry, run, validate

    registry = bootstrap_registry(user_plugins=False)
    raw = tmp_path / "frames.h5"
    planes = np.broadcast_to(np.arange(5, dtype=np.float32)[:, None, None], (5, 8, 8))
    with h5py.File(str(raw), "w") as handle:
        handle.create_dataset("data", data=np.array(planes)).attrs["axes"] = "CYX"
    code = 'out = make_result(data.mean(axis=(1, 2)), axes=["Frame"], name="frame mean")\n'
    workflow = Workflow("trace", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("trace", "python", {"code": code, "ports": "out"}, inputs=["rec"]),
        Save("csv", input="trace.out", fmt="csv", path_template="{out_dir}/{source_stem}_trace{ext}"),
    ])
    assert validate(workflow, registry) == []
    with run(workflow, registry=registry, out_dir=tmp_path / "out") as report:
        assert report.ok
        assert isinstance(report.result("trace.out"), CurveResult)
        written = report.receipts[0].primary
    assert written.name == "frames_trace.csv"
    np.testing.assert_allclose(
        np.loadtxt(written, delimiter=",", skiprows=1), np.column_stack([np.arange(5), np.arange(5)])
    )


def test_the_console_publishes_a_curve_and_can_read_one_back():
    from imswitch.improcess.processors.python_step.console import ConsoleSession

    stack, made = _stack(), []
    session = ConsoleSession(selected=lambda: [stack], current=lambda: stack, publish=lambda result, name: made.append(result))
    namespace = session.namespace
    curve = session.publish(np.asarray(namespace["data"]).mean(axis=(1, 2)), axes=["Frame"], name="mean")
    assert isinstance(curve, CurveResult) and made == [curve] and curve.name == "mean"
    # selecting the curve rebinds `data` to its one dimension: a console can post-process what a step made
    shown = [curve]
    session = ConsoleSession(selected=lambda: shown, current=lambda: curve, publish=lambda result, name: made.append(result))
    assert session.namespace["data"].shape == (6,) and session.namespace["axes"] == ["Frame"]
    with pytest.raises(ScriptError, match="single number"):
        session.publish(float(np.asarray(session.namespace["data"]).max()))


def test_a_step_given_a_curve_stops_the_run_and_says_why(tmp_path):
    """A curve ends a chain: validation cannot know a script's output kind, the run can."""
    from imswitch.improcess.workflows import Process, Reconstruct, RunError, Source, Workflow, bootstrap_registry, run, validate

    registry = bootstrap_registry(user_plugins=False)
    raw = tmp_path / "frames.h5"
    with h5py.File(str(raw), "w") as handle:
        handle.create_dataset("data", data=np.zeros((5, 8, 8), np.float32)).attrs["axes"] = "CYX"
    workflow = Workflow("chain", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("a", "python", {"code": 'out = make_result(data.mean(axis=(1, 2)), axes=["Frame"])', "ports": "out"}, inputs=["rec"]),
        Process("b", "python", {"code": "out = data * 2", "ports": "out"}, inputs=["a.out"]),
    ])
    assert validate(workflow, registry) == []
    with pytest.raises(RunError, match=r"b: 'python' does not accept result .*kind 'curve'"):
        with run(workflow, registry=registry, out_dir=tmp_path / "out"):
            pass
