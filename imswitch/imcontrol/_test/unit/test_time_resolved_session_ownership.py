"""Product capture is a session with one owner.

From the first external review of the Lifetime 2.0 plan: the WORKFLOW
lease keeps a detector armed but does not serialise workflows, so a second
run could reconfigure the detector under the first and its cleanup could
erase the first run's state. Now a run owns the session it opens.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
)
from imswitch.imcontrol.model.workflows import (
    TimeResolvedScanWorkflow,
    TimeResolvedWorkflowParams,
)
from imswitch.imcontrol.model.workflows.mock_facade import build_mock_facade

pytestmark = pytest.mark.nohardware


# --------------------------------------------------------------------------- #
# The detector                                                                 #
# --------------------------------------------------------------------------- #


class _Signal:
    def connect(self, slot):
        pass


class _Nidaq:
    isSimulated = True
    sigScanBuilt = _Signal()
    sigScanStarted = _Signal()
    sigScanDone = _Signal()


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _detector():
    return SwabianTimeTaggerManager(
        _DetectorInfo({"click_channel": 1, "start_channel": 2, "line_channel": 3}),
        "FLIM", _Nidaq(),
    )


def test_a_second_owner_is_refused_while_a_session_is_open():
    det = _detector()
    assert det.configureTimeResolvedProducts(TimeResolvedScanConfig(), owner="A") == "A"
    assert det.timeResolvedSessionOwner() == "A"
    with pytest.raises(RuntimeError, match="owned by another run \\(A\\)"):
        det.configureTimeResolvedProducts(TimeResolvedScanConfig(), owner="B")
    with pytest.raises(RuntimeError, match="owned by another run"):
        det.configureTimeResolvedProducts(TimeResolvedScanConfig())  # no token either
    # The owner may reconfigure its own session.
    det.configureTimeResolvedProducts(
        TimeResolvedScanConfig(gates=(GateSpec("g", 1.0, 2.0),)), owner="A"
    )
    assert det._tr_config.gates[0].name == "g"


def test_only_the_owner_can_clear_an_owned_session():
    det = _detector()
    det.configureTimeResolvedProducts(TimeResolvedScanConfig(capture_cube=True), owner="A")
    det.clearTimeResolvedProducts(owner="B")
    det.clearTimeResolvedProducts()
    assert det.timeResolvedSessionOwner() == "A", "B's and a tokenless clear are no-ops"
    assert det._tr_config.capture_cube is True
    det.clearTimeResolvedProducts(owner="A")
    assert det.timeResolvedSessionOwner() is None
    assert det._tr_enabled is False


def test_unowned_sessions_keep_the_single_consumer_behaviour():
    det = _detector()
    det.configureTimeResolvedProducts(TimeResolvedScanConfig())
    assert det.timeResolvedSessionOwner() is None
    det.configureTimeResolvedProducts(TimeResolvedScanConfig(capture_cube=True))
    assert det._tr_config.capture_cube is True, "an unowned session can be re-armed"
    det.clearTimeResolvedProducts()
    assert det._tr_enabled is False


def test_wait_for_final_checks_the_owner():
    det = _detector()
    det.configureTimeResolvedProducts(TimeResolvedScanConfig(), owner="A")
    with pytest.raises(RuntimeError, match="owned by another run"):
        det.waitForFinalTimeResolvedProducts(timeout_s=0.01, owner="B")
    with pytest.raises(TimeoutError):
        det.waitForFinalTimeResolvedProducts(timeout_s=0.01, owner="A")


# --------------------------------------------------------------------------- #
# Two workflows overlapping                                                    #
# --------------------------------------------------------------------------- #


def _products():
    cube = np.ones((2, 2, 4), dtype=np.float32)
    return TimeResolvedScanProducts(
        cube_counts=cube,
        cube_axes=("y", "x", "tcspc_bin"),
        t_axis_ns=np.array([0.5, 1.5, 2.5, 3.5], dtype=np.float32),
        intensity=cube.sum(axis=-1),
        lifetime_ns=np.full((2, 2), 2.0, dtype=np.float32),
        gate_images={},
        decay_counts=cube.sum(axis=(0, 1)),
        global_tau_ns=2.0,
        metadata={"backend": "mock"},
        is_final=True,
    )


def test_overlapping_workflows_do_not_disturb_each_other(tmp_path):
    facade = build_mock_facade()
    facade.time_resolved.set_canned_products(_products())
    params = TimeResolvedWorkflowParams(
        save_folder=tmp_path, save_h5=False, save_npz=False, save_tiff=False,
        measurement_name="first",
    )
    second_params = TimeResolvedWorkflowParams(
        save_folder=tmp_path, save_h5=False, save_npz=False, save_tiff=False,
        measurement_name="second",
    )
    outcome = {}

    def _first_acquisition():
        # While the first run is between configure and wait, a second run
        # starts: it must be refused at configure, before any scan, and its
        # cleanup must leave the first run's session in place.
        with pytest.raises(RuntimeError, match="owned by another run"):
            TimeResolvedScanWorkflow(facade, second_params).run(acquisition=lambda: None)
        outcome["owner_during_first"] = facade.time_resolved.session_owner()

    result = TimeResolvedScanWorkflow(facade, params).run(acquisition=_first_acquisition)

    assert result.products.global_tau_ns == 2.0
    assert outcome["owner_during_first"].startswith("first-")
    assert facade.time_resolved.session_owner() is None, "the first run cleared its own session"
    # The second run succeeds once the first is done.
    TimeResolvedScanWorkflow(facade, second_params).run(acquisition=lambda: None)
    assert facade.time_resolved.session_owner() is None


def test_runs_racing_for_the_session_get_exactly_one_owner():
    """The check and the claim share one lock block: of N runs that call
    ``configure`` at the same moment, one owns the session and the others
    are refused -- never two owners."""
    import threading

    det = _detector()
    n = 8
    barrier = threading.Barrier(n)
    outcomes = []
    lock = threading.Lock()

    def run(index):
        barrier.wait()
        try:
            token = det.configureTimeResolvedProducts(
                TimeResolvedScanConfig(), owner=f"run-{index}"
            )
            result = ("owner", token)
        except RuntimeError as error:
            result = ("refused", str(error))
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)
    owners = [o for o in outcomes if o[0] == "owner"]
    assert len(owners) == 1, outcomes
    assert det.timeResolvedSessionOwner() == owners[0][1]
    det.clearTimeResolvedProducts(owners[0][1])
    assert det.timeResolvedSessionOwner() is None
