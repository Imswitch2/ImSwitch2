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


def test_communication_channel_signal_inventory_matches_snapshot():
    expected = json.loads(INVENTORY_PATH.read_text())

    assert _signal_inventory() == expected


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


def test_communication_channel_has_no_unused_legacy_surface():
    names = {item['name'] for item in _signal_inventory()}
    source = CHANNEL_PATH.read_text()

    # Removed in the 2026-10 cleanup: no producer and no consumer anywhere.
    for removed in ('sigGridToggled', 'sigCrosshairToggled',
                    'sigScanFrameFinished', 'sigInitiateEt', 'sigSetXYPosition',
                    'sigSetZPosition', 'sigSetExposure', 'sigSetSpeed'):
        assert removed not in names
    assert 'DEPRECATED_SIGNALS' not in source
    assert '_create_event_groups' not in source
