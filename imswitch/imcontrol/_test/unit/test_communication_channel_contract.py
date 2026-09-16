import ast
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CHANNEL_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'CommunicationChannel.py'
INVENTORY_PATH = Path(__file__).with_name('communication_channel_signal_inventory.json')


def _channel_class():
    module = ast.parse(CHANNEL_PATH.read_text())
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == 'CommunicationChannel'
    )


def _signal_inventory():
    inventory = []
    for node in _channel_class().body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        if getattr(node.value.func, 'id', None) != 'Signal':
            continue
        inventory.append(
            {
                'name': node.targets[0].id,
                'args': [ast.unparse(arg) for arg in node.value.args],
            }
        )
    return inventory


def _deprecated_signals():
    for node in _channel_class().body:
        if not isinstance(node, ast.Assign):
            continue
        if node.targets[0].id == 'DEPRECATED_SIGNALS':
            return ast.literal_eval(node.value)
    return {}


def _class_literal(name):
    for node in _channel_class().body:
        if not isinstance(node, ast.Assign):
            continue
        if node.targets[0].id == name:
            return ast.literal_eval(node.value)
    return None


def test_communication_channel_signal_inventory_matches_snapshot():
    expected = json.loads(INVENTORY_PATH.read_text())

    assert _signal_inventory() == expected


def test_communication_channel_deprecated_signals_are_explicit_and_available():
    deprecated = _deprecated_signals()
    signal_names = {item['name'] for item in _signal_inventory()}

    assert deprecated == {
        'sigGridToggled': 'No internal producer or consumer found in repository scan.',
        'sigCrosshairToggled': 'No internal producer or consumer found in repository scan.',
        'sigScanFrameFinished': 'No internal producer or consumer found in repository scan.',
        'sigClockWidefield': 'No internal producer or consumer found in repository scan.',
    }
    assert set(deprecated).issubset(signal_names)
    assert 'sigSaveFocus' not in deprecated


def test_communication_channel_declares_domain_event_groups():
    assert _class_literal('EVENT_GROUP_NAMES') == (
        'acquisitionEvents',
        'viewEvents',
        'recordingEvents',
        'scanEvents',
        'snapshotEvents',
        'slmEvents',
        'focusEvents',
        'rotationEvents',
        'eventTriggeredEvents',
        'beadRecEvents',
        'useqEvents',
        'scriptEvents',
    )


def test_communication_channel_event_groups_alias_existing_signals():
    source = CHANNEL_PATH.read_text()

    assert "'updateImage': self.sigUpdateImage" in source
    assert "'recordingEnded': self.sigRecordingEnded" in source
    assert "'recordingFailed': self.sigRecordingFailed" in source
    assert "'scanStarting': self.sigScanStarting" in source
    assert "'scanEnded': self.sigScanEnded" in source
    assert "'setAxisCenters': self.sigSetAxisCenters" in source
    assert "'snapImgPrev': self.sigSnapImgPrev" in source
    assert "'updateRotatorPosition': self.sigUpdateRotatorPosition" in source
    assert "'initiateEtMonalisa': self.sigInitiateEtMonalisa" in source
    assert "'queryCenterCoord': self.sigQueryCenterCoord" in source
    assert "'setExposure': self.sigSetExposure" in source
    assert "'scriptExecutionFinished': self.sigScriptExecutionFinished" in source


def test_communication_channel_exposes_scan_workflow_service():
    source = CHANNEL_PATH.read_text()

    assert 'from .WorkflowServices import BeadRecWorkflowService, ScanWorkflowService' in source
    assert 'self.scanWorkflow = ScanWorkflowService(self)' in source
    assert 'self.beadRecWorkflow = BeadRecWorkflowService(self)' in source
    assert 'def run_scan(self, recalculate_signals: bool,' in (
        (ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'WorkflowServices.py').read_text()
    )


def test_communication_channel_has_no_duplicate_method_names():
    method_names = [
        node.name
        for node in _channel_class().body
        if isinstance(node, ast.FunctionDef)
    ]
    duplicates = {
        name: count
        for name, count in Counter(method_names).items()
        if count > 1
    }

    assert duplicates == {}


def test_communication_channel_keeps_public_api_signal_names():
    source = CHANNEL_PATH.read_text()

    assert "'acquisitionStarted': self.sigAcquisitionStarted" in source
    assert "'acquisitionStopped': self.sigAcquisitionStopped" in source
    assert "'recordingStarted': self.sigRecordingStarted" in source
    assert "'recordingEnded': self.sigRecordingEnded" in source
    assert "'recordingFailed': self.sigRecordingFailed" in source
    assert "'scanEnded': self.sigScanEnded" in source
    assert "'saveFocus': self.sigSaveFocus" in source
