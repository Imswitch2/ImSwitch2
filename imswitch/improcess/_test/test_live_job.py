"""Shared live-job bridge regression tests."""

from types import SimpleNamespace

from imswitch.improcess.live.discovery import JobQueue
from imswitch.improcess.live.job import watcher_entry_for_source


def test_jobqueue_public_resolve_entry_keeps_container_as_logical_job(tmp_path):
    container = tmp_path / "lapse"
    container.mkdir()
    later = container / "rec_scan02.hdf5"
    seed = container / "rec_scan00.hdf5"
    later.write_bytes(b"")
    seed.write_bytes(b"")

    discovered = JobQueue.resolve_entry(str(container))

    assert discovered is not None
    assert discovered.job_path == str(container)
    assert discovered.seed_path == str(seed)


def test_session_primary_path_maps_back_to_same_watcher_entry(tmp_path):
    container = tmp_path / "lapse"
    container.mkdir()
    seed = container / "rec_scan00.hdf5"
    seed.write_bytes(b"")

    assert watcher_entry_for_source(tmp_path, seed) == str(container.resolve())


def test_direct_root_dataset_remains_its_own_watcher_entry(tmp_path):
    source = tmp_path / "single.hdf5"
    source.write_bytes(b"")

    assert watcher_entry_for_source(tmp_path, source) == str(source.resolve())


def test_prepared_lapse_uses_logical_timelapse_provenance_identity():
    from imswitch.improcess.live.job import start_prepared_live_job
    from imswitch.improcess.live.source_type import RawSourceType

    captured = {}

    class _Live:
        def start(self, reconstructor, source, params, **kwargs):
            captured.update(kwargs)
            return True

    prepared = SimpleNamespace(
        seed_path="/tmp/lapse.hdf5",
        source_type=RawSourceType(
            format_id="hdf5",
            layout="single_lapse_file",
            frames_per_stack=576,
            num_timepoints=10,
        ),
        make_source=lambda detector_name=None: object(),
    )

    assert start_prepared_live_job(
        _Live(), prepared, object(), {}, name="lapse"
    )
    assert captured["source_dataset_name"] == "time-lapse"


def test_prepare_discovered_job_reports_persistent_hdf5_access_conflict(monkeypatch):
    import imswitch.improcess.live.job as job_module
    from imswitch.improcess.live.discovery import DiscoveredJob
    from imswitch.improcess.live.job import LiveJobState, prepare_discovered_job
    from imswitch.improcess.model.hdf5_access import RawHdf5AccessConflict

    def _conflict(_path):
        raise RawHdf5AccessConflict("raw file is already open with incompatible flags")

    monkeypatch.setattr(job_module, "probe_source_type", _conflict)
    discovered = DiscoveredJob("/tmp/raw.hdf5", "/tmp/raw.hdf5")
    reconstructor = SimpleNamespace(id="streaming", supports_streaming=True)

    resolution = prepare_discovered_job(discovered, reconstructor)

    assert resolution.state == LiveJobState.FAILED
    assert "incompatible flags" in resolution.reason
