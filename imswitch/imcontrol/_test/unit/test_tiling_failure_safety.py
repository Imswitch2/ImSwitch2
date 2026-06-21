"""Failure-path / cancellation tests for TilingWorkflow (WP1 audit follow-up).

These assert the guarantees added for production readiness:
- laser modulation mode is restored even when a tile raises (run() finally),
- a pulsed laser is turned off even if the camera path raises (_grab_image finally),
- a hardware acquisition is stopped even if snap/trigger raises (_grab_image_hw finally),
- ``should_stop`` cancels the scan early but still cleans up and returns to origin.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import TilingParams, TilingWorkflow


def _facade():
    """Minimal mock facade; one good frame by default."""
    facade = MagicMock()
    facade.laser_con.laser_on = MagicMock()
    facade.laser_con.laser_off = MagicMock()
    facade.laser_con.set_constant_power = MagicMock()
    facade.laser_con.set_triggered_mode = MagicMock()
    facade.laser_con.set_modulation_mode = MagicMock()
    facade.cam.prepare_acquisition = MagicMock()
    facade.cam.start_acquisition = MagicMock()
    facade.cam.stop_acquisition = MagicMock()
    facade.cam.prepare_live = MagicMock()
    facade.cam.start_live = MagicMock()
    facade.cam.stop_live = MagicMock()
    facade.cam.wait_for_frame = MagicMock(return_value=True)
    frame = np.zeros((8, 8), dtype=np.uint16)
    facade.cam.get_data = MagicMock(return_value=np.array([frame]))
    facade.trig.connected = False  # default: software path
    facade.trig.snap_trigger = MagicMock()
    facade.stage_con.move_to = MagicMock()
    facade.stage_con.get_position = MagicMock(return_value=(0.0, 0.0))
    return facade


def _params(**kw):
    base = dict(n_tiles=4, save_individual=False, build_stitched_overview=False)
    base.update(kw)
    return TilingParams(**base)


def test_laser_restored_when_tile_raises(tmp_path):
    facade = _facade()
    facade.stage_con.move_to.side_effect = RuntimeError("stage fault")
    wf = TilingWorkflow(facade, _params())

    with pytest.raises(RuntimeError, match="stage fault"):
        wf.run(save_folder=tmp_path)

    # run()'s finally must restore laser modulation mode despite the failure.
    facade.laser_con.set_modulation_mode.assert_called_once()


def test_pulsed_laser_off_when_grab_raises(tmp_path):
    facade = _facade()  # trig.connected False -> software path, pulsed -> _grab_image(pulsed_laser=True)
    facade.cam.get_data.side_effect = RuntimeError("camera fault")
    wf = TilingWorkflow(facade, _params(pulsed=True))

    with pytest.raises(RuntimeError, match="camera fault"):
        wf.run(save_folder=tmp_path)

    # _grab_image finally disables the pulsed laser; run() finally restores mode.
    facade.laser_con.laser_off.assert_called()
    facade.laser_con.set_modulation_mode.assert_called_once()


def test_hw_acquisition_stopped_when_snap_raises(tmp_path):
    facade = _facade()
    facade.trig.connected = True  # enable hardware-triggered path
    facade.trig.snap_trigger.side_effect = RuntimeError("trigger fault")
    wf = TilingWorkflow(facade, _params(pulsed=True, laser_pin=2, camera_pin=3))

    with pytest.raises(RuntimeError, match="trigger fault"):
        wf.run(save_folder=tmp_path)

    # _grab_image_hw finally must stop the acquisition it started.
    facade.cam.stop_acquisition.assert_called()
    facade.laser_con.set_modulation_mode.assert_called_once()


def test_should_stop_cancels_scan_and_cleans_up(tmp_path):
    facade = _facade()
    wf = TilingWorkflow(facade, _params(n_tiles=9))  # rounds to 9 tiles

    calls = {"n": 0}

    def should_stop():
        # Allow exactly two tiles, then request stop.
        stop = calls["n"] >= 2
        calls["n"] += 1
        return stop

    wf.run(save_folder=tmp_path, should_stop=should_stop)

    # Only 2 tiles acquired (2 grabs), far fewer than 9.
    assert facade.cam.get_data.call_count == 2
    # Cleanup still ran, and the origin move happened (recorded positions exist).
    facade.laser_con.set_modulation_mode.assert_called_once()
    assert wf._origin_stage_xy is not None


def test_normal_run_still_restores_mode_once(tmp_path):
    facade = _facade()
    wf = TilingWorkflow(facade, _params(n_tiles=4))
    wf.run(save_folder=tmp_path)
    # Success path unchanged: mode restored exactly once.
    facade.laser_con.set_modulation_mode.assert_called_once()
    assert facade.cam.get_data.call_count == 4
