"""Tests for the MoNaLISA lattice reconstructor plugin."""

import os
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess._test._monalisa_synthetic import (  # noqa: E402
    make_physical_scan,
    make_scan,
)
from imswitch.improcess.reconstructors import (  # noqa: E402
    available_reconstructor_ids,
    get_registry,
    register_reconstructor_by_id,
)
from imswitch.improcess.reconstructors.base import (  # noqa: E402
    CancellationToken,
    ReconstructionCancelled,
    ReconstructionContext,
)
from imswitch.improcess.reconstructors.monalisa_lattice import (  # noqa: E402
    MonalisaLatticeReconstructor,
)
from imswitch.improcess.reconstructors.monalisa_lattice.result import (  # noqa: E402
    MonalisaLatticeResult,
)
from imswitch.improcess.reconstructors.monalisa_lattice.scan import (  # noqa: E402
    recorded_scan,
)
from imswitch.improcess.reconstructors.registry import PluginRegistry  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _recording(frames, attrs=None, name="scan"):
    return types.SimpleNamespace(
        name=name, data=frames, attrs=dict(attrs or {}), numFrames=frames.shape[0]
    )


def _attrs(length_um=(0.69, 0.69), step_um=0.03):
    return {
        "ScanStage:axis_length": np.array([*length_um, 0.0]),
        "ScanStage:axis_step_size": np.array([step_um, step_um, 0.1]),
    }


def _params(**changes):
    return {**MonalisaLatticeReconstructor.default_params(), **changes}


PLAIN = dict(frame_gain=False, cell_offsets=False, reassignment="off")


class TestRecordedScan:
    @pytest.mark.parametrize(
        "length, step, frames, expected",
        [
            ((0.69, 0.69), 0.03, 576, (24, 24)),       # 23 steps, 24 positions
            ((0.815, 0.815), 0.035, 576, (24, 24)),
            ((0.625, 0.625), 0.035, 324, (18, 18)),
            ((0.53, 1.1), 0.035, 512, (16, 32)),       # the diamond's brick
        ],
    )
    def test_the_recordings(self, length, step, frames, expected):
        scan = recorded_scan(_attrs(length, step), frames)
        assert (scan.num_fast, scan.num_slow) == expected
        assert scan.num_stacks == 1
        assert scan.step_nm == pytest.approx(step * 1000)

    def test_counts_written_by_the_scan_are_taken_first(self):
        attrs = {**_attrs(), "ScanTTL:Nx": 12, "ScanTTL:Ny": 10}
        scan = recorded_scan(attrs, 240)
        assert (scan.num_fast, scan.num_slow, scan.num_stacks) == (12, 10, 2)

    def test_several_scans_in_one_recording(self):
        scan = recorded_scan(_attrs(), 3 * 576)
        assert scan.num_stacks == 3

    def test_without_metadata_a_square_scan(self):
        scan = recorded_scan({}, 484)
        assert (scan.num_fast, scan.num_slow) == (22, 22)
        assert scan.step_nm is None
        assert "square" in scan.describe()

    def test_what_is_set_wins(self):
        scan = recorded_scan(_attrs(), 512, num_fast=16, num_slow=32, step_nm=35.0)
        assert (scan.num_fast, scan.num_slow, scan.step_nm) == (16, 32, 35.0)

    def test_frames_that_make_no_scan(self):
        with pytest.raises(ValueError, match="square"):
            recorded_scan({}, 500)
        with pytest.raises(ValueError, match="number of scans"):
            recorded_scan({}, 500, num_fast=16, num_slow=32)
        with pytest.raises(ValueError, match="both"):
            recorded_scan({}, 484, num_fast=22)

    def test_line_step_conditions_are_refused(self):
        with pytest.raises(ValueError, match="line-step"):
            recorded_scan({**_attrs(), "ScanTTL:n_linesteps": 2}, 1152)


class TestPlugin:
    def test_is_a_built_in_reconstructor(self):
        assert "monalisa-lattice" in available_reconstructor_ids()
        registry = PluginRegistry()
        plugin = register_reconstructor_by_id(registry, "monalisa-lattice")
        assert isinstance(plugin, MonalisaLatticeReconstructor)
        assert plugin.execution_policy == "worker"
        assert registry.get_reconstructor("monalisa-lattice") is plugin
        assert get_registry() is not registry

    def test_the_panel_shows_the_scan_of_the_recording(self, qapp):
        plugin = MonalisaLatticeReconstructor()
        widget = plugin.make_param_widget(QtWidgets.QWidget())
        before = widget.get_values()
        widget.load_from_attrs(_attrs(), _recording(np.zeros((576, 4, 4))))
        assert "24 x 24 steps of 30 nm" in widget.p.param("Recording").value()
        assert widget.get_values() == before
        widget.setOutputPixelSize(30.0)
        assert widget.p.param("Output pixel").value() == "0.03 µm"
        widget.setOutputPixelSize((30.0, 35.0))
        assert widget.p.param("Output pixel").value() == "Y 0.03 / X 0.035 µm"
        widget.setOutputPixelSize(None)
        assert widget.p.param("Output pixel").value() == ""
        widget.load_from_attrs({}, _recording(np.zeros((500, 4, 4))))
        assert "square" in widget.p.param("Recording").value()


class TestFociPreview:
    def test_the_panel_has_the_checkbox(self, qapp):
        parent = QtWidgets.QWidget()
        widget = MonalisaLatticeReconstructor().make_param_widget(parent)
        assert widget.previewCheckbox.text() == "Show found foci"
        seen = []
        widget.sigPreviewToggled.connect(seen.append)
        widget.previewCheckbox.setChecked(True)
        assert seen == [True]
        widget.setPreviewStatus("3600 foci")
        assert widget.previewStatusLabel.text() == "3600 foci"

    def test_the_preview_draws_the_foci_of_the_recording(self):
        scan = make_scan()
        plugin = MonalisaLatticeReconstructor()
        recording = _recording(scan.frames)
        preview = plugin.detection_preview(recording, scan.frames[0], _params())
        rows, cols = scan.frames.shape[1:]
        inside = (
            (scan.focus_x >= 0) & (scan.focus_x < cols)
            & (scan.focus_y >= 0) & (scan.focus_y < rows)
        )
        assert preview.x.size == inside.sum()
        from scipy.spatial import cKDTree

        distance, _ = cKDTree(np.column_stack([preview.x, preview.y])).query(
            np.column_stack([scan.focus_x[inside], scan.focus_y[inside]])
        )
        assert np.max(distance) < 0.5
        assert "foci" in preview.status and "frame" in preview.status
        # Found once per recording, kept for the next frame on screen.
        finding = plugin._preview_cache[1]
        again = plugin.detection_preview(recording, scan.frames[1], _params())
        assert plugin._preview_cache[1] is finding
        np.testing.assert_array_equal(again.x, preview.x)
        assert plugin.detection_preview(None, scan.frames[0], _params()) is None

    def test_the_controller_draws_what_the_plugin_finds(self):
        from types import SimpleNamespace

        from imswitch.improcess.controller.ReconstructorManagerController import (
            ReconstructorManagerController,
        )

        class _Sig:
            def __init__(self):
                self.emitted = []

            def emit(self, *args):
                self.emitted.append(args)

        scan = make_scan()
        recording = _recording(scan.frames)
        ctrl = ReconstructorManagerController.__new__(ReconstructorManagerController)
        ctrl._main = SimpleNamespace(
            _activeReconstructor=MonalisaLatticeReconstructor(),
            _currentDataObj=recording,
            dataFrameController=SimpleNamespace(getDisplayedImage2D=lambda: scan.frames[0]),
        )
        statuses = []
        ctrl._previewWidget = SimpleNamespace(
            previewCheckbox=SimpleNamespace(isChecked=lambda: True),
            get_values=lambda: _params(),
            setPreviewStatus=statuses.append,
        )
        ctrl._commChannel = SimpleNamespace(
            sigDetectionPreviewUpdated=_Sig(),
            sigDetectionPreviewVisibilityChanged=_Sig(),
        )
        ctrl._logger = SimpleNamespace(debug=lambda *a, **k: None)

        ctrl._updatePreview()
        (x, y), = ctrl._commChannel.sigDetectionPreviewUpdated.emitted
        assert x.size > 50 and x.size == y.size
        assert statuses and "foci" in statuses[-1]

        # Frames without a pattern: the reason goes to the status line.
        ctrl._main._currentDataObj = _recording(
            np.random.default_rng(0).normal(100, 3, (40, 64, 64)).astype(np.float32),
            name="noise",
        )
        ctrl._updatePreview()
        x, y = ctrl._commChannel.sigDetectionPreviewUpdated.emitted[-1]
        assert x.size == 0
        assert statuses[-1].startswith("Preview:")


class TestReconstruction:
    def test_a_recording_without_metadata(self):
        scan = make_scan(noise="poisson")
        result = MonalisaLatticeReconstructor().process(
            _recording(scan.frames), _params()
        )
        assert isinstance(result, MonalisaLatticeResult)
        assert result.axis_labels == ["Y", "X"]
        assert result.scale_unit == "px"
        layers = [layer.component for layer in result.display_layers()]
        assert layers == ["reconstruction", "background"]
        assert np.all(np.isfinite(result.data))
        assert result.diagnostics["orientation"] == "+x+y"
        assert result.diagnostics["foci"] > 40

    def test_the_image_is_the_pipelines(self):
        from imswitch.improcess.reconstructors.monalisa.pipeline import (
            PipelineParams,
            reconstruct_scan,
        )

        scan = make_scan()
        result = MonalisaLatticeReconstructor().process(
            _recording(scan.frames), _params(**PLAIN)
        )
        direct = reconstruct_scan(
            scan.frames, None, (22, 22),
            PipelineParams(reassignment="off", frame_gain="off", cell_offsets="off"),
        )
        np.testing.assert_allclose(
            result.data, np.nan_to_num(direct.amplitude.image), rtol=1e-6
        )

    def test_the_scale_comes_from_the_scan_step(self):
        scan = make_scan()
        result = MonalisaLatticeReconstructor().process(
            _recording(scan.frames, _attrs((0.735, 0.735), 0.035)), _params(**PLAIN)
        )
        assert result.scale_unit == "um"
        assert result.axis_scales == pytest.approx([0.035, 0.035])
        assert result.output_pixel_size_nm == pytest.approx(35.0)
        assert result.diagnostics["output pixel (µm)"] == pytest.approx(0.035)
        # 22 steps of 35 nm across a period of 11 px.
        assert result.diagnostics["camera pixel (nm)"] == pytest.approx(70.0, abs=0.1)

    def test_wide_foci_are_reassigned_and_confined_ones_are_not(self):
        wide = make_physical_scan(cells=10, sigma_e=1.2, sigma_d=1.5, seed=3)
        result = MonalisaLatticeReconstructor().process(
            _recording(wide.frames), _params()
        )
        assert result.diagnostics["image"] == "reassigned"
        assert result.diagnostics["shift factor"] == pytest.approx(wide.alpha, abs=0.08)
        placed = MonalisaLatticeReconstructor().process(
            _recording(make_scan().frames), _params()
        )
        assert placed.diagnostics["image"] == "amplitudes placed"

    def test_a_fixed_shift_factor(self):
        wide = make_physical_scan(cells=10, sigma_e=1.2, sigma_d=1.5, seed=3)
        result = MonalisaLatticeReconstructor().process(
            _recording(wide.frames), _params(reassignment="fixed", shift_factor=0.3)
        )
        assert result.diagnostics["image"] == "reassigned"
        assert "shift factor" not in result.diagnostics

    def test_a_given_orientation_is_used(self):
        scan = make_scan(orientation="-x+y")
        found = MonalisaLatticeReconstructor().process(
            _recording(scan.frames), _params(**PLAIN)
        )
        given = MonalisaLatticeReconstructor().process(
            _recording(scan.frames), _params(orientation="-x+y", **PLAIN)
        )
        assert found.diagnostics["orientation"] == "-x+y"
        np.testing.assert_allclose(found.data, given.data)

    def test_several_scans_make_a_time_axis(self):
        scan = make_scan(steps=(11, 11), background=0.0)
        frames = np.concatenate([scan.frames, 2.0 * scan.frames])
        result = MonalisaLatticeReconstructor().process(
            _recording(frames),
            _params(scan_steps_fast=11, scan_steps_slow=11, **PLAIN),
        )
        assert result.axis_labels == ["T", "Y", "X"]
        assert result.data.shape[0] == 2
        inner = (slice(20, -20), slice(20, -20))
        ratio = result.data[1][inner] / result.data[0][inner]
        assert np.median(ratio) == pytest.approx(2.0, rel=0.02)

    def test_the_diagnostics_are_rows_of_a_table(self):
        result = MonalisaLatticeReconstructor().process(
            _recording(make_scan().frames), _params()
        )
        rows = result.table_records()
        assert result.table_columns() == ["result", "quantity", "value"]
        assert {row["quantity"] for row in rows} >= {
            "scan", "foci", "spot sigma (px)", "orientation", "image", "time (s)"
        }
        assert all(row["result"] == result.name for row in rows)


class TestOutputs:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        scan = make_physical_scan(cells=10, sigma_e=1.2, sigma_d=1.5, seed=3)
        return MonalisaLatticeReconstructor().process(
            _recording(scan.frames),
            _params(sharpen=True, pinhole_stack=True, reach_sigma=2.0),
        )

    def test_the_layers(self, result):
        layers = {layer.component: layer for layer in result.display_layers()}
        assert list(layers) == ["reconstruction", "sharpened", "background", "pinholes"]
        assert layers["reconstruction"].visible
        assert not layers["pinholes"].visible
        shapes = {np.shape(layer.data) for layer in layers.values()}
        assert len(shapes) == 1
        for layer in layers.values():
            assert layer.axis_labels == ["Pinhole", "Y", "X"]
            low, high = layer.display_levels
            assert np.isfinite(low) and high > low

    def test_one_image_per_pixel_of_the_footprint(self, result):
        radius = 2.0 * result.diagnostics["spot sigma (px)"]
        span = np.arange(-9, 10)
        expected = int(np.sum(np.add.outer(span**2, span**2) <= radius**2))
        assert result.pinholes.shape == (expected, *result.data.shape)
        dx, dy = result.pinhole_offsets
        assert (dx[0], dy[0]) == (0, 0)
        assert result.diagnostics["pinhole stack"] == f"{expected} pinholes"

    def test_the_mean_of_the_stack_shows_the_structure(self, result):
        """The images are raw and lie on each other: their mean is an image
        of the specimen on the background of the frames."""
        mean = result.pinholes.mean(axis=0)
        inner = (slice(20, -20), slice(20, -20))
        assert np.corrcoef(mean[inner].ravel(), result.data[inner].ravel())[0, 1] > 0.85
        assert np.median(mean[inner]) > 15.0           # the background is in it
        central = result.pinholes[0][inner]
        assert np.corrcoef(central.ravel(), mean[inner].ravel())[0, 1] > 0.9

    def test_the_reconstruction_is_the_same_at_every_pinhole(self, result):
        layer = result.display_layers()[0]
        assert np.shares_memory(layer.data, result.data)
        np.testing.assert_array_equal(layer.data[0], layer.data[-1])

    def test_a_processor_can_take_the_stack(self, result):
        choices = {choice.id: choice for choice in result.processor_input_choices()}
        stack = choices["component:pinholes"].result
        assert stack.axis_labels == ["Pinhole", "Y", "X"]
        assert stack.data.shape == result.pinholes.shape

    def test_the_sharpened_image_is_the_sharper(self, result):
        def fine(image):
            image = image[30:-30, 30:-30]
            return np.std(np.diff(image, axis=1)) / np.std(image)

        assert fine(result.sharpened) > 1.1 * fine(result.data)

    def test_saved_as_an_image(self, result, tmp_path):
        import tifffile

        result.save(tmp_path / "lattice.tiff")
        saved = tifffile.imread(tmp_path / "lattice.tiff")
        np.testing.assert_allclose(np.squeeze(saved), result.data, rtol=1e-6)


class TestFailures:
    def test_frames_of_the_wrong_rank(self):
        with pytest.raises(ValueError, match="frames, rows, cols"):
            MonalisaLatticeReconstructor().process(
                _recording(np.zeros((4, 4))), _params()
            )

    def test_a_pixel_size_needs_a_step(self):
        scan = make_scan()
        with pytest.raises(ValueError, match="step"):
            MonalisaLatticeReconstructor().process(
                _recording(scan.frames), _params(pixel_size_nm=77.0)
            )

    def test_frames_without_a_pattern(self):
        rng = np.random.default_rng(0)
        with pytest.raises(ValueError, match="periodic|peaks"):
            MonalisaLatticeReconstructor().process(
                _recording(rng.poisson(100.0, (484, 64, 64)).astype(float)), _params()
            )

    def test_cancelled(self):
        token = CancellationToken()
        token.cancel()
        context = ReconstructionContext(cancellation_token=token)
        with pytest.raises(ReconstructionCancelled):
            MonalisaLatticeReconstructor().process(
                _recording(make_scan().frames), _params(), context=context
            )

    def test_progress_is_reported(self):
        seen = []
        context = ReconstructionContext(progress_callback=seen.append)
        MonalisaLatticeReconstructor().process(
            _recording(make_scan().frames), _params(**PLAIN), context=context
        )
        assert [update.phase for update in seen][0] == "inspect"
        assert seen[-1].phase == "finalize"
        fractions = [update.fraction for update in seen]
        assert fractions == sorted(fractions)


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
