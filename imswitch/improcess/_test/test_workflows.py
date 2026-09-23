"""Workflows run the GUI's own paths without the GUI, and say what they did."""

import json
import os

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.model.provenance_io import read_provenance  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Consolidate,
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    SourceSpec,
    Workflow,
    WorkflowError,
    bindings_for_inputs,
    bindings_from_manifest,
    bootstrap_registry,
    run,
    run_over,
    validate,
)


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _h5(path, shape=(3, 8, 8), name="data", *, extra=None):
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset(name, data=np.random.default_rng(0).random(shape).astype(np.float32))
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        # The axes are declared, not left to be guessed from the rank: a
        # file that says nothing reads as Frame/Y/X, never as channels.
        if len(shape) == 3:
            dataset.attrs["axes"] = "CYX"
        for key, value in (extra or {}).items():
            other = handle.create_dataset(key, data=np.zeros(shape, np.float32))
            other.attrs["element_size_um"] = [1.0, 0.1, 0.1]
            if len(shape) == 3:
                other.attrs["axes"] = "CYX"
    return path


def _chain(fmt="tiff"):
    return Workflow("chain", [
        Source("raw", path="scan.h5"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("proj", "projection", {"axis": "C", "mode": "max"}, inputs=["rec"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["proj"]),
        Save("out", input="blur", fmt=fmt),
    ])


# -- structure and serialisation -------------------------------------------------

def test_a_workflow_round_trips_through_yaml_and_json(tmp_path):
    wf = _chain()
    for suffix in (".yaml", ".json"):
        path = wf.save(tmp_path / f"wf{suffix}")
        again = Workflow.load(path)
        assert again.to_dict() == wf.to_dict()
    assert json.loads(wf.to_json())["steps"][2]["params"] == {"axis": "C", "mode": "max"}


def test_duplicate_or_bad_ids_and_bad_refs_are_refused():
    with pytest.raises(WorkflowError, match="duplicate"):
        Workflow("x", [Source("a"), Source("a")])
    with pytest.raises(WorkflowError, match="bad step id"):
        Source("has space")
    with pytest.raises(WorkflowError, match="bad step reference"):
        Process("p", "filter", inputs=["a.b.c"])


def test_static_validation_catches_what_it_can(registry):
    wf = Workflow("bad", [
        Source("raw"),
        Reconstruct("rec", "no-such-recon", inputs=["raw"]),
        Process("p1", "projection", {"axis": "Z", "bogus": 1}, inputs=["rec.nope"]),
        Process("p2", "channel-merge", inputs=["p1"]),              # needs >= 2 inputs
        Process("p3", "subtract-background", inputs=["later"]),     # forward reference
        Save("s", input="p2", fmt="xyz"),
    ])
    messages = [str(i) for i in validate(wf, registry)]
    assert any("unknown reconstructor" in m for m in messages)
    assert any("port 'nope'" in m for m in messages)
    assert any("unknown parameter" in m and "bogus" in m for m in messages)
    assert any("takes 2" in m for m in messages)
    assert any("not an earlier step" in m for m in messages)
    assert any("unknown save format" in m for m in messages)


def test_dynamic_ports_validate_against_the_declared_pattern(registry):
    wf = Workflow("split", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("a", "filter", inputs=["split.C0"]),
        Process("b", "filter", inputs=["split.C1"]),
        Process("bad", "filter", inputs=["split.out"]),
    ])
    messages = [str(i) for i in validate(wf, registry)]
    assert messages == ["bad: port 'out' is not one 'split' produces (/[A-Za-z]+\\d+/)"]


def test_background_ports_follow_its_params(registry):
    wf = Workflow("bg", [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("bg", "subtract-background", {"radius": 5, "output_background": True}, inputs=["rec"]),
        Process("use", "filter", inputs=["bg.background"]),
    ])
    assert validate(wf, registry) == []
    wf.step("bg").params["output_background"] = False
    assert any("port 'background'" in str(i) for i in validate(wf, registry))


# -- running -----------------------------------------------------------------------

def test_a_chain_runs_saves_and_records_the_same_provenance_as_the_gui(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    out = tmp_path / "out"
    with run(_chain(), registry=registry, bindings={"raw": str(raw)}, out_dir=out) as report:
        assert report.ok and report.steps_run == ["raw", "rec", "proj", "blur", "out"]
        blurred = report.result("blur")
        assert blurred.data.shape == (8, 8)
        node = output_node(blurred)
        assert node["plugin_id"] == "filter" and node["params"]["radius"] == 1.0
        assert node["params"]["method"] == "gaussian"           # defaults filled in
        ops = [n["op"] for n in graph_of(blurred)["nodes"].values()]
        assert sorted(ops) == ["process", "process", "reconstruct", "source"]
        receipt = report.receipts[0]
        assert receipt.primary == out / "scan_out.ome.tif"
        assert receipt.primary.exists()
        document = read_provenance(receipt.primary)
        assert document.graph["nodes"] == graph_of(blurred)["nodes"]
        assert document.artifact["primary"] == "scan_out.ome.tif"
    assert report._sources == []                                # closed


def test_a_diamond_with_dynamic_ports_runs(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5", shape=(2, 8, 8))
    wf = Workflow("diamond", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("a", "filter", {"radius": 1.0}, inputs=["split.C0"]),
        Process("b", "filter", {"radius": 2.0}, inputs=["split.C1"]),
        Process("merge", "channel-merge", inputs=["a", "b"]),
        Save("out", input="merge", fmt="hdf5"),
    ])
    with run(wf, registry=registry, out_dir=tmp_path / "out") as report:
        assert report.ports_of("split") == ["C0", "C1"]
        merged = report.result("merge")
        node = output_node(merged)
        assert [ref["port"] for ref in node["inputs"]] == ["out", "out"]
        gains = sorted(n["params"]["radius"] for n in graph_of(merged)["nodes"].values()
                       if n.get("plugin_id") == "filter")
        assert gains == [1.0, 2.0]


def test_a_single_input_processor_over_several_inputs_runs_once_per_input(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5", shape=(2, 8, 8))
    wf = Workflow("fanout", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("blur", "filter", inputs=["split.C0", "split.C1"]),
    ])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        assert sorted(report.ports_of("blur")) == ["out", "out1"]


def test_consolidation_needs_reconstructions_of_one_reconstructor(registry, tmp_path):
    a = _h5(tmp_path / "a.h5")
    b = _h5(tmp_path / "b.h5")
    wf = Workflow("consolidate", [
        Source("a", path=str(a)), Source("b", path=str(b)),
        Reconstruct("ra", "view-only", inputs=["a"]),
        Reconstruct("rb", "view-only", inputs=["b"]),
        Consolidate("all", "view-only", inputs=["ra", "rb"]),
    ])
    messages = [str(i) for i in validate(wf, registry)]
    assert any("does not support consolidation" in m for m in messages)


def test_a_failing_step_raises_with_the_report_attached(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("boom", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("crop", "stack-subset", {"ranges": [{"axis": 5, "start": 0, "stop": 1}]}, inputs=["rec"]),
    ])
    with pytest.raises(RunError) as excinfo:
        run(wf, registry=registry, out_dir=tmp_path)
    report = excinfo.value.report
    assert report.failed_step == "crop" and report.steps_run == ["raw", "rec"]
    report.close()


def test_saves_do_not_clobber_unless_asked(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = _chain()
    run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path / "out").close()
    with pytest.raises(RunError, match="refusing to overwrite"):
        run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path / "out").close()
    run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path / "out", overwrite=True).close()


def test_path_templates_and_placeholders(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    wf = _chain()
    wf.step("out").path_template = "{out_dir}/{name}/{input_step}_{fmt}{ext}"
    with run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path / "o") as report:
        primary = report.receipts[0].primary
        assert primary.parent.parent == tmp_path / "o"
        assert primary.name == "blur_tiff.ome.tif"
    wf.step("out").path_template = "{nope}"
    with pytest.raises(RunError, match="unknown placeholder"):
        run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path / "o2")


# -- sources -----------------------------------------------------------------------

def test_a_multi_dataset_container_needs_the_dataset_named(registry, tmp_path):
    raw = _h5(tmp_path / "multi.h5", extra={"other": 1})
    wf = Workflow("m", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])])
    with pytest.raises(RunError, match="name one with"):
        run(wf, registry=registry, bindings={"raw": str(raw)}, out_dir=tmp_path)
    with run(wf, registry=registry, bindings={"raw": f"{raw}::other"}, out_dir=tmp_path) as report:
        assert report.result("raw").datasetName == "other"


def test_relative_source_paths_resolve_against_the_source_root(registry, tmp_path):
    _h5(tmp_path / "data" / "scan.h5") if (tmp_path / "data").mkdir() or True else None
    wf = Workflow("rel", [Source("raw", path="scan.h5"), Reconstruct("rec", "view-only", inputs=["raw"])])
    with pytest.raises(RunError, match="does not exist"):
        run(wf, registry=registry, out_dir=tmp_path)
    run(wf, registry=registry, out_dir=tmp_path, source_root=tmp_path / "data").close()


def test_an_unbound_source_is_an_error(registry, tmp_path):
    wf = Workflow("u", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"])])
    with pytest.raises(RunError, match="not bound"):
        run(wf, registry=registry, out_dir=tmp_path)


def test_replay_mode_refuses_source_drift_unless_allowed(registry, tmp_path):
    raw = _h5(tmp_path / "scan.h5")
    from imswitch.improcess.workflows.sources import fingerprint_of, open_source

    recorded = fingerprint_of(open_source(SourceSpec(path=str(raw))))
    wf = Workflow("r", [
        Source("raw", SourceSpec(path=str(raw), fingerprint=recorded)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
    ])
    run(wf, registry=registry, out_dir=tmp_path, mode="replay").close()
    _h5(raw, shape=(4, 8, 8))                                    # re-recorded
    with pytest.raises(RunError, match="differs from the recorded run"):
        run(wf, registry=registry, out_dir=tmp_path, mode="replay")
    with run(wf, registry=registry, out_dir=tmp_path, mode="replay", allow_drift=True) as report:
        assert report.warnings and "shape" in report.warnings[0]
    run(wf, registry=registry, out_dir=tmp_path, mode="run").close()   # a run never checks


# -- batch ---------------------------------------------------------------------------

def test_batch_isolates_failures_and_writes_a_summary(registry, tmp_path):
    good = _h5(tmp_path / "good.h5")
    bad = tmp_path / "bad.h5"
    bad.write_bytes(b"not an hdf5 file")
    wf = _chain()
    batch = run_over(wf, bindings_for_inputs(wf, [good, bad]), registry=registry, out_dir=tmp_path / "out")
    assert [row.ok for row in batch.rows] == [True, False]
    assert batch.rows[1].failed_step == "raw" and batch.rows[1].error
    assert batch.rows[0].files and (tmp_path / "out" / "good_out.ome.tif").exists()
    summary = batch.write_summary(tmp_path / "out" / "summary.csv")
    text = summary.read_text()
    assert "source:raw" in text.splitlines()[0] and "bad.h5" in text


def test_a_manifest_binds_several_sources_per_row(registry, tmp_path):
    a = _h5(tmp_path / "a.h5")
    b = _h5(tmp_path / "b.h5", extra={"second": 1})
    manifest = tmp_path / "inputs.csv"
    manifest.write_text(f"left,right,right.dataset\n{a},{b},second\n")
    wf = Workflow("two", [
        Source("left"), Source("right"),
        Reconstruct("rl", "view-only", inputs=["left"]),
        Reconstruct("rr", "view-only", inputs=["right"]),
        Process("pl", "projection", {"axis": "C", "mode": "max"}, inputs=["rl"]),
        Process("pr", "projection", {"axis": "C", "mode": "max"}, inputs=["rr"]),
        Process("merge", "channel-merge", inputs=["pl", "pr"]),
    ])
    rows = bindings_from_manifest(manifest, ["left", "right"])
    assert rows[0]["right"].dataset == "second"
    batch, reports = run_over(wf, rows, registry=registry, out_dir=tmp_path, keep_reports=True)
    assert batch.ok
    assert reports[0].result("merge").data.shape[0] == 2
    reports[0].close()
    with pytest.raises(WorkflowError, match="lacks column"):
        bindings_from_manifest(manifest, ["left", "middle"])


def test_bindings_for_inputs_needs_one_source_or_a_name():
    wf = Workflow("two", [Source("a"), Source("b")])
    with pytest.raises(WorkflowError, match="name the one"):
        bindings_for_inputs(wf, ["x.h5"])
    assert bindings_for_inputs(wf, ["x.h5"], source_id="b")[0]["b"].path == "x.h5"


# -- registry bootstrap ---------------------------------------------------------------

def test_bootstrap_registry_is_fresh_versioned_and_rejects_duplicates():
    from imswitch.improcess.workflows.runtime import BootstrapError

    first = bootstrap_registry(user_plugins=False)
    second = bootstrap_registry(user_plugins=False, processor_ids=["filter"])
    assert first is not second
    assert [p.id for p in second.processors()] == ["filter"]
    assert all(p.version for p in first.processors())
    with pytest.raises(BootstrapError, match="twice"):
        bootstrap_registry(user_plugins=False, processor_ids=["filter", "filter"])
    with pytest.raises(BootstrapError, match="unknown"):
        bootstrap_registry(user_plugins=False, reconstructor_ids=["nope"])


def test_monalisa_fills_scan_params_from_the_file(registry, tmp_path):
    """prepare_params does headlessly what the GUI controller does on load."""
    from imswitch.improcess.reconstructors.monalisa.scan_params import DEFAULT_LABELS

    raw = tmp_path / "scan.h5"
    with h5py.File(str(raw), "w") as handle:
        dataset = handle.create_dataset("data", data=np.zeros((8, 6, 6), np.float32))
        dataset.attrs["ScanTTL:Nx"] = 4
        dataset.attrs["ScanTTL:Ny"] = 2
        dataset.attrs["ScanStage:target_device"] = [b"X", b"Y", b"Z"]
    from imswitch.improcess.workflows.sources import open_source

    plugin = registry.get_reconstructor("monalisa")
    params = plugin.prepare_params(open_source(SourceSpec(path=str(raw))), {})
    scan = params["scan_params"]
    assert scan["dimensions"][:2] == [DEFAULT_LABELS.r_l, DEFAULT_LABELS.u_d]
    assert scan["steps"][:2] == ["4", "2"]


# -- CLI ----------------------------------------------------------------------------------

def test_cli_validate_run_and_list(registry, tmp_path, capsys):
    from imswitch.improcess.workflows.__main__ import main

    raw = _h5(tmp_path / "scan.h5")
    wf_path = _chain().save(tmp_path / "chain.yaml")
    assert main(["--no-user-plugins", "validate", str(wf_path)]) == 0
    assert main(["--no-user-plugins", "run", str(wf_path), "--input", str(raw), "--out", str(tmp_path / "cli")]) == 0
    assert (tmp_path / "cli" / "scan_out.ome.tif").exists()
    assert (tmp_path / "cli" / "chain_summary.csv").exists()
    assert main(["--no-user-plugins", "list"]) == 0
    out = capsys.readouterr().out
    assert "projection" in out and "subtract-background" in out
