"""A result made by another module joins ImProcess's result list.

ImControl's live reconstruction tool sends the result it holds over the
module channel (``sigProcessingResultProduced``); ``ImProcessMainController``
publishes it exactly as a panel's result, so the reconstruction list, the
current-result followers and the status bar all hear about it.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from imswitch.imcommon.controller.ModuleCommunicationChannel import ModuleCommunicationChannel
from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
from imswitch.improcess.model.result import ProcessingResult


class _Result(ProcessingResult):
    def __init__(self, name):
        super().__init__(name=name, data=np.ones((3, 4)), axis_labels=['Y', 'X'])

    def save(self, path, fmt):
        pass


def _stub():
    stub = SimpleNamespace()
    stub._ImProcessMainController__commChannel = Mock()
    handler = ImProcessMainController._onExternalResultProduced.__get__(stub)
    return stub, handler, stub._ImProcessMainController__commChannel


def test_the_module_channel_carries_results_into_improcess():
    assert hasattr(ModuleCommunicationChannel, 'sigProcessingResultProduced')
    channel = ModuleCommunicationChannel()
    received = []
    channel.sigProcessingResultProduced.connect(lambda result, name: received.append((result, name)))
    result = _Result('live')

    channel.sigProcessingResultProduced.emit(result, 'SNOUTY deskew (CAM)')

    assert received == [(result, 'SNOUTY deskew (CAM)')]


def test_an_external_result_is_published_and_made_current():
    stub, handler, comm = _stub()
    result = _Result('live')

    handler(result, 'SNOUTY deskew (CAM)')

    comm.sigResultProduced.emit.assert_called_once_with(result, 'SNOUTY deskew (CAM)')
    comm.sigCurrentResultChanged.emit.assert_called_once_with(result)
    message = comm.sigStatusMessage.emit.call_args.args[0]
    assert 'SNOUTY deskew (CAM)' in message and 'ImControl' in message


def test_an_external_result_without_a_name_uses_its_own():
    stub, handler, comm = _stub()
    result = _Result('snouty_deskewed')

    handler(result, '')

    comm.sigResultProduced.emit.assert_called_once_with(result, 'snouty_deskewed')


def test_nothing_is_published_for_no_result():
    stub, handler, comm = _stub()

    handler(None, 'x')

    assert not comm.sigResultProduced.emit.called
