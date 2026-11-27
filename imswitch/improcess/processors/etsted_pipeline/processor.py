"""Image (+ mask) -> points: an etSTED analysis pipeline file run on every frame.

The adapter between etSTED's analysis pipelines and a smart pipeline. An
etSTED pipeline is a Python file ``<name>.py`` in the panel's
``analysis_pipelines`` folder defining ``<name>(img, prev_frames,
binary_mask, testmode, exinfo, **numeric_params)``, which returns the
coordinates of the events it detects (rows of ``(column, row)`` of the full
frame, strongest first) and an ``exinfo`` object that etSTED hands back with
the next frame. This step calls that very function with the arguments etSTED
would pass, and publishes the detections as a points result in the
camera's own frame (``(row, column)`` with pixel centres at integers, the
smart pipeline's convention) that a ``pick``, a ``transform`` and a
``run-scan`` take like any others. The file is **not** rewritten: any
pipeline an etSTED panel loads works here.

**The calling convention (what etSTED does, and so what this does)**

* ``img``: the camera frame as it arrived; ``prev_frames``: a deque of the
  last ``prev_frames`` (10) frames, oldest first, the current one NOT in it
  (it is appended after the call); ``binary_mask``: a boolean array or
  ``None`` (see ``mask``); ``testmode``: the ``testmode`` parameter (etSTED
  passes True in Visualize and Validate, where the pipeline also returns an
  analysis image); ``exinfo``: what the pipeline returned last time, ``None``
  on the first frame; then every numeric parameter, as a float, in the order
  of the signature (the value set here, else the signature's default, else
  0 as etSTED's panel).
* The first ``ignore_frames`` (6, etSTED's ``INIT_FRAMES + 1``) frames after
  the start, and after each triggered scan, are run (so the pipeline builds
  its state) but report no detections.
* After a triggered scan etSTED clears ``exinfo`` and the previous frames and
  starts the frame count again; so does this step, when the engine tells it
  that a scan was started and has ended (``after_scan`` ``reset``; etSNOUTY
  only restarts the frame count: ``restart-count``). While the scan runs the
  step does not call the pipeline (etSTED pauses its fast camera). Validate
  and Visualize runs scan nothing, and reset nothing.
* State (``exinfo``, the previous frames) belongs to this step and is
  discarded when the pipeline is armed again, or when the pipeline file
  changes on disk.

**Parameters and controls.** The pipeline's own numeric parameters are
step parameters of the same name, set beside ``pipeline`` (``dff_threshold:
0.5``); their fields, defaults and help (from the file's docstring) are read
from the file's syntax tree without running it, so a workflow control can
target them (``etsted.dff_threshold``) and the panel shows them. A parameter
whose name is also one of this step's own settings is given in ``params``
(``{"name": value}``) instead.

**Trust.** Like etSTED's panel and the Python step, this runs the Python file
you point it at, with your privileges; only use files you trust.

**Coordinates.** The pipeline's ``(column, row)`` becomes ``(row, column)``;
``coord_offset_px`` (0.5) is subtracted from both: ``dff_event`` reports the
centre of a binned pixel with the first pixel's centre at 0.5, the smart
pipeline puts it at 0. A pipeline that returns plain array indices needs 0.

Outputs: ``out`` (points, strongest first; properties ``rank``, ``etsted_c1``
and ``etsted_c2``, the pipeline's own numbers) and, with ``testmode`` on,
``analysis`` (the pipeline's analysis image).
"""

from __future__ import annotations

import re
from collections import deque
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import ProcessingResult, result_kind
from imswitch.improcess.processors.base import (
    OutputSpec,
    Processor,
    ProcessorInputError,
    ProcessorOutput,
)
from imswitch.improcess.processors.etsted_pipeline.files import (
    FOLDERS,
    PipelineFileError,
    PipelineInfo,
    PipelineParam,
    error_text,
    inspect_pipeline_file,
    list_pipelines,
    resolve_pipeline,
)

STATE_PARAM = "_state"
#: The key under which the engine tells this step about actions (``{"busy": bool, "epoch": int}``,
#: kept in the step's state; the engine writes it only into a state that holds it).
ACTION_STATE_KEY = "engine_actions"

MASK_SOURCES = ("none", "input", "file")
#: What a triggered scan does to the analysis: etSTED / etMonalisa clear exinfo and the previous frames and
#: restart the frame count; etSNOUTY only restarts the count; "nothing" keeps analysing through it.
AFTER_SCAN = ("reset", "restart-count", "nothing")
FOLDER_CHOICES = tuple(FOLDERS)
#: etSTED's ``INIT_FRAMES`` is 5 and a frame is ignored while the count is <= it: six frames.
DEFAULT_IGNORE_FRAMES = 6
#: etSTED's previous-frames deque.
DEFAULT_PREV_FRAMES = 10
POINTS_PORT = "out"
ANALYSIS_PORT = "analysis"


class EtstedPipelineError(RuntimeError):
    """The pipeline file raised or returned something etSTED would refuse; names file and line."""


class EtstedPipelineProcessor(Processor):
    """Run an etSTED analysis pipeline file on each frame; its detections are the points."""

    name = "etSTED analysis pipeline"
    id = "etsted-pipeline"
    category = "Spots and points"
    kinds = ("image", "labels")
    output_kind = "points"
    min_inputs = 1
    max_inputs = 2
    #: A pipeline's own parameters are step parameters beside these (see :meth:`step_param_spec`).
    extra_param_keys = None
    #: Not "whole image in, same image out": the output is points.
    preserves_grid = None

    @classmethod
    def default_params(cls) -> dict:
        return {
            "pipeline": "",
            "folder": "etsted",
            "frame": UNKNOWN_FRAME,
            "prev_frames": DEFAULT_PREV_FRAMES,
            "ignore_frames": DEFAULT_IGNORE_FRAMES,
            "mask": "none",
            "mask_path": "",
            "max_points": 0,
            "coord_offset_px": 0.5,
            "after_scan": "reset",
            "testmode": False,
            "params": {},
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("pipeline", "text", "", label="Pipeline", group="Pipeline",
                       help="The analysis pipeline: a name from the folder below (the file name without .py), "
                            "or the path of a .py file defining a function of that name"),
            ParamField("folder", "select", "etsted", label="Folder of", options=FOLDER_CHOICES, group="Pipeline",
                       help="Whose analysis_pipelines folder a pipeline name is looked up in: etSTED, etSNOUTY or "
                            "etMonalisa (their pipelines are called the same way)"),
            ParamField("frame", "text", UNKNOWN_FRAME, label="Frame", group="Output",
                       help="Frame of reference written on the points, e.g. detector:Camera; unset: the camera "
                            "the frame came from"),
            ParamField("prev_frames", "int", DEFAULT_PREV_FRAMES, label="Previous frames", min=1, max=1000,
                       group="Calling", help="How many earlier frames the pipeline gets in prev_frames "
                                             "(etSTED: 10)"),
            ParamField("ignore_frames", "int", DEFAULT_IGNORE_FRAMES, label="Ignore first frames", min=0,
                       max=100000, group="Calling",
                       help="Frames run but not reported after the start and after each triggered scan "
                            "(etSTED: 6)"),
            ParamField("mask", "select", "none", label="Binary mask", options=MASK_SOURCES, group="Calling",
                       help="none: binary_mask is None; input: the second input (a labels or mask image; "
                            "non-zero = inside); file: a mask image (.npy / .tif)"),
            ParamField("mask_path", "text", "", label="Mask file", group="Calling",
                       help="The mask image to load (.npy or .tif); mask from file"),
            ParamField("max_points", "int", 0, label="Max points", min=0, max=100000, group="Output",
                       help="Keep at most this many detections, the strongest; 0 = all"),
            ParamField("coord_offset_px", "float", 0.5, label="Pixel offset", min=-1000.0, max=1000.0, decimals=3,
                       suffix="px", group="Output",
                       help="Subtracted from the pipeline's coordinates: 0.5 for a pipeline whose first pixel "
                            "centre is at 0.5 (dff_event), 0 for plain array indices"),
            ParamField("after_scan", "select", "reset", label="After a scan", options=AFTER_SCAN, group="Calling",
                       help="reset: as etSTED and etMonalisa, forget exinfo and the previous frames and ignore the "
                            "next frames again; restart-count: as etSNOUTY, only ignore the next frames again; "
                            "nothing: keep analysing. Except for 'nothing' the pipeline is not called during the scan"),
            ParamField("testmode", "bool", False, label="Test mode", group="Calling",
                       help="Pass testmode=True: the pipeline also returns its analysis image (second output "
                            "'analysis'). etSTED does in Visualize and Validate"),
            ParamField("params", "json", {}, label="Parameters by name", group="Pipeline", advanced=True,
                       help="{name: value} for a pipeline parameter that cannot be a step parameter (its name is "
                            "one of the settings above)"),
        )

    # ------------------------------------------------------------ the pipeline's own parameters

    @classmethod
    def step_param_spec(cls, params: dict | None) -> tuple:
        """The fields of the pipeline's own parameters, for a step whose ``pipeline`` is ``params['pipeline']``.

        Read from the file's syntax tree (nothing runs); empty when no pipeline is named, and
        :class:`PipelineFileError` (file and line) when the one named cannot be read. What the control machinery asks for (``control_states``) beyond :meth:`param_spec`.
        """
        params = params or {}
        if not str(params.get("pipeline") or "").strip():
            return ()
        # a pipeline that cannot be read raises: the control machinery quotes the reason
        return pipeline_fields(inspect_pipeline_file(resolve_pipeline(params.get("pipeline"),
                                                                       params.get("folder", "etsted"))))

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: np.asarray(result.data).ndim == 2

    def check_inputs(self, results) -> tuple[bool, str]:
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        results = list(results or [])
        if result_kind(results[0]) != "image":
            return False, f"{self.name} takes a camera image first; '{getattr(results[0], 'name', 'input 1')}' is " \
                          f"{result_kind(results[0])}"
        if len(results) == 2 and tuple(np.asarray(results[0].data).shape) != tuple(np.asarray(results[1].data).shape):
            return False, f"{self.name}: the mask must have the shape of the image"
        return True, ""

    def output_spec(self, params: dict | None = None, input_specs=None) -> OutputSpec:
        """``out`` (points); with ``testmode`` also ``analysis`` (an image), so the one declared kind is ``any``."""
        if bool((params or {}).get("testmode", False)):
            return OutputSpec(ports=(POINTS_PORT, ANALYSIS_PORT), kind="any")
        return OutputSpec(ports=(POINTS_PORT,), kind=self.output_kind)

    # ------------------------------------------------------------------ widget

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        from imswitch.improcess.processors.psf_resolution._params import build_form

        spec = self.param_spec()
        widget = build_form(parent, [f for f in spec if f.type not in ("text", "json")], collapsed=())
        text_fields = [f for f in spec if f.type == "text"]
        edits = {}
        form = QtWidgets.QFormLayout()
        for f in text_fields:
            edit = QtWidgets.QLineEdit(str(f.default))
            edit.setToolTip(f.help)
            form.addRow(f.label + ":", edit)
            edits[f.key] = edit
        chooser = QtWidgets.QComboBox()
        chooser.setToolTip("The pipelines found in that folder: choosing one fills the name and its parameters")
        chooser.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        chooser.setMinimumContentsLength(12)
        form.insertRow(1, "Found:", chooser)
        widget.layout().insertLayout(0, form)

        group = QtWidgets.QGroupBox("Pipeline parameters")
        group_form = QtWidgets.QFormLayout(group)
        group.setVisible(False)
        outer = widget.layout()
        outer.insertWidget(outer.count() - 1, group)
        note = QtWidgets.QLabel("")
        note.setWordWrap(True)
        outer.insertWidget(outer.count() - 1, note)

        base_get, base_set = widget.get_values, widget.set_values
        pipeline_controls: dict[str, tuple[PipelineParam, QtWidgets.QWidget]] = {}
        held = {"params": {}}

        def folder_value():
            return widget.controls["folder"].currentData() or "etsted"

        def refresh_found():
            chooser.blockSignals(True)
            chooser.clear()
            chooser.addItem("")
            for name in list_pipelines(folder_value()):
                chooser.addItem(name)
            chooser.setCurrentText(edits["pipeline"].text())
            chooser.blockSignals(False)

        def rebuild(values=None):
            """One field per parameter of the chosen pipeline (as etSTED's panel makes), then ``values`` in them."""
            for control in list(pipeline_controls.values()):
                group_form.removeRow(control[1])
            pipeline_controls.clear()
            note.setText("")
            fields = ()
            name = edits["pipeline"].text().strip()
            if name:
                try:
                    info = inspect_pipeline_file(resolve_pipeline(name, folder_value()))
                except PipelineFileError as exc:
                    note.setText(str(exc))
                else:
                    reserved = set(self.default_params())
                    docs = parameter_docs(info.doc)
                    for param in info.exposed:
                        if param.name in reserved:
                            continue
                        if param.is_bool:
                            control = QtWidgets.QCheckBox()
                            control.setChecked(bool(param.start_value))
                        else:
                            control = QtWidgets.QLineEdit(repr(param.start_value))
                        control.setToolTip(docs.get(param.name) or param.note)
                        group_form.addRow(param.name + ":", control)
                        pipeline_controls[param.name] = (param, control)
                    if info.problems:
                        note.setText("; ".join(info.problems))
                    fields = info.exposed
            group.setVisible(bool(pipeline_controls))
            for key, value in (values or {}).items():
                if key in pipeline_controls:
                    _set_pipeline_control(pipeline_controls[key], value)
            return fields

        def get_values():
            values = {**base_get(), **{k: e.text().strip() for k, e in edits.items()}}
            values["params"] = dict(held["params"])
            for name, (param, control) in pipeline_controls.items():
                values[name] = _read_pipeline_control(param, control)
            return values

        def set_values(values):
            values = values or {}
            base_set(values)
            for key, edit in edits.items():
                if key in values:
                    edit.setText(str(values[key]))
            if "params" in values and isinstance(values["params"], dict):
                held["params"] = dict(values["params"])
            refresh_found()
            rebuild(values)

        def chosen(text):
            if text:
                edits["pipeline"].setText(text)
                rebuild()

        chooser.textActivated.connect(chosen)
        edits["pipeline"].editingFinished.connect(rebuild)
        widget.controls["folder"].currentIndexChanged.connect(lambda _i: (refresh_found(), rebuild()))
        refresh_found()
        widget.get_values = get_values
        widget.set_values = set_values
        widget.pipelineControls = pipeline_controls
        widget.textEdits = edits
        widget.rebuild = rebuild
        widget.chooser = chooser
        return widget

    # ------------------------------------------------------------------- run

    def apply(self, result: ProcessingResult, params: dict) -> ProcessorOutput:
        results = list(params.get("results", []) or []) or [result]
        image_result = results[0]
        mask_result = results[1] if len(results) > 1 else None
        defaults = self.default_params()
        p = {**defaults, **{k: v for k, v in params.items() if k in defaults}}
        state = params.get(STATE_PARAM)
        if not isinstance(state, dict):
            state = {}
        image = np.asarray(image_result.data)
        if image.ndim != 2:
            raise ProcessorInputError(f"{self.name} needs a (Y, X) camera frame, got shape {image.shape}")

        try:
            path = resolve_pipeline(p["pipeline"], p["folder"])
        except PipelineFileError as exc:
            raise ProcessorInputError(str(exc)) from exc
        runner = self._runner(state, path)

        mine = state.setdefault(ACTION_STATE_KEY, {"busy": False, "epoch": 0})
        if state.get("epoch_seen") is None:
            state["epoch_seen"] = mine["epoch"]
        testmode = bool(p["testmode"])
        if p["after_scan"] != "nothing":
            if mine["busy"]:
                return self._output(image_result, p, np.empty((0, 2)), None, "running", path, 0,
                                    message="paused: a triggered scan is running")
            if mine["epoch"] != state["epoch_seen"]:
                state["epoch_seen"] = mine["epoch"]
                if p["after_scan"] == "reset":
                    self._reset(state)
                else:
                    state["calls"] = 0
        wanted = max(1, int(p["prev_frames"]))
        previous = state.get("prev")
        if previous is None or previous.maxlen != wanted:
            previous = deque(previous or (), maxlen=wanted)
            state["prev"] = previous

        mask = self._mask(state, p, image, mask_result)
        values = _values(runner, p, params)
        try:
            outcome = runner.execute(image, previous, mask, testmode, state.get("exinfo"), values)
        except Exception as exc:  # noqa: BLE001 - the user's file; name it, never kill the run loop
            raise EtstedPipelineError(error_text(path, exc)) from exc
        state["exinfo"] = outcome.exinfo
        previous.append(image)
        seen = state.get("calls", 0)
        state["calls"] = seen + 1
        detections = outcome.coords_detected
        if seen < int(p["ignore_frames"]):
            return self._output(image_result, p, np.empty((0, 2)), outcome.analysis_image,
                                f"ignoring the first frames, {seen + 1} of {int(p['ignore_frames'])}", path,
                                int(detections.shape[0]))
        return self._output(image_result, p, detections, outcome.analysis_image, "running", path,
                            int(detections.shape[0]))

    # ------------------------------------------------------------- internals

    @staticmethod
    def _reset(state: dict) -> None:
        """As etSTED after a triggered scan: no exinfo, no previous frames, the frame count from 0."""
        state["exinfo"] = None
        state["calls"] = 0
        if state.get("prev") is not None:
            state["prev"].clear()

    def _runner(self, state: dict, path):
        """The loaded pipeline of this run (loaded again when the file changes; a failure is remembered)."""
        from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner

        try:
            key = (str(path.resolve()), path.stat().st_mtime_ns)
        except OSError as exc:
            raise ProcessorInputError(f"cannot read {path}: {exc}") from exc
        if state.get("key") != key:
            state["key"] = key
            state["runner"] = None
            state["load_error"] = None
            self._reset(state)
            runner = EtSTEDPipelineRunner()
            try:
                runner.load_file(path)
            except Exception as exc:  # noqa: BLE001 - the user's file
                state["load_error"] = error_text(path, exc)
            else:
                state["runner"] = runner
        if state.get("load_error"):
            raise EtstedPipelineError(state["load_error"])
        return state["runner"]

    @staticmethod
    def _mask(state: dict, p: dict, image: np.ndarray, mask_result):
        source = p["mask"]
        if source == "none":
            return None
        if source == "input":
            if mask_result is None:
                raise ProcessorInputError("mask from input needs a labels or mask image wired in as the second input")
            mask = np.asarray(mask_result.data) > 0
        else:
            path = str(p["mask_path"] or "")
            if not path:
                raise ProcessorInputError("mask from file needs a mask file")
            if state.get("mask_file") != path:
                state["mask_file"], state["mask_array"] = path, _load_mask(path)
            mask = state["mask_array"]
        if mask.shape != image.shape:
            raise ProcessorInputError(f"the mask has shape {mask.shape}, the frame {image.shape}")
        return mask

    def _output(self, image_result, p: dict, detections, analysis, status: str, path, n_detected: int,
                message: str = ""):
        coords = np.asarray(detections, dtype=np.float64).reshape(-1, 2)
        limit = int(p["max_points"] or 0)
        if limit > 0:
            coords = coords[:limit]
        offset = float(p["coord_offset_px"])
        # etSTED's (column, row) -> the smart pipeline's (row, column), pixel centres at integers
        points = np.column_stack([coords[:, 1] - offset, coords[:, 0] - offset]) if coords.size else np.zeros((0, 2))
        meta = getattr(image_result, "metadata", None) or {}
        envelope = meta.get("frame") if isinstance(meta, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        frame = str(p["frame"] or UNKNOWN_FRAME)
        if frame == UNKNOWN_FRAME and envelope.get("detector"):
            frame = f"detector:{envelope['detector']}"
        frame_index = meta.get("frame_index") if isinstance(meta, dict) else None
        count = points.shape[0]
        points_result = PointsResult(
            f"{image_result.name} (etSTED {path.stem})", points, frame=frame,
            frame_index=None if frame_index is None else int(frame_index), axis_names=["y", "x"],
            properties={"rank": np.arange(count), "etsted_c1": coords[:, 0].copy() if count else np.zeros(0),
                        "etsted_c2": coords[:, 1].copy() if count else np.zeros(0)},
            metadata={"operation": self.id, "state": status, "pipeline": path.stem, "pipeline_file": str(path),
                      "detected": int(n_detected), "message": message},
        )
        results, keys = [points_result], [POINTS_PORT]
        if bool(p["testmode"]) and analysis is not None:
            image = np.asarray(analysis)
            if image.ndim == 2:
                from imswitch.improcess.model.array_result import ArrayProcessingResult

                results.append(ArrayProcessingResult(f"{image_result.name} (etSTED {path.stem} analysis)", image,
                                                     ["Y", "X"], metadata={"operation": self.id}))
                keys.append(ANALYSIS_PORT)
            else:
                points_result.metadata["message"] = f"the analysis image is {image.ndim}-D, not shown"
        return ProcessorOutput(results, keys=keys)


# ---------------------------------------------------------------------- helpers

def pipeline_fields(info: PipelineInfo) -> tuple:
    """The :class:`ParamField` of each parameter of ``info`` a form can show (not one named like a setting of the step)."""
    reserved = set(EtstedPipelineProcessor.default_params())
    docs = parameter_docs(info.doc)
    fields = []
    for param in info.exposed:
        if param.name in reserved:
            continue
        help_text = docs.get(param.name) or param.note
        if param.is_bool:
            fields.append(ParamField(param.name, "bool", bool(param.default), label=param.name, help=help_text,
                                     group="Pipeline parameters"))
        else:
            fields.append(ParamField(param.name, "float", float(param.start_value), label=param.name,
                                     help=help_text, decimals=6, group="Pipeline parameters"))
    return tuple(fields)


def parameter_docs(doc: str) -> dict[str, str]:
    """``{name: text}`` from a docstring that lists its parameters as ``name   what it does``
    (continuation lines indented further), as ``dff_event``'s does."""
    docs: dict[str, str] = {}
    current = None
    for line in (doc or "").splitlines():
        match = re.match(r"^(\w+)\s{2,}(\S.*)$", line)
        if match and match.group(1).isidentifier():
            current = match.group(1)
            docs[current] = match.group(2).strip()
        elif current and line.startswith("  ") and line.strip():
            docs[current] += " " + line.strip()
        else:
            current = None
    return docs


def _values(runner, p: dict, params: dict) -> list:
    """The positional parameter values, as etSTED's panel gives them: floats, in signature order."""
    named = p.get("params") if isinstance(p.get("params"), dict) else {}
    defaults = set(EtstedPipelineProcessor.default_params())
    values = []
    for name in runner.get_user_parameter_names():
        if name in named:
            value = named[name]
        elif name in params and name not in defaults:
            value = params[name]
        else:
            default = runner.parameters[name].default
            value = 0 if default is runner.parameters[name].empty else default
        if isinstance(value, bool):
            values.append(value)
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError) as exc:
            raise ProcessorInputError(f"pipeline parameter {name}: {value!r} is not a number") from exc
    return values


def _load_mask(path: str) -> np.ndarray:
    try:
        if path.lower().endswith(".npy"):
            return np.load(path) > 0
        import tifffile

        return tifffile.imread(path) > 0
    except Exception as exc:  # noqa: BLE001 - say which file
        raise ProcessorInputError(f"cannot read the mask file {path!r}: {exc}") from exc


def _read_pipeline_control(param: PipelineParam, control):
    if isinstance(control, QtWidgets.QCheckBox):
        return bool(control.isChecked())
    try:
        return float(control.text())
    except ValueError:
        return param.start_value


def _set_pipeline_control(entry, value) -> None:
    param, control = entry
    if isinstance(control, QtWidgets.QCheckBox):
        control.setChecked(bool(value))
    else:
        control.setText(repr(float(value)))


__all__ = ["ACTION_STATE_KEY", "EtstedPipelineError", "EtstedPipelineProcessor", "parameter_docs", "pipeline_fields"]
