"""P-1: Show All / Labels — the visible half of the ROI manager (D-02).

The overlay is read-only, multipart, and aligned with the image being
measured. Hit testing is ours rather than napari's, because
``Shapes.get_value`` returns only the topmost shape.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord, new_uid
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask, roi_from_vertices
from imswitch.imcommon.view.guitools.naparitools import NapariROISetOverlay


def test_the_overlay_does_not_shadow_imcontrols_detector_roi():
    """Two different things: imcontrol's single detector rectangle, and the
    ROI manager's committed set. Defining both under one name broke every
    imcontrol settings test."""
    from imswitch.imcommon.view.guitools import naparitools

    assert naparitools.NapariROIOverlay is not naparitools.NapariROISetOverlay
    naparitools.NapariROIOverlay()  # imcontrol constructs it with no arguments


class _Event:
    def __init__(self):
        self._handlers = []

    def connect(self, fn):
        self._handlers.append(fn)

    def emit(self, *args):
        for fn in list(self._handlers):
            fn(*args)

    def disconnect(self, fn):
        if fn in self._handlers:
            self._handlers.remove(fn)


class _Shapes:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "ROI Manager")
        self.data = []
        self.shape_type = []
        self.edge_color = []
        self.features = {}
        self.text = None
        self.mode = "pan_zoom"
        self.visible = True
        self.editable = True
        self.edge_width = 1.0
        self.scale = (1.0, 1.0)
        self.translate = (0.0, 0.0)
        self.mouse_drag_callbacks = []
        self.events = SimpleNamespace(data=_Event(), mode=_Event())

    def world_to_data(self, point):
        point = np.asarray(point, dtype=float)
        scale = np.asarray(self.scale, dtype=float)[-len(point):]
        translate = np.asarray(self.translate, dtype=float)[-len(point):]
        return (point - translate) / scale


class _ImageLayer:
    def __init__(self, shape=(64, 64), scale=(1.0, 1.0), translate=(0.0, 0.0)):
        self.name = "Reconstruction"
        self.data = np.zeros(shape)
        self.scale = scale
        self.translate = translate
        self.ndim = len(shape)
        self.visible = True
        self.metadata = {}


class _Viewer:
    def __init__(self):
        self.layers = []
        self.camera = SimpleNamespace(zoom=1.0, events=SimpleNamespace(zoom=_Event()))

    def add_shapes(self, **kwargs):
        layer = _Shapes(**kwargs)
        self.layers.append(layer)
        return layer


def _roi(name, bounds, **kwargs):
    return ROIRecord(name, "rectangle", bounds, uid=new_uid(), **kwargs)


@pytest.fixture
def overlay():
    return NapariROISetOverlay(_Viewer())


# --------------------------------------------------------------------------
# D-02 — ROIs are actually drawn
# --------------------------------------------------------------------------

def test_rois_are_drawn_in_the_viewer(overlay):
    overlay.set_rois([_roi("a", (0, 4, 0, 4)), _roi("b", (8, 12, 8, 12))])

    assert overlay.layer is not None
    assert len(overlay.layer.data) == 2


def test_hidden_rois_are_not_drawn(overlay):
    overlay.set_rois(
        [_roi("shown", (0, 4, 0, 4)), _roi("hidden", (8, 12, 8, 12), visible=False)]
    )

    assert len(overlay.layer.data) == 1


def test_show_all_off_draws_nothing_but_keeps_the_set(overlay):
    rois = [_roi("a", (0, 4, 0, 4))]
    overlay.set_rois(rois)

    overlay.set_visible(False)
    assert len(overlay.layer.data) == 0

    overlay.set_visible(True)
    assert len(overlay.layer.data) == 1


# --------------------------------------------------------------------------
# multipart rendering (F-16)
# --------------------------------------------------------------------------

def test_a_composite_with_a_hole_draws_both_boundaries_under_one_uid(overlay):
    mask = np.zeros((20, 20), dtype=bool)
    mask[3:15, 3:15] = True
    mask[7:11, 7:11] = False
    annulus = roi_from_mask(mask, name="annulus")
    annulus = ROIRecord(
        annulus.name, annulus.roi_type, annulus.bounds,
        source=annulus.source, mask=annulus.mask, uid=new_uid(),
    )

    overlay.set_rois([annulus])

    assert len(overlay.layer.data) >= 2, "a hole has its own boundary"
    uids = set(overlay.layer.features["roi_uid"])
    assert uids == {annulus.uid}, "every part belongs to the one ROI"


def test_labels_are_one_per_roi_not_one_per_part(overlay):
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:5, 2:5] = True
    mask[12:15, 12:15] = True
    blobs = roi_from_mask(mask, name="two-blobs")
    blobs = ROIRecord(
        blobs.name, blobs.roi_type, blobs.bounds,
        source=blobs.source, mask=blobs.mask, uid=new_uid(),
    )

    overlay.set_rois([blobs])
    overlay.set_labels_visible(True)

    # napari's text is per-shape, so the name sits on the ROI's largest part
    # and the other parts carry an empty string. Repeating it per fragment
    # would label a two-blob ROI twice.
    assert len(overlay.layer.data) >= 2
    strings = overlay.layer.text["string"]
    assert len(strings) == len(overlay.layer.data), "one text entry per shape"
    assert [text for text in strings if text] == ["two-blobs"]


# --------------------------------------------------------------------------
# read-only (F-16)
# --------------------------------------------------------------------------

def test_overlay_is_not_editable(overlay):
    overlay.set_rois([_roi("a", (0, 4, 0, 4))])

    assert overlay.layer.editable is False
    assert overlay.layer.mode == "pan_zoom"


def test_tampering_with_the_layer_resyncs_from_the_model(overlay):
    overlay.set_rois([_roi("a", (0, 4, 0, 4)), _roi("b", (8, 12, 8, 12))])

    # Something edits the layer directly; the records are unchanged, so they win.
    overlay.layer.data = []
    overlay.layer.events.data.emit(None)

    assert len(overlay.layer.data) == 2


# --------------------------------------------------------------------------
# target alignment — one coordinate representation (F-31)
# --------------------------------------------------------------------------

def test_overlay_takes_its_transform_from_the_target_layer(overlay):
    target = _ImageLayer(scale=(0.1, 0.1), translate=(5.0, 7.0))
    overlay.set_target_layer(target)

    overlay.set_rois([_roi("a", (0, 4, 0, 4))])

    assert tuple(overlay.layer.scale) == (0.1, 0.1)
    assert tuple(overlay.layer.translate) == (5.0, 7.0)


def test_shape_vertices_stay_in_roi_pixel_coordinates(overlay):
    """Not pre-transformed into world space: one representation, not two."""
    overlay.set_target_layer(_ImageLayer(scale=(0.1, 0.1)))

    overlay.set_rois([_roi("a", (0, 10, 0, 10))])

    drawn = np.asarray(overlay.layer.data[0])
    assert drawn.min() >= 0.0 and drawn.max() <= 10.0


# --------------------------------------------------------------------------
# hit testing (A-05b)
# --------------------------------------------------------------------------

def test_click_selects_the_roi_under_it(overlay):
    a = _roi("a", (0, 10, 0, 10))
    overlay.set_rois([a])

    assert overlay.roi_at((5, 5)) == a.uid
    assert overlay.roi_at((50, 50)) is None


def test_overlapping_rois_select_the_smallest(overlay):
    big = _roi("big", (0, 20, 0, 20))
    small = _roi("small", (8, 12, 8, 12))
    overlay.set_rois([big, small])

    assert overlay.roi_at((10, 10)) == small.uid
    assert overlay.roi_at((2, 2)) == big.uid


def test_click_in_a_hole_falls_through(overlay):
    mask = np.zeros((20, 20), dtype=bool)
    mask[3:15, 3:15] = True
    mask[7:11, 7:11] = False
    annulus = roi_from_mask(mask, name="annulus")
    annulus = ROIRecord(
        annulus.name, annulus.roi_type, annulus.bounds,
        source=annulus.source, mask=annulus.mask, uid=new_uid(),
    )
    overlay.set_rois([annulus])

    assert overlay.roi_at((4, 4)) == annulus.uid
    assert overlay.roi_at((9, 9)) is None, "the hole is not part of the ROI"


def test_a_polygon_is_hit_tested_as_a_polygon(overlay):
    triangle = roi_from_vertices(
        [[0, 0], [0, 10], [10, 0]], roi_type="polygon", name="tri"
    )
    triangle = ROIRecord(
        triangle.name, triangle.roi_type, triangle.bounds,
        vertices=triangle.vertices, uid=new_uid(),
    )
    overlay.set_rois([triangle])

    assert overlay.roi_at((1, 1)) == triangle.uid
    assert overlay.roi_at((9, 9)) is None


def test_hidden_rois_are_not_hit_tested(overlay):
    overlay.set_rois([_roi("a", (0, 10, 0, 10), visible=False)])

    assert overlay.roi_at((5, 5)) is None


# --------------------------------------------------------------------------
# selection highlight
# --------------------------------------------------------------------------

def test_selected_roi_is_drawn_differently(overlay):
    a = _roi("a", (0, 4, 0, 4))
    b = _roi("b", (8, 12, 8, 12))
    overlay.set_rois([a, b])
    unselected = list(overlay.layer.edge_color)

    overlay.set_selection([a.uid])

    assert overlay.layer.edge_color[0] != unselected[0]
    assert overlay.layer.edge_color[1] == unselected[1]


def test_group_members_share_a_colour(overlay):
    first = ROIRecord("a", "rectangle", (0, 4, 0, 4), group=2, uid=new_uid())
    second = ROIRecord("b", "rectangle", (8, 12, 8, 12), group=2, uid=new_uid())
    other = ROIRecord("c", "rectangle", (16, 20, 16, 20), group=5, uid=new_uid())

    overlay.set_rois([first, second, other])

    colors = list(overlay.layer.edge_color)
    assert colors[0] == colors[1]
    assert colors[2] != colors[0]


# --------------------------------------------------------------------------
# the overlay is never an image source (P-1.2)
# --------------------------------------------------------------------------

def test_overlay_layer_is_not_an_image_source():
    from imswitch.improcess.layer_selection import is_image_layer

    layer = _ImageLayer()
    layer.name = "ROI Manager"

    assert not is_image_layer(layer), "the overlay must never be measured as an image"


def test_clicking_a_drawn_roi_reports_its_uid(overlay):
    """Selection flows viewer → panel; the panel resolves the row by uid."""
    big = _roi("big", (0, 20, 0, 20))
    small = _roi("small", (8, 12, 8, 12))
    overlay.set_rois([big, small])
    clicked = []
    overlay.install_click_handler(lambda uid, mods: clicked.append(uid))

    handler = overlay.layer.mouse_drag_callbacks[0]
    handler(overlay.layer, SimpleNamespace(position=(10, 10), modifiers=()))

    assert clicked == [small.uid], "the click should pick the smaller ROI"


def test_click_handler_is_removed_with_the_overlay(overlay):
    overlay.set_rois([_roi("a", (0, 10, 0, 10))])
    overlay.install_click_handler(lambda uid, mods: None)
    assert overlay.layer.mouse_drag_callbacks

    overlay.remove_click_handler()

    assert overlay.layer.mouse_drag_callbacks == []


# --------------------------------------------------------------------------
# lifecycle: nothing may outlive the panel (round-7)
# --------------------------------------------------------------------------

def test_remove_disconnects_every_connection(overlay):
    """Removing the layer is not enough: the zoom and click handlers are
    attached to objects that outlive the panel."""
    viewer = overlay._viewer
    overlay.set_rois([_roi("a", (0, 4, 0, 4))])
    overlay.install_click_handler(lambda uid, mods: None)
    layer = overlay.layer
    assert layer.mouse_drag_callbacks
    assert viewer.camera.events.zoom._handlers

    overlay.remove()

    assert layer.mouse_drag_callbacks == []
    assert viewer.camera.events.zoom._handlers == [], "zoom handler outlived the overlay"
    assert overlay.layer is None


# --------------------------------------------------------------------------
# full style, not just stroke colour (P-1.5)
# --------------------------------------------------------------------------

def test_stroke_width_and_fill_come_from_the_style(overlay):
    from imswitch.imcommon.algorithms.roi_style import ROIStyle

    styled = ROIRecord(
        "a", "rectangle", (0, 4, 0, 4), uid=new_uid(),
        style=ROIStyle(stroke_color="#123456", stroke_width=5.0,
                       fill_color="#abcdef", fill_opacity=0.25),
    )

    overlay.set_rois([styled])

    assert overlay.layer.edge_color[0] == "#123456"
    assert overlay.layer.edge_width == [5.0]
    assert overlay.layer.face_color[0][3] == pytest.approx(0.25)


def test_a_set_default_style_applies_to_rois_without_their_own(overlay):
    from imswitch.imcommon.algorithms.roi_style import ROIStyle

    overlay.set_rois(
        [_roi("a", (0, 4, 0, 4))],
        default_style=ROIStyle(stroke_color="#00ff00", stroke_width=3.0),
    )

    assert overlay.layer.edge_color[0] == "#00ff00"
    assert overlay.layer.edge_width == [3.0]


def test_label_visibility_can_be_turned_off_per_roi(overlay):
    from imswitch.imcommon.algorithms.roi_style import ROIStyle

    quiet = ROIRecord(
        "quiet", "rectangle", (0, 4, 0, 4), uid=new_uid(),
        style=ROIStyle(label_visible=False),
    )
    overlay.set_rois([quiet, _roi("loud", (8, 12, 8, 12))])
    overlay.set_labels_visible(True)

    strings = [text for text in overlay.layer.text["string"] if text]
    assert strings == ["loud"]


# --------------------------------------------------------------------------
# an nD target must still align the 2D overlay (round-7)
# --------------------------------------------------------------------------

def test_a_3d_target_aligns_the_2d_overlay(overlay):
    """Copying a 3-element scale onto a 2D layer fails; swallowing that left
    the overlay at identity while the image sat at 0.1 um/px."""
    target = _ImageLayer(shape=(8, 64, 64), scale=(1.0, 0.1, 0.2),
                         translate=(0.0, 5.0, 7.0))

    overlay.set_target_layer(target)
    overlay.set_rois([_roi("a", (0, 4, 0, 4))])

    assert tuple(overlay.layer.scale) == (0.1, 0.2)
    assert tuple(overlay.layer.translate) == (5.0, 7.0)
    assert overlay.failures == [], overlay.failures


# --------------------------------------------------------------------------
# edge width — reported from a real image: outlines far too thick
# --------------------------------------------------------------------------

def _overlay_at(scale, zoom):
    """An overlay aligned to a target image layer with this pixel scale."""
    from imswitch.imcommon.view.guitools.naparitools import NapariROISetOverlay

    viewer = _Viewer()
    viewer.camera.zoom = zoom
    overlay = NapariROISetOverlay(viewer)
    overlay.set_target_layer(
        SimpleNamespace(scale=(scale, scale), translate=(0.0, 0.0))
    )
    return overlay


@pytest.mark.parametrize(
    "scale,zoom",
    [(1.0, 0.39), (0.1, 3.9), (65.0, 0.006), (100.0, 0.004), (10.0, 0.039)],
)
def test_the_outline_is_the_same_thickness_at_any_calibration(scale, zoom):
    """napari measures edge_width in DATA units and multiplies by the layer's
    scale and the camera zoom. The overlay copies the target's scale, so both
    terms are in play — dividing by the zoom alone makes the zoom terms cancel
    and leaves `PIXEL_WIDTH x scale` screen pixels, which for a 100 nm/px
    result calibrated in nm is two hundred pixels of outline.
    """
    from imswitch.imcommon.view.guitools.naparitools import NapariROISetOverlay

    overlay = _overlay_at(scale, zoom)
    edge_width = overlay._get_edge_width()
    on_screen = edge_width * scale * zoom

    assert on_screen == pytest.approx(NapariROISetOverlay._PIXEL_WIDTH, rel=1e-6)


def test_an_explicit_stroke_width_is_screen_pixels_as_documented():
    """ROIStyle.stroke_width says "screen pixels"; it was passed through as a
    data-unit width, so it meant something different on every calibration."""
    overlay = _overlay_at(scale=50.0, zoom=0.01)
    assert overlay._edge_width_for(6.0) * 50.0 * 0.01 == pytest.approx(6.0)


def test_zooming_keeps_per_roi_widths_apart():
    """Rescaling the layer to one value would discard every per-ROI width the
    moment the user zoomed."""
    from imswitch.imcommon.algorithms.roi_style import ROIStyle

    overlay = _overlay_at(scale=1.0, zoom=1.0)
    overlay.set_rois([
        _roi("thin", (0, 4, 0, 4), style=ROIStyle(stroke_width=1.0)),
        _roi("thick", (8, 12, 8, 12), style=ROIStyle(stroke_width=8.0)),
    ])
    assert overlay._screen_widths == [1.0, 8.0]

    overlay._viewer.camera.zoom = 4.0
    overlay._on_zoom_changed()
    widths = list(overlay._layer.edge_width)
    assert widths[0] != widths[1]
    assert widths[0] * 1.0 * 4.0 == pytest.approx(1.0)
    assert widths[1] * 1.0 * 4.0 == pytest.approx(8.0)


def test_the_set_default_width_reaches_rois_without_one():
    from imswitch.imcommon.algorithms.roi_style import ROIStyle

    overlay = _overlay_at(scale=1.0, zoom=1.0)
    overlay.set_rois(
        [_roi("a", (0, 4, 0, 4))],
        default_style=ROIStyle(stroke_width=5.0),
    )
    assert overlay._screen_widths == [5.0]
