"""Scan handles cross REST/Pyro as their status dict (plan R-12)."""
from imswitch.imcontrol.controller.WorkflowServices import ScanRequestCompletion
from imswitch.imcontrol.controller.server.ImSwitchServer import serializeApiResult
from imswitch.imcontrol.controller.server._serialize import SerScanRunHandle
from imswitch.imcontrol.model.scan_request import ScanRunHandle


def _handle():
    completion = ScanRequestCompletion(None)
    token = object()
    completion.bind(token)
    return ScanRunHandle('Scan', completion, requestId='abc123'), completion, token


def test_pending_and_completed_handles_serialize_to_plain_dicts():
    handle, completion, token = _handle()
    assert serializeApiResult(handle) == {
        'requestId': 'abc123', 'source': 'Scan', 'state': 'pending',
        'message': '', 'exact': True,
    }
    completion.resolve(token, False, 'aborted')
    assert serializeApiResult(handle)['state'] == 'failed'
    assert serializeApiResult(handle)['message'] == 'aborted'
    assert SerScanRunHandle().to_dict(handle) == handle.to_dict()
    assert SerScanRunHandle().from_dict('x', handle.to_dict()) == handle.to_dict()


def test_primitive_results_pass_through_unchanged():
    for value in (None, 3, 2.5, 'x', b'y', [1], {'a': 1}, (1, 2), True):
        assert serializeApiResult(value) is value
