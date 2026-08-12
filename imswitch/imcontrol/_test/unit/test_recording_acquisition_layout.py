from dataclasses import replace
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model import (
    ACQUISITION_LAYOUT_SCHEMA,
    AcquisitionLayout,
    AcquisitionLayoutError,
    AcquisitionLoop,
)
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
    build_point_scan_layouts,
)
from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)
from imswitch.imcontrol.model import (
    DetectorsManager,
    RecMode,
    RecordingManager,
    SaveMode,
)

from . import detectorInfosBasic


def _manager_with_prepare_counter():
    manager = RecordingManager(
        DetectorsManager(detectorInfosBasic, updatePeriod=100)
    )
    calls = []
    manager._RecordingManager__prepareRecordingThread = lambda: calls.append(
        True
    )
    return manager, calls


def _point_layout(*, detector="CAM", x=3, y=2):
    return build_point_scan_layouts(
        {
            "img_dims": [x, y],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [0.1, 0.2],
        },
        (detector,),
        scan_source="ScanControllerPointScan",
    )[detector]


def _start_that_must_fail(manager, layout, *, recFrames=6):
    manager.startRecording(
        detectorNames=["CAM"],
        recMode=RecMode.ScanOnce,
        savename="must_not_open",
        saveMode=SaveMode.RAM,
        attrs={"CAM": {}},
        recFrames=recFrames,
        numCamTTL={"CAM": 1},
        acquisitionLayouts={"CAM": layout},
    )


def test_scan_position_mismatch_is_rejected_before_prepare_or_writer_open():
    manager, prepareCalls = _manager_with_prepare_counter()

    with pytest.raises(AcquisitionLayoutError) as error:
        _start_that_must_fail(manager, _point_layout(), recFrames=5)

    assert {issue.code for issue in error.value.issues} >= {
        "SCAN_POSITION_COUNT_MISMATCH"
    }
    assert prepareCalls == []
    assert manager.record is False


def test_unclassifiable_loop_kind_does_not_block_recording():
    """Loop kinds are an open vocabulary; an unknown one must not veto a scan.

    The scan-position cross-check used to treat every kind outside a fixed
    ``{condition, repeat}`` exclusion list as advancing the scan, so a producer
    modelling a non-positional loop under any other name (a polarization state,
    for instance) was rejected before a writer could open.
    """
    manager, prepareCalls = _manager_with_prepare_counter()
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("scan_y", "scan_y", 2),
            AcquisitionLoop("polarization", "polarization", 2, labels=("H", "V")),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
        scan_source="WidefieldStarssWorkflow",
    )

    manager.startRecording(
        detectorNames=["CAM"],
        recMode=RecMode.ScanOnce,
        savename="unclassified_kind",
        saveMode=SaveMode.RAM,
        attrs={"CAM": {}},
        recFrames=6,
        numCamTTL={"CAM": 1},
        acquisitionLayouts={"CAM": layout},
    )

    assert prepareCalls == [True]


def test_layout_detector_mismatch_is_rejected_before_prepare():
    manager, prepareCalls = _manager_with_prepare_counter()

    with pytest.raises(AcquisitionLayoutError) as error:
        _start_that_must_fail(manager, _point_layout(detector="Other"))

    assert "DETECTOR_MISMATCH" in {
        issue.code for issue in error.value.issues
    }
    assert prepareCalls == []


def test_detector_pulse_mismatch_is_rejected_before_prepare():
    manager, prepareCalls = _manager_with_prepare_counter()
    layout = build_point_scan_layouts(
        {
            "img_dims": [3, 2],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [0.1, 0.2],
        },
        ("CAM",),
        scan_source="ScanControllerPointScan",
        pulse_counts={"CAM": 2},
    )["CAM"]

    with pytest.raises(AcquisitionLayoutError) as error:
        _start_that_must_fail(manager, layout)

    assert "DETECTOR_PULSE_COUNT_MISMATCH" in {
        issue.code for issue in error.value.issues
    }
    assert prepareCalls == []


def test_oversized_layout_is_rejected_before_prepare():
    manager, prepareCalls = _manager_with_prepare_counter()
    count = 30_000
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop(
                "condition",
                "condition",
                count,
                labels=tuple(
                    f"condition_{index:05d}_with_a_long_label"
                    for index in range(count)
                ),
            ),
        ),
    )

    with pytest.raises(AcquisitionLayoutError) as error:
        manager.startRecording(
            detectorNames=["CAM"],
            recMode=RecMode.SpecFrames,
            savename="must_not_open",
            saveMode=SaveMode.RAM,
            attrs={"CAM": {}},
            recFrames=count,
            acquisitionLayouts={"CAM": layout},
        )

    assert "LAYOUT_METADATA_TOO_LARGE" in {
        issue.code for issue in error.value.issues
    }
    assert prepareCalls == []


def test_layout_selection_is_authoritative_for_worker_frame_target():
    manager = RecordingManager(
        DetectorsManager(detectorInfosBasic, updatePeriod=100)
    )
    worker = manager._RecordingManager__recordingWorker
    layout = build_advanced_scan_layouts(
        {
            "img_dims": [3, 2],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [0.1, 0.2],
            "n_linesteps": 2,
        },
        ("CAM",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"CAM": [False, True]},
        pulse_counts_by_condition={"CAM": [1, 1]},
    )["CAM"]
    worker.recMode = RecMode.ScanOnce
    worker.acquisitionLayouts = {"CAM": layout}

    assert worker._expectedFramesFor("CAM", 6, {"CAM": 2}) == 6


def test_recording_controller_queries_only_its_pinned_layout_source():
    layout = _point_layout()

    class Source:
        def getNumScanPositions(self):
            return 6

        def getNumCamTTL(self):
            return {"CAM": 1}

        def getAcquisitionLayouts(self, detectorNames):
            assert tuple(detectorNames) == ("CAM",)
            return {"CAM": layout}

    failures = []
    controller = SimpleNamespace(
        recMode=RecMode.ScanOnce,
        recordingArgs={"detectorNames": ["CAM"]},
        _recordingScanSource=Source(),
        _producerAcquisitionLayouts=None,
        _commChannel=SimpleNamespace(
            getAcquisitionLayouts=lambda *_args: pytest.fail(
                "global layout accessor must not be used for a pinned source"
            )
        ),
        _handleRecordingFailure=lambda message, **_kwargs: failures.append(
            message
        ),
    )
    controller._scanAccessor = RecordingController._scanAccessor.__get__(
        controller
    )
    controller._scanDimsForRecording = (
        RecordingController._scanDimsForRecording.__get__(controller)
    )
    controller._scanStepSizesForRecording = (
        RecordingController._scanStepSizesForRecording.__get__(controller)
    )

    assert RecordingController._applyScanGeometryToRecordingArgs(controller)
    assert failures == []
    assert controller.recordingArgs["acquisitionLayouts"] == {"CAM": layout}


def test_recording_controller_keeps_legacy_source_compatibility():
    source = SimpleNamespace(
        getNumScanPositions=lambda: 6,
        getNumCamTTL=lambda: {"CAM": 1},
    )
    controller = SimpleNamespace(
        recMode=RecMode.ScanOnce,
        recordingArgs={
            "detectorNames": ["CAM"],
            "acquisitionLayouts": {"stale": _point_layout()},
        },
        _recordingScanSource=source,
        _producerAcquisitionLayouts={"stale": _point_layout()},
        _commChannel=SimpleNamespace(),
        _handleRecordingFailure=lambda *_args, **_kwargs: pytest.fail(
            "legacy source should remain recordable"
        ),
    )
    controller._scanAccessor = RecordingController._scanAccessor.__get__(
        controller
    )
    controller._scanDimsForRecording = (
        RecordingController._scanDimsForRecording.__get__(controller)
    )
    controller._scanStepSizesForRecording = (
        RecordingController._scanStepSizesForRecording.__get__(controller)
    )

    assert RecordingController._applyScanGeometryToRecordingArgs(controller)
    assert "acquisitionLayouts" not in controller.recordingArgs
    assert controller._producerAcquisitionLayouts is None


def test_recording_controller_reindexes_the_layout_for_each_lapse_partition():
    layout = _point_layout()
    controller = SimpleNamespace(
        recMode=RecMode.ScanLapse,
        lapseCurrent=0,
        lapseTotal=2,
        recordingArgs={"singleLapseFile": True},
        _producerAcquisitionLayouts={"CAM": layout},
    )

    RecordingController._applyAcquisitionLayoutPartitions(controller)
    first = controller.recordingArgs["acquisitionLayouts"]["CAM"]
    controller.lapseCurrent = 1
    RecordingController._applyAcquisitionLayoutPartitions(controller)
    second = controller.recordingArgs["acquisitionLayouts"]["CAM"]

    assert first.partitions[0].index == 0
    assert second.partitions[0].index == 1
    assert replace(second, partitions=first.partitions) == first
