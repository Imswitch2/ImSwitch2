"""EditorController forwards a finished run to the module-level signal, which
carries no payload (rig regression: 'signal has 0 argument(s) but 1
provided' on every script end)."""
from types import SimpleNamespace

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imscripting.controller.EditorController import EditorController
from imswitch.imscripting.model import ScriptRunResult


def test_execution_finished_is_forwarded_without_arguments(qtbot):
    channel = ModuleCommunicationChannel()
    received = []
    channel.sigExecutionFinished.connect(lambda *args: received.append(args))
    ctrl = EditorController.__new__(EditorController)
    ctrl._moduleCommChannel = channel
    stopping = []
    ctrl._widget = SimpleNamespace(setStopping=stopping.append)

    ctrl._onExecutionFinished(ScriptRunResult())

    assert received == [()]
    assert stopping == [False]
