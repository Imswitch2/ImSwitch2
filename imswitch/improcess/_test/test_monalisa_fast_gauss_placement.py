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
from imswitch.improcess.reconstructors.monalisa.result import (  # noqa: E402
    MonalisaProcessingResult,
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
    def test_a_scan_of_x_y_and_z(self):
        scan = make_scan(steps=(22, 22), offset=OFFSET)
        frames = _planes(scan, (1.0, 2.0))
        result = _process(
            frames, [_loop("scan_z", 2, 0.1), _loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)]
        )
        assert isinstance(result, MonalisaProcessingResult)
        assert result.axis_labels == ["Dataset", "Base", "T", "Z", "Y", "X"]
        rows, cols = scan.frames.shape[1:]
        foci_rows = int(np.ceil((rows - OFFSET[1]) / 11.0))
        foci_cols = int(np.ceil((cols - OFFSET[0]) / 11.0))
        assert result.data.shape == (1, 1, 1, 2, 22 * foci_rows, 22 * foci_cols)
        # Z from the layout's plane step, Y and X the scan step.
        assert result.axis_scales == pytest.approx([1.0, 1.0, 1.0, 100.0, 35.0, 35.0])
        assert result.output_pixel_size_nm == pytest.approx((35.0, 35.0))
        inner = (slice(30, -30), slice(30, -30))
        ratio = result.data[0, 0, 0, 1][inner] / result.data[0, 0, 0, 0][inner]
        assert np.median(ratio) == pytest.approx(2.0, rel=0.02)
        assert result.placement is not None and result.placement.slices == 2

    def test_a_scan_of_y_and_z_has_one_pixel_per_cell_along_x(self):
        line = make_scan(steps=(1, 22), offset=OFFSET)
        frames = _planes(line, (1.0, 0.5, 2.0))
        result = _process(frames, [_loop("scan_z", 3, 0.1), _loop("scan_y", 22, 0.035)])
        rows, cols = line.frames.shape[1:]
        foci_rows = int(np.ceil((rows - OFFSET[1]) / 11.0))
        foci_cols = int(np.ceil((cols - OFFSET[0]) / 11.0))
        assert result.data.shape == (1, 1, 1, 3, 22 * foci_rows, foci_cols)
        # X is one period: 11 px of 70 nm.
        assert result.axis_scales == pytest.approx([1.0, 1.0, 1.0, 100.0, 35.0, 770.0])
        assert result.output_pixel_size_nm == pytest.approx((35.0, 770.0))
        dims = result.scan_params["dimensions"]
        sizes = [float(v) for v in result.scan_params["step_sizes"]]
        assert sizes[dims.index("Right-Left")] == pytest.approx(770.0)
        assert sizes[dims.index("Back-Front")] == pytest.approx(100.0)
        inner = (slice(30, -30), slice(1, -1))
        for plane, factor in zip(result.data[0, 0, 0], (1.0, 0.5, 2.0)):
            ratio = plane[inner] / result.data[0, 0, 0, 0][inner]
            assert np.median(ratio) == pytest.approx(factor, rel=0.02)

    @pytest.mark.parametrize("orientation, flipped", [("-x-y", False), ("+x+y", True)])
    def test_the_planes_are_the_streamed_image_of_each(self, orientation, flipped):
        """One plane placed by the layout is the live session's image of it:
        the same extraction, the same raster, the same orientation.

        The layout says how the index runs along the stage axes; whether
        that is up or down the camera both paths read off the frames. With
        ``"-x-y"`` the sample moves against the index and the layout's
        placement is already right; with ``"+x+y"`` both paths find the
        mirrored placement, and say so.
        """
        scan = make_scan(steps=(22, 22), offset=OFFSET, orientation=orientation)
        frames = _planes(scan, (1.0, 3.0))
        placed = _process(
            frames, [_loop("scan_z", 2, 0.1), _loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)]
        )
        streamed = _process(scan.frames, [_loop("scan_y", 22, 0.035), _loop("scan_x", 22, 0.035)])
        assert streamed.placement is None
        expected = ["-", "-"] if flipped else ["+", "+"]
        assert streamed.scan_params["directions"][:2] == expected
        dims = placed.scan_params["dimensions"]
        placed_directions = [
            placed.scan_params["directions"][dims.index(label)]
            for label in ("Right-Left", "Up-Down")
        ]
        assert placed_directions == (["neg", "neg"] if flipped else ["pos", "pos"])
        np.testing.assert_allclose(
            placed.data[0, 0, 0, 0], streamed.data[0, 0, 0, 0], rtol=1e-4, atol=1e-3
        )

    def test_saved_with_its_planes(self, tmp_path):
        import tifffile

        scan = make_scan(steps=(11, 11), offset=OFFSET)
        result = _process(
            _planes(scan, (1.0, 2.0)),
            [_loop("scan_z", 2, 0.1), _loop("scan_y", 11, 0.035), _loop("scan_x", 11, 0.035)],
        )
        result.save(tmp_path / "planes.ome.tif")
        with tifffile.TiffFile(tmp_path / "planes.ome.tif") as handle:
            series = handle.series[0]
            assert series.axes == "ZYX"
            assert series.shape == (2, *result.data.shape[-2:])
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
