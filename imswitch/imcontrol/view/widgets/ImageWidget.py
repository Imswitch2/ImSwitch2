import contextlib
import re
import time
import weakref

import napari
import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.algorithms.transforms.conventions import pixel_matrix_to_world
from imswitch.imcommon.model import shortcut
from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol.model.scan_frame import (
    SCAN_GEOMETRY_METADATA_KEY,
    ScanFrame,
)
from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService


class ImageWidget(QtWidgets.QWidget):
    sigTransformRegistrationTargetsChanged = QtCore.Signal()

    """ Widget containing viewbox that displays the new detector frames. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        naparitools.addNapariGrayclipColormap()
        self.napariViewer = naparitools.EmbeddedNapari()
        self.updateLevelsWidget = naparitools.NapariUpdateLevelsWidget.addToViewer(
            self.napariViewer
        )
        # Under the levels tool: a Sum checkbox per dimension the live frames
        # have beyond their image (line steps, z, t), shown only then.
        self.sumAxesWidget = naparitools.NapariSumAxesWidget.addToViewer(self.napariViewer)
        #LR: The next to widgets are etSTED specific and i.m.o. not needed --> at least for now commented
        #self.NapariResetViewWidget = naparitools.NapariResetViewWidget.addToViewer(self.napariViewer, 'right')
        #self.NapariSumImageWidget = naparitools.NapariSumImageWidget.addToViewer(self.napariViewer, 'right')
        self._initViewerModel()

        self.viewCtrlLayout = QtWidgets.QVBoxLayout()
        self.viewCtrlLayout.setContentsMargins(0, 0, 0, 0)

        nw = self.napariViewer.get_widget()
        # Napari's _qt_window is a QMainWindow that restores its own saved size
        # from preferences and has a large minimumSizeHint() from its dock layout.
        # setMinimumSize(0, 0) only clears an *explicit* minimum -- Qt still
        # asks minimumSizeHint() afterwards -- so the hint itself has to go
        # (see minimumSizeHint() below), or the viewer forces the ImSwitch
        # window bigger than the screen.
        nw.setMinimumSize(0, 0)
        nw.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        self.viewCtrlLayout.addWidget(nw)
        self.setLayout(self.viewCtrlLayout)

    def _initViewerModel(self):
        """Everything about the viewer that is not a Qt dock: the live and
        result layer bookkeeping and the viewer events this widget follows.

        Split from ``__init__`` so the headless tests can build the widget on
        a canvas-less ``napari.components.ViewerModel`` (no OpenGL needed)
        and run exactly this code; ``__init__`` sets ``napariViewer`` and the
        docked ``sumAxesWidget`` first.
        """
        self.sigSumAxesChanged = self.sumAxesWidget.sigSumAxesChanged
        self.imgLayers = {}
        # Result layers other controllers keep in this viewer, updated in
        # place: {jobName: {layerName: layer}} -- see setResultLayers -- and
        # the napari layer type each was created as, which decides whether
        # it is padded when the viewer enters 3D mode.
        self.resultLayers = {}
        self.resultLayerTypes = {}
        self._liveSourceRegistrations = {}
        self._layerRegistrationBaselines = {}
        self._transformRegistrationOverlayConfigured = False
        # Napari emits layer-list events in the middle of live-layer
        # recreation. Suppress those transient events so a persistent logical
        # source registration is not mistaken for a real source deletion.
        self._suspendRegistrationTargetSignals = 0

        # The viewer's tool broker, and the one ViewerToolManager behind it.
        # Viewer Tools draws through the manager and the Line Profile panel
        # through the broker; two managers on the one "Viewer Tools" layer
        # each reacted to the other's shapes — a crosshair counted as the
        # profile's line and was pruned as a duplicate.
        self.toolService = ViewerToolService.for_viewer(self.napariViewer)
        self.toolManager = self.toolService.manager

        # When the viewer switches to 3D display mode all live-view layers
        # must have at least 3 dimensions; otherwise napari's extent
        # computation (extent.data[:, displayed_axes]) raises IndexError for
        # 2D layers while the user slides through a 3D scan result.
        self.napariViewer.dims.events.ndisplay.connect(self._on_ndisplay_changed)
        try:
            self.napariViewer.layers.events.inserted.connect(
                self._registrationTargetsChanged
            )
            self.napariViewer.layers.events.removed.connect(
                self._registrationTargetsChanged
            )
        except AttributeError:
            pass

        # Follow acquisition: during a scan the sliders follow the plane the
        # scan writes (followLivePlane). Moving a slider yourself turns it off
        # until the next run starts; unticking the box keeps it off.
        self._followAcquisition = True
        self._followOffByUser = False
        self._scanActive = False
        self._followingDims = 0
        self._followedAxes = set()
        self._lastDims = None
        try:
            self.napariViewer.dims.events.current_step.connect(self._onDimsStepChanged)
        except AttributeError:
            pass
        followToggled = getattr(self.sumAxesWidget, 'sigFollowToggled', None)
        if followToggled is not None:
            followToggled.connect(self._onFollowToggled)

        # Layers napari's own buttons add (new points / new shapes) are fitted
        # to the image they are drawn on; see _onLayerInserted.
        self._currentLiveName = None
        self._adoptingMarkers = 0
        self._markerSizes = naparitools.ScreenSizedPoints(self.napariViewer)
        try:
            self.napariViewer.layers.events.inserted.connect(self._onLayerInserted)
        except AttributeError:
            pass

    # -- following the scan; stale stacks ------------------------------------

    @property
    def followAcquisition(self):
        return self._followAcquisition

    def setFollowAcquisition(self, on, *, byUser=True):
        """Follow the plane being written, or not. ``byUser`` False is the
        automatic switch-off (a slider moved during a scan), which the next
        run undoes; an untick by the user is kept across runs."""
        self._followAcquisition = bool(on)
        if byUser:
            self._followOffByUser = not on
        setter = getattr(self.sumAxesWidget, 'setFollowChecked', None)
        if setter is not None:
            setter(on)

    def _onFollowToggled(self, on):
        self.setFollowAcquisition(on, byUser=True)

    def setScanActive(self, active):
        """A scan run starts (True) or ends (False). At the start, following
        comes back on unless the user unticked it."""
        self._scanActive = bool(active)
        self._followedAxes = set()
        if active and not self._followOffByUser:
            self.setFollowAcquisition(True, byUser=False)

    def _dimsState(self):
        dims = self.napariViewer.dims
        return tuple(dims.nsteps), tuple(dims.current_step)

    def _onDimsStepChanged(self, event=None):
        """A slider the user moves during a scan turns following off.

        Only a move counts: a change that comes with a change of the axes'
        ranges (a layer added, rebuilt or reshaped) is napari placing the
        sliders, and a change this widget makes is following."""
        state = self._dimsState()
        previous, self._lastDims = self._lastDims, state
        if self._followingDims or previous is None:
            return
        if not (self._scanActive and self._followAcquisition):
            return
        if previous[0] != state[0]:
            return
        moved = {axis for axis, (a, b) in enumerate(zip(previous[1], state[1])) if a != b}
        if moved & self._followedAxes:
            self.setFollowAcquisition(False, byUser=False)

    def followLivePlane(self, name, index):
        """Put the sliders of live layer ``name``'s leading axes on ``index``
        (the plane the scan is writing), while following is on."""
        if index is None or not self._followAcquisition:
            return
        layer = self.imgLayers.get(name)
        if layer is None or not any(item is layer for item in self.napariViewer.layers):
            return
        lead = int(layer.ndim) - 2
        index = tuple(int(i) for i in index)
        if len(index) > lead:
            return
        index = (0,) * (lead - len(index)) + index          # padded for 3D display
        dims = self.napariViewer.dims
        offset = int(dims.ndim) - int(layer.ndim)
        scale = tuple(float(v) for v in layer.scale)
        translate = tuple(float(v) for v in layer.translate)
        displayed = set(dims.displayed)
        self._followingDims += 1
        try:
            for k, i in enumerate(index):
                axis = offset + k
                if axis < 0 or axis in displayed:
                    continue
                self._followedAxes.add(axis)
                world = translate[k] + i * scale[k]
                if not np.isclose(dims.point[axis], world):
                    dims.set_point(axis, world)
        finally:
            self._followingDims -= 1
            self._lastDims = self._dimsState()

    def collapseLiveLayerToShownPlane(self, name):
        """Reduce live layer ``name`` to the 2-D plane it shows now.

        For a detector that does not take part in a scan: its stack from an
        earlier scan otherwise stays, and keeps the viewer's slider. The
        plane's pixels are copied (the detector may reuse its buffer), the
        layer is rebuilt at 2-D with its display settings, and its scan
        geometry is cleared. Returns ``(old shape, plane index)``, or None
        when the layer has no stack to reduce.
        """
        layer = self.imgLayers.get(name)
        if layer is None or not any(item is layer for item in self.napariViewer.layers):
            return None
        data = layer.data
        shape = tuple(np.shape(data))
        lead = len(shape) - 2
        if lead <= 0 or all(size == 1 for size in shape[:lead]):
            return None
        dims = self.napariViewer.dims
        offset = int(dims.ndim) - len(shape)
        scale = tuple(float(v) for v in layer.scale)
        translate = tuple(float(v) for v in layer.translate)
        index = []
        for k in range(lead):
            axis = offset + k
            try:
                position = (float(dims.point[axis]) - translate[k]) / (scale[k] or 1.0)
            except (IndexError, TypeError):
                position = 0.0
            index.append(int(min(max(round(position), 0), shape[k] - 1)))
        plane = np.array(np.asarray(data)[tuple(index)], copy=True)
        self.setImage(name, plane, scale[-2:], scanGeometry=None)
        return shape, tuple(index)

    # -- layers napari's own buttons add ---------------------------------

    #: Names napari gives the layers of its "new points" / "new shapes"
    #: buttons (``Points``, ``Shapes [1]``, ...).
    _NAPARI_NEW_LAYER_NAME = re.compile(r'(Points|Shapes)( \[\d+\])?')

    def _onLayerInserted(self, event):
        layer = getattr(event, 'value', None)
        if layer is None or self._adoptingMarkers:
            return
        if self._isNewMarkersLayer(layer):
            self._fitMarkersLayer(layer)
            return
        liveName = self._duplicatedLiveName(layer)
        if liveName is not None:
            self._freezeLiveCopy(layer, liveName)

    # -- napari's Duplicate Layer on a live layer ------------------------

    def _duplicatedLiveName(self, layer):
        """The detector whose live layer ``layer`` is napari's duplicate of,
        else None. napari's Duplicate Layer records the original as the new
        layer's ``source.parent`` and names it ``<name> copy``."""
        if not isinstance(layer, napari.layers.Image):
            return None
        parent = getattr(getattr(layer, 'source', None), 'parent', None)
        if callable(parent):
            parent = parent()
        if parent is None:
            return None
        for name, live in self.imgLayers.items():
            if live is parent:
                if str(layer.name).startswith(f'{live.name} copy'):
                    return name
                return None
        return None

    def _freezeLiveCopy(self, layer, liveName):
        """Make napari's duplicate of a live layer a frozen copy.

        As napari makes it, the copy sits on top of the live layer with the
        live layer's additive blending -- the frozen image is added onto the
        live one -- is selected, so the levels button and the Line Profile's
        *Active* layer move to it, and is named ``Live: <det> copy``. It is
        renamed ``<det> (copy HH:MM:SS)``, drawn translucent, hidden, and put
        just under its live layer, and the selection goes back to what it
        was. Show it with its eye to compare (hide the live layer, or the live
        image is added over it). It is an ordinary layer: not protected, not a
        live source, kept until deleted.
        """
        selection = self.napariViewer.layers.selection
        before = [item for item in selection if item is not layer]
        active = selection.active if selection.active is not layer else None
        try:
            layer.name = f'{liveName} (copy {time.strftime("%H:%M:%S")})'
            layer.protected = False
            layer.blending = 'translucent'
            layer.visible = False
        except Exception:
            pass
        ref = weakref.ref(layer)
        QtCore.QTimer.singleShot(
            0, lambda: self._placeFrozenCopy(ref, liveName, before, active)
        )

    def _placeFrozenCopy(self, ref, liveName, before, active):
        layer = ref()
        layers = self.napariViewer.layers
        live = self.imgLayers.get(liveName)
        if layer is None or not any(item is layer for item in layers):
            return
        try:
            if live is not None and any(item is live for item in layers):
                index, liveIndex = layers.index(layer), layers.index(live)
                if index > liveIndex:
                    layers.move(index, liveIndex)
            selection = layers.selection
            if active is not None and any(item is active for item in layers):
                selection.active = active
            elif before:
                selection.clear()
                selection.update(item for item in before
                                 if any(other is item for other in layers))
        except Exception:
            pass

    def _isNewMarkersLayer(self, layer):
        """An empty Points or Shapes layer under napari's default name, made
        from nothing (a duplicate or a conversion has a source parent)."""
        if not isinstance(layer, (napari.layers.Points, napari.layers.Shapes)):
            return False
        if not self._NAPARI_NEW_LAYER_NAME.fullmatch(str(getattr(layer, 'name', ''))):
            return False
        try:
            if len(layer.data):
                return False
        except TypeError:
            return False
        parent = getattr(getattr(layer, 'source', None), 'parent', None)
        return parent is None

    def _markersReference(self):
        """The image a new markers layer is drawn on: the image layer that was
        active when it was added (what napari's button took its scale from),
        else the current detector's live layer, else the first visible live
        layer with an image in it."""
        layers = self.napariViewer.layers

        def present(layer):
            return layer is not None and any(item is layer for item in layers)

        def hasImage(layer):
            return int(np.prod(np.shape(getattr(layer, 'data', ())))) > 1

        active = getattr(layers.selection, 'active', None)
        if isinstance(active, napari.layers.Image) and present(active):
            return active
        current = self.imgLayers.get(self._currentLiveName)
        if present(current) and hasImage(current):
            return current
        for layer in self.imgLayers.values():
            if present(layer) and getattr(layer, 'visible', True) and hasImage(layer):
                return layer
        return None

    def _markersPlacement(self):
        """``(ndim, scale, translate)`` for a new markers layer: the displayed
        plane's dimensionality (2, or 3 in 3D display), at the reference
        image's scale and offset on those axes."""
        ndim = max(2, self._ndisplay())
        reference = self._markersReference()
        scale, translate = [1.0] * ndim, [0.0] * ndim
        if reference is not None:
            refScale = [float(v) for v in tuple(reference.scale)]
            refTranslate = [float(v) for v in tuple(reference.translate)]
            for k in range(1, min(ndim, len(refScale)) + 1):
                scale[-k] = refScale[-k]
                translate[-k] = refTranslate[-k]
        return ndim, tuple(scale), tuple(translate)

    def _fitMarkersLayer(self, layer):
        """Fit an empty layer from napari's new points/shapes button to the
        image it is drawn on.

        napari makes it as many-dimensional as the selected layer (a z-stack
        gives a 3-D markers layer that keeps the viewer 3-D, with a slider,
        after the scan returns to 2-D), at scale 1 when nothing is selected
        (napari's default point size 10 is then 10 µm). Here it
        gets the displayed plane's dimensionality -- a 2-D marker is shown on
        every plane -- the image's scale, and points a few screen pixels
        across (kept so on zoom).

        Scale and size are set in place. A layer of the wrong dimensionality
        cannot be changed in place (napari fixes ``ndim`` at creation), so it
        is replaced by an equal empty layer under the same name at the same
        place, once napari has finished adding it (next event-loop turn).
        Nothing has been drawn in it by then; a script holding the original
        object (``viewer.add_points()`` with no data and napari's default
        name) would be left with a removed layer -- name the layer to opt out.
        """
        ndim, scale, translate = self._markersPlacement()
        if int(layer.ndim) == ndim:
            self._styleMarkersLayer(layer, scale, translate)
            return
        ref = weakref.ref(layer)
        placement = (ndim, scale, translate)
        QtCore.QTimer.singleShot(0, lambda: self._replaceMarkersLayer(ref, placement))

    def _styleMarkersLayer(self, layer, scale, translate):
        try:
            layer.scale = scale
            layer.translate = translate
        except Exception:
            pass
        if isinstance(layer, napari.layers.Points):
            self._markerSizes.add(layer)
        else:
            try:
                layer.current_edge_width = naparitools.screenPointSize(
                    self.napariViewer, layer, 2
                )
            except Exception:
                pass

    def _replaceMarkersLayer(self, ref, placement):
        layer = ref()
        layers = self.napariViewer.layers
        if layer is None or not any(item is layer for item in layers):
            return
        try:
            if len(layer.data):
                return             # something was drawn already: leave it
        except TypeError:
            return
        ndim, scale, translate = placement
        selection = layers.selection
        wasActive = selection.active is layer
        index = layers.index(layer)
        mode = getattr(layer, 'mode', None)
        add = (self.napariViewer.add_points
               if isinstance(layer, napari.layers.Points)
               else self.napariViewer.add_shapes)
        self._adoptingMarkers += 1
        try:
            layers.remove(layer)
            new = add(ndim=ndim, scale=scale, translate=translate, name=layer.name)
            newIndex = layers.index(new)
            if newIndex != index:
                layers.move(newIndex, index)
        finally:
            self._adoptingMarkers -= 1
        self._styleMarkersLayer(new, scale, translate)
        if mode is not None:
            try:
                new.mode = mode
            except Exception:
                pass
        if wasActive:
            selection.active = new

    #: The smallest viewer worth showing. Napari's embedded window adds up the
    #: minimums of its own docks, which on a populated viewer is larger than
    #: the space ImSwitch has left for it, and reporting that upwards is part
    #: of what makes the application window open taller than the screen.
    #: Capped rather than dropped to zero: a previous attempt to report no
    #: minimum at all collapsed the dock layout during startup, and this keeps
    #: the viewer weighing something while still fitting any screen.
    minimumViewerSize = (240, 180)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        width, height = self.minimumViewerSize
        return QtCore.QSize(min(hint.width(), width), min(hint.height(), height))

    def _removeProtectedLayer(self, layer):
        """Remove a layer from the viewer, bypassing the protected-layer guard.

        ``EmbeddedNapari`` monkey-patches the layer list's ``_delitem_indices``
        to filter out layers marked ``protected=True``.  Older napari versions
        accepted a ``force=True`` kwarg on ``LayerList.remove`` to override
        that, but the keyword no longer exists in current napari — passing it
        raises TypeError, and dropping it leaves protected layers in place
        forever (the next ``add_image`` then auto-suffixes the new layer to
        ``Live: Camera [1]``).  Clear the flag first instead.
        """
        try:
            layer.protected = False
        except AttributeError:
            pass
        try:
            self.napariViewer.layers.remove(layer)
        except ValueError:
            # Already gone from the layer list — fine.
            pass

    def setLiveViewLayers(self, names):
        names = tuple(names)
        self._suspendRegistrationTargetSignals += 1
        try:
            for name, layer in self.imgLayers.items():
                if name not in names:
                    self._removeProtectedLayer(layer)

            def addImage(name, colormap=None):
                self.imgLayers[name] = self.napariViewer.add_image(
                    np.zeros((1, 1)), rgb=False, name=f'Live: {name}',
                    blending='additive', colormap=colormap, protected=True
                )

            for name in names:
                if (name not in self.imgLayers
                        or self.imgLayers[name] not in self.napariViewer.layers):
                    try:
                        addImage(name, name.lower())
                    except KeyError:
                        addImage(name, 'grayclip')
                    self._applyLiveSourceRegistration(name)
        finally:
            self._suspendRegistrationTargetSignals -= 1
        self._registrationTargetsChanged()

    def _registrationTargetsChanged(self, event=None):
        if self._suspendRegistrationTargetSignals:
            return
        self._updateTransformRegistrationOverlay()
        self.sigTransformRegistrationTargetsChanged.emit()

    def _on_ndisplay_changed(self, event=None):
        """Pad every layer of ours to ndisplay dims when the viewer enters 3D mode.

        Live-view layers and result layers alike: a result layer showing the
        latest plane of a reconstruction is 2D, and it used to be the one
        layer left out, so switching to 3D with a live reconstruction shown
        failed the way the live view once did.
        """
        ndisplay = self.napariViewer.dims.ndisplay
        for name, layer in list(self.imgLayers.items()):
            if layer.data.ndim >= ndisplay:
                continue
            data, scale = self._padToNdisplay(layer.data, tuple(layer.scale), ndisplay)
            # ndim change ⇒ must recreate; see comment in setImage().
            self._recreateLiveLayer(name, data, scale)
        self._padResultLayersToNdisplay(ndisplay)

    @staticmethod
    def _padToNdisplay(data, scale, ndisplay):
        """``data`` with singleton axes prepended until it has ``ndisplay``
        dims, and ``scale`` (fitted to the data first) padded to match.

        The new axes take the smallest existing scale so they stay visually
        negligible: 1.0 would distort napari's 3D bounding box when the X/Y
        pixel pitch is << 1 µm and bloat the rendered image.
        """
        if not hasattr(data, 'ndim'):
            data = np.asarray(data)
        ndim = int(data.ndim)
        if scale is not None:
            scale = ImageWidget._fitScale(scale, ndim)
        if ndim >= int(ndisplay):
            return data, scale
        pad_scale = min(scale) if scale else 1.0
        while data.ndim < int(ndisplay):
            data = data[np.newaxis]
            if scale is not None:
                scale = (pad_scale,) + scale
        return data, scale

    def _recreateLiveLayer(self, name, im, scale):
        """Replace a live-view layer in place, preserving display properties.

        Mandatory when the layer's ndim has to change.  napari has no API to
        atomically grow/shrink a layer's ndim:

          * ``layer.scale = (longer_tuple)`` raises ValueError inside
            transform_utils.py — the scale setter materialises into
            ``np.ones(layer.ndim)`` and broadcasts the new tuple into it,
            so any scale longer than the current ndim fails immediately.
          * ``layer.data = different_ndim_array`` triggers ``_update_dims``,
            which fires ``events.set_data`` synchronously; downstream
            handlers index the still-mismatched affine and raise
            IndexError on ``_world_to_layer_units_scale[displayed_axes]``.

        Recreating preserves contrast limits, colormap, blending, gamma,
        opacity, visibility, list position and the layer selection (napari
        selects every layer it adds, so a rebuild used to take the selection
        from whatever the user was working on -- a markers layer in add mode
        stopped receiving clicks). The cost is a brief flicker.
        """
        old = self.imgLayers[name]
        layers = self.napariViewer.layers
        try:
            index = layers.index(old)
        except ValueError:
            index = None

        properties = dict(
            name=old.name,
            blending=old.blending,
            colormap=old.colormap.name,
            contrast_limits=tuple(old.contrast_limits),
            rgb=False,
            scale=scale,
            protected=True,
        )
        for key in ('gamma', 'opacity'):
            value = getattr(old, key, None)
            if value is not None:
                properties[key] = value

        self._suspendRegistrationTargetSignals += 1
        replaced = {}
        try:
            with self._selectionKept(replaced):
                self._removeProtectedLayer(old)
                new = self.napariViewer.add_image(im, **properties)
                replaced[id(old)] = new
                try:
                    new.visible = old.visible
                except AttributeError:
                    pass
                if index is not None:
                    try:
                        new_index = layers.index(new)
                        if new_index != index:
                            layers.move(new_index, index)
                    except (ValueError, IndexError):
                        pass
                self.imgLayers[name] = new
                self._applyLiveSourceRegistration(name)
        finally:
            self._suspendRegistrationTargetSignals -= 1
        self._registrationTargetsChanged()

    @contextlib.contextmanager
    def _selectionKept(self, replaced):
        """Give the layer selection back after layers are rebuilt in the block.

        napari makes every layer it adds the selected, active one. A rebuild
        is not the user choosing a layer, so whatever was selected and active
        before stays so afterwards; a rebuilt layer is followed to its
        replacement through ``replaced`` (``id(old) -> new``, filled by the
        block). A layer in an add mode keeps its mode and, being active
        again, keeps receiving the clicks.
        """
        selection = getattr(getattr(self.napariViewer, 'layers', None), 'selection', None)
        if selection is None:
            yield
            return
        before = list(selection)
        active = selection.active
        current = getattr(selection, '_current', None)
        try:
            yield
        finally:
            layers = self.napariViewer.layers

            def follow(layer):
                if layer is None:
                    return None
                layer = replaced.get(id(layer), layer)
                return layer if any(item is layer for item in layers) else None

            try:
                if active is not None and follow(active) is not None:
                    selection.active = follow(active)
                else:
                    selection.clear()
                    selection.update(
                        layer for layer in map(follow, before) if layer is not None
                    )
                    if follow(current) is not None:
                        selection._current = follow(current)
            except Exception:
                pass      # a selection napari refuses is not worth a frame

    def addStaticLayer(self, name, im, scale=None):
        """Add one more image layer; every call adds a new one (a snap)."""
        kwargs = dict(rgb=False, name=name, blending='additive')
        if scale is not None:
            kwargs['scale'] = self._fitScale(scale, im.ndim)
        self.napariViewer.add_image(im, **kwargs)

    @staticmethod
    def _fitScale(scale, ndim):
        """``scale`` with one value per axis: the last values given are the
        last axes', missing leading axes get 1."""
        sc = tuple(float(v) for v in scale)
        if len(sc) < ndim:
            sc = (1.0,) * (ndim - len(sc)) + sc
        elif len(sc) > ndim:
            sc = sc[-ndim:]
        return sc

    def upsertStaticLayer(self, name, im, scale=None, colormap=None, rgb=False,
                          contrast_limits=None):
        """See :meth:`_upsertStaticLayer`; a product arriving in the
        background (the Lifetime image, once a second) does not take the
        layer selection."""
        replaced = {}
        old = self._findLayerByName(name)
        with self._selectionKept(replaced):
            layer = self._upsertStaticLayer(name, im, scale, colormap, rgb,
                                            contrast_limits)
            if old is not None:
                replaced[id(old)] = layer
        return layer

    def _upsertStaticLayer(self, name, im, scale=None, colormap=None, rgb=False,
                           contrast_limits=None):
        """Create the named layer, or update its data in place.

        ``addStaticLayer`` makes a *new* layer on every call (napari then
        suffixes ``[1]``, ``[2]``...); live products that arrive once a
        second need one stable layer per name instead. ``rgb`` layers take a
        ``(..., 3)`` image. A layer whose ndim or kind changed is recreated.
        """
        im = np.asarray(im)
        spatial_ndim = im.ndim - 1 if rgb else im.ndim
        sc = self._fitScale(scale, spatial_ndim) if scale is not None else None
        layer = self._findLayerByName(name)
        if layer is not None:
            kind_changed = bool(getattr(layer, 'rgb', False)) != bool(rgb)
            ndim_changed = getattr(layer.data, 'ndim', None) != im.ndim
            if kind_changed or ndim_changed:
                self._removeProtectedLayer(layer)
                layer = None
        if layer is None:
            kwargs = dict(rgb=bool(rgb), name=name,
                          blending='additive' if not rgb else 'translucent')
            if sc is not None:
                kwargs['scale'] = sc
            if colormap is not None and not rgb:
                kwargs['colormap'] = colormap
            if contrast_limits is not None and not rgb:
                kwargs['contrast_limits'] = tuple(float(v) for v in contrast_limits)
            return self.napariViewer.add_image(im, **kwargs)
        if sc is not None:
            try:
                layer.scale = sc
            except Exception:
                pass
        layer.data = im
        if not rgb:
            try:
                if colormap is not None:
                    layer.colormap = colormap
                if contrast_limits is not None:
                    layer.contrast_limits = tuple(float(v) for v in contrast_limits)
            except Exception:
                pass
        return layer

    def removeStaticLayer(self, name):
        """Remove the named layer; a missing one is not an error."""
        layer = self._findLayerByName(name)
        if layer is None:
            return False
        self._removeProtectedLayer(layer)
        return True

    def hasStaticLayer(self, name):
        return self._findLayerByName(name) is not None

    def getActiveImageLayerSnapshot(self):
        """Snapshot the selected Napari Image layer for offline localization.

        Only the selected 2-D image is used: no detector, acquisition or
        image-display intensity transformations are involved.  The layer is
        returned as well so an overlay can be placed in its world coordinates.
        """
        viewer = self.napariViewer
        layer = getattr(viewer, 'active_layer', None)
        if layer is None:
            layer = viewer.layers.selection.active
        if not isinstance(layer, napari.layers.Image):
            raise ValueError('select an Image layer in Napari before localizing')
        if getattr(layer, 'rgb', False):
            raise ValueError('select a grayscale image layer, not an RGB image')
        image = np.asarray(layer.data)
        if image.ndim != 2:
            raise ValueError(
                f'active Napari layer {layer.name!r} must be 2-D '
                f'(got shape {image.shape})'
            )
        if not np.issubdtype(image.dtype, np.number):
            raise ValueError('active Napari image must have numeric pixel values')
        return np.array(image, copy=True), layer

    def getLiveLayerSnapshot(self, detectorName):
        layer = self.imgLayers.get(detectorName)
        if layer is None or layer not in self.napariViewer.layers:
            raise KeyError(f"live detector layer {detectorName!r} is unavailable")
        image = np.asarray(layer.data)
        if image.ndim != 2:
            raise ValueError(
                f"live detector {detectorName!r} is not a 2D image after squeeze "
                f"(shape {image.shape})"
            )
        return np.array(image, copy=True), tuple(layer.scale[-2:])

    def getTransformRegistrationTargets(self):
        targets = []
        for detectorName, layer in self.imgLayers.items():
            if layer in self.napariViewer.layers:
                targets.append((
                    f"Live source: {detectorName}",
                    {"kind": "live_source", "name": str(detectorName)},
                ))

        live_layer_ids = {id(layer) for layer in self.imgLayers.values()}
        for layer in self.napariViewer.layers:
            if id(layer) in live_layer_ids:
                continue
            if not isinstance(layer, napari.layers.Image):
                continue
            name = getattr(layer, 'name', 'image')
            targets.append((
                f"Layer: {name}",
                {"kind": "layer", "name": str(name), "layer_id": id(layer)},
            ))
        return targets

    def applyTransformRegistration(self, target, pixel_matrix, target_scale=None):
        if not isinstance(target, dict):
            raise TypeError("registration target must be a mapping")
        kind = target.get("kind")
        name = target.get("name")
        if kind == "live_source":
            self.setLiveSourceRegistration(str(name), pixel_matrix, target_scale)
            return
        if kind == "layer":
            self._setLayerTargetRegistration(target, pixel_matrix, target_scale)
            return
        raise ValueError(f"unknown registration target kind {kind!r}")

    def clearTransformRegistration(self, target):
        if not isinstance(target, dict):
            return
        kind = target.get("kind")
        name = target.get("name")
        if kind == "live_source":
            self.clearLiveSourceRegistration(str(name))
        elif kind == "layer":
            self._clearLayerTargetRegistration(target)

    def setLiveSourceRegistration(self, detectorName, pixel_matrix, target_scale=None):
        self._liveSourceRegistrations[str(detectorName)] = (
            np.asarray(pixel_matrix, dtype=np.float64).copy(),
            None if target_scale is None else tuple(float(v) for v in target_scale),
        )
        self._applyLiveSourceRegistration(str(detectorName))
        self._updateTransformRegistrationOverlay()

    def clearLiveSourceRegistration(self, detectorName):
        detectorName = str(detectorName)
        self._liveSourceRegistrations.pop(detectorName, None)
        layer = self.imgLayers.get(detectorName)
        if layer is not None and layer in self.napariViewer.layers:
            layer.affine = np.eye(int(layer.ndim) + 1)
        self._updateTransformRegistrationOverlay()

    def _applyLiveSourceRegistration(self, detectorName):
        entry = self._liveSourceRegistrations.get(str(detectorName))
        layer = self.imgLayers.get(str(detectorName))
        if entry is None or layer is None or layer not in self.napariViewer.layers:
            return
        pixel_matrix, target_scale = entry
        world = pixel_matrix_to_world(
            self._asPlaneAffine(pixel_matrix), getattr(layer, 'scale', None), target_scale
        )
        layer.affine = self._embedPlaneAffine(world, int(layer.ndim))

    def setLayerRegistration(self, layerName, pixel_matrix, target_scale=None):
        """Compatibility helper for callers that only know a current layer name."""
        target = {"kind": "layer", "name": str(layerName)}
        self._setLayerTargetRegistration(target, pixel_matrix, target_scale)

    def clearLayerRegistration(self, layerName):
        """Compatibility helper for callers that only know a current layer name."""
        target = {"kind": "layer", "name": str(layerName)}
        self._clearLayerTargetRegistration(target)

    def _setLayerTargetRegistration(self, target, pixel_matrix, target_scale=None):
        layer = self._findLayerForRegistrationTarget(target)
        if layer is None:
            raise KeyError(f"napari layer {target.get('name')!r} no longer exists")
        key = id(layer)
        if key not in self._layerRegistrationBaselines:
            self._layerRegistrationBaselines[key] = (
                weakref.ref(layer),
                self._layerAffineMatrix(layer),
            )
        baseline = self._layerRegistrationBaselines[key][1]
        world = pixel_matrix_to_world(
            self._asPlaneAffine(pixel_matrix), getattr(layer, 'scale', None), target_scale
        )
        registration = self._embedPlaneAffine(world, int(layer.ndim))
        layer.affine = registration @ baseline
        self._updateTransformRegistrationOverlay()

    def _clearLayerTargetRegistration(self, target):
        layer = self._findLayerForRegistrationTarget(target)
        if layer is None:
            layer_id = target.get("layer_id") if isinstance(target, dict) else None
            if layer_id is not None:
                self._layerRegistrationBaselines.pop(int(layer_id), None)
            self._updateTransformRegistrationOverlay()
            return
        entry = self._layerRegistrationBaselines.pop(id(layer), None)
        if entry is not None:
            layer.affine = entry[1]
        self._updateTransformRegistrationOverlay()

    def _transformRegistrationTextOverlay(self):
        """Return napari's viewer-anchored text overlay when available.

        ImSwitch currently pins napari 0.7.x, where ``viewer.text_overlay`` is
        the public compatibility accessor.  Keep the newer canvas path as a
        harmless fallback so this warning does not become an upgrade blocker.
        """
        overlay = getattr(self.napariViewer, 'text_overlay', None)
        if overlay is not None:
            return overlay
        canvas = getattr(self.napariViewer, 'canvas', None)
        overlays = getattr(canvas, 'overlays', None)
        if overlays is None:
            return None
        try:
            return overlays.text
        except (AttributeError, KeyError):
            try:
                return overlays['text']
            except (KeyError, TypeError):
                return None

    def _activeTransformRegistrationLabels(self):
        """Return concise labels for registrations currently owned by this viewer."""
        labels = [
            f"Live: {name}"
            for name in sorted(self._liveSourceRegistrations)
        ]

        stale = []
        static_labels = []
        for layer_id, (layer_ref, _baseline) in self._layerRegistrationBaselines.items():
            layer = layer_ref()
            if layer is None or layer not in self.napariViewer.layers:
                stale.append(layer_id)
                continue
            static_labels.append(f"Layer: {getattr(layer, 'name', 'image')}")
        for layer_id in stale:
            self._layerRegistrationBaselines.pop(layer_id, None)
        labels.extend(sorted(static_labels))
        return labels

    @staticmethod
    def _formatTransformRegistrationOverlay(labels):
        labels = tuple(str(label) for label in labels if str(label))
        if not labels:
            return ""
        return "DISPLAY TRANSFORM ACTIVE\n" + "\n".join(labels)

    def _updateTransformRegistrationOverlay(self):
        """Keep an always-in-view warning synchronized with display registration.

        This is deliberately a *viewer canvas* warning rather than a layer or
        image annotation: it stays visible while panning/zooming, like napari's
        scale bar, and names the affected layer/source when only part of the
        viewer is transformed.  The warning follows the actual ImageWidget
        registration state, not the Transform Calibration checkbox.
        """
        overlay = self._transformRegistrationTextOverlay()
        if overlay is None:
            return
        text = self._formatTransformRegistrationOverlay(
            self._activeTransformRegistrationLabels()
        )
        if not self._transformRegistrationOverlayConfigured:
            try:
                overlay.position = 'top_left'
                overlay.color = 'yellow'
                overlay.font_size = 14
            except (AttributeError, TypeError, ValueError):
                # Text/visibility are the important compatibility contract;
                # styling differs slightly across napari releases.
                pass
            self._transformRegistrationOverlayConfigured = True
        overlay.text = text
        overlay.visible = bool(text)

    def _findLayerForRegistrationTarget(self, target):
        if not isinstance(target, dict):
            return None
        layer_id = target.get("layer_id")
        if layer_id is not None:
            for layer in self.napariViewer.layers:
                if id(layer) == int(layer_id):
                    return layer
            return None
        return self._findLayerByName(str(target.get("name", "")))

    @staticmethod
    def _asPlaneAffine(matrix):
        matrix = np.asarray(matrix, dtype=np.float64)
        if matrix.shape == (3, 3):
            return matrix
        if matrix.shape == (2, 3):
            result = np.eye(3, dtype=np.float64)
            result[:2, :] = matrix
            return result
        raise ValueError(f"expected a 2D affine matrix, got shape {matrix.shape}")

    @staticmethod
    def _embedPlaneAffine(plane, ndim):
        plane = ImageWidget._asPlaneAffine(plane)
        ndim = int(ndim)
        if ndim < 2:
            raise ValueError("image layer must have at least 2 dimensions")
        if ndim == 2:
            return plane.copy()
        result = np.eye(ndim + 1, dtype=np.float64)
        result[-3:-1, -3:-1] = plane[:2, :2]
        result[-3:-1, -1] = plane[:2, 2]
        return result

    @staticmethod
    def _layerAffineMatrix(layer):
        affine = getattr(layer, 'affine', None)
        if affine is None:
            return np.eye(int(layer.ndim) + 1, dtype=np.float64)
        matrix = getattr(affine, 'affine_matrix', affine)
        return np.asarray(matrix, dtype=np.float64).copy()

    def setStaticLayer(self, name, im, scale=None):
        """Shows ``im`` in the layer called ``name``: replaces the data of
        that layer when there is one (keeping its colormap, blending and
        visibility, and fitting the contrast to the new data), and adds the
        layer when there is not. Returns whether the layer is new."""
        layers = self.napariViewer.layers
        existing = layers[name] if name in layers else None
        if existing is not None and (
            getattr(existing, 'protected', False)
            or getattr(existing, 'ndim', None) != im.ndim
            or not hasattr(existing, 'contrast_limits')
        ):
            # A live layer is never replaced; another kind of layer, or one of
            # another dimensionality (napari cannot change a layer's ndim in
            # place, see _recreateLiveLayer), is replaced by a new one.
            if getattr(existing, 'protected', False):
                raise ValueError(f'{name!r} is a live-view layer; use another name')
            self._removeProtectedLayer(existing)
            existing = None
        if existing is None:
            self.addStaticLayer(name, im, scale)
            return True
        existing.data = im
        if scale is not None:
            existing.scale = self._fitScale(scale, im.ndim)
        existing.reset_contrast_limits()
        return False

    # -- result layers ---------------------------------------------------

    _RESULT_LAYER_ADDERS = {
        'image': 'add_image',
        'labels': 'add_labels',
        'points': 'add_points',
        'shapes': 'add_shapes',
    }
    #: Layer types whose data is an image array, padded to the displayed
    #: dims like the live view (points and shapes are coordinate lists).
    _PADDED_RESULT_LAYER_TYPES = ('image', 'labels')

    def _ndisplay(self):
        try:
            return int(self.napariViewer.dims.ndisplay)
        except AttributeError:
            return 2

    def setResultLayers(self, jobName, layerData):
        """See :meth:`_setResultLayers`; a result stream (live
        reconstruction) creating or rebuilding its layers does not take the
        layer selection."""
        replaced = {}
        before = dict(self.resultLayers.get(jobName, {}))
        with self._selectionKept(replaced):
            created = self._setResultLayers(jobName, layerData)
            for name, layer in self.resultLayers.get(jobName, {}).items():
                if name in before:
                    replaced[id(before[name])] = layer
        return created

    def _setResultLayers(self, jobName, layerData):
        """Create or update the result layers of ``jobName`` in place.

        ``layerData`` is a list of napari ``(data, kwargs, layerType)`` tuples
        in display order. A layer is created once, keyed by job and layer
        name, and afterwards only its data (and scale) are swapped, so a live
        update costs a copy rather than a layer rebuild and keeps whatever
        contrast, colormap and visibility the user set. A layer whose ndim
        changes is recreated (napari cannot grow a layer's dims in place, see
        :meth:`_recreateLiveLayer`), and one the user deleted from the layer
        list comes back on the next update. Layers of the job that are absent
        from ``layerData`` are removed. Image data is padded to the viewer's
        displayed dims (see :meth:`_on_ndisplay_changed`), so a 2D plane
        arriving while the viewer is in 3D mode keeps updating the padded
        layer in place. Returns whether any layer was created (rather than
        updated), so a caller can fit the view once.
        """
        existing = self.resultLayers.setdefault(jobName, {})
        types = self.resultLayerTypes.setdefault(jobName, {})
        ndisplay = self._ndisplay()
        seen = set()
        created = False
        for data, kwargs, layerType in layerData:
            kwargs = dict(kwargs or {})
            layerType = str(layerType or 'image')
            name = str(kwargs.get('name') or jobName)
            kwargs['name'] = name
            seen.add(name)
            if layerType in self._PADDED_RESULT_LAYER_TYPES:
                data, scale = self._padToNdisplay(data, kwargs.get('scale'), ndisplay)
                if scale is not None:
                    kwargs['scale'] = scale
            layer = existing.get(name)
            if layer is not None and layer not in self.napariViewer.layers:
                layer = None
            if layer is not None and self._canUpdateInPlace(layer, data, layerType):
                scale = kwargs.get('scale')
                if scale is not None:
                    layer.scale = self._fitScale(scale, layer.ndim)
                layer.data = data
                existing[name] = layer
                types[name] = layerType
                continue
            if layer is not None:
                self._removeResultLayer(layer)
            existing[name] = self._addResultLayer(data, kwargs, layerType)
            types[name] = layerType
            created = True
        for name in list(existing):
            if name not in seen:
                self._removeResultLayer(existing.pop(name))
                types.pop(name, None)
        return created

    def removeResultLayers(self, jobName):
        """Remove every layer :meth:`setResultLayers` created for ``jobName``."""
        self.resultLayerTypes.pop(jobName, None)
        for layer in self.resultLayers.pop(jobName, {}).values():
            self._removeResultLayer(layer)

    def resultLayerNames(self, jobName):
        """The names of the layers currently held for ``jobName``."""
        return list(self.resultLayers.get(jobName, {}))

    def _padResultLayersToNdisplay(self, ndisplay):
        replaced = {}
        before = {(job, name): layer for job, layers in self.resultLayers.items()
                  for name, layer in layers.items()}
        with self._selectionKept(replaced):
            self._padResultLayersToNdisplayImpl(ndisplay)
            for job, layers in self.resultLayers.items():
                for name, layer in layers.items():
                    if (job, name) in before:
                        replaced[id(before[(job, name)])] = layer

    def _padResultLayersToNdisplayImpl(self, ndisplay):
        """Recreate image-like result layers with fewer than ``ndisplay`` dims.

        Napari cannot grow a layer's dims in place (:meth:`_recreateLiveLayer`
        says why), so the layer is rebuilt with its display properties and
        its place in the layer list carried over, exactly as a live-view
        layer is.
        """
        for jobName, layers in self.resultLayers.items():
            types = self.resultLayerTypes.get(jobName, {})
            for name, layer in list(layers.items()):
                layerType = types.get(name, 'image')
                if layerType not in self._PADDED_RESULT_LAYER_TYPES:
                    continue
                if layer not in self.napariViewer.layers:
                    continue
                data = layer.data
                if int(getattr(data, 'ndim', np.ndim(data))) >= int(ndisplay):
                    continue
                data, scale = self._padToNdisplay(data, tuple(layer.scale), ndisplay)
                kwargs = self._carriedLayerProperties(layer, layerType)
                kwargs['scale'] = scale
                viewerLayers = self.napariViewer.layers
                try:
                    index = viewerLayers.index(layer)
                except ValueError:
                    index = None
                self._removeResultLayer(layer)
                new = self._addResultLayer(data, kwargs, layerType)
                if index is not None:
                    try:
                        newIndex = viewerLayers.index(new)
                        if newIndex != index:
                            viewerLayers.move(newIndex, index)
                    except (AttributeError, ValueError, IndexError):
                        pass
                layers[name] = new

    @staticmethod
    def _carriedLayerProperties(layer, layerType):
        """The display properties a rebuilt result layer keeps."""
        props = {'name': layer.name}
        for key in ('blending', 'opacity', 'visible'):
            value = getattr(layer, key, None)
            if value is not None:
                props[key] = value
        if layerType == 'image':
            colormap = getattr(getattr(layer, 'colormap', None), 'name', None)
            if colormap:
                props['colormap'] = colormap
            limits = getattr(layer, 'contrast_limits', None)
            if limits is not None:
                props['contrast_limits'] = tuple(limits)
            gamma = getattr(layer, 'gamma', None)
            if gamma is not None:
                props['gamma'] = gamma
        return props

    @staticmethod
    def _canUpdateInPlace(layer, data, layerType):
        if layerType not in ('image', 'labels', 'points'):
            return False
        ndim = int(getattr(data, 'ndim', np.ndim(data)))
        if layerType == 'points':
            return ndim == 2 and int(data.shape[1]) == int(layer.ndim)
        return ndim == int(layer.data.ndim)

    def _addResultLayer(self, data, kwargs, layerType):
        adder = getattr(self.napariViewer, self._RESULT_LAYER_ADDERS.get(layerType, 'add_image'))
        if layerType == 'image':
            kwargs.setdefault('blending', 'additive')
            kwargs.setdefault('rgb', False)
        scale = kwargs.get('scale')
        if scale is not None and layerType != 'points':
            kwargs['scale'] = self._fitScale(scale, int(getattr(data, 'ndim', np.ndim(data))))
        return adder(data, **kwargs)

    def _removeResultLayer(self, layer):
        try:
            layer.protected = False
        except AttributeError:
            pass
        try:
            self.napariViewer.layers.remove(layer)
        except ValueError:
            pass

    def getCurrentImageName(self):
        return self.napariViewer.active_layer.name

    def setSummableAxes(self, name, labels, summed=None):
        """The dimensions live layer ``name``'s frames have beyond their
        image, outermost first, and (when given) which dimensions the view
        sums, by name."""
        self.sumAxesWidget.setAxes(name, labels, summed)

    def getImage(self, name):
        return self.imgLayers[name].data

    def getScanGeometry(self, name):
        """Geometry of the scan pixels the live layer ``name`` shows, if any
        (a :class:`~imswitch.imcontrol.model.scan_frame.DisplayedScanGeometry`)."""
        layer = self.imgLayers.get(name)
        if layer is None:
            return None
        return layer.metadata.get(SCAN_GEOMETRY_METADATA_KEY)

    def _setScanGeometry(self, name, scanGeometry):
        # Filed in the same call that set the layer's pixels, so the layer's
        # geometry is always that of what it shows. A frame without one clears
        # it: the pixels on screen no longer belong to any scan geometry.
        metadata = self.imgLayers[name].metadata
        if scanGeometry is None:
            metadata.pop(SCAN_GEOMETRY_METADATA_KEY, None)
        else:
            metadata[SCAN_GEOMETRY_METADATA_KEY] = scanGeometry

    def setImage(self, name, im, scale, scanGeometry=None):
        # napari moving the sliders while it takes a new frame (a rebuild, a
        # new range) is not the user moving them; see _onDimsStepChanged.
        self.__dict__['_followingDims'] = self.__dict__.get('_followingDims', 0) + 1
        try:
            self._setImage(name, im, scale, scanGeometry)
        finally:
            self._followingDims -= 1
        if '_lastDims' in self.__dict__:
            self._lastDims = self._dimsState()

    def _setImage(self, name, im, scale, scanGeometry=None):
        layer = self.imgLayers[name]
        scale = tuple(scale)
        if isinstance(im, ScanFrame):
            # napari gets a plain view of the same pixels; the geometry is
            # filed on the layer below.
            im = im.view(np.ndarray)

        # Normalise scale length to match im.ndim.
        if len(scale) < im.ndim:
            scale = (1.0,) * (im.ndim - len(scale)) + scale
        elif len(scale) > im.ndim:
            scale = scale[-im.ndim:]

        # Every layer must have at least ndisplay dimensions, otherwise napari
        # indexes displayed_axes into extent.data[:, displayed_axes] (shape
        # (2, ndim)) and raises IndexError.  Pad with the smallest existing
        # scale so the new singleton axes stay visually negligible (using 1.0
        # distorts the 3D bounding box and bloats the rendered image when the
        # real X/Y pixel pitch is << 1 µm).
        ndisplay = self.napariViewer.dims.ndisplay
        if im.ndim < ndisplay:
            pad_scale = min(scale) if scale else 1.0
            while im.ndim < ndisplay:
                im = im[np.newaxis]
                scale = (pad_scale,) + scale

        # If the layer's current ndim differs from the incoming image, in-place
        # mutation is not possible in this napari version — see the docstring
        # of _recreateLiveLayer for the full explanation.  Briefly:
        #   * scale-first fails because the scale setter validates
        #     len(scale) <= layer.ndim and raises ValueError otherwise
        #     (transform_utils.py:138, "could not broadcast … into shape").
        #   * data-first fails because the data setter synchronously fires
        #     events.set_data with a still-mismatched affine.
        # Recreate is the only path that lets napari rebuild its dims from
        # scratch.  Only hits on the first frame of an ndim transition.
        if layer.data.ndim != im.ndim:
            self._recreateLiveLayer(name, im, scale)
            self._setScanGeometry(name, scanGeometry)
            return

        # Same-ndim update: scale-before-data is safe because the scale setter
        # accepts a tuple matching the current ndim, and the data setter then
        # uses the already-correct scale length when rebuilding the world↔layer
        # transform.
        layer.scale = scale
        layer.data = im
        self._setScanGeometry(name, scanGeometry)
        self._applyLiveSourceRegistration(name)

    def clearImage(self, name):
        self.setImage(name, np.zeros((1, 1)))

    def getImageDisplayLevels(self, name):
        return self.imgLayers[name].contrast_limits

    def setImageDisplayLevels(self, name, minimum, maximum):
        self.imgLayers[name].contrast_limits = (minimum, maximum)

    def getCenterViewbox(self):
        """ Returns the center point of the viewbox in DATA-PIXEL coordinates,
        as an (x, y) tuple.

        ``camera.center`` is in world units (the image layer is drawn scaled by
        the detector pixel size), so divide by the current overlay pixel scale.
        Overlay ROIs keep their geometry in data pixels, so a center in the same
        units lands them correctly regardless of pixel size. """
        center = self.napariViewer.camera.center
        sx, sy = getattr(self, '_lastOverlayScale', (1.0, 1.0))
        return (center[2] / sx, center[1] / sy)

    def setOverlayPixelScale(self, scale, detectorName=None):
        """ Push the current image layer's pixel scale to overlay ROIs so they
        render in data-pixel units aligned to the (scaled) image.

        ``scale`` is the layer scale in (..., row/Y, col/X) order; ROI axes are
        (x=col, y=row). Stored so a ROI added later (overlays attach on demand)
        picks up the right scale immediately. """
        if detectorName is not None:
            self._currentLiveName = str(detectorName)
        if scale is None or len(scale) < 2:
            return
        sx, sy = float(scale[-1]), float(scale[-2])
        self._lastOverlayScale = (sx, sy)
        for item in getattr(self, '_overlayItems', []):
            setter = getattr(item, 'setPixelScale', None)
            if setter is not None:
                setter((sx, sy))

    def resetView(self):
        self.napariViewer.reset_view()

    def addItem(self, item):
        try:
            _canvas = self.napariViewer.window.qt_viewer.canvas
            _view = (getattr(_canvas, 'view', None)
                     or getattr(self.napariViewer.window.qt_viewer, 'view', None))
            _parent = _view.scene if _view else None
        except AttributeError:
            _canvas = None
            _view = None
            _parent = None
        item.attach(self.napariViewer,
                    canvas=_canvas,
                    view=_view,
                    parent=_parent,
                    order=1e6 + 8000)
        # Track overlays and apply the last known pixel scale right away, so a
        # ROI added after the image is already showing renders aligned without
        # waiting for the next frame.
        overlays = getattr(self, '_overlayItems', None)
        if overlays is None:
            overlays = self._overlayItems = []
        if item not in overlays:
            overlays.append(item)
        scale = getattr(self, '_lastOverlayScale', None)
        setter = getattr(item, 'setPixelScale', None)
        if scale is not None and setter is not None:
            setter(scale)

    def removeItem(self, item):
        item.detach()
        overlays = getattr(self, '_overlayItems', None)
        if overlays is not None and item in overlays:
            overlays.remove(item)

    def getLayerVisibilityState(self):
        """Return JSON-safe napari layer visibility without storing layer data."""
        layers = {}
        for layer in self.napariViewer.layers:
            name = getattr(layer, 'name', None)
            if name:
                layers[name] = bool(getattr(layer, 'visible', True))

        liveLayers = {}
        for detectorName, layer in self.imgLayers.items():
            liveLayers[detectorName] = bool(getattr(layer, 'visible', True))

        return {
            'layers': layers,
            'liveLayers': liveLayers,
        }

    def applyLayerVisibilityState(self, state):
        """Apply saved napari visibility to layers that currently exist."""
        warnings = []
        if not isinstance(state, dict):
            return ['Layer visibility state is not a mapping; skipped.']

        liveLayers = state.get('liveLayers', {})
        if isinstance(liveLayers, dict):
            for detectorName, visible in liveLayers.items():
                layer = self.imgLayers.get(detectorName)
                if layer is not None:
                    self._setLayerVisible(layer, visible)
        else:
            warnings.append('Saved live layer visibility is not a mapping; skipped.')

        layers = state.get('layers', {})
        if isinstance(layers, dict):
            for layerName, visible in layers.items():
                layer = self._findLayerByName(layerName)
                if layer is not None:
                    self._setLayerVisible(layer, visible)
        else:
            warnings.append('Saved layer visibility is not a mapping; skipped.')

        return warnings

    def setVisibleLayers(self, detectorNames):
        """Show only the named live detector layers."""
        visibleDetectorNames = set(detectorNames or ())
        for detectorName, layer in self.imgLayers.items():
            self._setLayerVisible(layer, detectorName in visibleDetectorNames)

    def _findLayerByName(self, layerName):
        for layer in self.napariViewer.layers:
            if getattr(layer, 'name', None) == layerName:
                return layer
        return None

    @staticmethod
    def _setLayerVisible(layer, visible):
        try:
            layer.visible = bool(visible)
        except AttributeError:
            pass

    @shortcut(actionId="image.updateLevels", defaultKey="Ctrl+U",
              displayName="Update levels", initiallyBound=True)
    def updateLevelsButton(self):
        self.updateLevelsWidget.updateLevelsButton.click()


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
