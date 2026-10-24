"""Mockup canvas: a patcher-style view over the real WorkflowDocument.

The spike behind docs/design/plans/improcess-workflow-editor-2.md. It draws
nodes, typed ports, cables, status badges and output thumbnails from the real
catalogue, the real document model and (optionally) a real run report.
Throwaway: Phase 1 reimplements this properly in the editor package.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.workflows.steps import Consolidate, Process, Reconstruct, Save, Source

# -- visual language -----------------------------------------------------------

CANVAS_BG = QtGui.QColor("#111921")
GRID_DOT = QtGui.QColor("#223040")
NODE_BODY = QtGui.QColor("#1c2732")
NODE_BORDER = QtGui.QColor("#2e3e4f")
NODE_SELECTED = QtGui.QColor("#9b9bff")
TEXT = QtGui.QColor("#e3e6ea")
TEXT_DIM = QtGui.QColor("#93a0ad")
STALE = QtGui.QColor("#e8913a")
ERROR = QtGui.QColor("#e2554f")
DONE = QtGui.QColor("#46b978")
RUNNING = QtGui.QColor("#5aa9e6")

KIND_COLOURS = {
    "data": "#a3adb8",        # a recording, before reconstruction
    "image": "#5aa9e6",
    "composite": "#4fd1c5",
    "labels": "#f2a541",
    "curve": "#7bd389",
    "table": "#c792ea",
    "localization": "#ff7a90",
    "rgb": "#f6e05e",
}

STEP_COLOURS = {
    "source": "#3c9a6e",
    "reconstruct": "#3b8bb5",
    "consolidate": "#3b6db5",
    "save": "#c27a38",
}

CATEGORY_COLOURS = {
    "Filters": "#6666cc",
    "Dimensions and channels": "#8a62c9",
    "Math": "#b2873a",
    "Measurement": "#c4577c",
    "Registration": "#4a9fa0",
    "Restoration": "#5c8fd6",
    "SMLM": "#d0645a",
    "Scripting": "#a8905f",
    "Segmentation": "#5ea85a",
    "Transform": "#7d7fd8",
    "Visualization": "#cf6fb4",
}

NODE_W = 204
HEADER_H = 44
PORT_R = 6.5
PORT_GAP = 24
THUMB_W, THUMB_H = 186, 104
H_GAP = 58
V_GAP = 36


def step_kind(step) -> str:
    return {Source: "source", Reconstruct: "reconstruct", Consolidate: "consolidate",
            Process: "process", Save: "save"}[type(step)]


def refs_of(step) -> list:
    if isinstance(step, Save):
        return [step.input]
    return list(getattr(step, "inputs", []) or [])


# -- what a node shows -----------------------------------------------------------

@dataclass
class NodeModel:
    id: str
    kind: str
    title: str
    colour: str
    summary: list = field(default_factory=list)
    code: list = field(default_factory=list)
    inputs: list = field(default_factory=list)      # [(label, kind)]
    outputs: list = field(default_factory=list)     # [(port, kind)]
    variadic: bool = False
    status: str = "idle"                            # idle | done | stale | running | error | queued
    thumb: QtGui.QPixmap | None = None
    footer: str = ""
    error: str = ""
    muted: bool = False


def _fmt_value(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt_value(v) for v in value)
    return str(value)


def would_write(model) -> None:
    """A Save box in a run with Write files off: it says what it would write."""
    name = model.summary[0].replace("→ ", "") if model.summary else ""
    model.summary = ["would write", name]
    model.status = "idle"
    model.footer = ""


def _shape_text(result) -> str:
    data = getattr(result, "data", None)
    shape = getattr(data, "shape", None)
    if not shape:
        return ""
    if type(result).__name__ == "DataObj":
        return " × ".join(str(n) for n in shape)
    labels = list(getattr(result, "axis_labels", []) or [])
    if len(labels) == len(shape):
        return " × ".join(f"{n}" for n in shape) + "  " + "".join(str(l)[0] for l in labels)
    return " × ".join(str(n) for n in shape)


def build_models(document, report=None, durations=None, *, status=None, errors=None) -> dict[str, NodeModel]:
    """One NodeModel per step, from the document (and what a run produced)."""
    results = dict(getattr(report, "results", {}) or {})
    durations = dict(durations or {})
    status = dict(status or {})
    errors = dict(errors or {})
    models: dict[str, NodeModel] = {}
    receipts = list(getattr(report, "receipts", []) or [])
    save_ids = [st.id for st in document.steps if isinstance(st, Save) and st.id in durations]
    saved = dict(zip(save_ids, receipts))

    def kind_of(result) -> str:
        if result is None:
            return "image"
        if type(result).__name__ == "DataObj":
            return "data"
        return str(getattr(result, "kind", "image") or "image")

    for step in document.steps:
        kind = step_kind(step)
        entry = document.entry_for(step)
        produced = {key.split(".", 1)[1]: value for key, value in results.items() if key.split(".", 1)[0] == step.id}
        if kind == "source":
            title, colour = "Source", STEP_COLOURS["source"]
        elif kind == "save":
            title, colour = f"Save · {step.fmt.upper()}", STEP_COLOURS["save"]
        elif kind in ("reconstruct", "consolidate"):
            title = entry.name if entry else step.reconstructor
            colour = STEP_COLOURS[kind]
        else:
            title = entry.name if entry else step.processor
            colour = CATEGORY_COLOURS.get(entry.category if entry else "", "#7a8794")
        model = NodeModel(id=step.id, kind=kind, title=title, colour=colour)

        # summary lines
        if kind == "source":
            path = str(getattr(step.source, "path", "") or "")
            data = produced.get("data")
            model.summary = [path.rsplit("/", 1)[-1] if path else "bound when run (drop a file)"]
            if data is not None:
                model.summary.append(_shape_text(data))
        elif kind == "save":
            receipt = saved.get(step.id)
            if receipt is not None:
                model.summary = [f"→ {receipt.primary.name}", "in results/"]
            else:
                template = step.path_template.replace("{out_dir}/", "").replace("{source_stem}", "‹file›")
                model.summary = [template.replace("{ext}", "." + step.fmt if step.fmt != "hdf5" else ".h5")]
        else:
            params = dict(getattr(step, "params", {}) or {})
            if kind == "process" and step.processor == "python":
                code = [line for line in str(params.get("code", "")).splitlines()]
                model.code = [line for line in code if line.strip() and not line.lstrip().startswith("#")][:4]
                model.summary = [f"ports: {params.get('ports', 'out')}"]
            else:
                shown = [f"{key}  {_fmt_value(value)}" for key, value in params.items()]
                model.summary = shown[:3] if shown else ["defaults"]

        # ports
        for ref in refs_of(step):
            upstream = results.get(f"{ref.step}.{ref.port}") if ref.port else None
            if upstream is None:
                for key, value in results.items():
                    if key.startswith(f"{ref.step}."):
                        upstream = value
                        break
            label = ref.port if ref.port and ref.port not in ("out", "data") else ""
            model.inputs.append((label, kind_of(upstream) if upstream is not None else "image"))
        if kind == "process" and entry is not None and entry.max_inputs is None:
            model.variadic = True
        if kind != "save":
            if produced:
                model.outputs = [(port, kind_of(value)) for port, value in produced.items()]
            elif kind == "source":
                model.outputs = [("data", "data")]
            elif kind == "process" and entry is not None:
                spec = entry.output_spec(dict(getattr(step, "params", {}) or {}))
                ports = list(getattr(spec, "ports", ()) or ()) or ["out"]
                model.outputs = [(port, "image") for port in ports]
            else:
                model.outputs = [("out", "image")]

        # run state
        if step.id in durations:
            model.status = "done"
            seconds = durations[step.id]
            shape = ""
            first = next(iter(produced.values()), None)
            if first is not None:
                shape = _shape_text(first)
            model.footer = (f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s") + (f"   {shape}" if shape else "")
            first = next(iter(produced.values()), None)
            if first is not None and len(produced) > 1 and all(kind_of(v) == "curve" for v in produced.values()):
                model.thumb = curves_thumbnail(list(produced.items()))
            elif first is not None:
                model.thumb = thumbnail(first, kind_of(first))
        model.status = status.get(step.id, model.status)
        if step.id in errors:
            model.status = "error"
            model.error = errors[step.id]
        models[step.id] = model
    return models


# -- thumbnails -------------------------------------------------------------------

def _normalise(plane: np.ndarray) -> np.ndarray:
    plane = np.asarray(plane, dtype=np.float32)
    finite = plane[np.isfinite(plane)]
    if finite.size == 0:
        return np.zeros(plane.shape, np.uint8)
    lo, hi = np.percentile(finite, [1.0, 99.7])
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((np.nan_to_num(plane, nan=lo) - lo) / (hi - lo), 0, 1)
    return (out * 255).astype(np.uint8)


def _qimage(rgb: np.ndarray) -> QtGui.QImage:
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    image = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
    return image.copy()


def image_rgb(result, kind: str) -> np.ndarray | None:
    data = getattr(result, "data", None)
    if data is None:
        return None
    array = np.asarray(data)
    if kind == "labels":
        labels = array if array.ndim == 2 else array.reshape(-1, *array.shape[-2:])[0]
        rng = np.random.default_rng(7)
        lut = rng.integers(60, 255, (int(labels.max()) + 1, 3)).astype(np.uint8)
        lut[0] = (12, 16, 22)
        return lut[labels.astype(np.int64)]
    if array.ndim == 2:
        g = _normalise(array)
        return np.stack([g, g, g], -1)
    labels = [str(l) for l in (getattr(result, "axis_labels", []) or [])]
    if array.ndim == 3 and labels and labels[0].upper().startswith("C") and array.shape[0] in (2, 3):
        chans = [_normalise(array[i]) for i in range(array.shape[0])]
        if len(chans) == 2:      # green / magenta
            return np.stack([chans[1], chans[0], chans[1]], -1)
        return np.stack(chans, -1)
    plane = array.reshape(-1, *array.shape[-2:]).max(axis=0)
    g = _normalise(plane)
    return np.stack([g, g, g], -1)


def thumbnail(result, kind: str, size=(THUMB_W, THUMB_H)) -> QtGui.QPixmap | None:
    w, h = size
    pixmap = QtGui.QPixmap(w, h)
    pixmap.fill(QtGui.QColor("#0c1218"))
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    try:
        if kind == "curve":
            y = np.asarray(getattr(result, "data", []), dtype=float)
            if y.size < 2:
                return None
            finite = np.isfinite(y)
            lo, hi = np.nanmin(y[finite]), np.nanmax(y[finite])
            if hi <= lo:
                hi = lo + 1
            pad = 10
            xs = np.linspace(pad, w - pad, y.size)
            ys = h - pad - (y - lo) / (hi - lo) * (h - 2 * pad - 8)
            path = QtGui.QPainterPath(QtCore.QPointF(xs[0], ys[0]))
            for px, py in zip(xs[1:], ys[1:]):
                path.lineTo(px, py)
            fill = QtGui.QPainterPath(path)
            fill.lineTo(xs[-1], h - pad)
            fill.lineTo(xs[0], h - pad)
            fill.closeSubpath()
            colour = QtGui.QColor(KIND_COLOURS["curve"])
            grad = QtGui.QLinearGradient(0, 0, 0, h)
            top = QtGui.QColor(colour); top.setAlpha(110)
            bottom = QtGui.QColor(colour); bottom.setAlpha(10)
            grad.setColorAt(0, top); grad.setColorAt(1, bottom)
            painter.fillPath(fill, grad)
            painter.setPen(QtGui.QPen(QtGui.QColor("#2a3846"), 1))
            painter.drawLine(pad, h - pad, w - pad, h - pad)
            painter.setPen(QtGui.QPen(colour, 2))
            painter.drawPath(path)
            painter.setPen(TEXT_DIM)
            font = painter.font(); font.setPointSizeF(7.5); painter.setFont(font)
            painter.drawText(QtCore.QRectF(pad, 2, w - 2 * pad, 12), QtCore.Qt.AlignRight, f"{hi:.3g}")
            painter.drawText(QtCore.QRectF(pad, h - pad - 12, w - 2 * pad, 12), QtCore.Qt.AlignRight, f"{lo:.3g}")
            return pixmap
        if kind == "data":
            rgb = image_rgb(type("R", (), {"data": np.asarray(result.data), "axis_labels": ["F"]})(), "image")
        else:
            rgb = image_rgb(result, kind)
        if rgb is None:
            return None
        image = _qimage(rgb).scaled(w, h, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        painter.drawImage(QtCore.QPointF((w - image.width()) / 2, (h - image.height()) / 2), image)
        return pixmap
    finally:
        painter.end()


def curves_thumbnail(pairs, size=(THUMB_W, THUMB_H)) -> QtGui.QPixmap:
    """Small multiples: one sparkline band per curve port, labelled."""
    w, h = size
    out = QtGui.QPixmap(w, h)
    out.fill(QtGui.QColor("#0c1218"))
    painter = QtGui.QPainter(out)
    band = h / len(pairs)
    for i, (port, result) in enumerate(pairs):
        piece = thumbnail(result, "curve", size=(w, int(band)))
        if piece is not None:
            painter.drawPixmap(0, int(i * band), piece)
        painter.setPen(QtGui.QColor(KIND_COLOURS["curve"]).lighter(120))
        painter.setFont(_font(7.4, bold=True))
        painter.drawText(QtCore.QRectF(8, i * band + 2, 80, 12), QtCore.Qt.AlignLeft, port)
        if i:
            painter.setPen(QtGui.QPen(QtGui.QColor("#22303c"), 1))
            painter.drawLine(0, int(i * band), w, int(i * band))
    painter.end()
    return out


# -- items --------------------------------------------------------------------------

def _font(size: float, bold: bool = False, mono: bool = False) -> QtGui.QFont:
    font = QtGui.QFont("DejaVu Sans Mono" if mono else "DejaVu Sans")
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


class NodeItem(QtWidgets.QGraphicsItem):
    def __init__(self, model: NodeModel):
        super().__init__()
        self.model = model
        self.setFlags(QtWidgets.QGraphicsItem.ItemIsSelectable | QtWidgets.QGraphicsItem.ItemIsMovable)
        self.setZValue(2)
        self._lay = self._layout()
        self._height = self._lay["height"]

    def _layout(self) -> dict:
        """Where everything in the body goes; paint and height share it."""
        m = self.model
        rows = max(len(m.inputs) + (1 if m.variadic else 0), len(m.outputs), 1)
        ports_bottom = HEADER_H + 20 + (rows - 1) * PORT_GAP + 12
        y = HEADER_H + 10
        summary_y = y
        y += 17 * len(m.summary)
        code_rect = None
        if m.code:
            top = max(y + 2, ports_bottom if len(m.outputs) > 1 else y + 2)
            code_rect = QtCore.QRectF(14, top, NODE_W - 28, 15 * len(m.code) + 6)
            y = code_rect.bottom() + 6
        y = max(y, ports_bottom)
        thumb_rect = None
        if m.thumb is not None:
            thumb_rect = QtCore.QRectF((NODE_W - THUMB_W) / 2, y + 2, THUMB_W, THUMB_H)
            y = thumb_rect.bottom() + 6
        footer_y = y if m.footer else None
        if m.footer:
            y += 18
        return {"summary_y": summary_y, "code": code_rect, "thumb": thumb_rect, "footer_y": footer_y,
                "height": y + 6, "labelled_outputs": len(m.outputs) > 1 or any(p not in ("out", "data") for p, _k in m.outputs)}

    def boundingRect(self) -> QtCore.QRectF:
        extra = 46 if self.model.error else 0
        return QtCore.QRectF(-14, -10, NODE_W + 28, self._height + 24 + extra)

    def node_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, NODE_W, self._height)

    def port_pos(self, side: str, index: int) -> QtCore.QPointF:
        y = HEADER_H + 20 + index * PORT_GAP
        x = 0 if side == "in" else NODE_W
        return self.mapToScene(QtCore.QPointF(x, y))

    def paint(self, painter: QtGui.QPainter, option, widget=None) -> None:
        m = self.model
        lay = self._lay
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = self.node_rect()
        radius = 9.0
        for i, alpha in enumerate((40, 28, 16, 8)):
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(rect.adjusted(-i, -i + 4, i, i + 6), radius + i, radius + i)
        painter.setOpacity(0.45 if m.muted else 1.0)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, NODE_BODY)
        header = QtGui.QPainterPath()
        header.addRoundedRect(QtCore.QRectF(0, 0, NODE_W, HEADER_H + radius), radius, radius)
        clip = QtGui.QPainterPath()
        clip.addRect(QtCore.QRectF(0, 0, NODE_W, HEADER_H))
        colour = QtGui.QColor(m.colour)
        grad = QtGui.QLinearGradient(0, 0, NODE_W, 0)
        grad.setColorAt(0, colour)
        grad.setColorAt(1, colour.darker(135))
        painter.fillPath(header.intersected(clip), grad)
        painter.setPen(QtGui.QColor("#ffffff"))
        painter.setFont(_font(9.8, bold=True))
        title_rect = QtCore.QRectF(12, 5, NODE_W - 50, 18)
        painter.drawText(title_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                         QtGui.QFontMetrics(painter.font()).elidedText(m.title, QtCore.Qt.ElideRight, int(title_rect.width())))
        painter.setFont(_font(7.8, mono=True))
        painter.setPen(QtGui.QColor(255, 255, 255, 170))
        painter.drawText(QtCore.QRectF(12, 24, NODE_W - 50, 14), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, m.id)
        # summary, clear of the output labels
        painter.setFont(_font(8.6))
        width = NODE_W - (96 if lay["labelled_outputs"] else 36)
        y = lay["summary_y"]
        for line in m.summary:
            painter.setPen(TEXT if not line.startswith("bound") else TEXT_DIM)
            text = QtGui.QFontMetrics(painter.font()).elidedText(line, QtCore.Qt.ElideRight, int(width))
            painter.drawText(QtCore.QRectF(18, y, width, 16), QtCore.Qt.AlignLeft, text)
            y += 17
        if lay["code"] is not None:
            box = lay["code"]
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor("#121a22"))
            painter.drawRoundedRect(box, 4, 4)
            painter.setFont(_font(7.4, mono=True))
            painter.setPen(QtGui.QColor("#c3d1a3"))
            for i, line in enumerate(m.code):
                text = QtGui.QFontMetrics(painter.font()).elidedText(line, QtCore.Qt.ElideRight, int(box.width()) - 10)
                painter.drawText(QtCore.QRectF(box.x() + 6, box.y() + 3 + 15 * i, box.width() - 10, 15), QtCore.Qt.AlignLeft, text)
        if lay["thumb"] is not None:
            trect = lay["thumb"]
            clip_path = QtGui.QPainterPath()
            clip_path.addRoundedRect(trect, 5, 5)
            painter.save()
            painter.setClipPath(clip_path)
            painter.drawPixmap(trect.toRect(), m.thumb)
            if m.status == "stale":
                painter.fillRect(trect, QtGui.QColor(17, 25, 33, 150))
                painter.setPen(STALE)
                painter.setFont(_font(8.6, bold=True))
                painter.drawText(trect, QtCore.Qt.AlignCenter, "outdated")
            painter.restore()
            painter.setPen(QtGui.QPen(QtGui.QColor("#2b3a49"), 1))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(trect, 5, 5)
        if lay["footer_y"] is not None:
            painter.setFont(_font(7.8))
            painter.setPen(TEXT_DIM)
            painter.drawText(QtCore.QRectF(14, lay["footer_y"], NODE_W - 28, 14), QtCore.Qt.AlignLeft, m.footer)
        # border (status / selection)
        border, width = NODE_BORDER, 1.2
        if m.status == "stale":
            border, width = STALE, 1.6
        elif m.status == "error":
            border, width = ERROR, 2.0
        elif m.status == "running":
            border, width = RUNNING, 2.0
        if self.isSelected():
            border, width = NODE_SELECTED, 2.2
        pen = QtGui.QPen(border, width)
        if m.status == "stale" and not self.isSelected():
            pen.setStyle(QtCore.Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        # ports
        for i, (_label, kind) in enumerate(m.inputs):
            self._paint_port(painter, QtCore.QPointF(0, HEADER_H + 20 + i * PORT_GAP), kind, "", "in")
        if m.variadic:
            p = QtCore.QPointF(0, HEADER_H + 20 + len(m.inputs) * PORT_GAP)
            painter.setPen(QtGui.QPen(QtGui.QColor("#6c7c8c"), 1.4, QtCore.Qt.DashLine))
            painter.setBrush(NODE_BODY)
            painter.drawEllipse(p, PORT_R - 1, PORT_R - 1)
        for i, (label, kind) in enumerate(m.outputs):
            shown = label if lay["labelled_outputs"] else ""
            self._paint_port(painter, QtCore.QPointF(NODE_W, HEADER_H + 20 + i * PORT_GAP), kind, shown, "out")
        self._paint_badge(painter, QtCore.QPointF(NODE_W - 20, HEADER_H / 2))
        painter.setOpacity(1.0)
        if m.error:
            box = QtCore.QRectF(0, self._height + 8, NODE_W, 38)
            painter.setPen(QtGui.QPen(ERROR, 1.2))
            painter.setBrush(QtGui.QColor(70, 22, 24, 230))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QtGui.QColor("#ffd6d4"))
            painter.setFont(_font(8.0))
            painter.drawText(box.adjusted(8, 3, -8, -3), QtCore.Qt.AlignVCenter | QtCore.Qt.TextWordWrap, m.error)

    def _paint_port(self, painter, p, kind, label, side):
        colour = QtGui.QColor(KIND_COLOURS.get(kind, "#a3adb8"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#0b1117"), 2.2))
        painter.setBrush(colour)
        painter.drawEllipse(p, PORT_R, PORT_R)
        if label:
            painter.setFont(_font(7.8, bold=True))
            painter.setPen(colour.lighter(115))
            if side == "out":
                painter.drawText(QtCore.QRectF(p.x() - 74, p.y() - 8, 62, 16), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, label)
            else:
                painter.drawText(QtCore.QRectF(p.x() + 11, p.y() - 8, 62, 16), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, label)

    def _paint_badge(self, painter, centre):
        status = self.model.status
        if status == "idle":
            return
        colour = {"done": DONE, "stale": STALE, "error": ERROR, "running": RUNNING, "queued": TEXT_DIM}[status]
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 90), 1))
        painter.setBrush(colour if status != "running" else QtGui.QColor("#13202b"))
        painter.drawEllipse(centre, 9, 9)
        if status == "running":
            painter.setPen(QtGui.QPen(RUNNING, 2.6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            painter.drawArc(QtCore.QRectF(centre.x() - 6, centre.y() - 6, 12, 12), 90 * 16, -250 * 16)
            return
        glyph = {"done": "✓", "stale": "↻", "error": "✕", "queued": "…"}[status]
        painter.setPen(QtGui.QColor("#0d1419") if status != "error" else QtGui.QColor("#ffffff"))
        painter.setFont(_font(9.5, bold=True))
        painter.drawText(QtCore.QRectF(centre.x() - 9, centre.y() - 9.5, 18, 18), QtCore.Qt.AlignCenter, glyph)


class EdgeItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, start: QtCore.QPointF, end: QtCore.QPointF, kind: str, *, dim=False, dashed=False):
        super().__init__()
        self.setZValue(1)
        dx = max(60.0, abs(end.x() - start.x()) * 0.5)
        path = QtGui.QPainterPath(start)
        path.cubicTo(start + QtCore.QPointF(dx, 0), end - QtCore.QPointF(dx, 0), end)
        self.setPath(path)
        colour = QtGui.QColor(KIND_COLOURS.get(kind, "#a3adb8"))
        if dim:
            colour.setAlpha(120)
        self._colour = colour
        self._dashed = dashed

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(5, 8, 12, 200), 6.0, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        painter.drawPath(self.path())
        pen = QtGui.QPen(self._colour, 2.6, QtCore.Qt.DashLine if self._dashed else QtCore.Qt.SolidLine, QtCore.Qt.RoundCap)
        painter.setPen(pen)
        painter.drawPath(self.path())


class CanvasView(QtWidgets.QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform | QtGui.QPainter.TextAntialiasing)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setDragMode(QtWidgets.QGraphicsView.RubberBandDrag)

        self._legend = QtWidgets.QLabel(self)
        self._legend.setStyleSheet("background: rgba(23,33,43,225); border: 1px solid #2a3846; border-radius: 9px; color: #c9d1d9; padding: 5px 10px; font-size: 8.5pt;")
        parts = [f"<span style='color:{KIND_COLOURS[k]}; font-size:11pt'>●</span>&nbsp;{label}"
                 for k, label in (("data", "recording"), ("image", "image"), ("labels", "labels"), ("curve", "curve"), ("table", "table"))]
        self._legend.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        self._legend.adjustSize()
        self._zoom = QtWidgets.QLabel(self)
        self._zoom.setTextFormat(QtCore.Qt.RichText)
        self._zoom.setStyleSheet("background: rgba(23,33,43,225); border: 1px solid #2a3846; border-radius: 9px; color: #c9d1d9; padding: 5px 10px; font-size: 8.5pt;")
        self._zoom.setText("−&nbsp;&nbsp; 100% &nbsp;&nbsp;+ &nbsp;&nbsp;│&nbsp;&nbsp; ⊡ Fit &nbsp;&nbsp;│&nbsp;&nbsp; ⇱ Tidy up")

    def set_zoom_text(self):
        self._zoom.setText(f"−&nbsp;&nbsp; {self.transform().m11() * 100:.0f}% &nbsp;&nbsp;+ &nbsp;&nbsp;│&nbsp;&nbsp; ⊡ Fit &nbsp;&nbsp;│&nbsp;&nbsp; ⇱ Tidy up")
        self._zoom.adjustSize()
        self._place_overlays()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_overlays()

    def _place_overlays(self):
        self._legend.adjustSize()
        self._zoom.adjustSize()
        self._legend.move(14, self.height() - self._legend.height() - 14)
        self._zoom.move(self.width() - self._zoom.width() - 14, self.height() - self._zoom.height() - 14)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, CANVAS_BG)
        step = 26
        left = int(math.floor(rect.left() / step)) * step
        top = int(math.floor(rect.top() / step)) * step
        painter.setPen(QtGui.QPen(GRID_DOT, 1.6))
        y = top
        while y < rect.bottom():
            x = left
            while x < rect.right():
                painter.drawPoint(QtCore.QPointF(x, y))
                x += step
            y += step


# -- layout ---------------------------------------------------------------------------

def layered_positions(document, items: dict[str, NodeItem]) -> dict[str, QtCore.QPointF]:
    """Longest-path layers left to right; inside a layer, by the inputs' order."""
    rank: dict[str, int] = {}
    for step in document.steps:
        ups = [rank[r.step] for r in refs_of(step) if r.step in rank]
        rank[step.id] = (max(ups) + 1) if ups else 0
    layers: dict[int, list] = {}
    for step in document.steps:
        layers.setdefault(rank[step.id], []).append(step.id)
    positions: dict[str, QtCore.QPointF] = {}
    order_y: dict[str, float] = {}
    for r in sorted(layers):
        ids = layers[r]

        def bary(sid):
            ups = [order_y[x.step] for x in refs_of(document.step(sid)) if x.step in order_y]
            return sum(ups) / len(ups) if ups else 0.0
        ids.sort(key=bary)
        heights = [items[i]._height for i in ids]
        total = sum(heights) + V_GAP * (len(ids) - 1)
        centre = sum(bary(i) for i in ids) / len(ids) if r else 0.0
        y = centre - total / 2
        for sid, h in zip(ids, heights):
            positions[sid] = QtCore.QPointF(r * (NODE_W + H_GAP), y)
            order_y[sid] = y + h / 2
            y += h + V_GAP
    return positions


def populate(scene, document, models: dict[str, NodeModel], *, positions=None, dim_edges_from=()):
    items = {sid: NodeItem(model) for sid, model in models.items()}
    positions = positions or layered_positions(document, items)
    for sid, item in items.items():
        item.setPos(positions[sid])
        scene.addItem(item)
    for step in document.steps:
        target = items[step.id]
        for index, ref in enumerate(refs_of(step)):
            source = items.get(ref.step)
            if source is None:
                continue
            ports = [p for p, _k in source.model.outputs]
            port = ref.port or (ports[0] if ports else "out")
            out_index = ports.index(port) if port in ports else 0
            kind = source.model.outputs[out_index][1] if source.model.outputs else "image"
            stale = target.model.status == "stale"
            edge = EdgeItem(source.port_pos("out", out_index), target.port_pos("in", index), kind,
                            dim=stale or step.id in dim_edges_from)
            scene.addItem(edge)
    return items


# -- list mode: the same document as a pipeline of cards ------------------------------------

CARD_W = 760
CARD_H = 92
CARD_GAP = 14
GUTTER = 70


class CardItem(QtWidgets.QGraphicsItem):
    """One step as a row: lane dot in the gutter, colour bar, title, summary, thumbnail, state."""

    def __init__(self, model: NodeModel, lane_x: float):
        super().__init__()
        self.model = model
        self.lane_x = lane_x
        self.setFlags(QtWidgets.QGraphicsItem.ItemIsSelectable)
        extra = 30 if model.error else 0
        self._h = CARD_H + extra

    def boundingRect(self):
        return QtCore.QRectF(-GUTTER - 10, -6, CARD_W + GUTTER + 20, self._h + 14)

    def paint(self, painter, option, widget=None):
        m = self.model
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(0, 0, CARD_W, self._h)
        painter.setPen(QtCore.Qt.NoPen)
        for i, alpha in enumerate((34, 20, 10)):
            painter.setBrush(QtGui.QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(rect.adjusted(-i, -i + 3, i, i + 5), 9 + i, 9 + i)
        painter.setOpacity(0.5 if m.status == "queued" else 1.0)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.fillPath(path, NODE_BODY)
        bar = QtGui.QPainterPath()
        bar.addRoundedRect(QtCore.QRectF(0, 0, 14, self._h), 9, 9)
        clip = QtGui.QPainterPath()
        clip.addRect(QtCore.QRectF(0, 0, 7, self._h))
        painter.fillPath(bar.intersected(clip), QtGui.QColor(m.colour))
        border, width = NODE_BORDER, 1.2
        if m.status == "error":
            border, width = ERROR, 2.0
        if self.isSelected():
            border, width = NODE_SELECTED, 2.2
        painter.setPen(QtGui.QPen(border, width))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, 9, 9)
        # title / id / summary
        painter.setPen(TEXT)
        painter.setFont(_font(10.4, bold=True))
        painter.drawText(QtCore.QRectF(24, 10, 300, 20), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, m.title)
        painter.setFont(_font(8.2, mono=True))
        painter.setPen(TEXT_DIM)
        painter.drawText(QtCore.QRectF(24, 31, 300, 16), QtCore.Qt.AlignLeft, m.id)
        painter.setFont(_font(8.8))
        painter.setPen(QtGui.QColor("#c9d1d9"))
        lines = (m.code[:2] if m.code else []) or m.summary[:2]
        if m.code:
            painter.setFont(_font(8.0, mono=True))
            painter.setPen(QtGui.QColor("#c3d1a3"))
        for i, line in enumerate(lines):
            text = QtGui.QFontMetrics(painter.font()).elidedText(line, QtCore.Qt.ElideRight, 330)
            painter.drawText(QtCore.QRectF(24, 50 + 17 * i, 340, 16), QtCore.Qt.AlignLeft, text)
        # inputs, as words
        if m.inputs and self.data(0):
            painter.setFont(_font(8.2))
            painter.setPen(TEXT_DIM)
            painter.drawText(QtCore.QRectF(370, 12, 200, 16), QtCore.Qt.AlignLeft, "← " + self.data(0))
        # output ports as chips
        x = 370
        for port, kind in m.outputs:
            colour = QtGui.QColor(KIND_COLOURS.get(kind, "#a3adb8"))
            label = f"{port}  ·  {kind}"
            painter.setFont(_font(8.0, bold=True))
            w = QtGui.QFontMetrics(painter.font()).horizontalAdvance(label) + 18
            chip = QtCore.QRectF(x, 38, w, 20)
            fill = QtGui.QColor(colour); fill.setAlpha(38)
            painter.setPen(QtGui.QPen(colour, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(chip, 10, 10)
            painter.setPen(colour.lighter(120))
            painter.drawText(chip, QtCore.Qt.AlignCenter, label)
            x += w + 6
        if m.footer:
            painter.setFont(_font(8.0))
            painter.setPen(TEXT_DIM)
            painter.drawText(QtCore.QRectF(370, 64, 230, 16), QtCore.Qt.AlignLeft, m.footer)
        # thumbnail
        if m.thumb is not None:
            trect = QtCore.QRectF(CARD_W - 150, 8, 138, 76)
            clip_path = QtGui.QPainterPath()
            clip_path.addRoundedRect(trect, 5, 5)
            painter.save()
            painter.setClipPath(clip_path)
            painter.drawPixmap(trect.toRect(), m.thumb)
            painter.restore()
            painter.setPen(QtGui.QPen(QtGui.QColor("#2b3a49"), 1))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(trect, 5, 5)
        elif m.status == "queued":
            painter.setFont(_font(8.4))
            painter.setPen(TEXT_DIM)
            painter.drawText(QtCore.QRectF(CARD_W - 190, 30, 170, 20), QtCore.Qt.AlignRight, "not run: waits for ‘script’")
        if m.error:
            box = QtCore.QRectF(16, CARD_H - 4, CARD_W - 32, 28)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(70, 22, 24, 230))
            painter.drawRoundedRect(box, 5, 5)
            painter.setPen(QtGui.QColor("#ffd6d4"))
            painter.setFont(_font(8.6))
            painter.drawText(box.adjusted(10, 0, -10, 0), QtCore.Qt.AlignVCenter, "✕  " + m.error)
        painter.setOpacity(1.0)
        # lane dot + badge in the gutter
        centre = QtCore.QPointF(-GUTTER + self.lane_x, CARD_H / 2)
        colour = {"done": DONE, "error": ERROR, "stale": STALE, "running": RUNNING}.get(m.status, QtGui.QColor("#4a5a6a"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#0b1117"), 3))
        painter.setBrush(colour)
        painter.drawEllipse(centre, 10, 10)
        glyph = {"done": "✓", "error": "✕", "stale": "↻"}.get(m.status, "")
        if glyph:
            painter.setPen(QtGui.QColor("#0d1419") if m.status != "error" else QtGui.QColor("#ffffff"))
            painter.setFont(_font(9.5, bold=True))
            painter.drawText(QtCore.QRectF(centre.x() - 10, centre.y() - 10.5, 20, 20), QtCore.Qt.AlignCenter, glyph)


def populate_list(scene, document, models):
    """Cards top to bottom in file order; the gutter draws who feeds whom, git-graph style."""
    lanes: dict[str, int] = {}
    items = {}
    y = 0.0
    for index, step in enumerate(document.steps):
        refs = refs_of(step)
        lane = 0
        if refs and refs[0].step in lanes:
            lane = lanes[refs[0].step]
            consumers = [s for s in document.steps[:index] if any(r.step == refs[0].step for r in refs_of(s))]
            lane += len(consumers)
        lanes[step.id] = lane
        item = CardItem(models[step.id], 22 + lane * 20)
        item.setData(0, ", ".join(str(r) for r in refs))
        item.setPos(0, y)
        scene.addItem(item)
        items[step.id] = (item, y)
        y += item._h + CARD_GAP
    # gutter lanes
    for step in document.steps:
        target, ty = items[step.id]
        for ref in refs_of(step):
            if ref.step not in items:
                continue
            source, sy = items[ref.step]
            kind = next((k for p, k in source.model.outputs if p == (ref.port or p)), "image")
            colour = QtGui.QColor(KIND_COLOURS.get(kind, "#a3adb8"))
            x1 = -GUTTER + source.lane_x
            x2 = -GUTTER + target.lane_x
            path = QtGui.QPainterPath(QtCore.QPointF(x1, sy + CARD_H / 2))
            mid = ty + CARD_H / 2 - 30
            path.lineTo(x1, max(sy + CARD_H / 2, mid - 10))
            path.cubicTo(QtCore.QPointF(x1, mid + 10), QtCore.QPointF(x2, mid + 10), QtCore.QPointF(x2, ty + CARD_H / 2))
            line = QtWidgets.QGraphicsPathItem(path)
            line.setPen(QtGui.QPen(colour, 3, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            line.setZValue(-1)
            scene.addItem(line)
    return {sid: pair[0] for sid, pair in items.items()}
