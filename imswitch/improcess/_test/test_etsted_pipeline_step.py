"""The ``etsted-pipeline`` processor: an etSTED analysis pipeline file run on a frame.

The calling convention (previous frames, exinfo, mask, testmode, parameters), the coordinate
convention pinned against ``dff_event``'s documentation, the state and its reset, the controls
the pipeline's parameters give a workflow, and the errors that name the file and the line.
"""

import textwrap
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.base import ProcessorInputError
from imswitch.improcess.processors.etsted_pipeline import EtstedPipelineProcessor
from imswitch.improcess.processors.etsted_pipeline.files import (
    PipelineFileError,
    analysis_folder,
    inspect_pipeline_file,
    list_pipelines,
    resolve_pipeline,
)
from imswitch.improcess.processors.etsted_pipeline.processor import (
    ACTION_STATE_KEY,
    EtstedPipelineError,
    parameter_docs,
)

import imswitch

DFF_EVENT = Path(imswitch.__file__).parent / "_data" / "user_defaults" / "imcontrol_etsted" / "analysis_pipelines" / "dff_event.py"
SHAPE = (256, 320)          # rows, columns: not square, so a row/column mix-up shows


def _image(image, index=0, detector="Camera"):
    return ArrayProcessingResult(f"{detector} #{index}", np.asarray(image), ["Y", "X"],
                                 metadata={"frame_index": index, "frame": {"detector": detector, "sequence": index}})


def _frame(rng, spikes=(), base=200.0):
    image = rng.poisson(base, SHAPE).astype(np.float32)
    yy, xx = np.mgrid[:SHAPE[0], :SHAPE[1]]
    for row, col, amplitude in spikes:
        image += amplitude * np.exp(-((yy - row) ** 2 + (xx - col) ** 2) / (2 * 4.0 ** 2))
    return image


class Step:
    """One step of a pipeline run: the processor with its between-frames state."""

    def __init__(self, **params):
        self.processor = EtstedPipelineProcessor()
        self.state = {}
        self.params = {"pipeline": str(DFF_EVENT), "ignore_frames": 0, **params}
        self.index = 0

    def push(self, image, **more):
        out = self.processor.apply(_image(image, self.index), {**self.processor.default_params(), **self.params,
                                                               **more, "_state": self.state})
        self.index += 1
        return out

    def points(self, image, **more):
        return self.push(image, **more).results[0]


def _write(tmp_path, name, source):
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    return path


# --------------------------------------------------------------- the contract

def test_the_adapter_is_registered_and_declares_what_it_produces():
    from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES, available_processor_specs
    from imswitch.improcess.processors.base import call_output_spec

    assert _AVAILABLE_PROCESSOR_CLASSES["etsted-pipeline"] is EtstedPipelineProcessor
    assert ("etsted-pipeline", "etSTED analysis pipeline", "Spots and points") in available_processor_specs()
    plain = call_output_spec(EtstedPipelineProcessor(), EtstedPipelineProcessor.default_params(), ("image",))
    assert plain.ports == ("out",) and plain.kind == "points"
    test = call_output_spec(EtstedPipelineProcessor(), {"testmode": True}, ("image",))
    assert test.ports == ("out", "analysis") and test.kind == "any"


def test_the_numeric_parameters_of_the_file_are_read_without_running_it(tmp_path):
    marker = tmp_path / "ran"
    path = _write(tmp_path, "probe", f"""
        '''Probe.

        gain   how much (2.5)
               continued here
        '''
        open({str(marker)!r}, 'w').write('ran')
        def probe(img, prev_frames, binary_mask, testmode, exinfo, bare, gain=2.5, flag=True, offset=-1,
                  nothing=None, weird=object()):
            return None, exinfo
    """)
    info = inspect_pipeline_file(path)
    assert not marker.exists()
    assert [(p.name, p.default, p.exposed) for p in info.params] == [
        ("bare", None, True), ("gain", 2.5, True), ("flag", True, True), ("offset", -1, True),
        ("nothing", None, False), ("weird", None, True)]
    fields = EtstedPipelineProcessor.step_param_spec({"pipeline": str(path)})
    assert [(f.key, f.type, f.default) for f in fields] == [
        ("bare", "float", 0.0), ("gain", "float", 2.5), ("flag", "bool", True), ("offset", "float", -1.0),
        ("weird", "float", 0.0)]
    assert fields[1].help == "how much (2.5) continued here"
    assert not marker.exists()
    assert parameter_docs(info.doc) == {"gain": "how much (2.5) continued here"}


def test_dff_events_parameters_are_the_fields_of_the_step():
    fields = {f.key: f for f in EtstedPipelineProcessor.step_param_spec({"pipeline": str(DFF_EVENT)})}
    assert list(fields) == ["dff_threshold", "bin_px", "baseline_frames", "smooth_px", "min_baseline", "warmup_frames"]
    assert fields["dff_threshold"].default == 0.5 and fields["bin_px"].default == 4.0
    assert "Raise it until a quiet sample never triggers" in fields["dff_threshold"].help


def test_a_pipeline_is_found_by_name_in_the_panels_folder_or_by_path(tmp_path, monkeypatch):
    from imswitch.imcommon.model import dirtools

    monkeypatch.setattr(dirtools.UserFileDirs, "Root", str(tmp_path))
    folder = tmp_path / "imcontrol_etsted" / "analysis_pipelines"
    folder.mkdir(parents=True)
    for name in ("alpha", "beta", "_private"):
        (folder / f"{name}.py").write_text("")
    other = tmp_path / "imcontrol_etsnouty" / "analysis_pipelines"
    other.mkdir(parents=True)
    (other / "gamma.py").write_text("")
    assert analysis_folder("etsted") == folder and analysis_folder("etsnouty") == other
    assert list_pipelines("etsted") == ["alpha", "beta"] and list_pipelines("etsnouty") == ["gamma"]
    assert list_pipelines("etmonalisa") == []
    assert resolve_pipeline("beta", "etsted") == folder / "beta.py"
    assert resolve_pipeline("gamma", "etsnouty") == other / "gamma.py"
    assert resolve_pipeline(str(other / "gamma.py")) == other / "gamma.py"
    with pytest.raises(PipelineFileError, match="no pipeline 'gamma'.*have: alpha, beta"):
        resolve_pipeline("gamma", "etsted")
    with pytest.raises(PipelineFileError, match="no analysis pipeline chosen"):
        resolve_pipeline("")


# ----------------------------------------------------------- the coordinates

def test_coordinates_are_row_then_column_with_pixel_centres_at_integers():
    """dff_event documents ``(column, row)`` of the full frame at the centre of the binned pixel, the first
    pixel's centre at 0.5, i.e. ``(j + 0.5) * bin_px``. The step publishes ``(row, column)`` with the first
    pixel's centre at 0: ``(j + 0.5) * bin_px - 0.5``."""
    rng = np.random.default_rng(1)
    step = Step()
    for _ in range(30):
        step.points(_frame(rng))
    spike_row, spike_col = 60, 200
    points = step.points(_frame(rng, spikes=[(spike_row, spike_col, 400.0)]))
    assert points.frame == "detector:Camera" and list(points.axis_names) == ["y", "x"]
    row, col = points.coordinates[0]
    bin_px = 4
    expected_row = (spike_row // bin_px + 0.5) * bin_px - 0.5
    expected_col = (spike_col // bin_px + 0.5) * bin_px - 0.5
    assert abs(row - expected_row) <= bin_px and abs(col - expected_col) <= bin_px
    # and the pipeline's own numbers are kept beside it, in its own order (column, row)
    c1, c2 = points.properties["etsted_c1"][0], points.properties["etsted_c2"][0]
    assert (c1, c2) == (col + 0.5, row + 0.5)
    assert c1 % bin_px == 2.0 and c2 % bin_px == 2.0          # the centre of a 4-px bin: (j + 0.5) * 4
    # the offset is a parameter: 0 for a pipeline that returns plain array indices
    again = step.points(_frame(rng, spikes=[(spike_row, spike_col, 400.0)]), coord_offset_px=0.0)
    assert tuple(again.coordinates[0]) == (c2, c1)


def test_detections_come_strongest_first_and_are_capped_by_max_points():
    rng = np.random.default_rng(2)
    step = Step()
    for _ in range(30):
        step.points(_frame(rng))
    spikes = [(40, 50, 300.0), (150, 250, 900.0), (200, 120, 500.0)]
    points = step.points(_frame(rng, spikes=spikes))
    assert points.count == 3 and list(points.properties["rank"]) == [0, 1, 2]
    assert abs(points.coordinates[0][0] - 150) <= 4 and abs(points.coordinates[0][1] - 250) <= 4
    assert abs(points.coordinates[2][1] - 50) <= 4
    capped = step.points(_frame(rng, spikes=spikes), max_points=2)
    assert capped.count == 2 and abs(capped.coordinates[0][0] - 150) <= 4


# ------------------------------------------------------- the calling convention

RECORDER = """
    import numpy as np
    LOG = []
    def recorder(img, prev_frames, binary_mask, testmode, exinfo, a=1.5, b=2, c=True, d=0):
        LOG.append(dict(n_prev=len(prev_frames), last_is_previous=(len(prev_frames) > 0 and
                        np.array_equal(prev_frames[-1], img - 1)), maxlen=prev_frames.maxlen,
                        mask=None if binary_mask is None else (binary_mask.dtype.name, binary_mask.shape, int(binary_mask.sum())),
                        testmode=testmode, exinfo=exinfo, values=(a, b, c, d),
                        types=tuple(type(v).__name__ for v in (a, b, c, d)), dtype=img.dtype.name))
        state = (exinfo or 0) + 1
        coords = np.array([[10.0, 20.0], [30.0, 40.0]])
        if testmode:
            return coords, state, np.full((3, 4), float(state))
        return coords, state
"""


def _recorder(tmp_path):
    return _write(tmp_path, "recorder", RECORDER)


def _log(step):
    return step.state["runner"].function.__globals__["LOG"]


def test_the_pipeline_gets_what_etsted_gives_it(tmp_path):
    path = _recorder(tmp_path)
    step = Step(pipeline=str(path))
    frames = [np.full((8, 6), float(i), dtype=np.uint16) for i in range(14)]
    for frame in frames:
        out = step.push(frame)
    log = _log(step)
    # prev_frames: the last 10 frames, oldest first, the current one not in it (it is added after the call)
    assert [entry["n_prev"] for entry in log] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10]
    assert all(entry["last_is_previous"] for entry in log[1:]) and log[0]["maxlen"] == 10
    # exinfo is what the last call returned; None on the first
    assert [entry["exinfo"] for entry in log] == [None] + list(range(1, 14))
    # numeric parameters: floats, in signature order, from the signature's defaults; a flag stays a flag
    assert log[0]["values"] == (1.5, 2.0, True, 0.0)
    assert log[0]["types"] == ("float", "float", "bool", "float")
    assert log[0]["mask"] is None and log[0]["testmode"] is False and log[0]["dtype"] == "uint16"
    assert out.keys == ("out",)
    # the values set on the step, or by name, win; a parameter with no default starts at 0
    step.push(frames[0], a=7, b=0.25, c=False)
    step.push(frames[0], params={"d": 3, "a": 1})
    assert _log(step)[-2]["values"] == (7.0, 0.25, False, 0.0)
    assert _log(step)[-1]["values"] == (1.0, 2.0, True, 3.0)
    assert out.results[0].metadata["detected"] == 2


def test_prev_frames_has_the_requested_length_and_testmode_gives_an_analysis_port(tmp_path):
    path = _recorder(tmp_path)
    step = Step(pipeline=str(path), prev_frames=3, testmode=True)
    for i in range(6):
        out = step.push(np.full((8, 6), float(i)))
    assert [e["n_prev"] for e in _log(step)] == [0, 1, 2, 3, 3, 3] and _log(step)[-1]["maxlen"] == 3
    assert _log(step)[-1]["testmode"] is True
    assert out.keys == ("out", "analysis")
    analysis = out.results[1]
    assert analysis.data.shape == (3, 4) and float(analysis.data[0, 0]) == 6.0
    assert out.results[0].count == 2


def test_the_first_frames_are_run_but_report_nothing(tmp_path):
    path = _recorder(tmp_path)
    step = Step(pipeline=str(path), ignore_frames=6)
    counts = [step.points(np.zeros((8, 6))).count for _ in range(9)]
    assert counts == [0] * 6 + [2] * 3
    assert len(_log(step)) == 9                       # the pipeline ran on all of them (it builds its state)
    assert step.points(np.zeros((8, 6))).metadata["state"] == "running"


def test_a_mask_reaches_the_pipeline_as_a_boolean_array(tmp_path):
    path = _recorder(tmp_path)
    mask = np.zeros((8, 6), dtype=np.uint8)
    mask[2:4] = 7
    np.save(tmp_path / "mask.npy", mask)
    from imswitch.improcess.model.array_result import ArrayProcessingResult as Labels

    step = Step(pipeline=str(path), mask="file", mask_path=str(tmp_path / "mask.npy"))
    step.push(np.zeros((8, 6)))
    assert _log(step)[0]["mask"] == ("bool", (8, 6), 12)
    step = Step(pipeline=str(path), mask="input")
    step.processor.apply(_image(np.zeros((8, 6))), {**step.processor.default_params(), "pipeline": str(path),
                         "mask": "input", "results": [_image(np.zeros((8, 6))), Labels("m", mask, ["Y", "X"])],
                         "_state": step.state})
    assert _log(step)[0]["mask"] == ("bool", (8, 6), 12)
    with pytest.raises(ProcessorInputError, match="second input"):
        Step(pipeline=str(path), mask="input").push(np.zeros((8, 6)))
    with pytest.raises(ProcessorInputError, match="mask has shape"):
        Step(pipeline=str(path), mask="file", mask_path=str(tmp_path / "mask.npy")).push(np.zeros((9, 6)))


# ------------------------------------------------------------------ the state

def test_the_state_is_reset_after_a_triggered_scan_as_etsted_does(tmp_path):
    path = _recorder(tmp_path)
    step = Step(pipeline=str(path), ignore_frames=2)
    for i in range(5):
        step.push(np.full((8, 6), float(i)))
    assert _log(step)[-1]["exinfo"] == 4 and _log(step)[-1]["n_prev"] == 4
    info = step.state[ACTION_STATE_KEY]
    # a scan is running: the pipeline is not called, nothing is reported
    info["busy"] = True
    paused = step.points(np.zeros((8, 6)))
    assert paused.count == 0 and paused.metadata["message"].startswith("paused") and len(_log(step)) == 5
    # the scan ended (the engine counts the chain complete): exinfo, previous frames and the count restart
    info["busy"], info["epoch"] = False, 1
    first = step.points(np.zeros((8, 6)))
    log = _log(step)
    assert log[-1]["exinfo"] is None and log[-1]["n_prev"] == 0
    assert first.count == 0 and first.metadata["state"].startswith("ignoring")       # the ignore count restarted
    step.push(np.zeros((8, 6)))
    assert step.points(np.zeros((8, 6))).count == 2
    assert _log(step)[-1]["exinfo"] == 2
    # off: the hook is ignored
    step = Step(pipeline=str(path), after_scan="nothing")
    step.push(np.zeros((8, 6)))
    step.state[ACTION_STATE_KEY].update(busy=True, epoch=3)
    step.push(np.zeros((8, 6)))
    assert [e["exinfo"] for e in _log(step)] == [None, 1]
    # etSNOUTY: only the frame count restarts (exinfo and the previous frames are kept)
    step = Step(pipeline=str(path), after_scan="restart-count", ignore_frames=2)
    for _ in range(4):
        step.push(np.zeros((8, 6)))
    step.state[ACTION_STATE_KEY].update(busy=False, epoch=1)
    again = step.points(np.zeros((8, 6)))
    assert _log(step)[-1]["exinfo"] == 4 and _log(step)[-1]["n_prev"] == 4
    assert again.count == 0 and again.metadata["state"].startswith("ignoring")


def test_a_new_run_starts_with_a_fresh_state_and_an_edited_file_is_read_again(tmp_path):
    import os

    path = _recorder(tmp_path)
    step = Step(pipeline=str(path))
    step.push(np.zeros((8, 6)))
    step.push(np.zeros((8, 6)))
    assert _log(step)[-1]["exinfo"] == 1
    fresh = Step(pipeline=str(path))               # the engine hands a new state dict at arm
    fresh.push(np.zeros((8, 6)))
    assert _log(fresh)[-1]["exinfo"] is None
    source = path.read_text().replace("(a=1.5", "(a=9.5").replace("a=1.5", "a=9.5")
    path.write_text(source)
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    step.push(np.zeros((8, 6)))
    assert _log(step)[-1]["values"][0] == 9.5 and _log(step)[-1]["exinfo"] is None


# ------------------------------------------------------------------- errors

def test_a_pipeline_that_raises_names_its_file_and_line(tmp_path):
    path = _write(tmp_path, "broken", """
        import numpy as np
        def helper(x):
            return x / 0
        def broken(img, prev_frames, binary_mask, testmode, exinfo, k=1):
            return helper(k), exinfo
    """)
    with pytest.raises(EtstedPipelineError) as raised:
        Step(pipeline=str(path)).push(np.zeros((4, 4)))
    assert "broken.py:4 in helper: ZeroDivisionError: " in str(raised.value)


def test_a_file_that_does_not_load_or_does_not_fit_names_what_is_wrong(tmp_path):
    syntax = _write(tmp_path, "syntax", "def syntax(img, prev_frames, binary_mask, testmode, exinfo):\n    return (\n")
    with pytest.raises(EtstedPipelineError, match=r"syntax\.py:2.*SyntaxError"):
        Step(pipeline=str(syntax)).push(np.zeros((4, 4)))
    with pytest.raises(PipelineFileError, match=r"syntax\.py:\d+: syntax error"):
        inspect_pipeline_file(syntax)
    wrong = _write(tmp_path, "wrong", "def wrong(img, exinfo):\n    return None, exinfo\n")
    with pytest.raises(EtstedPipelineError, match="missing required parameters"):
        Step(pipeline=str(wrong)).push(np.zeros((4, 4)))
    assert inspect_pipeline_file(wrong).problems and "prev_frames" in inspect_pipeline_file(wrong).problems[0]
    named = _write(tmp_path, "named", "def other(img, prev_frames, binary_mask, testmode, exinfo):\n    return None, exinfo\n")
    with pytest.raises(PipelineFileError, match="must define a function called 'named'"):
        inspect_pipeline_file(named)
    bad_shape = _write(tmp_path, "shape", """
        def shape(img, prev_frames, binary_mask, testmode, exinfo):
            return [[1, 2, 3]], exinfo
    """)
    with pytest.raises(EtstedPipelineError, match=r"shape\.py.*expected \(2,\) or \(N, 2\)"):
        Step(pipeline=str(bad_shape)).push(np.zeros((4, 4)))
    with pytest.raises(ProcessorInputError, match="no pipeline 'nothing'"):
        Step(pipeline="nothing", folder=str(tmp_path)).push(np.zeros((4, 4)))
    with pytest.raises(ProcessorInputError, match="needs a .Y, X. camera frame"):
        Step(pipeline=str(DFF_EVENT)).push(np.zeros((2, 4, 4)))
    # a load failure is remembered: the file is not run again on every frame
    marker = tmp_path / "count"
    counted = _write(tmp_path, "counted", f"""
        with open({str(marker)!r}, 'a') as handle:
            handle.write('x')
        raise ValueError('nope')
    """)
    step = Step(pipeline=str(counted))
    for _ in range(3):
        with pytest.raises(EtstedPipelineError, match=r"counted\.py:4 in <module>: ValueError: nope"):
            step.push(np.zeros((4, 4)))
    assert marker.read_text() == "x"


def test_a_pipeline_parameter_named_like_a_setting_of_the_step_is_given_in_params(tmp_path):
    path = _write(tmp_path, "clash", """
        def clash(img, prev_frames, binary_mask, testmode, exinfo, mask=3.0, k=1.0):
            return [[1.0, 2.0]], (mask, k)
    """)
    assert [f.key for f in EtstedPipelineProcessor.step_param_spec({"pipeline": str(path)})] == ["k"]
    step = Step(pipeline=str(path), params={"mask": 8})
    step.push(np.zeros((4, 4)))
    assert step.state["exinfo"] == (8.0, 1.0)


# ------------------------------------------------------------------- controls

def test_a_workflow_control_can_set_a_pipeline_parameter():
    """``etsted.dff_threshold`` is a valid control target, with the pipeline's own default and help."""
    from imswitch.imcommon.model.smartpipeline import Live, SmartPipelineDocument
    from imswitch.imcommon.model.workflow.steps import Control, Process, Workflow
    from imswitch.imcontrol.model.smartpipeline.parameters import checked_value, exposed_parameters
    from imswitch.improcess.workflows.runtime import bootstrap_registry

    registry = bootstrap_registry(user_plugins=False)
    workflow = Workflow("etsted", [
        Live("cam", detector="Camera"),
        Process("etsted", "etsted-pipeline", {"pipeline": str(DFF_EVENT), "dff_threshold": 0.8}, inputs=["cam"]),
    ], controls=[Control("threshold", ["etsted.dff_threshold"], label="Threshold"),
                 Control("typo", ["etsted.not_a_parameter"])])
    parameters = {p.name: p for p in exposed_parameters(workflow, registry=registry)}
    threshold = parameters["threshold"]
    assert threshold.usable and threshold.file_value == 0.8 and threshold.spec.reset == 0.5 and threshold.spec.type == 'float'
    assert "Raise it until" in threshold.help
    assert checked_value(threshold, 0.25) == 0.25
    assert not parameters["typo"].usable and "not_a_parameter" in parameters["typo"].problem
    document = SmartPipelineDocument(workflow)
    issues = [str(i) for i in document.validate(registry=registry) if "etsted" in str(i)]
    assert any("not_a_parameter" in text for text in issues)           # the typo is reported ...
    assert not any("dff_threshold" in text for text in issues)         # ... the real parameter is not


# ------------------------------------------------------------------- the designer

def test_the_designer_exposes_a_pipeline_parameter_with_the_pipelines_own_value():
    """Exposing ``etsted.dff_threshold`` writes the pipeline's default (not nothing) and the form lists it."""
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
    from imswitch.improcess.workflows import Process, Workflow, bootstrap_registry
    from imswitch.improcess.workflows.steps import Source

    catalog = build_catalog(bootstrap_registry(user_plugins=False))
    document = WorkflowDocument(Workflow("p", [
        Source("raw", path=None),
        Process("etsted", "etsted-pipeline", {"pipeline": str(DFF_EVENT)}, inputs=["raw"]),
    ]), catalog=catalog)
    entry = document.entry_for(document.step("etsted"))
    assert [f.key for f in entry.step_fields(document.step("etsted").params)] == [
        "dff_threshold", "bin_px", "baseline_frames", "smooth_px", "min_baseline", "warmup_frames"]
    assert document.defaults_for("etsted")["dff_threshold"] == 0.5
    with document.history.edit("Expose"):
        control = document.add_control("threshold", "etsted.dff_threshold")
    assert control.targets == ["etsted.dff_threshold"]
    assert document.step("etsted").params["dff_threshold"] == 0.5
    with document.history.edit("Set"):
        document.set_control_value("threshold", 0.7)
    assert document.step("etsted").params["dff_threshold"] == 0.7


def test_the_inspector_form_shows_the_pipelines_parameters():
    pytest.importorskip("pyqtgraph")
    from qtpy import QtWidgets

    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
    from imswitch.improcess.workflows import Process, Workflow, bootstrap_registry
    from imswitch.improcess.workflows.steps import Source
    from imswitch.imcommon.view.workfloweditor.inspector import Inspector

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    catalog = build_catalog(bootstrap_registry(user_plugins=False))
    document = WorkflowDocument(Workflow("p", [
        Source("raw", path=None),
        Process("etsted", "etsted-pipeline", {"pipeline": str(DFF_EVENT)}, inputs=["raw"]),
    ]), catalog=catalog)
    inspector = Inspector()
    inspector.set_document(document)
    inspector.set_editor(lambda label, action, **kwargs: (action(), True)[1])
    inspector.show_step("etsted")
    shown = set(inspector.param_form._fields)
    assert {"pipeline", "dff_threshold", "warmup_frames"} <= shown
    del app
