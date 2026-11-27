"""Session result labels reflect published timepoints, not just requested scope."""

from pathlib import Path
from types import SimpleNamespace

from imswitch.improcess.model.session_result_presentation import reconstruction_presentation
from imswitch.improcess.model.save_protocol import artifact_record, ProvenanceDocument
from imswitch.improcess.model.session_result_presentation import result_presentation


def _graph(mode="all", first=None, last=None, total=8):
    scope = {"mode": mode, "index_base": 0, "total": total}
    if first is not None:
        scope.update(first=first, last=last if last is not None else first)
    return {
        "output": {"node": "rec"},
        "nodes": {"rec": {
            "label": "MoNaLISA", "params": {"preset": "Fast"},
            "timepoint_scope": scope,
        }},
    }


def _assembly(ranges, *, mode="all", total=8, status="complete", missing=None):
    scope = {"mode": mode, "total": total, "index_base": 0}
    return {
        "relation": "timepoint", "axis": "T", "status": status,
        "requested_scope": scope,
        "published_t_ranges": ranges,
        "missing_frame_ranges": missing or [],
    }


def _label(graph, assembly=None):
    artifact = {"result_assembly": assembly} if assembly is not None else None
    return reconstruction_presentation(graph, artifact=artifact)


def test_full_acquisition_has_no_suffix():
    name, _, tooltip = _label(_graph(), _assembly([[0, 8]]))
    assert name == "MoNaLISA - Fast"
    assert "All 8 timepoints" in tooltip


def test_manual_range_shows_actual_indices_and_total():
    name, _, tooltip = _label(
        _graph("range", 2, 4),
        _assembly([[2, 5]], mode="range"),
    )
    assert name == "MoNaLISA - Fast · T3–T5 / 8"
    assert "Timepoints: T3–T5 / 8" in tooltip
    assert "Processing: Succeeded" in tooltip


def test_manual_single_shows_single_index_and_total():
    assert _label(_graph("single", 2), _assembly([[2, 3]], mode="single"))[0] == (
        "MoNaLISA - Fast · T3 / 8"
    )


def test_interrupted_live_shows_completed_not_requested_range():
    name, _, tooltip = _label(_graph(), _assembly([[0, 3]], status="failed"))
    assert name == "MoNaLISA - Fast · T1–T3 / 8"
    assert "Processing: Failed" in tooltip


def test_interrupted_manual_range_does_not_claim_unfinished_timepoints():
    name, _, _ = _label(
        _graph("range", 2, 6), _assembly([[2, 3]], mode="range", status="cancelled")
    )
    assert name == "MoNaLISA - Fast · T3 / 8"


def test_noncontiguous_coverage_is_not_silently_filled():
    name, _, _ = _label(_graph(), _assembly([[0, 2], [3, 4]], status="failed"))
    assert name == "MoNaLISA - Fast · T1–T2, T4 / 8"


def test_missing_raw_frames_prevent_implicit_full_coverage():
    name, _, tooltip = _label(_graph(), _assembly([[0, 8]], missing=[[12, 20]]))
    assert name == "MoNaLISA - Fast · T1–T8 / 8"
    assert "Missing raw-frame ranges: 1" in tooltip


def test_legacy_saved_subset_uses_scope_without_runtime_assembly():
    assert _label(_graph("range", 2, 4))[0] == "MoNaLISA - Fast · T3–T5 / 8"
    assert _label(_graph())[0] == "MoNaLISA - Fast"


def test_unknown_total_does_not_invent_denominator():
    graph = _graph("range", 2, 4)
    del graph["nodes"]["rec"]["timepoint_scope"]["total"]
    assert _label(graph)[0] == "MoNaLISA - Fast · T3–T5"


def test_inconsistent_actual_coverage_does_not_fall_back_to_requested_scope():
    bad = _assembly([[2, 9]], mode="range")
    assert _label(_graph("range", 2, 4), bad)[0] == "MoNaLISA - Fast"


def test_artifact_preserves_actual_coverage_for_saved_results():
    graph = _graph()
    assembly = _assembly([[0, 3]], status="failed")
    result = SimpleNamespace(name="my result", metadata={
        "provenance": graph, "result_assembly": assembly,
    }, result_uid="r1")
    # An artifact record is the persisted provenance vehicle; after reopening
    # the Session can render it without reading any image pixels.
    plan = SimpleNamespace(fmt="hdf5", primary=SimpleNamespace(name="out.h5"),
                           relative_names=lambda: ("out.h5",))
    artifact = artifact_record(plan, result)
    assert artifact["result_assembly"] == assembly
    assert reconstruction_presentation(graph, artifact=artifact)[0] == (
        "MoNaLISA - Fast · T1–T3 / 8"
    )


def test_saved_provenance_roundtrip_preserves_partial_label():
    graph = _graph()
    assembly = _assembly([[0, 3]], status="cancelled")
    document = ProvenanceDocument(graph=graph, artifact={"result_assembly": assembly})
    restored = ProvenanceDocument.from_json(document.to_json(), strict=True)
    name, _, tooltip = result_presentation(Path("saved.h5"), restored)
    assert name == "MoNaLISA - Fast · T1–T3 / 8"
    assert "Processing: Cancelled" in tooltip
