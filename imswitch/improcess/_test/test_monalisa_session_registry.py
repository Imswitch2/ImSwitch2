"""Session-scoped MoNaLISA calibration registry and locked resolution."""

import json
from pathlib import Path

import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.params import (
    LOCALIZATION_LOCKED,
    ORIENTATION_LOCKED,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.session_registry import (
    JOURNAL_FILENAME,
    MonalisaSessionRegistry,
)


def _localization(period=10.0, offset=1.0):
    return PatternLocalization.manual_grid(
        period, period + 1.0, offset, offset + 0.5,
        stagger_axis="x", stagger=0.5,
    )


def test_registry_keeps_multiple_named_profiles_per_detector(tmp_path):
    registry = MonalisaSessionRegistry(tmp_path)
    first = registry.lock_localization(
        name="square", detector="CamA", localization=_localization(10.0), source="auto"
    )
    second = registry.lock_localization(
        name="staggered", detector="CamA", localization=_localization(12.0), source="manual"
    )
    other = registry.lock_localization(
        name="square", detector="CamB", localization=_localization(14.0), source="auto"
    )

    assert {entry.id for entry in registry.localization_profiles("CamA")} == {first.id, second.id}
    assert {entry.id for entry in registry.localization_profiles("CamB")} == {other.id}

    renamed = registry.rename_localization(first.id, "square aligned")
    assert renamed.name == "square aligned"
    registry.delete_localization(second.id)
    assert [entry.id for entry in registry.localization_profiles("CamA")] == [first.id]
    registry.complete_session()


def test_job_payload_survives_profile_rename_or_delete(tmp_path):
    registry = MonalisaSessionRegistry(tmp_path)
    profile = registry.lock_localization(
        name="good", detector="CamA", localization=_localization(), source="auto"
    )
    # This is the immutable payload queued by the parameter widget.
    queued_payload = profile.localization.to_dict()
    registry.rename_localization(profile.id, "renamed")
    registry.delete_localization(profile.id)

    restored = PatternLocalization.from_dict(queued_payload)
    assert restored is not None
    assert restored.to_dict()["lattice"] == profile.localization.to_dict()["lattice"]
    registry.complete_session()


def test_orientation_lock_is_detector_scoped(tmp_path):
    registry = MonalisaSessionRegistry(tmp_path)
    registry.lock_orientation("CamA", "+x-y", source="auto")
    registry.lock_orientation("CamB", "-y+x", source="manual")

    assert registry.locked_orientation("CamA").orientation.to_literal() == "+x-y"
    assert registry.locked_orientation("CamB").orientation.to_literal() == "-y+x"
    registry.clear_orientation("CamA")
    assert registry.locked_orientation("CamA") is None
    assert registry.locked_orientation("CamB") is not None
    registry.complete_session()


def test_recovery_only_returns_interrupted_active_sessions(tmp_path):
    interrupted = MonalisaSessionRegistry(tmp_path)
    interrupted.lock_localization(
        name="recover me", detector="CamA", localization=_localization(), source="auto"
    )
    assert interrupted.journal_path.exists()

    current = MonalisaSessionRegistry(tmp_path)
    candidates = current.recovery_candidates()
    assert len(candidates) == 1
    assert candidates[0].path == interrupted.journal_path

    current.restore(candidates[0])
    assert [entry.name for entry in current.localization_profiles("CamA")] == ["recover me"]
    old_payload = json.loads(interrupted.journal_path.read_text(encoding="utf-8"))
    assert old_payload["status"] == "restored"

    current.complete_session()
    assert MonalisaSessionRegistry(tmp_path).recovery_candidates() == ()
    interrupted.complete_session()



def test_discard_marks_recovery_journal_and_removes_candidate(tmp_path):
    interrupted = MonalisaSessionRegistry(tmp_path)
    interrupted.lock_orientation("CamA", "+x+y", source="auto")

    current = MonalisaSessionRegistry(tmp_path)
    candidate = current.recovery_candidates()[0]
    current.discard(candidate)

    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    assert payload["status"] == "discarded"
    assert current.recovery_candidates() == ()
    current.complete_session()
    interrupted.complete_session()

def test_clean_completed_session_is_not_recoverable(tmp_path):
    registry = MonalisaSessionRegistry(tmp_path)
    registry.lock_orientation("CamA", "+x+y", source="manual")
    journal = registry.journal_path
    registry.complete_session()

    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert MonalisaSessionRegistry(tmp_path).recovery_candidates() == ()


def test_journal_is_under_sessions_root_not_source_data_path(tmp_path):
    sessions = tmp_path / "imswitch-sessions"
    source = tmp_path / "experiment" / "movie.hdf5"
    registry = MonalisaSessionRegistry(sessions)
    registry.lock_localization(
        name="session", detector="CamA", localization=_localization(), source="auto",
        source_path=str(source),
    )

    assert registry.journal_path.parent.parent == sessions
    assert registry.journal_path.name == JOURNAL_FILENAME
    assert source.parent not in registry.journal_path.parents
    registry.complete_session()


def test_locked_reconstructor_resolution_uses_serialized_payload():
    # Import here so registry model tests stay useful even in minimal installs.
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor

    localization = _localization()
    params = default_params()
    params.update({
        "localization_mode": LOCALIZATION_LOCKED,
        "locked_localization": localization.to_dict(),
        "orientation_mode": ORIENTATION_LOCKED,
        "locked_orientation": "-x+y",
    })

    resolved = MonalisaReconstructor._resolved_localization(params)
    assert resolved is not None
    assert resolved.to_dict()["lattice"] == localization.to_dict()["lattice"]
    assert MonalisaReconstructor._orientation_literal(params) == "-x+y"


def test_locked_reconstructor_resolution_rejects_missing_values():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor

    params = default_params()
    params["localization_mode"] = LOCALIZATION_LOCKED
    params["locked_localization"] = {}
    with pytest.raises(ValueError, match="no locked localization"):
        MonalisaReconstructor._resolved_localization(params)

    params = default_params()
    params["orientation_mode"] = ORIENTATION_LOCKED
    params["locked_orientation"] = ""
    with pytest.raises(ValueError, match="no detector orientation"):
        MonalisaReconstructor._orientation_literal(params)
