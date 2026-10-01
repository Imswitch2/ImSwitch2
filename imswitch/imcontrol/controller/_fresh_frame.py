"""A frame that provably started exposing after now.

With a free-running camera ``getLatestFrameShared()`` hands back whatever is
newest in the buffer -- routinely a frame that began exposing before the
stage move it is supposed to follow, or during it. The handshake: open a
"frames after now" chunk-consumer boundary, then require *two* frames past
it. The first may have started exposing before the boundary; the second
provably started after it, so it cannot contain any of the move.

Tiling implemented this for its tiles; autofocus slept a fixed 150 ms and
read the newest frame, which with an exposure longer than that belonged to
the previous Z position -- every focus metric one sweep step off, the vertex
fitted through it, and success reported.
"""

import time

import numpy as np

#: Longest wait for two fresh frames before the newest buffered one is used.
FRESH_FRAME_TIMEOUT_S = 5.0
FRAME_POLL_S = 0.005


def grab_fresh_frame(detector, consumer_key, *, timeout_s=FRESH_FRAME_TIMEOUT_S,
                     poll_s=FRAME_POLL_S, should_stop=None, logger=None, release=True):
    """Return ``(frame, was_fresh)``; the newest buffered frame when it times out.

    ``release`` drops the consumer boundary afterwards; a caller that keeps one
    boundary open across many grabs (tiling) passes False and releases it
    itself.
    """
    try:
        detector.startChunkConsumer(consumer_key)
    except Exception as e:
        if logger is not None:
            logger.warning(
                f'Could not open a fresh-frame boundary ({e}); falling back to '
                f'the latest buffered frame.'
            )
        return _latest_frame(detector), False

    received = 0
    newest = None
    deadline = time.monotonic() + timeout_s
    try:
        while received < 2:
            if should_stop is not None and should_stop():
                break
            try:
                frames = detector.readChunk(consumer_key)
            except Exception as e:
                # Includes an overflow: more frames arrived than the broker
                # retained, which still means frames are flowing.
                if logger is not None:
                    logger.debug(f'Chunk read interrupted ({e})')
                break
            if frames is not None and len(frames) > 0:
                received += len(frames)
                newest = frames[-1]
                continue
            if time.monotonic() > deadline:
                break
            time.sleep(poll_s)
    finally:
        releaser = getattr(detector, 'releaseChunkConsumer', None) if release else None
        if callable(releaser):
            try:
                releaser(consumer_key)
            except Exception:
                pass

    if received >= 2 and newest is not None:
        return np.asarray(newest), True
    return _latest_frame(detector), False


def _latest_frame(detector):
    frame = detector.getLatestFrameShared()
    return None if frame is None else np.asarray(frame)
