"""Discovery contract for every scan controller class (plan A-10).

Any class in ``imswitch.imcontrol.controller.controllers`` that defines
``runScanExternal`` is a scan source. For each one, without an allow-list:

1. it exports no ``runScan`` of its own (the API is owned once, R-13);
2. its family's start gate records a refusal on ``sigScanRequestRejected``
   without publishing any other lifecycle signal (R-07 / D-03);
3. every ``@APIExport`` method inherited from a base stays exported on the
   subclass (D-13 generalised).
"""
import importlib
import inspect
import pkgutil
from types import SimpleNamespace

import pytest

import imswitch.imcontrol.controller.controllers as controllers_pkg
from imswitch.imcommon.model import generateAPI
from imswitch.imcontrol.controller.basecontrollers import (
    ImConWidgetController, ScanLifecycleMixin, SuperScanController,
)
from imswitch.imcontrol.controller.controllers._triggerscope_scan_lifecycle import (
    TriggerScopeScanLifecycleMixin,
)


def _controller_classes():
    seen = {}
    for module in pkgutil.iter_modules(controllers_pkg.__path__):
        if module.name.startswith('_'):
            continue
        try:
            mod = importlib.import_module(f'{controllers_pkg.__name__}.{module.name}')
        except Exception:
            continue
        for name, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__:
                continue
            if issubclass(cls, ImConWidgetController):
                seen[f'{module.name}.{name}'] = cls
    return seen


CONTROLLERS = _controller_classes()
SCAN_SOURCES = {k: v for k, v in CONTROLLERS.items() if 'runScanExternal' in dir(v)}


def _shell(cls, channel):
    ctrl = cls.__new__(cls)
    ImConWidgetController.__init__(
        ctrl, setupInfo=SimpleNamespace(), commChannel=channel,
        master=SimpleNamespace(), widget=SimpleNamespace(
            setScanButtonChecked=lambda *_a: None,
            setRepeatEnabled=lambda *_a: None,
        ),
        factory=None, moduleCommChannel=None,
    )
    return ctrl


class _Signal:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def emit(self, *args):
        self.log.append((self.name,) + args)


class _Channel:
    def __init__(self):
        self.lifecycle = []
        for name in ('sigScanStarting', 'sigScanStarted', 'sigScanDone',
                     'sigScanEnded', 'sigScanRequestRejected',
                     'sigScanDevicesResolved', 'sigScanBuilt'):
            setattr(self, name, _Signal(self.lifecycle, name))

    def setActiveScanSource(self, _c):
        pass

    def clearActiveScanSource(self, _c):
        pass


def test_the_discovery_found_both_families():
    assert SCAN_SOURCES, 'no scan controller classes discovered'
    families = {issubclass(c, SuperScanController) for c in SCAN_SOURCES.values()}
    assert families == {True, False}


@pytest.mark.parametrize('name', sorted(SCAN_SOURCES))
def test_scan_controllers_export_no_run_scan_of_their_own(name, qtbot):
    cls = SCAN_SOURCES[name]
    runScan = getattr(cls, 'runScan', None)
    assert runScan is None or not getattr(runScan, '_APIExport', False), (
        f'{name}.runScan is exported; the scan API is owned by '
        'WorkflowFacadeController.runScan (plan R-13)'
    )
    shell = _shell(cls, _Channel())
    assert 'runScan' not in generateAPI([shell])._asdict()


@pytest.mark.parametrize('name', sorted(SCAN_SOURCES))
def test_a_refused_start_is_recorded_without_any_lifecycle_signal(name, qtbot):
    cls = SCAN_SOURCES[name]
    channel = _Channel()
    shell = _shell(cls, channel)
    if issubclass(cls, SuperScanController):
        shell._scanCompletionPublishing = True          # the re-entrant gate
        shell._scanCoordinator = SimpleNamespace(
            runForOwner=lambda _o: None, tokenForOwner=lambda _o: None,
        )
        result = SuperScanController._beginScanRun(shell, sigScanStartingEmitted=False)
        assert result is None
    else:
        assert issubclass(cls, TriggerScopeScanLifecycleMixin)
        token = SimpleNamespace(releaseRequested=False)
        shell._triggerScopeRunToken = token
        shell._scanStopRequested = False
        shell._scanCoordinator = SimpleNamespace(
            runForOwner=lambda _o: token, tokenForOwner=lambda _o: object(),
        )
        result = TriggerScopeScanLifecycleMixin._startTriggerScopeScan(
            shell, parameters={}, scanType='rasterScan', laserDevices=[],
            sigScanStartingEmitted=False,
        )
        assert result is False
    assert shell._lastScanStartRejection
    names = [e[0] for e in channel.lifecycle]
    assert names == ['sigScanRequestRejected'], names


@pytest.mark.parametrize('name', sorted(CONTROLLERS))
def test_inherited_api_exports_survive_overrides(name):
    cls = CONTROLLERS[name]
    for base in cls.__mro__[1:]:
        for attr, value in vars(base).items():
            if not callable(value) or not getattr(value, '_APIExport', False):
                continue
            sub = getattr(cls, attr, None)
            assert sub is not None and getattr(sub, '_APIExport', False), (
                f'{name}.{attr} overrides an API export of {base.__name__} '
                'without re-applying @APIExport (plan D-13)'
            )
