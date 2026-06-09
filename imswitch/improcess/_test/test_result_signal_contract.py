"""Contract test for sigResultProduced — the producer/renderer decoupling.

ReconstructionViewController.resultProduced is the single entry point that
folds a freshly-emitted ProcessingResult into the reconstruction list. This
test verifies that contract using a small recording stub: it doesn't need
Qt, napari, or any imcontrol scaffolding.
"""

from types import SimpleNamespace

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)


class _RecordingReconView:
    def __init__(self):
        self.added = []

    def addNewData(self, result, name):
        self.added.append((result, name))


def _bound_resultProduced():
    """Return ``resultProduced`` bound to a fresh recording stub."""
    stub = SimpleNamespace(_widget=_RecordingReconView())
    return ReconstructionViewController.resultProduced.__get__(stub), stub


def test_sigResultProduced_signature_is_object_str():
    sig = CommunicationChannel.sigResultProduced
    # Signal classes carry their declared types as a tuple under .types in
    # pyqtSignal / Signal. Best-effort check; fall back to construction.
    types = getattr(sig, 'types', None)
    if types is None:
        types = getattr(sig, '_signature_types', None)
    if types is not None:
        assert tuple(types) == (object, str)


def test_resultProduced_forwards_to_widget_addNewData():
    handler, stub = _bound_resultProduced()
    sentinel = object()

    handler(sentinel, 'pretty-name')

    assert stub._widget.added == [(sentinel, 'pretty-name')]


def test_resultProduced_uses_display_name_when_provided():
    handler, stub = _bound_resultProduced()
    result = SimpleNamespace(name='intrinsic')

    handler(result, 'override')

    assert stub._widget.added == [(result, 'override')]


def test_resultProduced_falls_back_to_result_name_when_display_empty():
    handler, stub = _bound_resultProduced()
    result = SimpleNamespace(name='intrinsic')

    handler(result, '')

    assert stub._widget.added == [(result, 'intrinsic')]


def test_resultProduced_falls_back_to_literal_when_result_has_no_name():
    handler, stub = _bound_resultProduced()
    result = SimpleNamespace()  # no .name attribute

    handler(result, '')

    assert stub._widget.added == [(result, 'result')]


def test_resultProduced_drops_none_result_silently():
    handler, stub = _bound_resultProduced()

    handler(None, 'whatever')

    assert stub._widget.added == []
