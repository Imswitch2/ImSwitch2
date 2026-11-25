"""Which lifecycle signal sets BeadRec's frame boundary, per scan source.

NI-DAQ scans (Base, Advanced, Point, MoNaLISA) emit ``sigScanBuilt`` after the
detectors are leased and before the first output sample, so BeadRec drains the
camera and starts its consumer there. The TriggerScope emits ``sigScanBuilt``
about a second before its scan command (the parameter upload sits in between),
so its boundary stays at ``sigScanStarted``, which that controller emits
immediately before the command. An unknown source keeps the old boundary.
"""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers._triggerscope_scan_lifecycle import (
    TriggerScopeScanLifecycleMixin,
)
from imswitch.imcontrol.controller.controllers.BeadRecController import BeadRecController


def test_the_scan_sources_declare_their_boundary():
    assert SuperScanController.beadRecBoundary == 'built'
    assert TriggerScopeScanLifecycleMixin.beadRecBoundary == 'started'


def _controller(source, running=True):
    calls = []
    ctrl = BeadRecController.__new__(BeadRecController)
    ctrl._shutdownComplete = False
    ctrl._widget = SimpleNamespace(runButton=SimpleNamespace(isChecked=lambda: running))
    ctrl._commChannel = SimpleNamespace(getActiveScanSource=lambda: source)
    ctrl._scanArmed = False
    ctrl._builtBoundaryPending = False
    ctrl._scanDiag = {}
    ctrl.updateParameters = lambda: calls.append('parameters')

    def newScan():
        calls.append('boundary')
        ctrl._scanArmed = True

    ctrl.onNewScan = newScan
    return ctrl, calls


def test_a_built_source_sets_the_boundary_at_sigScanBuilt_and_only_once():
    ctrl, calls = _controller(SimpleNamespace(beadRecBoundary='built'))

    ctrl.onScanBuilt([])
    ctrl.onScanStarted()

    assert calls == ['parameters', 'boundary']
    assert ctrl._builtBoundaryPending is False


@pytest.mark.parametrize('source', [SimpleNamespace(beadRecBoundary='started'),
                                    SimpleNamespace(), None])
def test_other_sources_keep_the_boundary_at_sigScanStarted(source):
    ctrl, calls = _controller(source)

    ctrl.onScanBuilt([])
    assert calls == []
    ctrl.onScanStarted()

    assert calls == ['parameters', 'boundary']


def test_a_repeat_iteration_sets_its_own_boundary():
    ctrl, calls = _controller(SimpleNamespace(beadRecBoundary='built'))

    for _ in range(2):
        ctrl.onScanBuilt([])
        ctrl.onScanStarted()

    assert calls == ['parameters', 'boundary'] * 2


def test_nothing_happens_while_beadrec_is_not_running():
    ctrl, calls = _controller(SimpleNamespace(beadRecBoundary='built'), running=False)

    ctrl.onScanBuilt([])

    assert calls == []


@pytest.mark.parametrize('filled, expected, surplus, late, text', [
    (30, 30, 0, 0, 'Bead reconstruction scan complete'),
    (29, 30, 0, 0, 'Bead reconstruction incomplete: 29 of 30 frames'),
    (30, 30, 1, 0, 'Bead reconstruction: 30 of 30 frames, 1 extra frame(s) ignored'),
    (30, 30, 0, 1, 'Bead reconstruction scan complete (1 arrived after the scan ended)'),
    (29, 30, 0, 2, 'Bead reconstruction incomplete: 29 of 30 frames (2 arrived after the scan ended)'),
])
def test_only_a_matching_count_is_called_complete(filled, expected, surplus, late, text):
    assert BeadRecController._completionText(filled, expected, surplus, late) == text
