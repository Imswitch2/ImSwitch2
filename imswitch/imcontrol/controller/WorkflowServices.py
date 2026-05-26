class ScanWorkflowService:
    """Small service wrapper for scan/recording workflow signals.

    This keeps legacy CommunicationChannel signals intact while giving new code
    a narrower coordination surface than the full global signal bus.
    """

    def __init__(self, comm_channel) -> None:
        self._comm_channel = comm_channel

    def request_scan_parameters(self) -> None:
        self._comm_channel.sigRequestScanParameters.emit()

    def run_scan(self, recalculate_signals: bool, is_non_final_part_of_sequence: bool) -> None:
        self._comm_channel.sigRunScan.emit(recalculate_signals, is_non_final_part_of_sequence)

    def request_scan_frequency(self) -> None:
        self._comm_channel.sigRequestScanFreq.emit()

    def set_axis_centers(self, devices, centers) -> None:
        self._comm_channel.sigSetAxisCenters.emit(devices, centers)

    def start_external_recording(self) -> None:
        self._comm_channel.sigStartRecordingExternal.emit()

    def notify_scan_starting(self) -> None:
        self._comm_channel.sigScanStarting.emit()


class BeadRecWorkflowService:
    """Wrapper for bead-recognition and MoNaLISA center-query signals."""

    def __init__(self, comm_channel) -> None:
        self._comm_channel = comm_channel

    def query_center_coord(self, search_mode: str) -> None:
        self._comm_channel.sigQueryCenterCoord.emit(search_mode)

    def finish_center_coord_pipeline(self, coord) -> None:
        self._comm_channel.sigCenterCoordPipelineFinished.emit(coord)

    def update_bead_rec_center(self, y: int, x: int) -> None:
        self._comm_channel.sigUpdateBeadRecCenter.emit(y, x)

    def show_bead_rec_center_cross(self, state: bool) -> None:
        self._comm_channel.sigShowBeadRecCenterCross.emit(state)

    def set_auto_axial(self, state: bool) -> None:
        self._comm_channel.sigAutoAxialToggled.emit(state)

    def set_axial_list_buffer(self, axial_list_buffer: list) -> None:
        self._comm_channel.sigNewAxialListBuffer.emit(axial_list_buffer)

    def on_query_center_coord(self, slot) -> None:
        self._comm_channel.sigQueryCenterCoord.connect(slot)

    def on_center_coord_pipeline_finished(self, slot) -> None:
        self._comm_channel.sigCenterCoordPipelineFinished.connect(slot)

    def on_update_bead_rec_center(self, slot) -> None:
        self._comm_channel.sigUpdateBeadRecCenter.connect(slot)

    def on_show_bead_rec_center_cross(self, slot) -> None:
        self._comm_channel.sigShowBeadRecCenterCross.connect(slot)

    def on_auto_axial_toggled(self, slot) -> None:
        self._comm_channel.sigAutoAxialToggled.connect(slot)

    def on_new_axial_list_buffer(self, slot) -> None:
        self._comm_channel.sigNewAxialListBuffer.connect(slot)
