"""The fast-Gauss MoNaLISA path on recordings the live session cannot stream.

Z planes, an axis the scan does not step, a serpentine fast axis: the live
session scatters contiguous X/Y rasters only, and used to refuse these. They
are now reconstructed with the same Gaussian extraction and every frame put
where the acquisition layout records it, as the classic path does.
"""

import os
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model.acquisition_layout import (  # noqa: E402
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    TraversalRule,
)
from imswitch.improcess._test._monalisa_synthetic import make_scan  # noqa: E402
from imswitch.improcess.model.acquisition_layout_resolver import (  # noqa: E402
    ResolvedAcquisitionLayout,
)
from imswitch.improcess.reconstructors.monalisa.reconstructor import (  # noqa: E402
    MonalisaReconstructor,
)
from imswitch.improcess.reconstructors.monalisa_lattice.result import (  # noqa: E402
    MonalisaLatticeResult,
)

OFFSET = (4.2, 5.7)        # (x, y) of the synthetic lattice, period 11 px
PIXEL_NM = 70.0
# What every ImControl recording carries beside its layout; the streaming
# path sizes its steps from these.
STAGE_ATTRS = {
    "ScanStage:axis_startpos": np.array([0.0, 0.0, 0.0]),
    "ScanStage:axis_length": np.array([0.735, 0.735, 0.1]),
    "ScanStage:axis_step_size": np.array([0.035, 0.035, 0.1]),
}


def _loop(kind, count, step_um=None, direction=1):
    scan = kind.startswith("scan_")
    return AcquisitionLoop(
        kind, kind, count, step=step_um, unit="um" if step_um else None,
        direction=direction if scan else None,
    )


def _recording(frames, loops, traversal=None):
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
        traversal=traversal or tuple(TraversalRule(loop.id, "forward") for loop in loops),
        provenance="recorded",
    )
    resolved = ResolvedAcquisitionLayout(
        layout=layout, source="explicit-metadata", confidence="certain"
    )
    return types.SimpleNamespace(
        name="scan", data=frames, dataLoaded=True, attrs=dict(STAGE_ATTRS),
        numFrames=frames.shape[0], acquisition_layout=resolved,
        checkAndLoadData=lambda: None, checkAndUnloadData=lambda: None,
    )


def _params(recording):
    reconstructor = MonalisaReconstructor()
    params = reconstructor.prepare_params(recording, reconstructor.default_params())
    params.update(
        device="CPU",
        pixel_size_nm=PIXEL_NM,
        row_offset=OFFSET[1], col_offset=OFFSET[0], row_period=11.0, col_period=11.0,
        auto_scan_orientation=True,
        # Geometry/placement equivalence tests should not conflate optional
        # intensity normalization with the signal's known plane ratios.
        intensity_correction=False, cell_offsets=False,
    )
    return params


def _planes(scan, factors):
    return np.concatenate([scan.frames * factor for factor in factors])


def _process(frames, loops, **changes):
    recording = _recording(frames, loops)
    params = {**_params(recording), **changes}
    return MonalisaReconstructor().process(recording, params)


class TestGeometryChoice:
    def test_a_plain_xy_stack_still_streams(self):
        scan = make_scan(steps=(22, 22), offset=OFFSET)
        recording = _recording(scan.frames, [_loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)])
        geometry = MonalisaReconstructor()._fast_gauss_geometry_from_layout(
            recording.acquisition_layout, STAGE_ATTRS, scan.frames.shape[0]
        )
        assert "placement" not in geometry
        assert (geometry["nx_s"], geometry["ny_s"]) == (22, 22)

    def test_planes_and_unscanned_axes_are_placed(self):
        scan = make_scan(steps=(22, 22), offset=OFFSET)
        for loops, reason in (
            ([_loop("scan_z", 2, 0.1), _loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)],
             "2 Z slices"),
            ([_loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035), _loop("scan_z", 1)],
             None),
            ([_loop("scan_x", 1), _loop("scan_y", 484, 0.035)], "a scan of one axis only"),
        ):
            frames = _planes(scan, (1.0, 1.0)) if reason == "2 Z slices" else scan.frames
            recording = _recording(frames, loops)
            geometry = MonalisaReconstructor()._fast_gauss_geometry_from_layout(
                recording.acquisition_layout, STAGE_ATTRS, frames.shape[0]
            )
            if reason is None:
                assert "placement" not in geometry
            else:
                assert geometry["reasons"] == [reason]


class TestPlacedReconstruction:
    """Finite unified reconstruction, with physically labelled result axes.

    The old Fast GUI route returned a padded (Dataset, Base, T, Z, Y, X)
    legacy object; the current MoNaLISA plugin returns only measured axes.
    """

    def test_a_scan_of_x_y_and_z(self):
        scan = make_scan(steps=(22, 22), offset=OFFSET)
        frames = _planes(scan, (1.0, 2.0))
        result = _process(
            frames, [_loop("scan_z", 2, 0.1), _loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)]
        )
        assert isinstance(result, MonalisaLatticeResult)
        assert result.axis_labels == ["Z", "Y", "X"]
        assert result.data.shape[0] == 2
        assert result.data.shape[-2] > 22 and result.data.shape[-1] > 22
        assert result.axis_scales == pytest.approx([100.0, 35.0, 35.0], rel=0.02)
        assert result.output_pixel_size_nm == pytest.approx((35.0, 35.0), rel=0.02)
        reference = result.data[0]
        # Exclude uncovered pixels rather than dividing by zeros in the scan border.
        mask = reference > 1e-5
        assert np.any(mask)
        assert np.median(result.data[1][mask] / reference[mask]) == pytest.approx(2.0, rel=0.03)

    def test_a_scan_of_y_and_z_has_one_pixel_per_cell_along_x(self):
        line = make_scan(steps=(1, 22), offset=OFFSET)
        frames = _planes(line, (1.0, 0.5, 2.0))
        result = _process(frames, [_loop("scan_z", 3, 0.1), _loop("scan_y", 22, 0.035)])
        assert result.axis_labels == ["Z", "Y", "X"]
        assert result.data.shape[0] == 3
        # X is not scanned: one output pixel per lattice cell, not per scan step.
        assert 190 <= result.data.shape[1] <= 200
        assert 9 <= result.data.shape[2] <= 11
        assert result.axis_scales == pytest.approx([100.0, 35.0, 770.0], rel=0.02)
        assert result.output_pixel_size_nm == pytest.approx((35.0, 770.0), rel=0.02)
        reference = result.data[0]
        mask = reference > 1e-5
        assert np.any(mask)
        for plane, factor in zip(result.data, (1.0, 0.5, 2.0)):
            assert np.median(plane[mask] / reference[mask]) == pytest.approx(factor, rel=0.03)

    @pytest.mark.parametrize("orientation", ["-x-y", "+x+y"])
    def test_a_z_plane_matches_the_same_standalone_xy_scan(self, orientation):
        """Same frames, extraction and orientation, with or without a Z dimension.

        Streaming parity belongs to the separate generalized-Session PR. The
        unified finite path must nevertheless place equal scans equally.
        """
        scan = make_scan(steps=(22, 22), offset=OFFSET, orientation=orientation)
        frames = _planes(scan, (1.0, 3.0))
        placed = _process(
            frames, [_loop("scan_z", 2, 0.1), _loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)],
            orientation_mode="Manual", orientation=orientation,
        )
        single = _process(
            scan.frames, [_loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)],
            orientation_mode="Manual", orientation=orientation,
        )
        assert placed.axis_labels == ["Z", "Y", "X"]
        assert single.axis_labels == ["Y", "X"]
        assert placed.diagnostics["orientation"] == orientation
        assert single.diagnostics["orientation"] == orientation
        np.testing.assert_allclose(placed.data[0], single.data, rtol=1e-4, atol=1e-3)

    def test_saved_with_its_planes(self, tmp_path):
        import tifffile

        # Small 11 x 11 grids may not contain enough image information to score
        # a scan orientation. Supply the orientation explicitly: this test is
        # about Z-plane serialization, not automatic orientation estimation.
        scan = make_scan(steps=(11, 11), offset=OFFSET)
        result = _process(
            _planes(scan, (1.0, 2.0)),
            [_loop("scan_z", 2, 0.1), _loop("scan_y", 11, 0.035), _loop("scan_x", 11, 0.035)],
            orientation_mode="Manual", orientation="+x+y",
        )
        assert result.axis_labels == ["Z", "Y", "X"]
        result.save(tmp_path / "planes.ome.tif")
        with tifffile.TiffFile(tmp_path / "planes.ome.tif") as handle:
            series = handle.series[0]
            assert series.axes == "ZYX"
            assert series.shape == result.data.shape
            assert 'PhysicalSizeZ="100.0"' in handle.ome_metadata


# Copyright (C) 2020-2026 ImSwitch developers
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
