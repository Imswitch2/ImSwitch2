"""The workflow editor's run session, without Qt: what is outdated after an
edit, what a run binds from the cache and what it computes, that results are
filed under the fingerprints of the plan (not of the document after an edit
made meanwhile), that a nondeterministic step never mixes realisations, and
that every source handle stays open exactly as long as a cached result needs
it."""

import sys
from pathlib import Path

import numpy as np
import pytest

from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.graph import descendants
from imswitch.improcess.model.workfloweditor.session import RunSession, execute
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry
from imswitch.improcess.workflows.steps import Save

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(bootstrap_registry(user_plugins=False))


@pytest.fixture
def recording(tmp_path):
    return str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))


def _diamond(catalog):
    return WorkflowDocument.load(_EXAMPLES / "split_process_merge_diamond.yaml", catalog)


def _run(session, document, targets=None, **options):
    plan = session.plan(document, targets, **options)
    session.begin(plan)
    report, error = execute(plan, document.catalog.registry, on_step=session.step_done,
                            progress=lambda _i, _n, step_id: session.step_started(step_id))
    summary = session.finish(report, error, document=document)
    return plan, summary


def _statuses(session, document):
    return {step_id: state.status for step_id, state in session.states(document).items()}


# -- fingerprints ---------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(_EXAMPLES.glob("*.yaml")), ids=lambda p: p.stem)
def test_an_edit_outdates_exactly_the_steps_after_it(catalog, path):
    document = WorkflowDocument.load(path, catalog)
    session = RunSession(catalog.registry)
    before = session.fingerprints(document)
    for step in document.steps:
        if not isinstance(step, (Reconstruct, Process)):
            continue
        entry = document.entry_for(step)
        numeric = [f for f in (entry.fields if entry else ()) if f.type in ("int", "float") and not f.nullable]
        if not numeric:
            continue
        field = numeric[0]
        value = document.effective_params(step.id)[field.key]
        changed = WorkflowDocument(Workflow.from_dict(document.to_dict()), catalog=catalog)
        changed.set_param(step.id, field.key, (value or 0) + (field.step or 1))
        after = session.fingerprints(changed)
        moved = {sid for sid in before if before[sid] != after[sid]}
        assert moved == {step.id} | descendants(document.steps, step.id), (path.name, step.id)


def test_a_rename_outdates_nothing(catalog):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    before = session.fingerprints(document)
    document.rename_step("smooth", "blur")
    after = session.fingerprints(document)
    assert after["blur"] == before["smooth"]
    assert all(after[sid] == before[sid] for sid in before if sid != "smooth")


def test_binding_a_source_or_touching_its_file_outdates_what_follows(catalog, recording, tmp_path):
    import os

    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    unbound = session.fingerprints(document)
    session.bind_source(document, "raw", recording)
    bound = session.fingerprints(document)
    assert all(bound[sid] != unbound[sid] for sid in bound)
    stat = os.stat(recording)
    os.utime(recording, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    touched = session.fingerprints(document)
    assert all(touched[sid] != bound[sid] for sid in bound)


# -- runs and the cache ------------------------------------------------------------------

def test_a_change_reruns_only_what_follows_it(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    plan, summary = _run(session, document)
    assert summary.error is None and summary.ran == ["raw", "rec", "split", "smooth", "sharp", "merge"]
    assert _statuses(session, document) == {"raw": "done", "rec": "done", "split": "done", "smooth": "done",
                                            "sharp": "done", "merge": "done", "out": "would-write"}
    assert summary.would_write["out"].name == "cells_diamond.h5"
    document.set_param("smooth", "radius", 4.0)
    statuses = _statuses(session, document)
    assert [sid for sid, status in statuses.items() if status == "outdated"] == ["smooth", "merge"]
    plan, summary = _run(session, document, ["merge"])
    assert plan.to_run == ["smooth", "merge"] and summary.ran == ["smooth", "merge"]
    assert sorted(summary.cached) == ["rec", "sharp", "split"]       # reported by on_step_done(cached=True)
    assert _statuses(session, document)["merge"] == "done"
    assert session.plan(document, ["merge"]).empty


def test_results_are_filed_under_the_plan_not_the_document_after_an_edit(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    plan = session.plan(document, ["smooth"])
    session.begin(plan)
    document.set_param("smooth", "radius", 5.0)              # edited while the run goes
    report, error = execute(plan, catalog.registry, on_step=session.step_done)
    session.finish(report, error, document=document)
    assert _statuses(session, document)["smooth"] == "outdated"
    document.set_param("smooth", "radius", 2.0)              # back to what ran
    assert _statuses(session, document)["smooth"] == "done"


def test_a_nondeterministic_step_recomputed_outdates_what_follows(catalog, recording):
    document = WorkflowDocument(Workflow("w", [
        Source("raw", path=recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "out = data + np.random.random(data.shape)", "ports": "out"}, inputs=["rec"]),
        Process("blur", "filter", inputs=["py"]), Process("sharp", "filter", {"method": "median"}, inputs=["py"]),
    ]), catalog=catalog)
    session = RunSession(catalog.registry)
    _run(session, document)
    assert set(_statuses(session, document).values()) == {"done"}
    sharp = session.result(document, "sharp")
    # Only "sharp" is dropped: "py" must run again for it, so "blur" -- made
    # from the old realisation -- may not be combined with the new one.
    session.rerun_from(document, "sharp")
    first_py = session.fingerprints(document)["py"]
    session.rerun_from(document, "py")
    plan, summary = _run(session, document)
    assert {"py", "blur", "sharp"} <= set(summary.ran)
    assert session.fingerprints(document)["py"] != first_py
    assert session.result(document, "sharp") is not sharp
    again = session.plan(document)
    assert again.empty


def test_a_deterministic_step_is_not_rerun_because_a_sibling_was(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    session.rerun_from(document, "sharp")
    plan, summary = _run(session, document)
    assert summary.ran == ["sharp", "merge"] and "smooth" in summary.cached


def test_a_failure_is_on_its_step_and_fixing_it_continues_from_there(catalog, recording):
    document = WorkflowDocument(Workflow("w", [
        Source("raw", path=recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "print('looking')\nx = 1\nraise ValueError('nope')\n", "ports": "out"},
                inputs=["rec"]),
        Save("out", input="py"),
    ]), catalog=catalog)
    session = RunSession(catalog.registry)
    plan, summary = _run(session, document)
    assert summary.failure["step"] == "py" and summary.failure["line"] == 3
    assert "looking" in summary.failure["stdout"]
    states = session.states(document)
    assert states["py"].status == "failed" and "nope" in states["py"].error["message"]
    assert states["rec"].status == "done" and states["out"].status == ""
    document.set_param("py", "code", "out = data.copy()\n")
    assert session.states(document)["py"].status == ""
    plan, summary = _run(session, document)
    assert plan.to_run == ["py", "out"] and summary.cached == ["rec"] and summary.failure is None


def test_write_files_writes_what_the_saves_would_have(catalog, recording, tmp_path):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    out = tmp_path / "out"
    plan, summary = _run(session, document, write_saves=True, out_dir=out, overwrite=True)
    assert plan.to_run == ["out"]                       # everything else came from the cache
    assert (out / "cells_diamond.h5").exists()
    assert session.states(document)["out"].status == "done"
    assert [p.name for p in summary.wrote["out"]] == ["cells_diamond.h5"]


def test_a_result_from_the_list_starts_its_step(catalog, recording):
    document = _diamond(catalog)
    source_session = RunSession(catalog.registry)
    source_session.bind_source(document, "raw", recording)
    _run(source_session, document, ["rec"])
    result = source_session.result(document, "rec")
    session = RunSession(catalog.registry)
    session.bind_result(document, "rec", result)
    plan, summary = _run(session, document, ["split"])
    assert "raw" not in plan.to_run and plan.cached == ["rec"]
    assert summary.error is None and session.states(document)["rec"].bound == result.name
    assert plan.source_stem == result.name
    source_session.close()
    session.close()


# -- handles and memory --------------------------------------------------------------------

def test_a_cached_lazy_result_keeps_its_file_open_until_nothing_needs_it(catalog, recording):
    import numpy as np

    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document, ["rec"])
    handles = session.open_handles()
    assert len(handles) == 1
    rec = session.result(document, "rec")
    _run(session, document)                          # a later run reads the cached result
    assert np.asarray(rec.data).shape == (2, 96, 96)  # still readable
    assert session.open_handles() == handles
    session.rerun_from(document, "rec")              # every entry made from the file is dropped...
    assert session.open_handles() == []              # ...so it is closed
    session.close()
    assert session.open_handles() == []


def test_rebinding_a_source_releases_what_was_made_from_the_old_one(catalog, recording, tmp_path):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    assert len(session.open_handles()) == 1
    other = str(write_synthetic_recording(tmp_path / "other.h5", frames=2, axes="CYX", seed=3))
    session.bind_source(document, "raw", other)
    assert session.open_handles() == []
    assert set(_statuses(session, document).values()) <= {"outdated", "would-write", "done"}
    assert session.cache_bytes() == 0


def test_the_cache_drops_outdated_results_first_and_keeps_the_selected_and_pinned(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    session._budget = 1
    document.set_param("smooth", "radius", 4.0)
    pinned = session.pin(document, "split")
    assert pinned is not None
    dropped = session.trim(document, keep=["sharp"])
    assert "smooth" in dropped and "merge" in dropped           # outdated: gone first
    assert session.result(document, "split") is pinned
    assert session.result(document, "sharp") is not None
    assert session.states(document)["smooth"].status == "outdated"   # its preview stays
    assert session.states(document)["smooth"].entry.previews


def test_closing_hands_over_the_files_of_pinned_results_and_closes_the_rest(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    rec = session.pin(document, "rec")
    handover = session.close()
    assert len(handover) == 1
    uids, handles = handover[0]
    assert uids == {rec.result_uid} and len(handles) == 1
    from imswitch.improcess.workflows.sources import close_source

    for handle in handles:
        close_source(handle)


def test_previews_describe_what_each_output_is(catalog, recording):
    document = WorkflowDocument.load(_EXAMPLES / "python_step_signal_trace.yaml", catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", str(write_synthetic_recording(
        Path(recording).with_name("trace.h5"), frames=12, seed=3)))
    _run(session, document)
    entry = session.states(document)["script"].entry
    assert set(entry.previews) == {"level", "area"}
    assert all(p.kind == "curve" and p.curve is not None for p in entry.previews.values())
    split = session.states(document)["rec"].entry.previews["out"]
    assert split.rgb is not None and split.rgb.dtype.name == "uint8" and split.describe()


# -- what the independent review found ------------------------------------------------------

def test_a_preview_reads_the_recording_lazily(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    seen = {}

    def on_step(step_id, ports, seconds, cached, previews):
        seen[step_id] = ports
        session.step_done(step_id, ports, seconds, cached, previews)

    plan = session.plan(document, ["rec"])
    session.begin(plan)
    report, error = execute(plan, catalog.registry, on_step=on_step)
    session.finish(report, error, document=document)
    assert error is None
    # Its preview was made, from one plane: the recording was not decoded...
    assert session.states(document)["raw"].entry.previews["data"].rgb is not None
    assert seen["raw"]["data"].dataLoaded is False
    # ...so the reconstruction after it is still a view of the file.
    assert not isinstance(session.result(document, "rec").data, np.ndarray)
    session.close()


def test_a_run_keeps_open_what_it_reads_whatever_is_done_meanwhile(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document, ["rec"])
    plan = session.plan(document, ["split"])
    assert plan.cached == ["rec"]
    session.begin(plan)
    session.rerun_from(document, "rec")         # drops every entry made from the file, while the run reads one
    assert len(session.open_handles()) == 1
    report, error = execute(plan, catalog.registry, on_step=session.step_done)
    summary = session.finish(report, error, document=document)
    assert summary.error is None
    assert np.asarray(session.result(document, "split").data).shape == (96, 96)
    session.rerun_from(document, "split")
    assert session.open_handles() == []         # nothing needs it any more
    session.close()


def test_two_pinned_results_of_one_file_hand_it_over_once(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document)
    rec = session.pin(document, "rec")
    split = session.pin(document, "split")
    handover = session.close()
    assert len(handover) == 1                   # the controller closes it once neither is loaded
    uids, handles = handover[0]
    assert uids == {rec.result_uid, split.result_uid} and len(handles) == 1
    from imswitch.improcess.workflows.sources import close_source

    close_source(handles[0])


def test_a_save_that_wrote_holds_no_result_and_no_file(catalog, recording, tmp_path):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document, write_saves=True, out_dir=tmp_path / "out", overwrite=True)
    assert session.states(document)["out"].status == "done"
    assert session.states(document)["out"].entry is None
    assert len(session._cache) == len(document.steps) - 1
    session.rerun_from(document, "raw")
    assert session.open_handles() == []         # no Save entry keeps the recording open


def test_a_bound_file_is_read_at_the_dataset_it_was_bound_with(catalog, recording):
    import h5py

    with h5py.File(recording, "a") as handle:
        handle.create_dataset("other", data=np.zeros((3, 40, 40), np.float32)).attrs["axes"] = "CYX"
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    default = session.fingerprints(document)["raw"]
    session.bind_source(document, "raw", recording, dataset="other")
    assert session.fingerprints(document)["raw"] != default
    plan, summary = _run(session, document, ["rec"])
    assert plan.bindings["raw"].dataset == "other" and summary.error is None
    assert np.asarray(session.result(document, "rec").data).shape == (3, 40, 40)
    session.close()


def test_a_plan_that_is_not_run_changes_nothing(catalog, recording):
    document = WorkflowDocument(Workflow("w", [
        Source("raw", path=recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "out = data + np.random.random(data.shape)", "ports": "out"}, inputs=["rec"]),
        Process("blur", "filter", inputs=["py"]), Process("sharp", "filter", {"method": "median"}, inputs=["py"]),
    ]), catalog=catalog)
    session = RunSession(catalog.registry)
    _run(session, document)
    session.rerun_from(document, "sharp")
    session._evict(session.states(document)["py"].entry, keep_preview=True)    # trimmed, say
    before, statuses = session.fingerprints(document), _statuses(session, document)
    plan = session.plan(document, ["sharp"])     # "py" is realised again, so "blur" would be outdated...
    assert plan.to_run == ["py", "sharp"] and plan.nonces != session._nonces
    # ...but only once the plan runs: one that is dropped (auto-run declined) changes nothing.
    assert session.fingerprints(document) == before and _statuses(session, document) == statuses
    assert statuses["blur"] == "done"
    session.begin(plan)
    assert _statuses(session, document)["blur"] == "outdated"
    session.finish(None, "cancelled", document=document)


def test_a_rename_keeps_what_the_session_bound_to_the_step(catalog, recording):
    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    _run(session, document, ["rec"])
    document.rename_step("raw", "input")
    session.rename_step("raw", "input")
    assert session.source_bindings == {"input": recording}
    assert _statuses(session, document)["rec"] == "done"
    plan = session.plan(document, ["split"])
    assert plan.to_run == ["split"] and plan.cached == ["rec"]
    session.close()


def test_a_stop_that_lands_between_steps_ends_the_run_cancelled(catalog, recording, monkeypatch):
    from imswitch.imcommon.model import OperationCancelled
    from imswitch.improcess.workflows import runner

    document = _diamond(catalog)
    session = RunSession(catalog.registry)
    session.bind_source(document, "raw", recording)
    plan = session.plan(document, ["rec"])

    def stopped(*_args, **_kwargs):
        raise OperationCancelled()

    with monkeypatch.context() as patch:
        patch.setattr(runner, "run", stopped)
        assert execute(plan, catalog.registry) == (None, "cancelled")

    # One landing while a finished step is reported stops the run with its
    # report, so the sources it opened can be closed.
    def on_step_done(step_id, _ports, _seconds, _cached):
        if step_id == "rec":
            raise OperationCancelled()

    with pytest.raises(runner.RunError) as caught:
        runner.run(plan.workflow, registry=catalog.registry, bindings=plan.bindings, out_dir=plan.out_dir,
                   on_step_done=on_step_done, write_saves=False)
    report = caught.value.report
    assert report.error == "rec: cancelled" and report.failure["type"] == "OperationCancelled"
    from imswitch.improcess.workflows.sources import close_source

    for handle in report.detach_sources():
        close_source(handle)
