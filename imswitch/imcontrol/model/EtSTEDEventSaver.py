"""Saving every etSTED-triggered scan, automatically, in the standard layout.

A triggered scan's APD frame is saved by the recording machinery that saves a
snapshot of an already-taken image (``RecordingManager.snapImagePrev``): the
same structured HDF5 / Zarr layout, detector metadata, laser state and scan
parameter groups as any other recording, so the file opens in ImProcess and
through "Load parameters from file". The frame is the scan's own (read from
the detector once the scan has ended), not whatever the image viewer shows.

The event is written as the ``EtSTED`` metadata group (``EtSTED:<name>``
attributes) and the scan's centres are corrected to the triggered ones in the
``ScanStage`` group (the scan panel's own shared attributes still describe the
panel's centres, not the event's).

Saving runs on one worker thread, after the scan has ended: it cannot delay a
scan start. A failed save is kept; the controller stops triggering on it
(``failure``), so a scan is never run that could not be saved.
"""
import copy
import datetime
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def utc_iso(timestamp=None):
    """Wall-clock time as an ISO-8601 UTC string with microseconds."""
    if timestamp is None:
        moment = datetime.datetime.now(datetime.timezone.utc)
    else:
        moment = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return moment.isoformat(timespec='microseconds')


class EtSTEDEventSaver:
    """One session's folder of per-event scan files."""

    ATTR_GROUP = 'EtSTED'

    def __init__(self, base_dir, save_image, logger=None):
        """``save_image(detector, savename, image, attrs)`` writes one file;
        it returns the list of paths written."""
        self._base_dir = base_dir
        self._save_image = save_image
        self._logger = logger
        self._executor = None
        self._lock = threading.Lock()
        self._pending = []
        self._failure = None
        self.session_dir = None
        self.saved_paths = []

    # -- session ------------------------------------------------------------

    def begin_session(self, session_id):
        """Create the session folder and prove it is writable."""
        self.session_dir = os.path.join(self._base_dir, f'session_{session_id}')
        os.makedirs(self.session_dir, exist_ok=True)
        probe = os.path.join(self.session_dir, '.write_test')
        with open(probe, 'w', encoding='utf-8') as handle:
            handle.write('ok')
        os.remove(probe)
        self._failure = None
        self.saved_paths = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='etsted-save')

    @property
    def failure(self):
        """The message of the first failed save, or None."""
        return self._failure

    def fail(self, message):
        with self._lock:
            if self._failure is None:
                self._failure = str(message)

    def wait_idle(self, timeout=10.0):
        """Wait for queued saves to finish; True if none is left running."""
        with self._lock:
            pending = list(self._pending)
        for future in pending:
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - recorded by the job itself
                pass
        with self._lock:
            return all(f.done() for f in self._pending)

    def close(self, timeout=30.0):
        self.wait_idle(timeout)
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # -- one event ----------------------------------------------------------

    def event_basename(self, event_index, stamp):
        return os.path.join(self.session_dir, f'event_{event_index:04d}_{stamp}')

    def submit(self, event_index, stamp, frames, attrs_by_detector):
        """Save ``{detector: frame}`` for one event, off the calling thread."""
        if self._executor is None:
            raise RuntimeError('The etSTED event saver has no open session.')
        frames = {name: np.array(frame, copy=True) for name, frame in frames.items()}
        attrs_by_detector = copy.deepcopy(attrs_by_detector)
        base = self.event_basename(event_index, stamp)

        def job():
            try:
                for name, frame in frames.items():
                    paths = self._save_image(name, base, frame, {name: attrs_by_detector[name]})
                    with self._lock:
                        self.saved_paths.extend(paths or [])
            except Exception as error:  # noqa: BLE001
                if self._logger is not None:
                    self._logger.error('Saving the triggered scan failed', exc_info=True)
                self.fail(f'event {event_index}: {error}')

        future = self._executor.submit(job)
        with self._lock:
            self._pending.append(future)
        return future


def event_attributes(*, event_index, session_id, coords_px, n_candidates, scan_center_um,
                     scan_axes, pipeline_name, pipeline_params, transform_name,
                     coefficients_name, coefficients, detector_fast, trigger_time,
                     scan_start_time, scan_end_time=None):
    """The ``EtSTED:*`` attributes of one event (flat ``group:key`` names).

    ``coords_px`` is ``(column, row)`` of the camera frame; ``scan_center_um``
    the transformed centres of the first two active scan axes (``scan_axes``).
    """
    prefix = EtSTEDEventSaver.ATTR_GROUP
    values = {
        'route': 'etsted',
        'event_index': int(event_index),
        'session_id': str(session_id),
        'event_coords_px': np.asarray(coords_px, dtype=float),
        'event_coords_order': 'column,row',
        'n_candidates': int(n_candidates),
        'scan_center_um': np.asarray(scan_center_um, dtype=float),
        'scan_axes': ','.join(str(a) for a in scan_axes),
        'pipeline_name': str(pipeline_name),
        'pipeline_params': {str(k): float(v) for k, v in pipeline_params.items()},
        'transform_name': str(transform_name),
        'transform_coefficients_name': str(coefficients_name),
        'transform_coefficients': np.asarray(coefficients, dtype=float),
        'detector_fast': str(detector_fast),
        'trigger_frame_time_utc': trigger_time,
        'scan_start_time_utc': scan_start_time,
    }
    if scan_end_time is not None:
        values['scan_end_time_utc'] = scan_end_time
    out = {}
    for key, value in values.items():
        if isinstance(value, dict):
            import json
            value = json.dumps(value, sort_keys=True)
        out[f'{prefix}:{key}'] = value
    return out
