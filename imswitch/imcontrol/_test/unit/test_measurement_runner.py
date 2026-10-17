"""Measurement-run engine on mock waveplates and a mock PAX (no hardware)."""
import math
import threading
import time

import numpy as np
import pytest

from imswitch.imcommon.algorithms.polarisation import (
    TwoPlateModel,
    aggregate_polarisation,
    angular_distance_deg,
)
from imswitch.imcommon.model.measurement_run import (
    AcquisitionOutcome,
    CleanupOutcome,
    MeasurementRunFile,
    PointStatus,
    RunLifecycle,
)
from imswitch.imcontrol.model.measurement import (
    ControlExecutor,
    InstrumentSession,
    MeasurementRunner,
    RunRefused,
    RunSettings,
    grid,
    points,
)
from imswitch.imcontrol.model.measurement.mocks import MockPAXDriver, MockRotatorControl
from imswitch.imcontrol.model.resources import (
    ReservationExpiredError,
    ResourceRegistry,
    set_resource_registry,
)


@pytest.fixture(autouse=True)
def registry():
    """A fresh process-wide resource registry per test."""
    fresh = ResourceRegistry()
    previous = set_resource_registry(fresh)
    yield fresh
    set_resource_registry(previous)

MODEL = TwoPlateModel(offset1_deg=3.0, offset2_deg=-2.0)


def _rig(*, speed=2000.0, latency=0.0, has_counter=True, has_clock=True,
         verified=True, revolution=0.008):
    hwp = MockRotatorControl('hwp', speed_deg_s=speed)
    qwp = MockRotatorControl('qwp', speed_deg_s=speed)
    driver = MockPAXDriver(qwp, hwp, model=MODEL, revolution_s=revolution,
                           latency_s=latency, has_counter=has_counter,
                           has_clock=has_clock, verified=verified)
    session = InstrumentSession('pax1', driver)
    session.connect()
    return hwp, qwp, driver, session


def _settings(**kw):
    base = dict(samples_per_point=2, fsync=False, window_deadline_s=2.0,
                move_deadline_s=5.0, plane_label='sample plane',
                illumination={'source': '633', 'wavelength_nm': 633.0})
    base.update(kw)
    return RunSettings(**base)


def _direction_errors(run_file, driver):
    """Angle between each committed point's aggregate and the model's truth."""
    run = MeasurementRunFile.load(run_file)
    qwp, _ = run.control_values('qwp')
    hwp, _ = run.control_values('hwp')
    errors = []
    for row in np.flatnonzero(run.committed_mask()):
        block = run.point_samples('pax1', int(row))
        agg = aggregate_polarisation(block['q_azimuth'], block['q_ellipticity'],
                                     block['q_dop'], block['q_power'])
        expected = driver.expected_direction(qwp[row], hwp[row])
        errors.append(float(angular_distance_deg(agg.direction, expected)))
    return np.array(errors)


@pytest.mark.parametrize('counter,clock_', [(True, True), (False, True), (False, False)])
def test_grid_run_measures_the_model_under_every_timing_rule(tmp_path, counter, clock_):
    hwp, qwp, driver, session = _rig(has_counter=counter, has_clock=clock_)
    seq = grid([('hwp', [0, 30, 60]), ('qwp', [0, 45, 90])])
    report = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(),
    ).run()
    assert report.acquisition is AcquisitionOutcome.COMPLETE
    assert report.cleanup is CleanupOutcome.DONE
    assert report.lifecycle is RunLifecycle.FINISHED
    assert report.points_committed == 9
    assert report.run_file is not None and report.run_file.exists()
    assert not report.journal_dir.exists()
    errors = _direction_errors(report.run_file, driver)
    assert len(errors) == 9
    assert errors.max() < 0.5, errors
    run = MeasurementRunFile.load(report.run_file)
    assert run.grid.shape == (3, 3)
    assert run.metadata['illumination']['wavelength_nm'] == 633.0
    assert run.metadata['instruments']['pax1']['verification'] == 'verified'
    # returned to the start positions
    assert hwp.read_position() == pytest.approx(0.0)
    assert qwp.read_position() == pytest.approx(0.0)


def test_stale_samples_across_a_move_are_rejected(tmp_path):
    """Slow moves + latency: reads right after settling return measurements
    that started during the move. The window must discard them."""
    hwp, qwp, driver, session = _rig(speed=600.0, latency=0.02, revolution=0.01)
    seq = grid([('hwp', [0, 45]), ('qwp', [0, 90])])
    report = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(),
    ).run()
    assert report.points_committed == 4
    run = MeasurementRunFile.load(report.run_file)
    assert run.instruments['pax1'].windows['discarded'].sum() > 0
    assert _direction_errors(report.run_file, driver).max() < 0.5


def test_stop_mid_run_keeps_committed_points_and_returns(tmp_path):
    hwp, qwp, driver, session = _rig()
    seq = grid([('hwp', [0, 20, 40, 60]), ('qwp', [0, 30, 60])])
    runner = None

    def progress(event):
        if event.point == 3:
            runner.stop('enough')

    runner = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(), progress=progress,
    )
    report = runner.run()
    assert report.acquisition is AcquisitionOutcome.STOPPED
    assert report.points_committed == 4
    assert report.detail == 'enough'
    assert report.cleanup is CleanupOutcome.DONE
    run = MeasurementRunFile.load(report.run_file)
    assert run.acquisition is AcquisitionOutcome.STOPPED
    assert run.n_points == 4
    assert hwp.read_position() == pytest.approx(0.0)


def test_failed_control_never_commits_and_skips_return(tmp_path):
    hwp, qwp, driver, session = _rig()
    seq = grid([('hwp', [0, 30]), ('qwp', [0, 45])])

    def progress(event):
        if event.point == 1:
            hwp.fail_next = 'driver error: motor fault'

    report = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(), progress=progress,
    ).run()
    assert report.acquisition is AcquisitionOutcome.FAILED
    run = MeasurementRunFile.load(report.run_file)
    statuses = run.status()
    assert statuses[:2] == [PointStatus.COMMITTED, PointStatus.COMMITTED]
    assert statuses[2] is PointStatus.FAILED
    assert run.controls['hwp']['ok'][2] == 0
    assert b'motor fault' in run.controls['hwp']['cause'][2]
    assert 'no return to start' in report.cleanup_detail
    assert hwp.read_position() == pytest.approx(0.0)   # the failed move never started
    assert qwp.read_position() == pytest.approx(45.0)  # left in place, not returned


def test_stuck_move_quarantines_its_backend_until_it_returns(tmp_path, registry):
    hwp, qwp, driver, session = _rig()
    executor = ControlExecutor()
    seq = grid([('hwp', [0, 30]), ('qwp', [0])])

    def progress(event):
        if event.point == 0:
            hwp.stall.set()

    runner = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, executor=executor,
        settings=_settings(move_deadline_s=0.2, cleanup_deadline_s=0.2),
        progress=progress,
    )
    report = runner.run()
    assert report.acquisition is AcquisitionOutcome.FAILED
    assert report.cleanup is CleanupOutcome.QUARANTINED
    assert report.quarantined == [hwp.resource]
    assert report.lifecycle is RunLifecycle.FINISHED
    # still held: nobody else may command it while the move runs
    assert registry.holder(hwp.resource) is not None
    assert registry.holder(qwp.resource) is None
    assert executor.is_quarantined(hwp.resource)
    hwp.release_stall.set()
    assert executor.wait_released(hwp.resource, 5.0)
    deadline = time.monotonic() + 2.0
    while registry.holder(hwp.resource) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.holder(hwp.resource) is None


def test_instrument_fault_ends_the_run(tmp_path):
    hwp, qwp, driver, session = _rig()
    seq = grid([('hwp', [0, 30, 60]), ('qwp', [0])])

    def progress(event):
        if event.point == 0:
            driver.unplugged = True

    report = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(), progress=progress,
    ).run()
    assert report.acquisition is AcquisitionOutcome.FAILED
    assert 'instrument fault' in report.detail
    assert session.faulted
    run = MeasurementRunFile.load(report.run_file)
    assert run.status()[0] is PointStatus.COMMITTED
    assert run.status()[1] is not PointStatus.COMMITTED
    assert run.instruments['pax1'].windows['cause'][1] == b'transport_fault'


def test_invalid_samples_are_kept_and_do_not_fault(tmp_path):
    hwp, qwp, driver, session = _rig()
    driver.invalid_next = 2
    report = MeasurementRunner(
        sequence=points(['hwp', 'qwp'], [[0, 0]]), controls=[hwp, qwp],
        instruments={'pax1': session}, folder=tmp_path, settings=_settings(),
    ).run()
    assert report.points_committed == 1
    run = MeasurementRunFile.load(report.run_file)
    everything = run.point_samples('pax1', 0, valid_only=False)
    assert (everything['valid'] == 0).sum() == 2
    assert len(run.point_samples('pax1', 0)) == 2
    assert not session.faulted


def test_a_run_of_invalid_samples_fails_the_point_not_the_connection(tmp_path):
    hwp, qwp, driver, session = _rig()
    driver.malformed_next = 50
    report = MeasurementRunner(
        sequence=points(['hwp', 'qwp'], [[0, 0], [10, 0]]), controls=[hwp, qwp],
        instruments={'pax1': session}, folder=tmp_path,
        settings=_settings(max_consecutive_failed_points=2),
    ).run()
    run = MeasurementRunFile.load(report.run_file)
    assert run.instruments['pax1'].windows['cause'][0] == b'invalid_samples'
    assert not session.faulted
    assert report.acquisition is AcquisitionOutcome.FAILED


def test_unverified_timing_is_refused_unless_characterising(tmp_path):
    hwp, qwp, driver, session = _rig(verified=False)
    seq = points(['hwp', 'qwp'], [[0, 0]])
    with pytest.raises(RunRefused, match='not verified'):
        MeasurementRunner(sequence=seq, controls=[hwp, qwp],
                          instruments={'pax1': session}, folder=tmp_path,
                          settings=_settings()).run()
    report = MeasurementRunner(
        sequence=seq, controls=[hwp, qwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(allow_unverified_timing=True),
    ).run()
    run = MeasurementRunFile.load(report.run_file)
    assert not run.window_verified('pax1', 0)
    assert (run.point_samples('pax1', 0)['verified'] == 0).all()


def test_settings_outside_every_profile_refuse_the_run(tmp_path):
    hwp, qwp, driver, session = _rig()
    session.set_setting('mode', 1)
    with pytest.raises(RunRefused, match='no timing profile'):
        MeasurementRunner(sequence=points(['hwp', 'qwp'], [[0, 0]]),
                          controls=[hwp, qwp], instruments={'pax1': session},
                          folder=tmp_path, settings=_settings()).run()


def test_reserved_resources_refuse_the_run_and_are_released_after(tmp_path, registry):
    hwp, qwp, driver, session = _rig()
    other = registry.reserve([hwp.resource], 'someone else')
    seq = points(['hwp', 'qwp'], [[0, 0]])
    with pytest.raises(RunRefused, match='someone else'):
        MeasurementRunner(sequence=seq, controls=[hwp, qwp],
                          instruments={'pax1': session}, folder=tmp_path,
                          settings=_settings(reserve_deadline_s=0.1)).run()
    registry.release(other.token)
    runner = MeasurementRunner(sequence=seq, controls=[hwp, qwp],
                               instruments={'pax1': session}, folder=tmp_path,
                               settings=_settings())
    runner.run()
    for resource in (hwp.resource, qwp.resource, 'instrument:pax1', 'waveform-output'):
        assert registry.holder(resource) is None
    with pytest.raises(ReservationExpiredError):
        registry.check(hwp.resource, runner._reservation.token)


def test_snake_grid_records_its_traversal(tmp_path):
    hwp, qwp, driver, session = _rig()
    seq = grid([('hwp', [0, 30]), ('qwp', [0, 45, 90])], traversal='snake')
    assert [i for i in seq.grid.index] == [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]
    report = MeasurementRunner(sequence=seq, controls=[hwp, qwp],
                               instruments={'pax1': session}, folder=tmp_path,
                               settings=_settings()).run()
    run = MeasurementRunFile.load(report.run_file)
    assert run.grid.traversal == 'snake'
    np.testing.assert_array_equal(run.points['grid_index'][3], [1, 2])


def test_point_list_run_has_no_grid(tmp_path):
    hwp, qwp, driver, session = _rig()
    report = MeasurementRunner(
        sequence=points(['hwp', 'qwp'], [[10, 20], [33.3, 71.0]], source='predicted'),
        controls=[hwp, qwp], instruments={'pax1': session}, folder=tmp_path,
        settings=_settings()).run()
    run = MeasurementRunFile.load(report.run_file)
    assert run.grid is None
    assert run.metadata['generator']['kind'] == 'points'
    assert _direction_errors(report.run_file, driver).max() < 0.5


def test_unknown_or_unused_controls_are_refused(tmp_path):
    hwp, qwp, driver, session = _rig()
    with pytest.raises(RunRefused, match='not in the point sequence'):
        MeasurementRunner(sequence=points(['hwp'], [[0]]), controls=[hwp, qwp],
                          instruments={'pax1': session}, folder=tmp_path,
                          settings=_settings()).run()


def test_timed_out_window_keeps_its_partial_samples(tmp_path):
    """A window that gets 1 of 3 samples before its deadline: the sample is
    committed with the point (FAILED_PARTIAL), not lost with an exception."""
    hwp, qwp, driver, session = _rig(revolution=0.15)
    report = MeasurementRunner(
        sequence=points(['hwp', 'qwp'], [[0, 0]]), controls=[hwp, qwp],
        instruments={'pax1': session}, folder=tmp_path,
        settings=_settings(samples_per_point=3, window_deadline_s=0.4),
    ).run()
    run = MeasurementRunFile.load(report.run_file)
    assert run.status()[0] is PointStatus.FAILED_PARTIAL
    window = run.instruments['pax1'].windows[0]
    assert window['cause'] == b'timeout'
    assert 1 <= window['accepted'] < 3
    assert len(run.point_samples('pax1', 0)) == window['accepted']


def test_end_to_end_mock_run_through_the_improcess_reconstructor(tmp_path):
    """ImControl acquires, the run file is written, ImProcess analyses it, and
    every target's distance agrees with the waveplate model's ground truth."""
    from imswitch.imcommon.algorithms.polarisation import angular_distance_deg
    from imswitch.improcess.reconstructors.polarisation_map import (
        PolarisationMapReconstructor,
        analyse_run,
    )
    from imswitch.imcontrol.model.measurement.demo import DEMO_MODEL, run_demo

    report = run_demo(tmp_path, hwp_step_deg=15.0, qwp_step_deg=30.0, samples=2,
                      noise_deg=0.0)
    assert report.acquisition is AcquisitionOutcome.COMPLETE
    assert report.points_committed == 36
    run = MeasurementRunFile.load(report.run_file)
    params = PolarisationMapReconstructor.default_params()
    result = analyse_run(run, params)
    assert result.metadata['summary']['committed'] == 36

    hwp, _ = run.control_values('hwp')
    qwp, _ = run.control_values('qwp')
    truth = np.array([DEMO_MODEL.output(q, h)[1:] for q, h in zip(qwp, hwp)])
    truth /= np.linalg.norm(truth, axis=1)[:, None]
    for i, target in enumerate(result.coordinates):
        expected = float(angular_distance_deg(truth, target[None, :]).min())
        assert result.properties['distance (°)'][i] == pytest.approx(expected, abs=0.5)
        status = result.properties['status'][i]
        if expected <= params['threshold_deg'] - 0.5:
            assert status == 'pass'
        elif expected >= params['threshold_deg'] + 0.5:
            assert status == 'failed'
    assert result.export_document()['run_id'] == report.run_id


# --------------------------------------------- review of P-1 (2026-10-07)
def test_script_cancellation_still_cleans_up_and_releases(tmp_path, registry):
    """OperationCancelled is a BaseException: cleanup, run file, release and
    the finished state must still happen, then the interrupt propagates."""
    from imswitch.imcommon.model.cancellation import OperationCancelled

    hwp, qwp, driver, session = _rig()
    seq = grid([('hwp', [0, 20, 40]), ('qwp', [0, 30])])

    def progress(event):
        if event.point == 1:
            raise OperationCancelled()

    runner = MeasurementRunner(sequence=seq, controls=[hwp, qwp],
                               instruments={'pax1': session}, folder=tmp_path,
                               settings=_settings(), progress=progress)
    with pytest.raises(OperationCancelled):
        runner.run()
    assert runner.wait_finished(0)
    assert runner.acquisition is AcquisitionOutcome.STOPPED
    for key in ('waveform-output', 'instrument:pax1', hwp.resource, qwp.resource):
        assert registry.holder(key) is None
    run_files = list(tmp_path.glob('*.run.h5'))
    assert len(run_files) == 1
    assert MeasurementRunFile.load(run_files[0]).acquisition is AcquisitionOutcome.STOPPED
    assert hwp.read_position() == pytest.approx(0.0)


class _SlowPAX(MockPAXDriver):
    def __init__(self, *args, delay=0.0, gate=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay, self.gate = delay, gate

    def read(self):
        if self.gate is not None:
            self.gate.wait()
        time.sleep(self.delay)
        return super().read()


def test_a_late_read_is_never_accepted(registry):
    """Review: a 125 ms read with a 10 ms deadline returned complete=True."""
    a, b = MockRotatorControl('a'), MockRotatorControl('b')
    session = InstrumentSession('pax1', _SlowPAX(a, b, delay=0.125, has_counter=False,
                                                 has_clock=False, revolution_s=0.001))
    session.connect()
    started = time.monotonic()
    window = session.sample_window(session.open_window(), 1, 0.01)
    assert time.monotonic() - started < 0.1
    assert not window.complete and window.cause.value == 'timeout'
    assert window.samples == ()


def test_a_stuck_read_keeps_the_instrument_until_it_returns(registry):
    from imswitch.imcontrol.model.measurement.instrument import InstrumentBusy

    a, b = MockRotatorControl('a'), MockRotatorControl('b')
    gate = threading.Event()
    session = InstrumentSession('pax1', _SlowPAX(a, b, gate=gate))
    session.connect()
    window = session.sample_window(session.open_window(), 2, 0.05)
    assert window.cause.value == 'timeout' and 'still running' in window.detail
    assert session.reading()
    assert registry.in_flight('instrument:pax1')        # still owned
    with pytest.raises(InstrumentBusy):
        session.open_window()
    with pytest.raises(InstrumentBusy):
        session.set_setting('wavelength_nm', 532)
    gate.set()
    deadline = time.monotonic() + 2
    while (session.reading() or registry.in_flight('instrument:pax1')) \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not session.reading() and not registry.in_flight('instrument:pax1')
    assert session.sample_window(session.open_window(), 1, 2.0).complete


def test_a_blocking_stop_does_not_stretch_the_deadline():
    """Review: a 10 ms deadline took 168 ms because stop() blocked."""
    from imswitch.imcommon.model.measurement_run import ControlResult
    from imswitch.imcontrol.model.measurement import ControlCapabilities, RunControl

    release = threading.Event()

    class SlowStop(RunControl):
        name, resource = 'slow', 'slow-ctrl'
        capabilities = ControlCapabilities(unit='deg', can_stop=True,
                                           readback='none', acknowledges=False)

        def apply(self, value, token=None):
            release.wait(5)
            return ControlResult(self.name, value)

        def stop(self):
            time.sleep(0.3)
            release.set()

    executor = ControlExecutor()
    started = time.monotonic()
    result = executor.apply(SlowStop(), 1.0, deadline_s=0.01)
    assert time.monotonic() - started < 0.1
    assert not result.ok and 'stop requested' in result.cause
    assert executor.is_quarantined('slow-ctrl')
    assert executor.wait_released('slow-ctrl', 2.0)


def test_settings_changed_inside_a_window_end_it(registry):
    """Review: same profile id, different configuration, old boundary used."""
    hwp, qwp, driver, session = _rig()
    boundary = session.open_window()
    session.set_setting('wavelength_nm', 532)          # still profile mock-mode9
    window = session.sample_window(boundary, 1, 1.0)
    assert not window.complete
    assert window.cause.value == 'cancelled'
    assert 'configuration changed' in window.detail


def test_a_stuck_cleanup_step_is_bounded_and_keeps_its_resource(tmp_path, registry):
    """Review: cleanup callbacks ran inline, so a 155 ms restore with a 10 ms
    deadline was reported DONE (and a stuck one would hang the run)."""
    import threading

    from imswitch.imcontrol.model.measurement.controls import ControlExecutor
    from imswitch.imcontrol.model.measurement.runner import CleanupStep

    hwp, qwp, driver, session = _rig()
    release = threading.Event()
    executor = ControlExecutor()
    report = MeasurementRunner(
        sequence=grid([('hwp', [0, 10])]), controls=[hwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(cleanup_deadline_s=0.01), executor=executor,
        cleanup_steps=[CleanupStep('restore laser', 'laser:775',
                                   lambda token: release.wait(5))],
    ).run()
    assert report.acquisition is AcquisitionOutcome.COMPLETE
    assert report.cleanup is CleanupOutcome.QUARANTINED
    assert 'restore laser did not finish within 0.01 s' in report.cleanup_detail
    assert executor.is_quarantined('laser:775')
    assert registry.holder('laser:775') is not None      # still held while it runs
    release.set()
    assert executor.wait_released('laser:775', 5)
    deadline = time.monotonic() + 5
    while registry.holder('laser:775') is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.holder('laser:775') is None


def test_a_failing_cleanup_step_is_a_failed_cleanup(tmp_path):
    from imswitch.imcontrol.model.measurement.controls import ControlExecutor
    from imswitch.imcontrol.model.measurement.runner import CleanupStep

    def broken(token):
        raise OSError('DAQ write failed')
    hwp, qwp, driver, session = _rig()
    report = MeasurementRunner(
        sequence=grid([('hwp', [0])]), controls=[hwp], instruments={'pax1': session},
        folder=tmp_path, settings=_settings(), executor=ControlExecutor(),
        cleanup_steps=[CleanupStep('restore laser', 'laser:775', broken)],
    ).run()
    assert report.cleanup is CleanupOutcome.FAILED
    assert 'OSError: DAQ write failed' in report.cleanup_detail
