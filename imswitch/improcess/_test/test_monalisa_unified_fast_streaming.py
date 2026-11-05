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


def _start(reconstructor, frames, params, layout, nt=1):
    session = reconstructor.make_session()
    init = StreamInit(
        name="recording", dataset_name="CAM", data=frames,
        attrs={"recording:detector_name": "CAM"}, source_path="recording.zarr",
        stack_info=StackInfo(
            frame_shape=frames.shape[1:], dtype=frames.dtype,
            frames_per_stack=len(frames), planned_timepoints=nt,
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
    # Partial second timepoint must not create fake preview or output.
    session.push(frames[:7], len(frames), len(frames) + 7)
    assert session.preview_revision() == 1
    assert session.live_plane() is None
    session.push(frames[7:], len(frames) + 7, len(frames) * 2)
    assert session.preview_revision() == 2
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


def test_capability_contract_never_changes_the_selected_preset():
    plugin = MonalisaReconstructor()
    info = RawInfo(T=2, S=(20,), frame_shape=(64, 64), dtype=np.dtype("uint16"),
                   mode="ScanLapse", acquisition_layout=_resolved(nt=2))
    contract = plugin.contract(info, _params())
    assert contract.init_frames == 20
    assert 20 % contract.chunk_frames == 0
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
    assert session.preview_revision() == 1
    session.close()
