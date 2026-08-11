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


# --------------------------------------------------------------------------
# C-11 / P-4.6 — a follower panel's result list follows the loaded set
# --------------------------------------------------------------------------

class _FollowerPanel:
    """A measurement panel that can both follow and enumerate results."""

    def __init__(self):
        self.available = None
        self.current = None

    def setAvailableResults(self, results, selected=None):
        self.available = list(results)

    def setCurrentResult(self, result):
        self.current = result


def _controllerStub(channel):
    """A main controller with only the parts _wire_result_follower touches."""
    from imswitch.improcess.controller.ImProcessMainController import (
        ImProcessMainController,
    )

    stub = ImProcessMainController.__new__(ImProcessMainController)
    stub._resultFollowers = set()
    # Name-mangled attributes, set directly because __init__ is not run.
    stub._ImProcessMainController__commChannel = channel
    stub._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None
    )
    stub.mainViewController = SimpleNamespace(
        reconstructionController=SimpleNamespace(getActiveResult=lambda: None)
    )
    return stub


def test_a_follower_panel_is_seeded_with_the_results_already_loaded():
    channel = CommunicationChannel()
    first = SimpleNamespace(name="a")
    channel.setResultProvider(_Provider([("a", first)], []))

    panel = _FollowerPanel()
    _controllerStub(channel)._wire_result_follower(panel)

    assert [name for name, _r in panel.available] == ["a"]


def test_loading_a_result_reaches_an_already_open_follower_panel():
    """Opening a reconstruction while the panel is open must update its list."""
    channel = CommunicationChannel()
    results = [("a", SimpleNamespace(name="a"))]
    channel.setResultProvider(_Provider(results, []))

    panel = _FollowerPanel()
    _controllerStub(channel)._wire_result_follower(panel)
    assert len(panel.available) == 1

    results.append(("b", SimpleNamespace(name="b")))
    channel.sigResultsChanged.emit()

    assert [name for name, _r in panel.available] == ["a", "b"]


def test_a_panel_that_both_publishes_and_follows_gets_both_wirings():
    """The ROI manager does both; routing it as a producer used to cost it the
    follower wiring, so its across-results list went stale."""
    channel = CommunicationChannel()
    results = [("a", SimpleNamespace(name="a"))]
    channel.setResultProvider(_Provider(results, []))

    class _Producer(_FollowerPanel):
        def __init__(self):
            super().__init__()
            self.sigResultProduced = SimpleNamespace(connect=lambda _fn: None)

    panel = _Producer()
    controller = _controllerStub(channel)
    controller._panelResultBridges = set()
    controller._wire_producing_panel(panel)

    assert [name for name, _r in panel.available] == ["a"]
    results.append(("b", SimpleNamespace(name="b")))
    channel.sigResultsChanged.emit()
    assert [name for name, _r in panel.available] == ["a", "b"]
