"""P-1.7: the napari Shapes behaviour the ROI overlay depends on.

A fake viewer cannot vouch for any of this — it does whatever the fake was
written to do. This exercises a *real* ``napari.layers.Shapes`` in a
subprocess, so a napari release that changed ``features``, ``text`` or
``editable`` fails here on the pull request rather than in someone's session.

Headless: it constructs a bare layer with no viewer and no display, so it fits
the ordinary improcess lane and needs no xvfb.
"""

import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("napari")


_CONTRACT = textwrap.dedent(
    """
    import numpy as np
    from napari.layers import Shapes

    square = np.array([[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0]])
    other = square + 20.0
    layer = Shapes([square, other], shape_type="polygon")

    # 1. Per-shape metadata: the overlay stores an ROI uid on every part.
    layer.features = {"roi_uid": ["a", "b"], "part_index": [0, 0]}
    assert list(layer.features["roi_uid"]) == ["a", "b"], layer.features

    # 2. Per-shape text: ROI name labels.
    layer.text = {"string": ["one", "two"], "anchor": "center"}
    assert layer.text is not None

    # 3. editable=False must also stand the interaction mode down, which is
    #    what makes the overlay read-only.
    layer.editable = False
    assert layer.editable is False
    assert str(layer.mode) in ("pan_zoom", "Mode.PAN_ZOOM"), layer.mode

    # 4. Mouse callbacks are how selection is delivered (we do the hit test).
    assert hasattr(layer, "mouse_drag_callbacks")

    # 5. Per-shape edge colour, for the selection highlight.
    layer.edge_color = ["#ffcc00", "#00b7eb"]
    assert len(layer.edge_color) == 2

    print("CONTRACT-OK")
    """
)


_POINTS_CONTRACT = textwrap.dedent(
    """
    import inspect

    import numpy as np
    from napari.layers import Points

    # 1. The colour keyword napari 0.5 renamed. The tool picks between the two
    #    by inspecting the signature; if neither is there, that choice is
    #    silently wrong rather than an error.
    params = inspect.signature(Points.__init__).parameters
    assert "border_color" in params or "edge_color" in params, sorted(params)

    layer = Points(np.array([[1.0, 2.0], [3.0, 4.0]]), size=8, ndim=2)

    # 2. `add` is the drawing mode the broker switches to.
    layer.mode = "add"
    assert str(layer.mode) in ("add", "Mode.ADD"), layer.mode
    layer.mode = "pan_zoom"

    # 3. Data is (n, 2) row/col and assignable, which is how points are read
    #    back at capture and cleared afterwards.
    layer.data = [(5.0, 6.0)]
    assert np.allclose(np.asarray(layer.data), [[5.0, 6.0]])
    layer.data = []
    assert len(layer.data) == 0

    # 4. A data event, so the panel learns that points were placed.
    assert hasattr(layer.events, "data")

    print("CONTRACT-OK")
    """
)


def test_real_napari_points_supports_what_the_point_tool_needs():
    """P-P: the same guard as the Shapes contract, for the Points layer."""
    result = subprocess.run(
        [sys.executable, "-c", _POINTS_CONTRACT],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "CONTRACT-OK" in result.stdout, (
        f"napari's Points layer no longer supports the point tool's "
        f"assumptions.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_real_napari_shapes_supports_what_the_overlay_needs():
    result = subprocess.run(
        [sys.executable, "-c", _CONTRACT],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "CONTRACT-OK" in result.stdout, (
        "napari's Shapes contract changed under the ROI overlay:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
