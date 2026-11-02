"""Skip entry, Reset, the entries the watch remembers, and lapses with
missing or short timepoints.

The DWC runs with its real workers and threads on real Zarr recordings, as in
test_directory_watcher_wind_down.py, whose stand-ins this file reuses.

    pytest imswitch/improcess/_test/test_directory_watcher_skip_reset.py -v
"""

import os
import time

import numpy as np
import zarr

from imswitch.improcess._test.test_directory_watcher_wind_down import (  # noqa: F401
    _APP,
    _recording,
    _wait_for,
    watch,
)


def _settle(seconds=1.5):
    # long enough for the ObserverWorker to list the root and the StreamWorker
    # to start on whatever it found
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.01)


def _lapse_timepoint(root, t, frames, *, timepoints, frames_per_stack):
    # one timepoint of a CameraLapse stored with a file per timepoint (E6);
    # frame i of timepoint t holds t * frames_per_stack + i + 1, so a blank
    # frame (0) stands out
    group = zarr.open_group(os.path.join(root, f"rec_time{t}_Cam.zarr"), mode="w")
    group.create_group("Cam/metadata/Rec").attrs["Mode"] = "CameraLapse"
    data = group.create_array("Cam/data", shape=(frames, 8, 8), dtype=np.uint16, chunks=(1, 8, 8))
    data[:] = (t * frames_per_stack + np.arange(frames, dtype=np.uint16) + 1)[:, None, None]
    data.attrs.update({
        "recording:frames_committed": frames,
        "writing": False,
        "recording:frames_per_stack": frames_per_stack,
        "recording:expected_frames": frames_per_stack,
        "recording:num_timepoints": timepoints,
        "recording:lapse_index": t,
        "recording:single_lapse_file": False,
    })


def _expected_lapse(timepoints, frames_per_stack, blank_frames):
    expected = np.arange(timepoints * frames_per_stack, dtype=np.uint16) + 1
    expected[list(blank_frames)] = 0
    return expected


# --- Skip entry ------------------------------------------------------------

def test_skip_entry_finishes_the_current_entry_and_moves_on(watch):
    dwc, widget, viewer, root = watch
    _recording(root, "a_live.zarr", 6, still_writing=True)  # never ends on its own
    _recording(root, "b_next.zarr", 4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a_live" in viewer.names())

    dwc.skipEntry()
    _wait_for(lambda: "b_next" in viewer.names())

    a_live = [result for result in viewer.results if result.name == "a_live"][-1]
    assert np.array_equal(a_live.data.reshape(-1, 8, 8)[:6, 0, 0], np.arange(6))
    assert dwc.isLiveReconstructionRunning()  # the watch goes on


def test_skip_entry_without_a_watch_does_nothing(watch):
    dwc, _widget, _viewer, _root = watch
    dwc.skipEntry()
    assert not dwc.isLiveReconstructionRunning()


# --- the entries remembered, and Reset -------------------------------------

def test_an_entry_is_not_reconstructed_again_after_a_restart(watch):
    dwc, widget, viewer, root = watch
    _recording(root, "a.zarr", 4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a" in viewer.names() and not dwc._stream_worker_busy)
    _settle(0.2)  # the last results of a.zarr, still queued, arrive
    widget.monitorFolderCheck.setChecked(False)
    viewer.results.clear()

    widget.monitorFolderCheck.setChecked(True)
    _settle()

    assert viewer.results == []


def test_reset_while_stopped_reconstructs_every_entry_again(watch):
    dwc, widget, viewer, root = watch
    _recording(root, "a.zarr", 4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a" in viewer.names() and not dwc._stream_worker_busy)
    _settle(0.2)  # the last results of a.zarr, still queued, arrive
    widget.monitorFolderCheck.setChecked(False)
    viewer.results.clear()

    dwc.reset()
    widget.monitorFolderCheck.setChecked(True)

    _wait_for(lambda: "a" in viewer.names())


def test_reset_while_monitoring_stops_the_watch_and_forgets(watch):
    dwc, widget, viewer, root = watch
    _recording(root, "a.zarr", 4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a" in viewer.names() and not dwc._stream_worker_busy)
    _settle(0.2)  # the last results of a.zarr, still queued, arrive

    dwc.reset()

    assert not widget.monitorFolderCheck.isChecked()
    assert not dwc.isLiveReconstructionRunning()
    viewer.results.clear()
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "a" in viewer.names())


# --- lapses with missing or short timepoints -------------------------------

def test_a_missing_timepoint_is_filled_with_blank_frames(watch):
    dwc, widget, viewer, root = watch
    for t in (0, 2):  # timepoint 1 was never written
        _lapse_timepoint(root, t, 4, timepoints=3, frames_per_stack=4)
    widget.monitorFolderCheck.setChecked(True)

    _wait_for(lambda: not dwc._stream_worker_busy and not dwc._entry_queue
              and "rec_Cam" in viewer.names())

    result = viewer.results[-1]
    assert result.data.shape[:2] == (3, 4)
    assert np.array_equal(result.data.reshape(-1, 8, 8)[:, 0, 0], _expected_lapse(3, 4, range(4, 8)))


def test_a_short_timepoint_is_filled_up_with_blank_frames(watch):
    dwc, widget, viewer, root = watch
    _lapse_timepoint(root, 0, 4, timepoints=3, frames_per_stack=4)
    _lapse_timepoint(root, 1, 2, timepoints=3, frames_per_stack=4)  # 2 of 4 frames
    _lapse_timepoint(root, 2, 4, timepoints=3, frames_per_stack=4)
    widget.monitorFolderCheck.setChecked(True)

    _wait_for(lambda: not dwc._stream_worker_busy and not dwc._entry_queue
              and "rec_Cam" in viewer.names())

    result = viewer.results[-1]
    assert np.array_equal(result.data.reshape(-1, 8, 8)[:, 0, 0], _expected_lapse(3, 4, (6, 7)))


def test_a_timepoint_appearing_after_its_lapse_was_skipped_is_not_streamed(watch):
    dwc, widget, viewer, root = watch
    _lapse_timepoint(root, 0, 4, timepoints=3, frames_per_stack=4)
    widget.monitorFolderCheck.setChecked(True)
    _wait_for(lambda: "rec_Cam" in viewer.names())

    dwc.skipEntry()
    _wait_for(lambda: not dwc._stream_worker_busy)
    viewer.results.clear()
    _lapse_timepoint(root, 1, 4, timepoints=3, frames_per_stack=4)
    _settle()

    assert viewer.results == []
