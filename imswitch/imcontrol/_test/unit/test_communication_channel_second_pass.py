"""CommunicationChannel second cleanup pass: bug fixes and private signals."""
from types import SimpleNamespace
from unittest.mock import Mock

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.controllers.EtSnoutyController import (
    EtSnoutyController,
)


def _channel(controllers=None):
    main = SimpleNamespace(
        _moduleCommChannel=Mock(),
        controllers=controllers if controllers is not None else {},
    )
    return CommunicationChannel(main, SimpleNamespace())


def test_acquire_image_works_outside_a_script(qtbot):
    # `output` used to exist only after runScript(), so a direct call from the
    # API raised AttributeError.
    frames = iter(['first', 'second'])
    channel = _channel({'View': SimpleNamespace(get_image=lambda _d: next(frames))})

    channel.acquireImage()
    channel.acquireImage()

    # Nothing clears it outside a script, so it must not accumulate.
    assert channel.output == ['second']


def test_acquire_image_collects_every_frame_inside_a_script(qtbot):
    frames = iter(['first', 'second'])
    channel = _channel({'View': SimpleNamespace(get_image=lambda _d: next(frames))})

    channel.runScript('pass')
    channel.acquireImage()
    channel.acquireImage()

    assert channel.output == ['first', 'second']


def test_scan_source_names_and_resolution_share_one_helper(qtbot):
    class Recording:
        def runScanExternal(self, *a): pass
        def abortScan(self): pass
        def getNumScanPositions(self): pass
        def getNumCamTTL(self): pass

    class Runner:
        def runScanExternal(self, *a): pass
        def abortScan(self): pass

    recording, runner = Recording(), Runner()
    channel = _channel({'A': recording, 'B': runner})

    assert channel.getRecordingScanSourceNames() == ['A']
    assert channel.getScanSourceNames() == ['A', 'B']
    assert channel.getRecordingScanSource() is recording
    assert channel.getScanSource('B') is runner


def test_etsnouty_widefield_clock_is_private_to_the_controller(qtbot):
    controller = EtSnoutyController.__new__(EtSnoutyController)
    ticks = []

    controller._clockSignal.connect(lambda: ticks.append(1))
    controller._clockSignal.emit()
    assert ticks == [1]

    # Same signal object on every access; a second controller is independent.
    other = EtSnoutyController.__new__(EtSnoutyController)
    other._clockSignal.emit()
    assert ticks == [1]

    controller._clockSignal.disconnect()
    controller._clockSignal.emit()
    assert ticks == [1]


def test_plsr_multicolor_controllers_expose_the_targeted_entry_point():
    # ScanWorkflowService.run_plsr_multicolor_scan() replaced a broadcast
    # signal both TriggerScope controllers listened to; it dispatches to this
    # method by name.
    from imswitch.imcontrol.controller.controllers.TriggerScopePLSRMulticolorController import (
        TriggerScopePLSRMulticolorController,
    )
    from imswitch.imcontrol.controller.controllers.TriggerScopeScanController import (
        TriggerScopeScanController,
    )

    assert callable(TriggerScopePLSRMulticolorController.runScanPLSRMulticolorExternal)
    assert callable(TriggerScopeScanController.runScanPLSRMulticolorExternal)
