"""The ROI drawing tool and a "viewer" for the active image window.

The ROI Manager, ROI statistics and Profile panels were written against a napari
viewer: they draw through :class:`~imswitch.imcommon.view.guitools.viewer_tools.ViewerToolService`
and read the image through ``viewer.layers`` / ``viewer.dims``. In window mode
there is no such viewer, only one :class:`ImageWindow` per result, so this
module gives the panels the same two things backed by whichever window is
active:

- :class:`WindowToolService` -- the shared drawing tool, with the same public
  API and token semantics (it *is* a ``ViewerToolService``), drawing
  pyqtgraph ROIs on the active window;
- :class:`ActiveWindowViewer` -- a duck type covering only what those panels
  touch (``dims``, ``layers`` and their events), whose one image layer is the
  active window's result (:class:`WindowImageLayer`).

Vertex format (what ``WindowToolService.shapes`` returns)
---------------------------------------------------------
Exactly what the napari service returns, because the panels' ROI model reads
it unchanged (``roi_from_shape``): one ``(N, 2)`` float64 array per shape, in
``(row, col)`` order, in *world* coordinates -- the window's calibrated view
coordinates, which a layer maps back to pixels with ``world_to_data`` just as
napari's does. napari's scratch layer is two-dimensional (``ndim=2``), so the
vertices carry no slider axes; the plane a shape was drawn on is the plane
shown when it is measured (``dims.current_step``). A rectangle has four
corners ``[[r0,c0], [r0,c1], [r1,c1], [r1,c0]]``, an ellipse the four corners
of its bounding box (rotated with it), a line two end points, a polygon its
vertices.

Modes
-----
``pan``, ``rectangle``, ``ellipse``, ``polygon``, ``line`` and ``point`` are
what the panels request. ``path`` (freehand) and ``select`` are not drawn in
window mode; asking for them raises :class:`UnsupportedToolMode`, whose message
the panel shows.
"""

from __future__ import annotations

import functools
import weakref
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.view.guitools.naparitools import NapariROISetOverlay
from imswitch.imcommon.view.guitools.viewer_tools import StaleToolToken, ToolToken, ViewerToolService

#: Modes that draw. ``pan`` is the idle mode.
DRAW_MODES = ("rectangle", "ellipse", "polygon", "line", "point")
SUPPORTED_MODES = ("pan",) + DRAW_MODES

#: Scratch shapes and points are yellow, like napari's "Viewer Tools" layer.
SCRATCH_COLOUR = "yellow"
#: Outline width in screen pixels -- ``ViewerToolManager._PIXEL_WIDTH``.
SCRATCH_PEN_WIDTH_PX = 2
#: A scratch point's diameter in screen pixels -- napari's tool Points layer size.
POINT_SIZE_PX = 8

#: A drag shorter than this (screen pixels, per side) is a click, not a shape.
#: Roughly pyqtgraph's own drag threshold, so a twitchy click draws nothing.
MIN_DRAG_SCREEN_PX = 3

#: A click within this many screen pixels of an ROI outline picks it: the
#: outline's 2 px width plus a pixel of slop either side.
PICK_TOLERANCE_SCREEN_PX = 3

#: Above the images and the read-only layers (z = layer index), below nothing.
SCRATCH_Z = 1000
OVERLAY_Z = 500

#: Vertices an ellipse preview is traced with (as ``image_window.ELLIPSE_SEGMENTS``).
PREVIEW_ELLIPSE_SEGMENTS = 64

#: Roi types drawn as open lines rather than closed outlines.
_OPEN_ROI_TYPES = ("line", "polyline", "path")


class UnsupportedToolMode(ValueError):
    """The requested drawing mode is not available with separate windows."""


# ----------------------------------------------------------------------
# the layer a panel measures
# ----------------------------------------------------------------------


class _NoEvent:
    """A napari-style event with nothing ever to report (a window layer's data does
    not change under the panels; the window announces it on the viewer instead)."""

    def connect(self, _handler) -> None:
        return None

    def disconnect(self, _handler) -> None:
        return None


class WindowImageLayer:
    """The active window's result, shaped like the napari image layer the panels read.

    Everything is read from the window on each access, so the object stays the
    same layer across reloads of the result (panels compare layers by
    identity). ``data`` is the *whole* array in displayed axis order and may be
    lazy: measurements slice it themselves.
    """

    #: Lets ``is_image_layer`` accept lazy data (it otherwise needs an ndarray).
    is_window_layer = True

    def __init__(self, window):
        self._window = weakref.ref(window)
        self.events = SimpleNamespace(data=_NoEvent())
        self.visible = True

    def window(self):
        return self._window()

    def _source(self):
        window = self.window()
        return window.measurementLayer() if window is not None else None

    @property
    def name(self) -> str:
        source = self._source()
        return str(source.name) if source is not None else "image"

    @property
    def data(self):
        window, source = self.window(), self._source()
        if window is None or source is None:
            return np.zeros((0, 0))
        return window.displayedData(source)

    @property
    def metadata(self) -> dict:
        source = self._source()
        return source.metadata if source is not None else {}

    @property
    def ndim(self) -> int:
        return int(self.data.ndim)

    @property
    def scale(self) -> tuple[float, ...]:
        window = self.window()
        return tuple(float(v) for v in window.axis_scales) if window is not None else ()

    @property
    def translate(self) -> tuple[float, ...]:
        return tuple(0.0 for _ in self.scale)

    def data_to_world(self, point):
        """Pixel -> calibrated coordinates (scale only: the window has no offset or rotation)."""
        point = np.asarray(point, dtype=float)
        scale = np.asarray(self.scale or (1.0,) * point.size, dtype=float)[-point.size:]
        return point * scale

    def world_to_data(self, point):
        point = np.asarray(point, dtype=float)
        scale = np.asarray(self.scale or (1.0,) * point.size, dtype=float)[-point.size:]
        return point / scale


class _Dims:
    """``viewer.dims`` of the active window: its sliders, one entry per displayed axis."""

    ndisplay = 2

    def __init__(self, viewer: "ActiveWindowViewer"):
        self._viewer = viewer
        self.events = SimpleNamespace(current_step=viewer.sigStep)

    @property
    def current_step(self) -> tuple[int, ...]:
        window = self._viewer.activeWindow()
        if window is None:
            return ()
        return tuple(0 if value is None else int(value) for value in window.current_index())

    @property
    def axis_labels(self) -> tuple[str, ...]:
        window = self._viewer.activeWindow()
        return tuple(window.axis_labels) if window is not None else ()


class _Selection:
    def __init__(self, viewer: "ActiveWindowViewer"):
        self._viewer = viewer
        self.events = SimpleNamespace(active=viewer.sigActive)

    @property
    def active(self):
        return self._viewer.activeLayer()


class _Layers:
    """``viewer.layers``: the active window's image layer, or nothing."""

    def __init__(self, viewer: "ActiveWindowViewer"):
        self._viewer = viewer
        self.selection = _Selection(viewer)
        self.events = SimpleNamespace(inserted=viewer.sigInserted, removed=viewer.sigRemoved)

    def _all(self) -> list:
        layer = self._viewer.activeLayer()
        return [layer] if layer is not None else []

    def __iter__(self):
        return iter(self._all())

    def __len__(self):
        return len(self._all())

    def __getitem__(self, index):
        return self._all()[index]

    def __contains__(self, layer) -> bool:
        return any(layer is candidate for candidate in self._all())


class ActiveWindowViewer(QtCore.QObject):
    """Stands in for the napari viewer the three ROI panels were written against.

    Covers only what they touch: ``dims.current_step`` / ``dims.events``,
    ``layers`` (iteration, ``selection.active``, ``events.inserted/removed``) and
    the tool service. Created before the window manager exists (the panels are
    built with the main view); :meth:`bind` attaches the manager later.
    """

    #: Marker the panels test (``is True``) to pick the window backend.
    is_window_viewer = True

    #: The active window's plane (or content) changed.
    sigStep = QtCore.Signal(object)
    #: A layer appeared / went away / is now the selected one.
    sigInserted = QtCore.Signal(object)
    sigRemoved = QtCore.Signal(object)
    sigActive = QtCore.Signal(object)

    def __init__(self, manager=None):
        super().__init__()
        self._logger = initLogger(self, tryInheritParent=False)
        self._manager = None
        self._current = None  # the window the signals above currently follow
        self._layers_by_window: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self.dims = _Dims(self)
        self.layers = _Layers(self)
        self.camera = SimpleNamespace(zoom=1.0)
        self._service = WindowToolService(self)
        if manager is not None:
            self.bind(manager)

    @property
    def tool_service(self) -> "WindowToolService":
        return self._service

    # -- the active window ------------------------------------------------

    def bind(self, manager) -> None:
        """Follow ``manager``'s active window."""
        if self._manager is manager:
            return
        self._manager = manager
        manager.sigWindowsChanged.connect(self._sync)
        self._sync()

    def windows(self) -> list:
        return self._manager.windows() if self._manager is not None else []

    def activeWindow(self):
        return self._manager.activeWindow() if self._manager is not None else None

    def layerFor(self, window) -> WindowImageLayer:
        layer = self._layers_by_window.get(window)
        if layer is None:
            layer = self._layers_by_window[window] = WindowImageLayer(window)
        return layer

    def activeLayer(self) -> WindowImageLayer | None:
        window = self.activeWindow()
        if window is None or window.measurementLayer() is None:
            return None
        return self.layerFor(window)

    def make_roi_overlay(self) -> "WindowROISetOverlay":
        return WindowROISetOverlay(self)

    def _sync(self) -> None:
        window = self.activeWindow()
        if window is self._current:
            return
        old, self._current = self._current, window
        old_layer = self.layerFor(old) if old is not None else None
        if old is not None:
            for signal, slot in ((old.sigAxisStepChanged, self._onWindowStep),
                                 (old.sigContentChanged, self._onWindowStep)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        if window is not None:
            window.sigAxisStepChanged.connect(self._onWindowStep)
            window.sigContentChanged.connect(self._onWindowStep)
        new_layer = self.activeLayer()
        if old_layer is not None:
            self.sigRemoved.emit(old_layer)
        if new_layer is not None:
            self.sigInserted.emit(new_layer)
        self.sigActive.emit(new_layer)
        self.sigStep.emit(None)

    def _onWindowStep(self, *_args) -> None:
        self.sigStep.emit(None)


# ----------------------------------------------------------------------
# the drawing tool
# ----------------------------------------------------------------------


@dataclass(eq=False)
class _Scratch:
    """One drawn shape: its editable pyqtgraph ROI and who drew it."""

    shape_type: str
    item: pg.ROI
    owner: str | None


@dataclass
class _WindowState:
    """Everything the tool keeps on one window."""

    shapes: list = field(default_factory=list)
    points: list = field(default_factory=list)
    scatter: pg.ScatterPlotItem | None = None
    #: the in-progress drag or polygon: ``(kind, [(x, y), ...])``, and its preview item.
    pending: tuple | None = None
    preview: pg.PlotCurveItem | None = None


class WindowToolService(ViewerToolService):
    """The shared drawing tool over the active image window.

    A :class:`ViewerToolService` in everything the panels can see -- tokens,
    ownership, callbacks, target layer -- with the drawing replaced: shapes are
    pyqtgraph ROIs on a window rather than a napari Shapes layer. Shapes belong
    to the window they were drawn on; the service follows the active window.
    """

    sigShapesChanged = QtCore.Signal()  # noqa: N815 -- replaces the base's property

    def __init__(self, viewer: ActiveWindowViewer):
        # Deliberately not ViewerToolService.__init__: that builds a napari
        # ViewerToolManager. The token and callback state it sets up is
        # recreated here.
        QtCore.QObject.__init__(self)
        self._logger = initLogger(self, tryInheritParent=False)
        self._viewer_ref = weakref.ref(viewer)
        self._generations: dict[str, int] = {}
        self._callbacks: dict[str, list] = {}
        self._shape_handlers: dict[str, list] = {}
        self._active_owner: str | None = None
        self._multi_shape_owners: set[str] = set()
        self._enforcing = False
        self._mode = "pan"
        self._states: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        viewer.sigActive.connect(self._onActiveChanged)
        self.sigShapesChanged.connect(self._on_shapes_changed)

    # -- construction -------------------------------------------------------

    @classmethod
    def for_viewer(cls, viewer):
        return viewer.tool_service

    # -- windows ------------------------------------------------------------

    def _viewer(self) -> ActiveWindowViewer | None:
        return self._viewer_ref()

    def active_window(self):
        viewer = self._viewer()
        return viewer.activeWindow() if viewer is not None else None

    def _state(self, window) -> _WindowState:
        state = self._states.get(window)
        if state is None:
            state = self._states[window] = _WindowState()
        return state

    def _onActiveChanged(self, layer) -> None:
        self._attach_windows()
        self.sigTargetLayerChanged.emit(layer)

    def _attach_windows(self) -> None:
        """Offer the canvas of every window to the drawing tool, or take it back."""
        viewer = self._viewer()
        if viewer is None:
            return
        for window in viewer.windows():
            handler = None
            if self._mode in DRAW_MODES:
                handler = functools.partial(self._canvas_event, weakref.ref(window))
            window.viewBox().drawHandler = handler

    # -- modes --------------------------------------------------------------

    def set_mode(self, token: ToolToken, mode: str) -> None:
        self._check(token)
        if not self.is_active(token.owner_key):
            raise StaleToolToken(
                f"{token.owner_key!r} does not hold the tool; acquire() first"
            )
        if mode not in SUPPORTED_MODES:
            raise UnsupportedToolMode(
                f"Drawing a {mode!r} is not supported with separate windows "
                f"(available: {', '.join(SUPPORTED_MODES)})"
            )
        self._cancel_pending()
        self._mode = mode
        self._attach_windows()

    def get_mode(self) -> str:
        return self._mode

    # -- shapes -------------------------------------------------------------

    def _shapes_of(self, window) -> list:
        return self._state(window).shapes if window is not None else []

    def _owned_indices(self, owner_key: str, total: int | None = None) -> list[int]:
        shapes = self._shapes_of(self.active_window())
        mine = [i for i, shape in enumerate(shapes) if shape.owner == owner_key]
        if mine:
            return mine
        if self._active_owner == owner_key:
            return [i for i, shape in enumerate(shapes) if shape.owner is None]
        return []

    def shapes(self, token: ToolToken) -> list:
        """``(index, shape_type, vertices)`` for the shapes this owner drew on the active window."""
        self._check(token)
        shapes = self._shapes_of(self.active_window())
        return [(i, shapes[i].shape_type, self._vertices(shapes[i]))
                for i in self._owned_indices(token.owner_key)]

    def clear(self, token: ToolToken) -> None:
        self._check(token)
        self._remove_owner_shapes(token.owner_key)
        if self.is_active(token.owner_key):
            window = self.active_window()
            if window is not None:
                self._clear_points(window)

    def points(self, token: ToolToken) -> list:
        """``(row, col)`` for every point placed on the active window."""
        self._check(token)
        window = self.active_window()
        if window is None:
            return []
        return [tuple(point) for point in self._state(window).points]

    def share(self, token: ToolToken, dst_owner_key: str) -> None:
        self._check(token)
        for index in self._owned_indices(token.owner_key):
            self._shapes_of(self.active_window())[index].owner = dst_owner_key

    def claim_new_shapes(self, token: ToolToken) -> None:
        self._check(token)
        for shape in self._shapes_of(self.active_window()):
            if shape.owner is None:
                shape.owner = token.owner_key

    def get_rectangle_bounds(self, index: int):
        """``(r0, c0, r1, c1)`` of the shape at ``index`` (its row and column extremes)."""
        vertices = self._vertices_at(index)
        if vertices is None:
            return None
        return (vertices[:, 0].min(), vertices[:, 1].min(), vertices[:, 0].max(), vertices[:, 1].max())

    def get_line_endpoints(self, index: int):
        vertices = self._vertices_at(index)
        if vertices is None or len(vertices) < 2:
            return None
        return (tuple(vertices[0]), tuple(vertices[1]))

    def _vertices_at(self, index: int):
        shapes = self._shapes_of(self.active_window())
        if not 0 <= index < len(shapes):
            return None
        return self._vertices(shapes[index])

    @staticmethod
    def _vertices(shape: _Scratch) -> np.ndarray:
        """The shape's vertices in the documented format (see the module docstring)."""
        item = shape.item
        if shape.shape_type in ("rectangle", "ellipse"):
            width, height = item.size()
            local = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
        else:
            local = [position for _name, position in item.getLocalHandlePositions()]
        mapped = [item.mapToParent(QtCore.QPointF(float(p[0]), float(p[1]))) for p in local]
        return np.asarray([[point.y(), point.x()] for point in mapped], dtype=np.float64)

    @property
    def target_image_layer(self):
        """The active window's layer. Follows the active window; not settable."""
        viewer = self._viewer()
        return viewer.activeLayer() if viewer is not None else None

    @target_image_layer.setter
    def target_image_layer(self, layer) -> None:
        # The window decides what is measured, so there is nothing to set: the
        # panels seed the target from the viewer on first use, which already
        # names the active window's layer.
        return None

    @property
    def manager(self):
        raise AttributeError("a window tool has no napari ViewerToolManager")

    # -- adding and removing -----------------------------------------------

    def add_shape(self, shape_type: str, vertices, window=None) -> _Scratch:
        """Place an editable shape at ``vertices`` (row, col, world), as a drag would.

        The same path the mouse takes, and what tests and scripts draw with.
        """
        window = window if window is not None else self.active_window()
        if window is None:
            raise RuntimeError("There is no active image window to draw on.")
        shape = self._create_shape(window, shape_type, np.asarray(vertices, dtype=float))
        self.sigShapesChanged.emit()
        return shape

    def add_point(self, row: float, col: float, window=None) -> None:
        window = window if window is not None else self.active_window()
        if window is None:
            raise RuntimeError("There is no active image window to draw on.")
        self._add_point(window, float(row), float(col))
        self.sigShapesChanged.emit()

    def _create_shape(self, window, shape_type: str, rc: np.ndarray) -> _Scratch:
        pen = pg.mkPen(SCRATCH_COLOUR, width=SCRATCH_PEN_WIDTH_PX)
        xy = [(float(c), float(r)) for r, c in rc]
        if shape_type in ("rectangle", "ellipse"):
            rows, cols = rc[:, 0], rc[:, 1]
            position = (float(cols.min()), float(rows.min()))
            size = (float(cols.max() - cols.min()), float(rows.max() - rows.min()))
            cls = pg.RectROI if shape_type == "rectangle" else pg.EllipseROI
            item = cls(position, size, pen=pen)
        elif shape_type == "line":
            item = pg.LineSegmentROI(xy[:2], pen=pen)
        elif shape_type == "polygon":
            item = pg.PolyLineROI(xy, closed=True, pen=pen)
        else:
            raise UnsupportedToolMode(f"Cannot draw a {shape_type!r} with separate windows")
        item.setZValue(SCRATCH_Z)
        window.viewBox().addItem(item, ignoreBounds=True)
        shape = _Scratch(shape_type=shape_type, item=item, owner=self._active_owner)
        item.sigRegionChangeFinished.connect(
            lambda *_a, w=weakref.ref(window): self._shape_edited(w)
        )
        self._state(window).shapes.append(shape)
        return shape

    def _shape_edited(self, window_ref) -> None:
        if window_ref() is not None:
            self.sigShapesChanged.emit()

    def _remove_shape(self, window, shape: _Scratch) -> None:
        state = self._state(window)
        if shape in state.shapes:
            state.shapes.remove(shape)
        try:
            window.viewBox().removeItem(shape.item)
        except Exception:  # noqa: BLE001 -- the window may be going away
            pass

    def _remove_owner_shapes(self, owner_key: str) -> None:
        window = self.active_window()
        if window is None:
            return
        shapes = self._shapes_of(window)
        self._enforcing = True
        try:
            for index in sorted(self._owned_indices(owner_key), reverse=True):
                self._remove_shape(window, shapes[index])
        finally:
            self._enforcing = False

    def _clear_points(self, window) -> None:
        state = self._state(window)
        state.points = []
        if state.scatter is not None:
            state.scatter.setData([], [])

    def _add_point(self, window, row: float, col: float) -> None:
        state = self._state(window)
        if state.scatter is None:
            state.scatter = pg.ScatterPlotItem(
                size=POINT_SIZE_PX, pxMode=True, brush=pg.mkBrush(SCRATCH_COLOUR),
                pen=pg.mkPen("k"),
            )
            state.scatter.setZValue(SCRATCH_Z)
            window.viewBox().addItem(state.scatter, ignoreBounds=True)
        state.points.append((row, col))
        state.scatter.setData([c for _r, c in state.points], [r for r, _c in state.points])

    # -- ownership and notification (as ViewerToolService) --------------------

    def _on_shapes_changed(self) -> None:
        if self._enforcing or self._active_owner is None:
            return
        owner = self._active_owner
        window = self.active_window()
        if window is not None:
            for shape in self._shapes_of(window):
                if shape.owner is None:
                    shape.owner = owner
            self._enforce_single_per_owner(owner, window)
        self._dispatch(owner)

    def _enforce_single_per_owner(self, owner_key: str, window) -> None:
        """Keep only the newest rectangle and line this owner drew, unless it keeps several."""
        if owner_key in self._multi_shape_owners:
            return
        shapes = self._shapes_of(window)
        owned = [shapes[i] for i in self._owned_indices(owner_key)]
        doomed = []
        for shape_type in ("rectangle", "line"):
            doomed.extend([s for s in owned if s.shape_type == shape_type][:-1])
        if not doomed:
            return
        self._enforcing = True
        try:
            for shape in doomed:
                self._remove_shape(window, shape)
        finally:
            self._enforcing = False

    # -- mouse drawing -------------------------------------------------------

    def _canvas_event(self, window_ref, kind: str, xy, _event) -> bool:
        """The window's view box offers an event; True means the tool took it."""
        window = window_ref()
        if window is None:
            return False
        mode = self._mode
        if mode in ("rectangle", "ellipse", "line"):
            if kind == "drag-start":
                self._cancel_pending()
                self._state(window).pending = ("drag", [xy, xy])
            elif kind in ("drag", "drag-finish"):
                pending = self._state(window).pending
                if pending is None or pending[0] != "drag":
                    return False
                pending[1][1] = xy
                if kind == "drag":
                    self._show_preview(window, mode, pending[1])
                else:
                    self._finish_drag(window, mode, pending[1])
            else:
                return False
            return True
        if mode == "polygon":
            if kind == "click":
                state = self._state(window)
                if state.pending is None or state.pending[0] != "polygon":
                    self._cancel_pending()
                    state.pending = ("polygon", [])
                state.pending[1].append(xy)
                self._show_preview(window, "polygon", state.pending[1])
                return True
            if kind == "double-click":
                self.finish_polygon(window)
                return True
            return False
        if mode == "point":
            if kind == "click":
                self._add_point(window, row=xy[1], col=xy[0])
                self.sigShapesChanged.emit()
                return True
        return False

    def _min_extent(self, window) -> tuple[float, float]:
        """The smallest drag worth a shape, in view units along (x, y)."""
        try:
            px, py = window.viewBox().viewPixelSize()
        except Exception:  # noqa: BLE001 -- a view with no size yet
            return 0.0, 0.0
        return MIN_DRAG_SCREEN_PX * px, MIN_DRAG_SCREEN_PX * py

    def _finish_drag(self, window, mode: str, ends) -> None:
        self._cancel_pending(window)
        (x0, y0), (x1, y1) = ends
        min_x, min_y = self._min_extent(window)
        if mode == "line":
            if abs(x1 - x0) < min_x and abs(y1 - y0) < min_y:
                return
            rc = np.array([[y0, x0], [y1, x1]])
        else:
            if abs(x1 - x0) < min_x or abs(y1 - y0) < min_y:
                return
            r0, r1, c0, c1 = min(y0, y1), max(y0, y1), min(x0, x1), max(x0, x1)
            rc = np.array([[r0, c0], [r0, c1], [r1, c1], [r1, c0]])
        self._create_shape(window, mode, rc)
        self.sigShapesChanged.emit()

    def finish_polygon(self, window=None) -> bool:
        """Close the polygon being clicked out (what a double click does); False if too short."""
        window = window if window is not None else self.active_window()
        if window is None:
            return False
        pending = self._state(window).pending
        vertices = list(pending[1]) if pending is not None and pending[0] == "polygon" else []
        # The click before a double click placed the same vertex twice.
        deduped = [v for i, v in enumerate(vertices) if i == 0 or v != vertices[i - 1]]
        self._cancel_pending(window)
        if len(deduped) < 3:
            return False
        self._create_shape(window, "polygon", np.array([[y, x] for x, y in deduped]))
        self.sigShapesChanged.emit()
        return True

    def _show_preview(self, window, mode: str, points) -> None:
        state = self._state(window)
        if mode in ("rectangle", "ellipse"):
            (x0, y0), (x1, y1) = points
            if mode == "rectangle":
                xs, ys = [x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0]
            else:
                angles = np.linspace(0.0, 2.0 * np.pi, PREVIEW_ELLIPSE_SEGMENTS + 1)
                xs = ((x0 + x1) / 2 + (x1 - x0) / 2 * np.cos(angles)).tolist()
                ys = ((y0 + y1) / 2 + (y1 - y0) / 2 * np.sin(angles)).tolist()
        else:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
        if state.preview is None:
            state.preview = pg.PlotCurveItem(
                pen=pg.mkPen(SCRATCH_COLOUR, width=1, style=QtCore.Qt.DashLine)
            )
            state.preview.setZValue(SCRATCH_Z)
            window.viewBox().addItem(state.preview, ignoreBounds=True)
        state.preview.setData(xs, ys)

    def _cancel_pending(self, window=None) -> None:
        windows = [window] if window is not None else list(self._states.keys())
        for each in windows:
            state = self._state(each)
            state.pending = None
            if state.preview is not None:
                try:
                    each.viewBox().removeItem(state.preview)
                except Exception:  # noqa: BLE001
                    pass
                state.preview = None


# ----------------------------------------------------------------------
# the ROI manager's committed ROIs, drawn on the window
# ----------------------------------------------------------------------


class WindowROISetOverlay(NapariROISetOverlay):
    """The ROI Manager's committed ROIs as read-only outlines on the active window.

    The napari overlay's state, styling and hit test are reused; only the
    rendering (pyqtgraph curves instead of a Shapes layer) and the click wiring
    differ. It follows the measured window: when the target layer changes the
    outlines move to the new window.
    """

    def __init__(self, viewer: ActiveWindowViewer):
        # Not NapariROISetOverlay.__init__: that connects napari camera events.
        self._viewer = viewer
        self._layer = None
        self._rois: list = []
        self._selected: set = set()
        self._show_labels = False
        self._visible = True
        self._target_layer = None
        self._default_style = None
        self._failures: list[str] = []
        self._suppress_resync = False
        self._click_handler = None
        self._screen_widths: list[float] = []
        self._zoom_signal = None
        self._window = None
        self._items: list = []

    # -- which window ---------------------------------------------------------

    def _current_window(self):
        layer = self._target_layer
        window = layer.window() if layer is not None and hasattr(layer, "window") else None
        return window if window is not None else self._viewer.activeWindow()

    def set_target_layer(self, layer):
        self._target_layer = layer
        self.refresh()

    # -- drawing --------------------------------------------------------------

    def _clear_items(self) -> None:
        window = self._window
        for item in self._items:
            try:
                window.viewBox().removeItem(item)
            except Exception:  # noqa: BLE001 -- the window may be gone
                pass
        self._items = []

    def set_visible(self, visible: bool):
        self._visible = bool(visible)
        self.refresh()

    def refresh(self):
        from imswitch.imcommon.algorithms.roi_geometry import roi_outline

        window = self._current_window()
        if window is not self._window:
            self._clear_items()
            self._rebind_click(self._window, window)
            self._window = window
        self._clear_items()
        drawn = [roi for roi in self._rois if getattr(roi, "visible", True)]
        if window is None or not self._visible or not drawn:
            return
        row_scale, col_scale = window.yxScales()
        groups: dict = {}
        labels = []
        for index, roi in enumerate(drawn):
            try:
                outline = roi_outline(roi)
            except Exception:  # noqa: BLE001 -- one untraceable ROI must not blank the rest
                continue
            if not outline:
                continue
            style = self._style_for(index, roi)
            key = (style.stroke_color, float(style.stroke_width or self._PIXEL_WIDTH))
            closed = str(roi.roi_type).lower() not in _OPEN_ROI_TYPES
            for part in outline:
                xy = np.column_stack([part[:, 1] * col_scale, part[:, 0] * row_scale])
                if closed:
                    xy = np.vstack([xy, xy[:1]])
                groups.setdefault(key, []).append(np.vstack([xy, np.full((1, 2), np.nan)]))
            if self._show_labels and style.label_visible:
                biggest = max(outline, key=len)
                centre = biggest.mean(axis=0)
                labels.append((roi.name, key[0], centre[1] * col_scale, centre[0] * row_scale))
        for (colour, width), blocks in groups.items():
            joined = np.vstack(blocks)
            curve = pg.PlotCurveItem(
                joined[:, 0], joined[:, 1], connect="finite",
                pen=pg.mkPen(QtGui.QColor(colour), width=max(1, round(width))),
            )
            curve.setZValue(OVERLAY_Z)
            window.viewBox().addItem(curve, ignoreBounds=True)
            self._items.append(curve)
        for name, colour, x, y in labels:
            text = pg.TextItem(name, color=QtGui.QColor(colour), anchor=(0.5, 0.5))
            text.setPos(x, y)
            text.setZValue(OVERLAY_Z)
            window.viewBox().addItem(text, ignoreBounds=True)
            self._items.append(text)

    # -- picking ----------------------------------------------------------------

    def install_click_handler(self, on_roi_clicked):
        self._click_handler = on_roi_clicked
        self._rebind_click(None, self._current_window())
        return on_roi_clicked

    def remove_click_handler(self):
        self._rebind_click(self._window, None)
        self._click_handler = None

    def _rebind_click(self, old, new) -> None:
        if old is not None:
            try:
                old.viewBox().sigViewClicked.disconnect(self._on_view_clicked)
            except (TypeError, RuntimeError):
                pass
        if new is not None and self._click_handler is not None:
            new.viewBox().sigViewClicked.connect(self._on_view_clicked)

    def _on_view_clicked(self, x: float, y: float, modifiers) -> None:
        window = self._window
        if window is None or self._click_handler is None:
            return
        row_scale, col_scale = window.yxScales()
        px, py = window.viewBox().viewPixelSize()
        tolerance = PICK_TOLERANCE_SCREEN_PX * max(px / col_scale, py / row_scale)
        uid = self.roi_at((y / row_scale, x / col_scale), tolerance=tolerance)
        if uid is not None:
            self._click_handler(uid, modifiers)

    def remove(self):
        self.remove_click_handler()
        self._clear_items()
        self._window = None


__all__ = [
    "ActiveWindowViewer",
    "DRAW_MODES",
    "SUPPORTED_MODES",
    "UnsupportedToolMode",
    "WindowImageLayer",
    "WindowROISetOverlay",
    "WindowToolService",
]
