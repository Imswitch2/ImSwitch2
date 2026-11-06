"""M3C-2: acquisition following must run the *offline unified* Fast algorithm.

The test exercises an explicit rectangular or staggered/hexagonal lattice,
not a filename-based guess, and verifies that streaming and completed-data
processing produce the same spatial pixels and physical calibration.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    TraversalRule,
)
from imswitch.improcess.model.acquisition_layout_resolver import ResolvedAcquisitionLayout
from imswitch.improcess.execution.contracts import RawInfo, Incompatible
from imswitch.improcess.reconstructors.base import StackInfo, StreamInit
from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
from imswitch.improcess.reconstructors.monalisa.unified_fast_session import (
    ALGORITHM_ID, UnifiedFastStreamingSession, timepoint_layout,
)
from imswitch.improcess._test._monalisa_synthetic import make_scan


def _resolved(nx=4, ny=5, nt=1, *, time_outermost=True, stagger=False):
    loops = (
        ([AcquisitionLoop("t", "time", nt)] if nt > 1 and time_outermost else [])
        + [
            AcquisitionLoop("y", "scan_y", ny, step=(8.66 if stagger else 10.) / ny * 77. / 1000., unit="um", direction=1),
            AcquisitionLoop("x", "scan_x", nx, step=10. / nx * 77. / 1000., unit="um", direction=1),
        ]
        + ([AcquisitionLoop("t", "time", nt)] if nt > 1 and not time_outermost else [])
    )
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
        traversal=tuple(TraversalRule(loop.id, "forward") for loop in loops),
        provenance="recorded",
    )
    return ResolvedAcquisitionLayout(
        layout=layout, source="explicit-metadata", confidence="certain",
    )


def _scan(*, stagger=False):
    if stagger:
        lattice = dict(a1=(10., 0.), a2=(5., 8.66))
    else:
        lattice = dict(a1=(10., 0.), a2=(0., 10.))
    return make_scan(
        shape=(64, 64), steps=(4, 5), sigma=1.7,
        offset=(3.2, 4.5), noise="none", **lattice,
    ).frames.astype(np.float32)


def _params(*, stagger=False, orientation="+x+y"):
    return {
        **MonalisaReconstructor.default_params(),
        "device": "CPU", "preset": "Fast", "pixel_size_nm": 77.,
        "localization_mode": "Manual",
        "manual_period_x_px": 10.,
        "manual_period_y_px": 8.66 if stagger else 10.,
        "manual_offset_x_px": 3.2, "manual_offset_y_px": 4.5,
        "stagger_axis": "X" if stagger else "None",
        "stagger": 0.5 if stagger else 0.,
        "orientation_mode": "Manual", "orientation": orientation,
    }


def _source(frames, layout):
    return SimpleNamespace(
        name="recording", datasetName="CAM", dataPath="recording.zarr",
        dataLoaded=True, data=frames, attrs={"recording:detector_name": "CAM"},
        acquisition_layout=layout,
    )


def _start(reconstructor, frames, params, layout, nt=1, frames_per_timepoint=None):
    session = reconstructor.make_session()
    init = StreamInit(
        name="recording", dataset_name="CAM", data=frames,
        attrs={"recording:detector_name": "CAM"}, source_path="recording.zarr",
        stack_info=StackInfo(
            frame_shape=frames.shape[1:], dtype=frames.dtype,
            frames_per_stack=frames_per_timepoint or len(frames), planned_timepoints=nt,
            acquisition_layout=layout,
        ),
    )
    plan = session.begin(init, params)
    return session, plan


@pytest.mark.parametrize("stagger", [False, True], ids=["rectangular", "hexagonal-stagger"])
def test_one_timepoint_stream_matches_unified_offline_fast(stagger):
    frames, layout, params = _scan(stagger=stagger), _resolved(stagger=stagger), _params(stagger=stagger)
    plugin = MonalisaReconstructor()
    offline = plugin.process(_source(frames, layout), params)
    session, plan = _start(plugin, frames, params, layout)
    assert isinstance(session, UnifiedFastStreamingSession)
    assert plan.axis_labels[0] == "T"
    streamed = session.finish()
    assert streamed.metadata["monalisa_streaming"]["algorithm"] == ALGORITHM_ID
    assert streamed.metadata["monalisa_streaming"]["completed_timepoints"] == 1
    assert streamed.data.shape[-2:] == offline.data.shape[-2:]
    np.testing.assert_allclose(streamed.data[0], offline.data, rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(streamed.axis_scales[-2:], offline.axis_scales[-2:])
    assert streamed.params["preset"] == "Fast"
    session.close()


def test_multi_timepoint_first_preview_and_commit_boundaries():
    frames, params, layout = _scan(), _params(), _resolved(nt=3)
    plugin = MonalisaReconstructor()
    session, plan = _start(plugin, frames, params, layout, nt=3)
    first = session.result()
    assert first.data.shape[0] == 3
    assert session.preview_revision() == 1
    assert np.isnan(first.data[1:]).all()   # never mark missing TP as reconstructed
    # Partial second timepoint is display-only: it changes the preview,
    # but cannot mark T1 complete or modify the previously published T0.
    session.push(frames[:7], len(frames), len(frames) + 7)
    assert session.preview_revision() > 1
    partial = session.provisional_preview()
    assert partial is not None
    assert partial.metadata["monalisa_streaming"]["provisional_preview"] is True
    assert partial.metadata["monalisa_streaming"]["timepoint"] == 1
    assert partial.metadata["monalisa_streaming"]["received_frames"] == 7
    assert session.result().metadata["monalisa_streaming"]["completed_timepoints"] == 1
    assert np.isnan(session.result().data[1:]).all()
    session.push(frames[7:], len(frames) + 7, len(frames) * 2)
    assert session.provisional_preview() is None
    index, plane = session.live_plane()
    assert index == 1 and plane.shape[0] == 1
    np.testing.assert_allclose(plane[0], first.data[0], rtol=1e-5, atol=1e-5)
    session.push(frames, len(frames) * 2, len(frames) * 3)
    final = session.finish()
    assert final.data.shape[0] == 3
    assert np.all(np.isfinite(final.data))
    np.testing.assert_allclose(final.data[0], final.data[1], rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(final.data[1], final.data[2], rtol=1e-5, atol=1e-5)
    # Published snapshot is detached from the worker's mutable result buffer.
    assert not np.shares_memory(first.data, final.data)
    session.close()


def test_unsafe_time_layout_fails_closed_before_processing():
    plugin = MonalisaReconstructor()
    layout = _resolved(nt=3, time_outermost=False)
    info = RawInfo(T=3, S=(20,), frame_shape=(64, 64), dtype=np.dtype("float32"),
                   mode="ScanLapse", acquisition_layout=layout)
    with pytest.raises(Incompatible, match="outermost"):
        plugin.contract(info, _params())
    with pytest.raises(Incompatible, match="outermost"):
        timepoint_layout(layout)


def test_conflicting_scanonce_lapse_metadata_rejected_before_first_frame():
    # A replay of a ScanOnce recording may create scan0..scan4 and correctly
    # declare T=5, but mistakenly retain the input Rec:Mode=ScanOnce. The raw
    # descriptor then says T=1 while the lapse source still says planned=5.
    # Report the metadata discrepancy explicitly rather than failing after T0.
    from imswitch.improcess.reconstructors.monalisa.unified_fast_session import (
        timepoint_stream_contract,
    )

    inconsistent = RawInfo(
        T=1, S=(20,), frame_shape=(64, 64), dtype=np.dtype("uint16"),
        mode="ScanOnce", planned_timepoints=5,
        attrs={"Rec:Mode": "ScanOnce", "recording:num_timepoints": 5},
        acquisition_layout=_resolved(),
    )
    with pytest.raises(Incompatible, match="ScanOnce.*5 timepoints"):
        timepoint_stream_contract(inconsistent, _params())

    corrected = RawInfo(
        T=5, S=(20,), frame_shape=(64, 64), dtype=np.dtype("uint16"),
        mode="ScanLapse", planned_timepoints=5,
        attrs={"Rec:Mode": "ScanLapse", "recording:num_timepoints": 5},
        acquisition_layout=_resolved(),
    )
    per_timepoint, chunk = timepoint_stream_contract(corrected, _params())
    assert per_timepoint == 1 and chunk == 1


def test_capability_contract_never_changes_the_selected_preset():
    plugin = MonalisaReconstructor()
    info = RawInfo(T=2, S=(20,), frame_shape=(64, 64), dtype=np.dtype("uint16"),
                   mode="ScanLapse", acquisition_layout=_resolved(nt=2))
    contract = plugin.contract(info, _params())
    assert contract.init_frames == 1
    assert contract.chunk_frames == 1
    assert contract.params["preset"] == "Fast"
    for preset in ("Full", "Legacy DLL", "Custom"):
        with pytest.raises(Incompatible):
            plugin.contract(info, {"preset": preset})
    unknown = RawInfo(T=None, S=(20,), frame_shape=(64, 64), dtype=np.dtype("uint16"),
                      mode="ScanLapse", acquisition_layout=_resolved(nt=2))
    with pytest.raises(Incompatible, match="declared timepoint count"):
        plugin.contract(unknown, _params())


def test_incomplete_timepoint_never_finalizes_as_complete():
    frames, params, layout = _scan(), _params(), _resolved(nt=2)
    session, _ = _start(MonalisaReconstructor(), frames, params, layout, nt=2)
    session.push(frames[:4], len(frames), len(frames) + 4)
    with pytest.raises(ValueError, match="unprocessed frames"):
        session.finish()
    assert session.preview_revision() > 1
    assert session.provisional_preview() is not None
    assert session.result().metadata["monalisa_streaming"]["completed_timepoints"] == 1
    session.close()


@pytest.mark.parametrize("stagger", [False, True], ids=["rectangular", "hexagonal-stagger"])
def test_explicit_calibration_starts_at_first_frame_and_keeps_partial_out_of_results(stagger):
    frames, layout, params = _scan(stagger=stagger), _resolved(stagger=stagger), _params(stagger=stagger)
    plugin = MonalisaReconstructor()
    offline = plugin.process(_source(frames, layout), params)
    session, plan = _start(
        plugin, frames[:1], params, layout,
        frames_per_timepoint=len(frames),
    )
    assert plan.axis_labels == ["T", "Y", "X"]
    assert plan.out_shape == (1, *offline.data.shape[-2:])
    assert session.preview_revision() == 1
    assert not session.has_completed_timepoint
    with pytest.raises(RuntimeError, match="No complete"):
        session.result()
    preview = session.provisional_preview()
    assert preview is not None
    assert preview.data.shape == offline.data.shape[-2:]
    assert preview.metadata["monalisa_streaming"]["received_frames"] == 1
    assert preview.metadata["monalisa_streaming"]["provisional_preview"]
    session.push(frames[1:8], 1, 8)
    assert session.provisional_preview().metadata["monalisa_streaming"]["received_frames"] == 8
    assert not session.has_completed_timepoint
    session.push(frames[8:], 8, len(frames))
    assert session.provisional_preview() is None
    assert session.has_completed_timepoint
    final = session.finish()
    np.testing.assert_allclose(final.data[0], offline.data, rtol=1e-5, atol=1e-5)
    session.close()


def test_auto_requires_full_t0_before_starting_partial_t1():
    from imswitch.improcess.reconstructors.monalisa.unified_fast_session import (
        timepoint_stream_contract,
    )
    params = _params()
    params["orientation_mode"] = "Auto"
    info = RawInfo(
        T=3, S=(20,), frame_shape=(64, 64), dtype=np.dtype("float32"),
        mode="ScanLapse", planned_timepoints=3,
        acquisition_layout=_resolved(nt=3),
    )
    init_frames, chunk = timepoint_stream_contract(info, params)
    assert init_frames == 20
    assert 20 % chunk == 0
    params = _params()
    params["localization_mode"] = "Auto"
    assert timepoint_stream_contract(info, params)[0] == 20


def test_live_rejects_correction_settings_instead_of_silently_ignoring_them():
    plugin = MonalisaReconstructor()
    defaults = _params()
    for patch in ({"intensity_correction": True}, {"cell_offsets": True},
                  {"reassignment": "fixed"}, {"variance_weighting": True},
                  {"sharpen": True}, {"pinhole_stack": True},
                  {"spot_width_mode": "Auto"}, {"background": "auto"}):
        settings = {**defaults, **patch}
        supported, reason = plugin.streaming_capability(settings)
        assert not supported and reason
        with pytest.raises(Incompatible):
            plugin.contract(RawInfo(
                T=1, S=(20,), frame_shape=(64, 64), dtype=np.dtype("float32"),
                mode="ScanOnce", acquisition_layout=_resolved(),
            ), settings)


def test_provisional_timepoint_updates_are_viewer_only():
    """The queue must not use workflow preview or claim incomplete pixels."""
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    class Publisher:
        def __init__(self):
            self.published = None
            self.snapshots = []
            self.provisionals = []
            self.planes = []

        def provisional_snapshot(self, result):
            self.provisionals.append(result)

        def provisional_plane(self, index, pixels):
            self.planes.append((index, pixels))

        def snapshot(self, result):
            self.published = result
            self.snapshots.append(result)

    publisher = Publisher()
    stub = SimpleNamespace(
        _incrementalRunner=object(),
        queue=SimpleNamespace(current=object()),
        _resultPublisher=publisher,
    )
    partial = SimpleNamespace(metadata={"monalisa_streaming": {"provisional_preview": True}})
    ProcessingQueueController._on_incremental_preview(stub, partial, None)
    assert publisher.provisionals == [partial]
    assert not publisher.snapshots
    assert publisher.published is None

    plane = np.ones((1, 2, 3))
    ProcessingQueueController._on_incremental_preview(
        stub, None, ("provisional", 1, plane)
    )
    assert publisher.planes[0][0] == 1
    assert publisher.planes[0][1] is plane
    assert not publisher.snapshots

    full = SimpleNamespace(metadata={"monalisa_streaming": {"completed_timepoints": 1}})
    ProcessingQueueController._on_incremental_preview(stub, full, None)
    assert publisher.snapshots == [full]
    assert stub._incrementalPublishedResult is full


def test_provisional_volume_uses_real_t_axis_and_never_mutates_complete_data():
    frames, params, layout = _scan(), _params(), _resolved(nt=2)
    plugin = MonalisaReconstructor()
    init = StreamInit(
        name="recording", dataset_name="CAM", data=frames[:1],
        attrs={"recording:detector_name": "CAM"}, source_path="recording.zarr",
        stack_info=StackInfo(
            frame_shape=frames.shape[1:], dtype=frames.dtype,
            frames_per_stack=len(frames), planned_timepoints=2,
            acquisition_layout=layout,
        ),
    )
    session = plugin.make_session()
    session.begin(init, params)
    preview = session.provisional_preview()
    volume = session.provisional_volume(preview)
    assert volume.name == "recording_monalisa"
    assert volume.axis_labels == ["T", "Y", "X"]
    assert volume.data.shape[0] == 2
    np.testing.assert_allclose(volume.data[0], preview.data, equal_nan=True)
    assert np.isnan(volume.data[1]).all()
    assert volume.metadata["monalisa_streaming"]["provisional_preview"] is True
    assert session.has_completed_timepoint is False
    session.close()


@pytest.mark.parametrize("stagger", [False, True], ids=["rectangular", "hexagonal-stagger"])
@pytest.mark.parametrize("placement_mode", ["auto", "interpolate"])
def test_partial_unified_extraction_placement_matches_complete_fast(stagger, placement_mode):
    """Streaming chunks use the exact unified extraction and placement math."""
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
    from imswitch.improcess.reconstructors.monalisa.pipeline import (
        PipelineParams, prepare_geometry, reconstruct_stack,
    )
    from imswitch.improcess.reconstructors.monalisa.scan_frame import raster_scan_index
    from imswitch.improcess.reconstructors.monalisa.incremental_fast import (
        IncrementalFastGeometry, IncrementalFastAccumulator,
    )

    frames = _scan(stagger=stagger)
    index = raster_scan_index(4, 5)
    localization = PatternLocalization.manual_grid(
        10., 8.66 if stagger else 10., 3.2, 4.5,
        stagger_axis="x" if stagger else None,
        stagger=0.5 if stagger else 0.,
    )
    settings = PipelineParams(
        spot_sigma_px=1.7, background="constant", device="CPU",
        reassignment="off", frame_gain="off", cell_offsets="off",
        variance_weighting=False, placement_mode=placement_mode,
        sharpen_sigma_px=None,
    )
    step = np.diag([2.5, 8.66 / 5 if stagger else 2.])
    geometry = prepare_geometry(
        frames[0], step, index, settings, localization=localization,
    )
    calibration = IncrementalFastGeometry(
        geometry, SimpleNamespace(scanned_axes=("x", "y")),
        "+x+y", 77., index,
    )
    incremental = IncrementalFastAccumulator(calibration)
    incremental.push(frames[:1])
    first = incremental.image()
    if placement_mode == "auto":
        assert first is not None and np.isfinite(first).any()
    incremental.push(frames[1:7])
    incremental.push(frames[7:])
    result = incremental.image()
    reference = reconstruct_stack(frames, geometry, index).amplitude.image
    np.testing.assert_allclose(result, reference, atol=1e-4, rtol=1e-4, equal_nan=True)


@pytest.mark.parametrize("stagger", [False, True], ids=["rectangular", "hexagonal-stagger"])
@pytest.mark.parametrize("placement_mode", ["Auto", "Interpolate"])
def test_m4b_explicit_fast_finalizes_retained_extraction_without_process(
    monkeypatch, stagger, placement_mode,
):
    """Each raw frame is fitted exactly once, including the final frame."""
    from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator

    frames = _scan(stagger=stagger)
    layout = _resolved(stagger=stagger)
    params = _params(stagger=stagger)
    params["placement_mode"] = placement_mode
    plugin = MonalisaReconstructor()
    offline = plugin.process(_source(frames, layout), params)

    calls = []
    original_apply = ExtractionOperator.apply_device

    def tracked_apply(self, batch, device="CPU", chunk=64):
        calls.append(len(batch))
        return original_apply(self, batch, device, chunk)

    def unexpected_process(*args, **kwargs):
        pytest.fail("M4B completed a locked/manual timepoint by repeating process()")

    monkeypatch.setattr(ExtractionOperator, "apply_device", tracked_apply)
    monkeypatch.setattr(plugin, "process", unexpected_process)
    session, _ = _start(
        plugin, frames[:1], params, layout, frames_per_timepoint=len(frames),
    )
    session.push(frames[1:7], 1, 7)
    session.push(frames[7:], 7, len(frames))
    assert sum(calls) == len(frames)
    assert len(calls) == 3  # initial frame, two bounded chunks
    assert not hasattr(session, "_chunks")  # no duplicate raw-camera buffer
    final = session.finish()
    np.testing.assert_allclose(final.data[0], offline.data, rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(final.background[0], offline.background,
                               rtol=1e-5, atol=1e-5)
    assert final.metadata["monalisa_calibration"]["resolved_orientation"] == params["orientation"]
    assert final.metadata["monalisa_calibration"]["localization_source"] == "Manual"
    assert final.metadata["monalisa_streaming"]["completed_timepoints"] == 1
    session.close()


def test_m4b_auto_t0_calibrates_once_and_following_timepoints_reuse_extraction(monkeypatch):
    """Auto still needs acquired T0, not a second fit on T1 or T2."""
    frames, layout, params = _scan(), _resolved(nt=3), _params()
    params["orientation_mode"] = "Auto"
    plugin = MonalisaReconstructor()
    original_process = plugin.process
    process_calls = []

    def counted_process(*args, **kwargs):
        process_calls.append(True)
        return original_process(*args, **kwargs)

    monkeypatch.setattr(plugin, "process", counted_process)
    session, _ = _start(plugin, frames, params, layout, nt=3)
    assert len(process_calls) == 1  # complete T0 for orientation detection
    session.push(frames[:8], len(frames), len(frames) + 8)
    session.push(frames[8:], len(frames) + 8, 2 * len(frames))
    session.push(frames, 2 * len(frames), 3 * len(frames))
    assert len(process_calls) == 1  # no repeat process() after T0
    final = session.finish()
    assert final.metadata["monalisa_streaming"]["completed_timepoints"] == 3
    assert final.metadata["monalisa_calibration"]["orientation_source"] == "Auto"
    np.testing.assert_allclose(final.data[0], final.data[1], rtol=1e-4, atol=1e-4)
    np.testing.assert_allclose(final.data[1], final.data[2], rtol=1e-4, atol=1e-4)
    session.close()
