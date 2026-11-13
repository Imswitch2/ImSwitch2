"""A slip in the Swabian detector's setup values never stops ImControl.

A blank field, a word where a number goes, a role the card does not have:
each is named in a warning and the detector takes the default. Found at
the redSTED rig: Config Studio's template has no default for
``line_trigger`` and ``n_bins``, a blank one reached ``float("")`` and
ImControl did not start.
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

_LEGACY = {"click_channel": 1, "start_channel": 2, "line_channel": 3}


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


def _legacy(**props):
    """A detector on a private card (no ``timeTagger`` block)."""
    return SwabianTimeTaggerManager(
        _DetectorInfo({**_LEGACY, **props}), "FLIM", _Nidaq())


def _on_card(props, **card):
    """A detector on a shared mock card (the ``timeTagger`` block)."""
    shared = TimeTaggerManager(TimeTaggerInfo(simulation=True, **card),
                               nidaqManager=_Nidaq())
    return SwabianTimeTaggerManager(
        _DetectorInfo(props), "FLIM", _Nidaq(), timeTaggerManager=shared)


def _default(field):
    return TimeTaggerInfo.__dataclass_fields__[field].default


# --------------------------------------------------------------------------- #
# The two rig crashes                                                          #
# --------------------------------------------------------------------------- #


def test_a_blank_line_trigger_takes_the_default_level(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _legacy(line_trigger="")
    assert manager._line_trigger == _default("lineClockTriggerV")
    assert manager.timeTagger.channelInfo("line_clock").trigger_v == \
        _default("lineClockTriggerV")
    assert "managerProperties[\"line_trigger\"] = '' is not a number of volts" \
        in caplog.text


def test_a_blank_n_bins_spans_one_laser_period(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _legacy(n_bins="")
    assert manager._n_bins == histogram_bins_for_period(32, 80.0)
    assert manager.parameters["n_bins"].value == manager._n_bins
    assert "managerProperties[\"n_bins\"] = ''" in caplog.text


# --------------------------------------------------------------------------- #
# Every number                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key, raw, attribute, expected", [
    ("binwidth_ps", "", "_binwidth_ps", 32),
    ("binwidth_ps", "fast", "_binwidth_ps", 32),
    ("binwidth_ps", 0, "_binwidth_ps", 32),
    ("binwidth_ps", 3.5, "_binwidth_ps", 32),
    ("n_bins", -4, "_n_bins", histogram_bins_for_period(32, 80.0)),
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
        manager = _legacy(**{key: raw})
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
        manager = _legacy(**{key: raw})
    assert getattr(manager, attribute) == expected
    assert "managerProperties[" not in caplog.text


def test_enabled_false_as_a_word_is_false():
    """``bool("false")`` is True: the word used to switch the detector on."""
    assert _legacy(enabled="false")._enabled is False
    assert _legacy(enabled="False").parameters["enabled"].value == "False"
    assert _legacy(enabled=0)._enabled is False
    assert _legacy(enabled="on")._enabled is True


def test_null_is_absent_and_quiet(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _legacy(binwidth_ps=None, n_bins=None)
    assert manager._binwidth_ps == 32
    assert manager._n_bins == histogram_bins_for_period(32, 80.0)
    assert "managerProperties[" not in caplog.text


def test_the_setup_file_is_not_modified():
    props = {**_LEGACY, "line_trigger": "", "n_bins": "", "enabled": "false"}
    SwabianTimeTaggerManager(_DetectorInfo(props), "FLIM", _Nidaq())
    assert props == {**_LEGACY, "line_trigger": "", "n_bins": "", "enabled": "false"}


# --------------------------------------------------------------------------- #
# The private card's channels and trigger levels                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", [None, "", "three", 0, 2.5, True])
def test_an_unusable_legacy_channel_takes_the_blocks_default(caplog, raw):
    props = {"click_channel": -1, "start_channel": 2}
    if raw is not None:
        props["line_channel"] = raw
    with caplog.at_level(logging.WARNING):
        manager = SwabianTimeTaggerManager(_DetectorInfo(props), "FLIM", _Nidaq())
    assert (manager._click_ch, manager._start_ch) == (-1, 2)
    assert manager._line_ch == _default("lineClockChannel")
    assert 'managerProperties["line_channel"] is not set' in caplog.text


def test_an_unreadable_trigger_levels_entry_takes_the_default(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _legacy(trigger_levels={"1": -0.1, "3": ""})
    assert manager._click_trigger == -0.1
    assert manager._line_trigger == _default("lineClockTriggerV")
    assert 'managerProperties["trigger_levels"]["3"] = \'\'' in caplog.text


def test_a_trigger_levels_that_is_not_a_map_is_ignored(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _legacy(trigger_levels="0.3")
    assert manager._line_trigger == _default("lineClockTriggerV")
    assert "is not a {\"channel\": volts} map" in caplog.text


def test_an_explicit_trigger_wins_over_a_bad_trigger_levels_entry():
    manager = _legacy(trigger_levels={"3": "x"}, line_trigger=0.8)
    assert manager._line_trigger == 0.8


def test_with_a_block_the_ignored_legacy_values_are_not_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"line_trigger": "", "click_channel": ""})
    assert manager._line_ch == _default("lineClockChannel")
    assert "is not a" not in caplog.text


# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #


def test_a_role_the_card_does_not_have_falls_back_to_the_default_role(caplog):
    with caplog.at_level(logging.WARNING):
        manager = _on_card({"line_role": "frame_clock"})
    assert manager._line_role == "line_clock"
    assert manager.parameters["line_role"].value == "line_clock"
    assert manager._line_ch == _default("lineClockChannel")
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
    detector was written with ``""`` for both: the rig's two crashes."""
    from imswitch.imcontrol.view.configeditor import editor

    device = editor._build_default_device("SwabianTimeTaggerManager")
    props = device["managerProperties"]
    assert "" not in props.values()
    with caplog.at_level(logging.WARNING):
        manager = SwabianTimeTaggerManager(_DetectorInfo(props), "FLIM", _Nidaq())
    assert "managerProperties[" not in caplog.text
    assert manager._n_bins == histogram_bins_for_period(32, 80.0)
