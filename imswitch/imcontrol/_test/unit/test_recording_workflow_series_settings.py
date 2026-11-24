"""Shared scan-series cadence owned by RecordingWorkflowService."""

import pytest

from imswitch.imcontrol.controller.WorkflowServices import (
    RecordingWorkflowService,
    ScanSeriesSettings,
)


class _Recording:
    def __init__(self):
        self.calls = []
        self.stops = []

    def recordScanSeries(
        self, source, detector_names, frames, interval_s, single_file=True
    ):
        self.calls.append(
            (source, tuple(detector_names), frames, interval_s, single_file)
        )
        return 'accepted'

    def stopScanSeries(self, source):
        self.stops.append(source)
        return True


class _Channel:
    def __init__(self):
        self.recording = _Recording()

    def controllerRegistry(self):
        return {'Recording': self.recording}


def test_scan_series_settings_are_runtime_shared_state():
    service = RecordingWorkflowService(_Channel())
    assert service.get_scan_series_settings() == ScanSeriesSettings(1, 0.0, True)

    seen = []
    service.sigScanSeriesSettingsChanged.connect(seen.append)
    updated = service.set_scan_series_settings(timepoints=7, interval_s=2.5)

    assert updated == ScanSeriesSettings(7, 2.5, True)
    assert service.get_scan_series_settings() == updated
    assert seen == [updated]


def test_scan_series_settings_validation_is_centralized():
    service = RecordingWorkflowService(_Channel())
    for kwargs in (
        {'timepoints': 0},
        {'timepoints': 2.5},
        {'interval_s': -0.1},
        {'interval_s': float('inf')},
    ):
        with pytest.raises(ValueError):
            service.set_scan_series_settings(**kwargs)
    assert service.get_scan_series_settings() == ScanSeriesSettings(1, 0.0, True)


def test_record_scan_series_snapshots_shared_settings_into_recording():
    channel = _Channel()
    service = RecordingWorkflowService(channel)
    source = object()
    service.set_scan_series_settings(
        timepoints=4, interval_s=3.25, single_file=False
    )

    assert service.record_scan_series(source, ['APD']) == 'accepted'
    assert channel.recording.calls == [
        (source, ('APD',), 4, 3.25, False)
    ]


def test_one_shot_request_preserves_shared_timelapse_configuration():
    channel = _Channel()
    service = RecordingWorkflowService(channel)
    source = object()
    service.set_scan_series_settings(
        timepoints=8, interval_s=1.5, single_file=False
    )

    assert service.record_scan_series(
        source, ['APD'], use_series=False
    ) == 'accepted'
    assert channel.recording.calls == [
        (source, ('APD',), 1, 0.0, False)
    ]
    assert service.get_scan_series_settings() == ScanSeriesSettings(
        8, 1.5, False
    )


def test_series_activity_and_stop_are_source_scoped():
    channel = _Channel()
    service = RecordingWorkflowService(channel)
    source = object()
    other = object()
    activity = []
    service.sigScanSeriesActivityChanged.connect(
        lambda owner, active: activity.append((owner, active))
    )

    service.publish_scan_series_activity(source, True)
    assert service.scan_series_active_for(source)
    assert not service.scan_series_active_for(other)
    assert service.stop_scan_series(source) is True
    assert channel.recording.stops == [source]

    service.publish_scan_series_activity(other, False)
    assert service.scan_series_active_for(source)
    service.publish_scan_series_activity(source, False)
    assert not service.scan_series_active_for(source)
    assert activity == [(source, True), (source, False)]
