"""M3C-1: contract-selected streaming must honor the actual algorithm."""
from types import SimpleNamespace

import pytest

from imswitch.improcess.execution.policy import (
    ExecutionPath, choose_execution, streaming_capability,
)
from imswitch.improcess.model.processing_queue import (
    ExecutionRequirement, ProcessingJob, ProcessingOrigin,
)


def _plugin(*, capable=True, reason=""):
    return SimpleNamespace(
        id="test-plugin", name="Test plugin", supports_streaming=True,
        streaming_capability=lambda params: (capable, reason),
    )


def _job(*, mode=ExecutionRequirement.FOLLOW):
    return ProcessingJob(
        dataset_id="acq", label="acq", reconstructor_id="test-plugin",
        params={"preset": "Fast"}, origin=ProcessingOrigin.AUTO,
        execution_requirement=mode,
    )


def test_explicit_capability_reason_and_legacy_plugin_fallback():
    reason = "Selected placement unsupported"
    status = streaming_capability(_plugin(capable=False, reason=reason), {})
    assert not status.supported and status.reason == reason
    assert streaming_capability(_plugin(), {}).supported
    assert not streaming_capability(SimpleNamespace(supports_streaming=False), {}).supported
    legacy = SimpleNamespace(
        supports_streaming=True, supports_streaming_params=lambda params: False,
    )
    assert not streaming_capability(legacy, {}).supported


def test_no_unannounced_fallback_to_live_or_other_algorithm():
    unavailable = _plugin(capable=False, reason="Requires offline Fast")
    choice = choose_execution(_job(), "scan", unavailable, growing=True, live_reader=True)
    assert choice.path == ExecutionPath.WAIT_FINITE
    assert "Requires offline Fast" in choice.reason
    assert choose_execution(_job(), "scan", unavailable).path == ExecutionPath.OFFLINE_FINITE
    supported = _plugin()
    assert (choose_execution(_job(), "scan", supported, growing=True,
                             live_reader=True).path == ExecutionPath.INCREMENTAL_FOLLOW)
    assert (choose_execution(_job(), "scan", supported, growing=True,
                             live_reader=False).path == ExecutionPath.WAIT_FINITE)
    assert (choose_execution(_job(mode=ExecutionRequirement.FINITE), "scan", supported,
                             growing=True, live_reader=True).path == ExecutionPath.WAIT_FINITE)


@pytest.mark.parametrize("preset,reason_fragment", [
    ("Fast", "unified"),
    ("Full", "Robust"),
    ("Legacy DLL", "Legacy DLL"),
    ("Custom", "Custom"),
])
def test_monalisa_never_routes_unified_mode_to_unverified_rectangular_stream(preset, reason_fragment):
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    from imswitch.improcess.execution.contracts import Incompatible

    reconstructor = MonalisaReconstructor()
    params = {"preset": preset}
    supported, reason = reconstructor.streaming_capability(params)
    assert not supported and reason_fragment.lower() in reason.lower()
    assert not reconstructor.supports_streaming_params(params)
    assert not streaming_capability(reconstructor, params).supported
    with pytest.raises(Incompatible):
        reconstructor.contract(SimpleNamespace(S=(12,), frames_per_timepoint=12), params)


def test_auto_recipe_snapshots_finite_when_streaming_unavailable():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    params = {"preset": "Fast"}
    stub = SimpleNamespace(
        _main=SimpleNamespace(_activeReconstructor=_plugin(capable=False)),
        autoPolicy=SimpleNamespace(enabled=True),
        _processWhileAcquiring=True,
        _snapshot_params=lambda reconstructor: dict(params),
    )
    recipe = ProcessingQueueController.captureAutoRecipe(stub)
    assert recipe["execution_requirement"] == "finite"
    assert recipe["params"] == params
    stub._main._activeReconstructor = _plugin(capable=True)
    recipe = ProcessingQueueController.captureAutoRecipe(stub)
    assert recipe["execution_requirement"] == "follow"


def test_selected_method_is_read_only_and_capability_changes_do_not_change_queued_jobs():
    from imswitch.improcess.controller.ProcessingSessionController import ProcessingSessionController

    views = []
    disarmed = []
    queue = SimpleNamespace(
        _processWhileAcquiring=True,
        setProcessWhileAcquiring=lambda state: disarmed.append(state),
    )
    widget = SimpleNamespace(
        getReconstructionParams=lambda: {"preset": "Fast"},
        setSessionStreamingCapability=lambda *args: views.append(args),
    )
    stub = SimpleNamespace(
        _main=SimpleNamespace(
            _activeReconstructor=SimpleNamespace(
                id="monalisa", name="MoNaLISA", supports_streaming=True,
                streaming_capability=lambda params: (False, "Fast parity pending"),
            ),
            _widget=widget,
        ),
        _queue_controller=lambda: queue,
    )
    ProcessingSessionController._sync_streaming_capability(stub)
    assert views[-1] == ("MoNaLISA · Fast", False, "Fast parity pending")
    assert disarmed == [False]
    # No job is touched or mutated; only future arrival policy is disarmed.


def test_footer_disables_unsupported_follow_with_specific_tooltip():
    pytest.importorskip("qtpy")
    from imswitch.improcess.view.ImProcessMainView import BtnFrame

    from qtpy import QtWidgets
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    footer = BtnFrame()
    footer.setContext("session")
    footer.setSessionActive(True)
    footer.setAutoProcessingAvailable(True)
    footer.setProcessingQueue({"auto_enabled": True, "process_while_acquiring": False})
    footer.setSessionStreamingCapability("MoNaLISA · Fast", False, "Fast parity pending")
    assert "MoNaLISA · Fast" in footer.selectedReconstructionLabel.toolTip()
    assert footer.selectedReconstructionLabel._fullText == "MoNaLISA · Fast"
    assert not footer.processWhileAcquiringCheck.isEnabled()
    assert "Fast parity pending" in footer.processWhileAcquiringCheck.toolTip()
    footer.setSessionStreamingCapability("Other", True, "Reader also required")
    assert footer.processWhileAcquiringCheck.isEnabled()
    assert footer.autoProcessCheck.isEnabled()


def test_auto_defaults_live_on_only_when_streaming_is_supported():
    from imswitch.improcess.controller.ProcessingQueueController import ProcessingQueueController

    state = {"supported": True}
    changes = []
    reconstructor = _plugin()
    reconstructor.streaming_capability = lambda params: (state["supported"], "")
    stub = SimpleNamespace(
        autoPolicy=SimpleNamespace(enabled=False),
        _processWhileAcquiring=False,
        _main=SimpleNamespace(_activeReconstructor=reconstructor),
        _snapshot_params=lambda _: {"preset": "Fast"},
        _emit_changed=lambda: changes.append(True),
    )

    ProcessingQueueController.setAutoEnabled(stub, True)
    assert stub.autoPolicy.enabled and stub._processWhileAcquiring
    assert len(changes) == 1

    # The operator's manual Live-off choice is not reset by repeated sync.
    stub._processWhileAcquiring = False
    ProcessingQueueController.setAutoEnabled(stub, True)
    assert not stub._processWhileAcquiring

    ProcessingQueueController.setAutoEnabled(stub, False)
    ProcessingQueueController.setAutoEnabled(stub, True)
    assert stub._processWhileAcquiring

    ProcessingQueueController.setAutoEnabled(stub, False)
    state["supported"] = False
    ProcessingQueueController.setAutoEnabled(stub, True)
    assert stub.autoPolicy.enabled and not stub._processWhileAcquiring


def test_footer_compact_action_row_and_live_visibility():
    pytest.importorskip("qtpy")
    from qtpy import QtWidgets
    from imswitch.improcess.view.ImProcessMainView import BtnFrame

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    footer = BtnFrame()
    footer.setContext('session')
    footer.setSessionActive(True)
    footer.setAutoProcessingAvailable(True)
    footer.setSessionStreamingCapability('MoNaLISA · Fast', True, 'Supported')
    row = footer.actionRow.layout()
    assert row.indexOf(footer.addToMultiDataBtn) < row.indexOf(footer.selectedReconstructionLabel)
    assert row.indexOf(footer.selectedReconstructionLabel) < row.indexOf(footer.autoProcessCheck)
    assert row.indexOf(footer.autoProcessCheck) < row.indexOf(footer.processWhileAcquiringCheck)
    assert footer.autoProcessCheck.text() == 'Auto'
    assert footer.processWhileAcquiringCheck.text() == 'Live'
    assert not hasattr(footer, 'sessionOptionsRow')
    footer.setProcessingQueue({'auto_enabled': True, 'process_while_acquiring': True})
    assert footer.processWhileAcquiringCheck.isChecked()
    assert not footer.processWhileAcquiringCheck.isHidden()
    footer.setProcessingQueue({'auto_enabled': True, 'process_while_acquiring': False})
    assert not footer.processWhileAcquiringCheck.isChecked()
    footer.setSessionStreamingCapability('MoNaLISA · Fast', False, 'Fast parity pending')
    assert not footer.processWhileAcquiringCheck.isEnabled()
    assert 'Fast parity pending' in footer.processWhileAcquiringCheck.toolTip()
    footer.setProcessingQueue({'auto_enabled': False, 'process_while_acquiring': False})
    assert footer.processWhileAcquiringCheck.isHidden()
    footer.setContext('loaded')
    assert footer.selectedReconstructionLabel.isHidden()
    footer.deleteLater()
