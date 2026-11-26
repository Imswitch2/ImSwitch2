"""Image (+ labels) -> points: dF/F per ROI, one row per ROI, with a trigger flag.

The measuring and deciding step of a signal-triggered pipeline (a camera
image shows a cell lighting up; the pipeline reacts). Per frame and per ROI
it measures ``F`` (mean or median of the ROI, after an optional background
subtraction), keeps a baseline ``F0`` per ROI by a selectable rule, and
reports ``dff = (F - F0) / F0`` and a ``triggered`` flag.

**The mask is frozen.** Re-segmenting every frame would relabel the ROIs and
destroy every baseline, so the processor segments once -- from the average
of the first ``reference_frames`` frames (``mask_source = segment``, with
the segmentation code of the Segmentation processor), from the labels an
upstream step delivers on the first tick (``input``), or from a saved label
file (``file``) -- and keeps the ROIs in its state. Change ``generation``
(or re-arm the pipeline) to start over. A frame of another shape or pixel
grid resets the state, with a message, instead of computing garbage.

**No trigger during warm-up.** Until the baseline has its frames the rows
carry ``triggered = False`` and the result's ``state`` metadata says
``warming up, n of N``.

Columns of the points result (coordinates are the centroid ``(y, x)`` in
camera pixels, rows sorted by ``dff`` descending): ``label``, ``area``,
``bbox_y0, bbox_x0, bbox_y1, bbox_x1`` (the ROI's bounding box in the
centroid's convention, pixel centres at integers, so a box of w pixels is w
wide), ``F``, ``F0``, ``dff``, ``triggered``, ``valid``.

Per-frame work is vectorised on the pixels that belong to a ROI (one
``bincount`` over a pre-computed index), not on the frame.
"""

from __future__ import annotations

import json
import math
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import ProcessingResult, result_kind
from imswitch.improcess.processors.base import Processor, ProcessorInputError

STATE_PARAM = "_state"

MASK_SOURCES = ("segment", "input", "file")
STATISTICS = ("mean", "median")
BACKGROUNDS = ("none", "constant", "outside-mean", "outside-percentile")
BASELINES = ("first-n", "running-median", "running-percentile", "ema")
THRESHOLD_METHODS = ("otsu", "manual", "triangle", "yen", "local")

#: Pixels outside every ROI used for a background estimate (a strided subset).
BACKGROUND_SAMPLE = 20_000

COLUMNS = ("label", "area", "bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1", "F", "F0", "dff", "triggered", "valid")
_BOOL_COLUMNS = ("triggered", "valid")


class RoiDffProcessor(Processor):
    """dF/F of every ROI of a frozen mask, with a per-ROI trigger flag."""

    name = "ROI dF/F"
    id = "roi-dff"
    category = "Measurement"
    kinds = ("image", "labels")
    output_kind = "points"
    min_inputs = 1
    max_inputs = 2

    @classmethod
    def default_params(cls) -> dict:
        return {
            "frame": UNKNOWN_FRAME,
            "mask_source": "segment",
            "reference_frames": 1,
            "labels_path": "",
            "generation": 0,
            "threshold_method": "otsu",
            "threshold_value": 0.0,
            "min_area": 20,
            "smooth_sigma": 1.0,
            "morphology_radius": 0,
            "fill_holes": True,
            "clear_border": False,
            "statistic": "mean",
            "background": "none",
            "background_value": 0.0,
            "background_percentile": 10.0,
            "baseline": "first-n",
            "baseline_frames": 20,
            "baseline_exclude_frames": 5,
            "baseline_percentile": 20.0,
            "ema_tau_frames": 50.0,
            "min_f0": 1.0,
            "threshold": 0.3,
            "min_consecutive": 1,
            "refractory_frames": 0,
            "refractory_s": 0.0,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField("frame", "text", UNKNOWN_FRAME, label="Frame", group="Output",
                       help="Frame of reference written on the points, e.g. detector:Camera"),
            ParamField("mask_source", "select", "segment", label="Mask from", options=MASK_SOURCES, group="Mask",
                       help="segment: threshold/segment the reference frame(s) once; input: the labels wired in as "
                            "the second input on the first tick; file: a saved label image (.npy/.tif)"),
            ParamField("reference_frames", "int", 1, label="Reference frames", min=1, max=10000, group="Mask",
                       help="Frames averaged into the image that is segmented (mask from segment)"),
            ParamField("labels_path", "text", "", label="Labels file", group="Mask",
                       help="Label image to load (.npy or .tif); mask from file"),
            ParamField("generation", "int", 0, label="Generation", min=0, group="Mask",
                       help="Change this number to discard the mask and every baseline and start over"),
            ParamField("threshold_method", "select", "otsu", label="Threshold", options=THRESHOLD_METHODS,
                       group="Mask"),
            ParamField("threshold_value", "float", 0.0, label="Manual value", decimals=6, group="Mask"),
            ParamField("min_area", "int", 20, label="Min area", min=1, suffix="px", group="Mask"),
            ParamField("smooth_sigma", "float", 1.0, label="Smooth sigma", min=0, max=1000, decimals=3, group="Mask"),
            ParamField("morphology_radius", "int", 0, label="Morph radius", min=0, max=9999, group="Mask"),
            ParamField("fill_holes", "bool", True, label="Fill holes", group="Mask"),
            ParamField("clear_border", "bool", False, label="Clear border", group="Mask"),
            ParamField("statistic", "select", "mean", label="F is the", options=STATISTICS, group="Signal",
                       help="mean or median of the ROI's pixels (median is slower)"),
            ParamField("background", "select", "none", label="Background", options=BACKGROUNDS, group="Signal",
                       help="Subtracted from F: none, a constant, or the mean / a percentile of the pixels "
                            "outside every ROI (from a fixed subset of up to 20000 of them)"),
            ParamField("background_value", "float", 0.0, label="Background value", decimals=3, group="Signal"),
            ParamField("background_percentile", "float", 10.0, label="Background percentile", min=0, max=100,
                       decimals=1, group="Signal"),
            ParamField("baseline", "select", "first-n", label="Baseline F0", options=BASELINES, group="Baseline",
                       help="first-n: mean of the first N frames, fixed; running-median / running-percentile: over "
                            "the last N frames excluding the most recent M; ema: moving average (starts as the "
                            "first-N mean), frozen while the ROI is above threshold"),
            ParamField("baseline_frames", "int", 20, label="Baseline frames N", min=1, max=100000, group="Baseline"),
            ParamField("baseline_exclude_frames", "int", 5, label="Exclude last M", min=0, max=100000,
                       group="Baseline", help="running baselines only: the newest M frames are not in the baseline"),
            ParamField("baseline_percentile", "float", 20.0, label="Percentile", min=0, max=100, decimals=1,
                       group="Baseline", help="running-percentile baseline"),
            ParamField("ema_tau_frames", "float", 50.0, label="EMA time constant", min=0.5, decimals=1,
                       suffix="frames", group="Baseline"),
            ParamField("min_f0", "float", 1.0, label="Min F0", min=0, decimals=3, group="Baseline",
                       help="A ROI whose baseline is at or below this is invalid (dff 0, never triggers): "
                            "the ratio of two near-zero numbers is not a signal"),
            ParamField("threshold", "float", 0.3, label="Trigger dF/F", decimals=4,
                       group="Trigger", help="A ROI is above threshold when dF/F is at least this"),
            ParamField("min_consecutive", "int", 1, label="Consecutive frames K", min=1, max=100000, group="Trigger",
                       help="Frames in a row above threshold before the ROI triggers"),
            ParamField("refractory_frames", "int", 0, label="Refractory", min=0, suffix="frames",
                       group="Trigger", help="A ROI that triggered cannot trigger again for this many frames"),
            ParamField("refractory_s", "float", 0.0, label="Refractory", min=0, decimals=2, suffix="s",
                       group="Trigger", help="... and for this many seconds of the frame clock; both must pass"),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: np.asarray(result.data).ndim == 2

    def check_inputs(self, results) -> tuple[bool, str]:
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        results = list(results or [])
        if result_kind(results[0]) != "image":
            return False, (f"{self.name} measures an image first; "
                           f"'{getattr(results[0], 'name', 'input 1')}' is {result_kind(results[0])}")
        if len(results) == 2:
            if result_kind(results[1]) != "labels":
                return False, f"{self.name}: the second input must be a labels result"
            if tuple(np.asarray(results[0].data).shape) != tuple(np.asarray(results[1].data).shape):
                return False, f"{self.name}: the labels must have the shape of the image"
        return True, ""

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        from imswitch.improcess.processors.psf_resolution._params import build_form

        spec = self.param_spec()
        widget = build_form(parent, [f for f in spec if f.type != "text"], collapsed=("Mask", "Signal", "Baseline"))
        edits = {}
        form = QtWidgets.QFormLayout()
        for f in (f for f in spec if f.type == "text"):
            edit = QtWidgets.QLineEdit(str(f.default))
            edit.setToolTip(f.help)
            form.addRow(f.label + ":", edit)
            edits[f.key] = edit
        widget.layout().insertLayout(0, form)
        base_get, base_set = widget.get_values, widget.set_values

        def get_values():
            return {**base_get(), **{k: e.text().strip() for k, e in edits.items()}}

        def set_values(values):
            base_set(values)
            for key, edit in edits.items():
                if key in (values or {}):
                    edit.setText(str(values[key]))

        widget.get_values = get_values
        widget.set_values = set_values
        return widget

    # ------------------------------------------------------------------ run

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        results = list(params.get("results", []) or []) or [result]
        image_result = results[0]
        labels_result = results[1] if len(results) > 1 else None
        defaults = self.default_params()
        p = {**defaults, **{k: v for k, v in params.items() if k in defaults}}
        state = params.get(STATE_PARAM)
        if not isinstance(state, dict):
            state = {}
        image = np.asarray(image_result.data)
        if image.ndim != 2:
            raise ProcessorInputError(f"ROI dF/F needs a (Y, X) image, got shape {image.shape}")
        meta = getattr(image_result, "metadata", None) or {}
        envelope = meta.get("frame") if isinstance(meta, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        clock = envelope.get("t")
        message = ""

        grid = json.dumps([list(image.shape), envelope.get("identity")], sort_keys=True, default=str)
        mask_key = json.dumps([p[k] for k in ("mask_source", "generation", "labels_path", "reference_frames",
                                              "threshold_method", "threshold_value", "min_area", "smooth_sigma",
                                              "morphology_radius", "fill_holes", "clear_border")], default=str)
        base_key = json.dumps([p[k] for k in ("baseline", "baseline_frames", "baseline_exclude_frames",
                                              "baseline_percentile", "statistic", "background", "background_value",
                                              "background_percentile")], default=str)
        if state.get("grid") not in (None, grid):
            message = f"frame shape or pixel grid changed (now {tuple(image.shape)}): ROIs and baselines reset"
            state.clear()
        elif state.get("mask_key") not in (None, mask_key):
            message = "mask settings changed: ROIs and baselines reset"
            state.clear()
        elif state.get("base_key") not in (None, base_key):
            message = "baseline or signal settings changed: baselines reset"
            for key in ("f0", "f0_sum", "history", "frames_seen", "streak", "last_trigger"):
                state.pop(key, None)
        state.setdefault("grid", grid)
        state.setdefault("mask_key", mask_key)
        state.setdefault("base_key", base_key)
        state["frames_in"] = state.get("frames_in", 0) + 1

        if "rois" not in state:
            status = self._build_mask(state, p, image, labels_result)
            if status:
                return self._empty(image_result, p, status, message)
        rois = state["rois"]
        if rois["n"] == 0:
            return self._empty(image_result, p, "no ROIs in the mask", message)
        F = self._measure(image, rois, p)
        row = self._update(state, p, F, clock)
        return self._result(image_result, p, rois, row, message)

    # -------------------------------------------------------------- the mask

    def _build_mask(self, state: dict, p: dict, image: np.ndarray, labels_result) -> str:
        """Freeze the mask into ``state['rois']``; the reason it is not yet, or ''."""
        source = p["mask_source"]
        if source == "segment":
            need = int(p["reference_frames"])
            acc = state.get("ref_sum")
            acc = image.astype(np.float64) if acc is None else acc + image
            state["ref_sum"], state["ref_n"] = acc, state.get("ref_n", 0) + 1
            if state["ref_n"] < need:
                return f"collecting reference, {state['ref_n']} of {need}"
            from imswitch.imcommon.algorithms.segmentation import segment_image

            analysis = segment_image(
                acc / state["ref_n"],
                threshold_method=p["threshold_method"],
                threshold_value=float(p["threshold_value"]) if p["threshold_method"] == "manual" else None,
                min_area=int(p["min_area"]), smooth_sigma=float(p["smooth_sigma"]),
                morphology_radius=int(p["morphology_radius"]), fill_holes=bool(p["fill_holes"]),
                clear_border=bool(p["clear_border"]),
            )
            labels = np.asarray(analysis.labels)
        elif source == "input":
            if labels_result is None:
                raise ProcessorInputError("mask from input needs a labels result wired in as the second input")
            labels = np.asarray(labels_result.data)
        else:
            path = str(p["labels_path"] or "")
            if not path:
                raise ProcessorInputError("mask from file needs a labels file")
            labels = _load_labels(path)
        if labels.shape != image.shape:
            raise ProcessorInputError(f"the mask has shape {labels.shape}, the frame {image.shape}")
        state.pop("ref_sum", None)
        state["rois"] = _freeze_rois(labels)
        return ""

    # ---------------------------------------------------------- measurement

    @staticmethod
    def _measure(image: np.ndarray, rois: dict, p: dict) -> np.ndarray:
        flat = image.reshape(-1)
        vals = flat[rois["sel"]].astype(np.float64, copy=False)
        n = rois["n"]
        if p["statistic"] == "median":
            sv = vals[np.lexsort((vals, rois["idx"]))]
            start, count = rois["start"], rois["area"]
            F = 0.5 * (sv[start + (count - 1) // 2] + sv[start + count // 2])
        else:
            F = np.bincount(rois["idx"], weights=vals, minlength=n) / rois["area"]
        background = p["background"]
        if background == "constant":
            F = F - float(p["background_value"])
        elif background in ("outside-mean", "outside-percentile"):
            sample = rois["bg_sel"]
            if sample.size == 0:
                raise ProcessorInputError("the ROIs cover the whole frame: no pixels left for a background")
            outside = flat[sample].astype(np.float64, copy=False)
            F = F - (float(outside.mean()) if background == "outside-mean"
                     else float(np.percentile(outside, p["background_percentile"])))
        return F

    # --------------------------------------------------- baseline + trigger

    @staticmethod
    def _update(state: dict, p: dict, F: np.ndarray, clock) -> dict:
        n = F.size
        rule = p["baseline"]
        N = max(1, int(p["baseline_frames"]))
        M = max(0, int(p["baseline_exclude_frames"])) if rule.startswith("running") else 0
        seen = state.get("frames_seen", 0)   # frames measured before this one
        state["frames_seen"] = seen + 1
        nan = np.full(n, np.nan)
        if rule in ("first-n", "ema"):
            total = N
            if seen < N:
                state["f0_sum"] = state.get("f0_sum", 0.0) + F
                if seen + 1 == N:
                    state["f0"] = state.pop("f0_sum") / N
                F0 = None
            else:
                F0 = state["f0"]
        else:
            total = N + M
            hist = state.get("history")
            if hist is None or hist.shape[1] != n:
                hist = np.full((total, n), np.nan)
                state["history"] = hist
            if seen < total:
                F0 = None
            else:
                window = hist[:N]          # the N frames before the newest M
                F0 = (np.median(window, axis=0) if rule == "running-median"
                      else np.percentile(window, p["baseline_percentile"], axis=0))
            hist[:-1] = hist[1:]
            hist[-1] = F
        if F0 is None:
            state["streak"] = np.zeros(n, dtype=np.int64)
            return {"F": F, "F0": state.get("f0", nan), "dff": np.zeros(n), "valid": np.zeros(n, dtype=bool),
                    "triggered": np.zeros(n, dtype=bool),
                    "state": f"warming up, {min(seen + 1, total)} of {total}"}
        valid = F0 > float(p["min_f0"])
        dff = np.zeros(n)
        np.divide(F - F0, F0, out=dff, where=valid)
        above = valid & (dff >= float(p["threshold"]))
        streak = state.get("streak")
        streak = np.where(above, (streak if streak is not None and streak.shape[0] == n else 0) + 1, 0)
        state["streak"] = streak
        frame_no = state["frames_in"]
        last = state.get("last_trigger")
        if last is None or last["frame"].shape[0] != n:
            last = {"frame": np.full(n, -(10**12), dtype=np.int64), "t": np.full(n, -np.inf)}
            state["last_trigger"] = last
        free = (frame_no - last["frame"]) > int(p["refractory_frames"])
        if float(p["refractory_s"]) > 0 and clock is not None:
            free &= (float(clock) - last["t"]) >= float(p["refractory_s"])
        triggered = above & (streak >= int(p["min_consecutive"])) & free
        last["frame"][triggered] = frame_no
        if clock is not None:
            last["t"][triggered] = float(clock)
        if rule == "ema":
            alpha = 1.0 - math.exp(-1.0 / max(0.5, float(p["ema_tau_frames"])))
            state["f0"] = np.where(above, F0, F0 + alpha * (F - F0))
        return {"F": F, "F0": F0, "dff": dff, "valid": valid, "triggered": triggered, "state": "running"}

    # --------------------------------------------------------------- output

    def _result(self, image_result, p, rois, row, message) -> PointsResult:
        order = np.argsort(-row["dff"], kind="stable")
        props = {
            "label": rois["label"], "area": rois["area"],
            "bbox_y0": rois["bbox"][:, 0], "bbox_x0": rois["bbox"][:, 1],
            "bbox_y1": rois["bbox"][:, 2], "bbox_x1": rois["bbox"][:, 3],
            "F": row["F"], "F0": row["F0"], "dff": row["dff"], "triggered": row["triggered"], "valid": row["valid"],
        }
        props = {k: np.asarray(v)[order] for k, v in props.items()}
        return PointsResult(
            f"{image_result.name} (roi dF/F)", rois["centroid"][order], frame=str(p["frame"] or UNKNOWN_FRAME),
            frame_index=_frame_index(image_result), axis_names=["y", "x"], properties=props,
            metadata={"operation": self.id, "state": row["state"], "message": message, "rois": int(rois["n"]),
                      "threshold": float(p["threshold"]), "baseline": p["baseline"]},
        )

    def _empty(self, image_result, p, status, message) -> PointsResult:
        props = {k: np.zeros(0, dtype=bool if k in _BOOL_COLUMNS else np.float64) for k in COLUMNS}
        return PointsResult(
            f"{image_result.name} (roi dF/F)", np.zeros((0, 2)), frame=str(p["frame"] or UNKNOWN_FRAME),
            frame_index=_frame_index(image_result), axis_names=["y", "x"], properties=props,
            metadata={"operation": self.id, "state": status, "message": message, "rois": 0},
        )


def _frame_index(result):
    value = (getattr(result, "metadata", None) or {}).get("frame_index")
    return None if value is None else int(value)


def _load_labels(path: str) -> np.ndarray:
    try:
        if path.lower().endswith(".npy"):
            return np.load(path)
        import tifffile

        return tifffile.imread(path)
    except Exception as exc:  # noqa: BLE001 - say which file
        raise ProcessorInputError(f"cannot read the labels file {path!r}: {exc}") from exc


def _freeze_rois(labels: np.ndarray) -> dict:
    """Everything the per-frame work needs about a label image, computed once."""
    labels = np.asarray(labels).astype(np.int64, copy=False)
    flat = labels.reshape(-1)
    ids = np.unique(flat[flat > 0])
    n = int(ids.size)
    if n == 0:
        return {"n": 0}
    lut = np.zeros(int(ids.max()) + 1, dtype=np.int64)
    lut[ids] = np.arange(n)
    sel = np.flatnonzero(flat > 0)           # ascending flat index, so rows ascend within a label
    idx = lut[flat[sel]]
    order = np.argsort(idx, kind="stable")
    sel, idx = sel[order], idx[order]
    area = np.bincount(idx, minlength=n).astype(np.int64)
    start = np.concatenate([[0], np.cumsum(area)[:-1]])
    rows, cols = sel // labels.shape[1], sel % labels.shape[1]
    cy = np.bincount(idx, weights=rows, minlength=n) / area
    cx = np.bincount(idx, weights=cols, minlength=n) / area
    # Box in the centroid's convention (pixel centres at integers): half a pixel beyond the outer pixels.
    y0, y1 = rows[start], rows[start + area - 1]
    x0, x1 = np.minimum.reduceat(cols, start), np.maximum.reduceat(cols, start)
    bbox = np.column_stack([y0 - 0.5, x0 - 0.5, y1 + 0.5, x1 + 0.5]).astype(np.float64)
    outside = np.flatnonzero(flat == 0)
    if outside.size > BACKGROUND_SAMPLE:
        outside = outside[:: int(math.ceil(outside.size / BACKGROUND_SAMPLE))]
    return {"n": n, "label": ids.astype(np.int64), "sel": sel, "idx": idx, "area": area, "start": start,
            "centroid": np.column_stack([cy, cx]), "bbox": bbox, "bg_sel": outside}


__all__ = ["RoiDffProcessor"]
