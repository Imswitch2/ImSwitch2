"""ImProcess reports the grid a live MoNaLISA run localized, without touching the widget."""

from types import SimpleNamespace
from unittest.mock import Mock

from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController


def _stub():
    stub = SimpleNamespace()
    stub._ImProcessMainController__commChannel = Mock()
    stub._ImProcessMainController__logger = Mock()
    handler = ImProcessMainController._onLivePatternLocalized.__get__(stub)
    return stub, handler, stub._ImProcessMainController__commChannel


def test_the_fresh_localization_goes_to_the_status_bar_and_the_log():
    stub, handler, comm = _stub()

    handler({'row_offset': 1.25, 'col_offset': 2.5, 'row_period': 11.05, 'col_period': 11.0,
             'source': 'auto'})

    message = comm.sigStatusMessage.emit.call_args.args[0]
    assert message.startswith('Live MoNaLISA: pattern localized on the first stack')
    assert '11.05 x 11.00 px' in message and '1.25 / 2.50 px' in message
    stub._ImProcessMainController__logger.info.assert_called_once_with(message)


def test_a_malformed_payload_is_ignored():
    stub, handler, comm = _stub()

    handler({'row_offset': 'x'})

    assert not comm.sigStatusMessage.emit.called
