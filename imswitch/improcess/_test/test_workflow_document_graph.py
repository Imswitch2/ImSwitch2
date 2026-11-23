"""The workflow document's graph operations and their undo history.

A graph editor lets the user connect anything to anything; the document must
turn every gesture into a file that still runs: the list put back in order
(Source and Save steps keeping theirs), loops refused, a Save never without
its input, positions kept as plain floats, and every edit undoable exactly.
"""

from pathlib import Path

import numpy as np
import pytest
import yaml

from imswitch.improcess.model.workfloweditor import DocumentError, WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.graph import (
    CycleError,
    ancestors,
    descendants,
    is_ordered,
    layers,
    stable_order,
)
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry
from imswitch.improcess.workflows.steps import Consolidate, Ref, Save

_EXAMPLE_DIR = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
_EXAMPLES = sorted(_EXAMPLE_DIR.glob("*.yaml"))


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(bootstrap_registry(user_plugins=False))


def _chain(catalog):
    """raw -> rec -> split -> blur(split.C0) -> out, as a document."""
    workflow = Workflow("chain", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("blur", "filter", {"radius": 1.5}, inputs=["split.C0"]),
        Save("out", input="blur", fmt="tiff"),
    ])
    return WorkflowDocument(workflow, catalog=catalog)


def _example(name, catalog):
    return WorkflowDocument.load(_EXAMPLE_DIR / name, catalog)


def _ids(document):
    return [step.id for step in document.steps]


def _of_kind(document, cls):
    return [step.id for step in document.steps if isinstance(step, cls)]


def _without_editor(payload):
    payload = dict(payload)
    metadata = {key: value for key, value in (payload.get("metadata") or {}).items() if key != "editor"}
    payload.pop("metadata", None)
    if metadata:
        payload["metadata"] = metadata
    return payload


def _reloads(document):
    """The document's file, written and read back."""
    return Workflow.from_dict(yaml.safe_load(document.to_yaml()))


# -- graph helpers -----------------------------------------------------------------

def test_a_valid_order_is_left_as_it_is(catalog):
    for path in _EXAMPLES:
        steps = list(_example(path.name, catalog).steps)
        assert is_ordered(steps), path.name
        assert stable_order(steps) == steps, path.name


def test_the_order_is_repaired_by_delaying_only_what_must_wait():
    steps = [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("a", "filter", inputs=["rec"]),
        Process("b", "filter", inputs=["d"]),
        Process("c", "filter", inputs=["rec"]),
        Process("d", "filter", inputs=["a"]),
    ]
    assert not is_ordered(steps)
    ordered = stable_order(steps)
    assert [step.id for step in ordered] == ["raw", "rec", "a", "c", "d", "b"]
    assert stable_order(ordered) == ordered


def test_saves_keep_their_order_when_the_list_is_repaired():
    # Without the rule, "s2" (whose input is ready early) would overtake "s1".
    steps = [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("late", "filter", inputs=["mid"]),
        Save("s1", input="late"),
        Save("s2", input="rec"),
        Process("mid", "filter", inputs=["rec"]),
    ]
    assert [step.id for step in stable_order(steps)] == ["raw", "rec", "mid", "late", "s1", "s2"]


def test_a_loop_has_no_order():
    steps = [Source("raw"), Process("a", "filter", inputs=["b"]), Process("b", "filter", inputs=["a"])]
    with pytest.raises(CycleError, match="a, b"):
        stable_order(steps)


def test_ancestry_and_columns_follow_the_cables(catalog):
    steps = _example("split_process_merge_diamond.yaml", catalog).steps
    assert ancestors(steps, "merge") == {"raw", "rec", "split", "smooth", "sharp"}
    assert descendants(steps, "split") == {"smooth", "sharp", "merge", "out"}
    assert ancestors(steps, "raw") == set() and descendants(steps, "out") == set()
    assert layers(steps) == [["raw"], ["rec"], ["split"], ["smooth", "sharp"], ["merge"], ["out"]]


def test_a_column_follows_the_order_of_what_it_reads_so_cables_do_not_cross():
    steps = [
        Source("top"), Source("bottom"),
        Reconstruct("from_bottom", "view-only", inputs=["bottom"]),
        Reconstruct("from_top", "view-only", inputs=["top"]),
    ]
    assert layers(steps) == [["top", "bottom"], ["from_top", "from_bottom"]]


# -- connect ------------------------------------------------------------------------

def test_a_second_cable_replaces_a_single_input(catalog):
    document = _chain(catalog)
    document.connect("split.C1", "blur")
    assert document.step("blur").inputs == [Ref("split", "C1")]


def test_a_cable_into_a_variadic_step_adds_an_input(catalog):
    document = _example("split_process_merge_diamond.yaml", catalog)
    document.connect("split.C2", "merge")
    assert document.step("merge").inputs == [Ref("smooth"), Ref("sharp"), Ref("split", "C2")]


def test_adding_a_cable_to_a_single_input_process_fans_it_out(catalog):
    document = _chain(catalog)
    document.connect("split.C1", "blur", add=True)
    assert document.step("blur").inputs == [Ref("split", "C0"), Ref("split", "C1")]
    assert "blur.out1" in {option.ref for option in document.port_options(None)}
    assert document.issues() == []


def test_an_index_replaces_that_input(catalog):
    document = _example("split_process_merge_diamond.yaml", catalog)
    document.connect("split.C3", "merge", index=1)
    assert document.step("merge").inputs == [Ref("smooth"), Ref("split", "C3")]


def test_a_save_and_a_reconstruction_always_have_their_input_replaced(catalog):
    document = _chain(catalog)
    document.connect("split.C1", "out", add=True)
    assert document.step("out").input == Ref("split", "C1")
    document.add_step("source", step_id="raw2")
    document.connect("raw2", "rec", add=True)
    assert document.step("rec").inputs == [Ref("raw2")]
    assert is_ordered(document.steps)


@pytest.mark.parametrize("source, target, message", [
    ("out", "blur", "save step"),
    ("rec", "raw", "no inputs"),
    ("blur", "blur", "loop"),
    ("blur", "split", "loop"),
    ("split.C0", "rec2", "reads from a source"),
])
def test_connections_that_cannot_run_are_refused(catalog, source, target, message):
    document = _chain(catalog)
    document.add_step("reconstruct", "view-only", step_id="rec2", inputs=["raw"])
    before = document.snapshot()
    with pytest.raises(DocumentError, match=message):
        document.connect(source, target)
    assert document.snapshot() == before


def test_connecting_into_an_earlier_step_moves_it_after_its_new_input(catalog):
    document = _chain(catalog)
    document.add_step("process", "filter", step_id="late", inputs=["split.C1"])
    document.connect("late", "blur")
    assert _ids(document) == ["raw", "rec", "split", "late", "blur", "out"]
    assert document.issues() == []


def test_a_reconstruction_moves_after_a_later_source():
    document = WorkflowDocument(Workflow("two", [
        Source("a"), Reconstruct("ra", "view-only", inputs=["a"]),
        Source("b"), Reconstruct("rb", "view-only", inputs=["b"]),
    ]))
    document.connect("b", "ra")
    assert _ids(document) == ["a", "b", "ra", "rb"]


def test_source_and_save_order_survive_reordering_on_the_consolidation_example(catalog):
    document = _example("consolidate_two_sources.yaml", catalog)
    document.add_step("save", step_id="out2", inputs=["rl"])
    document.add_step("process", "filter", step_id="late", inputs=["both"])
    document.connect("late", "out")
    assert _ids(document) == ["left", "right", "rl", "rr", "both", "late", "out", "out2"]
    assert _of_kind(document, Source) == ["left", "right"]
    assert _of_kind(document, Save) == ["out", "out2"]
    assert document.issues() == []


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda p: p.stem)
def test_every_connection_on_every_example_keeps_a_runnable_order(catalog, path):
    """Each producer into each step: refused, or an ordered file that loads
    and keeps its sources and saves in order."""
    original = _example(path.name, catalog)
    sources, saves = _of_kind(original, Source), _of_kind(original, Save)
    for producer in [step.id for step in original.steps if not isinstance(step, Save)]:
        for target in [step.id for step in original.steps if not isinstance(step, Source)]:
            for add in (False, True):
                document = _example(path.name, catalog)
                try:
                    document.connect(producer, target, add=add)
                except DocumentError:
                    assert target == producer or target in ancestors(document.steps, producer) \
                        or isinstance(document.step(target), Reconstruct)
                    continue
                assert is_ordered(document.steps), (producer, target)
                assert _of_kind(document, Source) == sources
                assert _of_kind(document, Save) == saves
                assert [step.id for step in _reloads(document).steps] == _ids(document)


# -- disconnect and splice ------------------------------------------------------------

def test_a_disconnected_input_is_an_issue_not_a_dangling_reference(catalog):
    document = _example("split_process_merge_diamond.yaml", catalog)
    document.disconnect("merge", 1)
    assert document.step("merge").inputs == [Ref("smooth")]
    document.disconnect("smooth")
    assert document.step("smooth").inputs == []
    assert document.issues_for("smooth")
    assert [step.id for step in _reloads(document).steps] == _ids(document)


@pytest.mark.parametrize("step_id, index, message", [
    ("out", 0, "delete the save"),
    ("raw", 0, "no inputs"),
    ("blur", 3, "no input 3"),
])
def test_disconnects_that_cannot_be_made_are_refused(catalog, step_id, index, message):
    document = _chain(catalog)
    with pytest.raises(DocumentError, match=message):
        document.disconnect(step_id, index)


def test_a_step_dropped_on_a_cable_is_spliced_in(catalog):
    document = _chain(catalog)
    document.add_step("process", "filter", step_id="pre")
    document.splice("pre", "blur")
    assert document.step("pre").inputs == [Ref("split", "C0")]
    assert document.step("blur").inputs == [Ref("pre")]
    assert _ids(document) == ["raw", "rec", "split", "pre", "blur", "out"]
    assert document.issues() == []
    document.add_step("process", "filter", step_id="last")
    document.splice("last", "out")
    assert document.step("out").input == Ref("last") and document.step("last").inputs == [Ref("blur")]
    assert document.issues() == []


@pytest.mark.parametrize("step_id, target, message", [
    ("out", "blur", "only a processing step"),
    ("rec", "split", "only a processing step"),
    ("split", "out", "loop"),
    ("blur", "blur", "loop"),
    ("split", "rec", "reads from a source"),
    ("blur", "raw", "no input 0"),
])
def test_splices_that_cannot_run_are_refused(catalog, step_id, target, message):
    document = _chain(catalog)
    before = document.snapshot()
    with pytest.raises(DocumentError, match=message):
        document.splice(step_id, target)
    assert document.snapshot() == before


# -- delete ---------------------------------------------------------------------------

def test_deleting_a_step_deletes_the_saves_that_read_from_it_in_one_undo(catalog):
    document = _chain(catalog)
    document.set_position("blur", 1, 2)
    document.set_position("out", 3, 4)
    before = document.snapshot()
    with document.history.edit("Delete blur"):
        removed = document.delete_step("blur")
    assert removed == ["blur", "out"]
    assert _ids(document) == ["raw", "rec", "split"]
    assert document.positions == {}
    assert document.history.undo() == "Delete blur"
    assert document.snapshot() == before


def test_deleting_a_step_empties_the_inputs_that_read_from_it(catalog):
    document = _example("split_process_merge_diamond.yaml", catalog)
    assert document.delete_step("smooth") == ["smooth"]
    assert document.step("merge").inputs == [Ref("sharp")]
    assert document.delete_step("split") == ["split"]
    assert document.step("sharp").inputs == []
    assert document.issues_for("sharp")
    assert [step.id for step in _reloads(document).steps] == ["raw", "rec", "sharp", "merge", "out"]


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda p: p.stem)
def test_deleting_any_step_of_an_example_leaves_a_file_that_loads(catalog, path):
    for step_id in _ids(_example(path.name, catalog)):
        document = _example(path.name, catalog)
        removed = set(document.delete_step(step_id))
        remaining = _ids(document)
        assert not removed & set(remaining)
        for step in document.steps:
            refs = [step.input] if isinstance(step, Save) else list(getattr(step, "inputs", []))
            assert all(ref.step in remaining for ref in refs), (step_id, step.id)
        assert [step.id for step in _reloads(document).steps] == remaining


# -- rename and positions -------------------------------------------------------------

def test_a_rename_moves_the_position_and_the_provenance_node(catalog):
    document = _chain(catalog)
    document.set_position("blur", 10, 20)
    document.workflow.metadata["provenance_nodes"] = {"blur": "node-7", "rec": "node-1"}
    document.rename_step("blur", "smooth")
    assert document.positions == {"smooth": (10.0, 20.0)}
    assert document.workflow.metadata["provenance_nodes"] == {"smooth": "node-7", "rec": "node-1"}
    assert document.step("out").input == Ref("smooth")


def test_positions_are_plain_floats_that_survive_the_file(catalog, tmp_path):
    document = _chain(catalog)
    document.set_position("blur", np.float64(12.5), np.float32(-3.0))
    document.set_position("raw", 0, 1)
    with pytest.raises(KeyError):
        document.set_position("nowhere", 0, 0)
    stored = document.workflow.metadata["editor"]["positions"]["blur"]
    assert all(type(value) is float for value in stored)
    written = document.save(tmp_path / "laid_out.yaml")
    assert "numpy" not in written.read_text(encoding="utf-8")
    reloaded = WorkflowDocument.load(written, catalog)
    assert reloaded.positions == {"blur": (12.5, -3.0), "raw": (0.0, 1.0)}


def test_a_hand_edited_layout_that_makes_no_sense_is_ignored(catalog):
    document = _chain(catalog)
    document.workflow.metadata["editor"] = 5
    assert document.positions == {}
    document.rename_step("blur", "smooth")
    document.delete_step("smooth")
    document.workflow.metadata["editor"] = {"positions": {"raw": "here", "rec": [1, "x"], "split": [1, 2]}}
    assert document.positions == {"split": (1.0, 2.0)}


def test_reading_editor_state_does_not_add_it_to_the_file(catalog, tmp_path):
    document = _example("split_process_merge_diamond.yaml", catalog)
    assert document.editor_metadata == {} and document.positions == {}
    assert "metadata" not in document.snapshot()
    written = document.save(tmp_path / "opened.yaml")
    assert "editor" not in written.read_text(encoding="utf-8")
    # Deleting the only laid-out step leaves no empty positions behind either.
    document.set_position("out", 1, 1)
    document.delete_step("out")
    assert "editor" not in document.to_yaml()


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda p: p.stem)
def test_every_example_round_trips_through_the_editor(catalog, path, tmp_path):
    """Load, look at it the way the editor does, save, reload: the same
    workflow, with or without a layout added under ``metadata.editor``."""
    original = Workflow.load(path).to_dict()
    document = WorkflowDocument.load(path, catalog)
    assert document.issues() == [], path.name
    document.editor_metadata, document.positions, document.port_options(None)
    untouched = document.save(tmp_path / "untouched.yaml")
    assert Workflow.load(untouched).to_dict() == original
    for rank, step_ids in enumerate(layers(document.steps)):
        for row, step_id in enumerate(step_ids):
            document.set_position(step_id, 240.0 * rank, 120.0 * row)
    laid_out = WorkflowDocument.load(document.save(tmp_path / "laid_out.yaml"), catalog)
    assert set(laid_out.positions) == set(_ids(document))
    assert _without_editor(laid_out.to_dict()) == original
    assert laid_out.issues() == []


# -- copy and paste -------------------------------------------------------------------

def test_a_pasted_copy_gets_free_ids_and_keeps_its_own_cables(catalog):
    document = _chain(catalog)
    document.set_position("split", 100, 50)
    document.set_position("blur", 300, 50)
    pasted = document.paste(document.fragment(["blur", "split"]))
    assert pasted == ["split2", "blur2"]
    assert document.step("split2").inputs == []
    assert document.step("blur2").inputs == [Ref("split2", "C0")]
    assert document.step("blur2").params == {"radius": 1.5}
    assert document.positions["split2"] == (130.0, 80.0) and document.positions["blur2"] == (330.0, 80.0)
    assert is_ordered(document.steps)


def test_a_save_is_pasted_only_with_its_input(catalog):
    document = _chain(catalog)
    assert document.paste(document.fragment(["out"])) == []
    assert document.paste(document.fragment(["blur", "out"])) == ["blur2", "out2"]
    assert document.step("out2").input == Ref("blur2")
    assert document.step("blur2").inputs == []


def test_a_fragment_pastes_into_another_document(catalog):
    fragment = _chain(catalog).fragment(["raw", "rec", "split"])
    other = WorkflowDocument(Workflow("empty"), catalog=catalog)
    assert other.paste(fragment) == ["raw", "rec", "split"]
    assert other.step("split").inputs == [Ref("rec")]
    assert other.issues() == []


@pytest.mark.parametrize("text, message", [
    ("just words", "not a workflow fragment"),
    ("steps: [{step: nonsense, id: x}]", "unknown step kind"),
    ("steps: [{step: process, id: a, processor: filter, inputs: [b]},"
     " {step: process, id: b, processor: filter, inputs: [a]}]", "loop"),
    ("steps: [{step: source, id: a}, {step: source, id: a}]", "share an id"),
    ("steps: [{step: source, id: a}]\npositions: {a: [1]}", "bad position"),
])
def test_a_bad_fragment_is_refused_and_changes_nothing(catalog, text, message):
    document = _chain(catalog)
    before = document.snapshot()
    with pytest.raises(DocumentError, match=message):
        document.paste(text)
    assert document.snapshot() == before


# -- undo and redo --------------------------------------------------------------------

def test_undo_and_redo_walk_back_and_forth_exactly(catalog):
    document = _chain(catalog)
    states = [document.snapshot()]
    edits = [
        ("Add", lambda: document.add_step("process", "filter", step_id="pre")),
        ("Splice", lambda: document.splice("pre", "blur")),
        ("Move", lambda: document.set_position("pre", 5.5, 6.5)),
        ("Rename", lambda: document.rename_step("pre", "smooth")),
        ("Parameter", lambda: document.set_param("smooth", "radius", 3.0)),
        ("Delete", lambda: document.delete_step("blur")),
        ("Paste", lambda: document.paste(document.fragment(["split", "smooth"]))),
    ]
    for label, edit in edits:
        with document.history.edit(label):
            edit()
        states.append(document.snapshot())
    assert document.history.undo_label == "Paste"
    for index in range(len(edits) - 1, -1, -1):
        assert document.history.undo() == edits[index][0]
        assert document.snapshot() == states[index]
    assert not document.history.can_undo and document.history.undo() is None
    for index, (label, _edit) in enumerate(edits, start=1):
        assert document.history.redo() == label
        assert document.snapshot() == states[index]
    assert not document.history.can_redo and document.history.redo() is None


def test_a_new_edit_after_undo_forgets_the_redo(catalog):
    document = _chain(catalog)
    with document.history.edit("First"):
        document.set_position("raw", 1, 1)
    document.history.undo()
    assert document.history.redo_label == "First"
    with document.history.edit("Second"):
        document.set_position("raw", 2, 2)
    assert not document.history.can_redo
    assert document.history.undo_label == "Second"


def test_an_edit_that_fails_records_nothing_and_changes_nothing(catalog):
    document = _chain(catalog)
    before, revision = document.snapshot(), document.revision
    with pytest.raises(DocumentError):
        with document.history.edit("Half"):
            document.set_position("raw", 1, 1)
            document.connect("blur", "split")
    assert document.snapshot() == before
    assert not document.dirty and document.revision > revision
    assert not document.history.can_undo


def test_looking_is_not_an_edit_and_nested_edits_undo_together(catalog):
    document = _chain(catalog)
    with document.history.edit("Look"):
        document.editor_metadata, document.positions, document.issues()
    assert not document.history.can_undo
    before = document.snapshot()
    with document.history.edit("Add and connect"):
        with document.history.edit("Add"):
            document.add_step("process", "filter", step_id="pre")
        with document.history.edit("Connect"):
            document.splice("pre", "blur")
    assert document.history.undo() == "Add and connect"
    assert document.snapshot() == before and not document.history.can_undo


def test_undo_marks_the_document_changed_and_the_history_is_bounded(catalog):
    document = _chain(catalog)
    document.history = type(document.history)(document, limit=3)
    for number in range(5):
        with document.history.edit(f"Move {number}"):
            document.set_position("raw", number, 0)
    document.dirty, revision = False, document.revision
    assert [document.history.undo() for _ in range(4)] == ["Move 4", "Move 3", "Move 2", None]
    assert document.positions["raw"] == (1.0, 0.0)
    assert document.dirty and document.revision == revision + 3
    document.history.clear()
    assert not document.history.can_undo and not document.history.can_redo


def test_undo_restores_a_consolidation_and_its_parameters(catalog):
    document = _example("consolidate_two_sources.yaml", catalog)
    before = document.snapshot()
    with document.history.edit("Delete"):
        document.delete_step("rr")
    assert isinstance(document.step("both"), Consolidate)
    assert document.step("both").inputs == [Ref("rl")]
    document.history.undo()
    assert document.snapshot() == before
    assert document.step("rr").params == {"device": "CPU"}
