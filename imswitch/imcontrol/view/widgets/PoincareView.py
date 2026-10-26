"""A small Poincaré sphere for live polarisation readings (no OpenGL).

Draws an orthographic projection of the unit sphere with QPainter: the
outline, the equator and the S1 / S3 meridians, the three axes, the current
state as a dot at DOP·(s1, s2, s3) -- the Stokes vector's length is the
degree of polarisation -- and a fading trail of the last readings. Drag to
rotate. Pure 2D painting, so it works wherever Qt does (offscreen tests
included) and costs nothing while nothing changes.
"""
from __future__ import annotations

import math
from collections import deque

from qtpy import QtCore, QtGui, QtWidgets


def stokes_from_angles(azimuth_rad: float, ellipticity_rad: float):
    """Normalised (s1, s2, s3) of a polarisation ellipse."""
    c = math.cos(2 * ellipticity_rad)
    return (c * math.cos(2 * azimuth_rad), c * math.sin(2 * azimuth_rad),
            math.sin(2 * ellipticity_rad))


class PoincareView(QtWidgets.QWidget):
    """The sphere; ``setState(s1, s2, s3, dop)`` moves the dot."""

    TRAIL = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._state = None                  # (x, y, z) scaled by DOP
        self._trail = deque(maxlen=self.TRAIL)
        # View angles: azimuth about S3, elevation above the S1-S2 plane.
        self._view_az = math.radians(-35.0)
        self._view_el = math.radians(25.0)
        self._drag_from = None
        self.setToolTip('Poincaré sphere: the dot is the current state, at a radius equal '
                        'to the degree of polarisation. Drag to rotate.')

    # -------------------------------------------------------------- state
    def setState(self, s1: float, s2: float, s3: float, dop: float = 1.0) -> None:
        try:
            r = max(0.0, min(1.0, float(dop)))
            point = (float(s1) * r, float(s2) * r, float(s3) * r)
        except (TypeError, ValueError):
            return
        if any(math.isnan(v) for v in point):
            return
        self._state = point
        self._trail.append(point)
        self.update()

    def clearState(self) -> None:
        self._state = None
        self._trail.clear()
        self.update()

    @property
    def state(self):
        return self._state

    # ---------------------------------------------------------- geometry
    def _project(self, x, y, z):
        """Rotate by the view angles; return (screen x, screen y, depth)."""
        ca, sa = math.cos(self._view_az), math.sin(self._view_az)
        ce, se = math.cos(self._view_el), math.sin(self._view_el)
        x1, y1 = x * ca - y * sa, x * sa + y * ca       # about S3
        y2, z2 = y1 * ce - z * se, y1 * se + z * ce       # tilt
        # Screen: S1-ish to the right, S3 up; depth y2 towards the viewer.
        return x1, z2, y2

    def _circle(self, axis: int, steps: int = 72):
        """Points of the great circle perpendicular to ``axis``."""
        points = []
        for i in range(steps + 1):
            t = 2 * math.pi * i / steps
            c, s = math.cos(t), math.sin(t)
            if axis == 2:
                points.append((c, s, 0.0))
            elif axis == 1:
                points.append((c, 0.0, s))
            else:
                points.append((0.0, c, s))
        return points

    # ----------------------------------------------------------- painting
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        radius = 0.42 * min(w, h)
        cx, cy = w / 2, h / 2
        palette = self.palette()
        fg = palette.color(QtGui.QPalette.WindowText)
        dim = QtGui.QColor(fg)
        dim.setAlpha(70)

        def to_screen(p):
            sx, sy, depth = self._project(*p)
            return QtCore.QPointF(cx + sx * radius, cy - sy * radius), depth

        # Outline
        painter.setPen(QtGui.QPen(dim, 1.2))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)

        # Great circles: front half solid, back half faint.
        for axis in (0, 1, 2):
            points = self._circle(axis)
            for a, b in zip(points, points[1:]):
                pa, da = to_screen(a)
                pb, db = to_screen(b)
                front = (da + db) / 2 >= 0
                pen = QtGui.QPen(dim if front else QtGui.QColor(fg.red(), fg.green(), fg.blue(), 25),
                                 1.0 if front else 0.8)
                painter.setPen(pen)
                painter.drawLine(pa, pb)

        # Axes with labels.
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        for label, vector in (('S1', (1, 0, 0)), ('S2', (0, 1, 0)), ('S3', (0, 0, 1))):
            origin, _ = to_screen((0, 0, 0))
            tip, depth = to_screen(tuple(1.15 * v for v in vector))
            painter.setPen(QtGui.QPen(fg if depth >= 0 else dim, 1.0))
            painter.drawLine(origin, tip)
            painter.drawText(tip + QtCore.QPointF(3, 3), label)

        # Trail, fading towards the oldest reading.
        trail = list(self._trail)
        n = len(trail)
        for index, point in enumerate(trail[:-1]):
            pos, depth = to_screen(point)
            alpha = int(30 + 150 * (index + 1) / max(1, n))
            color = QtGui.QColor(230, 120, 20, alpha if depth >= 0 else alpha // 3)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(pos, 2.5, 2.5)

        # The current state.
        if self._state is not None:
            pos, depth = to_screen(self._state)
            color = QtGui.QColor(230, 120, 20) if depth >= 0 else QtGui.QColor(230, 120, 20, 110)
            painter.setPen(QtGui.QPen(fg, 1.0))
            painter.setBrush(color)
            painter.drawEllipse(pos, 5.0, 5.0)
        painter.end()

    # ------------------------------------------------------------ rotation
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_from = event.pos()

    def mouseMoveEvent(self, event):
        if self._drag_from is None:
            return
        delta = event.pos() - self._drag_from
        self._drag_from = event.pos()
        self._view_az += delta.x() * 0.01
        self._view_el = max(-math.pi / 2, min(math.pi / 2, self._view_el + delta.y() * 0.01))
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag_from = None
