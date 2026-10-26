"""The Fiji history tracker against the P0 event logs and synthetic new-schema logs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from imswitch.improcess.fiji import tracker
from imswitch.improcess.fiji.tracker import (
    GRID_POLICY_VERSION,
    GRID_PRESERVING_COMMANDS_V1,
    grid_preserved,
    history_for,
    parse_run_line,
)

P0 = json.loads((Path(__file__).parent / "data" / "fiji" / "p0_event_logs.json").read_text())


# --------------------------------------------------------------------------
# the run(...) parser
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ('run("Gaussian Blur...", "sigma=2");', ("Gaussian Blur...", "sigma=2")),
    ('run("Crop");', ("Crop", "")),
    ('run("Duplicate...", " ");', ("Duplicate...", " ")),
    ('  run( "Median..." ,  "radius=2 stack" ) ;  ', ("Median...", "radius=2 stack")),
    ('run("Macro...", "code=print(\\"hi\\")");', ("Macro...", 'code=print("hi")')),
    ('run("Open...", "path=C:\\\\data\\\\a.tif");', ("Open...", "path=C:\\data\\a.tif")),
    ("run('Crop');", ("Crop", "")),
    ('run("Z Project...", "projection=[Max Intensity]");', ("Z Project...", "projection=[Max Intensity]")),
    ('run("Crop")', ("Crop", "")),
])
def test_parse_run_line_reads_the_command_and_its_options(line, expected):
    assert parse_run_line(line) == expected


@pytest.mark.parametrize("line", [
    "makeRectangle(76, 100, 94, 80);", 'selectImage("a");', 'run("Crop"', 'run("Crop", sigma);',
    'run("Crop", "a") + 1;', 'run("unterminated);', "", 'runx("Crop");', 'run("a", "b" "c");',
])
def test_parse_run_line_refuses_what_is_not_a_run_call(line):
    assert parse_run_line(line) is None


# --------------------------------------------------------------------------
# P0 fixtures (old schema: "t", no seq/end/offsets/changes)
# --------------------------------------------------------------------------

def _steps(history):
    return [(s.command, s.input_id, s.output_id, s.in_place) for s in history.steps]


def test_the_gui_log_gives_the_chain_from_the_crop_back_to_the_source():
    log = P0["gui"]
    history = history_for(-4, log["events"], log["recorder"], sent_ids={-2})

    assert _steps(history) == [
        ("Duplicate...", -2, -3, False),
        ("Gaussian Blur...", -3, -3, True),
        ("Duplicate...", -3, -4, False),
        ("Crop", -4, -4, True),
    ]
    assert history.origin == "sent" and history.origin_id == -2
    by_command = [(s.options, s.options_source) for s in history.steps]
    assert by_command == [(" ", "recorder"), ("sigma=2", "recorder"), (" ", "recorder"), ("", "recorder")]
    crop = history.steps[-1]
    assert "makeRectangle(76, 100, 94, 80);" in crop.context
    # Every menu command has its run line and every image event falls inside a
    # command's window, so nothing is partial and no change went unseen.
    assert all(s.completeness == "recorded" for s in history.steps)
    assert history.unrecorded == "none observed" and history.completeness == "recorded"
    ok, reason = grid_preserved(history)
    assert not ok and "Crop" in reason


def test_a_filter_chain_prefix_of_the_gui_log_keeps_the_grid():
    log = P0["gui"]
    history = history_for(-3, log["events"], log["recorder"], sent_ids={-2})
    assert [s.command for s in history.steps] == ["Duplicate...", "Gaussian Blur..."]
    assert grid_preserved(history) == (True, "")


def test_the_gui_log_with_offsets_gives_the_same_options_and_context():
    log = copy.deepcopy(P0["gui"])
    text = log["recorder"]
    cursor = 0
    for event in log["events"]:
        if event["kind"] != "command":
            continue
        at = text.index(f'run("{event["command"]}"', cursor)
        event["recorder_offset"] = text.rfind("\n", 0, at) + 1
        cursor = at + 1
    history = history_for(-4, log["events"], text, sent_ids={-2})
    old = history_for(-4, P0["gui"]["events"], text, sent_ids={-2})
    assert [(s.options, s.context) for s in history.steps] == [(s.options, s.context) for s in old.steps]
    assert history.steps[-1].context[0] == "makeRectangle(76, 100, 94, 80);"


def test_the_scripted_log_takes_options_from_the_macro_and_sees_each_output():
    log = P0["scripted"]
    crop = history_for(-16, log["events"], log["recorder"], sent_ids={-14})
    assert _steps(crop) == [
        ("Specify...", -14, -14, True),
        ("Duplicate...", -14, -16, False),
        ("8-bit", -16, -16, True),
    ]
    assert [s.options_source for s in crop.steps] == ["macro", "macro", None]
    assert crop.steps[1].options == "title=crop duplicate "
    # A scripted 8-bit with no options looks like menu use but is not in the
    # recorder, so its parameters are unknown: the chain is partial, not recorded.
    assert crop.steps[2].completeness == "partial" and crop.completeness == "partial"
    assert crop.origin == "sent" and not grid_preserved(crop)[0]

    projection = history_for(-17, log["events"], log["recorder"], sent_ids={-14})
    assert [s.command for s in projection.steps] == ["Specify...", "Z Project..."]
    assert projection.steps[-1].output_id == -17 and projection.steps[-1].options == "projection=[Max Intensity] "
    assert projection.completeness == "recorded"

    scaled = history_for(-18, log["events"], log["recorder"], sent_ids={-14})
    assert scaled.steps[-1].command == "Scale..." and scaled.steps[-1].output_id == -18
    assert not scaled.steps[-1].in_place

    blurred = history_for(-15, log["events"], log["recorder"], sent_ids={-14})
    assert _steps(blurred) == [("Duplicate...", -14, -15, False), ("Gaussian Blur...", -15, -15, True)]
    assert grid_preserved(blurred) == (True, "")


# --------------------------------------------------------------------------
# synthetic new-schema logs
# --------------------------------------------------------------------------

class Log:
    """A tiny builder for new-schema event lists."""

    def __init__(self):
        self.events = []
        self.text = ""

    def _add(self, kind, time, **fields):
        self.events.append({"seq": len(self.events), "time": time, "kind": kind, **fields})

    def recorded(self, line):
        self.text += line + "\n"

    def opened(self, time, image, changes=False):
        self._add("opened", time, id=image, title=f"t{image}", changes=changes)

    def updated(self, time, image, changes=False):
        self._add("updated", time, id=image, title=f"t{image}", changes=changes)

    def command(self, time, end, name, current, options=None, line=None):
        offset = len(self.text)
        if line:
            self.recorded(line)
        self._add("command", time, command=name, end=end, current_id=current, options=options,
                  recorder_offset=offset, plugin_class=f"ij.plugin.{name}", plugin_jar="ij.jar")


def _filter_chain():
    log = Log()
    log.updated(1.0, -2)
    log.opened(1.1, -2)
    log.command(2.0, 2.1, "Gaussian Blur...", -2, options="sigma=2 ")
    log.updated(2.05, -2, changes=True)
    log.command(3.0, 3.2, "Median...", -2, line='run("Median...", "radius=2");')
    log.updated(3.1, -2, changes=True)
    log.command(4.0, 4.3, "Duplicate...", -2, line='run("Duplicate...", "title=d duplicate");')
    log.updated(4.1, -3)
    log.opened(4.2, -3)
    log.command(5.0, 5.1, "Invert", -3, line='run("Invert");')
    log.updated(5.05, -3, changes=True)
    return log


def test_a_pure_filter_chain_is_recorded_and_keeps_the_grid():
    log = _filter_chain()
    history = history_for(-3, log.events, log.text, sent_ids={-2})
    assert _steps(history) == [
        ("Gaussian Blur...", -2, -2, True), ("Median...", -2, -2, True),
        ("Duplicate...", -2, -3, False), ("Invert", -3, -3, True),
    ]
    assert [s.options_source for s in history.steps] == ["macro", "recorder", "recorder", "recorder"]
    assert history.steps[0].plugin_jar == "ij.jar" and history.steps[0].end == 2.1
    assert history.unrecorded == "none observed" and history.completeness == "recorded"
    assert grid_preserved(history) == (True, "")
    assert "Median" in history.summary()


def test_a_duplicate_after_a_recorded_selection_is_a_crop():
    log = Log()
    log.opened(1.0, -2)
    log.recorded("makeRectangle(1, 2, 3, 4);")
    log.command(2.0, 2.1, "Duplicate...", -2, line='run("Duplicate...", "title=d");')
    log.opened(2.05, -3)
    history = history_for(-3, log.events, log.text, sent_ids={-2})
    assert history.steps[0].context == ("makeRectangle(1, 2, 3, 4);",)
    ok, reason = grid_preserved(history)
    assert not ok and "selection" in reason and "makeRectangle" in reason


def test_a_scripted_duplicate_sees_the_selection_recorded_after_the_previous_command():
    log = Log()
    log.opened(1.0, -2)
    log.command(2.0, 2.1, "Invert", -2, line='run("Invert");')
    log.recorded("makeOval(1, 2, 3, 4);")
    log.command(3.0, 3.1, "Duplicate...", -2, options="title=d ")
    log.opened(3.05, -3)
    history = history_for(-3, log.events, log.text, sent_ids={-2})
    assert history.steps[1].options_source == "macro" and history.steps[1].context == ("makeOval(1, 2, 3, 4);",)
    assert not grid_preserved(history)[0]


@pytest.mark.parametrize("context, options, keeps", [
    ((), "title=d duplicate", True),
    (('run("Select None");',), "title=d", True),
    (('run("Select All");',), "title=d", False),
    (("setSelectionLocation(1, 2);",), "title=d", False),
    (("doWand(3, 4);",), "title=d", False),
    ((), "title=d duplicate range=1-3", False),
])
def test_the_duplicate_rule(context, options, keeps):
    step = tracker.FijiStep("Duplicate...", -2, -3, options, "macro", context, None, None, None, None, "recorded", False)
    history = tracker.FijiHistory(-3, -2, "sent", (step,), "none observed", (), "recorded")
    assert grid_preserved(history)[0] is keeps


def test_changes_turning_true_without_a_command_is_detected():
    log = Log()
    log.updated(1.0, -2)
    log.opened(1.1, -2, changes=False)
    log.updated(9.0, -2, changes=True)
    history = history_for(-2, log.events, log.text, sent_ids={-2})
    assert history.unrecorded == "detected" and history.completeness == "unknown"
    assert history.steps == () and "outside any recorded command" in history.unrecorded_notes[0]
    assert "changed flag turned on" in history.unrecorded_notes[0]
    assert "outside" in grid_preserved(history)[1]


def test_an_update_after_a_recorded_in_place_edit_is_only_possible():
    log = Log()
    log.opened(1.0, -2)
    log.command(2.0, 2.1, "Invert", -2, line='run("Invert");')
    log.updated(2.05, -2, changes=True)
    log.updated(9.0, -2, changes=True)
    history = history_for(-2, log.events, log.text, sent_ids={-2})
    assert history.unrecorded == "possible" and history.completeness == "partial"
    assert any("display refresh" in note for note in history.unrecorded_notes)


def test_a_display_style_update_is_possible_and_the_creation_burst_is_not_counted():
    log = Log()
    log.updated(1.0, -2)            # before its own "opened": the creation burst
    log.updated(1.05, -2)
    log.opened(1.1, -2)
    assert history_for(-2, log.events, None, sent_ids={-2}).unrecorded == "none observed"
    log.updated(8.0, -2, changes=False)
    history = history_for(-2, log.events, None, sent_ids={-2})
    assert history.unrecorded == "possible" and history.completeness == "partial"


def test_updates_to_an_image_off_the_chain_are_ignored():
    log = Log()
    log.opened(1.0, -2)
    log.opened(1.1, -9)
    log.updated(8.0, -9, changes=True)
    assert history_for(-2, log.events, None, sent_ids={-2}).unrecorded == "none observed"


def test_an_image_nothing_created_and_nothing_sent_has_an_unknown_origin():
    log = Log()
    log.opened(1.0, -7)
    history = history_for(-7, log.events, None, sent_ids={-2})
    assert history.origin == "unknown" and history.origin_id == -7 and history.steps == ()
    assert history.completeness == "unknown" and not grid_preserved(history)[0]


def test_an_opened_file_is_not_derived_from_the_image_that_happened_to_be_current():
    log = Log()
    log.opened(1.0, -2)
    log.command(2.0, 2.5, "Open...", -2, line='run("Open...", "path=a.tif");')
    log.opened(2.4, -3)
    history = history_for(-3, log.events, log.text, sent_ids={-2})
    assert history.origin == "unknown" and history.origin_id is None
    assert [s.command for s in history.steps] == ["Open..."] and history.steps[0].input_id is None


def test_a_loop_in_the_log_is_cut_and_reported():
    # A log that opens -3 and -4 from each other, with neither opened before.
    events = [
        {"kind": "command", "time": 1.0, "command": "Duplicate...", "current_id": -3, "options": "a"},
        {"kind": "opened", "time": 1.1, "id": -4},
        {"kind": "command", "time": 2.0, "command": "Duplicate...", "current_id": -4, "options": "b"},
        {"kind": "opened", "time": 2.1, "id": -3},
    ]
    history = history_for(-3, events, None, sent_ids=set())
    assert history.origin == "unknown" and any("loops" in note for note in history.unrecorded_notes)
    assert len(history.steps) == 2


def test_a_command_that_runs_other_commands_is_partial_and_the_inner_one_owns_the_output():
    log = Log()
    log.opened(1.0, -2)
    log.command(2.0, 5.0, "Run...", -2, options="macro ")
    log.command(3.0, 3.5, "Duplicate...", -2, options="title=d ")
    log.opened(3.4, -3)
    history = history_for(-3, log.events, None, sent_ids={-2})
    assert history.steps[-1].command == "Duplicate..." and history.steps[-1].output_id == -3
    outer = history_for(-2, log.events, None, sent_ids={-2}).steps[0]
    assert outer.command == "Run..." and outer.completeness == "partial"


def test_the_details_are_plain_json_and_the_policy_is_versioned():
    log = _filter_chain()
    history = history_for(-3, log.events, log.text, sent_ids={-2})
    details = history.to_details()
    assert json.loads(json.dumps(details)) == details
    assert details["origin"] == "sent" and len(details["steps"]) == 4 and details["steps"][1]["context"] == []
    assert GRID_POLICY_VERSION == "fiji-grid-v1"
    assert {"Gaussian Blur...", "8-bit", "Fill Holes"} <= GRID_PRESERVING_COMMANDS_V1
    for geometric in ("Crop", "Scale...", "Size...", "Translate...", "Rotate...", "Flip Horizontally", "Canvas Size...",
                      "Z Project...", "Reslice [/]...", "Stack to Images", "Make Substack...", "Duplicate..."):
        assert geometric not in GRID_PRESERVING_COMMANDS_V1


def test_a_log_without_a_recorder_leaves_menu_commands_partial():
    log = _filter_chain()
    history = history_for(-3, log.events, None, sent_ids={-2})
    assert history.steps[1].options is None and history.steps[1].completeness == "partial"
    assert history.completeness == "partial" and "Median" in grid_preserved(history)[1]
