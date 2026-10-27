"""One result in one free-floating pyqtgraph window (the Fiji-style display).

The embedded napari viewer shows one result at a time; this shows one result
per window, so several can sit side by side. It is deliberately minimal, like
an ImageJ image window: the canvas, one slider per axis that is not on screen,
and a status line. It has *no contrast controls of its own*: the ImSwitch image
toolbar (contrast, LUT, channels) acts on whichever window is active, through
the :class:`~imswitch.improcess.view.result_display.ResultDisplay` methods
implemented here.

Rendering follows the W0 measurements (pyqtgraph 0.14, offscreen): one
``ImageItem`` per channel, the first drawn normally and the rest with
``CompositionMode_Plus`` (3 x 2048^2 composited scrubs at 25 fps, a single
2048^2 plane at 84 fps). ``ImageItem.setAutoDownsample`` made scrubbing slower
(15 fps against 25), so it is left off.

Data is never materialised: a slider move reads exactly one plane
(``data[idx...]``), which is what keeps an HDF5/Zarr-backed or
:class:`LazySubsetArray` result usable at any size.

Which layers are drawn, and how, mirrors ``ReconstructionView``:

- a result with ``display_layers()`` shows every *image*-kind spec, in spec
  order (context layers sit under the primary one), each with the spec's
  colormap, levels, visibility, scales and unit;
- any other result is one layer from ``result.data``, transposed by the
  selected view mode, with the result's remembered levels and colormap;
- a result of kind ``"composite"`` supplies one display layer per channel, and
  that is the whole rule for a channel axis being *drawn* as channels (added
  together, each with its own LUT). A plain result that merely has a ``C`` axis
  gets a slider for it, exactly as in the napari view.

The other display-layer kinds are drawn read-only, over the images and on the
same sliders: ``labels`` as a semi-transparent categorical colour layer (a
raster, so it is read plane by plane like an image), ``points`` as a scatter of
the points on the current plane, ``shapes`` as non-editable outlines. A layer
that asks for the 3D canvas (``ndisplay == 3``) cannot be drawn here and is
reported in the status line.
"""

from __future__ import annotations

import colorsys
import functools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.contrast import (
    AUTO_SATURATED_PERCENT,
    auto_levels,
    finite_range,
    safe_display_levels,
)
from imswitch.improcess.model.result import result_kind

#: How much of the view's width the scale bar may span. A fifth reads as a
#: ruler without hiding the structure it measures, and the bar shortens (to the
#: next 1/2/5 x 10^n) as the user zooms in rather than growing past it. A
#: presentation policy, not a measurement.
SCALE_BAR_VIEW_FRACTION = 0.2

#: The "nice" mantissas of a scale-bar length, largest first: the 1-2-5
#: sequence of every engineering ruler.
NICE_MANTISSAS = (5, 2, 1)

#: A new window's canvas is the image's own pixel size, but never more than
#: this fraction of the screen's usable area per side -- a 4096 px plane must
#: not open a window larger than the monitor it is on.
INITIAL_SCREEN_FRACTION = 0.5

#: Smallest canvas side, so a 64 px or 1 px image still opens a window with
#: room for its sliders and status line. Roughly a thumbnail you can hit.
MIN_CANVAS_SIDE_PX = 256

#: Width of the colour lookup tables. pyqtgraph indexes an 8-bit table, so the
#: levels span is cut into this many steps.
LUT_STEPS = 256

#: Opacity of a labels layer when its spec does not ask for one. The label
#: producers in this package pass 0.6 (``SegmentationResult``), which lets the
#: image under a mask stay readable; napari's own default is 0.7.
LABELS_OPACITY = 0.6

#: Distinct colours in the categorical label table; an id takes colour
#: ``id % LABEL_COLOURS``. Sixty-four hues are well past what the eye can tell
#: apart, so neighbouring ids differ and ids a table length apart are the only
#: ones that share.
LABEL_COLOURS = 64

#: Successive label hues step by the golden-ratio conjugate (0.618...), the
#: step that spreads any number of consecutive ids most evenly round the hue
#: circle; saturation and value keep every colour vivid against a gray image.
GOLDEN_RATIO_CONJUGATE = 0.6180339887498949
LABEL_SATURATION = 0.75
LABEL_VALUE = 0.95

#: napari's default point diameter, in data units, for a points layer whose
#: spec gives no ``size``.
DEFAULT_POINT_SIZE = 10.0
DEFAULT_POINT_FACE = "white"
DEFAULT_POINT_BORDER = "dimgray"

#: A point is on a plane when its coordinate along every other axis is within
#: half a pixel of the slider's index: napari's slicing rule for a points layer
#: with its default one-pixel slice thickness.
POINT_SLICE_HALF_THICKNESS = 0.5

#: Default outline colour of a shapes layer, and its width in screen pixels.
#: (napari measures ``edge_width`` in data units; a screen-pixel line stays
#: visible at any zoom, which is what an outline over an image needs.)
DEFAULT_SHAPE_EDGE = "white"
SHAPE_PEN_WIDTH_PX = 2

#: Vertices an ellipse outline is traced with; matches ``roi_outline``.
ELLIPSE_SEGMENTS = 64

#: Colours (RGB) of the single-hue ramps napari offers by those names.
_RAMP_COLOURS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
}

#: The image-toolbar default (``naparitools.addNapariGrayclipColormap``).
DEFAULT_COLORMAP = "grayclip"

_UNIT_DISPLAY = {"um": "µm"}


def display_unit(unit: str) -> str:
    """The unit as printed ("um" is written "µm", as in the napari scale bar)."""
    return _UNIT_DISPLAY.get(unit, unit)


def nice_length(maximum: float) -> float:
    """The largest 1, 2 or 5 x 10^n that does not exceed ``maximum``; 0 if none."""
    if not np.isfinite(maximum) or maximum <= 0:
        return 0.0
    exponent = math.floor(math.log10(maximum))
    for decade in (exponent, exponent - 1):
        for mantissa in NICE_MANTISSAS:
            length = mantissa * 10.0 ** decade
            if length <= maximum * (1 + 1e-12):
                return length
    return 0.0


@functools.lru_cache(maxsize=None)
def lut_table(name: str) -> np.ndarray:
    """A ``(LUT_STEPS, 3)`` uint8 table for the colormap ``name``.

    Names are the ones the toolbar sets on napari (``luts.IMAGE_LUTS``):
    ``grayclip`` is a gray ramp whose top step is red, so a saturated pixel
    stands out; the single-hue names are black-to-colour ramps; the rest come
    from pyqtgraph's own maps, then matplotlib's. An unknown name gives plain
    gray rather than failing (the caller reports it).
    """
    ramp = np.linspace(0, 255, LUT_STEPS).round().astype(np.uint8)
    gray = np.stack([ramp] * 3, axis=1)
    if name == "gray":
        return gray
    if name == "grayclip":
        table = gray.copy()
        table[-1] = (255, 0, 0)
        return table
    if name in _RAMP_COLOURS:
        colour = np.array(_RAMP_COLOURS[name], dtype=float) / 255.0
        return (ramp[:, None] * colour[None, :]).round().astype(np.uint8)
    for source in (None, "matplotlib"):
        try:
            cmap = pg.colormap.get(name) if source is None else pg.colormap.get(name, source=source)
            return np.asarray(cmap.getLookupTable(0.0, 1.0, LUT_STEPS, alpha=False), dtype=np.uint8)
        except Exception:  # noqa: BLE001 -- not in this source; try the next
            continue
    return gray


def is_known_colormap(name: str) -> bool:
    """Whether :func:`lut_table` knows ``name`` (rather than falling back to gray)."""
    if name in ("gray", "grayclip") or name in _RAMP_COLOURS:
        return True
    for source in (None, "matplotlib"):
        try:
            pg.colormap.get(name) if source is None else pg.colormap.get(name, source=source)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


@functools.lru_cache(maxsize=None)
def label_table() -> np.ndarray:
    """A ``(LABEL_COLOURS, 3)`` uint8 table of distinct, vivid colours for label ids."""
    rows = []
    for index in range(LABEL_COLOURS):
        hue = (index * GOLDEN_RATIO_CONJUGATE) % 1.0
        rows.append([round(255 * v) for v in colorsys.hsv_to_rgb(hue, LABEL_SATURATION, LABEL_VALUE)])
    return np.asarray(rows, dtype=np.uint8)


def label_rgba(plane: np.ndarray) -> np.ndarray:
    """A label plane as ``(H, W, 4)`` uint8: id 0 transparent, every other id coloured."""
    ids = np.rint(np.asarray(plane)).astype(np.int64)
    rgba = np.empty(ids.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = label_table()[np.mod(ids, LABEL_COLOURS)]
    rgba[..., 3] = np.where(ids != 0, 255, 0)
    return rgba


def to_qcolor(value, default="white") -> QtGui.QColor:
    """A colour from a name/hex string or an RGB(A) sequence (napari's 0..1 floats, or 0..255)."""
    if isinstance(value, QtGui.QColor):
        return QtGui.QColor(value)
    if isinstance(value, str):
        colour = QtGui.QColor(value)
        return colour if colour.isValid() else QtGui.QColor(default)
    try:
        numbers = [float(v) for v in value]
    except (TypeError, ValueError):
        return QtGui.QColor(default)
    if len(numbers) not in (3, 4):
        return QtGui.QColor(default)
    scale = 255.0 if max(numbers) <= 1.0 else 1.0
    return QtGui.QColor(*(int(round(min(255.0, max(0.0, v * scale)))) for v in numbers))


def colour_list(value, count: int, default) -> list:
    """``count`` colours from one colour or a per-item list (napari accepts both)."""
    if value is None:
        return [default] * count
    if isinstance(value, str):
        return [value] * count
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        items = list(value)
        return [items[i % len(items)] for i in range(count)] if items else [default] * count
    if array.ndim == 1:
        return [array] * count
    if array.ndim == 2 and len(array):
        return [array[i % len(array)] for i in range(count)]
    return [default] * count


def _shape_of(data) -> tuple[int, ...]:
    shape = getattr(data, "shape", None)
    return tuple(int(size) for size in (shape if shape is not None else np.shape(data)))


def _component_of(spec) -> str:
    component = getattr(spec, "component", None)
    if component:
        return str(component)
    metadata = getattr(spec, "metadata", None) or {}
    if metadata.get("component"):
        return str(metadata["component"])
    return str(getattr(spec, "name", "layer"))


@dataclass
class _Layer:
    """One image layer: its source array, look and ImageItem."""

    layer_id: str
    name: str
    #: The display-layer component this layer's settings are filed under, or
    #: None for the result as a whole (a plain-image result).
    component: str | None
    data: Any
    item: pg.ImageItem
    colormap: str = DEFAULT_COLORMAP
    levels: tuple[float, float] | None = None
    value_range: tuple[float, float] | None = None
    visible: bool = True
    rgb: bool = False
    role: str = "primary"
    metadata: dict = field(default_factory=dict)
    plane: np.ndarray | None = None
    placed: tuple | None = None
    #: ``"image"`` or ``"labels"``: both are rasters read plane by plane.
    kind: str = "image"
    opacity: float = 1.0


@dataclass
class _Overlay:
    """One points or shapes layer: read-only items over the images."""

    overlay_id: str
    name: str
    kind: str
    visible: bool
    role: str
    metadata: dict
    #: Calibrated size of one data unit along (row, col), from the spec's scales.
    scales: tuple[float, float]
    items: list = field(default_factory=list)
    #: points: the (N, D) coordinates, the sizes/colours, and the scatter item.
    coords: np.ndarray | None = None
    size: float = DEFAULT_POINT_SIZE
    face: list = field(default_factory=list)
    border: Any = None
    border_width: float | None = None
    #: shapes: (leading coords, (V, 2) xy outline) per shape, grouped by colour
    #: (``QColor.rgba()``); ``items[i]`` is the curve drawing ``groups[i]``.
    outlines: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)


class ImageWindow(QtWidgets.QWidget):
    """Shows one :class:`ProcessingResult` and serves the image toolbar."""

    #: The window became the active one (gained focus / was raised on purpose).
    sigActivated = QtCore.Signal()
    #: The user closed the window. It is only hidden; the result stays listed.
    sigClosed = QtCore.Signal()
    # ResultDisplay signals
    sigAxisStepChanged = QtCore.Signal(tuple)
    sigViewChanged = QtCore.Signal()
    sigImageLevelsChanged = QtCore.Signal(object, object)

    def __init__(self, result, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self._logger = initLogger(self, tryInheritParent=False)
        self._result = result
        self._layers: list[_Layer] = []
        self._overlays: list[_Overlay] = []
        self._notes: list[str] = []
        self._perm: list[int] = []
        self._axis_labels: list[str] = []
        self._axis_scales: list[float] = []
        self._scale_unit = "px"
        self._index: list[int] = []
        self._sliders: list[tuple[QtWidgets.QSlider, QtWidgets.QSpinBox]] = []
        self._view_modes: list = []
        self._closing_for_good = False
        self._cursor_text = ""
        self._active_id: str | None = None
        self._sized = False

        self.setWindowTitle(str(getattr(result, "name", "") or "Result"))

        self._canvas = pg.GraphicsLayoutWidget()
        self._canvas.setBackground("k")
        self._vb = self._canvas.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self._vb.setBackgroundColor("k")
        self._scalebar = pg.ScaleBar(size=1.0, suffix="")
        self._scalebar.setParentItem(self._vb)
        self._scalebar.hide()
        self._vb.sigRangeChanged.connect(self._updateScaleBar)
        self._canvas.scene().sigMouseMoved.connect(self._onMouseMoved)

        self._modeCombo = QtWidgets.QComboBox()
        self._modeCombo.setVisible(False)
        self._modeCombo.currentIndexChanged.connect(self._onModeChanged)
        self._slidersLayout = QtWidgets.QGridLayout()
        self._slidersLayout.setContentsMargins(0, 0, 0, 0)
        self._status = QtWidgets.QLabel()
        self._status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self._canvas, 1)
        layout.addWidget(self._modeCombo)
        layout.addLayout(self._slidersLayout)
        layout.addWidget(self._status)

        self._loadResult()

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def result(self):
        return self._result

    def setResult(self, result) -> None:
        """Show another result in this window (a refined or replaced list entry)."""
        self._result = result
        self.setWindowTitle(str(getattr(result, "name", "") or "Result"))
        self._loadResult()

    def activate(self) -> None:
        """Raise this window and make it the active one."""
        self.show()
        self.raise_()
        self.activateWindow()
        self.sigActivated.emit()

    def changeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.ActivationChange and self.isActiveWindow():
            self.sigActivated.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        """Closing hides: the result is still in the list and can be reopened."""
        if self._closing_for_good:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.sigClosed.emit()

    def dispose(self) -> None:
        """Really close (the result left the list, or ImProcess is closing)."""
        self._closing_for_good = True
        self.close()
        self.deleteLater()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def _clearLayers(self) -> None:
        for layer in self._layers:
            self._vb.removeItem(layer.item)
        for overlay in self._overlays:
            for item in overlay.items:
                self._vb.removeItem(item)
        self._layers = []
        self._overlays = []
        self._notes = []
        self._active_id = None

    def _loadResult(self) -> None:
        """Build the layers from the result, the way the napari path would."""
        result = self._result
        specs = []
        if hasattr(result, "display_layers"):
            specs = list(result.display_layers() or [])
        if specs:
            if hasattr(result, "applyDisplayLayerSettings"):
                specs = result.applyDisplayLayerSettings(specs)
            self._setViewModesQuietly(None)
            self.setDisplayLayers(specs)
            return
        if result_kind(result) in ("curve", "table", "localization"):
            self._clearLayers()
            self._notes.append(f"{result_kind(result)} results have no image to show in a window")
            self._rebuildSliders()
            self._refreshStatus()
            return
        self._setViewModesQuietly(list(getattr(result, "view_modes", None) or []))
        self._loadPlainResult()

    def _loadPlainResult(self) -> None:
        result = self._result
        data = result.data
        shape = _shape_of(data)
        self._clearLayers()
        if len(shape) < 2:
            self._notes.append(f"{self._result.name!r} has {len(shape)} axis/axes and cannot be shown as an image")
            self._rebuildSliders()
            self._refreshStatus()
            return
        mode = self._currentMode()
        perm = list(mode.transpose) if mode is not None else list(range(len(shape)))
        labels = [str(result.axis_labels[a]) for a in perm]
        scales = [float(result.axis_scales[a]) for a in perm]
        colormap = result.getDisplayColormap() if hasattr(result, "getDisplayColormap") else DEFAULT_COLORMAP
        levels = result.getDispLevels() if hasattr(result, "getDispLevels") else None
        self._setGeometry(perm, labels, scales, result.scale_unit, shape)
        layer = self._makeLayer(
            layer_id=str(result.name), name=str(result.name), component=None, data=data,
            colormap=colormap, levels=levels, visible=True, rgb=False, role="primary",
            metadata={"source_result": str(result.name), "axis_labels": labels,
                      "scale_unit": result.scale_unit},
        )
        self._layers.append(layer)
        self._finishLoad()

    def _setViewModesQuietly(self, modes) -> None:
        self._view_modes = list(modes or [])
        self._modeCombo.blockSignals(True)
        self._modeCombo.clear()
        for mode in self._view_modes:
            self._modeCombo.addItem(mode.name)
        self._modeCombo.blockSignals(False)
        self._modeCombo.setVisible(len(self._view_modes) > 1)

    def _currentMode(self):
        if not self._view_modes:
            return None
        row = max(0, self._modeCombo.currentIndex())
        return self._view_modes[min(row, len(self._view_modes) - 1)]

    def _setGeometry(self, perm, labels, scales, unit, shape) -> None:
        self._perm = list(perm)
        self._axis_labels = list(labels)
        self._axis_scales = list(scales)
        self._scale_unit = str(unit or "px")
        self._source_shape = tuple(shape)
        self._index = [0] * len(shape)

    def _makeLayer(self, *, layer_id, name, component, data, colormap, levels, visible,
                   rgb, role, metadata, kind="image", opacity=1.0) -> _Layer:
        item = pg.ImageItem(axisOrder="row-major")
        # A labels layer is coloured by id in _refreshPlanes (RGBA), so it has no LUT.
        item.setLookupTable(None if rgb or kind == "labels" else lut_table(colormap))
        if kind == "labels":
            item.setOpacity(float(opacity))
        layer = _Layer(
            layer_id=layer_id, name=name, component=component, data=data, item=item,
            colormap=str(colormap), levels=None, visible=bool(visible), rgb=bool(rgb),
            role=role, metadata=dict(metadata), kind=kind, opacity=float(opacity),
        )
        if kind == "image" and not is_known_colormap(layer.colormap):
            self._notes.append(f"colormap {layer.colormap!r} is not available here; shown in gray")
        if levels is not None:
            layer.levels = safe_display_levels(*levels)
        return layer

    def _finishLoad(self) -> None:
        """Add the items, size the sliders, pick levels and draw."""
        first = self._layers[0] if self._layers else None
        for index, layer in enumerate(self._layers):
            composite = result_kind(self._result) == "composite" and index > 0
            if composite:
                layer.item.setCompositionMode(QtGui.QPainter.CompositionMode_Plus)
            layer.item.setZValue(index)
            self._vb.addItem(layer.item)
        # Overlays sit above every raster, in spec order, like napari's layer list.
        for offset, overlay in enumerate(self._overlays):
            for item in overlay.items:
                item.setZValue(len(self._layers) + offset)
                self._vb.addItem(item)
        # The toolbar's "active" layer is the first primary *image*: contrast
        # does not apply to a labels layer, and napari keeps the image anchor active.
        images = [layer for layer in self._layers if layer.kind == "image"]
        primary = next((layer for layer in images if layer.role == "primary"),
                       images[0] if images else first)
        self._active_id = primary.layer_id if primary is not None else None
        self._rebuildSliders()
        self._refreshPlanes()
        for layer in self._layers:
            self._settleLevels(layer)
        self._vb.autoRange(padding=0)
        self._updateScaleBar()
        self._sizeToImage()
        self._refreshStatus()

    def _settleLevels(self, layer: _Layer) -> None:
        """Give a layer levels: the remembered ones, else scaled to its data.

        Only a layer with no remembered levels reads (a bounded sample of) its
        data here; the slider's value range is worked out when the toolbar asks
        for it, so reopening a lazy result reads its plane and nothing else.
        """
        if layer.rgb or layer.kind != "image":
            return
        if layer.levels is None:
            try:
                layer.levels = auto_levels(layer.data, saturated_percent=AUTO_SATURATED_PERCENT)
            except Exception as exc:  # noqa: BLE001 -- say so, do not draw black silently
                self._logger.warning("Could not scale %r automatically: %s", layer.name, exc)
                self._notes.append(f"could not scale {layer.name!r} automatically: {exc}")
                if layer.plane is not None and layer.plane.size:
                    layer.levels = (float(np.nanmin(layer.plane)), float(np.nanmax(layer.plane)))
        if layer.levels is not None:
            layer.item.setLevels(layer.levels)

    def _place(self, layer: _Layer) -> None:
        """Place an item in calibrated coordinates (image pixel -> scale units)."""
        if layer.plane is None:
            return
        sy, sx = self._yx_scales()
        height, width = layer.plane.shape[:2]
        placed = (height, width, sy, sx)
        if layer.placed != placed:
            layer.item.setRect(QtCore.QRectF(0, 0, width * sx, height * sy))
            layer.placed = placed

    def _yx_scales(self) -> tuple[float, float]:
        if len(self._axis_scales) < 2:
            return 1.0, 1.0
        n_img = 2
        return self._axis_scales[-n_img] or 1.0, self._axis_scales[-1] or 1.0

    def _sizeToImage(self) -> None:
        """Size a new window to its image, once (a view-mode switch keeps the user's size)."""
        if self._sized or not self._layers or self._layers[0].plane is None:
            return
        self._sized = True
        height, width = self._layers[0].plane.shape[:2]
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            width = min(width, int(screen.width() * INITIAL_SCREEN_FRACTION))
            height = min(height, int(screen.height() * INITIAL_SCREEN_FRACTION))
        self._canvas.setMinimumSize(MIN_CANVAS_SIDE_PX, MIN_CANVAS_SIDE_PX)
        self.resize(
            max(width, MIN_CANVAS_SIDE_PX),
            max(height, MIN_CANVAS_SIDE_PX) + self._chromeHeight(),
        )

    def _chromeHeight(self) -> int:
        """Height of everything under the canvas (sliders, mode combo, status line)."""
        return self.layout().totalSizeHint().height() - self._canvas.sizeHint().height()

    # ------------------------------------------------------------------
    # sliders and planes
    # ------------------------------------------------------------------

    def _spatial_axes(self) -> int:
        return 3 if self._layers and self._layers[0].rgb else 2

    def _rebuildSliders(self) -> None:
        while self._slidersLayout.count():
            widget = self._slidersLayout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self._sliders = []
        if not self._layers:
            return
        for k in range(len(self._perm) - self._spatial_axes()):
            size = self._source_shape[self._perm[k]]
            label = QtWidgets.QLabel(self._axis_labels[k])
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(0, max(0, size - 1))
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, max(0, size - 1))
            slider.setValue(self._index[k] if k < len(self._index) else 0)
            spin.setValue(slider.value())
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(lambda value, axis=k: self._onSliderMoved(axis, value))
            self._slidersLayout.addWidget(label, k, 0)
            self._slidersLayout.addWidget(slider, k, 1)
            self._slidersLayout.addWidget(spin, k, 2)
            self._sliders.append((slider, spin))
            self._slidersLayout.setColumnStretch(1, 1)

    def _onSliderMoved(self, axis: int, value: int) -> None:
        if self._index[axis] == value:
            return
        self._index[axis] = int(value)
        self._refreshPlanes()
        self.sigAxisStepChanged.emit(tuple(self._index))
        self._refreshStatus()

    def _read_plane(self, layer: _Layer) -> np.ndarray:
        """Read the one plane on screen -- ``data[idx...]`` and nothing more."""
        n = len(layer_shape := _shape_of(layer.data))
        key: list[Any] = [slice(None)] * n
        for k in range(n - self._spatial_axes()):
            axis = self._perm[k]
            key[axis] = max(0, min(self._index[k], layer_shape[axis] - 1))
        plane = np.asarray(layer.data[tuple(key)])
        if not layer.rgb and self._perm[-2] > self._perm[-1]:
            plane = plane.T
        return plane

    def _refreshPlanes(self, only: _Layer | None = None) -> None:
        for layer in self._layers:
            if only is not None and layer is not only:
                continue
            layer.item.setVisible(layer.visible)
            if not layer.visible:
                continue
            try:
                layer.plane = self._read_plane(layer)
            except Exception as exc:  # noqa: BLE001 -- a closed file, a bad slice
                self._logger.warning("Could not read a plane of %r: %s", layer.name, exc)
                self._notes.append(f"could not read {layer.name!r}: {exc}")
                continue
            if layer.kind == "labels":
                layer.item.setImage(label_rgba(layer.plane), autoLevels=False)
            else:
                layer.item.setImage(layer.plane, autoLevels=False,
                                    levels=layer.levels if not layer.rgb else None)
            self._place(layer)
        if only is None:
            self._refreshOverlays()

    def current_index(self) -> tuple[int | None, ...]:
        """The plane on screen, one entry per displayed axis.

        Displayed order (after the view mode): each slider axis holds its index
        and the Y, X axes (the colour axis too, for an RGB layer) hold ``None``.
        """
        n_img = self._spatial_axes()
        return tuple(self._index[:len(self._perm) - n_img]) + (None,) * n_img if self._layers else ()

    def set_current_index(self, index) -> None:
        """Move the sliders to ``index`` (as :meth:`current_index` returns; ``None`` and
        missing entries leave their axis alone; values are clamped to the axis)."""
        for k, (slider, _spin) in enumerate(self._sliders):
            if k < len(index) and index[k] is not None:
                slider.setValue(max(slider.minimum(), min(int(index[k]), slider.maximum())))

    @property
    def axis_labels(self) -> list[str]:
        """Axis labels in displayed order (matches :meth:`current_index`)."""
        return list(self._axis_labels)

    def currentPlane(self):
        """The active layer's plane on screen (what the contrast dialog's "view" scope reads)."""
        layer = self._activeLayer()
        return layer.plane if layer is not None else None

    # ------------------------------------------------------------------
    # status line, cursor, scale bar
    # ------------------------------------------------------------------

    def _indexText(self) -> str:
        return "  ".join(
            f"{self._axis_labels[k]}={self._index[k] + 1}/{self._source_shape[self._perm[k]]}"
            for k in range(len(self._sliders))
        )

    def readoutText(self, x: float, y: float) -> str:
        """The status text for a cursor at view coordinates ``(x, y)`` (scale units).

        Position first (calibrated, or pixel indices when the unit is px), then
        the value under it -- per visible channel for a composite -- then the
        slider indices. Outside the image only the position is given.
        """
        sy, sx = self._yx_scales()
        unit = display_unit(self._scale_unit)
        col, row = int(math.floor(x / sx)), int(math.floor(y / sy))
        if unit == "px":
            position = f"x={col}  y={row}"
        else:
            position = f"x={x:.4g} {unit}  y={y:.4g} {unit}"
        values = []
        visible = [layer for layer in self._layers if layer.visible and layer.plane is not None]
        for layer in visible:
            plane = layer.plane
            if 0 <= row < plane.shape[0] and 0 <= col < plane.shape[1]:
                value = plane[row, col]
                text = (
                    ",".join(f"{v:g}" for v in np.atleast_1d(value))
                    if layer.rgb else f"{value:.6g}"
                )
                values.append(text if len(visible) == 1 else f"{layer.name}={text}")
        parts = [position]
        if values:
            parts.append("value=" + (values[0] if len(values) == 1 else "  ".join(values)))
        index = self._indexText()
        if index:
            parts.append(index)
        return "  ".join(parts)

    def _onMouseMoved(self, scene_pos) -> None:
        if not self._layers:
            return
        point = self._vb.mapSceneToView(scene_pos)
        self._cursor_text = self.readoutText(point.x(), point.y())
        self._refreshStatus(cursor=True)

    def _refreshStatus(self, cursor: bool = False) -> None:
        notes = "; ".join(self._notes)
        base = self._cursor_text if cursor and self._cursor_text else self._indexText()
        self._status.setText("  |  ".join(part for part in (base, notes) if part))

    def statusText(self) -> str:
        return self._status.text()

    def _updateScaleBar(self, *_args) -> None:
        unit = display_unit(self._scale_unit)
        if unit == "px" or not self._layers:
            self._scalebar.hide()
            return
        length = nice_length(self._vb.viewRect().width() * SCALE_BAR_VIEW_FRACTION)
        if length <= 0:
            self._scalebar.hide()
            return
        self._scalebar.size = length
        self._scalebar.text.setText(f"{length:g} {unit}")
        self._scalebar.updateBar()
        self._scalebar.show()

    def scaleBarVisible(self) -> bool:
        return self._scalebar.isVisible()

    # ------------------------------------------------------------------
    # ResultDisplay: what is shown
    # ------------------------------------------------------------------

    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px", colormap=DEFAULT_COLORMAP,
                 name=None, identity=None) -> None:
        """Show ``im`` (already in displayed axis order) as the only layer."""
        shape = _shape_of(im)
        self._clearLayers()
        if len(shape) < 2:
            self._notes.append("an image needs two axes")
            self._rebuildSliders()
            return
        scales = list(axisScales) if axisScales is not None else [1.0] * len(shape)
        labels = [str(label) for label in axisLabels]
        self._setGeometry(list(range(len(shape))), labels, scales, scaleUnit, shape)
        layer_name = str(name) if name is not None else "Reconstruction"
        self._layers.append(self._makeLayer(
            layer_id=layer_name, name=layer_name, component=None, data=im, colormap=colormap,
            levels=None, visible=True, rgb=False, role="primary",
            metadata={"source_result": layer_name, "axis_labels": labels, "scale_unit": scaleUnit},
        ))
        self._finishLoad()

    def setDisplayLayers(self, layerSpecs, identity=None) -> None:
        """Show the specs: rasters (image, labels) as items, points and shapes as overlays."""
        self._clearLayers()
        rasters, others = [], []
        for spec in list(layerSpecs or []):
            kind = str(getattr(spec, "kind", "image") or "image")
            if (spec.metadata or {}).get("ndisplay") == 3:
                self._notes.append(f"{spec.name!r} is a 3D layer; it needs the napari view")
                continue
            if kind in ("points", "shapes"):
                others.append((kind, spec))
                continue
            # "image", "labels" and any unknown kind (napari treats it as an image).
            kind = "labels" if kind == "labels" else "image"
            if len(_shape_of(spec.data)) < 2:
                self._notes.append(f"skipped {spec.name!r}: an image layer needs two axes")
                continue
            rasters.append((kind, spec))
        if not rasters:
            if others:
                self._notes.append("points and shapes need an image or labels layer to sit on")
            self._rebuildSliders()
            self._refreshStatus()
            return
        first = rasters[0][1]
        shape = _shape_of(first.data)
        scales = list(first.axis_scales) if first.axis_scales is not None else [1.0] * len(shape)
        self._setGeometry(list(range(len(shape))), [str(a) for a in first.axis_labels], scales,
                          first.scale_unit, shape)
        for kind, spec in rasters:
            if len(_shape_of(spec.data)) != len(shape):
                self._notes.append(f"skipped {spec.name!r}: its axes differ from {first.name!r}")
                continue
            component = _component_of(spec)
            metadata = dict(spec.metadata or {})
            metadata.setdefault("axis_labels", list(spec.axis_labels))
            metadata.setdefault("scale_unit", spec.scale_unit)
            metadata.setdefault("component", component)
            metadata.setdefault("source_result", str(getattr(self._result, "name", "")))
            extra = dict(spec.layer_kwargs or {})
            self._layers.append(self._makeLayer(
                layer_id=component, name=str(spec.name), component=component, data=spec.data,
                colormap=spec.colormap, levels=spec.display_levels if kind == "image" else None,
                visible=spec.visible, rgb=bool(spec.rgb) and kind == "image",
                role=str(getattr(spec, "role", "primary") or "primary"),
                metadata=metadata, kind=kind,
                opacity=float(extra.get("opacity", LABELS_OPACITY)),
            ))
        for kind, spec in others:
            overlay = self._makeOverlay(kind, spec)
            if overlay is not None:
                self._overlays.append(overlay)
        self._finishLoad()

    # ------------------------------------------------------------------
    # points and shapes (read-only overlays)
    # ------------------------------------------------------------------

    def _makeOverlay(self, kind: str, spec) -> _Overlay | None:
        """Build the items of one points/shapes spec, or None (and say why)."""
        component = _component_of(spec)
        extra = dict(spec.layer_kwargs or {})
        try:
            data = spec.data
            scales = list(spec.axis_scales) if spec.axis_scales is not None else []
            if len(scales) >= 2:
                row_scale, col_scale = float(scales[-2] or 1.0), float(scales[-1] or 1.0)
            else:
                row_scale = col_scale = 1.0
            overlay = _Overlay(
                overlay_id=component, name=str(spec.name), kind=kind, visible=bool(spec.visible),
                role=str(getattr(spec, "role", "primary") or "primary"),
                metadata=dict(spec.metadata or {}), scales=(row_scale, col_scale),
            )
            if kind == "points":
                coords = np.asarray(data, dtype=float)
                if coords.ndim != 2 or coords.shape[1] < 2:
                    raise ValueError(f"points need an (N, >=2) array, got shape {coords.shape}")
                overlay.coords = coords
                overlay.size = float(np.mean(np.atleast_1d(extra.get("size", DEFAULT_POINT_SIZE))))
                overlay.face = colour_list(extra.get("face_color"), len(coords), DEFAULT_POINT_FACE)
                border = extra.get("border_color", extra.get("edge_color"))
                overlay.border = border if border is not None else DEFAULT_POINT_BORDER
                width = extra.get("border_width", extra.get("edge_width"))
                overlay.border_width = None if width is None else float(np.mean(np.atleast_1d(width)))
                scatter = pg.ScatterPlotItem(pxMode=False)
                overlay.items.append(scatter)
            else:
                self._buildOutlines(overlay, data, extra)
        except Exception as exc:  # noqa: BLE001 -- one bad layer must not blank the window
            self._logger.warning("Could not draw the %s layer %r: %s", kind, spec.name, exc)
            self._notes.append(f"could not draw {spec.name!r}: {exc}")
            return None
        return overlay

    def _buildOutlines(self, overlay: _Overlay, data, extra: dict) -> None:
        """Trace every shape once; slicing and colouring happen on refresh."""
        from imswitch.imcommon.algorithms.roi_geometry import ellipse_axes_from_quad

        shapes = [np.asarray(vertices, dtype=float) for vertices in data]
        types = extra.get("shape_type", "rectangle")
        types = [types] * len(shapes) if isinstance(types, str) else list(types)
        colours = colour_list(extra.get("edge_color"), len(shapes), DEFAULT_SHAPE_EDGE)
        row_scale, col_scale = overlay.scales
        for index, vertices in enumerate(shapes):
            if vertices.ndim != 2 or vertices.shape[1] < 2 or len(vertices) < 2:
                continue
            kind = str(types[index % len(types)] if types else "rectangle").lower()
            leading = vertices[:, :-2]
            plane = vertices[:, -2:]
            if kind in ("rectangle", "ellipse") and len(plane) == 2:
                (r0, c0), (r1, c1) = plane
                plane = np.array([[r0, c0], [r0, c1], [r1, c1], [r1, c0]])
            closed = kind in ("rectangle", "polygon")
            if kind == "ellipse":
                centre, axis_a, axis_b = ellipse_axes_from_quad(plane)
                angles = np.linspace(0.0, 2.0 * np.pi, ELLIPSE_SEGMENTS, endpoint=False)
                plane = centre + np.outer(np.cos(angles), axis_a) + np.outer(np.sin(angles), axis_b)
                closed = True
            xy = np.column_stack([plane[:, 1] * col_scale, plane[:, 0] * row_scale])
            if closed:
                xy = np.vstack([xy, xy[:1]])
            colour = to_qcolor(colours[index], DEFAULT_SHAPE_EDGE)
            overlay.outlines.setdefault(colour.rgba(), []).append((leading, xy))
        for rgba in overlay.outlines:
            curve = pg.PlotCurveItem(pen=pg.mkPen(QtGui.QColor.fromRgba(rgba), width=SHAPE_PEN_WIDTH_PX),
                                     connect="finite")
            overlay.groups.append(rgba)
            overlay.items.append(curve)

    def _slice_matches(self, leading: np.ndarray, *, tolerance: float) -> np.ndarray:
        """Which rows of ``leading`` (the non-plane coordinates) lie on the plane shown.

        Aligned on the trailing axes, like napari: a 3-column points layer on a
        4D window reads the last of the window's slider indices.
        """
        count = len(leading)
        sliders = max(0, len(self._perm) - self._spatial_axes())
        used = min(leading.shape[1], sliders)
        if used == 0:
            return np.ones(count, dtype=bool)
        index = np.asarray(self._index[sliders - used:sliders], dtype=float)
        return np.all(np.abs(leading[:, leading.shape[1] - used:] - index) <= tolerance, axis=1)

    def _refreshOverlays(self) -> None:
        for overlay in self._overlays:
            for item in overlay.items:
                item.setVisible(overlay.visible)
            if not overlay.visible:
                continue
            if overlay.kind == "points":
                self._refreshPoints(overlay)
            else:
                self._refreshOutlines(overlay)

    def _refreshPoints(self, overlay: _Overlay) -> None:
        coords = overlay.coords
        keep = self._slice_matches(coords[:, :-2], tolerance=POINT_SLICE_HALF_THICKNESS)
        row_scale, col_scale = overlay.scales
        picked = np.flatnonzero(keep)
        border = to_qcolor(overlay.border, DEFAULT_POINT_BORDER)
        pen = pg.mkPen(None) if overlay.border_width == 0 else pg.mkPen(border, width=1)
        overlay.items[0].setData(
            x=coords[picked, -1] * col_scale, y=coords[picked, -2] * row_scale,
            size=overlay.size * (row_scale + col_scale) / 2.0,
            brush=[pg.mkBrush(to_qcolor(overlay.face[i], DEFAULT_POINT_FACE)) for i in picked],
            pen=pen,
        )

    def _refreshOutlines(self, overlay: _Overlay) -> None:
        for rgba, curve in zip(overlay.groups, overlay.items):
            parts = [xy for leading, xy in overlay.outlines[rgba] if self._outline_on_plane(leading)]
            if not parts:
                curve.setData([], [])
                continue
            gap = np.full((1, 2), np.nan)
            joined = np.vstack([block for xy in parts for block in (xy, gap)])
            curve.setData(joined[:, 0], joined[:, 1])

    def _outline_on_plane(self, leading: np.ndarray) -> bool:
        """A shape lies on the plane when all its vertices round to the slider index."""
        if leading.shape[1] == 0:
            return True
        rounded = np.rint(leading)
        return bool(self._slice_matches(rounded, tolerance=0.0).all())

    def clearImage(self) -> None:
        self._clearLayers()
        self._rebuildSliders()
        self._refreshStatus()

    def tryFastLiveUpdate(self, result, transposeOrder) -> bool:
        """Swap the pixels of an unchanged layer structure; False (untouched) otherwise."""
        try:
            if self._overlays:
                return False  # points/shapes are rebuilt, not patched
            arrays = list(result.display_layer_data()) if hasattr(result, "display_layer_data") else []
            if not arrays:
                arrays = [result.data]
            if len(arrays) != len(self._layers):
                return False
            if any(_shape_of(a) != _shape_of(layer.data) for a, layer in zip(arrays, self._layers)):
                return False
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("tryFastLiveUpdate: fell back (%s)", exc)
            return False
        for array, layer in zip(arrays, self._layers):
            layer.data = array
        self._result = result
        self._refreshPlanes()
        return True

    def moveDimStep(self, axis: int, step: int) -> None:
        """Move the slider of displayed axis ``axis`` (an index into the displayed order)."""
        if 0 <= axis < len(self._sliders):
            slider = self._sliders[axis][0]
            slider.setValue(max(slider.minimum(), min(int(step), slider.maximum())))

    def resetView(self) -> None:
        self._vb.autoRange(padding=0)

    # ------------------------------------------------------------------
    # ResultDisplay: view modes
    # ------------------------------------------------------------------

    def setViewModes(self, viewModes) -> None:
        self._setViewModesQuietly(viewModes)

    def getViewName(self):
        mode = self._currentMode()
        return mode.name if mode is not None else "standard"

    def _onModeChanged(self, _row: int) -> None:
        if not self._view_modes:
            return
        self._loadPlainResult()
        self.sigViewChanged.emit()

    # ------------------------------------------------------------------
    # ResultDisplay: images and contrast
    # ------------------------------------------------------------------

    def _activeLayer(self) -> _Layer | None:
        for layer in self._layers:
            if layer.layer_id == self._active_id:
                return layer
        return self._layers[0] if self._layers else None

    def _imageLayers(self) -> list[_Layer]:
        """The intensity layers: what the contrast toolbar and channel controls address."""
        return [layer for layer in self._layers if layer.kind == "image"]

    def _firstImage(self) -> _Layer | None:
        images = self._imageLayers()
        return images[0] if images else None

    def _layerById(self, layer_id: str) -> _Layer | None:
        requested = str(layer_id)
        return next((layer for layer in self._layers if layer.layer_id == requested), None)

    def getImage(self):
        first = self._firstImage()
        return first.data if first is not None else np.zeros((1, 1))

    def getActiveImage(self):
        layer = self._activeLayer()
        return layer.data if layer is not None else np.zeros((1, 1))

    def getActiveImageLayerMetadata(self) -> dict:
        layer = self._activeLayer()
        return dict(layer.metadata) if layer is not None else {}

    def getImageDisplayLevels(self):
        first = self._firstImage()
        return first.levels if first is not None else None

    def getActiveImageDisplayLevels(self):
        layer = self._activeLayer()
        return layer.levels if layer is not None else None

    def _setLevels(self, layer: _Layer, minimum, maximum) -> None:
        if layer.kind != "image":
            return
        levels = safe_display_levels(minimum, maximum)
        layer.levels = levels
        if not layer.rgb:
            layer.item.setLevels(levels)
        self._persistLevels(layer)
        self.sigImageLevelsChanged.emit(dict(layer.metadata), tuple(float(v) for v in levels))

    def setImageDisplayLevels(self, minimum, maximum) -> None:
        first = self._firstImage()
        if first is not None:
            self._setLevels(first, minimum, maximum)

    def setActiveImageDisplayLevels(self, minimum, maximum) -> None:
        layer = self._activeLayer()
        if layer is not None:
            self._setLevels(layer, minimum, maximum)

    def setImageDisplayLevelsRange(self, minimum, maximum) -> None:
        first = self._firstImage()
        if first is not None:
            first.value_range = safe_display_levels(minimum, maximum)

    def setActiveImageDisplayLevelsRange(self, minimum, maximum) -> None:
        layer = self._activeLayer()
        if layer is not None:
            layer.value_range = safe_display_levels(minimum, maximum)

    def getActiveImageDisplayLevelsRange(self):
        layer = self._activeLayer()
        if layer is None or layer.rgb or layer.kind != "image":
            return None
        if layer.value_range is None:
            layer.value_range = finite_range(layer.data)
        return layer.value_range

    def getActiveImageColormap(self) -> str:
        layer = self._activeLayer()
        return layer.colormap if layer is not None else DEFAULT_COLORMAP

    def _setColormap(self, layer: _Layer, colormap: str) -> None:
        if layer.kind != "image":
            return
        layer.colormap = str(colormap)
        if not layer.rgb:
            layer.item.setLookupTable(lut_table(layer.colormap))
        if not is_known_colormap(layer.colormap):
            self._notes.append(f"colormap {layer.colormap!r} is not available here; shown in gray")
            self._refreshStatus()
        self._persistColormap(layer)

    def setActiveImageColormap(self, colormap: str) -> None:
        layer = self._activeLayer()
        if layer is not None:
            self._setColormap(layer, colormap)

    def getImageLayerStates(self) -> list[dict]:
        return [
            {
                "id": layer.layer_id,
                "name": layer.name,
                "visible": layer.visible,
                "colormap": layer.colormap,
                "display_levels": tuple(float(v) for v in layer.levels) if layer.levels else None,
                "metadata": dict(layer.metadata),
            }
            for layer in self._imageLayers()
        ]

    def setImageLayerVisible(self, layer_id: str, visible: bool) -> None:
        """Show or hide a layer of any kind (image, labels, points, shapes)."""
        layer = self._layerById(layer_id)
        if layer is None:
            overlay = next((o for o in self._overlays if o.overlay_id == str(layer_id)), None)
            if overlay is not None:
                overlay.visible = bool(visible)
                self._persistVisible(overlay, overlay.overlay_id)
                self._refreshOverlays()
            return
        layer.visible = bool(visible)
        self._persistVisible(layer)
        self._refreshPlanes(only=layer)
        layer.item.setVisible(layer.visible)

    def setImageLayerColormap(self, layer_id: str, colormap: str) -> None:
        layer = self._layerById(layer_id)
        if layer is not None:
            self._setColormap(layer, colormap)

    # ------------------------------------------------------------------
    # persistence onto the result (so a result keeps its look in any display)
    # ------------------------------------------------------------------

    def _persistLevels(self, layer: _Layer) -> None:
        result = self._result
        if layer.levels is None:
            return
        if layer.component and hasattr(result, "setDisplayLayerLevels"):
            result.setDisplayLayerLevels(layer.component, layer.levels)
        elif hasattr(result, "setDispLevels"):
            result.setDispLevels(layer.levels)

    def _persistColormap(self, layer: _Layer) -> None:
        result = self._result
        if layer.component and hasattr(result, "setDisplayLayerColormap"):
            result.setDisplayLayerColormap(layer.component, layer.colormap)
        elif hasattr(result, "setDisplayColormap"):
            result.setDisplayColormap(layer.colormap)

    def _persistVisible(self, layer, layer_id: str | None = None) -> None:
        result = self._result
        if hasattr(result, "setDisplayLayerVisible"):
            result.setDisplayLayerVisible(layer_id or layer.layer_id, layer.visible)


__all__ = [
    "ImageWindow",
    "display_unit",
    "is_known_colormap",
    "label_rgba",
    "label_table",
    "lut_table",
    "nice_length",
]
