from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass
class EtSTEDTriggeredScanResult:
    """Outcome of preparing and triggering one etSTED slow scan."""

    success: bool
    message: str = ''
    signal_dict: dict | None = None
    scan_info_dict: dict | None = None


class EtSTEDTriggeredScanRunner:
    """Validate, prepare, and trigger etSTED slow scans."""

    scan_widget_mode = 'ScanWidget'
    recording_widget_mode = 'RecordingWidget'

    def prepare(
        self,
        position,
        scan_initiation_mode: str,
        analog_params: dict,
        digital_params: dict,
        positioners_scan: list,
        scan_manager=None,
        comm_channel=None,
        positioners_manager=None,
        apply_fast_axis_shift: bool = False,
        fast_axis_shift_fn: Callable[[float], float] | None = None,
    ) -> EtSTEDTriggeredScanResult:
        """Prepare a triggered slow scan without starting unsupported hardware paths."""
        position = self._validate_position(position)
        self.validate_scan_parameters(analog_params, digital_params, positioners_scan)
        self.set_center_scan_parameter(
            analog_params,
            positioners_scan,
            position,
            positioners_manager,
            apply_fast_axis_shift,
            fast_axis_shift_fn,
        )

        if scan_initiation_mode == self.scan_widget_mode:
            if scan_manager is None:
                return EtSTEDTriggeredScanResult(False, 'ScanWidget initiation requires a scan manager.')
            try:
                signal_dict, scan_info_dict = scan_manager.makeFullScan(
                    analog_params, digital_params, False
                )
            except Exception as e:
                return EtSTEDTriggeredScanResult(False, f'Error when initiating ScanWidget scan: {e}')
            return EtSTEDTriggeredScanResult(
                True,
                signal_dict=signal_dict,
                scan_info_dict=scan_info_dict,
            )

        if scan_initiation_mode == self.recording_widget_mode:
            if comm_channel is None:
                return EtSTEDTriggeredScanResult(False, 'RecordingWidget initiation requires a communication channel.')
            comm_channel.sigRequestScanFreq.emit()
            self.set_centers_scan_widget(analog_params, comm_channel)
            return EtSTEDTriggeredScanResult(True)

        return EtSTEDTriggeredScanResult(False, f'Unknown scan initiation mode: {scan_initiation_mode}')

    def trigger(self, scan_initiation_mode: str, nidaq_manager=None, signal_dict=None, scan_info_dict=None, comm_channel=None) -> EtSTEDTriggeredScanResult:
        """Trigger a previously prepared slow scan."""
        if scan_initiation_mode == self.scan_widget_mode:
            if nidaq_manager is None:
                return EtSTEDTriggeredScanResult(False, 'ScanWidget trigger requires a nidaq manager.')
            if signal_dict is None or scan_info_dict is None:
                return EtSTEDTriggeredScanResult(False, 'ScanWidget trigger requires prepared scan signals.')
            nidaq_manager.runScan(signal_dict, scan_info_dict)
            return EtSTEDTriggeredScanResult(True)

        if scan_initiation_mode == self.recording_widget_mode:
            if comm_channel is None:
                return EtSTEDTriggeredScanResult(False, 'RecordingWidget trigger requires a communication channel.')
            comm_channel.sigStartRecordingExternal.emit()
            return EtSTEDTriggeredScanResult(True)

        return EtSTEDTriggeredScanResult(False, f'Unknown scan initiation mode: {scan_initiation_mode}')

    def set_center_scan_parameter(
        self,
        analog_params: dict,
        positioners_scan: list,
        position: np.ndarray,
        positioners_manager=None,
        apply_fast_axis_shift: bool = False,
        fast_axis_shift_fn: Callable[[float], float] | None = None,
    ) -> None:
        """Set active scan-axis centers from event coordinates using loaded scan axis order."""
        scan_axis_indices = self._scan_axis_indices(analog_params)
        for event_axis, analog_index in enumerate(scan_axis_indices[:2]):
            center = float(position[event_axis])
            if event_axis == 0 and apply_fast_axis_shift:
                if fast_axis_shift_fn is None:
                    raise ValueError('Fast-axis shift was requested without a shift function.')
                center = fast_axis_shift_fn(center)
            analog_params['axis_centerpos'][analog_index] = center

        if positioners_manager is None:
            return

        for index, positioner_name in enumerate(analog_params['target_device']):
            if positioner_name != 'None' and positioner_name not in positioners_scan:
                positioner = positioners_manager[positioner_name]
                positioner.setPosition(analog_params['axis_centerpos'][index], 0)

    def set_centers_scan_widget(self, analog_params: dict, comm_channel) -> None:
        """Emit updated scan centers to the scan widget."""
        devices = []
        centers = []
        for device, center in zip(analog_params['target_device'], analog_params['axis_centerpos']):
            devices.append(device)
            centers.append(center)
        comm_channel.sigSetAxisCenters.emit(devices, centers)

    def _validate_position(self, position) -> np.ndarray:
        if position is None:
            position = [0.0, 0.0, 0.0]
        position = np.asarray(position, dtype=float)
        if position.size < 2:
            raise ValueError('Triggered etSTED scans require at least X/Y event coordinates.')
        return position

    def validate_scan_parameters(self, analog_params: dict, digital_params: dict, positioners_scan: list) -> None:
        if not analog_params:
            raise ValueError('No analog scan parameters are loaded.')
        if digital_params is None:
            raise ValueError('No digital scan parameters are loaded.')
        if positioners_scan is None:
            raise ValueError('No scan positioner list is loaded.')

        required_keys = ('target_device', 'axis_centerpos')
        missing = [key for key in required_keys if key not in analog_params]
        if missing:
            raise ValueError(f'Analog scan parameters missing keys: {", ".join(missing)}')

        if len(analog_params['target_device']) != len(analog_params['axis_centerpos']):
            raise ValueError('target_device and axis_centerpos must have the same length.')

        if len(self._scan_axis_indices(analog_params)) < 2:
            raise ValueError('Triggered etSTED scans require at least two active scan axes.')

    def _scan_axis_indices(self, analog_params: dict) -> list[int]:
        return [
            index
            for index, positioner_name in enumerate(analog_params['target_device'])
            if positioner_name != 'None'
        ]
