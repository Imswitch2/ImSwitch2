"""Camera-based photophysics suite — ImProcess drop-in analysis plugin.

Reversibly-switchable fluorescent protein (RSFP) photophysics characterization
from a camera recording, ported from the TestaLab MATLAB scripts
(ON.m / OFF_g.m / FATIGUE.m). One :class:`Processor` with a mode selector:

* ``fatigue`` — bleaching decay: background-subtracted, normalized fluorescence
  vs. cycle (from FATIGUE.m).
* ``off`` — off-switching kinetics: cut per-cycle decays, average, characteristic
  times + 1-/2-exponential fit (from OFF_g.m / offFit.m). *[P1]*
* ``on`` — photo-activation: integrate fluorescence plateaus vs. activation power
  (from ON.m). *[P2]*

The analysis operates on the intensity-vs-frame profile of the loaded stack
(whole-frame sum or ROI mean), so it takes an image result and returns a
``curve`` result rendered as a line plot and saved as an ascii/csv table.

Drop this file into ``~/ImSwitchConfig/improcess_plugins/`` to load it at ImProcess
startup. Kept in the repo under ``examples/`` for development and testing until the
public plugins repo is available.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries


# ---------------------------------------------------------------------------
# Analysis core — pure numpy, no Qt, unit-testable in isolation.
# ---------------------------------------------------------------------------

def frame_profile(stack, roi=None, reduce="sum"):
    """Reduce an image stack to a 1-D intensity-vs-frame profile.

    ``stack`` has spatial axes as the last two dims (``..., Y, X``) and frames
    along the third-from-last (``..., T, Y, X``); any leading axes are flattened
    into the frame sequence. ``roi`` is ``(y0, y1, x0, x1)`` in pixels; ``reduce``
    is ``"sum"`` or ``"mean"`` over the spatial axes.
    """
    arr = np.asarray(stack, dtype=float)
    if arr.ndim < 3:
        raise ValueError(
            f"frame_profile needs a stack with >=3 dims (…,T,Y,X), got shape {arr.shape}"
        )
    if roi is not None:
        y0, y1, x0, x1 = (int(v) for v in roi)
        arr = arr[..., y0:y1, x0:x1]
    if arr.shape[-1] == 0 or arr.shape[-2] == 0:
        raise ValueError("ROI selects an empty region")
    reducer = np.sum if reduce == "sum" else np.mean
    profile = reducer(arr, axis=(-2, -1))
    return np.asarray(profile, dtype=float).reshape(-1)


def subtract_background(profile, mode="tail_mean", value=0.0, tail=500):
    """Return ``(profile - background, background)``.

    ``mode``: ``"none"`` (0), ``"constant"`` (``value``), or ``"tail_mean"`` (mean
    of the last ``tail`` frames — the recording's dark baseline, as in OFF_g.m).
    """
    p = np.asarray(profile, dtype=float)
    if mode == "none":
        bkg = 0.0
    elif mode == "constant":
        bkg = float(value)
    elif mode == "tail_mean":
        n = int(min(max(tail, 1), p.size))
        bkg = float(np.mean(p[-n:])) if p.size else 0.0
    else:
        raise ValueError(f"Unknown background mode {mode!r}")
    return p - bkg, bkg


def _normalize(values, mode):
    v = np.asarray(values, dtype=float)
    if mode == "first":
        ref = v[0] if v.size and v[0] != 0 else 1.0
        return v / ref
    if mode == "max":
        m = np.max(v) if v.size else 1.0
        return v / (m or 1.0)
    if mode == "none":
        return v
    raise ValueError(f"Unknown normalization {mode!r}")


def analyze_fatigue(profile, *, background="tail_mean", bkg_value=0.0, tail=500,
                    normalize="first"):
    """Bleaching / fatigue curve (FATIGUE.m).

    Background-subtract, normalize, return fluorescence vs. cycle number.
    """
    profile_bkg, bkg = subtract_background(profile, background, bkg_value, tail)
    cycles = np.arange(1, profile_bkg.size + 1, dtype=float)
    normalized = _normalize(profile_bkg, normalize)
    return {
        "cycles": cycles,
        "profile_bkg": profile_bkg,
        "normalized": normalized,
        "background": bkg,
    }


# ---------------------------------------------------------------------------
# Curve result
# ---------------------------------------------------------------------------

class PhotophysicsResult(ProcessingResult):
    """A photophysics analysis curve (``kind="curve"``), line-plotted + ascii-saved."""

    kind = "curve"

    def __init__(self, name, *, mode, columns, table, title, x_label, y_label,
                 series, scalars=None):
        table = np.asarray(table, dtype=float)
        super().__init__(name=name, data=table, axis_labels=["Point", "Column"])
        self.mode = str(mode)
        self.columns = list(columns)
        self._title = title
        self._x_label = x_label
        self._y_label = y_label
        # series: list of (name, x_array, y_array, style_dict)
        self._series = list(series)
        self.scalars = dict(scalars or {})
        self.metadata = {"mode": self.mode, "columns": self.columns, **self.scalars}

    def plot_payloads(self):
        series = [
            PlotSeries(name=name, x=np.asarray(x, dtype=float),
                       y=np.asarray(y, dtype=float), kind="line", style=dict(style))
            for (name, x, y, style) in self._series
        ]
        return [PlotPayload(title=self._title, x_label=self._x_label,
                            y_label=self._y_label, series=series,
                            metadata=dict(self.scalars))]

    def save(self, path, fmt="txt"):
        path = Path(path)
        arr = np.asarray(self.data, dtype=float)
        if fmt in ("txt", "ascii", "dat"):
            np.savetxt(str(path), arr, header=" ".join(self.columns))
        elif fmt == "csv":
            np.savetxt(str(path), arr, header=",".join(self.columns), delimiter=",")
        else:
            raise ValueError(f"PhotophysicsResult saves txt/csv, got {fmt!r}")


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

_MODES = ("fatigue", "off", "on")


class PhotophysicsProcessor(Processor):
    name = "Photophysics suite"
    id = "user.photophysics-suite"
    category = "Photophysics"
    kinds = ("image",)

    @property
    def applies_to(self):
        # Needs a frame stack (spatial axes + a frame axis).
        return lambda result: getattr(result.data, "ndim", 0) >= 3

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        mode = QtWidgets.QComboBox()
        mode.addItems(_MODES)
        reduce_box = QtWidgets.QComboBox()
        reduce_box.addItems(("sum", "mean"))
        background = QtWidgets.QComboBox()
        background.addItems(("tail_mean", "constant", "none"))
        bkg_value = QtWidgets.QDoubleSpinBox()
        bkg_value.setRange(-1e9, 1e9)
        normalize = QtWidgets.QComboBox()
        normalize.addItems(("first", "max", "none"))

        layout.addRow("Experiment", mode)
        layout.addRow("Frame reduce", reduce_box)
        layout.addRow("Background", background)
        layout.addRow("Background value", bkg_value)
        layout.addRow("Normalize", normalize)

        widget.get_values = lambda: {
            "mode": mode.currentText(),
            "reduce": reduce_box.currentText(),
            "background": background.currentText(),
            "bkg_value": float(bkg_value.value()),
            "normalize": normalize.currentText(),
        }
        return widget

    def apply(self, result, params):
        mode = params.get("mode", "fatigue")
        profile = frame_profile(
            result.data, roi=params.get("roi"), reduce=params.get("reduce", "sum")
        )

        if mode == "fatigue":
            out = analyze_fatigue(
                profile,
                background=params.get("background", "tail_mean"),
                bkg_value=params.get("bkg_value", 0.0),
                tail=params.get("tail", 500),
                normalize=params.get("normalize", "first"),
            )
            columns = ["cycle", "profile_bkg", "normalized"]
            table = np.column_stack(
                [out["cycles"], out["profile_bkg"], out["normalized"]]
            )
            series = [("Rel. fluorescence", out["cycles"], out["normalized"], {})]
            return PhotophysicsResult(
                name=f"{result.name} (fatigue)",
                mode="fatigue",
                columns=columns,
                table=table,
                title="Fatigue / bleaching",
                x_label="# cycle",
                y_label="Rel. fluorescence",
                series=series,
                scalars={"background": out["background"]},
            )

        raise NotImplementedError(
            f"Photophysics mode {mode!r} is not implemented yet "
            "(fatigue is available; off/on land in P1/P2)."
        )
