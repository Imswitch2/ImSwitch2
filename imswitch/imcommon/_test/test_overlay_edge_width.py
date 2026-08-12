"""Overlay line thickness must follow the detector's pixel pitch.

The viewer-tool overlays (crosshair, grid, profile line) live on a Shapes layer
that is deliberately scale-(1, 1), so their ``edge_width`` is in WORLD units
(µm). Anything hardcoded there is a physical thickness, and a fine-pitch camera
turns it into a slab: a 1 µm grid line is 2 data pixels on a 500 nm/px camera
but 15 on a 65 nm/px one.

napari's own edge-width slider cannot be used to fix that by hand -- as of
0.7.1 it is an integer QLabeledSlider clamped to 0-40 -- so the value we
compute is the only one the operator gets.
"""

import types

import numpy as np
import pytest

napari = pytest.importorskip('napari')

from imswitch.imcommon.view.guitools.naparitools import (  # noqa: E402
    dataPixelWorldSize, worldEdgeWidth,
)

SCREEN_PIXELS = 2
CANVAS_PX = 800
IMAGE_PX = 2048


def viewerAt(pixelSizeUm, zoomMultiplier=1.0):
    """A viewer showing one image layer at fit-to-view * zoomMultiplier."""
    viewer = types.SimpleNamespace()
    viewer.layers = [napari.layers.Image(
        np.zeros((IMAGE_PX, IMAGE_PX), dtype=np.uint16),
        scale=(pixelSizeUm, pixelSizeUm),
    )]
    fitZoom = CANVAS_PX / (IMAGE_PX * pixelSizeUm)
    viewer.camera = types.SimpleNamespace(zoom=fitZoom * zoomMultiplier)
    return viewer


def thicknessInDataPixels(pixelSizeUm, zoomMultiplier=1.0):
    viewer = viewerAt(pixelSizeUm, zoomMultiplier)
    return worldEdgeWidth(viewer, SCREEN_PIXELS) / pixelSizeUm


@pytest.mark.parametrize('pixelSizeUm', [0.065, 0.082, 0.15, 0.5])
def test_overlay_renders_the_same_thickness_on_every_camera(pixelSizeUm):
    """The point of the zoom compensation: pitch must not change thickness."""
    reference = thicknessInDataPixels(0.15)

    assert thicknessInDataPixels(pixelSizeUm) == pytest.approx(reference, rel=1e-6)


def test_fine_pitch_camera_is_not_pinned_to_a_fixed_micron_floor():
    """Regression: a 0.5 µm floor kept a 65 nm/px overlay ~8 data px thick.

    No amount of zooming would thin it out, because the floor is absolute.
    """
    zoomedIn = thicknessInDataPixels(0.065, zoomMultiplier=20)

    assert zoomedIn < 1.0
    assert zoomedIn == pytest.approx(
        thicknessInDataPixels(0.5, zoomMultiplier=20), rel=1e-6)


def test_edge_width_stays_strictly_positive_at_absurd_zoom():
    """The floor still exists; it is just relative to the data now."""
    width = worldEdgeWidth(viewerAt(0.065, zoomMultiplier=1e9), SCREEN_PIXELS)

    assert width > 0


@pytest.mark.parametrize('zoom', [0, None])
def test_degenerate_zoom_does_not_raise(zoom):
    viewer = viewerAt(0.065)
    viewer.camera.zoom = zoom

    assert worldEdgeWidth(viewer, SCREEN_PIXELS) > 0


def test_data_pixel_size_ignores_the_overlay_layers_own_scale():
    """Shapes overlays are scale-(1, 1); counting them makes every answer 1.0."""
    viewer = viewerAt(0.065)
    viewer.layers.append(napari.layers.Shapes(name='Viewer Tools', ndim=2))

    assert dataPixelWorldSize(viewer) == pytest.approx(0.065)


def test_data_pixel_size_falls_back_when_no_image_layer_is_present():
    viewer = types.SimpleNamespace(
        layers=[], camera=types.SimpleNamespace(zoom=1.0))

    assert dataPixelWorldSize(viewer) == 1.0
