"""The smart pipeline's ROIs on the live view: outlines with their ids, and a
layer to draw new ones (napari layers of their own).

View only: it shows what the controller gives it, in *displayed* pixels (the
controller maps the camera's pixels through the live view's display
transform), and hands back what the user did -- a shape drawn (its kind and
vertices, in displayed pixels), a ROI clicked (its id). The layers take the
live layer's scale and translation, so they sit on the camera image however
the view is zoomed.
"""

from __future__ import annotations

import numpy as np
from qtpy import QtCore


class RoiOverlay(QtCore.QObject):
    """``viewer`` is the napari viewer (or anything with ``layers``,
    ``add_labels``, ``add_points`` and ``add_shapes``)."""

    sigShapeDrawn = QtCore.Signal(str, object)     # (kind, vertices [[row, col], ...] in displayed pixels)
    sigRoiClicked = QtCore.Signal(int)             # the id under the click (0: none)

    LABELS_NAME = 'Pipeline ROIs'
    IDS_NAME = 'Pipeline ROI ids'
    DRAW_NAME = 'Draw pipeline ROI'
    _SELECTED = '#ff4fd8'
    _NORMAL = '#ffd400'

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self._viewer = viewer
        self._labels = None
        self._ids = None
        self._draw = None
        self._updating = False
        self._pending = False
        self._liveName = None

    # -- the live layer's placement -----------------------------------------------

    def _placement(self):
        layers = getattr(self._viewer, 'layers', [])
        layer = None
        present = [getattr(lay, 'name', None) for lay in layers]
        # the image view names a detector's live layer "Live: <detector>"
        for name in (f'Live: {self._liveName}', self._liveName):
            if self._liveName is not None and name in present:
                layer = layers[name]
                break
        if layer is None:
            return {}
        scale = tuple(float(v) for v in tuple(layer.scale)[-2:])
        translate = tuple(float(v) for v in tuple(layer.translate)[-2:])
        return {'scale': scale, 'translate': translate}

    def _place(self, layer):
        placement = self._placement()
        if placement:
            layer.scale = placement['scale']
            layer.translate = placement['translate']

    def _keep(self, layer):
        return layer is not None and layer in self._viewer.layers

    # -- what is shown ------------------------------------------------------------

    def show(self, liveLayerName, outline, centres, ids, selected=()):
        """The outlines (a label image in displayed pixels, 0 = nothing) and
        the id of every ROI at its centre (displayed (row, col))."""
        self._liveName = liveLayerName
        selected = set(int(i) for i in selected)
        if not self._keep(self._labels):
            self._labels = self._viewer.add_labels(np.asarray(outline), name=self.LABELS_NAME, opacity=0.9)
            try:
                self._labels.mouse_drag_callbacks.append(self._onClick)
            except AttributeError:
                pass
        else:
            self._labels.data = np.asarray(outline)
        self._place(self._labels)
        centres = np.asarray(centres, dtype=float).reshape(-1, 2)
        colours = [self._SELECTED if int(i) in selected else self._NORMAL for i in ids]
        text = {'string': [str(int(i)) for i in ids], 'color': colours or self._NORMAL, 'size': 10,
                'anchor': 'center'}
        if not self._keep(self._ids):
            self._ids = self._viewer.add_points(centres, name=self.IDS_NAME, size=0.1, face_color='transparent',
                                                border_color='transparent', text=text)
        else:
            self._ids.data = centres
            self._ids.text = text
        self._place(self._ids)

    def hide(self):
        for layer in (self._labels, self._ids, self._draw):
            if self._keep(layer):
                self._viewer.layers.remove(layer)
        self._labels = self._ids = self._draw = None

    def visible(self):
        return self._keep(self._labels)

    # -- drawing ------------------------------------------------------------------

    def startDrawing(self, kind):
        """Draw one ``rectangle``, ``ellipse`` or ``polygon`` on the live view."""
        if not self._keep(self._draw):
            self._draw = self._viewer.add_shapes(name=self.DRAW_NAME, edge_color='#00e5ff',
                                                 face_color=[0.0, 0.9, 1.0, 0.1])
            self._draw.events.data.connect(self._onData)
        self._place(self._draw)
        self._viewer.layers.selection.active = self._draw
        self._draw.mode = {'rectangle': 'add_rectangle', 'ellipse': 'add_ellipse',
                           'polygon': 'add_polygon'}[kind]

    def _onData(self, event=None):
        if self._updating:
            return
        action = getattr(event, 'action', None)
        if getattr(action, 'value', action) not in (None, 'added'):
            return
        if not self._pending:
            self._pending = True
            QtCore.QTimer.singleShot(0, self._emitDrawn)

    def _emitDrawn(self):
        self._pending = False
        layer = self._draw
        if not self._keep(layer) or not len(layer.data):
            return
        vertices = np.asarray(layer.data[-1], dtype=float)[:, -2:]
        kind = str(layer.shape_type[-1])
        self._updating = True
        try:
            layer.data = []           # it is a ROI now: the outline shows it
            layer.mode = 'pan_zoom'
        finally:
            self._updating = False
        # data coordinates of the draw layer are displayed pixels (same scale as the live layer)
        self.sigShapeDrawn.emit(kind, vertices.tolist())

    def _onClick(self, layer, event):
        try:
            value = layer.get_value(event.position, world=True)
        except Exception:
            return
        self.sigRoiClicked.emit(int(value or 0))


__all__ = ['RoiOverlay']
