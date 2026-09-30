"""Tiles are tiles: the partition kind, the point's identity, and payload axes.

A tiling payload session is the Recording widget's scan lapse with a
positioning provider. Its items used to be written with a ``time``
partition -- every tile claiming to be a timepoint -- and a payload file
carried nothing saying which tile it was; the manifest beside it had to be
trusted for that. The payload's axis names were a frame-count guess even
though the writer held the recording's acquisition layout.
"""
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA, PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM, AcquisitionLayout, AcquisitionLoop,
    encode_acquisition_layout,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_point_scan_layouts, with_lapse_partition, with_time_partition,
)
from imswitch.imcontrol.model.workflows.positioning_request import PositioningRequest

from .test_acquisition_layout_adapters import ONE_PULSE_EACH, _scan_info

recording_controller = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.RecordingController'
)
RecordingController = recording_controller.RecordingController
RecMode = recording_controller.RecMode
writer_module = importlib.import_module('imswitch.imcontrol.model.managers.RecordingManager')
WriterThread = writer_module.WriterThread


def _camera_layout():
    return build_point_scan_layouts(
        _scan_info(), ("Camera",), scan_source="ScanControllerPointScan",
        pulse_counts=ONE_PULSE_EACH,
    )["Camera"]


# ----------------------------------------------------------------------
# The partition helper
# ----------------------------------------------------------------------


def test_a_lapse_item_can_be_a_tile_and_replaces_whatever_lapse_partition_was_there():
    timed = with_time_partition(_camera_layout(), index=2, planned_count=9, single_file=False)
    tiled = with_lapse_partition(timed, kind="tile", index=2, planned_count=9, single_file=False)

    assert [(p.kind, p.index, p.planned_count, p.storage) for p in tiled.partitions] == [
        ("tile", 2, 9, "one-file-per-item")
    ]


def test_with_time_partition_is_unchanged():
    layout = with_time_partition(_camera_layout(), index=0, planned_count=3, single_file=True)
    assert [(p.kind, p.storage) for p in layout.partitions] == [("time", "one-group-per-item")]


def test_an_unknown_lapse_partition_kind_is_refused():
    with pytest.raises(ValueError, match="not 'cycle'"):
        with_lapse_partition(_camera_layout(), kind="cycle", index=0,
                             planned_count=1, single_file=False)


# ----------------------------------------------------------------------
# The recording controller applies what the provider says the points are
# ----------------------------------------------------------------------


def _session(**provider):
    stub = SimpleNamespace(
        recMode=RecMode.ScanLapse, lapseCurrent=3, lapseTotal=12,
        recordingArgs={'singleLapseFile': False, 'detectorNames': ['Camera']},
        _producerAcquisitionLayouts={'Camera': _camera_layout()},
    )
    for name in ('setPositioningProvider', 'clearPositioningProvider',
                 '_lapsePartitionKind', '_applyAcquisitionLayoutPartitions',
                 '_awaitPositioning', '_applyPositionedAttributes'):
        setattr(stub, name, getattr(RecordingController, name).__get__(stub))
    if provider:
        stub.setPositioningProvider(provider.pop('provider'), **provider)
    return stub


def _partitions(stub):
    stub._applyAcquisitionLayoutPartitions()
    layout = stub.recordingArgs['acquisitionLayouts']['Camera']
    return [(p.kind, p.index, p.planned_count) for p in layout.partitions]


def test_a_plain_scan_lapse_still_records_timepoints():
    assert _partitions(_session()) == [("time", 3, 12)]


def test_a_tiling_session_records_tiles():
    stub = _session(provider=lambda index: None, partitionKind='tile')
    assert _partitions(stub) == [("tile", 3, 12)]


def test_any_other_positioned_lapse_records_positions():
    assert _partitions(_session(provider=lambda index: None)) == [("position", 3, 12)]


def test_clearing_the_provider_goes_back_to_timepoints():
    stub = _session(provider=lambda index: None, partitionKind='tile')
    stub.clearPositioningProvider()
    assert _partitions(stub) == [("time", 3, 12)]


def test_each_positioned_point_carries_its_own_attributes_and_nothing_leaks():
    requests = {
        0: PositioningRequest(0, attributes={'Tiling:grid_x': 0, 'Tiling:only_first': 1}),
        1: PositioningRequest(1, attributes={'Tiling:grid_x': 1}),
    }
    for request in requests.values():
        request.resolve()
    stub = _session(provider=lambda index: requests[index], partitionKind='tile')
    stub.recordingArgs['attrs'] = {'Camera': {'Laser:power': 5}}
    stub._lapseBaseAttrs = None

    stub.lapseCurrent = 0
    assert stub._awaitPositioning() is True
    stub._applyPositionedAttributes()
    assert stub.recordingArgs['attrs']['Camera'] == {
        'Laser:power': 5, 'Tiling:grid_x': 0, 'Tiling:only_first': 1,
    }

    stub.lapseCurrent = 1
    assert stub._awaitPositioning() is True
    stub._applyPositionedAttributes()
    assert stub.recordingArgs['attrs']['Camera'] == {'Laser:power': 5, 'Tiling:grid_x': 1}


# ----------------------------------------------------------------------
# Tiling hands the point's identity to whichever request the session holds
# ----------------------------------------------------------------------


def _dispatcher():
    tiling = importlib.import_module(
        'imswitch.imcontrol.controller.controllers.TilingController'
    )
    return tiling._RecordingDispatcher(SimpleNamespace(), SimpleNamespace(), 5.0)


def test_the_session_asking_first_still_gets_the_tiles_attributes():
    dispatcher = _dispatcher()
    held = dispatcher.provider(4)                   # the session asks before tiling
    tile = PositioningRequest(4, attributes={'Tiling:grid_x': -1, 'Tiling:grid_y': 2})
    tile.resolve()

    dispatcher.runPoint(tile)

    assert held is not tile
    assert held.outcome.mayProceed
    assert held.attributes == {'Tiling:grid_x': -1, 'Tiling:grid_y': 2}


def test_tiling_asking_first_hands_over_its_own_request():
    dispatcher = _dispatcher()
    tile = PositioningRequest(4, attributes={'Tiling:grid_x': 3})
    tile.resolve()
    dispatcher.runPoint(tile)                       # queued: the session has not asked
    assert dispatcher.provider(4) is tile


class _Signal:
    def connect(self, _slot):
        pass

    def disconnect(self, _slot):
        pass


def _start_session(recording_host):
    tiling = importlib.import_module(
        'imswitch.imcontrol.controller.controllers.TilingController'
    )
    main = SimpleNamespace(controllers={'Recording': recording_host})
    stub = SimpleNamespace(
        _commChannel=SimpleNamespace(_main=main),
        _master=SimpleNamespace(recordingManager=SimpleNamespace(
            sigRecordingEndedDetailed=_Signal(), sigRecordingFailedTyped=_Signal())),
        _logger=SimpleNamespace(info=lambda *_: None, error=lambda *_a, **_k: None),
        _scanTimeoutS=lambda: 5.0,
    )
    stub._endPayloadSession = tiling.TilingController._endPayloadSession.__get__(stub)
    source = SimpleNamespace(useDispatcher=lambda dispatcher: None)
    return tiling.TilingController._startPayloadSession(stub, ['APD'], 4, source, None)


def test_tiling_declares_its_points_as_tiles():
    seen = {}

    class Host:
        def setPositioningProvider(self, provider, *, partitionKind='position'):
            seen['kind'] = partitionKind

        def setCycleTerminalCallback(self, callback):
            pass

    assert _start_session(Host()) is not None
    assert seen == {'kind': 'tile'}


def test_an_older_recording_host_still_runs_the_session():
    seen = {}

    class OlderHost:
        def setPositioningProvider(self, provider):
            seen['provider'] = provider

        def setCycleTerminalCallback(self, callback):
            pass

    assert _start_session(OlderHost()) is not None
    assert 'provider' in seen


# ----------------------------------------------------------------------
# The writer names payload axes from the layout where it determines them
# ----------------------------------------------------------------------


def _assembled(storage, loops):
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA, payload_kind=PAYLOAD_ASSEMBLED_IMAGE,
        detector="APD", storage_axes=storage, event_loops=loops,
    )


_LOOPS = (
    AcquisitionLoop("scan_z", "scan_z", 5, storage_axis="scan_z"),
    AcquisitionLoop("condition", "condition", 2, storage_axis="condition"),
    AcquisitionLoop("scan_y", "scan_y", 16, storage_axis="scan_y"),
    AcquisitionLoop("scan_x", "scan_x", 16, storage_axis="scan_x"),
)


@pytest.mark.parametrize('storage, n_frames, rank, expected', [
    # A point detector's assembled volume, frame wrapper dropped.
    (("frame", "condition", "scan_z", "scan_y", "scan_x"), 1, 4, "CZYX"),
    # The true array order, not a sorted one.
    (("frame", "scan_z", "condition", "scan_y", "scan_x"), 1, 4, "ZCYX"),
    # A rank the layout does not account for: leave it to the guess.
    (("frame", "condition", "scan_z", "scan_y", "scan_x"), 1, 3, None),
])
def test_assembled_payload_axes_come_from_the_layout(storage, n_frames, rank, expected):
    layout = _assembled(storage, _LOOPS)
    assert WriterThread._layout_logical_axes(layout, n_frames, rank) == expected


def test_a_frame_stream_over_a_raster_has_no_single_letter_for_its_frames():
    stream = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA, payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Camera", storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("scan_y", "scan_y", 4),
                     AcquisitionLoop("scan_x", "scan_x", 6)),
    )
    assert WriterThread._layout_logical_axes(stream, 24, 3) is None     # the parked X'/Y' case
    assert WriterThread._layout_logical_axes(stream, 1, 2) == "YX"


def test_a_loop_with_no_ome_letter_leaves_the_naming_to_the_guess():
    resolft = _assembled(("frame", "time", "cycle", "plane"), (
        AcquisitionLoop("time", "time", 2, storage_axis="time"),
        AcquisitionLoop("cycle", "cycle", 3, storage_axis="cycle"),
        AcquisitionLoop("plane", "plane", 4, storage_axis="plane"),
    ))
    assert WriterThread._layout_logical_axes(resolft, 1, 3) is None
    assert WriterThread._layout_logical_axes(None, 1, 2) is None


def _writer_with(layout, *, scan_driven=True):
    writer = WriterThread.__new__(WriterThread)
    writer._attrs = {'APD': {'AcquisitionLayout:json': encode_acquisition_layout(layout)}}
    writer._currentFrames = {'APD': 1}
    writer._recordingMode = 'scan'
    writer._scanDims = [16, 16, 5]
    writer._scanDrivenDetectors = {'APD': scan_driven}
    writer._filePaths = {'APD': 'rec_APD.h5'}
    return writer


def test_the_locator_takes_the_layouts_axes_and_says_when_the_guess_disagreed(monkeypatch):
    lines = []
    monkeypatch.setattr(writer_module, 'logger', SimpleNamespace(info=lines.append))
    info = SimpleNamespace(stored_shape=(1, 5, 2, 16, 16), frame_axis_stored=True, group='APD')

    agreeing = _writer_with(_assembled(("frame", "condition", "scan_z", "scan_y", "scan_x"), _LOOPS))
    locator = agreeing._final_payload_locators({'APD': info})['APD']
    assert (locator.axes, locator.stored_axes) == ("CZYX", "TCZYX")
    assert lines == []

    declared = _writer_with(_assembled(("frame", "scan_z", "condition", "scan_y", "scan_x"), _LOOPS))
    locator = declared._final_payload_locators({'APD': info})['APD']
    assert locator.axes == "ZCYX"                       # the declaration, not the guess
    assert len(lines) == 1 and "ZCYX" in lines[0] and "CZYX" in lines[0]


def test_without_a_layout_the_locator_keeps_the_guess():
    writer = _writer_with(_assembled(("frame", "condition", "scan_z", "scan_y", "scan_x"), _LOOPS))
    writer._attrs = {'APD': {}}
    info = SimpleNamespace(stored_shape=(1, 5, 2, 16, 16), frame_axis_stored=True, group='APD')
    assert writer._final_payload_locators({'APD': info})['APD'].axes == "CZYX"
