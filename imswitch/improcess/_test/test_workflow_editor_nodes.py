"""What the graph view draws, decided without Qt: boxes, ports and their
kinds, which cables may be made, what the quick-add picker offers, and where
boxes go when the file does not say."""

from pathlib import Path

import pytest

from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.graph import layered_positions, place_missing
from imswitch.improcess.model.workfloweditor.nodes import (
    can_feed,
    candidates,
    matches,
    node_infos,
    save_file_name,
)
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry
from imswitch.improcess.workflows.steps import Save

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(bootstrap_registry(user_plugins=False))


def _doc(name, catalog):
    return WorkflowDocument.load(_EXAMPLES / name, catalog)


def test_every_box_says_what_it_is_in_words(catalog):
    infos = node_infos(_doc("split_process_merge_diamond.yaml", catalog))
    assert [infos[s].title for s in ("raw", "rec", "split", "smooth", "merge", "out")] == [
        "Recording", "View only", "Split stack", "Filter", "Merge channels", "Save · HDF5"]
    assert infos["raw"].summary == ("bound when run",)
    assert infos["smooth"].summary == ("Filter  gaussian", "Radius/sigma  2")
    assert infos["merge"].summary == ("defaults",)
    assert infos["out"].summary == ("→ ‹recording›_diamond.h5",)
    assert all(info.issues == () for info in infos.values())


def test_ports_carry_their_declared_kinds(catalog):
    infos = node_infos(_doc("split_process_merge_diamond.yaml", catalog))
    assert [(o.ref, o.kind) for o in infos["raw"].outputs] == [("raw", "recording")]
    assert [(o.label, o.placeholder) for o in infos["split"].outputs] == [("C0", False), ("C1", False), ("C2", True)]
    assert [(i.label, i.kind) for i in infos["smooth"].inputs] == [("C0", "image")]
    merge = infos["merge"].inputs
    assert [slot.adds for slot in merge] == [False, False, True] and merge[-1].index is None
    assert infos["out"].outputs == ()
    python = node_infos(_doc("python_step_signal_trace.yaml", catalog))["script"]
    assert [(o.ref, o.kind) for o in python.outputs] == [("script.level", "any"), ("script.area", "any")]
    assert python.code and not python.code[0].startswith("#")


def test_a_split_keeps_its_lower_members_and_offers_the_next(catalog):
    document = _doc("split_process_merge_diamond.yaml", catalog)
    document.connect("split.C1", "smooth")
    assert [o.label for o in node_infos(document)["split"].outputs] == ["C0", "C1", "C2"]
    document.add_step("process", "filter", step_id="third", inputs=["split.C4"])
    assert [o.label for o in node_infos(document)["split"].outputs] == ["C0", "C1", "C2", "C3", "C4", "C5"]
    fresh = WorkflowDocument(Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
                                            Process("split", "channel-split", inputs=["rec"])]), catalog=catalog)
    assert [(o.label, o.placeholder) for o in node_infos(fresh)["split"].outputs] == [("C0", True)]


def test_an_unconnected_input_the_step_needs_is_a_required_slot(catalog):
    document = WorkflowDocument(Workflow("w", [Source("raw"), Process("blur", "filter"),
                                               Process("merge", "channel-merge", inputs=["blur"])]), catalog=catalog)
    infos = node_infos(document)
    assert [(s.ref, s.required) for s in infos["blur"].inputs] == [(None, True)]
    assert [(s.ref is None, s.required, s.adds) for s in infos["merge"].inputs] == [
        (False, False, False), (True, True, False), (True, False, True)]
    assert infos["blur"].issues


def test_which_cables_fit(catalog):
    document = _doc("split_process_merge_diamond.yaml", catalog)
    assert can_feed(document, "raw", "rec")
    assert not can_feed(document, "raw", "smooth")          # a recording needs a reconstruction first
    assert not can_feed(document, "rec", "raw")             # a source reads nothing
    assert not can_feed(document, "merge", "split")         # a loop
    assert not can_feed(document, "out", "merge")           # a save makes nothing
    assert can_feed(document, "split", "merge") and can_feed(document, "smooth", "out")
    assert not can_feed(document, "smooth", "smooth")
    consolidation = _doc("consolidate_two_sources.yaml", catalog)
    assert can_feed(consolidation, "rl", "both") and not can_feed(consolidation, "left", "both")


def test_a_table_does_not_fit_an_image_filter(catalog):
    document = WorkflowDocument(Workflow("w", [
        Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("coloc", "colocalization", inputs=["rec", "rec"]), Process("blur", "filter", inputs=["rec"]),
        Save("out", input="rec"),
    ]), catalog=catalog)
    assert not can_feed(document, "coloc", "blur")
    assert can_feed(document, "coloc", "out")


def test_the_quick_add_picker_offers_what_takes_the_cable(catalog):
    everything = candidates(catalog)
    assert {c.step_kind for c in everything} == {"source", "reconstruct", "consolidate", "process", "save"}
    recording = candidates(catalog, "recording")
    assert recording and {c.step_kind for c in recording} == {"reconstruct"}
    image = candidates(catalog, "image")
    assert {"filter", "channel-merge"} <= {c.plugin_id for c in image} and ("save", "") in {c.key for c in image}
    assert "table-to-localizations" not in {c.plugin_id for c in image}
    table = candidates(catalog, "table")
    assert {c.plugin_id for c in table if c.step_kind == "process"} >= {"table-to-localizations"}
    assert "filter" not in {c.plugin_id for c in table}
    loose = candidates(catalog, "any")
    assert "filter" in {c.plugin_id for c in loose}
    found = [c for c in everything if matches(c, "merge channels")]
    assert [c.plugin_id for c in found] == ["channel-merge"]


def test_a_save_says_which_file_it_writes():
    assert save_file_name(Save("out", input="x", fmt="tiff")) == "‹recording›_out.tif"
    assert save_file_name(Save("s", input="x", fmt="csv", path_template="{out_dir}/{input_step}{ext}")) == "x.csv"


def test_a_layout_runs_left_to_right_and_fans_out_around_the_chain(catalog):
    document = _doc("split_process_merge_diamond.yaml", catalog)
    heights = {step.id: 100.0 for step in document.steps}
    positions = layered_positions(document.steps, heights, width=200, h_gap=50, v_gap=20)
    assert [positions[s][0] for s in ("raw", "rec", "split", "smooth", "merge", "out")] == [0, 250, 500, 750, 1000, 1250]
    assert positions["smooth"][0] == positions["sharp"][0]
    assert positions["smooth"][1] + 100 + 20 == positions["sharp"][1]
    assert positions["raw"][1] == positions["rec"][1] == positions["split"][1]
    centre = (positions["smooth"][1] + positions["sharp"][1]) / 2
    assert positions["merge"][1] == pytest.approx(centre)


def test_a_box_without_a_place_goes_next_to_its_input_and_overlaps_nothing(catalog):
    document = _doc("split_process_merge_diamond.yaml", catalog)
    heights = {step.id: 100.0 for step in document.steps}
    stored = layered_positions(document.steps, heights, width=200, h_gap=50, v_gap=20)
    document.add_step("process", "filter", step_id="extra", inputs=["smooth"])
    heights["extra"] = 100.0
    placed = place_missing(document.steps, stored, heights, width=200, h_gap=50, v_gap=20)
    assert placed["extra"][0] == stored["smooth"][0] + 250
    ex, ey = placed["extra"]
    for step_id, (x, y) in placed.items():
        if step_id != "extra":
            assert not (ex < x + 208 and x < ex + 208 and ey < y + 108 and y < ey + 108), step_id
    lonely = place_missing([*document.steps, Source("alone")], placed, {**heights, "alone": 80.0},
                           width=200, h_gap=50, v_gap=20)
    assert lonely["alone"][1] > max(y for _x, y in placed.values())
    assert place_missing(document.steps, {}, heights, width=200, h_gap=50, v_gap=20) == layered_positions(
        document.steps, heights, width=200, h_gap=50, v_gap=20)
