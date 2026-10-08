"""SwabianTimeTaggerManager on the shared card: roles, write-through
trigger levels, the compatibility path, and the scan hold through a
complete mock scan.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    TimeTaggerManager,
)
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)

pytestmark = pytest.mark.nohardware


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()
        self.scanBuildFailures = []

    def reportScanBuildFailure(self, stage, error):
        self.scanBuildFailures.append((stage, error))


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _shared_card(**overrides):
    info = TimeTaggerInfo(simulation=True, **overrides)
    return TimeTaggerManager(info, nidaqManager=_Nidaq())


def _scan_info(manager, nx=4, ny=3):
    return {
        PARTICIPANTS_KEY: [manager.name],
        "img_dims": [nx, ny],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [1.0, 1.0],
        "dwell_time": 10e-6,
        "tot_scan_time_s": 1.0,
    }


# --------------------------------------------------------------------------- #
# Roles on the shared card                                                     #
# --------------------------------------------------------------------------- #


def test_detector_resolves_its_roles_from_the_shared_card():
    card = _shared_card(photonsChannel=-5, photonsTriggerV=-0.2,
                        laserSyncChannel=6, lineClockChannel=7,
                        lineClockTriggerV=1.0)
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )

    assert manager.timeTagger is card
    assert (manager._click_ch, manager._start_ch, manager._line_ch) == (-5, 6, 7)
    assert manager._click_trigger == -0.2
    assert manager._line_trigger == 1.0
    assert manager.parameters["click_role"].value == "photons"
    assert manager.parameters["click_channel"].value == -5
    assert manager.parameters["click_channel"].editable is False
    assert manager.parameters["click_trigger"].value == -0.2


def test_legacy_channel_properties_are_ignored_when_the_block_exists(caplog):
    card = _shared_card(photonsChannel=1)
    with caplog.at_level(logging.INFO):
        manager = SwabianTimeTaggerManager(
            _DetectorInfo({"click_channel": 9, "click_trigger": 9.0}),
            "FLIM", _Nidaq(), timeTaggerManager=card,
        )
    assert manager._click_ch == 1
    assert manager._click_trigger == 0.5
    assert "ignored: click_channel, click_trigger" in caplog.text


def test_changing_a_role_parameter_re_resolves_the_channel():
    card = _shared_card(frameClockChannel=4, frameClockTriggerV=0.9)
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    manager.setParameter("line_role", "frame_clock")
    assert manager._line_ch == 4
    assert manager._line_trigger == 0.9
    assert manager.parameters["line_channel"].value == 4
    assert manager.parameters["line_trigger"].value == 0.9


def test_unconfigured_role_is_rejected_at_construction():
    card = _shared_card()
    with pytest.raises(ValueError, match="line_role 'frame_clock' is not"):
        SwabianTimeTaggerManager(
            _DetectorInfo({"line_role": "frame_clock"}), "FLIM", _Nidaq(),
            timeTaggerManager=card,
        )


def test_channel_parameters_are_read_only_views():
    card = _shared_card()
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    manager.setParameter("click_channel", 42)
    assert manager._click_ch == 1
    assert manager.parameters["click_channel"].value == 1


def test_trigger_level_parameter_writes_through_to_the_card():
    card = _shared_card()
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    manager.setParameter("click_trigger", -0.15)
    assert card.tagger.getTriggerLevel(1) == -0.15
    assert card.channelInfo("photons").trigger_v == -0.15
    assert manager._click_trigger == -0.15


def test_trigger_level_write_is_refused_during_a_scan_hold(caplog):
    card = _shared_card()
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    card.beginScanHold("someone")
    with caplog.at_level(logging.WARNING):
        manager.setParameter("click_trigger", -0.15)
    assert card.tagger.getTriggerLevel(1) == 0.5
    assert manager.parameters["click_trigger"].value == 0.5, "shows the card's value"
    assert "while a scan holds the card" in caplog.text


# --------------------------------------------------------------------------- #
# Compatibility: no block                                                      #
# --------------------------------------------------------------------------- #


def test_without_a_block_the_detector_builds_a_private_card_and_says_so(caplog):
    with caplog.at_level(logging.WARNING):
        manager = SwabianTimeTaggerManager(
            _DetectorInfo({
                "click_channel": -1, "start_channel": 2, "line_channel": 3,
                "click_trigger": -0.1, "trigger_levels": {"3": 0.7},
            }),
            "FLIM", _Nidaq(),
        )
    assert manager._ownsTimeTagger
    assert isinstance(manager.timeTagger, TimeTaggerManager)
    assert (manager._click_ch, manager._start_ch, manager._line_ch) == (-1, 2, 3)
    assert manager._click_trigger == -0.1
    assert manager._line_trigger == 0.7
    assert 'no top-level "timeTagger" block' in caplog.text
    # The warning prints a block that reproduces the legacy properties.
    block = json.loads(caplog.text.split('"timeTagger": ', 1)[1].split("\n")[0])
    assert block == {
        "photonsChannel": -1, "photonsTriggerV": -0.1,
        "laserSyncChannel": 2, "laserSyncTriggerV": 0.5,
        "lineClockChannel": 3, "lineClockTriggerV": 0.7,
    }


def test_without_a_block_and_without_the_library_a_scan_rolls_back():
    """Today's behaviour, kept: no silent mock on a rig."""
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({"click_channel": 1, "start_channel": 2, "line_channel": 3}),
        "FLIM", _Nidaq(),
    )
    assert not manager.timeTagger.connected
    manager._onScanBuilt(_scan_info(manager), {}, [])
    assert manager._flim is None
    assert manager._preparedScanGeneration is None
    stage, error = manager._nidaqManager.scanBuildFailures[0]
    assert stage == "FLIM.prepare"
    assert "Time Tagger is not available" in str(error)
    assert not manager.timeTagger.scanHeld


# --------------------------------------------------------------------------- #
# A whole scan on the mock card                                                #
# --------------------------------------------------------------------------- #


def test_scan_on_the_mock_card_holds_it_until_the_final_frame_lands():
    card = _shared_card()
    nidaq = _Nidaq()
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", nidaq, timeTaggerManager=card,
    )

    manager._onScanBuilt(_scan_info(manager, nx=4, ny=3), {}, [])
    assert manager._preparedScanGeneration is not None
    assert card.scanHeld, "held from preparation on"
    flim = manager._flim
    assert flim.n_pixels == 12
    assert flim.start_channel == 2 and flim.click_channel == 1
    assert manager._ev_pix_begin.trigger_channel == 3
    np.testing.assert_array_equal(
        manager._ev_pix_begin.pattern, np.arange(4) * 10_000_000,
    )
    assert card.tagger.getInputDelay(1) == 0  # t0_ps 0

    # Hand the worker's final frame to the manager the way the Qt signal does.
    generation = manager._preparedScanGeneration
    manager._activeScanGeneration = generation
    intensity = np.zeros((3, 4), np.float32)
    lifetime = np.zeros((3, 4), np.float32)
    manager._on_frame_ready(
        intensity, lifetime, True, np.zeros(391, np.float32),
        np.zeros(391, np.float32), 0.0, generation,
    )
    assert not card.scanHeld, "released once the final frame landed"
    assert manager._rawReady


def test_abort_releases_the_card_and_metadata_names_the_card():
    card = _shared_card(photonsChannel=-1)
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    manager._onScanBuilt(_scan_info(manager), {}, [])
    assert card.scanHeld

    acknowledged = []
    manager.finishScan("abort", lambda: acknowledged.append(True))
    assert acknowledged == [True]
    assert not card.scanHeld

    manager.configureTimeResolvedProducts(
        __import__(
            "imswitch.imcontrol.model.timeresolved", fromlist=["TimeResolvedScanConfig"]
        ).TimeResolvedScanConfig()
    )
    manager._scan = {"scan_info": {}}
    cube = np.ones((2, 2, 4), np.float32)
    manager._store_time_resolved_products(
        cube_counts=cube, intensity=cube.sum(-1), lifetime_s=np.ones((2, 2)) * 1e-9,
        decay_counts=cube.sum((0, 1)), t_axis_ns=np.arange(4, dtype=np.float32),
        global_tau_ns=1.0, peak_bin=0, peak_time_ns=0.0, is_final=True,
    )
    md = manager.getLastTimeResolvedProducts().metadata
    assert md["time_tagger"]["roles"]["photons"]["channel"] == -1
    assert md["click_role"] == "photons"
    assert md["click_channel"] == -1


def test_stop_acquisition_releases_the_card():
    card = _shared_card()
    manager = SwabianTimeTaggerManager(
        _DetectorInfo({}), "FLIM", _Nidaq(), timeTaggerManager=card,
    )
    manager._onScanBuilt(_scan_info(manager), {}, [])
    assert card.scanHeld
    manager.stopAcquisition()
    assert not card.scanHeld
