"""Previews to pixmaps, on the GUI thread.

The worker hands over plain data (:class:`~imswitch.improcess.model.workfloweditor.previews.Preview`);
this turns it into what a box or the inspector draws: the image, a sparkline
for a curve, the first rows for a table.
"""

from __future__ import annotations

import numpy as np
from qtpy import QtCore, QtGui

from .canvas import style as st

_BACKGROUND = "#0c1218"


def image_from_rgb(rgb: np.ndarray) -> QtGui.QImage:
    rgb = np.ascontiguousarray(rgb[..., :3].astype(np.uint8))
    height, width, _ = rgb.shape
    return QtGui.QImage(rgb.data, width, height, 3 * width, QtGui.QImage.Format_RGB888).copy()


def preview_pixmap(preview, size=(st.THUMB_W, st.THUMB_H)) -> QtGui.QPixmap | None:
    """What a box shows for one output; ``None`` when there is nothing to draw."""
    if preview is None:
        return None
    width, height = size
    if preview.rgb is not None:
        image = image_from_rgb(preview.rgb)
        return QtGui.QPixmap.fromImage(image)
    if preview.curve is not None and len(preview.curve) >= 2:
        return curve_pixmap(preview.curve, (width, height))
    if preview.kind in ("table", "localization") and (preview.rows or preview.row_count):
        return table_pixmap(preview, (width, height))
    return None


def curve_pixmap(values, size, *, label: str = "") -> QtGui.QPixmap:
    width, height = size
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtGui.QColor(_BACKGROUND))
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    try:
        y = np.asarray(values, dtype=float)
        finite = np.isfinite(y)
        if finite.sum() < 2:
            return pixmap
        lo, hi = float(np.nanmin(y[finite])), float(np.nanmax(y[finite]))
        if hi <= lo:
            hi = lo + 1.0
        pad = 8
        xs = np.linspace(pad, width - pad, y.size)
        ys = height - pad - (np.nan_to_num(y, nan=lo) - lo) / (hi - lo) * (height - 2 * pad - 8)
        path = QtGui.QPainterPath(QtCore.QPointF(xs[0], ys[0]))
        for px, py in zip(xs[1:], ys[1:]):
            path.lineTo(px, py)
        fill = QtGui.QPainterPath(path)
        fill.lineTo(xs[-1], height - pad)
        fill.lineTo(xs[0], height - pad)
        fill.closeSubpath()
        colour = st.kind_colour("curve")
        gradient = QtGui.QLinearGradient(0, 0, 0, height)
        top = QtGui.QColor(colour)
        top.setAlpha(110)
        bottom = QtGui.QColor(colour)
        bottom.setAlpha(10)
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, bottom)
        painter.fillPath(fill, gradient)
        painter.setPen(QtGui.QPen(colour, 2))
        painter.drawPath(path)
        painter.setPen(st.colour(st.TEXT_DIM))
        painter.setFont(st.font(7.2))
        painter.drawText(QtCore.QRectF(pad, 1, width - 2 * pad, 12), QtCore.Qt.AlignRight, f"{hi:.3g}")
        painter.drawText(QtCore.QRectF(pad, height - pad - 12, width - 2 * pad, 12), QtCore.Qt.AlignRight, f"{lo:.3g}")
        if label:
            painter.setPen(colour.lighter(120))
            painter.setFont(st.font(7.4, bold=True))
            painter.drawText(QtCore.QRectF(pad, 1, width / 2, 12), QtCore.Qt.AlignLeft, label)
    finally:
        painter.end()
    return pixmap


def curves_pixmap(pairs, size=(st.THUMB_W, st.THUMB_H)) -> QtGui.QPixmap:
    """Small multiples: one labelled sparkline band per curve output."""
    width, height = size
    out = QtGui.QPixmap(width, height)
    out.fill(QtGui.QColor(_BACKGROUND))
    painter = QtGui.QPainter(out)
    band = height / max(len(pairs), 1)
    for index, (port, preview) in enumerate(pairs):
        piece = curve_pixmap(preview.curve, (width, int(band)), label=port)
        painter.drawPixmap(0, int(index * band), piece)
        if index:
            painter.setPen(QtGui.QPen(QtGui.QColor("#22303c"), 1))
            painter.drawLine(0, int(index * band), width, int(index * band))
    painter.end()
    return out


def table_pixmap(preview, size) -> QtGui.QPixmap:
    width, height = size
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtGui.QColor(_BACKGROUND))
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setFont(st.font(7.2, mono=True))
        colour = st.kind_colour(preview.kind if preview.kind in st.KIND_COLOURS else "table")
        columns = list(preview.columns[:3])
        y = 4
        if columns:
            painter.setPen(colour.lighter(120))
            painter.drawText(QtCore.QRectF(6, y, width - 12, 13), QtCore.Qt.AlignLeft,
                             "  ".join(c[:9].ljust(9) for c in columns))
            y += 15
        painter.setPen(st.colour(st.TEXT))
        for row in preview.rows[:5]:
            painter.drawText(QtCore.QRectF(6, y, width - 12, 13), QtCore.Qt.AlignLeft,
                             "  ".join(str(cell)[:9].ljust(9) for cell in row[:3]))
            y += 14
        painter.setPen(st.colour(st.TEXT_DIM))
        painter.drawText(QtCore.QRectF(6, height - 16, width - 12, 13), QtCore.Qt.AlignRight,
                         preview.describe())
    finally:
        painter.end()
    return pixmap


def box_pixmap(previews: dict) -> QtGui.QPixmap | None:
    """The thumbnail of a box: its curves as small multiples, else its first output."""
    if not previews:
        return None
    curves = [(port, p) for port, p in previews.items() if p is not None and p.curve is not None and len(p.curve) >= 2]
    if len(curves) > 1 and len(curves) == len(previews):
        return curves_pixmap(curves)
    first = next(iter(previews.values()))
    return preview_pixmap(first)


__all__ = ["box_pixmap", "curve_pixmap", "curves_pixmap", "image_from_rgb", "preview_pixmap", "table_pixmap"]
