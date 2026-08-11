import dataclasses
import warnings
from abc import abstractmethod

import napari
import numpy as np
from napari.utils.translations import trans
from qtpy import QtCore, QtGui, QtWidgets
from vispy.color import Color
from vispy.scene.visuals import Compound, Line, Markers
from vispy.visuals.transforms import STTransform

import matplotlib
try:
    # Selects the interactive Qt backend for standalone pyplot windows when a
    # display is available. In headless environments (CI / QT_QPA_PLATFORM=
    # offscreen) this raises, so it is best-effort: embedding FigureCanvasQTAgg
    # below works regardless of the global backend, so importing this module
    # must not crash where no display exists.
    matplotlib.use('Qt5Agg')
except Exception:
    pass
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

import matplotlib.pyplot as plt

from .imagetools import minmaxLevels


def addNapariGrayclipColormap():
    try:
        if hasattr(napari.utils.colormaps.AVAILABLE_COLORMAPS, 'grayclip'):
            return

        grayclip = []
        for i in range(255):
            grayclip.append([i / 255, i / 255, i / 255])
        grayclip.append([1, 0, 0])
        napari.utils.colormaps.AVAILABLE_COLORMAPS['grayclip'] = napari.utils.Colormap(
            name='grayclip', colors=grayclip
        )
    except (AttributeError, TypeError):
        # AVAILABLE_COLORMAPS API changed or is not a dict - skip silently
        pass


class EmbeddedNapari(napari.Viewer):
    """ Napari viewer to be embedded in non-napari windows. Also includes a
    feature to protect certain layers from being removed when added using
    the add_image method. """

    def __init__(self, *args, show=False, **kwargs):
        super().__init__(*args, show=show, **kwargs)

        # Monkeypatch layer removal methods
        oldDelitemIndices = self.layers._delitem_indices

        def newDelitemIndices(key):
            indices = oldDelitemIndices(key)
            for index in indices[:]:
                layer = index[0][index[1]]
                if hasattr(layer, 'protected') and layer.protected:
                    indices.remove(index)
            return indices

        self.layers._delitem_indices = newDelitemIndices

        # Make menu bar not native
        self.window._qt_window.menuBar().setNativeMenuBar(False)

        # Remove unwanted menu bar items
        menuChildren = self.window._qt_window.findChildren(QtWidgets.QAction)
        for menuChild in menuChildren:
            try:
                if menuChild.text() in [trans._('Close Window'), trans._('Exit')]:
                    self.window.file_menu.removeAction(menuChild)
            except Exception:
                pass
        
        self.scale_bar.visible = True
        self.scale_bar.unit = 'µm'

    def add_image(self, *args, protected=False, **kwargs):
        result = super().add_image(*args, **kwargs)

        if isinstance(result, list):
            for layer in result:
                layer.protected = protected
        else:
            result.protected = protected

        return result

    def get_widget(self):
        return self.window._qt_window


class NapariBaseWidget(QtWidgets.QWidget):
    """ Base class for Napari widgets. """

    @property
    @abstractmethod
    def name(self):
        pass

    def __init__(self, napariViewer):
        super().__init__()
        self.viewer = napariViewer

    @classmethod
    def addToViewer(cls, napariViewer, position='left'):
        """ Adds this widget to the specified Napari viewer. """

        # Add dock for this widget
        widget = cls(napariViewer)
        napariViewer.window.add_dock_widget(widget, name=widget.name, area=position)

        # Move layer list to bottom. This reaches into private napari API
        # (window.qt_viewer / window._qt_window) which raises a FutureWarning
        # in recent napari versions — purely cosmetic so we silence the
        # warnings while keeping the repositioning. Guarded with try/except
        # so future API removal degrades gracefully.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', FutureWarning)
                warnings.simplefilter('ignore', DeprecationWarning)
                qt_window = napariViewer.window._qt_window
                dock_layer_list = napariViewer.window.qt_viewer.dockLayerList
                qt_window.removeDockWidget(dock_layer_list)
                qt_window.addDockWidget(dock_layer_list.qt_area, dock_layer_list)
                dock_layer_list.show()
        except AttributeError:
            pass
        return widget

    def addItemToViewer(self, item):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', FutureWarning)
                warnings.simplefilter('ignore', DeprecationWarning)
                _canvas = self.viewer.window.qt_viewer.canvas
                _view = (getattr(_canvas, 'view', None)
                         or getattr(self.viewer.window.qt_viewer, 'view', None))
            item.attach(self.viewer,
                        canvas=_canvas,
                        view=_view,
                        parent=_view.scene,
                        order=1e6 + 8000)
        except AttributeError:
            pass


class NapariUpdateLevelsWidget(NapariBaseWidget):
    """ Napari widget for auto-levelling the currently selected layer.
    Includes both one-click auto-levels and manual min/max range input. """

    @property
    def name(self):
        return 'update levels widget'

    def __init__(self, napariViewer):
        super().__init__(napariViewer)

        # Auto levels button
        self.updateLevelsButton = QtWidgets.QPushButton('Auto levels')
        self.updateLevelsButton.clicked.connect(self._on_update_levels)

        # Manual min/max inputs
        self._minInput = QtWidgets.QLineEdit('0.0')
        self._maxInput = QtWidgets.QLineEdit('1.0')
        self._minInput.setPlaceholderText('Min')
        self._maxInput.setPlaceholderText('Max')
        self._minInput.setFixedWidth(80)
        self._maxInput.setFixedWidth(80)
        self._applyButton = QtWidgets.QPushButton('Set range')
        self._applyButton.clicked.connect(self._on_apply_range)

        minMaxRow = QtWidgets.QHBoxLayout()
        minMaxRow.addWidget(QtWidgets.QLabel('Min:'))
        minMaxRow.addWidget(self._minInput)
        minMaxRow.addWidget(QtWidgets.QLabel('Max:'))
        minMaxRow.addWidget(self._maxInput)

        # Layout
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(self.updateLevelsButton)
        self.layout().addLayout(minMaxRow)
        self.layout().addWidget(self._applyButton)

        # Make sure widget isn't too big
        self.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                                 QtWidgets.QSizePolicy.Maximum))

    def _set_layer_range(self, layer, lo, hi):
        """Apply [lo, hi] contrast range to layer, guarding against zero-width ranges."""
        if lo == hi:
            hi = lo + 1.0
        layer.contrast_limits_range = (lo, hi)
        layer.contrast_limits = (lo, hi)

    def _on_update_levels(self):
        for layer in self.viewer.layers.selection:
            mn, mx = minmaxLevels(layer.data)
            self._set_layer_range(layer, mn, mx)
            # Populate manual inputs so the user can fine-tune from here
            self._minInput.setText(f'{mn:.6g}')
            self._maxInput.setText(f'{mx:.6g}')

    def _on_apply_range(self):
        try:
            lo = float(self._minInput.text())
            hi = float(self._maxInput.text())
        except ValueError:
            return
        lo, hi = min(lo, hi), max(lo, hi)
        for layer in self.viewer.layers.selection:
            self._set_layer_range(layer, lo, hi)


class NapariResetViewWidget(NapariBaseWidget):
    """ Napari widget for resetting the dimensional view of the currently
    selected layer with a single click. """

    @property
    def name(self):
        return 'reset view widget'

    def __init__(self, napariViewer):
        super().__init__(napariViewer)

        # Reset buttons and line edit
        self.resetViewButton = QtWidgets.QPushButton('Reset view')
        self.resetViewButton.clicked.connect(self._on_reset_view)
        self.resetOrderButton = QtWidgets.QPushButton('Reset axis order')
        self.resetOrderButton.clicked.connect(self._on_reset_axis_order)
        self.setOrderButton = QtWidgets.QPushButton('Set axis order')
        self.setOrderButton.clicked.connect(self._on_set_axis_order)
        self.setOrderLineEdit = QtWidgets.QLineEdit('0,1')

        # Layout
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(self.resetViewButton)
        self.layout().addWidget(self.resetOrderButton)
        self.layout().addWidget(self.setOrderLineEdit)
        self.layout().addWidget(self.setOrderButton)

        # Make sure widget isn't too big
        self.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                                 QtWidgets.QSizePolicy.Maximum))

    def _on_reset_view(self):
        self.viewer.reset_view()

    def _on_reset_axis_order(self):
        order_curr = self.viewer.dims.order
        self.viewer.dims.order = tuple(sorted(order_curr))
        step_curr = self.viewer.dims.current_step
        step_curr = [0 for _ in step_curr]
        self.viewer.dims.current_step = tuple(step_curr)

    def _on_set_axis_order(self):
        order_new = [int(c) for c in self.setOrderLineEdit.text().split(',')]
        self.viewer.dims.order = tuple(order_new)


class MplCanvas(FigureCanvas):
    """ Canvas for matplotlib figures in a qt gui layout.
    Source code: https://www.pythonguis.com/tutorials/plotting-matplotlib/."""

    def __init__(self, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = fig.add_subplot(111)
        super(MplCanvas, self).__init__(fig)


class NapariSumImageWidget(NapariBaseWidget):
    """ Napari widget for plotting the sum of individual frames in a stack. """

    @property
    def name(self):
        return 'sum image widget'

    def __init__(self, napariViewer):
        super().__init__(napariViewer)

        # Add matplotlib canvas
        self.canvas = MplCanvas(self, width=5, height=4, dpi=100)

        # Add buttons and edit fields
        self.detector = QtWidgets.QLineEdit('Live: APDred')
        self.step_deg = QtWidgets.QLineEdit('10')
        self.resetPlotButton = QtWidgets.QPushButton('Reset plot')
        self.updatePlotButton = QtWidgets.QPushButton('Update plot')
        self.updatePlotButton.clicked.connect(self._plot_sum)
        self.resetPlotButton.clicked.connect(self._reset_plot)

        # Layout
        self.setLayout(QtWidgets.QVBoxLayout())
        self.layout().addWidget(self.detector)
        self.layout().addWidget(self.step_deg)
        self.layout().addWidget(self.resetPlotButton)
        self.layout().addWidget(self.updatePlotButton)
        self.layout().addWidget(self.canvas)

        # Make sure widget isn't too big
        self.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                                 QtWidgets.QSizePolicy.Maximum))
        
        self.__plot_num = 0

    def _reset_plot(self):
        self.canvas.axes.cla()
        self.canvas.draw()
        self.__plot_num = 0

    def _plot_sum(self):
        self.ydata = np.sum(self.viewer.layers[self.detector.text()].data[0],(-1,-2))
        if self.ydata.ndim > 1:
            self.ydata = np.swapaxes(self.ydata, 0, 1)
        self.xdata = np.arange(len(self.ydata))*float(self.step_deg.text())
        self.canvas.axes.plot(self.xdata, self.ydata, label=self.__plot_num)
        if np.ndim(self.ydata) > 1:
            ydata_mean = np.mean(self.ydata,axis=1)
            self.canvas.axes.plot(self.xdata, ydata_mean, label=f'Mean {self.__plot_num}')
        self.canvas.axes.legend(loc='lower right')
        self.canvas.axes.autoscale(True)
        self.canvas.draw()
        self.__plot_num += 1

    def _on_reset_view(self):
        self.viewer.reset_view()

    def _on_reset_axis_order(self):
        order_curr = self.viewer.dims.order
        self.viewer.dims.order = tuple(sorted(order_curr))
        step_curr = self.viewer.dims.current_step
        step_curr = [0 for _ in step_curr]
        self.viewer.dims.current_step = tuple(step_curr)

    def _on_set_axis_order(self):
        order_new = [int(c) for c in self.setOrderLineEdit.text().split(',')]
        self.viewer.dims.order = tuple(order_new)


class NapariROIOverlay(QtCore.QObject):
    """Napari Shapes-layer based draggable ROI rectangle.

    Drop-in replacement for VispyROIVisual — uses only public napari API.
    attach() is called automatically by ImageWidget.addItem().
    position / size use (x, y) = (col, row) convention to match detector
    x0/y0/width/height parameters.
    """

    sigROIChanged = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._viewer = None
        self._layer = None
        self._position = np.array([0.0, 0.0])   # (col, row) = (x, y)
        self._size = np.array([64.0, 64.0])      # (width, height)
        self._visible = False
        self._updating = False

    def attach(self, viewer, canvas=None, view=None, parent=None, order=0):
        """Called by ImageWidget.addItem(); canvas/view/parent are ignored."""
        self._viewer = viewer
        self._layer = viewer.add_shapes(
            name='_det_roi',
            edge_color='yellow',
            face_color=[1, 1, 0, 0.08],
            edge_width=2,
        )
        self._layer.visible = False
        self._layer.events.data.connect(self._on_data_changed)

    def setPixelScale(self, scale):
        """Render in data-pixel units by scaling the shapes layer to the image
        layer's pixel size, so position/size/bounds stay in detector pixels
        (what the subarray crop expects). `scale` is (x, y) world-units/pixel;
        a napari layer's scale is (row, col) = (y, x)."""
        if self._layer is None or scale is None or len(scale) < 2:
            return
        sx, sy = float(scale[0]), float(scale[1])
        if not (sx > 0 and sy > 0):
            return
        try:
            self._layer.scale = (sy, sx)
        except Exception:
            pass

    def detach(self):
        if self._layer is not None and self._viewer is not None:
            try:
                self._viewer.layers.remove(self._layer)
            except Exception:
                pass
        self._layer = None
        self._viewer = None

    @property
    def position(self):
        return self._position.copy()

    @position.setter
    def position(self, value):
        self._position = np.array(value, dtype=float)
        self._update_layer()

    @property
    def size(self):
        return self._size.copy()

    @size.setter
    def size(self, value):
        self._size = np.array(value, dtype=float)
        self._update_layer()

    @property
    def bounds(self):
        c0, r0 = self._position
        c1, r1 = c0 + self._size[0], r0 + self._size[1]
        return int(c0), int(r0), int(c1), int(r1)

    def _update_layer(self):
        if self._layer is None or self._updating:
            return
        c0, r0 = float(self._position[0]), float(self._position[1])
        c1 = c0 + float(self._size[0])
        r1 = r0 + float(self._size[1])
        self._updating = True
        try:
            self._layer.data = []
            self._layer.add_rectangles(
                [[[r0, c0], [r1, c1]]],
                edge_color='yellow',
                face_color=[1, 1, 0, 0.08],
                edge_width=2,
            )
        finally:
            self._updating = False

    def _on_data_changed(self, event):
        if self._updating or self._layer is None or not self._layer.data:
            return
        coords = np.array(self._layer.data[0])  # (4, 2) in (row, col)
        r_min = coords[:, 0].min()
        c_min = coords[:, 1].min()
        r_max = coords[:, 0].max()
        c_max = coords[:, 1].max()
        self._position = np.array([c_min, r_min])
        self._size = np.array([c_max - c_min, r_max - r_min])
        self.sigROIChanged.emit()

    def show(self):
        self._visible = True
        if self._layer is not None:
            self._layer.visible = True
            self._update_layer()
            try:
                self._layer.mode = 'select'
            except Exception:
                pass

    def hide(self):
        self._visible = False
        if self._layer is not None:
            self._layer.visible = False


class VispyBaseVisual(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self._viewer = None
        self._view = None
        self._canvas = None
        self._nodes = []
        self._visible = True
        self._attached = False

    def attach(self, viewer, view, canvas, parent=None, order=0):
        self._viewer = viewer
        self._view = view
        self._canvas = canvas
        self._attached = True

    def detach(self):
        for node in self._nodes:
            node.parent = None

        self._viewer = None
        self._view = None
        self._canvas = None
        self._attached = False

    def setVisible(self, value):
        for node in self._nodes:
            node.visible = value

        self._visible = value

    def show(self):
        self.setVisible(True)

    def hide(self):
        self.setVisible(False)

    def _get_center_line_p1(self, pos, line_length, vertical):
        if vertical:
            return [pos[0], pos[1] - line_length / 2, 0]
        else:
            return [pos[0] - line_length / 2, pos[1], 0]

    def _get_center_line_p2(self, pos, line_length, vertical):
        if vertical:
            return [pos[0], pos[1] + line_length / 2, 0]
        else:
            return [pos[0] + line_length / 2, pos[1], 0]


class VispyROIVisual(VispyBaseVisual):
    sigROIChanged = QtCore.Signal(object, object)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = np.array(value, dtype=int)
        self._update_position()
        self.sigROIChanged.emit(self.position, self.size)

    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        self._size = np.array(value, dtype=int)
        self._update_size()
        self.sigROIChanged.emit(self.position, self.size)

    @property
    def bounds(self):
        pos = self.position
        size = self.size
        x0 = int(pos[0])
        y0 = int(pos[1])
        x1 = int(x0 + size[0])
        y1 = int(y0 + size[1])
        return x0, y0, x1, y1

    def __init__(self, rect_color='yellow', handle_color='orange'):
        super().__init__()
        self._drag_mode = None
        self._world_scale = 1

        # World units (µm) per data pixel for (x, y). The napari image layer is
        # drawn scaled by the detector pixel size, so to render the ROI aligned
        # to the image while keeping position/size/bounds in DATA-PIXEL units
        # (what consumers crop with), every node transform multiplies by this.
        # Updated live via setPixelScale() as the current detector changes.
        self._pixel_scale = np.array([1.0, 1.0])

        self._position = [0, 0]
        self._size = [64, 64]

        self._rect_color = Color(rect_color)
        self._handle_color = Color(handle_color)

        # note order is x, y, z for VisPy
        self._rect_line_data2D = np.array(
            [[0, 0, 0], [1, 0, 0], [1, 0, 0], [1, 1, 0],
             [1, 1, 0], [0, 1, 0], [0, 1, 0], [0, 0, 0]]
        )
        self._handle_line_data2D = np.array(
            [[0, 0, 0], [1, 0, 0], [1, 0, 0], [1, 1, 0],
             [1, 1, 0], [0, 1, 0], [0, 1, 0], [0, 0, 0]]
        )
        self._handle_side_length = 16

    def attach(self, viewer, view, canvas, parent=None, order=0):
        super().attach(viewer, view, canvas, parent, order)

        self.rect_node = Compound(
            [Line(connect='segments', method='gl', width=4)],
            parent=parent,
        )
        self.rect_node.transform = STTransform()
        self.rect_node.order = order

        self.handle_node = Compound(
            [Line(connect='segments', method='gl', width=2)],
            parent=parent,
        )
        self.handle_node.transform = STTransform()
        self.handle_node.order = order

        self._nodes = [self.rect_node, self.handle_node]

        canvas.events.mouse_press.connect(self.on_mouse_press)
        canvas.events.mouse_move.connect(self.on_mouse_move)
        canvas.events.mouse_release.connect(self.on_mouse_release)
        self._viewer.camera.events.zoom.connect(self._on_zoom_change)
        self._viewer.dims.events.ndisplay.connect(self._on_data_change)

        self._on_zoom_change(None)
        self._on_data_change(None)
        self._update_position()
        self._update_size()

    def setVisible(self, value):
        super().setVisible(value)
        self._on_data_change(None)

    def setPixelScale(self, scale):
        """Set the world-units-per-data-pixel for (x, y) and re-render.

        Lets the ROI stay in data-pixel units while aligning to an image layer
        that napari draws scaled by the detector pixel size. Ignores invalid
        scales so a bad value can never collapse the ROI to zero size.
        """
        scale = np.asarray(scale, dtype=float)
        if scale.shape != (2,) or not np.all(np.isfinite(scale)) or np.any(scale <= 0):
            return
        if np.array_equal(scale, self._pixel_scale):
            return
        self._pixel_scale = scale
        self._update_position()
        self._update_size()

    def _update_position(self):
        if not self._attached:
            return

        sx, sy = self._pixel_scale
        self.rect_node.transform.translate = [(self._position[0] - 0.5) * sx,
                                              (self._position[1] - 0.5) * sy,
                                              0, 0]
        self._update_handle()

    def _update_size(self):
        if not self._attached:
            return

        sx, sy = self._pixel_scale
        self.rect_node.transform.scale = [self._size[0] * sx, self._size[1] * sy, 1, 1]
        self._update_handle()

    def _update_handle(self):
        if not self._attached:
            return

        sx, sy = self._pixel_scale
        self.handle_node.transform.translate = [(self._position[0] - 0.5 + self._size[0]) * sx,
                                                (self._position[1] - 0.5 + self._size[1]) * sy,
                                                0, 0]

    def _on_data_change(self, event):
        if not self._attached or not self._visible:
            return

        # Actual number of displayed dims
        ndisplay = len(self._viewer.dims.displayed)
        if ndisplay != 2:
            raise ValueError('ndisplay not supported')

        self.rect_node._subvisuals[0].set_data(self._rect_line_data2D, self._rect_color)
        self.handle_node._subvisuals[0].set_data(self._handle_line_data2D, self._handle_color)

    def _on_zoom_change(self, event):
        if not self._attached:
            return

        self._world_scale = 1 / self._viewer.camera.zoom
        self.handle_node.transform.scale = [self._handle_side_length * self._world_scale,
                                            self._handle_side_length * self._world_scale,
                                            1, 1]

    def on_mouse_press(self, event):
        if not self._visible or event.button != 1:
            return

        # Determine whether the line was clicked. imap() returns scene/world
        # coords; convert to data pixels (the ROI's own units) so the hit-test
        # stays correct under a non-unity pixel scale.
        mouse_pos = self._mouse_pos_in_pixels(event)

        pos_start = self.position
        pos_end = self.position + self._size

        # The grab handle is drawn at a fixed on-screen size (world units); its
        # extent in data pixels is that size divided by the pixel scale.
        sx, sy = self._pixel_scale
        handle_x = self._world_scale * self._handle_side_length / sx
        handle_y = self._world_scale * self._handle_side_length / sy

        if (pos_end[0] <= mouse_pos[0] < pos_end[0] + handle_x and
            pos_end[1] <= mouse_pos[1] < pos_end[1] + handle_y):
            self._drag_mode = 'scale'
        elif (pos_start[0] <= mouse_pos[0] < pos_end[0] and
              pos_start[1] <= mouse_pos[1] < pos_end[1]):
            self._drag_mode = 'move'
        else:
            return

        # Prepare for dragging
        self._view.interactive = False
        self._start_move_visual_pos = self.position
        self._start_move_visual_size = self.size
        self._start_move_mouse_pos = mouse_pos

    def _mouse_pos_in_pixels(self, event):
        """Mouse position mapped from canvas to ROI data-pixel coordinates."""
        world = self._view.scene.node_transform(self._view).imap(event.pos)[0:2]
        sx, sy = self._pixel_scale
        return np.array([world[0] / sx, world[1] / sy])

    def on_mouse_move(self, event):
        if not self._visible or self._drag_mode is None:
            return

        mouse_pos = self._mouse_pos_in_pixels(event)
        if self._drag_mode == 'move':
            self.position = np.rint(
                self._start_move_visual_pos + mouse_pos - self._start_move_mouse_pos
            )
        elif self._drag_mode == 'scale':
            self.size = np.rint(
                np.clip(self._start_move_visual_size + mouse_pos - self._start_move_mouse_pos,
                        1, None)
            )

    def on_mouse_release(self, event):
        if not self._visible or event.button != 1:
            return

        self._view.interactive = True
        self._drag_mode = None


class VispyLineVisual(VispyBaseVisual):
    sigPositionChanged = QtCore.Signal(np.ndarray, int)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = np.array(value, dtype=int)
        self._update_position()

    @property
    def angle(self):
        return self._angle

    @angle.setter
    def angle(self, value):
        self._angle = value
        self._update_angle()

    def __init__(self, color='yellow', movable=False):
        super().__init__()
        self._drag_mode = None
        self._world_scale = 1

        self._position = [0, 0]
        self._angle = 0.0

        self._color = Color(color)
        self._movable = movable
        self._click_sensitivity = 16

        # note order is x, y, z for VisPy
        self._line_data2D = np.array(
            [[0, 0, 0], [1, 0, 0]]
        )
        self._line_length = 4096

    def attach(self, viewer, view, canvas, parent=None, order=0):
        super().attach(viewer, view, canvas, parent, order)

        self.node = Compound(
            [Line(connect='segments', method='gl', width=4)],
            parent=parent,
        )
        self.node.transform = STTransform()
        self.node.order = order

        self._nodes = [self.node]

        canvas.events.mouse_press.connect(self.on_mouse_press)
        canvas.events.mouse_move.connect(self.on_mouse_move)
        canvas.events.mouse_release.connect(self.on_mouse_release)
        self._viewer.camera.events.zoom.connect(self._on_zoom_change)
        self._viewer.dims.events.ndisplay.connect(self._on_data_change)

        self._on_zoom_change(None)
        self._on_data_change(None)
        self._update_position()

    def setVisible(self, value):
        super().setVisible(value)
        self._on_data_change(None)

    def _update_position(self):
        if not self._attached:
            return

        angleRad = np.deg2rad(self._angle)
        self.node.transform.translate = [
            self._position[0] - self._line_length / 2 * self._world_scale * (np.cos(angleRad)),
            self._position[1] - self._line_length / 2 * self._world_scale * (np.sin(angleRad)),
            0, 0
        ]

    def _update_angle(self):
        if not self._attached:
            return

        self._line_data2D = np.array(
            [
                [0, 0, 0],
                [self._world_scale * self._line_length * np.cos(np.deg2rad(self._angle)),
                 self._world_scale * self._line_length * np.sin(np.deg2rad(self._angle)),
                 0]
            ]
        )
        self._on_data_change(None)
        self._update_position()

    def _on_data_change(self, event):
        if not self._attached or not self._visible:
            return

        # Actual number of displayed dims
        ndisplay = len(self._viewer.dims.displayed)
        if ndisplay != 2:
            raise ValueError('ndisplay not supported')

        self.node._subvisuals[0].set_data(self._line_data2D, self._color)

    def _on_zoom_change(self, event):
        if not self._attached:
            return

        self._world_scale = 1 / self._viewer.camera.zoom
        self._update_angle()

    def on_mouse_press(self, event):
        if not self._visible or not self._movable or event.button != 1:
            return

        # Determine whether the line was clicked
        mouse_pos = np.array(self._view.scene.node_transform(self._view).imap(event.pos)[0:2])

        s = np.sin(np.deg2rad(-self.angle))
        c = np.cos(np.deg2rad(-self.angle))

        center = np.array(self.position)

        mouse_pos_rot = mouse_pos - center
        mouse_pos_rot = np.array([mouse_pos_rot[0] * c - mouse_pos_rot[1] * s,
                                  mouse_pos_rot[0] * s + mouse_pos_rot[1] * c])
        mouse_pos_rot = mouse_pos_rot + center

        x_start = self.position[0] - self._line_length / 2
        x_end = self.position[0] + self._line_length / 2
        y_start = self.position[1] - self._click_sensitivity * self._world_scale
        y_end = self.position[1] + self._click_sensitivity * self._world_scale

        if x_start <= mouse_pos_rot[0] <= x_end and y_start <= mouse_pos_rot[1] <= y_end:
            self._drag_mode = 'move'
        else:
            return

        # Prepare for dragging
        self._view.interactive = False
        self._start_move_visual_pos = self.position
        self._start_move_mouse_pos = mouse_pos

    def on_mouse_move(self, event):
        if not self._visible or not self._movable or self._drag_mode is None:
            return

        mouse_pos = self._view.scene.node_transform(self._view).imap(event.pos)[0:2]
        if self._drag_mode == 'move':
            self.position = np.rint(
                self._start_move_visual_pos + mouse_pos - self._start_move_mouse_pos
            )

    def on_mouse_release(self, event):
        if not self._visible or not self._movable or event.button != 1:
            return

        self._view.interactive = True
        self._drag_mode = None


class VispyGridVisual(VispyBaseVisual):
    def __init__(self, color='yellow'):
        super().__init__()
        self._color = Color(color).rgba
        self._shape = np.array([0, 0])
        self._line_data2D = None
        self._line_length = 4096

    def attach(self, viewer, view, canvas, parent=None, order=0):
        super().attach(viewer, view, canvas, parent, order)

        self._update_line_data()

        self.node = Compound(
            [Line(connect='segments', method='gl', width=4)],
            parent=parent,
        )
        self.node.transform = STTransform()
        self.node.order = order

        self._nodes = [self.node]

        self._viewer.camera.events.zoom.connect(self._on_zoom_change)
        self._viewer.dims.events.ndisplay.connect(self._on_data_change)

        self._on_data_change(None)

    def setVisible(self, value):
        super().setVisible(value)
        self._on_data_change(None)

    def update(self, shape):
        self._shape = np.array(shape)
        self._update_line_data()

    def _update_line_data(self):
        scaled_line_length = self._line_length / self._viewer.camera.zoom
        self._line_data2D = np.array(
            [
                self._get_center_line_p1(0.25 * self._shape, scaled_line_length, True),
                self._get_center_line_p2(0.25 * self._shape, scaled_line_length, True),
                self._get_center_line_p1(0.375 * self._shape, scaled_line_length, True),
                self._get_center_line_p2(0.375 * self._shape, scaled_line_length, True),
                self._get_center_line_p1(0.50 * self._shape, scaled_line_length, True),
                self._get_center_line_p2(0.50 * self._shape, scaled_line_length, True),
                self._get_center_line_p1(0.625 * self._shape, scaled_line_length, True),
                self._get_center_line_p2(0.625 * self._shape, scaled_line_length, True),
                self._get_center_line_p1(0.75 * self._shape, scaled_line_length, True),
                self._get_center_line_p2(0.75 * self._shape, scaled_line_length, True),

                self._get_center_line_p1(0.25 * self._shape, scaled_line_length, False),
                self._get_center_line_p2(0.25 * self._shape, scaled_line_length, False),
                self._get_center_line_p1(0.375 * self._shape, scaled_line_length, False),
                self._get_center_line_p2(0.375 * self._shape, scaled_line_length, False),
                self._get_center_line_p1(0.50 * self._shape, scaled_line_length, False),
                self._get_center_line_p2(0.50 * self._shape, scaled_line_length, False),
                self._get_center_line_p1(0.625 * self._shape, scaled_line_length, False),
                self._get_center_line_p2(0.625 * self._shape, scaled_line_length, False),
                self._get_center_line_p1(0.75 * self._shape, scaled_line_length, False),
                self._get_center_line_p2(0.75 * self._shape, scaled_line_length, False)
            ]
        )
        self._on_data_change(None)

    def _on_data_change(self, event):
        if not self._attached or not self._visible or self._line_data2D is None:
            return

        # Actual number of displayed dims
        ndisplay = len(self._viewer.dims.displayed)
        if ndisplay != 2:
            raise ValueError('ndisplay not supported')

        self.node._subvisuals[0].set_data(self._line_data2D, self._color)

    def _on_zoom_change(self, event):
        if not self._attached:
            return

        self._update_line_data()


class VispyCrosshairVisual(VispyBaseVisual):
    def __init__(self, color='yellow'):
        super().__init__()
        self._paused = False
        self._mouse_moved_since_press = False
        self._color = Color(color).rgba
        self._line_positions = [0, 0]
        self._line_data2D = None
        self._line_length = 4096

    def attach(self, viewer, view, canvas, parent=None, order=0):
        super().attach(viewer, view, canvas, parent, order)

        self._update_line_data()

        self.node = Compound(
            [Line(connect='segments', method='gl', width=4)],
            parent=parent,
        )
        self.node.transform = STTransform()
        self.node.order = order

        self._nodes = [self.node]

        canvas.events.mouse_press.connect(self.on_mouse_press)
        canvas.events.mouse_move.connect(self.on_mouse_move)
        canvas.events.mouse_release.connect(self.on_mouse_release)
        self._viewer.camera.events.zoom.connect(self._on_zoom_change)
        self._viewer.dims.events.ndisplay.connect(self._on_data_change)

        self._on_data_change(None)

    def setVisible(self, value):
        super().setVisible(value)
        self._on_data_change(None)

    def _update_line_data(self):
        scaled_line_length = self._line_length / self._viewer.camera.zoom
        self._line_data2D = np.array(
            [
                self._get_center_line_p1(self._line_positions, scaled_line_length, True),
                self._get_center_line_p2(self._line_positions, scaled_line_length, True),
                self._get_center_line_p1(self._line_positions, scaled_line_length, False),
                self._get_center_line_p2(self._line_positions, scaled_line_length, False)
            ]
        )
        self._on_data_change(None)

    def _on_data_change(self, event):
        if not self._attached or not self._visible or self._line_data2D is None:
            return

        # Actual number of displayed dims
        ndisplay = len(self._viewer.dims.displayed)
        if ndisplay != 2:
            raise ValueError('ndisplay not supported')

        self.node._subvisuals[0].set_data(self._line_data2D, self._color)

    def _on_zoom_change(self, event):
        if not self._attached:
            return

        self._update_line_data()

    def on_mouse_press(self, event):
        if event.button != 1 or not self._visible:
            return

        self._mouse_moved_since_press = False

    def on_mouse_move(self, event):
        self._mouse_moved_since_press = True

        if not self._visible or self._paused:
            return

        mouse_pos = self._view.scene.node_transform(self._view).imap(event.pos)[0:2]
        self._line_positions = [mouse_pos[0], mouse_pos[1]]
        self._update_line_data()

    def on_mouse_release(self, event):
        if event.button != 1 or not self._visible or self._mouse_moved_since_press:
            return

        self._paused = not self._paused
        if not self._paused:
            self.on_mouse_move(event)


class VispyScatterVisual(VispyBaseVisual):
    def __init__(self, color='red', symbol='x'):
        super().__init__()
        self._color = Color(color)
        self._symbol = symbol
        self._markers_data = -1e8 * np.ones((1, 2))
        self._pixel_scale = (1.0, 1.0)

    def attach(self, viewer, view, canvas, parent=None, order=0):
        super().attach(viewer, view, canvas, parent, order)

        self.node = Markers(pos=self._markers_data, parent=parent)
        self.node.transform = STTransform()
        self.node.transform.scale = [self._pixel_scale[0], self._pixel_scale[1], 1, 1]
        self.node.order = order

        self._nodes = [self.node]

        self._viewer.dims.events.ndisplay.connect(self._on_data_change)

        self._on_data_change(None)

    def setPixelScale(self, scale):
        """Scale the scatter to the image layer's detector pixel size so the grid
        (computed in data-pixel units) stays aligned when napari draws the image
        scaled by the pixel size -- matching the ROI overlays. ``scale`` is
        (x, y) world-units/pixel. Ignores invalid values."""
        if scale is None or len(scale) < 2:
            return
        sx, sy = float(scale[0]), float(scale[1])
        if not (sx > 0 and sy > 0):
            return
        self._pixel_scale = (sx, sy)
        node = getattr(self, 'node', None)
        if node is not None:
            node.transform.scale = [sx, sy, 1, 1]

    def setVisible(self, value):
        super().setVisible(value)
        self._on_data_change(None)

    def setData(self, x, y):
        self._markers_data = np.column_stack((x, y))
        self._on_data_change(None)

    def _on_data_change(self, event):
        if not self._attached or not self._visible:
            return

        # Actual number of displayed dims
        ndisplay = len(self._viewer.dims.displayed)
        if ndisplay != 2:
            raise ValueError('ndisplay not supported')

        self.node.set_data(self._markers_data, edge_color=self._color, face_color=self._color,
                           symbol=self._symbol)


class ViewerToolManager(QtCore.QObject):
    """
    Manages napari Shapes layer for interactive viewer tools (ROI, line, etc.).
    
    Provides a unified interface for mode switching and shape data access,
    while maintaining backward compatibility with existing Vispy overlay system.
    
    Examples
    --------
    >>> manager = ViewerToolManager(napari_viewer)
    >>> manager.set_mode('rectangle')  # Enable rectangle drawing
    >>> manager.set_mode('line')       # Switch to line drawing
    >>> manager.set_mode('pan')        # Return to pan/zoom
    >>> 
    >>> # Access shape data
    >>> bounds = manager.get_rectangle_bounds(0)
    >>> endpoints = manager.get_line_endpoints(0)
    """
    
    sigShapesChanged = QtCore.Signal()  # Emitted when shapes data changes
    sigModeChanged = QtCore.Signal(str)  # Emitted when mode changes (mode_name)

    _PIXEL_WIDTH = 2  # desired line width in screen pixels

    def __init__(self, napari_viewer, enforce_single=True):
        """
        Initialize the ViewerToolManager.

        Parameters
        ----------
        napari_viewer : napari.Viewer
            The napari viewer instance to manage tools for.
        enforce_single : bool
            Keep at most one rectangle and one line in the layer, discarding
            older ones as new shapes are drawn.  This is the historical
            behaviour and stays the default for single-panel callers such as
            imcontrol's ImageWidget.  ``ViewerToolService`` turns it off,
            because on a layer shared between panels a global "last one wins"
            rule makes one panel's drawing delete another's; the service
            applies the same limit per owner instead.
        """
        super().__init__()
        self._viewer = napari_viewer
        self._shapes_layer = None
        # Points need a layer of their own; created on first use (C-08).
        self._points_layer = None
        self._current_mode = 'pan'
        self._processing_data_change = False
        self._enforce_single = bool(enforce_single)

        # Recompute edge width whenever the camera zoom changes so user-drawn
        # shapes stay ~PIXEL_WIDTH screen pixels thick regardless of which
        # detector is active.  The Shapes layer is scale-(1,1), so edge_width
        # is in world units (µm) — without this handler, a hardcoded value
        # gives wildly different screen thickness between detectors with
        # different µm/px pitch (a 0.1 µm/px camera at fit-to-view zooms ~5×
        # tighter than a 0.5 µm/px camera, so the same edge_width looks 5×
        # thicker on the small-pitch detector).
        try:
            self._viewer.camera.events.zoom.connect(self._on_zoom_changed)
        except Exception:
            pass

    def _get_edge_width(self):
        """Edge width in world units that renders to ~PIXEL_WIDTH screen pixels."""
        zoom = getattr(self._viewer.camera, 'zoom', 1.0) or 1.0
        return max(0.5, self._PIXEL_WIDTH / zoom)

    def _on_zoom_changed(self, event):
        """Rescale existing shapes' edge widths to match the new zoom."""
        if self._shapes_layer is None:
            return
        if self._shapes_layer not in self._viewer.layers:
            return
        ew = self._get_edge_width()
        # current_edge_width feeds newly-drawn shapes; setting edge_width on
        # the layer rescales every existing shape's edge.  Both are needed:
        # one for shapes already on the canvas, one for the next click.
        try:
            self._shapes_layer.current_edge_width = ew
        except Exception:
            pass
        try:
            if len(self._shapes_layer.data) > 0:
                self._shapes_layer.edge_width = ew
        except Exception:
            pass

    LAYER_NAME = 'Viewer Tools'

    def _ensure_shapes_layer(self):
        """Lazily create the Shapes layer, adopting an existing one if present.

        Several panels can manage tools on the same viewer.  Creating a layer
        unconditionally gave each of them its own ('Viewer Tools',
        'Viewer Tools [1]', ...), so a shape drawn for one panel was invisible
        to the others and the layer list filled up with duplicates.  Adopting
        the layer this viewer already has keeps them on one.
        """
        if self._shapes_layer is not None:
            return

        adopted = None
        try:
            for layer in self._viewer.layers:
                if getattr(layer, 'name', None) == self.LAYER_NAME:
                    adopted = layer
                    break
        except Exception:
            adopted = None

        if adopted is not None:
            self._shapes_layer = adopted
        else:
            self._shapes_layer = self._viewer.add_shapes(
                name=self.LAYER_NAME,
                edge_color='yellow',
                face_color=[0, 0, 0, 0],  # Transparent fill
                edge_width=self._get_edge_width(),
                ndim=2
            )
        # Connect to data change events (also for an adopted layer: each
        # manager needs its own notifications).
        self._shapes_layer.events.data.connect(self._on_shapes_data_changed)
        self._shapes_layer.events.mode.connect(self._on_mode_changed)
    
    def _on_shapes_data_changed(self, event):
        """Handle shapes data change events and enforce single rectangle/line constraint."""
        # Prevent recursion when we modify data ourselves
        if self._processing_data_change:
            return
        
        if self._shapes_layer is None or not self._enforce_single:
            self.sigShapesChanged.emit()
            return

        try:
            self._processing_data_change = True
            
            # Get current data and shape types
            data = list(self._shapes_layer.data)
            shape_types = list(self._shapes_layer.shape_type)
            
            if len(data) != len(shape_types):
                self.sigShapesChanged.emit()
                return
            
            # Find all rectangles and lines
            rectangle_indices = [i for i, stype in enumerate(shape_types) if stype == 'rectangle']
            line_indices = [i for i, stype in enumerate(shape_types) if stype == 'line']
            
            # Determine which shapes to remove (keep only the last of each type)
            indices_to_remove = set()
            
            if len(rectangle_indices) > 1:
                indices_to_remove.update(rectangle_indices[:-1])
            
            if len(line_indices) > 1:
                indices_to_remove.update(line_indices[:-1])
            
            # Remove shapes if needed
            if indices_to_remove:
                indices_to_keep = [i for i in range(len(data)) if i not in indices_to_remove]
                new_data = [data[i] for i in indices_to_keep]
                self._shapes_layer.data = new_data
        
        finally:
            self._processing_data_change = False
        
        self.sigShapesChanged.emit()
    
    def _on_mode_changed(self, event):
        """Handle mode change events."""
        mode = event.value if hasattr(event, 'value') else event
        # napari 0.4.18+ uses Mode enum; coerce to string
        if hasattr(mode, 'value'):
            mode = mode.value
        mode = str(mode)
        if mode.startswith('add_'):
            self._current_mode = mode.replace('add_', '')
        elif mode == 'select':
            self._current_mode = 'select'
        elif mode == 'pan_zoom':
            self._current_mode = 'pan'
        self.sigModeChanged.emit(self._current_mode)
    
    #: Points live on a layer of their own — napari's Shapes layer has no
    #: point type — so the tool has two layers and a mode decides which one is
    #: being drawn on (C-08).
    POINTS_LAYER_NAME = 'Viewer Tool Points'

    def _ensure_points_layer(self):
        """Lazily create the Points layer, adopting an existing one.

        The same discipline as the Shapes layer, for the same reason: several
        panels manage tools on one viewer, and creating unconditionally gave
        each of them its own duplicate.
        """
        if getattr(self, '_points_layer', None) is not None:
            return

        adopted = None
        try:
            for layer in self._viewer.layers:
                if getattr(layer, 'name', None) == self.POINTS_LAYER_NAME:
                    adopted = layer
                    break
        except Exception:
            adopted = None

        if adopted is not None:
            self._points_layer = adopted
        else:
            # napari renamed Points' `edge_color` to `border_color` in 0.5;
            # the kwarg is built rather than passed as None, because passing
            # an unknown keyword is a TypeError whatever its value.
            options = {
                'name': self.POINTS_LAYER_NAME,
                'face_color': 'yellow',
                'size': 8,
                'ndim': 2,
            }
            options[
                'border_color' if self._supports_border_color() else 'edge_color'
            ] = 'black'
            self._points_layer = self._viewer.add_points(**options)
        try:
            self._points_layer.events.data.connect(
                lambda _event=None: self.sigShapesChanged.emit()
            )
        except Exception:
            pass

    @staticmethod
    def _supports_border_color() -> bool:
        """napari renamed `edge_color` to `border_color` for Points in 0.5."""
        try:
            import inspect

            import napari

            return 'border_color' in inspect.signature(
                napari.layers.Points.__init__
            ).parameters
        except Exception:
            return False

    def get_points_data(self):
        """Every point currently on the tool's Points layer, as (row, col)."""
        layer = getattr(self, '_points_layer', None)
        if layer is None:
            return []
        try:
            return [tuple(float(v) for v in point[-2:]) for point in layer.data]
        except Exception:
            return []

    def clear_points(self):
        layer = getattr(self, '_points_layer', None)
        if layer is None:
            return
        try:
            layer.data = []
        except Exception:
            pass

    def set_mode(self, mode):
        """
        Set the interaction mode.
        
        Parameters
        ----------
        mode : str
            One of: 'pan', 'select', 'rectangle', 'line', 'ellipse', 'polygon',
            'path', 'point'
        """
        if mode == 'point':
            # Drawing points means the Points layer is the one taking clicks,
            # and the Shapes layer must stop taking them — two layers both in
            # an add mode is how a click lands on whichever napari happens to
            # consider active.
            self._ensure_points_layer()
            if self._shapes_layer is not None:
                self._shapes_layer.mode = 'pan_zoom'
            self._points_layer.mode = 'add'
            try:
                self._viewer.layers.selection.active = self._points_layer
            except Exception:
                pass
            self._current_mode = mode
            self.sigModeChanged.emit(mode)
            return

        self._ensure_shapes_layer()
        points_layer = getattr(self, '_points_layer', None)
        if points_layer is not None:
            points_layer.mode = 'pan_zoom'

        # Map simplified mode names to napari Shape layer modes
        mode_map = {
            'pan': 'pan_zoom',
            'select': 'select',
            'rectangle': 'add_rectangle',
            'line': 'add_line',
            'ellipse': 'add_ellipse',
            'polygon': 'add_polygon',
            'path': 'add_path'
        }
        
        napari_mode = mode_map.get(mode, mode)
        self._shapes_layer.mode = napari_mode
        self._current_mode = mode
        self.sigModeChanged.emit(mode)
    
    def get_mode(self):
        """Get the current interaction mode."""
        return self._current_mode
    
    def get_shapes_data(self):
        """
        Get all shapes data.
        
        Returns
        -------
        list of np.ndarray
            List of shape vertex arrays. Each shape is an Nx2 array of (x, y) coordinates.
        """
        if self._shapes_layer is None:
            return []
        return list(self._shapes_layer.data)
    
    def get_shape_count(self):
        """Get the number of shapes."""
        if self._shapes_layer is None:
            return 0
        return len(self._shapes_layer.data)
    
    def get_shape_types(self):
        """
        Get the shape types for all shapes in the layer.
        
        Returns
        -------
        list of str
            List of shape types ('line', 'rectangle', etc.), empty list if no layer.
        """
        if self._shapes_layer is None:
            return []
        return list(self._shapes_layer.shape_type)
    
    def get_rectangle_bounds(self, shape_index):
        """
        Get bounds of a rectangle shape.
        
        Parameters
        ----------
        shape_index : int
            Index of the shape in the shapes list.
        
        Returns
        -------
        tuple or None
            (x0, y0, x1, y1) bounds of the rectangle, or None if invalid index.
        """
        if self._shapes_layer is None or shape_index >= len(self._shapes_layer.data):
            return None
        
        shape_data = self._shapes_layer.data[shape_index]
        # Rectangle is stored as 4 vertices
        x_coords = shape_data[:, 0]
        y_coords = shape_data[:, 1]
        
        x0, x1 = x_coords.min(), x_coords.max()
        y0, y1 = y_coords.min(), y_coords.max()
        
        return (x0, y0, x1, y1)
    
    def get_line_endpoints(self, shape_index):
        """
        Get endpoints of a line shape.
        
        Parameters
        ----------
        shape_index : int
            Index of the shape in the shapes list.
        
        Returns
        -------
        tuple or None
            ((x0, y0), (x1, y1)) endpoints of the line, or None if invalid index.
        """
        if self._shapes_layer is None or shape_index >= len(self._shapes_layer.data):
            return None
        
        shape_data = self._shapes_layer.data[shape_index]
        # Line is stored as 2 vertices
        if len(shape_data) >= 2:
            p0 = tuple(shape_data[0])
            p1 = tuple(shape_data[1])
            return (p0, p1)
        return None
    
    def clear_shapes(self):
        """Remove all shapes from the layer."""
        if self._shapes_layer is not None:
            self._shapes_layer.data = []

    # Large enough that the lines always extend well beyond any viewport, even
    # at extreme zoom-in.  1e6 image pixels is ~500× a typical 2048-px sensor.
    _CROSSHAIR_SPAN = 1e6
    _GRID_FRACTIONS = [0.25, 0.375, 0.50, 0.625, 0.75]

    def place_crosshair(self, row, col):
        """Draw a full-extent crosshair centred at (row, col) in the Shapes layer."""
        self._ensure_shapes_layer()
        s = self._CROSSHAIR_SPAN
        # Suppress the single-shape enforcement callback while we add two lines.
        self._processing_data_change = True
        try:
            self._shapes_layer.data = []
            self._shapes_layer.add_lines(
                [[[row - s, col], [row + s, col]],
                 [[row, col - s], [row, col + s]]],
                edge_color='yellow', edge_width=2,
            )
        finally:
            self._processing_data_change = False

    def draw_grid(self, H, W):
        """Draw a reference grid spanning an H×W region in the Shapes layer.

        H and W are in the Shapes layer's coordinates (world units), so callers
        must pass the image's *world* extent (pixels x scale), not its raw pixel
        shape — otherwise the grid is misplaced over a scaled image layer.
        """
        self._ensure_shapes_layer()
        s = self._CROSSHAIR_SPAN
        lines = []
        for f in self._GRID_FRACTIONS:
            lines.append([[f * H, -s],        [f * H, W + s]])   # horizontal
            lines.append([[-s,    f * W],      [H + s, f * W]])   # vertical
        self._processing_data_change = True
        try:
            self._shapes_layer.data = []
            self._shapes_layer.add_lines(lines, edge_color='yellow', edge_width=1)
        finally:
            self._processing_data_change = False
    
    def remove_shape(self, shape_index):
        """
        Remove a specific shape.
        
        Parameters
        ----------
        shape_index : int
            Index of the shape to remove.
        """
        if self._shapes_layer is not None and shape_index < len(self._shapes_layer.data):
            data = list(self._shapes_layer.data)
            data.pop(shape_index)
            self._shapes_layer.data = data
    
    def set_visible(self, visible):
        """
        Show or hide the shapes layer.
        
        Parameters
        ----------
        visible : bool
            True to show, False to hide.
        """
        if self._shapes_layer is not None:
            self._shapes_layer.visible = visible
    
    def get_layer(self):
        """
        Get the underlying napari Shapes layer.
        
        Returns
        -------
        napari.layers.Shapes
            The shapes layer (created if it doesn't exist).
        """
        self._ensure_shapes_layer()
        return self._shapes_layer


class NapariCrosshairOverlay:
    """Crosshair overlay using a dedicated napari Shapes layer.

    Drop-in replacement for VispyCrosshairVisual without private napari APIs.
    Position is set via place_at(row, col) on each click; defaults to image centre.
    """

    _PIXEL_WIDTH = 2  # desired line width in screen pixels

    def __init__(self, viewer, color='yellow'):
        self._viewer = viewer
        self._layer = None
        self._color = color
        self._shape = np.array([1.0, 1.0])
        self._cy = None  # None → centred on image
        self._cx = None
        try:
            self._viewer.camera.events.zoom.connect(self._on_zoom_changed)
        except Exception:
            pass

    def _get_edge_width(self):
        """Convert PIXEL_WIDTH to data-unit edge_width for the current zoom."""
        zoom = getattr(self._viewer.camera, 'zoom', 1.0) or 1.0
        return max(0.5, self._PIXEL_WIDTH / zoom)

    def _on_zoom_changed(self, event):
        if self._layer is not None and self._layer in self._viewer.layers \
                and self._layer.visible:
            self._redraw()

    def _ensure_layer(self):
        if self._layer is not None and self._layer in self._viewer.layers:
            return
        self._layer = self._viewer.add_shapes(
            name='_crosshair_overlay', edge_color=self._color,
            face_color=[0, 0, 0, 0], edge_width=self._get_edge_width())
        self._layer.mode = 'pan_zoom'

    def _redraw(self):
        H, W = float(self._shape[0]), float(self._shape[1])
        cy = self._cy if self._cy is not None else H / 2.0
        cx = self._cx if self._cx is not None else W / 2.0
        span = max(H, W) * 10.0
        ew = self._get_edge_width()
        self._layer.data = []
        self._layer.add_lines(
            [[[cy - span, cx], [cy + span, cx]],
             [[cy, cx - span], [cy, cx + span]]],
            edge_color=self._color, edge_width=ew)

    def place_at(self, row, col):
        """Position the crosshair at (row, col) in image data coordinates."""
        self._cy = float(row)
        self._cx = float(col)
        if self._layer is not None and self._layer in self._viewer.layers \
                and self._layer.visible:
            self._redraw()

    def update(self, shape):
        """Update the known image shape (used to compute span); redraws if visible."""
        self._shape = np.array(shape, dtype=float)
        if self._layer is not None and self._layer in self._viewer.layers \
                and self._layer.visible:
            self._redraw()

    def setVisible(self, value):
        if value:
            self._ensure_layer()
            self._redraw()
        if self._layer is not None and self._layer in self._viewer.layers:
            self._layer.visible = value

    def hide(self):
        self.setVisible(False)

    def show(self):
        self.setVisible(True)


class NapariGridOverlay:
    """Grid overlay using a dedicated napari Shapes layer.

    Drop-in replacement for VispyGridVisual without private napari APIs.
    """

    _FRACTIONS = [0.25, 0.375, 0.50, 0.625, 0.75]
    _PIXEL_WIDTH = 2  # desired line width in screen pixels

    def __init__(self, viewer, color='yellow'):
        self._viewer = viewer
        self._layer = None
        self._color = color
        self._shape = np.array([1.0, 1.0])
        try:
            self._viewer.camera.events.zoom.connect(self._on_zoom_changed)
        except Exception:
            pass

    def _get_edge_width(self):
        """Convert PIXEL_WIDTH to data-unit edge_width for the current zoom."""
        zoom = getattr(self._viewer.camera, 'zoom', 1.0) or 1.0
        return max(0.5, self._PIXEL_WIDTH / zoom)

    def _on_zoom_changed(self, event):
        if self._layer is not None and self._layer in self._viewer.layers \
                and self._layer.visible:
            self._redraw()

    def _ensure_layer(self):
        if self._layer is not None and self._layer in self._viewer.layers:
            return
        self._layer = self._viewer.add_shapes(
            name='_grid_overlay', edge_color=self._color,
            face_color=[0, 0, 0, 0], edge_width=self._get_edge_width())
        self._layer.mode = 'pan_zoom'

    def _redraw(self):
        H, W = float(self._shape[0]), float(self._shape[1])
        lines = []
        for f in self._FRACTIONS:
            lines.append([[f * H, 0], [f * H, W]])
            lines.append([[0, f * W], [H, f * W]])
        ew = self._get_edge_width()
        self._layer.data = []
        self._layer.add_lines(lines, edge_color=self._color, edge_width=ew)

    def update(self, shape):
        self._shape = np.array(shape, dtype=float)
        if self._layer is not None and self._layer in self._viewer.layers \
                and self._layer.visible:
            self._redraw()

    def setVisible(self, value):
        if value:
            self._ensure_layer()
            self._redraw()
        if self._layer is not None and self._layer in self._viewer.layers:
            self._layer.visible = value

    def hide(self):
        self.setVisible(False)

    def show(self):
        self.setVisible(True)


class NapariROISetOverlay:
    """Draws a managed ROI *set* into the viewer, read-only.

    Named for the set rather than "ROI" because ``NapariROIOverlay`` above is
    already taken by imcontrol's single detector-ROI rectangle — a different
    thing entirely, and one this must not shadow.

    Separate from ``ViewerToolManager``'s scratch layer: that one is where the
    user draws, this one is what has been committed.  Keeping them apart is
    what makes "draw, then Add" work the way ImageJ's does, and it is why
    clearing a scratch shape after capture no longer makes the ROI disappear.

    Three properties are load-bearing:

    * **multipart.**  An ROI is a *list* of polygons, not one: a composite can
      be disconnected, and a region with a hole has an inner boundary as well
      as an outer one.  Every part carries the ROI's uid, so clicking any of
      them selects the one ROI.
    * **read-only.**  The layer is not editable and re-renders from the model
      if anything mutates it.  Dragging a vertex here would desynchronise the
      overlay from the records with no undo; editing an ROI is a command.
    * **target-aligned.**  Shape coordinates are the ROI's own pixel
      coordinates, and the layer carries the target image layer's transform.
      There is therefore one coordinate representation, not two kept in step.
    """

    LAYER_NAME = 'ROI Manager'
    _PIXEL_WIDTH = 2

    #: Distinct, colour-blind-safe defaults cycled by ROI index.
    _COLOR_CYCLE = (
        '#ffcc00', '#00b7eb', '#ff6f61', '#7ac74f',
        '#c77dff', '#ff9f1c', '#4cc9f0', '#f72585',
    )
    _SELECTED_COLOR = '#ffffff'

    def __init__(self, viewer):
        self._viewer = viewer
        self._layer = None
        self._rois = []
        self._selected = set()
        self._show_labels = False
        self._visible = True
        self._target_layer = None
        self._default_style = None
        self._failures: list[str] = []
        self._suppress_resync = False
        self._click_handler = None
        # Remembered so remove() can undo them: an overlay that leaves its
        # viewer callbacks connected keeps being driven after its panel is gone.
        self._zoom_signal = None
        try:
            self._zoom_signal = self._viewer.camera.events.zoom
            self._zoom_signal.connect(self._on_zoom_changed)
        except Exception:
            self._zoom_signal = None

    # -- appearance ---------------------------------------------------------

    def _get_edge_width(self):
        zoom = getattr(self._viewer.camera, 'zoom', 1.0) or 1.0
        return max(0.5, self._PIXEL_WIDTH / zoom)

    def _on_zoom_changed(self, _event=None):
        if self._layer is not None and self._rois:
            try:
                self._layer.edge_width = self._get_edge_width()
            except Exception:
                pass

    def _style_for(self, index, roi):
        """The ROI's own style over the set's default, with a colour fallback."""
        from imswitch.imcommon.algorithms.roi_style import ROIStyle

        style = getattr(roi, 'style', None) or ROIStyle()
        style = style.merged_with(self._default_style)
        if not style.stroke_color:
            # Group members share a colour so a group reads as one thing.
            key = roi.group if getattr(roi, 'group', 0) else index
            style = dataclasses.replace(
                style, stroke_color=self._COLOR_CYCLE[key % len(self._COLOR_CYCLE)]
            )
        if roi.uid in self._selected:
            style = dataclasses.replace(style, stroke_color=self._SELECTED_COLOR)
        return style

    @staticmethod
    def _rgba(color, opacity):
        """A colour with an alpha, for the fill napari expects as RGBA."""
        if not color:
            return [0.0, 0.0, 0.0, 0.0]
        try:
            from napari.utils.colormaps.standardize_color import transform_color

            rgba = np.asarray(transform_color(color)[0], dtype=float)
        except Exception:
            return [0.0, 0.0, 0.0, float(opacity or 0.0)]
        rgba[3] = float(opacity or 0.0)
        return rgba.tolist()

    # -- layer lifecycle ----------------------------------------------------

    def _ensure_layer(self):
        if self._layer is not None and self._layer in getattr(self._viewer, 'layers', []):
            return self._layer
        self._layer = self._viewer.add_shapes(
            name=self.LAYER_NAME,
            edge_color=self._COLOR_CYCLE[0],
            face_color=[0, 0, 0, 0],
            edge_width=self._get_edge_width(),
            ndim=2,
        )
        try:
            # editable=False also forces mode to pan_zoom, so the user cannot
            # drag the overlay out of step with the model.
            self._layer.editable = False
        except Exception:
            pass
        try:
            self._layer.mode = 'pan_zoom'
        except Exception:
            pass
        try:
            self._layer.events.data.connect(self._on_layer_data_changed)
        except Exception:
            pass
        return self._layer

    def _on_layer_data_changed(self, _event=None):
        """Re-render from the model if anything edited the layer.

        The overlay is a view of the records; anything that changed it did not
        change them, so the records win.
        """
        if self._suppress_resync:
            return
        self.refresh()

    def remove(self):
        """Take the overlay out of the viewer and undo every connection.

        Removing the layer is not enough on its own: the camera-zoom handler
        and the click handler are connected to objects that outlive the panel,
        so leaving them attached keeps a dead overlay being driven.
        """
        self.remove_click_handler()
        if self._zoom_signal is not None:
            try:
                self._zoom_signal.disconnect(self._on_zoom_changed)
            except Exception:
                # Teardown runs while things are being dismantled; a signal
                # that cannot be disconnected must not stop the rest of it.
                pass
            self._zoom_signal = None
        if self._layer is not None:
            try:
                self._layer.events.data.disconnect(self._on_layer_data_changed)
            except Exception:
                pass
            try:
                self._viewer.layers.remove(self._layer)
            except Exception:
                pass
        self._layer = None

    # -- state --------------------------------------------------------------

    def set_target_layer(self, layer):
        """Align the overlay with the image being measured."""
        self._target_layer = layer
        self.refresh()

    def set_rois(self, rois, *, selected_uids=(), default_style=None):
        self._rois = list(rois)
        self._selected = set(selected_uids or ())
        if default_style is not None:
            self._default_style = default_style
        self.refresh()

    def set_selection(self, uids):
        self._selected = set(uids or ())
        self.refresh()

    def set_labels_visible(self, show: bool):
        self._show_labels = bool(show)
        self.refresh()

    def set_visible(self, visible: bool):
        """Show All. Hiding keeps the ROI list; it only stops drawing it."""
        self._visible = bool(visible)
        if self._layer is not None:
            try:
                self._layer.visible = self._visible
            except Exception:
                pass
        self.refresh()

    # -- rendering ----------------------------------------------------------

    def refresh(self):
        from imswitch.imcommon.algorithms.roi_geometry import roi_outline

        drawn = [roi for roi in self._rois if getattr(roi, 'visible', True)]
        if not self._visible or not drawn:
            if self._layer is not None:
                self._set_layer_data([], [], [], [], [], [], labels=[])
            return

        layer = self._ensure_layer()
        self._align_to_target(layer)

        shapes, edges, fills, widths, uids, parts, labels = [], [], [], [], [], [], []
        for index, roi in enumerate(drawn):
            try:
                outline = roi_outline(roi)
            except Exception:
                # An ROI whose geometry cannot be traced is skipped rather
                # than taking the whole overlay down with it.
                continue
            if not outline:
                continue
            style = self._style_for(index, roi)
            # One label per ROI, on its largest part. napari's text is
            # per-shape, so the other parts carry an empty string rather than
            # repeating the name once per fragment — a disconnected ROI would
            # otherwise be labelled three times.
            biggest = max(range(len(outline)), key=lambda i: len(outline[i]))
            show_label = self._show_labels and style.label_visible
            width = style.stroke_width
            for part_index, part in enumerate(outline):
                shapes.append(np.asarray(part, dtype=float))
                edges.append(style.stroke_color)
                fills.append(self._rgba(style.fill_color, style.fill_opacity))
                widths.append(
                    float(width) if width else self._get_edge_width()
                )
                uids.append(roi.uid)
                parts.append(part_index)
                labels.append(
                    roi.name if (show_label and part_index == biggest) else ""
                )

        self._set_layer_data(shapes, edges, fills, widths, uids, parts, labels=labels)

    def _align_to_target(self, layer):
        """Copy the target image layer's *displayed-plane* transform.

        The overlay is 2D — ROI records are 2D — so an nD target's transform
        cannot be copied wholesale: assigning a 4-element scale to a 2D layer
        fails, and swallowing that failure left the overlay at identity while
        the image sat at 0.1 µm/px, putting every drawn ROI in the wrong place.
        Only the last two components, which are the plane the ROIs live on, are
        taken, and a failure is recorded rather than ignored.

        The alternative — pre-transforming every vertex into world space —
        means keeping two representations of the same coordinates in step, and
        they drift the moment the target's scale changes.
        """
        target = self._target_layer
        if target is None or layer is None:
            return
        for attribute in ('scale', 'translate'):
            value = getattr(target, attribute, None)
            if value is None:
                continue
            try:
                trimmed = tuple(float(v) for v in value)[-2:]
            except TypeError:
                continue
            if len(trimmed) == 2:
                self._apply(layer, attribute, trimmed)

        # A rotation or shear on the displayed plane is carried as the 2x2
        # spatial block of the target's affine.
        affine = getattr(target, 'affine', None)
        matrix = None
        for name in ('affine_matrix', 'matrix'):
            candidate = getattr(affine, name, None)
            if candidate is not None:
                matrix = np.asarray(candidate, dtype=float)
                break
        if matrix is not None and matrix.ndim == 2 and matrix.shape[0] >= 3:
            plane = np.eye(3)
            plane[:2, :2] = matrix[-3:-1, -3:-1]
            plane[:2, 2] = matrix[-3:-1, -1]
            if not np.allclose(plane, np.eye(3)):
                self._apply(layer, 'affine', plane)

    def _set_layer_data(self, shapes, edges, fills, widths, uids, parts, labels=None):
        layer = self._layer
        if layer is None:
            if not shapes:
                return
            layer = self._ensure_layer()
        self._suppress_resync = True
        try:
            layer.data = list(shapes)
            if shapes:
                for attribute, value in (
                    ('shape_type', ['polygon'] * len(shapes)),
                    ('edge_color', list(edges)),
                    ('face_color', list(fills)),
                    # Per shape, so one ROI's width does not set everyone's.
                    ('edge_width', list(widths)),
                ):
                    self._apply(layer, attribute, value)
                self._apply(
                    layer, 'features',
                    {'roi_uid': list(uids), 'part_index': list(parts)},
                )
                self._apply(
                    layer, 'text',
                    {'string': list(labels), 'anchor': 'center'} if any(labels or [])
                    else None,
                )
        finally:
            self._suppress_resync = False

    def _apply(self, layer, attribute, value):
        """Set one layer attribute, reporting rather than swallowing failure.

        Silently ignoring these left the overlay looking fine while sitting at
        the wrong transform or without its ROI ids — the sort of failure that
        only shows up as measurements landing in the wrong place.
        """
        try:
            setattr(layer, attribute, value)
            return True
        except Exception as exc:
            self._failures.append(f'{attribute}: {exc}')
            return False

    @property
    def failures(self) -> list[str]:
        """Layer attributes that could not be applied, for diagnosis."""
        return list(self._failures)

    # -- hit testing --------------------------------------------------------

    def roi_at(self, data_position, *, tolerance=None):
        """The uid of the ROI under a point, or None.

        Ours rather than napari's: ``Shapes.get_value`` returns only the
        topmost shape by draw order, which cannot pick the smallest of several
        overlapping ROIs and cannot fall through the hole in an annulus.
        """
        from imswitch.imcommon.algorithms.roi_geometry import roi_hit_test, roi_mask_local

        if tolerance is None:
            zoom = getattr(self._viewer.camera, 'zoom', 1.0) or 1.0
            tolerance = max(self._PIXEL_WIDTH / zoom, 0.5)

        hits = []
        for roi in self._rois:
            if not getattr(roi, 'visible', True):
                continue
            try:
                if not roi_hit_test(roi, data_position, tolerance=tolerance):
                    continue
                local, _slices = roi_mask_local(roi, (10 ** 6, 10 ** 6))
                area = int(local.sum())
            except Exception:
                continue
            hits.append((area, roi.uid))
        if not hits:
            return None
        # Smallest wins: a small ROI inside a big one is the one you meant.
        return min(hits, key=lambda item: item[0])[1]

    def world_to_data(self, world_position):
        """Map a world-space event position into ROI pixel coordinates."""
        layer = self._layer
        if layer is None:
            return tuple(float(v) for v in world_position[-2:])
        try:
            return tuple(
                float(v) for v in np.asarray(layer.world_to_data(world_position))[-2:]
            )
        except Exception:
            return tuple(float(v) for v in world_position[-2:])

    def install_click_handler(self, on_roi_clicked):
        """Call ``on_roi_clicked(uid, modifiers)`` when a drawn ROI is clicked.

        A plain mouse callback plus our own hit test: napari's ``get_value``
        would only ever report the topmost shape, which cannot express
        "smallest of the overlapping ones" or "the hole is not part of it".
        """
        layer = self._ensure_layer()

        def _handler(_layer, event):
            try:
                data_point = self.world_to_data(event.position)
                uid = self.roi_at(data_point)
            except Exception:
                return
            if uid is not None:
                on_roi_clicked(uid, getattr(event, "modifiers", ()))

        try:
            layer.mouse_drag_callbacks.append(_handler)
        except Exception:
            return None
        self._click_handler = _handler
        return _handler

    def remove_click_handler(self):
        handler = getattr(self, "_click_handler", None)
        if handler is None or self._layer is None:
            return
        try:
            self._layer.mouse_drag_callbacks.remove(handler)
        except Exception:
            pass
        self._click_handler = None

    @property
    def layer(self):
        return self._layer
