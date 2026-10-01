"""The fresh-frame handshake shared by tiling and autofocus."""

import numpy as np

from imswitch.imcontrol.controller._fresh_frame import grab_fresh_frame


class _Detector:
    def __init__(self, chunks, latest):
        self._chunks = list(chunks)
        self._latest = latest
        self.consumers = []
        self.released = []

    def startChunkConsumer(self, key):
        self.consumers.append(key)

    def releaseChunkConsumer(self, key):
        self.released.append(key)

    def readChunk(self, key):
        return self._chunks.pop(0) if self._chunks else []

    def getLatestFrameShared(self):
        return self._latest


def test_the_second_frame_past_the_boundary_is_the_one_returned():
    stale = np.full((2, 2), 1)
    fresh = np.full((2, 2), 2)
    detector = _Detector(chunks=[[], [stale], [fresh]], latest=stale)
    frame, was_fresh = grab_fresh_frame(detector, 'test', poll_s=0.0)
    assert was_fresh
    np.testing.assert_array_equal(frame, fresh)
    assert detector.consumers == ['test'] and detector.released == ['test']


def test_without_two_frames_in_time_the_newest_buffered_frame_is_returned():
    latest = np.full((2, 2), 7)
    detector = _Detector(chunks=[[np.zeros((2, 2))]], latest=latest)
    frame, was_fresh = grab_fresh_frame(detector, 'test', timeout_s=0.05, poll_s=0.001)
    assert not was_fresh
    np.testing.assert_array_equal(frame, latest)
    assert detector.released == ['test']


def test_a_stop_request_ends_the_wait():
    detector = _Detector(chunks=[], latest=np.zeros((2, 2)))
    frame, was_fresh = grab_fresh_frame(detector, 'test', timeout_s=10.0, poll_s=0.001,
                                        should_stop=lambda: True)
    assert not was_fresh and frame is not None
