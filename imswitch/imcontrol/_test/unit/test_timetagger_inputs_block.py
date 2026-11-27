"""The ``inputs`` block of ``timeTagger``: how a block resolves to inputs and
roles, what is refused, and that a block without it means what it always
meant (every shipped setup)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imswitch.imcontrol.model.SetupInfo import (
    SetupInfo,
    TimeTaggerConfigError,
    TimeTaggerInfo,
    TimeTaggerInputInfo,
)
from imswitch.imcontrol.model.managers.TimeTaggerManager import ROLE_FIELDS, TimeTaggerManager

pytestmark = pytest.mark.nohardware

SETUPS = Path(__file__).resolve().parents[3] / "_data" / "user_defaults" / "imcontrol_setups"


def _inp(channel, kind="detector", **kw):
    return TimeTaggerInputInfo(channel=channel, kind=kind, **kw)


def _legacy_channels(info):
    """What the role fields meant before the inputs block: role -> values."""
    out = {}
    for role, (ch, trig, dead, delay) in ROLE_FIELDS.items():
        if getattr(info, ch) is None:
            continue
        out[role] = (int(getattr(info, ch)), float(getattr(info, trig)),
                     int(getattr(info, dead)) if dead else 0,
                     int(getattr(info, delay)) if delay else 0)
    return out


def _shipped_blocks():
    found = []
    for path in sorted(SETUPS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("timeTagger") is not None:
            found.append(path.name)
    return found


@pytest.mark.parametrize("name", _shipped_blocks())
def test_every_shipped_setup_resolves(name):
    setup = SetupInfo.from_json((SETUPS / name).read_text(encoding="utf-8"), infer_missing=True)
    info = setup.timeTagger
    resolved = info.resolvedInputs()
    if not info.inputs:
        # The older form: one input per role, named by it, same values.
        assert resolved.roles == {r: r for r in resolved.inputs}
        got = {n: (s.channel, s.triggerV, s.deadtimePs, s.delayPs) for n, s in resolved.inputs.items()}
        assert got == _legacy_channels(info)
    card = TimeTaggerManager(info)
    try:
        assert card.conditioned
    finally:
        card.finalize()


def test_default_block_is_the_three_default_roles():
    resolved = TimeTaggerInfo().resolvedInputs()
    assert list(resolved.inputs) == ["photons", "laser_sync", "line_clock"]
    assert resolved.fromInputsBlock is False


def test_roles_resolve_by_name_or_by_the_only_input_of_their_kind():
    info = TimeTaggerInfo(inputs={
        "apd_green": _inp(-1), "apd_red": _inp(-2),
        "sync": _inp(5, "laser_sync"), "lc": _inp(3, "line_clock"),
        "sted_pulse": _inp(6, "marker"), "trigger_out": _inp(7, "marker"),
    })
    resolved = info.resolvedInputs()
    assert resolved.roles == {"laser_sync": "sync", "line_clock": "lc", "sted_pulse": "sted_pulse"}
    assert "photons" not in resolved.roles, "two detector inputs: photons is ambiguous"
    single = TimeTaggerInfo(inputs={"apd": _inp(-1), "lc": _inp(3, "line_clock")}).resolvedInputs()
    assert single.roles == {"photons": "apd", "line_clock": "lc"}


def test_role_fields_at_their_defaults_are_ignored_next_to_inputs():
    info = TimeTaggerInfo(inputs={"apd": _inp(1)})   # photonsChannel defaults to 1 too
    assert list(info.resolvedInputs().inputs) == ["apd"]


def test_a_role_field_set_away_from_its_default_adds_its_input():
    info = TimeTaggerInfo(inputs={"apd_a": _inp(-1), "apd_b": _inp(-4)}, lineClockChannel=7,
                          lineClockDelayPs=-500)
    resolved = info.resolvedInputs()
    assert resolved.roles["line_clock"] == "line_clock"
    assert resolved.inputs["line_clock"] == _inp(7, "line_clock", delayPs=-500)


def test_the_same_role_declared_twice_with_the_same_values_is_accepted():
    info = TimeTaggerInfo(inputs={"line_clock": _inp(7, "line_clock")}, lineClockChannel=7)
    assert list(info.resolvedInputs().inputs) == ["line_clock"]


@pytest.mark.parametrize("info, message", [
    (TimeTaggerInfo(inputs={"a": _inp(-1), "b": _inp(1)}), "both on input 1"),
    (TimeTaggerInfo(inputs={"a": _inp(3, "line_clock"), "b": _inp(4, "line_clock")}),
     "all line_clock inputs"),
    (TimeTaggerInfo(inputs={"lc": _inp(3, "line_clock")}, lineClockChannel=5),
     "line_clock role is declared twice"),
    (TimeTaggerInfo(inputs={"apd": _inp(-1)}, photonsTriggerV=-0.3),
     "photonsTriggerV is set but photonsChannel is not"),
    (TimeTaggerInfo(inputs={"line_clock": _inp(3)}), "must be a 'line_clock' input"),
    (TimeTaggerInfo(inputs={"x": _inp(3, "clock")}), "kind 'clock' is not one of"),
    (TimeTaggerInfo(inputs={"x": _inp(0)}), "numbered from 1"),
    (TimeTaggerInfo(inputs={"none": _inp(2)}), "needs a name"),
    (TimeTaggerInfo(inputs={"x": {"triggerV": 0.5}}), "'channel' is required"),
    (TimeTaggerInfo(inputs={"x": {"channel": 2, "level": 0.5}}), "unknown field"),
    (TimeTaggerInfo(inputs={"apd": _inp(-1)}, lineClockChannel=-1), "both on input 1"),
])
def test_ambiguous_blocks_are_refused_with_what_to_change(info, message):
    with pytest.raises(TimeTaggerConfigError, match=message):
        info.resolvedInputs()
    with pytest.raises(ValueError, match=message):   # the card manager refuses it too
        TimeTaggerManager(info)


def test_an_inputs_block_round_trips_through_a_saved_setup():
    raw = {"timeTagger": {"simulation": True, "inputs": {
        "apd_a": {"channel": -1, "triggerV": -0.25, "deadtimePs": 40000},
        "apd_b": {"channel": -4, "triggerV": -0.25},
        "line_clock": {"channel": 3, "kind": "line_clock"}}}}
    setup = SetupInfo.from_json(json.dumps(raw), infer_missing=True)
    saved = json.loads(json.dumps(setup.to_dict()))
    # A save writes every role field at its default: still the same card.
    assert saved["timeTagger"]["photonsChannel"] == 1
    again = SetupInfo.from_json(json.dumps(saved), infer_missing=True)
    assert again.timeTagger.resolvedInputs() == setup.timeTagger.resolvedInputs()
    assert again.timeTagger.inputs["apd_a"].deadtimePs == 40000


def test_the_card_manager_keys_inputs_by_name_and_roles_as_aliases():
    info = TimeTaggerInfo(simulation=True, filterSyncByPhotons=True, inputs={
        "apd_a": _inp(-1, triggerV=-0.25), "apd_b": _inp(-4, triggerV=-0.25),
        "sync": _inp(2, "laser_sync"), "lc": _inp(3, "line_clock", delayPs=800),
        "fc": _inp(6, "frame_clock"),
    })
    card = TimeTaggerManager(info)
    try:
        assert card.inputs() == ("apd_a", "apd_b", "sync", "lc", "fc")
        assert card.channel("laser_sync") == card.channel("sync") == 2
        assert card.channelInfo("lc").plays == "line_clock"
        assert card.channelInfo("apd_b").plays is None
        # The filter's triggers come from use: no detector yet, no filter.
        assert card.filterChannels() == ([], [2])
        assert card.tagger.getConditionalFilterTrigger() == []
        card.registerDetector("FLIM_a", "apd_a", "flim")
        card.registerDetector("FLIM_b", "apd_b", "flim")
        assert card.waitForFilter()
        assert card.filterChannels() == ([-1, -4], [2])
        assert card.tagger.getConditionalFilterTrigger() == [-1, -4]
        # The positive line delay moves the markers, not the card's input.
        assert card.patternOffsetPs("line_clock") == info.pixelPatternOffsetPs + 800
        assert card.tagger.getInputDelay(3) == 0
        # The frame clock still follows the line clock's delay and dead time.
        card.setDelay("line_clock", -300)
        card.setDeadtime("lc", 50_000)
        assert card.channelInfo("fc").delay_ps == -300
        assert card.channelInfo("fc").deadtime_ps == 50_000
        meta = card.metadata()
        assert set(meta["inputs"]) == set(card.inputs())
        assert meta["inputs"]["apd_b"]["channel"] == -4
        assert set(meta["roles"]) == {"laser_sync", "line_clock", "frame_clock"}
        assert meta["filter_channels"] == [[-1, -4], [2]]
        rates = card.health(0.01).rates_hz
        assert set(rates) == set(card.inputs())
        assert rates["apd_b"] > 0 and rates["apd_a"] > 0
    finally:
        card.finalize()
