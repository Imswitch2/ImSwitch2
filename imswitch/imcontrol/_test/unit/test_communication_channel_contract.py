import ast
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CHANNEL_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'CommunicationChannel.py'


def _channel_class():
    module = ast.parse(CHANNEL_PATH.read_text())
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == 'CommunicationChannel'
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
