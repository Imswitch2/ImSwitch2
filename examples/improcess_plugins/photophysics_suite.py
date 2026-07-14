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


def parse_power_sequence(text):
    """Parse a comma/whitespace-separated activation-power list into floats."""
    if text is None:
        return []
    tokens = str(text).replace(",", " ").split()
    out = []
    for token in tokens:
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


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


def _r_squared(y, y_fit):
    y = np.asarray(y, dtype=float)
    ss_res = float(np.sum((y - y_fit) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def _fit_exp(time, y, n_exp):
    """Fit a 1- or 2-exponential decay to a normalized [0,1] curve.

    1-exp: ``k + a*exp(-x/b)`` (offFit.m opts1); 2-exp:
    ``k + a*exp(-x/b) + c*exp(-x/d)`` (opts2). Returns a dict with params, R²
    and the fitted curve, or ``None`` if the fit fails.
    """
    from scipy.optimize import curve_fit

    time = np.asarray(time, dtype=float)
    y = np.asarray(y, dtype=float)
    below_half = np.where(y < 0.5)[0]
    t_half = float(time[below_half[0]]) if below_half.size else float(time[len(time) // 2])
    t_half = max(t_half, float(time[1]) if time.size > 1 else 1.0)

    if n_exp == 1:
        def model(x, a, b, k):
            return k + a * np.exp(-x / b)
        p0 = [1.0, t_half, float(y[-1])]
        bounds = ([0.0, 1e-9, -np.inf], [1.0, 1000.0, np.inf])
    else:
        def model(x, a, b, c, d, k):
            return k + a * np.exp(-x / b) + c * np.exp(-x / d)
        p0 = [1.0, t_half, 0.0, t_half / 10.0, float(y[-1])]
        bounds = ([0.0, 1e-9, 0.0, 1e-9, -np.inf], [1.0, 1000.0, 1.0, 1000.0, np.inf])

    try:
        popt, _ = curve_fit(model, time, y, p0=p0, bounds=bounds, maxfev=10000)
    except Exception:
        return None
    y_fit = model(time, *popt)
    out = {"r2": _r_squared(y, y_fit), "curve": y_fit}
    if n_exp == 1:
        out.update(a=float(popt[0]), tau_ms=float(popt[1]), y0=float(popt[2]))
    else:
        out.update(a1=float(popt[0]), tau1_ms=float(popt[1]),
                   a2=float(popt[2]), tau2_ms=float(popt[3]), y0=float(popt[4]))
    return out


def _find_cycle_peaks(profile, min_height_frac=0.15, min_distance=1):
    from scipy.signal import find_peaks

    profile = np.asarray(profile, dtype=float)
    height = float(np.max(profile)) * float(min_height_frac)
    peaks, _ = find_peaks(profile, height=height, distance=max(1, int(min_distance)))
    return peaks


def analyze_off(profile, *, time_unit_ms=1.0, window=200, n_cycles=None,
                background="tail_mean", bkg_value=0.0, tail=500,
                peak_min_height_frac=0.15, do_fit=True):
    """Off-switching kinetics (OFF_g.m / offFit.m).

    Detect per-cycle peaks, cut ``window``-frame decays after each, average over
    cycles, and report characteristic times (t½/t80/t_end) + optional 1-/2-exp
    fits. ``time_unit_ms`` is the camera exposure (frame period). The first
    detected peak is skipped (as in OFF_g.m).
    """
    profile_bkg, bkg = subtract_background(profile, background, bkg_value, tail)
    window = int(window)
    peaks = _find_cycle_peaks(profile_bkg, peak_min_height_frac, window)

    cuts = []
    limit = len(peaks) if n_cycles is None else min(int(n_cycles), len(peaks))
    for i in range(1, limit):  # skip the first detected peak (OFF_g.m: i = 2:...)
        start = int(peaks[i])
        if start + window <= profile_bkg.size:
            cuts.append(profile_bkg[start:start + window])
    if not cuts:
        raise ValueError(
            "No complete off-switch cycles found — check the window length, "
            "peak-height fraction, and that the recording has cycles."
        )

    stack = np.column_stack(cuts)                 # (window, n_cuts)
    time = np.arange(1, window + 1, dtype=float) * float(time_unit_ms)
    mean = stack.mean(axis=1)
    std = stack.std(axis=1)
    denom = (mean.max() - mean.min()) or 1.0
    norm01 = (mean - mean.min()) / denom

    def _t_at(frac):
        below = np.where(norm01 < frac)[0]
        return float(time[below[0]]) if below.size else float("nan")

    out = {
        "time": time, "mean": mean, "std": std, "normalized": norm01,
        "n_cycles": stack.shape[1], "background": bkg,
        "t_half_ms": _t_at(0.5), "t80_ms": _t_at(0.2), "t_end_ms": _t_at(0.01),
    }
    if do_fit:
        out["fit1"] = _fit_exp(time, norm01, 1)
        out["fit2"] = _fit_exp(time, norm01, 2)
    return out


def analyze_on(profile, powers, *, auto_detect=True, offset=10, jump=35, dpnts=10,
               background="none", bkg_value=0.0, tail=500, normalize="max"):
    """Photo-activation curve (ON.m).

    Integrate the fluorescence plateau of each activation-power step and return
    the activation value vs. power. ``powers`` is the activation-power sequence
    (one per step). With ``auto_detect`` the steps are assumed evenly spaced and
    inferred from ``len(powers)`` (the plateau = the tail ``dpnts`` frames of each
    segment); otherwise the fixed ``offset``/``jump``/``dpnts`` timing from ON.m
    is used.
    """
    profile_bkg, bkg = subtract_background(profile, background, bkg_value, tail)
    powers = np.asarray(powers, dtype=float)
    n = powers.size
    if n == 0:
        raise ValueError("The activation-power list is empty")

    activation = np.zeros(n, dtype=float)
    error = np.zeros(n, dtype=float)
    if auto_detect:
        step_len = profile_bkg.size // n
        if step_len < 1:
            raise ValueError(f"More power steps ({n}) than frames ({profile_bkg.size})")
        d = int(min(max(dpnts, 1), step_len))
        for k in range(n):
            seg = profile_bkg[(k + 1) * step_len - d:(k + 1) * step_len]
            activation[k] = float(np.mean(seg))
            error[k] = float(np.std(seg))
    else:
        pos = int(offset)
        d = int(max(dpnts, 1))
        for k in range(n):
            seg = profile_bkg[pos:pos + d]
            if seg.size == 0:
                raise ValueError("Manual step timing runs past the end of the recording")
            activation[k] = float(np.mean(seg))
            error[k] = float(np.std(seg))
            pos += int(jump)

    normalized = _normalize(activation, normalize)
    return {
        "powers": powers, "activation": activation, "error": error,
        "normalized": normalized, "background": bkg,
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

        # Off-switching-specific parameters, shown only for that mode.
        off_group = QtWidgets.QGroupBox("Off-switching")
        off_form = QtWidgets.QFormLayout(off_group)
        time_unit = QtWidgets.QDoubleSpinBox()
        time_unit.setRange(1e-6, 1e6); time_unit.setDecimals(4); time_unit.setValue(1.0)
        off_window = QtWidgets.QSpinBox()
        off_window.setRange(2, 100000); off_window.setValue(200)
        n_cycles = QtWidgets.QSpinBox()
        n_cycles.setRange(0, 100000); n_cycles.setValue(0)  # 0 = all
        peak_frac = QtWidgets.QDoubleSpinBox()
        peak_frac.setRange(0.0, 1.0); peak_frac.setSingleStep(0.05); peak_frac.setValue(0.15)
        do_fit = QtWidgets.QCheckBox("1-/2-exp fit"); do_fit.setChecked(True)
        off_form.addRow("Exposure / frame (ms)", time_unit)
        off_form.addRow("Window Dt (frames)", off_window)
        off_form.addRow("Cycles to avg (0=all)", n_cycles)
        off_form.addRow("Peak height frac", peak_frac)
        off_form.addRow(do_fit)
        layout.addRow(off_group)

        # Photo-activation-specific parameters, shown only for that mode.
        on_group = QtWidgets.QGroupBox("Photo-activation")
        on_form = QtWidgets.QFormLayout(on_group)
        powers_edit = QtWidgets.QLineEdit("0, 10, 25, 50, 100, 200")
        auto_detect = QtWidgets.QCheckBox("Auto-detect steps"); auto_detect.setChecked(True)
        on_offset = QtWidgets.QSpinBox(); on_offset.setRange(0, 100000); on_offset.setValue(10)
        on_jump = QtWidgets.QSpinBox(); on_jump.setRange(1, 100000); on_jump.setValue(35)
        on_dpnts = QtWidgets.QSpinBox(); on_dpnts.setRange(1, 100000); on_dpnts.setValue(10)
        on_form.addRow("Power sequence", powers_edit)
        on_form.addRow(auto_detect)
        on_form.addRow("Offset (manual)", on_offset)
        on_form.addRow("Jump (manual)", on_jump)
        on_form.addRow("Plateau pts (Dpnts)", on_dpnts)
        layout.addRow(on_group)

        def _update_visibility():
            current = mode.currentText()
            off_group.setVisible(current == "off")
            on_group.setVisible(current == "on")
        mode.currentTextChanged.connect(lambda _t: _update_visibility())
        _update_visibility()

        widget.get_values = lambda: {
            "mode": mode.currentText(),
            "reduce": reduce_box.currentText(),
            "background": background.currentText(),
            "bkg_value": float(bkg_value.value()),
            "normalize": normalize.currentText(),
            "time_unit_ms": float(time_unit.value()),
            "window": int(off_window.value()),
            "n_cycles": int(n_cycles.value()),
            "peak_min_height_frac": float(peak_frac.value()),
            "do_fit": bool(do_fit.isChecked()),
            "powers": parse_power_sequence(powers_edit.text()),
            "auto_detect": bool(auto_detect.isChecked()),
            "offset": int(on_offset.value()),
            "jump": int(on_jump.value()),
            "dpnts": int(on_dpnts.value()),
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

        if mode == "off":
            out = analyze_off(
                profile,
                time_unit_ms=params.get("time_unit_ms", 1.0),
                window=params.get("window", 200),
                n_cycles=params.get("n_cycles") or None,
                background=params.get("background", "tail_mean"),
                bkg_value=params.get("bkg_value", 0.0),
                tail=params.get("tail", 500),
                peak_min_height_frac=params.get("peak_min_height_frac", 0.15),
                do_fit=params.get("do_fit", True),
            )
            columns = ["time_ms", "mean", "std", "normalized"]
            table = np.column_stack(
                [out["time"], out["mean"], out["std"], out["normalized"]]
            )
            series = [("Averaged decay", out["time"], out["normalized"], {})]
            scalars = {
                "n_cycles": out["n_cycles"], "background": out["background"],
                "t_half_ms": out["t_half_ms"], "t80_ms": out["t80_ms"],
                "t_end_ms": out["t_end_ms"],
            }
            for label, key in (("1-exp", "fit1"), ("2-exp", "fit2")):
                fit = out.get(key)
                if fit:
                    series.append((f"{label} fit", out["time"], fit["curve"], {}))
                    scalars.update(
                        {f"{key}_{k}": v for k, v in fit.items() if k != "curve"}
                    )
            return PhotophysicsResult(
                name=f"{result.name} (off-switch)",
                mode="off",
                columns=columns,
                table=table,
                title="Off-switching kinetics",
                x_label="time (ms)",
                y_label="Norm. fluorescence",
                series=series,
                scalars=scalars,
            )

        if mode == "on":
            out = analyze_on(
                profile,
                params.get("powers") or [],
                auto_detect=params.get("auto_detect", True),
                offset=params.get("offset", 10),
                jump=params.get("jump", 35),
                dpnts=params.get("dpnts", 10),
                background=params.get("background", "none"),
                bkg_value=params.get("bkg_value", 0.0),
                tail=params.get("tail", 500),
                normalize=params.get("normalize", "max"),
            )
            columns = ["power", "activation", "error", "normalized"]
            table = np.column_stack(
                [out["powers"], out["activation"], out["error"], out["normalized"]]
            )
            series = [("Activation", out["powers"], out["normalized"],
                       {"symbol": "o"})]
            return PhotophysicsResult(
                name=f"{result.name} (photo-activation)",
                mode="on",
                columns=columns,
                table=table,
                title="Photo-activation",
                x_label="activation power",
                y_label="Rel. fluorescence",
                series=series,
                scalars={"background": out["background"]},
            )

        raise ValueError(f"Unknown photophysics mode {mode!r}; expected one of {_MODES}")
