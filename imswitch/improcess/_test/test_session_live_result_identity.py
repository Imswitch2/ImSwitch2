"""Completed live snapshots and their saved artifact form one Session row."""

from pathlib import Path
from types import SimpleNamespace

from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController


class _SessionIndex:
    """Exercise the real index methods without constructing the Qt widget."""

    _index_loaded_result = ProcessingSessionController._index_loaded_result
    _unindex_loaded_result = ProcessingSessionController._unindex_loaded_result
    _loaded_reconstructions_for_dataset = (
        ProcessingSessionController._loaded_reconstructions_for_dataset
    )
    _loaded_by_saved_path = ProcessingSessionController._loaded_by_saved_path
    _reconstruction_entries = ProcessingSessionController._reconstruction_entries
    _loaded_key = staticmethod(ProcessingSessionController._loaded_key)
    _is_session_reconstruction = staticmethod(lambda _result: True)

    def __init__(self):
        self.dataset = SimpleNamespace(id="acquisition", saved_reconstructions=[])
        self._session = SimpleNamespace(dataset_for_result=lambda _result: self.dataset)
        self._loadedResultOwners = {}
        self._loadedResultsByDatasetId = {}
        self._loadedBySavedPath = {}


def _snapshot(job_id: str | None, completed: int, total: int = 3):
    graph = {
        "output": {"node": "rec"},
        "nodes": {"rec": {
            "op": "reconstruct", "label": "MoNaLISA", "params": {"preset": "Fast"},
            "timepoint_scope": {"mode": "all", "total": total, "index_base": 0},
        }},
    }
    assembly = {
        "relation": "timepoint", "axis": "T", "status": "complete",
        "requested_scope": {"mode": "all", "total": total, "index_base": 0},
        "published_t_ranges": [[0, completed]],
    }
    return SimpleNamespace(
        name="live", _improcess_queue_job_id=job_id,
        metadata={"provenance": graph, "result_assembly": assembly},
    )


def test_live_final_replaces_initial_snapshot_in_session_index(tmp_path):
    index = _SessionIndex()
    preview = _snapshot("job-1", 1)
    final = _snapshot("job-1", 3)

    index._index_loaded_result("live", preview)
    assert [row["name"] for row in index._reconstruction_entries(index.dataset)] == [
        "MoNaLISA - Fast · T1 / 3"
    ]

    # The viewer has replaced its own entry in place, without emitting a
    # separate membership signal. Session receives the terminal result.
    index._index_loaded_result("live", final)
    assert len(index._loadedResultsByDatasetId[index.dataset.id]) == 1
    assert id(preview) not in index._loadedResultOwners
    assert index._loadedResultOwners[id(final)] == index.dataset.id
    assert [row["name"] for row in index._reconstruction_entries(index.dataset)] == [
        "MoNaLISA - Fast"
    ]

    # Auto-save registers the final file. The same final object is merged
    # with its saved entry, not displayed alongside a phantom T1 snapshot.
    path = tmp_path / "final.h5"
    final._session_saved_path = str(path)
    index._loadedBySavedPath[str(path.resolve())] = final
    index.dataset.saved_reconstructions.append(SimpleNamespace(
        path=str(path), name="MoNaLISA - Fast", tooltip="saved",
    ))
    rows = index._reconstruction_entries(index.dataset)
    assert len(rows) == 1
    assert rows[0]["saved"] is index.dataset.saved_reconstructions[0]
    assert rows[0]["loaded"] is final


def test_different_live_jobs_are_kept_separate():
    index = _SessionIndex()
    first = _snapshot("job-1", 3)
    second_preview = _snapshot("job-2", 1)
    second_final = _snapshot("job-2", 3)
    for result in (first, second_preview, second_final):
        index._index_loaded_result("live", result)
    rows = index._reconstruction_entries(index.dataset)
    assert len(rows) == 2
    assert {id(row["loaded"]) for row in rows} == {id(first), id(second_final)}


def test_unqueued_results_do_not_collapse_by_name():
    index = _SessionIndex()
    first = _snapshot(None, 3)
    second = _snapshot(None, 3)
    index._index_loaded_result("same name", first)
    index._index_loaded_result("same name", second)
    assert len(index._reconstruction_entries(index.dataset)) == 2
