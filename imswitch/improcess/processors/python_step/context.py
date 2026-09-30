"""What a Python step's code sees, and how its outputs are collected.

Qt-free on purpose: the namespace, the output rules and the error reporting
are the step's contract, and the workflow runner, the tests and a headless
``python -m imswitch.improcess.workflows run`` all use them without a GUI.
The panel lives in :mod:`.processor`.

The code runs in-process with full Python, like a drop-in plugin or an
ImScripting script; nothing here restricts it.
"""

from __future__ import annotations

import io
import re
import traceback
from dataclasses import dataclass
from typing import Any

import numpy as np

from imswitch.imcommon.model import currentRoute, routeThisThreadsOutputTo
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
)

DEFAULT_PORTS = "out"

DEFAULT_CODE = '''\
# Names you can use: np, data (the first input as an array), inputs, axes,
# scales, unit, axis("Z"), results, make_result(...), make_labels(...).
# print() output is shown below the editor.
# Set `outputs` to {port: array} with one entry for each name in "Output ports".
outputs = {"out": data}
'''

#: The compile filename: the one frame name kept when a traceback is trimmed
#: to the user's own code.
SCRIPT_FILENAME = "<python step>"

_PORT_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")
#: dtype kinds an output array may have: bool, signed and unsigned integer, float.
_NUMERIC_KINDS = "biuf"


def parse_ports(text) -> tuple[str, ...]:
    """The output port names in ``text``: comma-separated, blanks dropped.

    Empty text is the single port ``out``. A name must be one a step
    reference can carry (letters, digits, ``_`` and ``-``), and may not repeat.
    """
    names = [part.strip() for part in str(text or "").split(",")]
    names = [name for name in names if name]
    if not names:
        return (DEFAULT_PORTS,)
    for name in names:
        if not _PORT_NAME.match(name):
            raise ValueError(
                f"output port {name!r} is not a valid name: use letters, digits, '_' or '-'"
            )
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ValueError(f"output port {duplicated[0]!r} is listed more than once")
    return tuple(names)


class ScriptError(RuntimeError):
    """The code failed, or left outputs the step cannot use.

    ``str()`` is one line -- ``line 3: NameError: name 'x' is not defined`` --
    because that is what a status label and a workflow error can carry.
    ``line`` is the innermost line of the user's own code the error passed
    through (``None`` for a problem with the outputs as a whole), and
    ``traceback_text`` is the traceback trimmed to those frames.
    """

    def __init__(self, message: str, *, line: int | None = None, traceback_text: str = ""):
        self.message = str(message)
        self.line = line
        self.traceback_text = traceback_text or self.message
        super().__init__(f"line {line}: {self.message}" if line else self.message)

    @classmethod
    def from_exception(cls, exc: BaseException, code: str, filename: str = SCRIPT_FILENAME) -> "ScriptError":
        """Describe ``exc``, raised while running ``code`` compiled as ``filename``."""
        source = str(code).splitlines()
        if isinstance(exc, SyntaxError):
            line = exc.lineno if isinstance(exc.lineno, int) else None
            message = f"SyntaxError: {exc.msg}"
            text = "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
            return cls(message, line=line, traceback_text=text)

        name = "SystemExit" if isinstance(exc, SystemExit) else type(exc).__name__
        detail = "the code called exit()" if isinstance(exc, SystemExit) else str(exc)
        message = f"{name}: {detail}" if detail else name
        frames = [
            frame for frame in traceback.extract_tb(exc.__traceback__)
            if frame.filename == filename
        ]
        lines = ["Traceback (most recent call last):"]
        for frame in frames:
            lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
            if frame.lineno and 0 < frame.lineno <= len(source) and source[frame.lineno - 1].strip():
                lines.append("    " + source[frame.lineno - 1].strip())
        lines.append(message)
        return cls(
            message,
            line=frames[-1].lineno if frames else None,
            traceback_text="\n".join(lines),
        )


@dataclass(frozen=True)
class ScriptOutput:
    """An output the code described itself, from :func:`make_result` or :func:`make_labels`.

    ``axes`` / ``scales`` / ``name`` left ``None`` are filled in from the
    input; ``kind`` is ``"image"`` or ``"labels"``.
    """

    array: Any
    axes: tuple[str, ...] | None = None
    scales: tuple[float, ...] | None = None
    name: str | None = None
    kind: str = "image"


def _described(array, axes, scales, name, kind) -> ScriptOutput:
    return ScriptOutput(
        array=array,
        axes=tuple(str(label) for label in axes) if axes is not None else None,
        scales=tuple(float(scale) for scale in scales) if scales is not None else None,
        name=None if name is None else str(name),
        kind=kind,
    )


def make_result(array, *, axes=None, scales=None, name=None) -> ScriptOutput:
    """An image output with the axes (and optionally scales and name) you give.

    Needed when the array's dimensionality differs from the input's; one of
    the same dimensionality inherits the input's axes without it.
    """
    return _described(array, axes, scales, name, "image")


def make_labels(array, *, axes=None, scales=None, name=None) -> ScriptOutput:
    """Like :func:`make_result`, for an integer label image (segmentation)."""
    return _described(array, axes, scales, name, "labels")


def _in_memory_or_lazy(data):
    """``data`` as it is when it is an array already or a lazy view, else an array."""
    if isinstance(data, np.ndarray) or (hasattr(data, "shape") and hasattr(data, "__getitem__")):
        return data
    return np.asarray(data)


def _make_axis_lookup(labels: list[str]):
    def axis(label_or_index) -> int:
        """The index of the axis called ``label_or_index`` ("Z"), or that index itself."""
        if isinstance(label_or_index, (int, np.integer)) and not isinstance(label_or_index, bool):
            index = int(label_or_index)
            if -len(labels) <= index < len(labels):
                return index % len(labels)
        elif isinstance(label_or_index, str):
            if label_or_index in labels:
                return labels.index(label_or_index)
            lowered = [label.lower() for label in labels]
            if label_or_index.lower() in lowered:
                return lowered.index(label_or_index.lower())
        raise ValueError(
            f"no axis {label_or_index!r}: the axes are {', '.join(labels)} "
            f"(indices 0 to {len(labels) - 1})"
        )

    return axis


def build_namespace(inputs, *, materialise: bool = True) -> dict:
    """The names the code starts with, and no others.

    ``inputs`` are the step's input results, in the order the step lists
    them. ``data`` is the first one's own array (a lazy source is read into
    memory): copy it before changing it in place.

    ``materialise=False`` leaves a result that is not in memory as the lazy
    array it is, for the console, which rebinds these names on every change
    of selection and must not read a whole recording each time.
    """
    results = list(inputs)
    first = results[0]
    labels = axis_labels_for_result(first)
    as_array = np.asarray if materialise else _in_memory_or_lazy
    # Each input is read once: ``data`` is ``inputs[0]``, not a second read of it.
    arrays = [as_array(result.data) for result in results]
    return {
        "np": np,
        "data": arrays[0],
        "inputs": arrays,
        "axes": labels,
        "scales": axis_scales_for_result(first),
        "unit": getattr(first, "scale_unit", "px"),
        "axis": _make_axis_lookup(labels),
        "results": results,
        "make_result": make_result,
        "make_labels": make_labels,
        "outputs": None,
        "out": None,
    }


def run_script(
    code: str, inputs, ports, *, step_name: str = "python"
) -> tuple[list[ProcessingResult], str]:
    """Run ``code`` over ``inputs`` and collect one result per declared port.

    Returns ``(results, stdout)``: the results in the order of ``ports``, and
    what the code printed (stdout and stderr together). Raises
    :class:`ScriptError` for a failure in the code and for outputs that do not
    follow the rules.
    """
    inputs = list(inputs)
    ports = tuple(ports)
    if not inputs:
        raise ScriptError("the Python step needs at least one input")
    # A named step shows up by name in a traceback; the processor, which is
    # not told its step's id, uses the plain name.
    filename = SCRIPT_FILENAME if step_name == "python" else f"<python step '{step_name}'>"
    namespace = build_namespace(inputs)
    capture = _Capture(currentRoute())
    try:
        compiled = compile(code, filename, "exec")
        # This thread's output only: a run on a worker thread must not swallow
        # what the GUI thread prints meanwhile. What the code prints is also
        # passed on to an outer route, which is how a panel streams it live.
        with routeThisThreadsOutputTo(capture):
            exec(compiled, namespace)  # noqa: S102 - the feature: code runs as typed, like a plugin
    except (Exception, SystemExit) as exc:
        raise ScriptError.from_exception(exc, code, filename) from exc
    produced = _collect(namespace, ports)
    return _build_results(produced, ports, inputs), capture.buffer.getvalue()


class _Capture:
    """What a run's code printed (stdout and stderr together), kept and passed on."""

    def __init__(self, outer=None):
        self.buffer = io.StringIO()
        self._outer = outer

    def write(self, text):
        self.buffer.write(text)
        if self._outer is not None:
            self._outer.write(text)
        return len(text)

    def flush(self):
        flush = getattr(self._outer, "flush", None)
        if callable(flush):
            flush()


def _collect(namespace: dict, ports: tuple[str, ...]) -> dict:
    """The ``{port: value}`` the code produced, checked against ``ports``."""
    outputs = namespace.get("outputs")
    out = namespace.get("out")
    if outputs is not None:
        if isinstance(outputs, dict):
            produced = dict(outputs)
        elif ports == ("out",):
            produced = {"out": outputs}
        else:
            raise ScriptError(
                f"'outputs' must be a dict of port -> array (ports: {', '.join(ports)}), "
                f"got {type(outputs).__name__}"
            )
    elif out is not None:
        if len(ports) != 1:
            raise ScriptError(
                f"'out' is for a single output port, but the ports are {', '.join(ports)}: "
                f"set 'outputs' to a dict with one entry for each"
            )
        produced = {ports[0]: out}
    else:
        raise ScriptError("the code set neither 'outputs' nor 'out'")

    declared = ", ".join(ports)
    missing = [port for port in ports if port not in produced]
    extra = [str(key) for key in produced if key not in ports]
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"not declared: {', '.join(extra)}")
        raise ScriptError(
            f"the outputs do not match the declared ports ({declared}): {'; '.join(parts)}"
        )
    return produced


def _build_results(produced: dict, ports: tuple[str, ...], inputs) -> list[ProcessingResult]:
    first = inputs[0]
    return [
        result_from_value(
            produced[port], first,
            what=f"output {port!r}",
            default_name=f"{first.name} ({port})",
            metadata={"operation": "python", "port": port},
        )
        for port in ports
    ]


def result_from_value(
    value,
    reference,
    *,
    what: str = "output",
    default_name: str | None = None,
    name=None,
    axes=None,
    scales=None,
    metadata=None,
) -> ProcessingResult:
    """One result from an array or a :func:`make_result` / :func:`make_labels` output.

    ``reference`` is the result the array is taken to be derived from (the
    step's first input): an array of its dimensionality inherits its axes,
    scales and unit, any other says its axes itself, and with no reference
    (``None``) every array must. ``name`` / ``axes`` / ``scales`` given here
    take the place of those a :class:`ScriptOutput` left unset. ``what`` names
    the value in error messages.
    """
    described = value if isinstance(value, ScriptOutput) else ScriptOutput(array=value)
    array = _as_array(what, described)
    axes = described.axes if described.axes is not None else (
        tuple(str(label) for label in axes) if axes is not None else None
    )
    scales = described.scales if described.scales is not None else (
        tuple(float(scale) for scale in scales) if scales is not None else None
    )
    if reference is not None:
        reference_labels = axis_labels_for_result(reference)
        reference_scales = axis_scales_for_result(reference)
        unit = getattr(reference, "scale_unit", "px")
    else:
        reference_labels, reference_scales, unit = [], [], "px"
    if axes is not None:
        if len(axes) != array.ndim:
            raise ScriptError(
                f"{what}: axes {list(axes)} name {len(axes)} "
                f"dimensions but the array has {array.ndim}"
            )
        labels = list(axes)
        if scales is not None:
            scale_values = list(scales)
        else:
            # Keep the calibration of the axes the output shares with the input.
            by_label = dict(zip(reference_labels, reference_scales))
            scale_values = [by_label.get(label, 1.0) for label in labels]
    elif reference is not None and array.ndim == len(reference_labels):
        labels = list(reference_labels)
        scale_values = list(scales) if scales is not None else list(reference_scales)
    elif reference is None:
        raise ScriptError(
            f"{what} has {array.ndim} dimensions and there is no input to take its axes from: "
            f"say what they are with make_result(array, axes=[...])"
        )
    else:
        raise ScriptError(
            f"{what} has {array.ndim} dimensions but the input has "
            f"{len(reference_labels)} ({', '.join(reference_labels)}): say what its axes are "
            f"with make_result(array, axes=[...])"
        )
    if len(scale_values) != array.ndim:
        raise ScriptError(f"{what}: {len(scale_values)} scales for {array.ndim} dimensions")
    result_name = described.name or (None if name is None else str(name)) or default_name or "python result"
    metadata = dict(metadata or {})
    if described.kind == "labels":
        return LabelsResult(
            result_name, _as_labels(what, array), labels,
            axis_scales=scale_values, scale_unit=unit, metadata=metadata,
        )
    return ArrayProcessingResult(
        result_name, array, labels,
        display_levels=finite_range(array),
        axis_scales=scale_values, scale_unit=unit, metadata=metadata,
    )


def _as_array(what: str, described: ScriptOutput) -> np.ndarray:
    try:
        array = np.asarray(described.array)
    except Exception as exc:  # noqa: BLE001 - reported with the value's name
        raise ScriptError(f"{what} is not an array: {exc}") from exc
    if array.dtype.kind not in _NUMERIC_KINDS:
        raise ScriptError(
            f"{what} must be a numeric array, got "
            f"{type(described.array).__name__} of dtype {array.dtype}"
        )
    return array


def _as_labels(what: str, array: np.ndarray) -> np.ndarray:
    """``array`` as label values: integers, or floats that are whole numbers."""
    if array.dtype.kind in "biu":
        return array
    if array.size and not np.all(np.isfinite(array) & (array == np.round(array))):
        raise ScriptError(f"labels {what} must hold whole numbers, got dtype {array.dtype}")
    return array.astype(np.int32)


__all__ = [
    "DEFAULT_CODE",
    "DEFAULT_PORTS",
    "SCRIPT_FILENAME",
    "ScriptError",
    "ScriptOutput",
    "build_namespace",
    "make_labels",
    "make_result",
    "parse_ports",
    "result_from_value",
    "run_script",
]
