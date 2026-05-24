import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_SERVICES_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'WorkflowServices.py'

spec = importlib.util.spec_from_file_location('WorkflowServices', WORKFLOW_SERVICES_PATH)
workflow_services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_services)
BeadRecWorkflowService = workflow_services.BeadRecWorkflowService
ScanWorkflowService = workflow_services.ScanWorkflowService


class _Signal:
    def __init__(self) -> None:
        self.emitted = []

    def emit(self, *args) -> None:
        self.emitted.append(args)

    def connect(self, slot) -> None:
        self.emitted.append(('connect', slot))


class _CommChannel:
    def __init__(self) -> None:
        self.sigRequestScanParameters = _Signal()
        self.sigRunScan = _Signal()
        self.sigRequestScanFreq = _Signal()
        self.sigSetAxisCenters = _Signal()
        self.sigStartRecordingExternal = _Signal()
        self.sigScanStarting = _Signal()
        self.sigQueryCenterCoord = _Signal()
        self.sigCenterCoordPipelineFinished = _Signal()
        self.sigUpdateBeadRecCenter = _Signal()
        self.sigShowBeadRecCenterCross = _Signal()
        self.sigAutoAxialToggled = _Signal()
        self.sigNewAxialListBuffer = _Signal()


def test_scan_workflow_service_wraps_legacy_scan_signals():
    comm_channel = _CommChannel()
    workflow = ScanWorkflowService(comm_channel)

    workflow.request_scan_parameters()
    workflow.run_scan(True, False)
    workflow.request_scan_frequency()
    workflow.set_axis_centers(['X', 'Y'], [1.0, 2.0])
    workflow.start_external_recording()
    workflow.notify_scan_starting()

    assert comm_channel.sigRequestScanParameters.emitted == [()]
    assert comm_channel.sigRunScan.emitted == [(True, False)]
    assert comm_channel.sigRequestScanFreq.emitted == [()]
    assert comm_channel.sigSetAxisCenters.emitted == [(['X', 'Y'], [1.0, 2.0])]
    assert comm_channel.sigStartRecordingExternal.emitted == [()]
    assert comm_channel.sigScanStarting.emitted == [()]


def test_bead_rec_workflow_service_wraps_legacy_signals():
    comm_channel = _CommChannel()
    workflow = BeadRecWorkflowService(comm_channel)
    slot = object()

    workflow.query_center_coord('Maxima')
    workflow.finish_center_coord_pipeline((1, 2))
    workflow.update_bead_rec_center(3, 4)
    workflow.show_bead_rec_center_cross(True)
    workflow.set_auto_axial(False)
    workflow.set_axial_list_buffer(['XZ'])
    workflow.on_query_center_coord(slot)
    workflow.on_center_coord_pipeline_finished(slot)
    workflow.on_update_bead_rec_center(slot)
    workflow.on_show_bead_rec_center_cross(slot)
    workflow.on_auto_axial_toggled(slot)
    workflow.on_new_axial_list_buffer(slot)

    assert comm_channel.sigQueryCenterCoord.emitted == [('Maxima',), ('connect', slot)]
    assert comm_channel.sigCenterCoordPipelineFinished.emitted == [((1, 2),), ('connect', slot)]
    assert comm_channel.sigUpdateBeadRecCenter.emitted == [(3, 4), ('connect', slot)]
    assert comm_channel.sigShowBeadRecCenterCross.emitted == [(True,), ('connect', slot)]
    assert comm_channel.sigAutoAxialToggled.emitted == [(False,), ('connect', slot)]
    assert comm_channel.sigNewAxialListBuffer.emitted == [(['XZ'],), ('connect', slot)]
