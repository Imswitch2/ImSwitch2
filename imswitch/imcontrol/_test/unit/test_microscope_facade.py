"""Tests for the workflow facade and its mock variant.

The mock facade is the contract Phase-1 workflow ports depend on; these
tests pin its call-recording shape so a regression in the mock would
surface here rather than in a workflow test.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.workflows import (
    MicroscopeFacade,
    RotatorPresets,
    build_mock_facade,
)


# ---------------------------------------------------------------------------
# MockMicroscopeFacade structural contract
# ---------------------------------------------------------------------------


def test_build_mock_facade_returns_populated_facade():
    f = build_mock_facade()
    assert isinstance(f, MicroscopeFacade)
    assert f.laser_con is not None
    assert f.cam is not None
    assert f.trig is not None
    assert f.stage_con is not None
    assert f.z_stage_con is not None
    assert f.rotator_hwp is not None
    assert f.rotator_qwp is not None
    assert f.time_resolved is not None
    assert f.scan is not None
    assert f.calls == []


def test_laser_con_records_calls():
    f = build_mock_facade()
    f.laser_con.set_constant_power(["488"], [50.0])
    f.laser_con.set_triggered_mode(["488"], [25.0])
    f.laser_con.set_modulation_mode(["488"])
    f.laser_con.laser_off(["488"])

    assert f.call_names() == [
        "laser_con.set_constant_power",
        "laser_con.set_triggered_mode",
        "laser_con.set_modulation_mode",
        "laser_con.laser_off",
    ]
    assert f.calls[0] == ("laser_con.set_constant_power", (["488"], [50.0]), {})


def test_set_modulation_mode_accepts_none():
    f = build_mock_facade()
    f.laser_con.set_modulation_mode(None)
    assert f.calls == [("laser_con.set_modulation_mode", (None,), {})]


def test_cam_get_data_returns_canned_array():
    f = build_mock_facade()
    arr = np.zeros((4, 8, 8), dtype=np.uint16)
    f.cam.set_canned_data(arr)

    f.cam.prepare_acquisition(4)
    f.cam.start_acquisition()
    result = f.cam.get_data()
    f.cam.stop_acquisition()

    assert result is arr
    assert f.call_names() == [
        "cam.prepare_acquisition",
        "cam.start_acquisition",
        "cam.get_data",
        "cam.stop_acquisition",
    ]


def test_cam_wait_for_frame_true_when_canned():
    f = build_mock_facade()
    f.cam.set_canned_data(np.zeros((1, 2, 2), dtype=np.uint16))
    assert f.cam.wait_for_frame(timeout_s=0.01) is True


def test_cam_wait_for_frame_false_when_empty():
    f = build_mock_facade()
    assert f.cam.wait_for_frame(timeout_s=0.01) is False


def test_cam_expo_is_mutable():
    f = build_mock_facade()
    f.cam.expo = 10000.0
    assert f.cam.expo == 10000.0


def test_trig_snap_trigger_records_kwargs():
    f = build_mock_facade()
    f.trig.snap_trigger(laser_pin=8, camera_pin=11, exposure_us=5000)
    assert f.calls == [
        ("trig.snap_trigger", (), {"laser_pin": 8, "camera_pin": 11, "exposure_us": 5000}),
    ]


def test_stage_con_move_and_position():
    f = build_mock_facade()
    f.stage_con.move_to(1560.0, 0.0)
    assert f.stage_con.get_position() == (1560.0, 0.0)
    assert f.call_names() == ["stage_con.move_to", "stage_con.get_position"]


def test_z_stage_con_default_range_and_round_trip():
    f = build_mock_facade()
    assert f.z_stage_con.pos_range_um == (0.0, 100.0)
    f.z_stage_con.activate_ext_control()
    f.z_stage_con.set_pos_um(42.0)
    assert f.z_stage_con.read_pos_um() == 42.0


# ---------------------------------------------------------------------------
# Rotator H/V + chained moves
# ---------------------------------------------------------------------------


def test_rotator_chained_move_calls_followup():
    f = build_mock_facade()
    f.rotator_qwp.chained_move_to_h(f.rotator_hwp.move_to_h)
    f.rotator_qwp.chained_move_to_v(f.rotator_hwp.move_to_v)

    assert f.call_names() == [
        "rotator_qwp.move_to_h",
        "rotator_hwp.move_to_h",
        "rotator_qwp.move_to_v",
        "rotator_hwp.move_to_v",
    ]


def test_rotator_presets_drive_position():
    from imswitch.imcontrol.model.workflows.mock_facade import _MockRotator, _Recorder
    rec = _Recorder()
    rot = _MockRotator(rec, "rotator_test", RotatorPresets(h_deg=12.5, v_deg=102.5))
    rot.move_to_h()
    assert rot.position() == 12.5
    rot.move_to_v()
    assert rot.position() == 102.5


# ---------------------------------------------------------------------------
# Real-adapter input validation (no hardware needed)
# ---------------------------------------------------------------------------


def test_laser_con_validates_name_power_length():
    from imswitch.imcontrol.model.workflows.facade import LaserConFacade

    facade = LaserConFacade({"488": object(), "405": object()})
    with pytest.raises(ValueError, match="length mismatch"):
        facade.set_constant_power(["488"], [10.0, 20.0])


def test_laser_con_unknown_name_raises():
    from imswitch.imcontrol.model.workflows.facade import LaserConFacade

    facade = LaserConFacade({"488": object()})
    with pytest.raises(KeyError, match="561"):
        facade.laser_off(["561"])


def test_trig_facade_snap_without_pulsegen_raises():
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    trig = TrigFacade(pulsegen=None)
    assert trig.connected is False
    with pytest.raises(RuntimeError, match="neither WFS Teensy nor pulse generator"):
        trig.snap_trigger(laser_pin=1, camera_pin=2, exposure_us=100)


def test_time_resolved_mock_records_and_returns_canned_products():
    from imswitch.imcontrol.model.timeresolved import (
        GateSpec,
        TimeResolvedScanConfig,
        TimeResolvedScanProducts,
    )

    f = build_mock_facade()
    products = TimeResolvedScanProducts(
        cube_counts=np.ones((2, 3, 4), dtype=np.float32),
        cube_axes=("y", "x", "tcspc_bin"),
        t_axis_ns=np.arange(4, dtype=np.float32),
        intensity=np.ones((2, 3), dtype=np.float32),
        lifetime_ns=np.ones((2, 3), dtype=np.float32) * 2,
        gate_images={"late": np.ones((2, 3), dtype=np.float32)},
        decay_counts=np.ones(4, dtype=np.float32),
        global_tau_ns=2.0,
        metadata={"backend": "mock"},
        is_final=True,
    )
    f.time_resolved.set_canned_products(products)
    config = TimeResolvedScanConfig(gates=(GateSpec("late", 1.0, 3.0),))

    f.time_resolved.configure(config)
    result = f.time_resolved.wait_for_final(timeout_s=0.1)

    assert result.metadata["backend"] == "mock"
    assert f.call_names() == [
        "time_resolved.configure",
        "time_resolved.wait_for_final",
    ]


def test_time_resolved_facade_rejects_missing_contract():
    from imswitch.imcontrol.model.workflows.facade import TimeResolvedDetectorFacade

    with pytest.raises(TypeError, match="time-resolved contract"):
        TimeResolvedDetectorFacade(object())


def test_time_resolved_facade_workflow_lease_is_scoped_and_released():
    from imswitch.imcontrol.model.managers._acquisition_leases import (
        LeasePurpose,
    )
    from imswitch.imcontrol.model.workflows.facade import (
        TimeResolvedDetectorFacade,
    )

    class Detector:
        timeResolvedCapabilities = lambda self: {}
        configureTimeResolvedProducts = lambda self, config: None
        waitForFinalTimeResolvedProducts = lambda self, timeout: None
        getLastTimeResolvedProducts = lambda self, copy=True: None
        clearTimeResolvedProducts = lambda self: None

    class Manager:
        def __init__(self):
            self.events = []

        def acquire(self, names, purpose):
            self.events.append(('acquire', tuple(names), purpose))
            return 'lease'

        def release(self, handle):
            self.events.append(('release', handle))

    manager = Manager()
    facade = TimeResolvedDetectorFacade(
        Detector(), detectorsManager=manager, detectorName='TT'
    )

    with facade.acquisition_lease():
        manager.events.append(('work',))

    assert manager.events == [
        ('acquire', ('TT',), LeasePurpose.WORKFLOW),
        ('work',),
        ('release', 'lease'),
    ]


def test_mock_scan_records_default_run_once():
    f = build_mock_facade()

    f.scan.run_once(timeout_s=0.25)

    assert f.calls == [
        (
            "scan.run_once",
            (),
            {
                "recalculate_signals": True,
                "is_non_final_part_of_sequence": False,
                "wait": True,
                "timeout_s": 0.25,
                "notify_starting": True,
            },
        )
    ]


def test_scan_workflow_facade_runs_and_waits_for_done_signal():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            for slot in list(self.slots):
                slot()

    class ScanWorkflow:
        def __init__(self, signal):
            self.signal = signal
            self.calls = []

        def notify_scan_starting(self):
            self.calls.append(("notify_scan_starting",))

        def run_scan(self, recalculate_signals, is_non_final_part_of_sequence):
            self.calls.append(
                (
                    "run_scan",
                    recalculate_signals,
                    is_non_final_part_of_sequence,
                )
            )
            self.signal.emit()

    signal = Signal()
    scan_workflow = ScanWorkflow(signal)
    facade = ScanWorkflowFacade(scan_workflow, signal)

    facade.run_once(timeout_s=0.1)

    assert scan_workflow.calls == [
        ("notify_scan_starting",),
        ("run_scan", True, False),
    ]
    assert signal.slots == []


def test_scan_workflow_facade_rejection_is_synchronous_and_pairs_start():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

    class Rejected:
        handled = True
        accepted = False
        rejectionMessage = "scanner is already reserved"
        reports = ()

    class ScanWorkflow:
        def __init__(self):
            self.calls = []

        def notify_scan_starting(self):
            self.calls.append("starting")

        def notify_scan_ended(self):
            self.calls.append("ended")

        def run_scan(self, *_args):
            self.calls.append("run")
            return Rejected()

    done_signal = Signal()
    scan_workflow = ScanWorkflow()
    facade = ScanWorkflowFacade(scan_workflow, done_signal)

    with pytest.raises(
        RuntimeError, match="Scan request rejected: scanner is already reserved"
    ):
        facade.run_once(timeout_s=60)

    assert scan_workflow.calls == ["starting", "run", "ended"]
    assert done_signal.slots == []


def test_scan_workflow_facade_legacy_fallback_publishes_start_then_runs():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class ScanWorkflow:
        def __init__(self):
            self.calls = []

        def prepare_scan_start(self):
            self.calls.append('obsolete-split-preflight')

        def notify_scan_starting(self):
            self.calls.append('starting')

        def run_scan(self, *_args):
            self.calls.append('run')

    workflow = ScanWorkflow()

    ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.calls == ['starting', 'run']


def test_scan_workflow_facade_atomic_refusal_does_not_pair_unpublished_start():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Rejected:
        handled = True
        accepted = False
        rejectionMessage = 'scanner is already reserved'
        reports = ()
        startingPublished = False
        endingPublished = False

    class ScanWorkflow:
        def __init__(self):
            self.calls = []

        def run_scan_prepared(self, *_args):
            self.calls.append('prepared-run')
            return Rejected()

        def prepare_scan_start(self):
            self.calls.append('split-preflight')

        def notify_scan_starting(self):
            self.calls.append('legacy-starting')

        def notify_scan_ended(self):
            self.calls.append('ended')

        def run_scan(self, *_args):
            self.calls.append('legacy-run')

    workflow = ScanWorkflow()

    with pytest.raises(RuntimeError, match='scanner is already reserved'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.calls == ['prepared-run']


def test_scan_workflow_facade_does_not_repair_atomic_ui_end():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Rejected:
        handled = True
        accepted = False
        rejectionMessage = 'arm refused after pre-arm'
        reports = ()
        startingPublished = True
        endingPublished = True

    class ScanWorkflow:
        def __init__(self):
            self.calls = []

        def run_scan_prepared(self, *_args):
            self.calls.append('prepared-run')
            return Rejected()

        def notify_scan_ended(self):
            self.calls.append('duplicate-worker-end')

        def run_scan(self, *_args):
            self.calls.append('legacy-run')

    workflow = ScanWorkflow()

    with pytest.raises(RuntimeError, match='arm refused after pre-arm'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.calls == ['prepared-run']


def test_scan_workflow_facade_does_not_double_end_failed_claimed_run():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []
            self.emissions = 0

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            self.emissions += 1
            for slot in list(self.slots):
                slot()

    class Rejected:
        handled = True
        accepted = False
        rejectionMessage = "scan build failed"
        reports = ()

    class ScanWorkflow:
        def __init__(self):
            self._comm_channel = type(
                "CommChannel", (), {"sigScanEnded": Signal()}
            )()
            self.notify_ended_calls = 0

        def notify_scan_starting(self):
            pass

        def notify_scan_ended(self):
            self.notify_ended_calls += 1
            self._comm_channel.sigScanEnded.emit()

        def run_scan(self, *_args):
            # A controller can reserve the caller-published lifecycle, fail
            # while building, and publish its terminal signal before reporting
            # the request as rejected.
            self._comm_channel.sigScanEnded.emit()
            return Rejected()

    scan_workflow = ScanWorkflow()
    facade = ScanWorkflowFacade(scan_workflow)

    with pytest.raises(RuntimeError, match="scan build failed"):
        facade.run_once(wait=False)

    assert scan_workflow._comm_channel.sigScanEnded.emissions == 1
    assert scan_workflow.notify_ended_calls == 0
    assert scan_workflow._comm_channel.sigScanEnded.slots == []


def test_scan_workflow_facade_infers_deferred_controller_end():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = type(
        "Controller",
        (),
        {
            "_externalScanRequestAccepted": True,
            "_scanRunToken": None,
            "_scanRunStartingPublished": False,
        },
    )()

    class Rejected:
        handled = True
        accepted = False
        rejectionMessage = "participant teardown is pending"
        reports = ((owner, False, rejectionMessage),)

    class ScanWorkflow:
        def __init__(self):
            self.notify_ended_calls = 0

        def notify_scan_starting(self):
            pass

        def notify_scan_ended(self):
            self.notify_ended_calls += 1

        def run_scan(self, *_args):
            return Rejected()

    scan_workflow = ScanWorkflow()

    with pytest.raises(RuntimeError, match="participant teardown is pending"):
        ScanWorkflowFacade(scan_workflow).run_once(wait=False)

    # The controller cleared its local token/start state only after accepting
    # responsibility for the terminal publication (possibly deferred behind
    # detector teardown), so the facade must not publish a second end.
    assert scan_workflow.notify_ended_calls == 0


def test_scan_workflow_facade_preserves_unhandled_legacy_request():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Unhandled:
        handled = False
        accepted = False
        rejectionMessage = "legacy receiver did not acknowledge"

    class ScanWorkflow:
        def __init__(self):
            self.ended = 0

        def notify_scan_starting(self):
            pass

        def notify_scan_ended(self):
            self.ended += 1

        def run_scan(self, *_args):
            return Unhandled()

    scan_workflow = ScanWorkflow()
    facade = ScanWorkflowFacade(scan_workflow)

    facade.run_once(wait=False)

    assert scan_workflow.ended == 0


def test_scan_workflow_facade_wakes_when_legacy_scan_ends_without_done():
    import threading

    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            for slot in list(self.slots):
                slot()

    class Unhandled:
        handled = False
        accepted = False

    ended = Signal()

    class ScanWorkflow:
        def __init__(self):
            self._comm_channel = type(
                'CommChannel', (), {'sigScanEnded': ended}
            )()

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            threading.Timer(0.01, ended.emit).start()
            return Unhandled()

    with pytest.raises(RuntimeError, match='ended before'):
        ScanWorkflowFacade(
            ScanWorkflow(), Signal()
        ).run_once(timeout_s=0.2)


def test_legacy_done_wakes_blocked_script_worker(qtbot):
    import threading

    from qtpy import QtCore

    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Emitter(QtCore.QObject):
        done = QtCore.Signal()

    class Unhandled:
        handled = False
        accepted = False

    ready = threading.Event()
    errors = []

    class ScanWorkflow:
        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            ready.set()
            return Unhandled()

    emitter = Emitter()
    facade = ScanWorkflowFacade(ScanWorkflow(), emitter.done)

    def run():
        try:
            facade.run_once(timeout_s=0.5)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    qtbot.waitUntil(ready.is_set, timeout=1000)
    emitter.done.emit()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=1000)
    thread.join(timeout=0.1)

    assert errors == []


def test_scan_workflow_facade_ignores_stale_done_for_exact_request():
    import threading

    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            for slot in list(self.slots):
                slot()

    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)
    stale_done = Signal()

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    class ScanWorkflow:
        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            # This terminal belongs to an older run and must not complete the
            # newly accepted token-scoped request.
            stale_done.emit()
            threading.Timer(
                0.01,
                lambda: completion.resolve(
                    run_token, False, 'exact scan request failed'
                ),
            ).start()
            return Accepted()

    with pytest.raises(RuntimeError, match='exact scan request failed'):
        ScanWorkflowFacade(
            ScanWorkflow(), stale_done
        ).run_once(timeout_s=0.2)


def test_scan_workflow_facade_rejects_handled_acceptance_without_terminal():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    run_token = object()

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ()

    class ScanWorkflow:
        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

    with pytest.raises(RuntimeError, match='exact request terminal'):
        ScanWorkflowFacade(ScanWorkflow()).run_once(wait=False)


def test_malformed_accepted_request_aborts_only_its_reported_owner():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    unrelated = object()
    run_token = object()

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''), (unrelated, False, 'not selected'))
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ()

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source):
            self.aborted.append(source)

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='exact request terminal'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.aborted == [owner]


def test_non_waitable_exact_terminal_triggers_targeted_abort():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    run_token = object()
    malformed_completion = type(
        'MalformedCompletion',
        (),
        {'owner': owner, 'runToken': run_token},
    )()

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = (
            (owner, run_token, malformed_completion),
        )

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source):
            self.aborted.append(source)

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='non-waitable'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.aborted == [owner]


def test_foreign_accepted_token_entry_aborts_every_reported_identity():
    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    foreign_owner = object()
    run_token = object()
    foreign_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = (
            (owner, run_token),
            (foreign_owner, foreign_token),
        )
        acceptedCompletions = ((owner, run_token, completion),)

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source, token=None):
            self.aborted.append((source, token))

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='accepted run tokens'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.aborted == [
        (owner, run_token),
        (foreign_owner, foreign_token),
    ]


def test_reported_token_must_match_unresolved_owner_reservation():
    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    reported_token = object()
    active_token = object()

    class Owner:
        pass

    owner = Owner()
    owner._scanCoordinator = type(
        'Coordinator',
        (),
        {'runForOwner': lambda _self, _owner: active_token},
    )()
    completion = ScanRequestCompletion(owner)
    completion.bind(reported_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, reported_token),)
        acceptedCompletions = (
            (owner, reported_token, completion),
        )

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source, token=None):
            self.aborted.append((source, token))

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='active reservation'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.aborted == [(owner, reported_token)]


def test_broken_acceptance_envelope_triggers_targeted_abort():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    run_token = object()

    class BrokenResult:
        @property
        def handled(self):
            raise RuntimeError('broken handled property')

        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ()

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return BrokenResult()

        def abort_scan_from(self, source, token=None):
            self.aborted.append((source, token))

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='broken handled property'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.aborted == [(owner, run_token)]


def test_scan_workflow_facade_without_done_signal_preserves_fire_and_return():
    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            for slot in list(self.slots):
                slot()

    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    class ScanWorkflow:
        def __init__(self):
            self._comm_channel = type(
                'CommChannel', (), {'sigScanEnded': Signal()}
            )()

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            self._comm_channel.sigScanEnded.emit()
            return Accepted()

    workflow = ScanWorkflow()
    ScanWorkflowFacade(workflow).run_once(timeout_s=0)

    assert workflow._comm_channel.sigScanEnded.slots == []


def test_scan_workflow_facade_pairs_start_when_dispatch_raises():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class ScanWorkflow:
        def __init__(self):
            self.started = 0
            self.ended = 0

        def notify_scan_starting(self):
            self.started += 1

        def notify_scan_ended(self):
            self.ended += 1

        def run_scan(self, *_args):
            raise RuntimeError('dispatch failed')

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='dispatch failed'):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert workflow.started == 1
    assert workflow.ended == 1


def test_raised_accepted_dispatch_waits_for_exact_terminal_without_early_end():
    import threading

    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def disconnect(self, slot):
            self.slots.remove(slot)

        def emit(self):
            for slot in list(self.slots):
                slot()

    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, 'arm failed'),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    dispatch_error = RuntimeError('dispatch raised after acceptance')
    dispatch_error.scanRequestResult = Accepted()

    class ScanWorkflow:
        def __init__(self):
            self._comm_channel = type(
                'CommChannel', (), {'sigScanEnded': Signal()}
            )()
            self.notify_ended_calls = 0
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def notify_scan_ended(self):
            self.notify_ended_calls += 1
            self._comm_channel.sigScanEnded.emit()

        def run_scan(self, *_args):
            threading.Timer(
                0.01,
                lambda: completion.resolve(
                    run_token, False, 'arm failed'
                ),
            ).start()
            raise dispatch_error

        def abort_scan_from(self, source):
            self.aborted.append(source)

    workflow = ScanWorkflow()
    with pytest.raises(
        RuntimeError, match='dispatch raised after acceptance'
    ):
        ScanWorkflowFacade(
            workflow, Signal()
        ).run_once(timeout_s=0.2)

    assert completion.wait(timeout=0) is True
    assert workflow.notify_ended_calls == 0
    assert workflow.aborted == []


def test_raised_accepted_dispatch_without_wait_never_synthesizes_end():
    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, 'arm failed'),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    dispatch_error = RuntimeError('dispatch raised after acceptance')
    dispatch_error.scanRequestResult = Accepted()

    class ScanWorkflow:
        def __init__(self):
            self.notify_ended_calls = 0

        def notify_scan_starting(self):
            pass

        def notify_scan_ended(self):
            self.notify_ended_calls += 1

        def run_scan(self, *_args):
            raise dispatch_error

    workflow = ScanWorkflow()
    with pytest.raises(
        RuntimeError, match='dispatch raised after acceptance'
    ):
        ScanWorkflowFacade(workflow).run_once(wait=False)

    assert completion.wait(timeout=0) is False
    assert workflow.notify_ended_calls == 0


def test_exceptional_exact_wait_triggers_targeted_abort():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def connect(self, _slot):
            pass

        def disconnect(self, _slot):
            pass

    owner = object()
    run_token = object()

    class BrokenCompletion:
        runToken = run_token

        def __init__(self):
            self.owner = owner

        def wait(self, timeout=None):
            raise RuntimeError('broken exact wait')

    completion = BrokenCompletion()

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source):
            self.aborted.append(source)

    workflow = ScanWorkflow()
    with pytest.raises(RuntimeError, match='broken exact wait'):
        ScanWorkflowFacade(
            workflow, Signal()
        ).run_once(timeout_s=0.1)

    assert workflow.aborted == [owner]


def test_exact_wait_timeout_triggers_targeted_abort():
    from imswitch.imcontrol.controller.WorkflowServices import (
        ScanRequestCompletion,
    )
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class Signal:
        def connect(self, _slot):
            pass

        def disconnect(self, _slot):
            pass

    owner = object()
    run_token = object()
    completion = ScanRequestCompletion(owner)
    completion.bind(run_token)

    class Accepted:
        handled = True
        accepted = True
        reports = ((owner, True, ''),)
        acceptedTokens = ((owner, run_token),)
        acceptedCompletions = ((owner, run_token, completion),)

    class ScanWorkflow:
        def __init__(self):
            self.aborted = []

        def notify_scan_starting(self):
            pass

        def run_scan(self, *_args):
            return Accepted()

        def abort_scan_from(self, source):
            self.aborted.append(source)

    workflow = ScanWorkflow()
    with pytest.raises(TimeoutError, match='Timed out'):
        ScanWorkflowFacade(
            workflow, Signal()
        ).run_once(timeout_s=0)

    assert workflow.aborted == [owner]


def test_scan_workflow_facade_allows_continuation_without_new_start():
    from imswitch.imcontrol.model.workflows.facade import ScanWorkflowFacade

    class ScanWorkflow:
        def __init__(self):
            self.started = 0
            self.runs = 0

        def notify_scan_starting(self):
            self.started += 1

        def run_scan(self, *_args):
            self.runs += 1

    workflow = ScanWorkflow()
    ScanWorkflowFacade(workflow).run_once(
        wait=False, notify_starting=False
    )

    assert workflow.started == 0
    assert workflow.runs == 1


def test_trig_facade_snap_uses_existing_legacy_teensy_driver():
    """Board pin numbers should pass through an already-open WFS-compatible Teensy."""
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    class Driver:
        def __init__(self):
            self.calls = []

        def _send_recv(self, cmd, terminal, timeout):
            self.calls.append((cmd, terminal, timeout))
            return "DONE"

    class Pulsegen:
        connected = True

        def __init__(self):
            self.driver = Driver()

    pulsegen = Pulsegen()
    trig = TrigFacade(pulsegen=pulsegen)

    trig.snap_trigger(laser_pin=8, camera_pin=11, exposure_us=50000)

    assert pulsegen.driver.calls[0][0] == "Snap,8,11,50000\n"


def test_trig_facade_command_pulse_scheme_math():
    """The pure-numpy command() builds the WFS pulse-scheme arrays without
    needing any hardware — verifies the timing math against a known case."""
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    trig = TrigFacade()  # no pulsegen, no WFS — command() doesn't need either
    tw, l1, l2, l3 = trig.command(
        start488="0", start405=25_000, start_camera=0,
        width488="20000", width405=20_000, width_camera=50_000,
        dwelltime=50_000,
    )
    # All arrays padded to 16
    assert tw.shape == (16,) and l1.shape == (16,) and l2.shape == (16,) and l3.shape == (16,)
    # 488 fires in the first window; 405 fires in the third window
    assert int(l1[0]) == 1 and int(l1[1]) == 0
    assert int(l2[0]) == 0 and int(l2[2]) == 1
    # Camera HIGH throughout the 50 ms exposure (across the four active windows)
    assert all(int(l3[i]) == 1 for i in range(4))
    # Sum of time windows equals the total dwell time
    assert int(tw.sum()) == 50_000


def test_trig_facade_sendsignal_requires_wfs_serial():
    """Sendsignal must raise if no WFS Teensy serial has been opened."""
    import numpy as np
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    trig = TrigFacade()
    with pytest.raises(RuntimeError, match="WFS Teensy serial is not connected"):
        trig.Sendsignal(
            pin488=8, pin405=6, camerapin=11,
            delay_time=0, frame_number=1,
            tWindowM=np.zeros(16), laserMod_1=np.zeros(16),
            laserMod_2=np.zeros(16), laserMod_3=np.zeros(16),
        )


def test_trig_facade_sendsignal_uses_existing_legacy_teensy_driver():
    """Recording workflow can reuse the Teensy manager's open serial driver."""
    from imswitch.imcontrol.model.workflows.facade import TrigFacade

    class Driver:
        def __init__(self):
            self.calls = []

        def _send_recv(self, cmd, terminal, timeout):
            self.calls.append((cmd, terminal, timeout))
            return "DONE"

    class Pulsegen:
        connected = True

        def __init__(self):
            self.driver = Driver()

    pulsegen = Pulsegen()
    trig = TrigFacade(pulsegen=pulsegen)

    trig.Sendsignal(
        pin488=8, pin405=6, camerapin=11,
        delay_time=0, frame_number=1,
        tWindowM=np.zeros(16), laserMod_1=np.zeros(16),
        laserMod_2=np.zeros(16), laserMod_3=np.zeros(16),
    )

    assert pulsegen.driver.calls[0][0].startswith("Parameters,")
    assert ",8," in pulsegen.driver.calls[0][0]
    assert ",6," in pulsegen.driver.calls[0][0]
    assert ",11," in pulsegen.driver.calls[0][0]


def test_build_facade_reads_rotator_presets_from_manager_properties():
    """Workflow H/V positions can come from the setup JSON rotator config."""
    from imswitch.imcontrol.model.workflows.facade import build_facade_from_master

    class Info:
        managerProperties = {"workflowPresets": {"h_deg": 12.5, "v_deg": 102.5}}

    class Rotator:
        _rotatorInfo = Info()

        def __init__(self):
            self.moves = []
            self.position = 0.0

        def move_abs(self, pos):
            self.moves.append(pos)
            self.position = pos

    class Rotators:
        def __init__(self):
            self.hwp = Rotator()
            self.qwp = Rotator()

        def __getitem__(self, name):
            return {"HWP": self.hwp, "QWP": self.qwp}[name]

    class Master:
        rotatorsManager = Rotators()
        pulseGeneratorManager = None

    facade = build_facade_from_master(Master(), hwp_name="HWP", qwp_name="QWP")

    assert facade.rotator_hwp.presets == RotatorPresets(h_deg=12.5, v_deg=102.5)
    assert facade.rotator_qwp.presets == RotatorPresets(h_deg=12.5, v_deg=102.5)
