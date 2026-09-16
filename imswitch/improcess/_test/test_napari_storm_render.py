"""Rendering checks that need a real napari viewer and a GL session.

The rest of the napari-storm tests are planner-side and headless. These are
the ones that cannot be: they assert how the point cloud lands *in the viewer*,
next to ordinary napari layers.

That distinction is the whole point of the axis-order test here. napari-storm
once drew reconstructions transposed against every other napari layer, and its
own suite could not see it because the error was self-consistent inside its
dock widget. It took an embedding host putting a napari ``Points`` layer beside
a reconstruction to expose it, so that comparison is pinned here.

Skipped unless a viewer can actually be created, so a headless CI box without
GL reports a skip rather than a failure.

**Deselected by default.** A live GL context does not survive alongside the
rest of the suite in one process — it segfaults the interpreter reproducibly,
with or without clean teardown — so these carry the ``glviewer`` marker and
``addopts`` excludes it. Run them on their own, on a real display::

    pytest -m glviewer

Not under ``QT_QPA_PLATFORM=offscreen``: napari's canvas queries GL texture
limits while it is built, and the offscreen platform has no GL to answer
with, so the interpreter segfaults before any test body runs.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns

pytest.importorskip("napari_storm.core", reason="optional 'storm' extra not installed")
pytest.importorskip("qtpy.QtWidgets")

pytestmark = pytest.mark.glviewer

# x deliberately spans four times what y does, so an axis swap is unmissable.
X_SPAN = (0.0, 40000.0)
Y_SPAN = (0.0, 10000.0)


def _pump(widget, cycles: int = 15) -> None:
    """Let the canvas paint. Screenshots are empty until it has."""
    from qtpy import QtWidgets

    app = QtWidgets.QApplication.instance()
    for _ in range(cycles):
        if app is not None:
            app.processEvents()


@pytest.fixture
def view(qtbot):
    """A real ReconstructionView with the point-cloud backend enabled."""
    from imswitch.improcess.view.ReconstructionView import ReconstructionView

    try:
        widget = ReconstructionView(useNapariStormViewer=True)
    except Exception as exc:  # noqa: BLE001 - no GL session on this box
        pytest.skip(f"could not create a napari viewer: {exc}")
    qtbot.addWidget(widget)
    if widget.napariStormDisplay is None or not widget.napariStormDisplay.importable:
        pytest.skip("napari-storm not importable")
    # Shown and sized on purpose: the canvas only paints once it has a size,
    # so a screenshot of an unshown widget is empty however healthy the
    # bookkeeping looks.
    widget.resize(900, 700)
    widget.show()
    _pump(widget)
    yield widget

    # Teardown in dependency order: GPU datasets, then layers, then the
    # viewer's own window. A napari viewer left open holds a GL context that
    # outlives the test and takes the interpreter down at exit.
    for step in (
        widget.napariStormDisplay.close_all,
        widget.napariViewer.layers.clear,
        widget.napariViewer.close,
        widget.close,
    ):
        try:
            step()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass
    _pump(widget)


def _asymmetric_result(count=3000, *, with_z=False):
    rng = np.random.default_rng(1)
    x = rng.uniform(*X_SPAN, count)
    y = rng.uniform(*Y_SPAN, count)
    columns = {
        "frame": np.arange(count),
        "x_nm": x,
        "y_nm": y,
        "sigma_x_nm": np.full(count, 140.0),
        "sigma_y_nm": np.full(count, 140.0),
        "lp_x_nm": np.full(count, 12.0),
        "lp_y_nm": np.full(count, 12.0),
        "photons": np.full(count, 1000.0),
    }
    if with_z:
        columns["z_nm"] = rng.uniform(-300, 300, count)
        columns["lp_z_nm"] = np.full(count, 30.0)
    return LocalizationResult(
        "asymmetric",
        localizations_from_columns(columns),
        pixel_size_nm=100.0,
        dims="3D" if with_z else "2D",
    ), x, y


def _lateral_spans(layer):
    """(rows, columns) span of a layer, in napari's last-two world axes."""
    extent = np.asarray(layer.extent.world, dtype=float)
    span = extent[1] - extent[0]
    return float(span[-2]), float(span[-1])


def test_a_reconstruction_is_not_transposed_against_an_ordinary_layer(view):
    """The regression that only an embedding host can see.

    A napari ``Points`` layer built from the same numbers in napari's own
    ``(y, x)`` order is the control. If the point cloud disagrees with it about
    which axis is which, then ROI shapes, overlays and reference images all
    misregister against the reconstruction.
    """
    result, x, y = _asymmetric_result()
    assert view.napariStormDisplay.show(result) is True

    cloud = view.napariViewer.layers[result.name]
    control = view.napariViewer.add_points(
        np.stack([y, x], axis=1), name="control", size=10,
    )

    cloud_rows, cloud_cols = _lateral_spans(cloud)
    control_rows, control_cols = _lateral_spans(control)

    assert (cloud_rows > cloud_cols) == (control_rows > control_cols), (
        f"point cloud spans rows={cloud_rows:.0f} cols={cloud_cols:.0f} while "
        f"an equivalent napari Points layer spans rows={control_rows:.0f} "
        f"cols={control_cols:.0f}: the reconstruction is transposed against "
        f"every ordinary layer sharing this viewer"
    )


def test_the_wide_axis_is_the_one_that_is_wide(view):
    """Absolute check, not just agreement with the control layer."""
    result, x, y = _asymmetric_result()
    assert view.napariStormDisplay.show(result) is True

    rows, cols = _lateral_spans(view.napariViewer.layers[result.name])

    # napari's last axis is x, and x is the wide one here.
    assert cols == pytest.approx(x.max() - x.min(), rel=0.05)
    assert rows == pytest.approx(y.max() - y.min(), rel=0.05)


def test_the_camera_is_framed_on_the_cloud_not_the_blank_image_layer(view):
    """ImProcess's protected 1x1 image layer sits at the origin.

    napari counts it in the framed extent even when hidden, so a cloud at real
    sample coordinates ends up a speck in the corner unless the adapter
    corrects for it.
    """
    result, x, y = _asymmetric_result()
    assert view.napariStormDisplay.show(result) is True

    centre = np.asarray(view.napariViewer.camera.center, dtype=float)
    expected_rows = 0.5 * (y.min() + y.max())
    expected_cols = 0.5 * (x.min() + x.max())

    assert centre[-2] == pytest.approx(expected_rows, abs=0.05 * (y.max() - y.min()))
    assert centre[-1] == pytest.approx(expected_cols, abs=0.05 * (x.max() - x.min()))


def test_a_three_dimensional_result_switches_the_canvas(view):
    result, _x, _y = _asymmetric_result(with_z=True)
    assert view.napariStormDisplay.show(result) is True
    assert view.napariViewer.dims.ndisplay == 3


def test_the_canvas_actually_draws_something(view):
    """A screenshot with nothing in it is the failure this catches.

    Every piece of state can report itself healthy while the canvas stays
    empty — napari-storm's own docs call that the worst thing to debug in the
    codebase — so the pixels are checked rather than the bookkeeping.
    """
    result, _x, _y = _asymmetric_result()
    assert view.napariStormDisplay.show(result) is True
    _pump(view)

    shot = np.asarray(view.napariViewer.screenshot(canvas_only=True))
    lit = int(np.count_nonzero(shot[..., :3].sum(axis=-1)))
    assert lit > 0, "the point cloud reported success but drew no pixels"
