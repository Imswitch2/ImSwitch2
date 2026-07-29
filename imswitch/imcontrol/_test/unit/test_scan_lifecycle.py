"""Tests for the active-scan-source lifecycle (see docs/scan-lifecycle.rst).

Covers:
- ScanLifecycleMixin announcing/withdrawing the controller on isRunning flips.
- An adoption audit ensuring every scan controller (any class that assigns
  self.isRunning or defines runScanAdvanced) inherits ScanLifecycleMixin,
  directly or transitively. A new scan controller that forgets the mixin
  fails here with a clear message instead of silently breaking consumers
  such as BeadRec.
- Contract assertions that CommunicationChannel resolves scan state through
  the active scan source.
"""

import ast
from pathlib import Path

from imswitch.imcontrol.controller.basecontrollers import ScanLifecycleMixin


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_DIR = ROOT / 'imswitch' / 'imcontrol' / 'controller'
CHANNEL_PATH = CONTROLLER_DIR / 'CommunicationChannel.py'


class _FakeCommChannel:
    def __init__(self):
        self.activeSource = None
        self.calls = []

    def setActiveScanSource(self, controller):
        self.activeSource = controller
        self.calls.append(('set', controller))

    def clearActiveScanSource(self, controller):
        self.calls.append(('clear', controller))
        if self.activeSource is controller:
            self.activeSource = None


class _FakeScanController(ScanLifecycleMixin):
    def __init__(self, commChannel):
        self._commChannel = commChannel


def test_mixin_announces_on_start_and_withdraws_on_stop():
    channel = _FakeCommChannel()
    controller = _FakeScanController(channel)

    assert controller.isRunning is False

    controller.isRunning = True
    assert controller.isRunning is True
    assert channel.activeSource is controller

    controller.isRunning = False
    assert controller.isRunning is False
    assert channel.activeSource is None


def test_mixin_init_clear_does_not_evict_other_controller():
    channel = _FakeCommChannel()
    running = _FakeScanController(channel)
    running.isRunning = True

    # Another controller initializing its flag must not clear the active scan
    other = _FakeScanController(channel)
    other.isRunning = False

    assert channel.activeSource is running


def test_mixin_last_announcement_wins():
    channel = _FakeCommChannel()
    first = _FakeScanController(channel)
    second = _FakeScanController(channel)

    first.isRunning = True
    second.isRunning = True
    assert channel.activeSource is second

    # The superseded controller stopping must not clear the newer scan
    first.isRunning = False
    assert channel.activeSource is second

    second.isRunning = False
    assert channel.activeSource is None


def _iter_controller_classes():
    paths = [CONTROLLER_DIR / 'basecontrollers.py']
    paths += sorted((CONTROLLER_DIR / 'controllers').glob('*.py'))
    for path in paths:
        module = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef):
                yield path, node


def _base_names(classNode):
    names = set()
    for base in classNode.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _assigns_is_running(classNode):
    for node in ast.walk(classNode):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and target.attr == 'isRunning'
                        and isinstance(target.value, ast.Name)
                        and target.value.id == 'self'):
                    return True
    return False


def _defines_run_scan_advanced(classNode):
    return any(
        isinstance(node, ast.FunctionDef) and node.name == 'runScanAdvanced'
        for node in classNode.body
    )


def test_every_scan_controller_inherits_scan_lifecycle_mixin():
    classes = list(_iter_controller_classes())

    # Fixed-point closure over inheritance: a class is lifecycle-aware if it
    # is the mixin or inherits (transitively) from a lifecycle-aware class.
    aware = {'ScanLifecycleMixin'}
    changed = True
    while changed:
        changed = False
        for _, classNode in classes:
            if classNode.name not in aware and _base_names(classNode) & aware:
                aware.add(classNode.name)
                changed = True

    offenders = [
        f'{classNode.name} ({path.name})'
        for path, classNode in classes
        if (_assigns_is_running(classNode) or _defines_run_scan_advanced(classNode))
        and classNode.name not in aware
    ]

    assert offenders == [], (
        'Scan controllers must inherit ScanLifecycleMixin (directly or via '
        'SuperScanController) so the CommunicationChannel learns about their '
        'scans (see docs/scan-lifecycle.rst). Offending classes: '
        + ', '.join(offenders)
    )


def test_known_scan_controllers_are_covered_by_the_audit():
    """Guard the audit itself: the inventoried scan controllers must all be
    detected as scan controllers by the heuristics above."""
    expected = {
        'SuperScanController',
        'ScanControllerBase',
        'ScanControllerPointScan',
        'ScanControllerAdvanced',
        'ScanControllerMoNaLISA',
        'TriggerScopeRasterController',
        'TriggerScopeScanController',
        'TriggerScopePLSRController',
        'TriggerScopePLSRMulticolorController',
        'TriggerScopeLSXYRController',
        'TriggerScopeGalvoDetectionController',
        'LightSheetMulticolorController',
    }
    detected = {
        classNode.name
        for _, classNode in _iter_controller_classes()
        if _assigns_is_running(classNode) or _defines_run_scan_advanced(classNode)
    }

    assert expected.issubset(detected), (
        f'Audit heuristics no longer detect: {sorted(expected - detected)}'
    )


def test_communication_channel_resolves_scan_state_via_active_source():
    source = CHANNEL_PATH.read_text(encoding='utf-8')

    assert 'def setActiveScanSource(self, controller)' in source
    assert 'def clearActiveScanSource(self, controller)' in source
    assert 'def getActiveScanSource(self)' in source
    assert 'if self._activeScanSource is controller:' in source

    isScanRunning = source[
        source.index('def isScanRunning'):source.index('def getCenterViewbox')
    ]
    assert 'return self._activeScanSource is not None' in isScanRunning
    assert '_get_required_controller' not in isScanRunning

    # Metadata accessors must consult the active source first
    for accessor in ('def getDimsScan', 'def getScanStepSizes',
                     'def getNumLineSteps', 'def getFramesPerScanPixel'):
        body = source[source.index(accessor):]
        body = body[:body.index('\n    def ')]
        assert '_activeBeadRecScanSource()' in body, (
            f'{accessor} does not consult the active scan source first'
        )
