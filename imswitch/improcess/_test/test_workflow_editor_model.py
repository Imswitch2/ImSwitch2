"""The workflow editor's Qt-free model: the plugin catalogue and the document.

The catalogue must describe exactly what the runner can run; the document
must keep every file it touches runnable, and say what is wrong otherwise.
"""

from pathlib import Path

import h5py
import numpy as np
import pytest

from imswitch.improcess.model.workfloweditor import (
    PROCESSOR,
    RECONSTRUCTOR,
    DocumentError,
    WorkflowDocument,
    build_catalog,
)
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry, run
from imswitch.improcess.workflows.steps import Consolidate, Ref, Save

_EXAMPLES = sorted((Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows").glob("*.yaml"))


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture(scope="module")
def catalog(registry):
    return build_catalog(registry)


def _chain(catalog):
    """source -> view-only -> stack-split -> filter(C0) -> save, as a document."""
    workflow = Workflow("chain", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("blur", "filter", {"radius": 1.5}, inputs=["split.C0"]),
        Save("out", input="blur", fmt="tiff"),
    ])
    return WorkflowDocument(workflow, catalog=catalog)


# -- catalogue ----------------------------------------------------------------

def test_the_catalogue_describes_every_registered_plugin(catalog, registry):
    assert {entry.id for entry in catalog.processors} == {p.id for p in registry.processors()}
    assert {entry.id for entry in catalog.reconstructors} == {r.id for r in registry.reconstructors()}
    for entry in catalog.processors + catalog.reconstructors:
        plugin = entry.plugin
        assert {f.key for f in entry.fields} == set(type(plugin).default_params())
        assert entry.defaults() == type(plugin).default_params()
        assert entry.param_keys() == type(plugin).param_keys()
        assert entry.origin == "builtin"
        assert entry.gui_only is None, entry.id
        assert entry.version
    merge = catalog.processor("channel-merge")
    assert merge.min_inputs == 2 and merge.max_inputs is None and merge.arity == "2..∞"
    assert catalog.processor("filter").arity == "1"
    assert catalog.processor("filter").category == "Filters"
    assert catalog.get(PROCESSOR, "no-such-thing") is None
    assert catalog.get(RECONSTRUCTOR, "filter") is None
    assert "monalisa" in {e.id for e in catalog.consolidators()}
    assert catalog.categories()[-1] != "" and "Filters" in catalog.categories()
    assert catalog.processors_in("Filters")
    assert {"tiff", "hdf5", "zarr", "csv"} <= set(catalog.save_formats)


def test_the_catalogue_reports_a_processors_ports_for_given_params(catalog):
    background = catalog.processor("subtract-background")
    assert background.output_spec().ports == ("out",)
    assert background.output_spec({"output_background": True}).ports == ("signal", "background")
    assert catalog.processor("stack-split").output_spec().pattern
    assert catalog.reconstructor("view-only").output_spec() is None


def test_the_path_placeholders_are_the_ones_the_runner_fills(catalog, tmp_path):
    from imswitch.improcess.workflows.runner import render_save_path

    template = "{out_dir}/" + "_".join("{%s}" % name for name in catalog.path_placeholders if name != "out_dir")
    step = Save("s", input="x", fmt="tiff", path_template=template)
    rendered = render_save_path(step, out_dir=tmp_path, source_stem="scan", result=None, input_step="x")
    assert str(rendered).startswith(str(tmp_path.resolve()))


# -- files --------------------------------------------------------------------

@pytest.mark.parametrize("path", _EXAMPLES, ids=[p.stem for p in _EXAMPLES])
def test_the_shipped_examples_load_validate_and_round_trip(path, catalog, tmp_path):
    document = WorkflowDocument.load(path, catalog)
    assert document.to_dict() == Workflow.load(path).to_dict()
    assert document.issues() == []
    assert document.dirty is False and document.path == path
    written = document.save(tmp_path / path.name)
    assert Workflow.load(written).to_dict() == document.to_dict()
    assert document.path == written and document.dirty is False


def test_saving_needs_a_name_and_marks_clean(catalog, tmp_path):
    document = _chain(catalog)
    with pytest.raises(DocumentError):
        document.save()
    document.set_name("renamed")
    assert document.dirty
    written = document.save(tmp_path / "chain.json")
    assert written.suffix == ".json" and not document.dirty
    assert Workflow.load(written).name == "renamed"


# -- adding, ordering, renaming, removing ---------------------------------------

def test_ids_are_allocated_per_kind(catalog):
    document = _chain(catalog)
    assert document.new_id("process") == "proc1"
    document.add_step("process", "filter", after="blur")
    assert document.new_id("process") == "proc2"
    assert document.new_id("source") == "src1" and document.new_id("save") == "save1"


def test_a_new_step_follows_the_selection_and_takes_it_as_input(catalog):
    document = _chain(catalog)
    step = document.add_step("process", "filter", after="rec")
    assert [s.id for s in document.steps] == ["raw", "rec", "proc1", "split", "blur", "out"]
    assert step.inputs == [Ref("rec")] and step.params == {}
    assert document.dirty and document.revision == 1
    save = document.add_step("save", after="proc1")
    assert save.input == Ref("proc1")
    assert document.issues() == []


def test_a_new_step_is_never_placed_before_its_inputs(catalog):
    document = _chain(catalog)
    step = document.add_step("process", "channel-merge", after="raw", inputs=["split.C0", "blur"])
    assert document.index_of(step.id) > document.index_of("blur")
    assert document.issues() == []


def test_adding_needs_a_plugin_and_a_known_kind(catalog):
    document = _chain(catalog)
    with pytest.raises(DocumentError):
        document.add_step("process")
    with pytest.raises(DocumentError):
        document.add_step("merge", "x")
    with pytest.raises(DocumentError):
        document.add_step("save")               # nothing to save
    with pytest.raises(DocumentError):
        document.add_step("process", "filter", step_id="blur")   # taken
    with pytest.raises(DocumentError):
        document.add_step("process", "filter", step_id="bad id")


def test_renaming_rewrites_every_reference(catalog):
    document = _chain(catalog)
    document.rename_step("split", "channels")
    assert document.step("blur").inputs == [Ref("channels", "C0")]
    document.rename_step("blur", "smooth")
    assert document.step("out").input == Ref("smooth")
    assert document.issues() == []
    with pytest.raises(DocumentError):
        document.rename_step("rec", "raw")


def test_moving_keeps_inputs_before_their_consumers(catalog):
    document = _chain(catalog)
    with pytest.raises(DocumentError):
        document.move_step("blur", 1)           # before split, its input
    with pytest.raises(DocumentError):
        document.move_step("rec", 4)            # after split, which consumes it
    document.move_step("out", 4)                # a no-op position is fine
    document.add_step("process", "filter", after="rec")   # proc1 after rec
    document.move_step("proc1", 4)              # later is fine: it only reads rec
    assert [s.id for s in document.steps] == ["raw", "rec", "split", "blur", "proc1", "out"]
    assert document.issues() == []


def test_removing_leaves_a_reported_dangling_reference(catalog):
    document = _chain(catalog)
    removed = document.remove_step("split")
    assert removed.id == "split"
    messages = [str(issue) for issue in document.issues()]
    assert any(issue.startswith("blur:") and "split" in issue for issue in messages)


# -- parameters -----------------------------------------------------------------

def test_params_hold_only_what_differs_from_the_defaults(catalog):
    document = _chain(catalog)
    document.set_param("blur", "method", "median")
    assert document.step("blur").params == {"radius": 1.5, "method": "median"}
    document.set_param("blur", "radius", 2.0)          # the default
    assert document.step("blur").params == {"method": "median"}
    assert document.effective_params("blur") == {"method": "median", "radius": 2.0, "amount": 0.6}
    document.set_params("blur", {"method": "gaussian", "radius": 3.0, "amount": 0.6})
    assert document.step("blur").params == {"radius": 3.0}
    document.clear_param("blur", "radius")
    assert document.step("blur").params == {}
    with pytest.raises(DocumentError):
        document.set_param("raw", "x", 1)


def test_changing_the_plugin_drops_parameters_it_does_not_take(catalog):
    document = _chain(catalog)
    document.set_param("blur", "amount", 0.9)
    document.set_plugin("blur", "projection")
    assert document.step("blur").processor == "projection"
    assert document.step("blur").params == {}
    document.set_plugin("rec", "time-lapse")
    assert document.step("rec").reconstructor == "time-lapse"
    with pytest.raises(DocumentError):
        document.set_plugin("raw", "filter")


def test_a_value_that_is_not_one_of_its_field_is_an_issue(catalog):
    document = _chain(catalog)
    document.set_param("blur", "radius", "wide")
    assert [str(i) for i in document.issues_for("blur")] == ["blur: radius: 'wide' must be a number"]
    document.set_param("blur", "radius", 4)
    assert document.issues() == []


# -- ports ------------------------------------------------------------------------

def test_port_options_come_from_the_upstream_declarations(catalog):
    document = _chain(catalog)
    options = {option.ref: option for option in document.port_options("out")}
    assert options["raw"].port is None and options["rec"].port is None
    assert options["blur"].port is None
    assert "split" in options and options["split"].pattern
    assert options["split"].port == catalog.processor("stack-split").output_spec().pattern
    document.add_step("process", "subtract-background", after="rec", step_id="bg")
    document.set_param("bg", "output_background", True)
    refs = [option.ref for option in document.port_options(None)]
    assert "bg.signal" in refs and "bg.background" in refs
    document.add_step("process", "filter", after="bg", step_id="fan", inputs=["rec", "bg.signal"])
    refs = [option.ref for option in document.port_options(None)]
    assert "fan" in refs and "fan.out1" in refs


# -- sources and saves -----------------------------------------------------------

def test_sources_and_saves_are_edited_in_place(catalog, tmp_path):
    document = _chain(catalog)
    document.set_source("raw", path=str(tmp_path / "scan.h5"), dataset="camera2")
    spec = document.step("raw").source
    assert spec.path == str(tmp_path / "scan.h5") and spec.dataset == "camera2" and spec.fingerprint == {}
    document.set_source("raw", source_kind="image")
    assert document.step("raw").source.source_kind == "image"
    with pytest.raises(DocumentError):
        document.set_source("raw", source_kind="stream")
    with pytest.raises(DocumentError):
        document.set_source("rec", path="x")
    document.set_save("out", fmt="hdf5", path_template="{out_dir}/{source_stem}_{when}{ext}")
    assert [str(i) for i in document.issues()] == ["out: unknown placeholder {when} in path template"]
    document.set_save("out", path_template="")
    assert document.step("out").path_template and document.issues() == []
    document.set_save("out", fmt="bmp")
    assert any("bmp" in str(issue) for issue in document.issues())


def test_two_saves_on_one_file_are_reported(catalog):
    document = _chain(catalog)
    document.set_save("out", path_template="{out_dir}/{source_stem}_final{ext}")
    document.add_step("save", after="out", step_id="also", inputs=["blur"])
    document.set_save("also", path_template="{out_dir}/{source_stem}_final{ext}")
    assert [str(i) for i in document.issues()] == ["also: writes the same file as save 'out'"]
    document.set_save("also", path_template="{out_dir}/{source_stem}_{step}{ext}")
    assert document.issues() == []


def test_a_restriction_is_set_and_cleared(catalog):
    document = _chain(catalog)
    document.set_restriction("blur", {"mode": "crop"})
    assert document.step("blur").restriction == {"mode": "crop"}
    document.set_restriction("blur", None)
    assert document.step("blur").restriction is None
    with pytest.raises(DocumentError):
        document.set_restriction("rec", {})


def test_editor_state_lives_in_metadata_and_round_trips(catalog, tmp_path):
    document = _chain(catalog)
    document.editor_metadata["collapsed"] = ["blur"]
    written = document.save(tmp_path / "wf.yaml")
    reloaded = WorkflowDocument.load(written, catalog)
    assert reloaded.editor_metadata == {"collapsed": ["blur"]}
    assert reloaded.issues() == []


# -- from a result -----------------------------------------------------------------

def test_a_result_becomes_a_minimized_document(catalog, registry, tmp_path):
    path = tmp_path / "scan.h5"
    with h5py.File(str(path), "w") as handle:
        handle.create_dataset("data", data=np.random.default_rng(0).random((2, 8, 8)).astype(np.float32))
    workflow = Workflow("made", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.5}, inputs=["rec"]),
    ])
    with run(workflow, registry=registry, bindings={"raw": str(path)}, out_dir=tmp_path) as report:
        result = report.result("blur")
        document = WorkflowDocument.from_result(result, catalog, name="again")
    kinds = [type(step).__name__ for step in document.steps]
    assert kinds == ["Source", "Reconstruct", "Process", "Save"]
    process = [step for step in document.steps if isinstance(step, Process)][0]
    assert process.processor == "filter" and process.params == {"radius": 1.5}
    assert document.name == "again" and document.dirty and document.issues() == []
    assert document.steps[0].source.path == str(path)
