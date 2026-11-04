"""Runner extensions for interactive runs (workflow editor 2.0, Phase 0).

What the editor's run session will need from ``run()``, each backwards
compatible: bindings for steps with several ports, a per-step callback,
runs that do not write files, an explicit file-name stem, and failures as data.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.processors.run import Failure, run_batch
from imswitch.improcess.processors.python_step.context import ScriptError, run_script
from imswitch.improcess.workflows import Workflow, bootstrap_registry, run
from imswitch.improcess.workflows.runner import RunError, failure_details

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture
def cells(tmp_path):
    spec = importlib.util.spec_from_file_location("synthetic", EXAMPLES / "_synthetic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX")


def _diamond():
    return Workflow.load(EXAMPLES / "split_process_merge_diamond.yaml")


def _full(registry, cells, out):
    return run(_diamond(), registry=registry, bindings={"raw": str(cells)}, out_dir=out)


# -- 1. a step with several ports binds from a mapping ------------------------------------

def test_a_multi_port_step_binds_from_a_mapping_and_its_feeders_are_skipped(registry, cells, tmp_path):
    with _full(registry, cells, tmp_path / "full") as full:
        ports = {"C0": full.result("split.C0"), "C1": full.result("split.C1")}
        executed = []
        with run(_diamond(), registry=registry, bindings={"raw": str(cells), "split": ports},
                 out_dir=tmp_path / "again", source_stem="cells",
                 on_step_done=lambda sid, produced, seconds, cached: executed.append((sid, cached))) as again:
            assert again.bound == ["split.C0", "split.C1"]
            assert executed == [("split", True), ("smooth", False), ("sharp", False), ("merge", False), ("out", False)]
            assert not again._sources                         # the recording was never opened
            assert np.array_equal(again.result("merge").data, full.result("merge").data)


def test_a_reference_without_a_port_takes_the_first_bound_port(registry, cells, tmp_path):
    with _full(registry, cells, tmp_path / "full") as full:
        flipped = {"C1": full.result("split.C1"), "C0": full.result("split.C0")}
        workflow = _diamond()
        workflow.step("smooth").inputs = [workflow.step("smooth").inputs[0].__class__("split", None)]
        with run(workflow, registry=registry, bindings={"split": flipped}, out_dir=tmp_path / "x") as report:
            # smooth read split's first port, which this mapping says is C1
            assert report.result("smooth").name.startswith(full.result("split.C1").name)


def test_a_plain_result_still_binds_the_out_port(registry, cells, tmp_path):
    with _full(registry, cells, tmp_path / "full") as full:
        with run(_diamond(), registry=registry, bindings={"rec": full.result("rec")}, out_dir=tmp_path / "x") as report:
            assert report.bound == ["rec.out"]


# -- 2. the per-step callback ------------------------------------------------------------

def test_every_step_is_reported_with_its_ports_and_time(registry, cells, tmp_path):
    seen = []
    with run(_diamond(), registry=registry, bindings={"raw": str(cells)}, out_dir=tmp_path,
             on_step_done=lambda sid, produced, seconds, cached: seen.append((sid, sorted(produced), seconds, cached))):
        pass
    assert [s[0] for s in seen] == ["raw", "rec", "split", "smooth", "sharp", "merge", "out"]
    ports = {sid: names for sid, names, _seconds, _cached in seen}
    assert ports["raw"] == ["data"] and ports["split"] == ["C0", "C1"] and ports["merge"] == ["out"]
    assert ports["out"] == []                                # a save produces no port
    assert all(seconds >= 0 and not cached for _sid, _names, seconds, cached in seen)


def test_a_failing_callback_is_logged_and_does_not_fail_the_step(registry, cells, tmp_path):
    def boom(*_args):
        raise RuntimeError("view bug")

    with run(_diamond(), registry=registry, bindings={"raw": str(cells)}, out_dir=tmp_path, on_step_done=boom) as report:
        assert report.ok and report.steps_run[-1] == "out"


# -- 3. runs that write nothing --------------------------------------------------------------

def test_with_write_saves_off_nothing_is_written_and_the_path_is_known(registry, cells, tmp_path):
    seen = []
    with run(_diamond(), registry=registry, bindings={"raw": str(cells)}, out_dir=tmp_path / "out",
             write_saves=False, on_step_done=lambda sid, *_rest: seen.append(sid)) as report:
        assert report.receipts == [] and "out" not in report.steps_run and "out" not in seen
        assert report.would_write == {"out": (tmp_path / "out" / "cells_diamond.h5").resolve()}
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


# -- 4. the file-name stem -------------------------------------------------------------------

def test_a_cached_reconstruction_keeps_the_file_name_when_the_caller_passes_the_stem(registry, cells, tmp_path):
    with _full(registry, cells, tmp_path / "full") as full:
        assert full.receipts[0].primary.name == "cells_diamond.h5"
        rec = full.result("rec")
        with run(_diamond(), registry=registry, bindings={"rec": rec}, out_dir=tmp_path / "default") as default:
            # today's rule, unchanged: no source opened, so the bound result's name
            assert default.receipts[0].primary.name == "cells-h5_diamond.h5"
        with run(_diamond(), registry=registry, bindings={"rec": rec}, out_dir=tmp_path / "named",
                 source_stem="cells") as named:
            assert named.receipts[0].primary.name == "cells_diamond.h5"


def test_an_explicit_stem_wins_over_the_source_the_run_opens(registry, cells, tmp_path):
    with run(_diamond(), registry=registry, bindings={"raw": str(cells)}, out_dir=tmp_path,
             source_stem="sample-7") as report:
        assert report.receipts[0].primary.name == "sample-7_diamond.h5"


def test_run_on_selected_results_still_names_each_run_after_its_own_result(registry, cells, tmp_path):
    """A path-bound Source (as an exported workflow has) must not name every run after itself."""
    with _full(registry, cells, tmp_path / "full") as full:
        workflow = _diamond()
        workflow.step("raw").source = workflow.step("raw").source.with_path(str(cells))
        names = []
        for label in ("alpha.h5", "beta.h5"):
            rec = full.result("rec")
            rec.name = label
            with run(workflow, registry=registry, bindings={"rec": rec}, out_dir=tmp_path / "sel") as report:
                names.append(report.receipts[0].primary.name)
    assert names == ["alpha-h5_diamond.h5", "beta-h5_diamond.h5"]


# -- 5. failures as data ---------------------------------------------------------------------

def _failing_python(code):
    return Workflow.from_dict({
        "name": "fails", "schema": 1,
        "steps": [
            {"step": "source", "id": "raw"},
            {"step": "reconstruct", "id": "rec", "reconstructor": "view-only", "inputs": ["raw"]},
            {"step": "process", "id": "script", "processor": "python", "inputs": ["rec"],
             "params": {"code": code, "ports": "out"}},
        ],
    })


def test_a_failing_python_step_reports_its_line_traceback_and_what_it_printed(registry, cells, tmp_path):
    code = "print('checking', data.shape)\nx = 1\nraise ValueError('two channels expected')\n"
    with pytest.raises(RunError) as caught:
        run(_failing_python(code), registry=registry, bindings={"raw": str(cells)}, out_dir=tmp_path)
    failure = caught.value.report.failure
    assert failure["step"] == "script" and failure["type"] == "ScriptError" and failure["line"] == 3
    assert "ValueError: two channels expected" in failure["message"]
    assert "raise ValueError" in failure["traceback"]
    assert failure["stdout"] == "checking (2, 96, 96)\n"
    caught.value.report.close()


def test_an_output_rule_failure_keeps_what_was_printed_too():
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    rec = ArrayProcessingResult("rec", np.zeros((2, 4, 4), np.float32), ["C", "Y", "X"])
    with pytest.raises(ScriptError) as caught:
        run_script("print('hello')\nout = 'not an array'", [rec], ("out",))
    assert caught.value.stdout == "hello\n" and caught.value.line is None


def test_a_cancellation_and_a_plain_error_are_described_too():
    from imswitch.imcommon.model import OperationCancelled

    assert failure_details("s", OperationCancelled("x")) == {"step": "s", "type": "OperationCancelled", "message": "cancelled"}
    plain = failure_details("s", KeyError("missing"))
    assert plain["type"] == "KeyError" and "line" not in plain and "stdout" not in plain


def test_a_batch_failure_is_still_text_but_knows_its_exception():
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    class Broken:
        id = "broken"
        accepts_roi = False

        def apply(self, _result, _params):
            raise ZeroDivisionError("nope")

    item = ArrayProcessingResult("a", np.zeros((2, 2)), ["Y", "X"])
    logger = type("L", (), {"exception": staticmethod(lambda *a, **k: None)})()
    results, failures = run_batch(Broken(), [item], {}, logger)
    (who, why), = failures
    assert results == [] and who is item
    assert why == "nope" and isinstance(why, str) and f"failed: {why}" == "failed: nope"
    assert isinstance(why, Failure) and isinstance(why.exception, ZeroDivisionError)
