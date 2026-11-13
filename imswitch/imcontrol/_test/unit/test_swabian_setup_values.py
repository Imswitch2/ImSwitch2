"""A slip in the Swabian detector's setup values never stops ImControl.

A blank field, a word where a number goes, a role the card does not have:
each is named in a warning and the detector takes the default. Found at
the redSTED rig: Config Studio's template had no default for
``line_trigger`` and ``n_bins``, a blank one reached ``float("")`` and
ImControl did not start. (``line_trigger`` has since been retired with the
other card properties: the ``timeTagger`` block describes the card.)
"""

from __future__ import annotations

import logging

import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    TimeTaggerManager,
)
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
    histogram_bins_for_period,
)

pytestmark = pytest.mark.nohardware

_ONE_PERIOD = histogram_bins_for_period(32, 80.0)


class _Signal:
    def connect(self, slot):
        pass


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _on_card(props=None, **card):
    """A detector on a shared mock card (the ``timeTagger`` block)."""
    shared = TimeTaggerManager(TimeTaggerInfo(simulation=True, **card),
                               nidaqManager=_Nidaq())
    return SwabianTimeTaggerManager(
        _DetectorInfo(props or {}), "FLIM", _Nidaq(), timeTaggerManager=shared)


# --------------------------------------------------------------------------- #
# The two rig crashes                                                          #
# --------------------------------------------------------------------------- #


def test_a_blank_n_bins_spans_one_laser_period(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"n_bins": ""})
    assert manager._n_bins == _ONE_PERIOD
    assert manager.parameters["n_bins"].value == _ONE_PERIOD
    assert "managerProperties[\"n_bins\"] = ''" in caplog.text


def test_a_blank_retired_line_trigger_does_not_stop_imcontrol(caplog):
    """Without a block: no card, and the printed block takes the default."""
    with caplog.at_level(logging.ERROR):
        manager = SwabianTimeTaggerManager(
            _DetectorInfo({"line_channel": 3, "line_trigger": ""}),
            "FLIM", _Nidaq())
    assert not manager.timeTagger.configured
    assert '"lineClockChannel": 3, "lineClockTriggerV": 0.5' in caplog.text


# --------------------------------------------------------------------------- #
# Every number                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key, raw, attribute, expected", [
    ("binwidth_ps", "", "_binwidth_ps", 32),
    ("binwidth_ps", "fast", "_binwidth_ps", 32),
    ("binwidth_ps", 0, "_binwidth_ps", 32),
    ("binwidth_ps", 3.5, "_binwidth_ps", 32),
    ("n_bins", -4, "_n_bins", _ONE_PERIOD),
    ("t0_ps", "x", "_t0_ps", 0),
    ("min_counts_per_pixel", -1, "_min_counts_per_pixel", 20),
    ("laser_rep_rate_mhz", 0, "_laser_rep_rate_mhz", 80.0),
    ("laser_rep_rate_mhz", "nan", "_laser_rep_rate_mhz", 80.0),
    ("background_rate_hz", -2.0, "_background_rate_hz", 0.0),
    ("live_fit_period_s", "", "_live_fit_period_s", 1.0),
    ("fit_method", "gauss", "_fit_method", "moment"),
    ("enabled", "maybe", "_enabled", True),
])
def test_an_unreadable_value_takes_the_default_with_a_warning(
        caplog, key, raw, attribute, expected):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({key: raw})
    assert getattr(manager, attribute) == expected
    assert f'managerProperties["{key}"] = {raw!r} is not' in caplog.text


@pytest.mark.parametrize("key, raw, attribute, expected", [
    ("binwidth_ps", "64", "_binwidth_ps", 64),
    ("binwidth_ps", 64.0, "_binwidth_ps", 64),
    ("laser_rep_rate_mhz", " 40 ", "_laser_rep_rate_mhz", 40.0),
    ("t0_ps", -120, "_t0_ps", -120),
    ("fit_method", "phasor", "_fit_method", "phasor"),
])
def test_a_readable_value_is_used_quietly(caplog, key, raw, attribute, expected):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({key: raw})
    assert getattr(manager, attribute) == expected
    assert "managerProperties[" not in caplog.text


def test_enabled_false_as_a_word_is_false():
    """``bool("false")`` is True: the word used to switch the detector on."""
    assert _on_card({"enabled": "false"})._enabled is False
    assert _on_card({"enabled": "False"}).parameters["enabled"].value == "False"
    assert _on_card({"enabled": 0})._enabled is False
    assert _on_card({"enabled": "on"})._enabled is True


def test_null_is_absent_and_quiet(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"binwidth_ps": None, "n_bins": None})
    assert manager._binwidth_ps == 32
    assert manager._n_bins == _ONE_PERIOD
    assert "managerProperties[" not in caplog.text


def test_the_setup_file_is_not_modified():
    props = {"n_bins": "", "enabled": "false", "binwidth_ps": "64"}
    _on_card(props)
    assert props == {"n_bins": "", "enabled": "false", "binwidth_ps": "64"}


def test_retired_card_values_are_named_once_not_parsed(caplog):
    """With the block, a blank retired property is just not read."""
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"line_trigger": "", "click_channel": ""})
    assert manager._line_trigger == 0.5
    assert "no longer read: click_channel, line_trigger" in caplog.text
    assert "managerProperties[" not in caplog.text


# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #


def test_a_role_the_card_does_not_have_falls_back_to_the_default_role(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"line_role": "frame_clock"})
    assert manager._line_role == "line_clock"
    assert manager.parameters["line_role"].value == "line_clock"
    assert manager._line_ch == 3
    assert "line_role 'frame_clock' is not a configured Time Tagger role" \
        in caplog.text


def test_a_role_name_that_does_not_exist_falls_back_to_the_default_role(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"click_role": "photon"})
    assert manager._click_role == "photons"
    assert 'managerProperties["click_role"] = \'photon\' is not one of' \
        in caplog.text


def test_a_frame_role_the_card_does_not_have_falls_back_to_auto(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"frame_role": "sted_pulse"})
    assert manager._frame_role == "auto"
    assert manager._frame_ch is None
    assert "frame_role 'sted_pulse' is not a configured" in caplog.text


class _NoCardApi:
    """The vendor library with no card plugged in."""

    @staticmethod
    def createTimeTagger(*_args):
        raise RuntimeError("no Time Tagger found")


def test_a_card_without_even_the_default_role_is_still_refused():
    """Nothing to fall back to: a line clock nulled in the block."""
    card = TimeTaggerManager(TimeTaggerInfo(lineClockChannel=None),
                             nidaqManager=_Nidaq(), api=_NoCardApi)
    with pytest.raises(ValueError, match="line_role 'line_clock' is not"):
        SwabianTimeTaggerManager(_DetectorInfo({}), "FLIM", _Nidaq(),
                                 timeTaggerManager=card)


# --------------------------------------------------------------------------- #
# Where the blanks came from                                                   #
# --------------------------------------------------------------------------- #


def test_config_studios_new_detector_reads_without_a_warning(caplog):
    """The template had no default for line_trigger and n_bins, so a new
    detector was written with ``""`` for both: the rig's two crashes. It
    offers no retired card property any more either."""
    from imswitch.imcontrol.view.configeditor import editor

    device = editor._build_default_device("SwabianTimeTaggerManager")
    props = device["managerProperties"]
    assert "" not in props.values()
    assert not {"click_channel", "start_channel", "line_channel",
                "click_trigger", "start_trigger", "line_trigger",
                "trigger_levels"} & set(props)
    with caplog.at_level(logging.WARNING):
        manager = _on_card(props)
    assert "managerProperties[" not in caplog.text
    assert "no longer read" not in caplog.text
    assert manager._n_bins == _ONE_PERIOD
