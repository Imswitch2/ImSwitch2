"""Run journal and run file: commit protocol, recovery, reader validation."""
import json
import math

import numpy as np
import pytest

from imswitch.imcommon.model.measurement_run import (
    AcquisitionOutcome,
    ControlResult,
    JournalError,
    MeasurementRunFile,
    PointStatus,
    QuantitySpec,
    RunJournalWriter,
    Sample,
    Verification,
    WindowCause,
    WindowResult,
    finalize_journal,
    is_measurement_run_file,
    read_journal,
)

QUANTITIES = (
    QuantitySpec('azimuth', 'rad', 'polarisation.azimuth', -math.pi / 2, math.pi / 2),
    QuantitySpec('power', 'W', 'optical.power', 0.0),
)


def _sample(seq, az=0.1, power=1e-3, valid=True):
    return Sample(
        values={'azimuth': az, 'power': power}, t_host=float(seq), generation=1,
        sequence=seq, device_id=seq, valid=valid,
        reason='' if valid else 'azimuth out of range',
        verification=Verification.VERIFIED, profile_id='mock',
    )


def _window(samples, invalid=(), complete=True, cause=None, requested=None):
    return WindowResult(
        samples=tuple(samples), invalid=tuple(invalid), discarded=2,
        complete=complete, cause=cause, verification=Verification.VERIFIED,
        profile_id='mock', boundary_t=0.0,
        requested=len(samples) if requested is None else requested,
    )


def _writer(tmp_path, name='run.journal', fsync=False):
    return RunJournalWriter(
        tmp_path / name,
        metadata={'run_id': 'r1', 'grid': None},
        instruments={'pax1': QUANTITIES},
        controls=['hwp', 'qwp'],
        fsync=fsync,
    )


def _commit(writer, point, n=3, status=PointStatus.COMMITTED, **window_kw):
    seqs = range(point * 10, point * 10 + n)
    writer.commit_point(
        point=point, status=status, t_start=point, t_end=point + 0.5,
        controls=[
            ControlResult('hwp', requested=point * 1.0, acknowledged=point * 1.0,
                          measured=point * 1.0 + 0.01),
            ControlResult('qwp', requested=2.0 * point, ok=True),
        ],
        windows={'pax1': _window([_sample(s) for s in seqs], **window_kw)},
    )


def test_commit_finish_and_finalize_round_trip(tmp_path):
    writer = _writer(tmp_path, fsync=True)
    for point in range(3):
        _commit(writer, point)
    writer.finish(AcquisitionOutcome.COMPLETE)

    contents = read_journal(tmp_path / 'run.journal')
    assert [c['point'] for c in contents.commits] == [0, 1, 2]
    assert contents.problems == []
    assert contents.acquisition == AcquisitionOutcome.COMPLETE

    out = finalize_journal(tmp_path / 'run.journal')
    assert out.name == 'run.run.h5'
    assert is_measurement_run_file(out)
    run = MeasurementRunFile.load(out)
    assert run.n_points == 3
    assert run.acquisition == AcquisitionOutcome.COMPLETE
    assert list(run.committed_mask()) == [True, True, True]
    values, measured = run.control_values('hwp')
    np.testing.assert_allclose(values, [0.01, 1.01, 2.01])
    assert measured.all()
    qwp, qwp_measured = run.control_values('qwp')
    np.testing.assert_allclose(qwp, [0, 2, 4])  # requested: no readback
    assert not qwp_measured.any()
    block = run.point_samples('pax1', 1)
    assert list(block['sequence']) == [10, 11, 12]
    assert run.instruments_with_quantity('polarisation.azimuth') == ['pax1']
    assert run.window_verified('pax1', 0)


def test_invalid_samples_are_kept_but_marked(tmp_path):
    writer = _writer(tmp_path)
    writer.commit_point(
        point=0, status=PointStatus.FAILED_PARTIAL, t_start=0, t_end=1,
        controls=[],
        windows={'pax1': _window(
            [_sample(1)], invalid=[_sample(2, az=9.0, valid=False)],
            complete=False, cause=WindowCause.INVALID_SAMPLES, requested=5)},
    )
    writer.finish(AcquisitionOutcome.FAILED)
    run = MeasurementRunFile.load(finalize_journal(tmp_path / 'run.journal'))
    assert len(run.point_samples('pax1', 0)) == 1
    everything = run.point_samples('pax1', 0, valid_only=False)
    assert list(everything['valid']) == [1, 0]
    assert everything['reason'][1].startswith(b'azimuth out of range')
    window = run.instruments['pax1'].windows[0]
    assert window['cause'] == b'invalid_samples'
    assert window['requested'] == 5 and window['complete'] == 0


def test_interrupted_journal_recovers_committed_points(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0)
    _commit(writer, 1)
    writer.close()  # process "dies": no end.json
    run = MeasurementRunFile.load(finalize_journal(tmp_path / 'run.journal'))
    assert run.acquisition == AcquisitionOutcome.INTERRUPTED
    assert run.n_points == 2


def test_payload_without_commit_record_is_not_a_point(tmp_path):
    """Crash after the payload fsync, before the commit record."""
    writer = _writer(tmp_path)
    _commit(writer, 0)
    # Write point 1's payload by hand, as commit_point would, then stop.
    extra = np.zeros(3, dtype=writer._dtypes['pax1'])
    with open(tmp_path / 'run.journal' / 'samples' / 'pax1.bin', 'ab') as fh:
        fh.write(extra.tobytes())
    writer.close()
    contents = read_journal(tmp_path / 'run.journal')
    assert [c['point'] for c in contents.commits] == [0]
    assert len(contents.samples['pax1']) == 3


def test_torn_commit_record_is_ignored(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0)
    _commit(writer, 1)
    writer.close()
    log = tmp_path / 'run.journal' / 'commits.log'
    raw = log.read_bytes()
    log.write_bytes(raw[:-17])  # cut the last line in the middle
    contents = read_journal(tmp_path / 'run.journal')
    assert [c['point'] for c in contents.commits] == [0]
    assert any('torn' in p for p in contents.problems)


def test_corrupted_payload_stops_reading_at_that_point(tmp_path):
    writer = _writer(tmp_path)
    for point in range(3):
        _commit(writer, point)
    writer.close()
    path = tmp_path / 'run.journal' / 'samples' / 'pax1.bin'
    raw = bytearray(path.read_bytes())
    item = writer._dtypes['pax1'].itemsize
    raw[3 * item + 5] ^= 0xFF  # flip a byte inside point 1's extent
    path.write_bytes(bytes(raw))
    contents = read_journal(tmp_path / 'run.journal')
    assert [c['point'] for c in contents.commits] == [0]
    assert any('payload checksum mismatch' in p for p in contents.problems)


def test_truncated_payload_extent_is_rejected(tmp_path):
    """The commit record names an extent the payload file does not hold."""
    writer = _writer(tmp_path)
    _commit(writer, 0)
    _commit(writer, 1)
    writer.close()
    path = tmp_path / 'run.journal' / 'samples' / 'pax1.bin'
    item = writer._dtypes['pax1'].itemsize
    path.write_bytes(path.read_bytes()[:4 * item])
    contents = read_journal(tmp_path / 'run.journal')
    assert [c['point'] for c in contents.commits] == [0]
    assert any('outside the payload' in p for p in contents.problems)


def test_tampered_commit_line_fails_its_checksum(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0)
    writer.close()
    log = tmp_path / 'run.journal' / 'commits.log'
    text = log.read_text()
    log.write_text(text.replace('"committed"', '"failed"'))
    contents = read_journal(tmp_path / 'run.journal')
    assert contents.commits == []
    assert any('checksum mismatch' in p for p in contents.problems)


def test_writer_never_appends_to_an_existing_journal(tmp_path):
    _writer(tmp_path).close()
    with pytest.raises(JournalError, match='already exists'):
        _writer(tmp_path)


def test_duplicate_point_refused(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0)
    with pytest.raises(JournalError, match='already committed'):
        _commit(writer, 0)


def test_names_must_be_safe_file_names(tmp_path):
    with pytest.raises(JournalError, match='instrument name'):
        RunJournalWriter(
            tmp_path / 'x', metadata={}, instruments={'../evil': QUANTITIES},
            controls=[], fsync=False,
        )


def test_variable_sample_counts_per_point(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0, n=1)
    _commit(writer, 1, n=4, status=PointStatus.COMMITTED)
    _commit(writer, 2, n=0, status=PointStatus.FAILED, complete=False,
            cause=WindowCause.TIMEOUT, requested=3)
    writer.finish(AcquisitionOutcome.FAILED, detail='timeout')
    run = MeasurementRunFile.load(finalize_journal(tmp_path / 'run.journal'))
    assert [len(run.point_samples('pax1', r)) for r in range(3)] == [1, 4, 0]
    assert list(run.committed_mask()) == [True, True, False]
    assert run.metadata['end']['detail'] == 'timeout'


def test_run_json_records_quantities(tmp_path):
    _writer(tmp_path).close()
    meta = json.loads((tmp_path / 'run.journal' / 'run.json').read_text())
    names = [q['name'] for q in meta['instruments']['pax1']['quantities']]
    assert names == ['azimuth', 'power']
    assert meta['controls'] == [{'name': 'hwp'}, {'name': 'qwp'}]


def test_damaged_finished_journal_is_recovered_as_corrupt(tmp_path):
    """Review: corrupting the second commit of a completed two-point run gave a
    one-point file still marked COMPLETE."""
    writer = _writer(tmp_path)
    _commit(writer, 0)
    _commit(writer, 1)
    writer.finish(AcquisitionOutcome.COMPLETE)
    path = tmp_path / 'run.journal' / 'samples' / 'pax1.bin'
    raw = bytearray(path.read_bytes())
    raw[-3] ^= 0xFF                      # inside point 1's extent
    path.write_bytes(bytes(raw))
    contents = read_journal(tmp_path / 'run.journal')
    assert contents.damaged
    run = MeasurementRunFile.load(finalize_journal(tmp_path / 'run.journal'))
    assert run.acquisition is AcquisitionOutcome.CORRUPT
    assert run.n_points == 1
    assert run.metadata['recovery'] == {
        'damaged': True, 'recorded_outcome': 'complete',
        'points_expected': 2, 'points_recovered': 1}


def test_torn_tail_of_an_interrupted_journal_is_not_damage(tmp_path):
    writer = _writer(tmp_path)
    _commit(writer, 0)
    _commit(writer, 1)
    writer.close()
    log = tmp_path / 'run.journal' / 'commits.log'
    log.write_bytes(log.read_bytes()[:-17])
    contents = read_journal(tmp_path / 'run.journal')
    assert not contents.damaged
    assert contents.acquisition is AcquisitionOutcome.INTERRUPTED
