"""Stack or concatenate compatible image results into one output.

Rank-general: N same-shaped inputs of any dimensionality gain one new leading
axis (two 2D images -> a 3D stack, two 3D stacks -> a 4D stack, ...), or are
appended along an existing shared axis (grow a T-series, add Z planes).
Unlike the one-click C-only `channel-merge`, the axis is user-chosen and
incompatibilities are reported as reasons instead of a silently dead button.

Strictness is deliberate. Axis labels, pixel scales and the scale unit must
match, so physically different arrays never silently become one calibrated
stack, and the compatibility check is metadata-only: it never reads lazy
pixel data. Two things are offered on top of that, both opt-in, so that a
refusal can come with a way forward instead of a dead end:

* **Padding** (``pad="pad"``). Inputs that agree in axes, labels, scales and
  unit but differ in size along the last two axes (the Y/X plane) are padded
  to the largest size per axis. Padding is not resampling: inputs whose
  scales differ are still refused, and the message points to Scale/Resize.
  Only the plane is padded; a different number of Z, T or C planes is never
  filled up with blank ones (concatenate those instead). A *sanity limit*
  keeps "slightly different" from becoming a 64 px image inside a 4096 px
  one: no input may grow by more than ``pad_max_ratio`` (default 2) along any
  axis, ``0`` lifting the limit. What was padded, per input, goes into the
  result's ``metadata["padding"]``.
* **Appending** (``mode="auto"``). A stack and lower-rank data that matches
  its other axes (``Z,Y,X`` + ``Y,X``; ``T,Z,Y,X`` + ``Z,Y,X``) are joined
  along the one axis the lower-rank input lacks, planes in selection order,
  labels and scales from the higher-rank input. Equal-rank inputs stack on
  a new axis exactly as ``mode="stack"`` does. When the labels do not say
  which axis is missing the combine asks (:class:`CombineMismatch` of kind
  ``"ambiguous"``, with the candidates) rather than guessing.

``mode="stack"`` and ``mode="concatenate"`` without padding behave exactly as
they always did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.processors.base import Processor

#: Common labels offered for the new stack axis; any custom string is valid.
STACK_AXIS_LABELS = ("Z", "T", "C")

COMBINE_MODES = ("stack", "concatenate", "auto")
PAD_MODES = ("none", "pad")
PAD_ANCHORS = ("centre", "origin")
PAD_VALUES = ("0", "nan", "edge")

#: No input may grow by more than this factor along a padded axis. 2x covers
#: "a few pixels off", a tile or a crop that lost an edge, and a half-size
#: preview; it stops a 64 px image being buried in a 4096 px one. ``0`` lifts it.
DEFAULT_PAD_MAX_RATIO = 2.0

_SCALE_HINT = "use Scale/Resize to bring them to the same pixel size first (padding is not resampling)"
_LABEL_HINT = "fix the axis labels or order with Swap / Relabel Axes (axis-swap) first"


class CombineMismatch(ValueError):
    """The inputs cannot be combined with the options given; says why.

    ``kind`` names the difference so a dialog can offer the right way out:
    ``count``, ``unreadable``, ``axis`` (join axis out of range), ``size``,
    ``rank``, ``labels``, ``unit``, ``scale``, ``label_collision``,
    ``ambiguous`` (``candidates`` holds ``(axis index, label)`` choices),
    ``pad_limit``, ``pad_value`` or ``param``.
    """

    def __init__(self, message: str, kind: str = "other", candidates=()):
        super().__init__(message)
        self.kind = kind
        self.candidates = tuple(candidates)


class StackCombineProcessor(Processor):
    """Stack or concatenate the selected reconstruction-list results."""

    name = "Stack/Combine"
    id = "stack-combine"
    output_kind = "image"
    category = "Dimensions and channels"
    min_inputs = 2
    max_inputs = None

    @classmethod
    def default_params(cls) -> dict:
        # Set by the Stack/Combine dialog in the GUI, by the step's params in
        # a workflow. ``name`` empty means "derive one from the mode". The
        # defaults are today's: stack, no padding.
        return {
            "mode": "stack", "join_axis": 0, "name": "", "axis_label": "Z",
            "append_axis": "", "pad": "none", "pad_anchor": "centre",
            "pad_value": "0", "pad_max_ratio": DEFAULT_PAD_MAX_RATIO,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                'mode', 'select', 'stack', label='Mode',
                help='stack: a new axis holds the inputs; concatenate: the inputs are joined along an '
                     'existing axis; auto: equal-rank inputs are stacked, a lower-rank input is appended '
                     'to the axis it lacks',
                options=('stack', 'concatenate', 'auto'),
            ),
            ParamField(
                'join_axis', 'int', 0, label='Join axis',
                help='Index of the existing axis to concatenate along (concatenate mode)', min=0,
            ),
            ParamField(
                'name', 'text', '', label='Result name',
                help='Empty derives a name from the mode',
            ),
            ParamField(
                'axis_label', 'text', 'Z', label='New axis label',
                help='Label of the new axis (stack mode)',
            ),
            ParamField(
                'append_axis', 'text', '', label='Append axis',
                help='auto mode: the label (or index) of the axis the lower-rank inputs are appended '
                     'along; empty picks it from the labels and asks when they do not decide',
            ),
            ParamField(
                'pad', 'select', 'none', label='Size mismatch',
                help='none: inputs must have the same size; pad: pad the Y/X plane of smaller inputs '
                     'to the largest size',
                options=('none', 'pad'),
            ),
            ParamField(
                'pad_anchor', 'select', 'centre', label='Pad anchor',
                help='centre: the image sits in the middle of the padding; origin: it keeps its '
                     'top-left corner and is padded at the far edges',
                options=('centre', 'origin'),
            ),
            ParamField(
                'pad_value', 'select', '0', label='Pad value',
                help='0: zeros; nan: not-a-number (float data only); edge: repeat the edge pixels',
                options=('0', 'nan', 'edge'),
            ),
            ParamField(
                'pad_max_ratio', 'float', DEFAULT_PAD_MAX_RATIO, label='Largest padding ratio',
                help='Refuse to pad an input to more than this many times its size along any axis; '
                     '0 lifts the limit',
                min=0.0, step=0.5, decimals=1,
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) >= 2

    def check_inputs(self, results) -> tuple[bool, str]:
        # Mode, axis and padding are parameters, decided at apply time. The
        # panel-level check therefore asks whether *some* setting could
        # combine the inputs (a size difference could be padded, a missing
        # axis appended) and refuses only what no setting fixes: different
        # labels, scales or units. An ambiguous append axis is a choice the
        # parameters make, not a refusal.
        try:
            align_results(results, mode="auto", pad="pad", pad_max_ratio=0)
        except CombineMismatch as exc:
            if exc.kind == "ambiguous":
                return True, ""
            return False, str(exc)
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        defaults = self.default_params()
        widget = QtWidgets.QWidget(parent)
        form = QtWidgets.QFormLayout(widget)

        def combo(key, options, tip):
            box = QtWidgets.QComboBox()
            for option in options:
                box.addItem(option, option)
            box.setCurrentIndex(box.findData(defaults[key]))
            box.setToolTip(tip)
            return box

        specs = {field.key: field for field in self.param_spec()}
        mode = combo('mode', COMBINE_MODES, specs['mode'].help)
        axis_label = QtWidgets.QLineEdit(defaults['axis_label'])
        axis_label.setToolTip(specs['axis_label'].help)
        join_axis = QtWidgets.QSpinBox()
        join_axis.setRange(0, 10 ** 9)
        join_axis.setValue(defaults['join_axis'])
        join_axis.setToolTip(specs['join_axis'].help)
        append_axis = QtWidgets.QLineEdit(defaults['append_axis'])
        append_axis.setPlaceholderText("auto-detect")
        append_axis.setToolTip(specs['append_axis'].help)
        pad = combo('pad', PAD_MODES, specs['pad'].help)
        anchor = combo('pad_anchor', PAD_ANCHORS, specs['pad_anchor'].help)
        value = combo('pad_value', PAD_VALUES, specs['pad_value'].help)
        ratio = QtWidgets.QDoubleSpinBox()
        ratio.setDecimals(1)
        ratio.setSingleStep(0.5)
        ratio.setRange(0.0, 10 ** 9)
        ratio.setValue(defaults['pad_max_ratio'])
        ratio.setToolTip(specs['pad_max_ratio'].help)
        name = QtWidgets.QLineEdit(defaults['name'])
        name.setToolTip(specs['name'].help)

        form.addRow("Mode:", mode)
        form.addRow("New axis label:", axis_label)
        form.addRow("Join axis:", join_axis)
        form.addRow("Append axis:", append_axis)
        form.addRow("Size mismatch:", pad)
        form.addRow("Pad anchor:", anchor)
        form.addRow("Pad value:", value)
        form.addRow("Largest padding ratio:", ratio)
        form.addRow("Result name:", name)

        def get_values():
            return {
                "mode": mode.currentData(),
                "join_axis": int(join_axis.value()),
                "name": name.text().strip(),
                "axis_label": axis_label.text().strip() or defaults['axis_label'],
                "append_axis": append_axis.text().strip(),
                "pad": pad.currentData(),
                "pad_anchor": anchor.currentData(),
                "pad_value": value.currentData(),
                "pad_max_ratio": float(ratio.value()),
            }

        def set_values(values):
            merged = {**defaults, **(values or {})}
            for box, key in ((mode, 'mode'), (pad, 'pad'), (anchor, 'pad_anchor'), (value, 'pad_value')):
                index = box.findData(str(merged[key]))
                if index >= 0:
                    box.setCurrentIndex(index)
            axis_label.setText(str(merged['axis_label'] or ''))
            join_axis.setValue(int(merged['join_axis'] or 0))
            append_axis.setText(str(merged['append_axis'] or ''))
            ratio.setValue(float(merged['pad_max_ratio'] if merged['pad_max_ratio'] is not None
                                 else DEFAULT_PAD_MAX_RATIO))
            name.setText(str(merged['name'] or ''))

        widget.get_values = get_values
        widget.set_values = set_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or [])
        if not results:
            results = [result, *list(params.get("additional_results", []) or [])]
        mode = str(params.get("mode") or "stack")
        if mode not in COMBINE_MODES:
            raise ValueError(
                f"mode must be 'stack', 'concatenate' or 'auto', got {mode!r}"
            )
        return combine_results(
            results,
            mode=mode,
            join_axis=int(params.get("join_axis") or 0),
            axis_label=str(params.get("axis_label") or "Z"),
            name=params.get("name") or None,
            **combine_options(params),
        )


def combine_options(params: dict) -> dict:
    """The padding and append keywords of ``params``, normalised.

    Accepts what a workflow file may hold: ``pad`` as ``true``/``false`` or a
    mode name, ``pad_value`` as the number ``0`` or ``NaN``.
    """
    pad = params.get("pad")
    if pad is True:
        pad = "pad"
    elif pad in (False, None, ""):
        pad = "none"
    value = params.get("pad_value")
    if value is None or value == "":
        value = "0"
    elif isinstance(value, float) and value != value:
        value = "nan"
    elif not isinstance(value, str):
        value = "0" if value == 0 else str(value)
    ratio = params.get("pad_max_ratio")
    return {
        "pad": str(pad),
        "pad_anchor": str(params.get("pad_anchor") or "centre"),
        "pad_value": str(value).lower(),
        "pad_max_ratio": DEFAULT_PAD_MAX_RATIO if ratio is None else float(ratio),
        "append_axis": str(params.get("append_axis") or "").strip() or None,
    }


#: Length units a scale can be in, in nanometres per unit. Scales in two of
#: these are comparable once one is converted to the other: a MoNaLISA
#: result in nm and a lattice result in um are the same picture.
_LENGTH_NM = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "micron": 1000.0, "mm": 1e6}


def _unit_factor(unit: str, other_unit: str) -> float | None:
    """Multiply scales in ``other_unit`` by this to state them in ``unit``.

    ``None`` when the two cannot be reconciled: pixels against a length, or
    a unit this does not know.
    """
    if other_unit == unit:
        return 1.0
    mine = _LENGTH_NM.get(str(unit).lower())
    theirs = _LENGTH_NM.get(str(other_unit).lower())
    if mine is None or theirs is None:
        return None
    return theirs / mine


@dataclass(frozen=True)
class CombineAlignment:
    """How the inputs of a combine line up, decided from metadata alone.

    ``shapes`` is the shape each input is read in, before padding. When the
    inputs agree axis for axis it is their own shape. When they differ only
    by singleton axes — the ``(1, 1, 1, 1, Y, X)`` result of one
    reconstructor and the ``(Y, X)`` result of another — it is the shape
    without those axes, which carry no pixels and nothing to disagree about;
    an input appended to a stack gains a length-1 axis it lacked. ``labels``,
    ``scales`` and ``unit`` are the output's, per plane: the reference
    input's (the first, or for ``auto`` the highest-rank one), in its unit.

    ``mode`` is what the combine does after ``auto`` has decided: ``stack``
    or ``concatenate``, along axis ``join`` (``join_label``). ``pads`` is, per
    input, ``(before, after)`` per axis of ``shapes``; ``output_shape`` is
    the result's shape.
    """

    shapes: tuple[tuple[int, ...], ...]
    labels: tuple[str, ...]
    scales: tuple[float, ...]
    unit: str
    mode: str = "stack"
    requested_mode: str = "stack"
    join: int | None = None
    join_label: str | None = None
    appended: tuple[int, ...] = ()
    pads: tuple[tuple[tuple[int, int], ...], ...] = ()
    pad_anchor: str = "centre"
    pad_value: str = "0"
    output_shape: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    reference: int = 0
    new_axis_label: str | None = None

    @property
    def padded(self) -> bool:
        return any(before or after for pad in self.pads for before, after in pad)

    @property
    def pad_fraction(self) -> float:
        """The share of the output's pixels that is padding."""
        if not self.padded:
            return 0.0
        real = sum(int(np.prod(shape)) for shape in self.shapes)
        total = int(np.prod(self.output_shape)) if self.output_shape else 0
        return max(0.0, 1.0 - real / total) if total else 0.0

    @property
    def output_labels(self) -> tuple[str, ...]:
        if self.mode == "stack":
            return (self.new_axis_label or "?", *self.labels)
        return self.labels

    def padded_shape(self, index: int) -> tuple[int, ...]:
        return tuple(
            size + before + after
            for size, (before, after) in zip(self.shapes[index], self.pads[index])
        ) if self.pads else tuple(self.shapes[index])

    def arrays(self, results: Sequence[ProcessingResult]) -> list[np.ndarray]:
        """The inputs' pixel data, each in the shape it is combined in."""
        out = []
        for index, (result, shape) in enumerate(zip(results, self.shapes)):
            array = np.asarray(result.data).reshape(shape)
            pad = self.pads[index] if self.pads else ()
            if any(before or after for before, after in pad):
                array = _pad_array(array, pad, self.pad_value)
            out.append(array)
        return out

    def padding_metadata(self) -> dict:
        """What was padded, per input, for the result's metadata."""
        axes = list(self.labels)
        per_input = []
        for index, pad in enumerate(self.pads):
            if not any(before or after for before, after in pad):
                continue
            per_input.append({
                "input": self.names[index],
                "original_shape": list(self.shapes[index]),
                "padded_shape": list(self.padded_shape(index)),
                "before": {axes[a]: int(b) for a, (b, _a) in enumerate(pad) if b},
                "after": {axes[a]: int(af) for a, (_b, af) in enumerate(pad) if af},
            })
        return {
            "anchor": self.pad_anchor,
            "value": self.pad_value,
            "fraction": round(self.pad_fraction, 6),
            "inputs": per_input,
            # Padding at the origin side moves every pixel of that input; it
            # is the first input's grid only while nothing is added before it.
            "first_input_grid_preserved": not any(
                before for before, _after in (self.pads[0] if self.pads else ())
            ),
        }

    def describe(self) -> str:
        """What the combine will do, in plain words."""
        n = len(self.shapes)
        shape = tuple(self.output_shape)
        labels = ", ".join(self.output_labels)
        if self.mode == "stack":
            head = f"Stacks {n} inputs along a new {self.new_axis_label or ''} axis".replace("  ", " ")
        elif self.appended:
            names = ", ".join(f"'{self.names[i]}'" for i in self.appended)
            head = (
                f"Appends {names} to the {self.join_label} axis of "
                f"'{self.names[self.reference]}'"
            )
        else:
            head = f"Joins {n} inputs along the existing {self.join_label} axis"
        lines = [f"{head}, in the order listed.", f"Result: {shape} ({labels})."]
        if self.padded:
            padded = [
                f"'{self.names[i]}' {tuple(self.shapes[i])} -> {self.padded_shape(i)}"
                for i, pad in enumerate(self.pads)
                if any(before or after for before, after in pad)
            ]
            value = {"0": "zeros", "nan": "NaN", "edge": "the edge pixels"}[self.pad_value]
            lines.append(
                f"Pads {'; '.join(padded)} ({self.pad_anchor}, {value}); "
                f"{self.pad_fraction * 100:.0f}% of the result is padding."
            )
        return " ".join(lines)


def _pad_array(array: np.ndarray, pad, value: str) -> np.ndarray:
    widths = [(int(before), int(after)) for before, after in pad]
    if value == "edge":
        return np.pad(array, widths, mode="edge")
    if value == "nan":
        if not np.issubdtype(array.dtype, np.floating):
            raise CombineMismatch(
                f"NaN padding needs floating-point data, this is {array.dtype}; pad with 0 or edge",
                "pad_value",
            )
        return np.pad(array, widths, mode="constant", constant_values=np.nan)
    return np.pad(array, widths, mode="constant", constant_values=0)


@dataclass(frozen=True)
class _Input:
    name: str
    shape: tuple[int, ...]
    labels: list
    scales: list
    unit: str
    dtype: object = None


def _read_input(result, index: int) -> _Input:
    name = getattr(result, "name", f"input {index}")
    try:
        shape = tuple(shape_for_result(result))
        labels = list(axis_labels_for_result(result))
        scales = list(axis_scales_for_result(result))
    except Exception:
        what = "the first input" if index == 1 else f"'{name}'"
        raise CombineMismatch(f"Could not read the shape of {what}", "unreadable") from None
    dtype = getattr(getattr(result, "data", None), "dtype", None)
    return _Input(name, shape, labels, scales, getattr(result, "scale_unit", "px"), dtype)


def combine_compatibility(
    results: Sequence[ProcessingResult],
    mode: str = "stack",
    join_axis: int = 0,
    new_axis_label: str | None = None,
    **options,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for combining ``results`` without reading pixels.

    The reason string is user-facing: the combine dialog shows it instead of
    presenting a silently disabled OK button. Metadata must match across ALL
    inputs — axis labels, axis scales (including the join axis) and the
    scale unit — so same-shaped but physically different arrays can never
    silently become one calibrated stack. Two things are forgiven because
    they are not differences in the pixels: singleton axes one input has and
    another lacks, and a length unit stated differently (nm against um).
    Sizes may differ only when ``pad="pad"`` is passed, and ranks only in
    ``mode="auto"`` (see the module docstring); ``options`` are the keywords
    of :func:`align_results`.
    This check must stay metadata-only: it uses the shape/labels/scales
    accessors and never materializes lazy pixel data.
    """
    try:
        align_results(
            results, mode=mode, join_axis=join_axis, new_axis_label=new_axis_label, **options
        )
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def _candidate_axes(ref_labels, lower_labels) -> list[int]:
    """Axes of the reference whose removal leaves exactly ``lower_labels``."""
    return [
        axis for axis in range(len(ref_labels))
        if ref_labels[:axis] + ref_labels[axis + 1:] == list(lower_labels)
    ]


def preferred_candidate(candidates) -> tuple[int, str] | None:
    """The best guess among ambiguous ``(axis, label)`` candidates: Z, then T."""
    candidates = list(candidates)
    for wanted in ("Z", "T"):
        for candidate in candidates:
            if candidate[1] == wanted:
                return candidate
    return candidates[0] if candidates else None


def _plan_append(inputs, append_axis):
    """``(reference index, join axis)`` for ``auto`` with unequal ranks.

    Raises :class:`CombineMismatch` when the lower-rank inputs do not line up
    as the higher-rank one with a single axis left out, or when that axis is
    not the only possibility and ``append_axis`` does not say which.
    """
    ranks = [len(item.shape) for item in inputs]
    top = max(ranks)
    ref = ranks.index(top)
    base = inputs[ref]
    lowers = [k for k, rank in enumerate(ranks) if rank < top]
    for k in lowers:
        gap = top - ranks[k]
        if gap != 1:
            raise CombineMismatch(
                f"'{inputs[k].name}' is {ranks[k]}D but '{base.name}' is {top}D: it lacks {gap} axes, "
                f"and only a single missing axis can be appended. Combine it with data of its own rank "
                f"first, or stack it",
                "rank",
            )
    options = None
    for k in lowers:
        found = set(_candidate_axes(base.labels, inputs[k].labels))
        if not found:
            raise CombineMismatch(
                f"'{inputs[k].name}' axes {inputs[k].labels} are not '{base.name}' axes "
                f"{base.labels} with one axis left out; {_LABEL_HINT}",
                "labels",
            )
        options = found if options is None else options & found
    if not options:
        raise CombineMismatch(
            "The lower-rank inputs lack different axes of "
            f"'{base.name}' ({base.labels}), so they cannot all be appended in one step",
            "rank",
        )
    requested = (append_axis or "").strip() if isinstance(append_axis, str) else append_axis
    if requested not in (None, ""):
        by_label = [a for a, label in enumerate(base.labels) if label == str(requested)]
        if by_label:
            wanted = set(by_label)
        elif str(requested).lstrip("-").isdigit() and 0 <= int(requested) < top:
            wanted = {int(requested)}
        else:
            raise CombineMismatch(
                f"Append axis '{requested}' is not an axis of '{base.name}' ({base.labels})", "axis"
            )
        options = options & wanted
        if not options:
            raise CombineMismatch(
                f"The lower-rank inputs cannot be appended along '{requested}': what they lack "
                f"of '{base.name}' ({base.labels}) is the "
                f"{' or '.join(base.labels[a] for a in sorted(set(_all_candidates(base, inputs, lowers))))} axis",
                "labels",
            )
    if len(options) > 1:
        choices = [(a, base.labels[a]) for a in sorted(options)]
        raise CombineMismatch(
            "Cannot tell which axis to append along: the lower-rank inputs could join "
            f"'{base.name}' along {' or '.join(f'{label} (axis {a})' for a, label in choices)}. "
            "Choose the axis",
            "ambiguous",
            candidates=choices,
        )
    return ref, next(iter(options))


def _all_candidates(base, inputs, lowers):
    found = set()
    for k in lowers:
        found |= set(_candidate_axes(base.labels, inputs[k].labels))
    return found


def _pad_flag(pad) -> bool:
    if pad in (True, "pad"):
        return True
    if pad in (False, None, "", "none"):
        return False
    raise CombineMismatch(f"pad must be 'none' or 'pad', got {pad!r}", "param")


def align_results(
    results: Sequence[ProcessingResult],
    mode: str = "stack",
    join_axis: int = 0,
    new_axis_label: str | None = None,
    *,
    pad="none",
    pad_anchor: str = "centre",
    pad_value: str = "0",
    pad_max_ratio: float = DEFAULT_PAD_MAX_RATIO,
    append_axis=None,
) -> CombineAlignment:
    """Line the inputs up for a combine, or raise ``CombineMismatch`` (a ``ValueError``).

    See :func:`combine_compatibility` and the module docstring for the
    rules. Metadata-only. ``pad``, ``pad_anchor``, ``pad_value`` and
    ``pad_max_ratio`` describe padding; ``append_axis`` (a label or index in
    the higher-rank input) settles an ambiguous ``auto`` append.
    """
    results = list(results or [])
    if len(results) < 2:
        raise CombineMismatch("Select at least two image results to combine", "count")
    if mode not in COMBINE_MODES:
        raise CombineMismatch(
            f"mode must be 'stack', 'concatenate' or 'auto', got {mode!r}", "param"
        )
    pad_on = _pad_flag(pad)
    if pad_anchor not in PAD_ANCHORS:
        raise CombineMismatch(f"pad_anchor must be one of {PAD_ANCHORS}, got {pad_anchor!r}", "param")
    if pad_value not in PAD_VALUES:
        raise CombineMismatch(f"pad_value must be one of {PAD_VALUES}, got {pad_value!r}", "param")
    max_ratio = float(DEFAULT_PAD_MAX_RATIO if pad_max_ratio is None else pad_max_ratio)

    inputs = [_read_input(result, index) for index, result in enumerate(results, start=1)]
    requested_mode = mode
    ref = 0
    append_join = None
    fallback_error = None
    if mode == "auto":
        mode = "stack"
        if len({len(item.shape) for item in inputs}) > 1:
            try:
                ref, append_join = _plan_append(inputs, append_axis)
                mode = "concatenate"
                join_axis = append_join
            except CombineMismatch as exc:
                if exc.kind in ("ambiguous", "axis"):
                    raise
                # Not an append: what stacking does with singleton axes (a
                # (1, 1, 1, 1, Y, X) result beside a (Y, X) one) still
                # applies; if that does not work either, the reason to
                # report is why the append did not.
                fallback_error = exc
                ref = 0
    try:
        return _align(
            results, inputs, ref, mode, join_axis, new_axis_label, append_join, requested_mode,
            pad_on, pad_anchor, pad_value, max_ratio,
        )
    except CombineMismatch as exc:
        # The append did not work and neither did stacking. A stacking
        # failure about labels, scales or units is the one to act on; one
        # that is only about the ranks differing is the append's to explain.
        if fallback_error is not None and exc.kind == "rank":
            raise fallback_error from None
        raise


def _align(
    results, inputs, ref, mode, join_axis, new_axis_label, append_join, requested_mode,
    pad_on, pad_anchor, pad_value, max_ratio,
) -> CombineAlignment:
    base = inputs[ref]
    shape, labels, unit = base.shape, base.labels, base.unit
    first_name = base.name

    if mode == "concatenate" and not (0 <= join_axis < len(shape)):
        raise CombineMismatch(
            f"Join axis {join_axis} is out of range for {len(shape)}D data", "axis"
        )

    views = []
    units = []
    for item in inputs:
        scales = list(item.scales)
        # A unit that cannot be converted is reported after the shape and
        # the labels, which are the coarser differences and the ones the
        # user sees first.
        factor = _unit_factor(unit, item.unit)
        if factor is not None:
            scales = [s * factor for s in scales]
        views.append((item.shape, item.labels, scales))
        units.append(item.unit)

    # Inputs that agree axis for axis are combined as they are. Inputs that
    # do not are read without their singleton axes, so that the
    # (1, 1, 1, 1, Y, X) of one reconstructor and the (Y, X) of another
    # count as the same picture; the join axis is kept, singleton or not.
    # Inputs that differ only in the size of the Y/X plane (what padding
    # fixes) agree axis for axis too: nothing is squeezed on account of it.
    ref_view = views[ref]
    plane_axes = [
        a for a in range(max(0, len(shape) - 2), len(shape))
        if not (mode == "concatenate" and a == join_axis)
    ]
    same_axes = all(
        (v[0] == shape and v[1] == labels)
        or (
            v[1] == labels
            and all(v[0][a] == shape[a] for a in range(len(shape)) if a not in plane_axes)
        )
        for v in views
    )
    join_label = labels[join_axis] if mode == "concatenate" else None
    if append_join is not None:
        # The lower-rank inputs are the reference minus one axis, matched by
        # label: they gain a length-1 axis there and nothing else changes.
        join = append_join
        reduced = [
            view if len(view[0]) == len(shape)
            else _insert_axis(view, join_label, join, ref_view[2][join])
            for view in views
        ]
        appended = tuple(k for k, view in enumerate(views) if len(view[0]) != len(shape))
    elif same_axes:
        reduced = views
        join = join_axis if mode == "concatenate" else None
        appended = ()
    else:
        reduced = [_without_singletons(*view, keep=join_label) for view in views]
        join = None
        appended = ()
        if join_label is not None:
            join = reduced[ref][1].index(join_label)
            reduced = [
                view if k == ref else _with_join_axis(view, join_label, join, reduced[ref][2][join])
                for k, view in enumerate(reduced)
            ]
    out_shape, out_labels, out_scales = reduced[ref]

    # A duplicate axis label would break every label-driven axis lookup
    # downstream (resolve_axis takes the FIRST match), so [Z, Z, Y, X] is
    # blocked here rather than handled ad hoc by each consumer.
    if mode == "stack" and new_axis_label is not None and new_axis_label in out_labels:
        raise CombineMismatch(
            f"Axis label '{new_axis_label}' already exists in the inputs "
            f"({out_labels}); pick a label that is not already used",
            "label_collision",
        )

    # Only the Y/X plane can be padded, and not the axis being joined.
    plane = [a for a in range(max(0, len(out_shape) - 2), len(out_shape)) if a != join]
    resized = False
    for k, (view, (other_shape, other_labels, other_scales)) in enumerate(zip(views, reduced)):
        if k == ref:
            continue
        result_name = inputs[k].name
        if mode == "concatenate":
            if len(other_shape) != len(out_shape):
                raise CombineMismatch(
                    f"'{result_name}' is {len(view[0])}D but "
                    f"'{first_name}' is {len(shape)}D",
                    "rank",
                )
            mismatched = [
                axis
                for axis in range(len(out_shape))
                if axis != join and other_shape[axis] != out_shape[axis]
            ]
            if mismatched and not (pad_on and set(mismatched) <= set(plane)):
                raise CombineMismatch(
                    f"'{result_name}' shape {tuple(view[0])} does not match "
                    f"'{first_name}' shape {tuple(shape)} outside the join axis"
                    + _size_hint(pad_on, mismatched, plane),
                    "size",
                )
            resized = resized or bool(mismatched)
        elif other_shape != out_shape:
            different = (
                [a for a in range(len(out_shape)) if other_shape[a] != out_shape[a]]
                if len(other_shape) == len(out_shape) else None
            )
            if different is None or not (pad_on and set(different) <= set(plane)):
                raise CombineMismatch(
                    f"'{result_name}' shape {tuple(view[0])} does not match "
                    f"'{first_name}' shape {tuple(shape)}"
                    + (_size_hint(pad_on, different, plane) if different else ""),
                    "size" if different else "rank",
                )
            resized = True
        if other_labels != out_labels:
            raise CombineMismatch(
                f"'{result_name}' axes {view[1]} do not match "
                f"'{first_name}' axes {labels}; {_LABEL_HINT}",
                "labels",
            )
        if _unit_factor(unit, units[k]) is None:
            raise CombineMismatch(
                f"'{result_name}' scale unit '{units[k]}' does not match "
                f"'{first_name}' unit '{unit}'; {_SCALE_HINT}",
                "unit",
            )
        if not _scales_close(other_scales, out_scales):
            across = units[k] != unit
            raise CombineMismatch(
                f"'{result_name}' pixel scales do not match '{first_name}'"
                + (f" (its unit {units[k]} converted to {unit})" if across else "")
                + f"; {_SCALE_HINT}",
                "scale",
            )

    shapes = tuple(tuple(view[0]) for view in reduced)
    pads = _pad_plan(inputs, shapes, plane, out_labels, pad_anchor, pad_value, max_ratio) if resized else ()
    if pads:
        padded = [
            tuple(s + b + a for s, (b, a) in zip(shp, pad)) for shp, pad in zip(shapes, pads)
        ]
    else:
        padded = list(shapes)
    if mode == "stack":
        final = (len(inputs), *padded[0])
    else:
        final = list(padded[0])
        final[join] = sum(p[join] for p in padded)
        final = tuple(final)
    return CombineAlignment(
        shapes=shapes,
        labels=tuple(out_labels),
        scales=tuple(float(s) for s in out_scales),
        unit=str(unit),
        mode=mode,
        requested_mode=requested_mode,
        join=join if mode == "concatenate" else None,
        join_label=join_label,
        appended=appended,
        pads=pads,
        pad_anchor=pad_anchor,
        pad_value=pad_value,
        output_shape=final,
        names=tuple(item.name for item in inputs),
        reference=ref,
        new_axis_label=new_axis_label,
    )


def _size_hint(pad_on: bool, different, plane) -> str:
    """Say what would help (or why padding cannot) with a size difference."""
    if not pad_on:
        if different and set(different) <= set(plane):
            return "; pad them to a common size, or crop/resample one"
        return ""
    if different and not set(different) <= set(plane):
        return "; only the last two (Y/X) axes can be padded"
    return ""


def _pad_plan(inputs, shapes, plane, labels, anchor, value, max_ratio):
    """Per input, ``(before, after)`` per axis that pads ``plane`` to the largest size."""
    rank = len(shapes[0])
    targets = {a: max(shape[a] for shape in shapes) for a in plane}
    pads = []
    for item, shape in zip(inputs, shapes):
        pad = [(0, 0)] * rank
        for a in plane:
            extra = targets[a] - shape[a]
            if extra <= 0:
                continue
            if max_ratio > 0 and shape[a] > 0 and targets[a] / shape[a] > max_ratio:
                raise CombineMismatch(
                    f"'{item.name}' would grow {targets[a] / shape[a]:.1f}x along its "
                    f"{labels[a]} axis "
                    f"({shape[a]} -> {targets[a]} px) when padded, past the {max_ratio:g}x limit; "
                    f"raise the largest padding ratio (0 = no limit) if that is intended, "
                    f"or resample with Scale/Resize",
                    "pad_limit",
                )
            before = extra // 2 if anchor == "centre" else 0
            pad[a] = (before, extra - before)
        if value == "nan" and any(b or a for b, a in pad):
            dtype = item.dtype
            if dtype is not None:
                try:
                    floating = np.issubdtype(np.dtype(dtype), np.floating)
                except TypeError:
                    floating = True
                if not floating:
                    raise CombineMismatch(
                        f"NaN padding needs floating-point data, but '{item.name}' is {dtype}; "
                        f"pad with 0 or edge",
                        "pad_value",
                    )
        pads.append(tuple(pad))
    return tuple(pads)


def _without_singletons(shape, labels, scales, keep=None):
    """The view without its size-1 axes, except one labelled ``keep``."""
    kept = [
        axis for axis, size in enumerate(shape)
        if size != 1 or (keep is not None and labels[axis] == keep)
    ]
    return (
        tuple(shape[axis] for axis in kept),
        [labels[axis] for axis in kept],
        [scales[axis] for axis in kept],
    )


def _with_join_axis(view, label, position, scale):
    """``view`` with a singleton axis ``label`` at ``position`` if it lacks one."""
    shape, labels, scales = view
    if label in labels:
        return view
    return _insert_axis(view, label, position, scale)


def _insert_axis(view, label, position, scale):
    shape, labels, scales = (list(part) for part in view)
    shape.insert(position, 1)
    labels.insert(position, label)
    scales.insert(position, scale)
    return tuple(shape), labels, scales


def _broadcast_alignment(shape, other_shape):
    """``(result_shape, offset, other_offset)`` for two right-aligned shapes.

    ``None`` when they cannot be lined up. Right-aligned because that is what
    numpy does, and the whole point is to agree with what the arithmetic will
    actually compute.
    """
    width = max(len(shape), len(other_shape))
    left = (1,) * (width - len(shape)) + tuple(shape)
    right = (1,) * (width - len(other_shape)) + tuple(other_shape)
    out = []
    for a, b in zip(left, right):
        if a == b or a == 1 or b == 1:
            out.append(max(a, b))
        else:
            return None
    result = tuple(out)
    # The answer has to be one of the inputs, not something larger than both.
    # Without this, a (233, 1) column and a (1, 233) row "broadcast" into a
    # 233x233 image neither of them contains — arithmetic numpy will happily
    # perform and nobody asked for.
    if result not in (tuple(shape), tuple(other_shape)):
        return None
    return result, width - len(shape), width - len(other_shape)


def elementwise_compatibility(
    results: Sequence[ProcessingResult],
) -> tuple[bool, str]:
    """``(ok, reason)`` for pixel-wise arithmetic between results.

    Deliberately looser than :func:`combine_compatibility`, because the two
    answer different questions. Stacking builds one array, so the shapes must
    genuinely agree; arithmetic only needs the operands to line up, and numpy
    lines up a ``(1, 233, 233)`` image with a ``(233, 233)`` mask without
    ambiguity. Refusing that pair — as this did, because the calculator reused
    the stacking check — turns "apply this mask to the image I drew it on"
    into an error about two shapes the user can see are the same picture.

    Length-1 axes may broadcast, and a plane may broadcast across a stack: one
    mask applied to every slice is what a mask on a stack means. What is still
    refused is anything where the answer is bigger than both inputs, and
    anything whose *shared* axes disagree in label, scale or unit — a nm result
    and a um one are not comparable however well their shapes line up.

    Metadata-only, like the stacking check: no pixel data is read.
    """
    results = list(results or [])
    if len(results) < 2:
        return False, "Select two image results"

    try:
        shape = tuple(shape_for_result(results[0]))
        labels = list(axis_labels_for_result(results[0]))
        scales = list(axis_scales_for_result(results[0]))
    except Exception:
        return False, "Could not read the shape of the first input"
    unit = getattr(results[0], "scale_unit", "px")
    first_name = getattr(results[0], "name", "input 1")

    for index, result in enumerate(results[1:], start=2):
        result_name = getattr(result, "name", f"input {index}")
        try:
            other_shape = tuple(shape_for_result(result))
            other_labels = list(axis_labels_for_result(result))
            other_scales = list(axis_scales_for_result(result))
        except Exception:
            return False, f"Could not read the shape of '{result_name}'"

        other_unit = getattr(result, "scale_unit", "px")
        factor = _unit_factor(unit, other_unit)
        if factor is None and "px" not in (other_unit, unit):
            return False, (
                f"'{result_name}' scale unit '{other_unit}' does not match "
                f"'{first_name}' unit '{unit}'"
            )
        if factor is not None:
            other_scales = [scale * factor for scale in other_scales]

        alignment = _broadcast_alignment(shape, other_shape)
        if alignment is None:
            return False, (
                f"'{result_name}' shape {other_shape} cannot be lined up with "
                f"'{first_name}' shape {shape}"
            )
        _result_shape, offset, other_offset = alignment

        # Only the axes both inputs actually have are compared. A broadcast
        # axis has no counterpart to disagree with, so demanding a matching
        # label there is demanding a label for an axis that is not there.
        shared = min(len(shape), len(other_shape))
        for position in range(1, shared + 1):
            mine, theirs = len(shape) - position, len(other_shape) - position
            if shape[mine] == 1 or other_shape[theirs] == 1:
                continue
            if labels[mine] != other_labels[theirs]:
                return False, (
                    f"'{result_name}' axis '{other_labels[theirs]}' does not "
                    f"match '{first_name}' axis '{labels[mine]}'"
                )
            if not _scales_close([other_scales[theirs]], [scales[mine]]):
                return False, (
                    f"'{result_name}' pixel scales along "
                    f"'{other_labels[theirs]}' do not match '{first_name}'"
                    + (f" (its unit {other_unit} converted to {unit})"
                       if other_unit != unit else "")
                )

    return True, ""


def default_stack_axis_label(labels: Sequence[str]) -> str:
    """Return the first common stack label not colliding with ``labels``."""
    for candidate in (*STACK_AXIS_LABELS, "S"):
        if candidate not in labels:
            return candidate
    index = 0
    while f"S{index}" in labels:
        index += 1
    return f"S{index}"


def _sources(results) -> list[str]:
    return [getattr(r, "name", "result") for r in results]


def _build(
    results, alignment: CombineAlignment, *, name: str, axis_label: str | None
) -> ArrayProcessingResult:
    """The combined result for an alignment already decided."""
    labels = list(alignment.labels)
    scales = list(alignment.scales)
    arrays = alignment.arrays(results)
    metadata = {
        "operation": StackCombineProcessor.id,
        "mode": alignment.mode,
        "source_results": _sources(results),
    }
    view_modes = None
    if alignment.mode == "stack":
        data = np.stack(arrays, axis=0)
        # A C-labelled stack is a channel stack; render it like channel-merge does.
        if axis_label == "C":
            view_modes = [ViewMode("Channels", tuple(range(data.ndim)))]
        out_labels = [axis_label, *labels]
        out_scales = [1.0, *scales]
        metadata["stack_axis"] = axis_label
    else:
        data = np.concatenate(arrays, axis=alignment.join)
        out_labels = list(labels)
        out_scales = list(scales)
        metadata["join_axis"] = alignment.join_label
        if alignment.appended:
            metadata["appended"] = [alignment.names[i] for i in alignment.appended]
    if alignment.requested_mode == "auto":
        metadata["requested_mode"] = "auto"
    if alignment.padded:
        metadata["padding"] = alignment.padding_metadata()
    return ArrayProcessingResult(
        name=name,
        data=data,
        axis_labels=out_labels,
        view_modes=view_modes,
        display_levels=finite_range(data),
        axis_scales=out_scales,
        scale_unit=alignment.unit,
        metadata=metadata,
    )


def combine_results(
    results: Sequence[ProcessingResult],
    *,
    mode: str = "stack",
    join_axis: int = 0,
    axis_label: str = "Z",
    name: str | None = None,
    **options,
) -> ArrayProcessingResult:
    """Combine ``results`` as ``mode`` (``stack``, ``concatenate`` or ``auto``) says.

    ``options`` are the padding and append keywords of :func:`align_results`.
    """
    results = list(results or [])
    alignment = align_results(
        results,
        mode=mode,
        join_axis=join_axis,
        new_axis_label=axis_label if mode in ("stack", "auto") else None,
        **options,
    )
    default = "Stacked" if alignment.mode == "stack" else "Concatenated"
    return _build(results, alignment, name=name or default, axis_label=axis_label)


def stack_results(
    results: Sequence[ProcessingResult],
    *,
    axis_label: str = "Z",
    name: str = "Stacked",
    **options,
) -> ArrayProcessingResult:
    """Stack same-shaped results along a new leading ``axis_label`` axis.

    ``options`` (``pad``, ``pad_anchor``, ``pad_value``, ``pad_max_ratio``)
    let results of different Y/X size be padded to a common one.
    """
    results = list(results or [])
    alignment = align_results(
        results, mode="stack", new_axis_label=axis_label, **options
    )
    return _build(results, alignment, name=name, axis_label=axis_label)


def concatenate_results(
    results: Sequence[ProcessingResult],
    *,
    join_axis: int = 0,
    name: str = "Concatenated",
    **options,
) -> ArrayProcessingResult:
    """Append results along the existing shared axis ``join_axis``."""
    results = list(results or [])
    alignment = align_results(
        results, mode="concatenate", join_axis=join_axis, **options
    )
    return _build(results, alignment, name=name, axis_label=None)


def _scales_close(scales: Sequence[float], other: Sequence[float]) -> bool:
    """Compare axis scales with float tolerance instead of exact equality."""
    if len(scales) != len(other):
        return False
    return bool(np.allclose(scales, other, rtol=1e-5, atol=1e-8))


__all__ = [
    "COMBINE_MODES",
    "CombineAlignment",
    "CombineMismatch",
    "DEFAULT_PAD_MAX_RATIO",
    "PAD_ANCHORS",
    "PAD_MODES",
    "PAD_VALUES",
    "StackCombineProcessor",
    "STACK_AXIS_LABELS",
    "align_results",
    "combine_compatibility",
    "combine_options",
    "combine_results",
    "concatenate_results",
    "default_stack_axis_label",
    "preferred_candidate",
    "stack_results",
]
