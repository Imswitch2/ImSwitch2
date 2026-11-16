from types import SimpleNamespace

from imswitch.imcontrol.controller.controllers.BeadRecController import (
    BeadRecController,
)



def _stub_lifecycle(ctrl):
    ctrl._scanEndDrainTimer = SimpleNamespace(stop=lambda: None)
    ctrl.thread = SimpleNamespace(quit=lambda: None, wait=lambda: None)


def test_bead_rec_scan_gate_drains_after_scan_until_expected_frames():
    ctrl = BeadRecController.__new__(BeadRecController)
    _stub_lifecycle(ctrl)
    ctrl._scanArmed = True
    ctrl._drainingEndedScan = True
    ctrl.dims = (5, 2)
    ctrl.framesReceivedThisScan = 8
    ctrl.beadWorker = SimpleNamespace(filledPixels=lambda: 8, stop=lambda: None)
    ctrl._commChannel = SimpleNamespace(isScanRunning=lambda: False)

    assert BeadRecController._scanFramesReady(ctrl) is True

    ctrl.framesReceivedThisScan = 10
    ctrl.beadWorker = SimpleNamespace(filledPixels=lambda: 10, stop=lambda: None)

    assert BeadRecController._scanFramesReady(ctrl) is False


def test_bead_rec_scan_gate_still_uses_active_scan_running_state():
    ctrl = BeadRecController.__new__(BeadRecController)
    _stub_lifecycle(ctrl)
    ctrl._scanArmed = True
    ctrl._drainingEndedScan = False
    ctrl.dims = (5, 2)
    ctrl.framesReceivedThisScan = 10
    ctrl.beadWorker = SimpleNamespace(filledPixels=lambda: 10, stop=lambda: None)
    ctrl._commChannel = SimpleNamespace(isScanRunning=lambda: True)

    assert BeadRecController._scanFramesReady(ctrl) is True


def test_bead_rec_syncs_final_count_from_worker_snapshot():
    buffer = object()
    ctrl = BeadRecController.__new__(BeadRecController)
    _stub_lifecycle(ctrl)
    ctrl.recIm = None
    ctrl.framesReceivedThisScan = 7
    ctrl.beadWorker = SimpleNamespace(snapshot=lambda: (buffer, 10), stop=lambda: None)

    BeadRecController._syncReconstructionFromWorker(ctrl)

    assert ctrl.recIm is buffer
    assert ctrl.framesReceivedThisScan == 10


