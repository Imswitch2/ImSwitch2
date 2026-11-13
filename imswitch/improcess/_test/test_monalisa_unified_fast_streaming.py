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
    iter_physical_coordinates,
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


def _multi_plane_fixture(axes=("y", "z"), *, interleaved=False, nt=1,
                         stagger=False, negative_y=False, conditions=1):
    """Synthetic layout with independently identifiable X/Y/Z coordinates."""
    nx, ny, nz = (4 if "x" in axes else 1), (5 if "y" in axes else 1), (3 if "z" in axes else 1)
    loops = []
    if nt > 1:
        loops.append(AcquisitionLoop("t", "time", nt))
    if conditions > 1:
        loops.append(AcquisitionLoop("c", "condition", conditions,
                                     labels=tuple(f"condition_{i}" for i in range(conditions))))
    spatial = {
        "x": AcquisitionLoop("x", "scan_x", nx, step=0.1925, unit="um"),
        "y": AcquisitionLoop("y", "scan_y", ny, step=0.154 if stagger else 0.154,
                             unit="um", direction=-1 if negative_y else 1),
        "z": AcquisitionLoop("z", "scan_z", nz, step=0.15, unit="um"),
    }
    # With Z innermost, frames from a given Z plane are interleaved.
    scan_order = (["y", "x", "z"] if interleaved else ["z", "y", "x"])
    loops.extend(spatial[axis] for axis in scan_order if axis in axes)
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=tuple(loops),
        traversal=tuple(TraversalRule(loop.id, "forward") for loop in loops),
        provenance="recorded",
    )
    base = _scan(stagger=stagger).reshape(5, 4, 64, 64)
    data = []
    for coords in iter_physical_coordinates(layout):
        x, y, z = (int(coords.get(axis, 0)) for axis in ("x", "y", "z"))
        # Vary specimen intensity with Z; no plane may consume a neighbour's
        # frames and still match the finite reference numerically.
        data.append(base[y, x] * (0.7 + z * 0.2 + 0.1 * int(coords.get("c", 0))))
    return (np.asarray(data, np.float32), ResolvedAcquisitionLayout(
        layout=layout, source="explicit-metadata", confidence="certain",
    ), nx * ny * nz * conditions)


@pytest.mark.parametrize("axes", [("x", "y"), ("y", "z"), ("x", "z"), ("x", "y", "z")])
@pytest.mark.parametrize("interleaved", [False, True])
def test_layout_general_fast_complete_scans_match_finite(axes, interleaved):
    """YZ/XZ/XYZ use the same Fast extraction as completed finite scans."""
    frames, layout, per_t = _multi_plane_fixture(axes, interleaved=interleaved,
                                                  negative_y=True)
    params = _params()
    plugin = MonalisaReconstructor()
    finite = plugin.process(_source(frames, layout), params)
    session, plan = _start(plugin, frames[:1], params, layout,
                           frames_per_timepoint=per_t)
    assert plan.axis_labels == ["T", *finite.axis_labels]
    assert plan.out_shape[1:] == finite.data.shape
    # Deliberately break at arbitrary offsets, including inside a plane.
    cut = min(7, per_t)
    session.push(frames[1:cut], 1, cut)
    session.push(frames[cut:], cut, per_t)
    result = session.finish()
    np.testing.assert_allclose(result.data[0], finite.data, rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(result.background[0], finite.background, rtol=1e-5, atol=1e-5)
    assert result.axis_scales == [1., *finite.axis_scales]
    assert result.scanned_axes == finite.scanned_axes
    session.close()


def test_interleaved_z_group_views_preserve_gain_chronology():
    """Group-local gain positions must match finite's selected indices."""
    from imswitch.improcess.reconstructors.monalisa_lattice.scan import (
        layout_scan, timepoint_group_view, validated_timepoint_groups,
    )
    frames, resolved, per_t = _multi_plane_fixture(("x", "y", "z"), interleaved=True)
    scan = layout_scan(resolved, per_t)
    groups = validated_timepoint_groups(scan, per_t)
    assert scan.num_stacks == 3 and all(len(group) == 20 for group in groups)
    assert not np.array_equal(groups[0], np.arange(20))  # actually interleaved
    for group in range(scan.num_stacks):
        view = timepoint_group_view(scan, group)
        np.testing.assert_array_equal(view.index, scan.index[groups[group]])
        for actual, expected in zip(view.gain_positions(0), scan.gain_positions(group)):
            np.testing.assert_array_equal(actual, expected)


def test_layout_multiplane_capability_contract_accepts_yz_xz_xyz():
    for axes in (("y", "z"), ("x", "z"), ("x", "y", "z")):
        frames, layout, per_t = _multi_plane_fixture(axes, interleaved=True)
        info = RawInfo(
            T=1, S=(per_t,), frame_shape=frames.shape[1:], dtype=frames.dtype,
            mode="ScanOnce", acquisition_layout=layout,
        )
        contract = MonalisaReconstructor().contract(info, _params())
        assert contract.init_frames == 1


def test_z_and_condition_groups_keep_separate_semantic_axes():
    frames, layout, per_t = _multi_plane_fixture(
        ("x", "y", "z"), interleaved=True, conditions=2,
    )
    params = _params()
    plugin = MonalisaReconstructor()
    finite = plugin.process(_source(frames, layout), params)
    session, plan = _start(plugin, frames[:1], params, layout,
                           frames_per_timepoint=per_t)
    session.push(frames[1:], 1, per_t)
    result = session.finish()
    assert plan.axis_labels == ["T", "C", "Z", "Y", "X"]
    np.testing.assert_allclose(result.data[0], finite.data, atol=1e-5, rtol=1e-5)
    assert result.condition_labels == finite.condition_labels
    assert result.plane_step_nm == finite.plane_step_nm
    session.close()


def test_multi_plane_two_timepoints_keep_one_result_and_correct_z():
    frames, layout, per_t = _multi_plane_fixture(("x", "y", "z"), nt=2,
                                                  interleaved=True)
    params = _params()
    plugin = MonalisaReconstructor()
    finite = plugin.process(_source(frames, layout), params)
    session, _ = _start(plugin, frames[:1], params, layout, nt=2,
                        frames_per_timepoint=per_t)
    session.push(frames[1:per_t], 1, per_t)
    first = session.result()
    assert first.axis_labels == ["T", "Z", "Y", "X"]
    assert np.isnan(first.data[1]).all()
    session.push(frames[per_t:2 * per_t], per_t, 2 * per_t)
    complete = session.finish()
    np.testing.assert_allclose(complete.data, finite.data, rtol=1e-5, atol=1e-5)
    assert complete.metadata["monalisa_streaming"]["completed_timepoints"] == 2
    session.close()



def _count_gaussian_device_extractions(monkeypatch, calls):
    """Count both scientific estimators (the default Fast mode is Local).

    ExtractionOperator is the Joint fitter; LocalExtractionOperator is a
    separate implementation, not its subclass. Instrumenting only the former
    silently reports zero frames for the default Local mode.
    """
    from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
    from imswitch.improcess.reconstructors.monalisa.local_extraction import LocalExtractionOperator

    for operator_type in (ExtractionOperator, LocalExtractionOperator):
        original_apply = operator_type.apply_device

        def counted(self, batch, *args, _original=original_apply, **kwargs):
            calls.append(len(batch))
            return _original(self, batch, *args, **kwargs)

        monkeypatch.setattr(operator_type, "apply_device", counted)


def test_interleaved_xyz_extracts_each_frame_exactly_once(monkeypatch):
    frames, layout, per_t = _multi_plane_fixture(("x", "y", "z"), interleaved=True)
    plugin, params = MonalisaReconstructor(), _params()
    finite = plugin.process(_source(frames, layout), params)
    calls = []
    _count_gaussian_device_extractions(monkeypatch, calls)
    monkeypatch.setattr(plugin, "process", lambda *a, **kw: pytest.fail(
        "Completed incremental XYZ must finalize retained coefficients, not refit"
    ))
    session, _ = _start(plugin, frames[:1], params, layout, frames_per_timepoint=per_t)
    session.push(frames[1:10], 1, 10)
    session.push(frames[10:37], 10, 37)
    session.push(frames[37:], 37, per_t)
    streamed = session.finish()
    assert sum(calls) == per_t
    np.testing.assert_allclose(streamed.data[0], finite.data, rtol=1e-5, atol=1e-5)
    session.close()


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
    # A reconstruction with secondary scientific layers (background here)
    # must publish an owned full snapshot, not a data-only plane that would
    # leave the displayed background slice stale.
    assert session.live_plane() is None
    complete = session.result()
    np.testing.assert_allclose(complete.data[1], first.data[0], rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(complete.background[1], first.background[0], rtol=1e-5, atol=1e-5)
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


def test_live_only_allows_verified_timepoint_corrections():
    plugin = MonalisaReconstructor()
    defaults = _params()
    info = RawInfo(
        T=1, S=(20,), frame_shape=(64, 64), dtype=np.dtype("float32"),
        mode="ScanOnce", acquisition_layout=_resolved(),
    )
    for intensity, offsets in ((False, False), (True, False), (False, True), (True, True)):
        settings = {**defaults, "intensity_correction": intensity, "cell_offsets": offsets}
        assert plugin.streaming_capability(settings)[0]
        assert plugin.contract(info, settings).params == settings
    # No raw-frame-dependent or other unverified processing becomes available.
    for patch in ({"reassignment": "fixed"}, {"reassignment": "auto"},
                  {"variance_weighting": True}, {"sharpen": True},
                  {"pinhole_stack": True}, {"spot_width_mode": "Auto"},
                  {"background": "auto"}):
        settings = {**defaults, "intensity_correction": True,
                    "cell_offsets": True, **patch}
        supported, reason = plugin.streaming_capability(settings)
        assert not supported and reason
        with pytest.raises(Incompatible):
            plugin.contract(info, settings)


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
    # Before T0 is scientific, the temporary viewer raster need not
    # preallocate every future T of a large 3D timelapse.
    assert volume.data.shape[0] == 1
    np.testing.assert_allclose(volume.data[0], preview.data, equal_nan=True)
    assert not np.shares_memory(volume.data, preview.data)
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

    frames = _scan(stagger=stagger)
    layout = _resolved(stagger=stagger)
    params = _params(stagger=stagger)
    params["placement_mode"] = placement_mode
    plugin = MonalisaReconstructor()
    offline = plugin.process(_source(frames, layout), params)

    calls = []
    _count_gaussian_device_extractions(monkeypatch, calls)

    def unexpected_process(*args, **kwargs):
        pytest.fail("M4B completed a locked/manual timepoint by repeating process()")

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


@pytest.mark.parametrize("stagger", [False, True], ids=["rectangular", "staggered-hex"])
@pytest.mark.parametrize("placement_mode", ["Auto", "Interpolate"])
@pytest.mark.parametrize("intensity,offsets", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_m4b1_timepoint_finalization_matches_finite_without_refit(
    monkeypatch, stagger, placement_mode, intensity, offsets,
):
    """Complete timepoint corrections reuse frame fits, also on an off-grid lattice."""

    frames = _scan(stagger=stagger)
    # Nontrivial frame-dependent brightness, the case gain fitting corrects.
    scales = np.linspace(0.72, 1.26, len(frames), dtype=np.float32)
    frames = (frames * scales[:, None, None]).astype(np.float32)
    layout = _resolved(stagger=stagger)
    params = _params(stagger=stagger)
    params.update({
        "placement_mode": placement_mode,
        "intensity_correction": intensity,
        "cell_offsets": offsets,
    })
    plugin = MonalisaReconstructor()
    offline = plugin.process(_source(frames, layout), params)
    calls = []
    _count_gaussian_device_extractions(monkeypatch, calls)
    monkeypatch.setattr(plugin, "process", lambda *a, **kw: pytest.fail(
        "Incrementally completed timepoint must not call process() again"
    ))
    session, _ = _start(
        plugin, frames[:1], params, layout, frames_per_timepoint=len(frames),
    )
    assert session.provisional_preview() is not None
    assert not session.has_completed_timepoint
    session.push(frames[1:9], 1, 9)
    session.push(frames[9:], 9, len(frames))
    result = session.finish()
    assert sum(calls) == len(frames) and len(calls) == 3
    np.testing.assert_allclose(result.data[0], offline.data, rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(result.background[0], offline.background, rtol=1e-5, atol=1e-5)
    assert result.metadata["monalisa_calibration"]["corrections"]["intensity_correction"] == intensity
    assert result.metadata["monalisa_calibration"]["corrections"]["cell_offsets"] == offsets
    if intensity:
        assert ("frame gain (% rms)" in result.diagnostics
                or "frame gain" in result.diagnostics)
    if offsets:
        assert result.metadata["monalisa_streaming"]["completed_timepoints"] == 1
    session.close()


def test_m4b1_auto_t0_and_incremental_t1_same_corrections():
    """Auto first-scan calibration and subsequent streaming use one correction policy."""
    frames, layout, params = _scan(), _resolved(nt=2), _params()
    params.update(orientation_mode="Auto", intensity_correction=True, cell_offsets=True)
    plugin = MonalisaReconstructor()
    session, _ = _start(plugin, frames, params, layout, nt=2)
    reference = session.result().data[0].copy()
    session.push(frames[:7], len(frames), len(frames) + 7)
    assert session.provisional_preview() is not None
    session.push(frames[7:], len(frames) + 7, 2 * len(frames))
    result = session.finish()
    np.testing.assert_allclose(result.data[1], reference, rtol=1e-4, atol=1e-4)
    assert result.metadata["monalisa_streaming"]["completed_timepoints"] == 2
    session.close()


def test_m4b1_frame_gain_low_signal_fallback_is_identical_for_finite_and_streamed():
    from imswitch.improcess.reconstructors.monalisa.pipeline import resolve_scan_frame_gain
    from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionResult

    extracted = ExtractionResult(
        amplitude=np.zeros((20, 4), dtype=np.float32),
        background=None, haze=None, noise_sigma=None, coefficients=None,
    )
    positions = (np.arange(20) % 4, np.arange(20) // 4)
    gain, diagnostics = resolve_scan_frame_gain(
        extracted, np.ones(4, dtype=bool), (4, 5), positions, "smooth",
    )
    assert gain is None
    assert "frame_gain_skipped" in diagnostics
    gain, diagnostics = resolve_scan_frame_gain(
        extracted, np.ones(4, dtype=bool), (4, 5), positions, "off",
    )
    assert gain is None and diagnostics == {}
    with pytest.raises(ValueError, match="Unknown frame gain"):
        resolve_scan_frame_gain(extracted, np.ones(4, dtype=bool), (4, 5), positions, "unknown")


@pytest.mark.parametrize("axes", [("y", "z"), ("x", "z"), ("x", "y", "z")])
@pytest.mark.parametrize("interleaved", [False, True])
def test_patch_b_first_frame_3d_preview_keeps_correct_axes_and_coverage(axes, interleaved):
    """Show a partial volume without waiting for a single Z plane to finish."""
    frames, layout, per_t = _multi_plane_fixture(axes, interleaved=interleaved, nt=2)
    plugin, params = MonalisaReconstructor(), _params()
    reference = plugin.process(_source(frames, layout), params)
    session, plan = _start(plugin, frames[:1], params, layout,
                           nt=2, frames_per_timepoint=per_t)
    assert not session.has_completed_timepoint
    assert plan.axis_labels == ["T", "Z", "Y", "X"]
    assert plan.out_shape == reference.data.shape
    assert plan.axis_scales == reference.axis_scales
    assert {view.name for view in plan.view_modes} == {"Standard", "Bottom", "Left"}

    preview = session.provisional_preview()
    assert preview is not None  # these fixtures use a directly placeable lattice
    assert preview.axis_labels == ["Z", "Y", "X"]
    assert preview.data.shape == reference.data.shape[1:]
    assert np.isfinite(preview.data).any()
    # The first frame never fills another Z plane (including Z-interleaved).
    assert sum(np.isfinite(plane).any() for plane in preview.data) == 1
    assert session.has_completed_timepoint is False
    with pytest.raises(RuntimeError, match="No complete"):
        session.result()

    volume = session.provisional_volume(preview)
    assert volume.axis_labels == plan.axis_labels
    assert volume.axis_scales == plan.axis_scales
    assert volume.data.shape == (1, *reference.data.shape[1:])
    np.testing.assert_array_equal(np.isnan(volume.data[0]), np.isnan(preview.data))
    assert volume.metadata["monalisa_streaming"]["provisional_preview"]
    assert not np.shares_memory(volume.data, preview.data)
    session.close()


def test_patch_b_interleaved_3d_preview_reuses_buffer_and_preserves_completed_t0():
    """T1's provisional Z volume must never overwrite completed scientific T0."""
    frames, layout, per_t = _multi_plane_fixture(
        ("x", "y", "z"), nt=2, interleaved=True,
    )
    session, _ = _start(MonalisaReconstructor(), frames[:1], _params(), layout,
                        nt=2, frames_per_timepoint=per_t)
    accumulator = session._incremental
    preview_data = accumulator.image()
    assert preview_data is not None
    # GUI refresh with no new frames must reuse a single spatial volume.
    assert accumulator.image() is preview_data
    session.push(frames[1:9], 1, 9)
    assert accumulator.image() is preview_data
    assert np.isfinite(preview_data).any()
    assert not session.has_completed_timepoint

    session.push(frames[9:per_t], 9, per_t)
    complete_t0 = session.result()
    assert complete_t0.axis_labels == ["T", "Z", "Y", "X"]
    assert session.has_completed_timepoint
    assert np.isnan(complete_t0.data[1]).all()
    science = complete_t0.data.copy()

    session.push(frames[per_t:per_t + 1], per_t, per_t + 1)
    provisional_t1 = session.provisional_preview()
    assert provisional_t1 is not None
    assert provisional_t1.metadata["monalisa_streaming"]["timepoint"] == 1
    assert np.isfinite(provisional_t1.data).any()
    np.testing.assert_array_equal(complete_t0.data, science)
    assert session.result().metadata["monalisa_streaming"]["completed_timepoints"] == 1

    session.push(frames[per_t + 1:], per_t + 1, 2 * per_t)
    final = session.finish()
    assert final.metadata["monalisa_streaming"]["completed_timepoints"] == 2
    np.testing.assert_allclose(final.data[0], final.data[1], rtol=1e-5, atol=1e-5)
    session.close()


@pytest.mark.parametrize("axes", [("x", "y"), ("y", "z"), ("x", "y", "z")])
def test_patch_b_begin_does_not_require_a_solvable_first_frame(monkeypatch, axes):
    """An underdetermined interpolation must not fail stream initialization."""
    from imswitch.improcess.reconstructors.monalisa.incremental_fast import (
        IncrementalFastAccumulator, IncrementalFastMultiAccumulator,
    )
    frames, layout, per_t = _multi_plane_fixture(axes)
    plugin, params = MonalisaReconstructor(), _params()
    reference = plugin.process(_source(frames, layout), params)
    with monkeypatch.context() as patch:
        patch.setattr(IncrementalFastAccumulator, "image", lambda self: None)
        patch.setattr(IncrementalFastMultiAccumulator, "image", lambda self: None)
        session, plan = _start(plugin, frames[:1], params, layout,
                               frames_per_timepoint=per_t)
        assert session.provisional_preview() is None
        assert plan.out_shape == (1, *reference.data.shape)
        assert plan.axis_labels == ["T", *reference.axis_labels]
        assert plan.axis_scales == [1., *reference.axis_scales]
    session.push(frames[1:], 1, per_t)
    complete = session.finish()
    np.testing.assert_allclose(complete.data[0], reference.data, rtol=1e-5, atol=1e-5)
    session.close()


def test_patch_b_worker_publishes_provisional_xyz_without_scientific_claim():
    """Qt transport must carry a complete Z volume as one provisional T slice."""
    from imswitch.improcess.execution.qt_workers import StreamProcessWorker

    frames, layout, per_t = _multi_plane_fixture(("y", "z"), nt=2, interleaved=True)
    plugin, params = MonalisaReconstructor(), _params()
    session, _ = _start(plugin, frames[:1], params, layout,
                        nt=2, frames_per_timepoint=per_t)
    worker = StreamProcessWorker(7, plugin, name="recording")
    worker._session = session
    worker._record_snapshot = lambda *args: None
    previews = []
    worker.sigPreview.connect(lambda generation, result, delta: previews.append(
        (generation, result, delta)
    ))
    worker._preview(force=True)
    assert len(previews) == 1
    generation, result, delta = previews[-1]
    assert generation == 7 and delta is None
    assert result.axis_labels == ["T", "Z", "Y", "X"]
    assert result.data.shape[0] == 1
    assert result.metadata["monalisa_streaming"]["provisional_preview"]
    session.push(frames[1:5], 1, 5)
    worker._preview(force=True)
    _, complete, delta = previews[-1]
    assert complete is None
    assert delta[0] == "provisional" and delta[1] == 0
    assert delta[2].shape == (1, *result.data.shape[1:])

    session.push(frames[5:per_t], 5, per_t)
    worker._preview(force=True)
    _, scientific, delta = previews[-1]
    assert delta is None and scientific is not None
    assert scientific.data.shape == (2, *result.data.shape[1:])
    assert scientific.metadata["monalisa_streaming"]["completed_timepoints"] == 1
    assert np.isnan(scientific.data[1]).all()

    # Further previews have a full-Z T slice, but must not mutate the
    # completed scientific snapshot retained by Session.
    published = scientific.data.copy()
    session.push(frames[per_t:per_t + 1], per_t, per_t + 1)
    worker._preview(force=True)
    _, scientific, delta = previews[-1]
    assert scientific is None
    assert delta[0] == "provisional" and delta[1] == 1
    assert delta[2].shape == (1, *published.shape[1:])
    np.testing.assert_array_equal(published, session.result().data)
    session.close()
