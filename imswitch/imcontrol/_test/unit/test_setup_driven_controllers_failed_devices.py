"""Controllers whose setup section names a device that failed at startup.

Focus lock, autofocus, tiling and the TriggerScope scan are driven by names in
the setup file. When the named device did not start they must not end startup:
the panel stays inert (or offers only what started) and says why.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy import QtCore

from imswitch.imcontrol.controller import basecontrollers
from imswitch.imcontrol.model.devices import DeviceNotConnectedError, FailedDeviceManager
from imswitch.imcontrol.model.managers.MultiManager import MultiManager, NoSuchSubManagerError
from imswitch.imcontrol.model.SetupInfo import AutofocusInfo, FocusLockInfo, TilingInfo


class _Group(MultiManager):
    def __init__(self, working=(), failed=()):
        super().__init__({}, 'detectors')
        for name in working:
            self._subManagers[name] = SimpleNamespace(name=name)
        for name in failed:
            self._failedSubManagers[name] = FailedDeviceManager(
                name, OSError(f'{name} unplugged'), managerName='Vendor')


def _master(detectors=None, positioners=None):
    detectors = detectors or _Group()
    detectors.acquire = MagicMock()
    return SimpleNamespace(
        detectorsManager=detectors, positionersManager=positioners or _Group(),
        lasersManager=_Group(), deviceLifecycleService=None, scanManager=MagicMock(),
        recordingManager=MagicMock(), standManager=MagicMock())


def _setup(**sections):
    base = dict(focusLock=None, autofocus=None, tiling=None, detectors={}, positioners={},
                lasers={}, triggerScope=None, scan=None, getTTLDevices=lambda: {})
    base.update(sections)
    return SimpleNamespace(**base)


@pytest.fixture
def controllers(monkeypatch):
    """Controllers built without a window: the base class only stores its arguments."""

    def init(self, setupInfo, commChannel, master, *args, **kwargs):
        QtCore.QObject.__init__(self)
        self._setupInfo, self._commChannel, self._master = setupInfo, commChannel, master
        self._widget = MagicMock()
        self._logger = MagicMock()

    monkeypatch.setattr(basecontrollers.ImConWidgetController, '__init__', init)


_FOCUS = dict(updateFreq=10, frameCropx=0, frameCropy=0, frameCropw=64, frameCroph=64)


def test_focus_lock_with_a_failed_camera_or_positioner_stays_inert(controllers):
    from imswitch.imcontrol.controller.controllers.FocusLockController import FocusLockController

    info = FocusLockInfo(camera='FocusCam', positioner='PiezoZ', swapImageAxes=False,
                         piKp=0.1, piKi=0.01, **_FOCUS)
    master = _master(_Group(failed=['FocusCam']), _Group(failed=['PiezoZ']))
    controller = FocusLockController(_setup(focusLock=info), MagicMock(), master)

    controller._widget.setEnabled.assert_called_once_with(False)
    tooltip = controller._widget.setToolTip.call_args.args[0]
    assert 'FocusCam' in tooltip and 'PiezoZ' in tooltip and 'unplugged' in tooltip
    assert 'camera' not in controller.__dict__           # nothing was wired
    master.detectorsManager.acquire.assert_not_called()


def test_autofocus_with_a_failed_camera_acquires_nothing_and_stays_inert(controllers):
    from imswitch.imcontrol.controller.controllers.AutofocusController import AutofocusController

    info = AutofocusInfo(camera='FocusCam', positioner='PiezoZ', **_FOCUS)
    master = _master(_Group(failed=['FocusCam']), _Group(working=['PiezoZ']))
    controller = AutofocusController(_setup(autofocus=info), MagicMock(), master)

    master.detectorsManager.acquire.assert_not_called()     # no lease on a camera that is not there
    controller._widget.setEnabled.assert_called_once_with(False)
    assert 'FocusCam' in controller._widget.setToolTip.call_args.args[0]


def test_tiling_offers_only_the_detectors_that_started(controllers):
    from imswitch.imcontrol.controller.controllers.TilingController import TilingController

    detectors = {name: SimpleNamespace(forAcquisition=True, managerName='AVManager')
                 for name in ('Good', 'Bad')}
    master = _master(_Group(working=['Good'], failed=['Bad']), _Group(working=['XY']))
    controller = TilingController(
        _setup(tiling=TilingInfo(xyPositioner='XY', camera='Bad'), detectors=detectors),
        MagicMock(), master)

    controller._widget.setDetectors.assert_called_once_with(['Good'])
    controller._widget.setDetector.assert_not_called()      # the preferred one failed


def test_triggerscope_scan_manager_without_a_board_connects_and_then_refuses_every_scan(monkeypatch):
    from imswitch.imcontrol.model.managers.ScanManagerBase import SuperScanManager
    from imswitch.imcontrol.model.managers.ScanManagerTriggerScope import ScanManagerTriggerScope

    monkeypatch.setattr(SuperScanManager, '__init__', lambda self, setupInfo: None)
    manager = ScanManagerTriggerScope(_setup(), None)
    manager.sigScanStarted.connect(lambda: None)           # the scan controllers subscribe
    manager.sigScanDone.connect(lambda: None)
    with pytest.raises(DeviceNotConnectedError, match='TriggerScope.*not connected.*Hardware status'):
        manager.runScan({'deviceParameters': {}, 'scanParameters': {}}, 'rasterScan')


def test_a_scan_positioner_that_failed_is_named_with_its_reason():
    master = _master(positioners=_Group(failed=['Stage']))
    with pytest.raises(NoSuchSubManagerError, match='Stage.*failed to start.*unplugged'):
        basecontrollers._positionerNamed(master, 'Stage')
    assert basecontrollers._missingDevices(master, positioner='Stage')
    assert basecontrollers._missingDevices(_master(positioners=_Group(working=['Stage'])),
                                           positioner='Stage') == []
