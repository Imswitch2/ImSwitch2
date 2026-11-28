"""A scan-once recording sets its frame boundary at its scan's own boundary.

The recording registers its chunk consumer when it is armed, which is before
the scan is announced, built and started (tens of milliseconds on the NI-DAQ
scans, about a second on the TriggerScope, which uploads its parameters first).
A frame that reaches the camera in between -- a late frame of the previous
scan, a stray trigger -- was recorded as frame 0 and the scan's last frame was
missing: a complete, shifted file. The scan source now says where its first
trigger cannot yet have fired (``beadRecBoundary``: ``'built'`` for the NI-DAQ
scans, ``'started'`` for the TriggerScope) and the recording discards, and
counts, everything it read or the camera held up to that point.

Frames are index coded: the scan's k-th frame is the constant k, a frame that
is not the scan's is 1000.
"""
from io import BytesIO
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
from imswitch.imcontrol.model import RecMode
from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget

from .test_beadrec_frame_alignment import BeadRig, IDEAL, N

STALE = 1000
KEY = 'recording:pre_scan_frames_discarded'


def _record(rig, rec):
    """A scan-once recording to RAM through the Recording controller, as REC
    does it; returns the camera's frame values and the dataset's attributes."""
    files, ended = {}, []
    rig.master.recordingManager.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: files.__setitem__(str(name), file))
    rig.channel.sigRecordingEnded.connect(lambda *a: ended.append(1))
    rec._startRecordingToMemory('Camera', rec.recScanOnce, 'scan-once recording')
    for _ in range(300):
        QtTest.QTest.qWait(50)
        if ended:
            break
    assert ended, 'the recording never ended'
    for file in files.values():
        with h5py.File(BytesIO(file.getvalue()), 'r') as handle:
            node = handle.get('Camera/data')
            if node is not None:
                return node[:, 0, 0].astype(int).tolist(), dict(node.attrs)
    raise AssertionError('no Camera data in the recording')


@pytest.fixture(params=['advanced', 'base'])
def recorded(request, qtbot):
    rig = BeadRig(request.param)
    rec = rig.factory.createController(RecordingController, RecordingWidget(optionsBasic))
    rig.main.controllers['Recording'] = rec
    yield rig, rec
    rig.close()


def test_a_clean_scan_records_its_frames_and_says_none_were_discarded(recorded):
    rig, rec = recorded
    frames, attrs = _record(rig, rec)
    assert frames == IDEAL
    assert int(attrs[KEY]) == 0


def test_a_frame_between_arming_and_the_scan_is_discarded_not_frame_zero(recorded):
    """Was: [STALE, 1 .. N-1], the last frame of the scan missing."""
    rig, rec = recorded
    # sigScanDevicesResolved: after the recording is armed, before the scan is
    # built (the scan source publishes it while arming its iteration).
    rig.channel.sigScanDevicesResolved.connect(lambda *a: rig.trigger(STALE))
    frames, attrs = _record(rig, rec)
    assert frames == IDEAL
    assert int(attrs[KEY]) == 1


def test_a_frame_before_the_recording_is_armed_is_not_in_the_file_either(recorded):
    rig, rec = recorded
    rig.channel.sigScanStarting.connect(lambda *a: rig.trigger(STALE))   # before arming here
    frames, attrs = _record(rig, rec)
    assert frames == IDEAL
    assert int(attrs[KEY]) == 0


def test_the_boundary_is_set_before_the_scans_first_trigger(recorded):
    """The guard against dropping real frames: the signal the recording waits
    for is emitted before any trigger of the scan, so nothing the scan fires
    can be read before the boundary. Here the camera's first scan frame is
    queued the instant sigScanStarted is emitted, the latest point at which an
    NI-DAQ scan can have fired; it must be frame 0 of the file."""
    rig, rec = recorded
    order = []
    rig.channel.sigScanBuilt.connect(lambda *a: order.append(('built', rig.source.real)))
    rig.channel.sigScanStarted.connect(lambda *a: order.append(('started', rig.source.real)))
    frames, _attrs = _record(rig, rec)
    assert frames == IDEAL
    assert order[0] == ('built', 0)         # no scan frame had been produced at Built


def _controller(recMode, source, active=None, manager=None):
    ctrl = RecordingController.__new__(RecordingController)
    ctrl.__dict__.update(
        recMode=recMode, _recordingScanSource=source,
        _commChannel=SimpleNamespace(getActiveScanSource=lambda: active if active is not None else source),
        _master=SimpleNamespace(recordingManager=manager),
        _RecordingController__logger=SimpleNamespace(error=lambda *a, **k: None),
    )
    return ctrl


@pytest.mark.parametrize('recMode, source, expected', [
    (RecMode.ScanOnce, SimpleNamespace(beadRecBoundary='built'), 'built'),
    (RecMode.ScanOnce, SimpleNamespace(beadRecBoundary='started'), 'started'),
    (RecMode.ScanLapse, SimpleNamespace(beadRecBoundary='built'), 'built'),
    (RecMode.ScanOnce, SimpleNamespace(), None),                 # a source that declares nothing
    (RecMode.ScanOnce, None, None),
    (RecMode.SpecFrames, SimpleNamespace(beadRecBoundary='built'), None),
    (RecMode.SpecTime, SimpleNamespace(beadRecBoundary='built'), None),
    (RecMode.UntilStop, SimpleNamespace(beadRecBoundary='built'), None),
    (RecMode.CameraLapse, SimpleNamespace(beadRecBoundary='built'), None),
])
def test_only_scan_bound_recordings_hold_for_a_boundary(recMode, source, expected):
    assert RecordingController._scanBoundaryForRecording(_controller(recMode, source)) == expected


def test_the_boundary_is_handed_over_once_for_its_own_scan_and_signal():
    calls = []
    manager = SimpleNamespace(markScanBoundary=lambda: calls.append(1))
    source, other = SimpleNamespace(beadRecBoundary='built'), object()

    ctrl = _controller(RecMode.ScanOnce,
                       source, manager=manager)
    ctrl._pendingScanBoundary = 'built'
    RecordingController._markRecordingScanBoundary(ctrl, 'started')   # the other signal
    assert calls == []
    ctrl._commChannel = SimpleNamespace(getActiveScanSource=lambda: other)   # another controller's scan
    RecordingController._markRecordingScanBoundary(ctrl, 'built')
    assert calls == []
    ctrl._commChannel = SimpleNamespace(getActiveScanSource=lambda: source)
    RecordingController._markRecordingScanBoundary(ctrl, 'built')
    RecordingController._markRecordingScanBoundary(ctrl, 'built')     # a repeat iteration's Built
    assert calls == [1]


def test_a_recording_that_is_not_holding_ignores_the_boundary(tmp_path):
    from .test_recording_stale_frame_alignment import _record as manager_record

    # the manager-level recording does not hold: a frame in front of the scan is
    # still frame 0 (the scan-bound hold is the controller's to ask for)
    frames, expected = manager_record(
        tmp_path, beforeScan=lambda camera, source: (source.special.append(STALE),
                                                     camera.mockTrigger(1)))
    assert expected == 4 and frames == [STALE, 1, 2, 3]
