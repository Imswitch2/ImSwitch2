"""P-6.1 / P-6.6: ImageJ interop, and what it cannot carry.

`roifile` is optional, so the conversions that need it are skipped when it is
absent. What is *not* skipped is the behaviour that has to hold either way:
the panel's menu entries say why they are disabled, and the loss report is a
structure the panel can show rather than a log line.
"""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask  # noqa: E402
from imswitch.imcommon.algorithms.roi_imagej import (  # noqa: E402
    IMAGEJ_AXES,
    ImageJInteropError,
    InteropReport,
    available,
    read_imagej,
    write_imagej,
)
from imswitch.imcommon.algorithms.roi_style import ROIStyle  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402

needs_roifile = pytest.mark.skipif(
    not available(), reason="the optional 'roifile' package is not installed"
)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.ones((32, 32), dtype=float)))
    yield widget
    widget.deleteLater()


# --------------------------------------------------------------------------
# the report — a structure, not a log line
# --------------------------------------------------------------------------

def test_a_report_summarises_itself():
    report = InteropReport(count=3)
    assert report.lossless
    assert "3 ROI(s)" in report.summary

    report.loss("styles have no ImageJ equivalent")
    assert not report.lossless
    assert "1 thing(s)" in report.summary


def test_a_report_does_not_repeat_the_same_loss_once_per_roi():
    """Two hundred ROIs with styles is one fact, not two hundred."""
    report = InteropReport()
    for _ in range(200):
        report.loss("styles have no ImageJ equivalent")
    assert len(report.losses) == 1


def test_imagej_positions_are_c_z_t_only():
    assert IMAGEJ_AXES == ("C", "Z", "T")


# --------------------------------------------------------------------------
# the optional dependency
# --------------------------------------------------------------------------

def test_the_menu_entries_say_why_they_are_disabled(panel):
    enabled = panel.imagejImportAction.isEnabled()
    assert enabled == available()
    if not enabled:
        # Disabled with the reason on it, rather than absent: a missing menu
        # entry looks like the feature does not exist.
        assert "roifile" in panel.imagejImportAction.toolTip()


@pytest.mark.skipif(available(), reason="roifile is installed")
def test_interop_without_roifile_refuses_by_name(tmp_path):
    with pytest.raises(ImageJInteropError, match="roifile"):
        write_imagej(tmp_path / "x.zip", [ROIRecord("a", "rectangle", (0, 4, 0, 4))])


# --------------------------------------------------------------------------
# round-tripping through Fiji's format
# --------------------------------------------------------------------------

@needs_roifile
def test_a_rectangle_survives_a_round_trip(tmp_path):
    target = tmp_path / "one.roi"
    original = ROIRecord("cell", "rectangle", (3, 11, 5, 17))
    write_imagej(target, [original])

    (restored,), report = read_imagej(target)
    assert restored.name == "cell"
    assert restored.roi_type == "rectangle"
    assert restored.bounds == (3, 11, 5, 17)
    assert report.count == 1


@needs_roifile
def test_a_polygon_keeps_its_vertices_in_row_column_order(tmp_path):
    """(row, col) vs (x, y) is the classic way to transpose an entire set."""
    target = tmp_path / "poly.roi"
    original = ROIRecord(
        "tri", "polygon", (0, 10, 0, 20),
        vertices=((0.0, 0.0), (10.0, 0.0), (0.0, 20.0)),
    )
    write_imagej(target, [original])

    (restored,), _report = read_imagej(target)
    rows = [v[0] for v in restored.vertices]
    cols = [v[1] for v in restored.vertices]
    assert max(rows) == pytest.approx(10, abs=1)
    assert max(cols) == pytest.approx(20, abs=1)


@needs_roifile
def test_a_set_round_trips_through_a_zip(tmp_path):
    target = tmp_path / "RoiSet.zip"
    originals = [
        ROIRecord("a", "rectangle", (0, 4, 0, 4)),
        ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ROIRecord("c", "rectangle", (16, 20, 16, 20)),
    ]
    write_imagej(target, originals)

    restored, report = read_imagej(target)
    assert [roi.name for roi in restored] == ["a", "b", "c"]
    assert report.count == 3


@needs_roifile
def test_a_z_position_crosses_and_counts_from_zero_on_our_side(tmp_path):
    """ImageJ counts slices from 1 and uses 0 for "not set"."""
    target = tmp_path / "z.roi"
    write_imagej(
        target, [ROIRecord("a", "rectangle", (0, 4, 0, 4), position=(("Z", 4),))]
    )
    (restored,), _report = read_imagej(target)
    assert restored.position == (("Z", 4),)


@needs_roifile
def test_an_axis_imagej_cannot_hold_is_reported_not_dropped(tmp_path):
    roi = ROIRecord("a", "rectangle", (0, 4, 0, 4), position=(("Base", 2),))
    report = write_imagej(tmp_path / "a.roi", [roi])
    assert any("Base" in loss for loss in report.losses)


@needs_roifile
def test_styles_and_properties_are_reported_as_lost(tmp_path):
    roi = ROIRecord(
        "a", "rectangle", (0, 4, 0, 4),
        style=ROIStyle(stroke_color="#ff0000"),
        properties=(("stain", "DAPI"),),
    )
    report = write_imagej(tmp_path / "a.roi", [roi])
    assert any("style" in loss.lower() for loss in report.losses)
    assert any("propert" in loss.lower() for loss in report.losses)


@needs_roifile
def test_a_disconnected_composite_reports_what_it_dropped(tmp_path):
    """ImageJ stores one outline; two blobs cannot both survive."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:6, 2:6] = True
    mask[12:18, 12:18] = True
    roi = roi_from_mask(mask, name="two", offset=(0, 0))

    report = write_imagej(tmp_path / "two.roi", [roi])
    assert any("disconnected" in loss for loss in report.losses)


@needs_roifile
def test_an_import_says_that_imagej_files_carry_no_frame(tmp_path):
    """Which is why an imported ROI cannot claim an exact spatial match."""
    target = tmp_path / "a.roi"
    write_imagej(target, [ROIRecord("a", "rectangle", (0, 4, 0, 4))])
    _rois, report = read_imagej(target)
    assert any("no spatial frame" in loss for loss in report.losses)


@needs_roifile
def test_an_imported_roi_gets_an_identity_of_its_own(tmp_path):
    target = tmp_path / "a.roi"
    write_imagej(target, [ROIRecord("a", "rectangle", (0, 4, 0, 4), uid="original")])
    (restored,), _report = read_imagej(target)
    assert restored.uid and restored.uid != "original"
    assert restored.source == "imagej"


@needs_roifile
def test_a_malformed_file_is_refused_with_its_path(tmp_path):
    broken = tmp_path / "broken.roi"
    broken.write_bytes(b"not an ImageJ ROI at all")
    with pytest.raises(ImageJInteropError, match="broken.roi"):
        read_imagej(broken)


@needs_roifile
def test_unicode_names_survive(tmp_path):
    target = tmp_path / "u.roi"
    write_imagej(target, [ROIRecord("célula β", "rectangle", (0, 4, 0, 4))])
    (restored,), _report = read_imagej(target)
    assert restored.name == "célula β"


@needs_roifile
def test_importing_through_the_panel_adds_the_rois(panel, tmp_path, monkeypatch):
    target = tmp_path / "RoiSet.zip"
    write_imagej(
        target,
        [
            ROIRecord("a", "rectangle", (0, 4, 0, 4)),
            ROIRecord("b", "rectangle", (8, 12, 8, 12)),
        ],
    )
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(target), "")),
    )
    panel.import_imagej()
    assert [roi.name for roi in panel._model.rois] == ["a", "b"]
    assert "Imported 2 ROI(s)" in panel.summaryLabel.text()


@needs_roifile
def test_a_polygon_encloses_the_same_pixels_after_a_round_trip(tmp_path):
    """The measurement that matters: the same region, not the same bytes."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask

    shape = (32, 32)
    original = ROIRecord(
        "blob", "polygon", (4, 24, 6, 26),
        vertices=((4.0, 6.0), (4.0, 26.0), (24.0, 26.0), (18.0, 12.0)),
    )
    target = tmp_path / "blob.roi"
    write_imagej(target, [original])
    (restored,), _report = read_imagej(target)

    before = roi_mask(original, shape)
    after = roi_mask(restored, shape)
    disagreement = np.logical_xor(before, after).sum()
    # A rasterisation may differ by a boundary pixel; the region may not.
    assert disagreement <= before.sum() * 0.05


@needs_roifile
def test_a_group_survives_a_round_trip(tmp_path):
    target = tmp_path / "g.roi"
    write_imagej(target, [ROIRecord("a", "rectangle", (0, 4, 0, 4), group=3)])
    (restored,), _report = read_imagej(target)
    assert restored.group == 3


@needs_roifile
def test_one_unreadable_entry_does_not_cost_a_whole_zip(tmp_path, monkeypatch):
    import roifile

    target = tmp_path / "RoiSet.zip"
    write_imagej(
        target,
        [ROIRecord(f"r{i}", "rectangle", (i, i + 2, i, i + 2)) for i in range(3)],
    )

    original = roifile.ImagejRoi.coordinates
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(self)
        if len(calls) == 2:
            raise ValueError("corrupt entry")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(roifile.ImagejRoi, "coordinates", flaky)
    rois, report = read_imagej(target)
    assert len(rois) == 3      # the failure is swallowed per ROI, not per file
    assert any("bounding box" in warning for warning in report.warnings)
