"""The communication channel's view of the loaded results.

``sigCurrentResultChanged`` only ever describes one result, so any panel
offering an operation over several reconstruction objects needs both a way to
enumerate them and something to refresh on. These are those.
"""

from types import SimpleNamespace

from imswitch.improcess.controller.CommunicationChannel import CommunicationChannel
from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)


class _Provider:
    def __init__(self, all_results, selected):
        self._all = all_results
        self._selected = selected

    def getAllResults(self):
        return iter(self._all)  # a generator is a valid provider answer

    def getSelectedResults(self):
        return self._selected


def test_channel_reports_no_results_without_a_provider():
    channel = CommunicationChannel()

    assert channel.getAllResults() == []
    assert channel.getSelectedResults() == []


def test_channel_delegates_to_the_registered_provider():
    channel = CommunicationChannel()
    first, second = SimpleNamespace(name="a"), SimpleNamespace(name="b")
    channel.setResultProvider(
        _Provider([("a", first), ("b", second)], [("b", second)])
    )

    assert [name for name, _r in channel.getAllResults()] == ["a", "b"]
    assert [name for name, _r in channel.getSelectedResults()] == ["b"]


def test_channel_survives_a_provider_that_lost_its_methods():
    """A partially torn-down provider must not take the panels down with it."""
    channel = CommunicationChannel()
    channel.setResultProvider(object())

    assert channel.getAllResults() == []


def test_results_changed_is_announced_when_a_result_is_added():
    channel = CommunicationChannel()
    fired = []
    channel.sigResultsChanged.connect(lambda: fired.append(True))

    stub = SimpleNamespace(
        _widget=SimpleNamespace(addNewData=lambda result, name: None),
        _commChannel=channel,
    )
    handler = ReconstructionViewController.resultProduced.__get__(stub)
    stub._resultsChanged = ReconstructionViewController._resultsChanged.__get__(stub)

    handler(SimpleNamespace(name="one"), "one")

    assert fired == [True]
