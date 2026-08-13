"""
Unit tests for SettingsController unified state persistence.

These tests drive the REAL SettingsController against the REAL SettingsWidget
(pyqtgraph parameter trees with live signal connections) on top of fake detector
managers that record every hardware call.

That matters: an earlier version of these tests used Mock() parameter objects,
so no Qt signal ever fired and no manager was ever touched. They asserted only
that the widget fields had been written, and passed while ROI restore reached no
detector at all. Assert on the DETECTOR (crop/setBinning/setParameter), and use
the widget only to check it ends up mirroring hardware.

Covered:
- getComponentState snapshots hardware, not un-applied widget edits
- applyComponentState crops/bins/parameterizes each detector directly
- restoring one detector does not disturb another's restored frame
- read-only (camera-reported) parameters are neither saved nor written back
- non-croppable detectors (APD/PMT) are handled without spurious warnings
- STARTUP_RESTORE and SETUP_MODE_APPLY behave identically; neither acquires
- describeComponentState / getComponentStateHazards payload handling
"""

import pytest
from unittest.mock import MagicMock

from qtpy import QtWidgets

from imswitch.imcommon.model import getWidgetStatePersistence
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.SettingsController import SettingsController
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    CAMERA_PIXEL_SIZE_PARAM, DetectorListParameter, DetectorNumberParameter,
)
from imswitch.imcontrol.view.widgets.SettingsWidget import SettingsWidget


@pytest.fixture
def qapp():
    """Ensure QApplication exists."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture(autouse=True)
def isolatePersistenceRegistry():
    """Constructing a real SettingsController registers it in the global
    persistence singleton; leaving it there leaks into other test modules."""
    yield
    getWidgetStatePersistence().unregister('Settings')


class FakeDetector:
    """Minimal DetectorManager stand-in that records hardware calls."""

    def __init__(self, name, croppable=True, fullShape=(2048, 2048)):
        self.name = name
        self.model = f'Model-{name}'
        self.forAcquisition = True
        self.croppable = croppable
        self.supportedBinnings = [1, 2, 4]
        self.binning = 1
        self.fullShape = fullShape
        self.frameStart = (0, 0)
        self.shape = fullShape
        self.pixelSizeUm = [1.0, 0.1, 0.1]
        self.actions = {}
        self.parameters = {
            'Set exposure time': DetectorNumberParameter(
                group='Timings', value=0.01, valueUnits='s', editable=True),
            # Camera-reported reading, not a setting.
            'Real exposure time': DetectorNumberParameter(
                group='Timings', value=0.0105, valueUnits='s', editable=False),
            'Trigger source': DetectorListParameter(
                group='Acquisition mode', value='Internal trigger',
                options=['Internal trigger', 'External "start-trigger"',
                         'External "frame-trigger"'],
                editable=True),
            CAMERA_PIXEL_SIZE_PARAM: DetectorNumberParameter(
                group='Miscellaneous', value=0.15, valueUnits='µm', editable=True),
        }
        self.calls = []

    def crop(self, hpos, vpos, hsize, vsize):
        self.calls.append(('crop', hpos, vpos, hsize, vsize))
        self.frameStart = (hpos, vpos)
        self.shape = (hsize, vsize)

    def setBinning(self, binning):
        self.calls.append(('setBinning', binning))
        self.binning = binning

    def setParameter(self, name, value):
        self.calls.append(('setParameter', name, value))
        self.parameters[name].value = value
        # A real camera re-reports its derived timings when the exposure is set
        # (cf. HamamatsuManager._updatePropertiesFromCamera).
        if name == 'Set exposure time':
            self.parameters['Real exposure time'].value = round(value * 1.05, 6)
        return self.parameters

    def cropCalls(self):
        return [call for call in self.calls if call[0] == 'crop']

    def parameterCalls(self):
        return [call for call in self.calls if call[0] == 'setParameter']


class FakeDetectorsManager:
    def __init__(self, detectors):
        self._detectors = {detector.name: detector for detector in detectors}
        self._current = detectors[0].name

    def hasDevices(self):
        return True

    def __iter__(self):
        return iter(self._detectors.items())

    def __getitem__(self, name):
        return self._detectors[name]

    def getAllDeviceNames(self):
        return list(self._detectors)

    def getCurrentDetectorName(self):
        return self._current

    def setCurrentDetector(self, name):
        self._current = name

    def execOn(self, name, func):
        return func(self._detectors[name])

    def execOnCurrent(self, func):
        return func(self._detectors[self._current])

    def execOnAll(self, func, condition=None):
        return {name: func(detector) for name, detector in self._detectors.items()
                if condition is None or condition(detector)}


def makeController(detectors):
    master = MagicMock()
    master.detectorsManager = FakeDetectorsManager(detectors)

    setupInfo = MagicMock()
    setupInfo.rois = {}

    commChannel = MagicMock()
    commChannel.getCenterViewbox.return_value = (1024, 1024)

    widget = SettingsWidget(options=MagicMock())
    controller = SettingsController(
        setupInfo, commChannel, master,
        widget=widget, factory=MagicMock(), moduleCommChannel=MagicMock(),
    )
    return controller


@pytest.fixture
def cameras(qapp):
    """Two cameras; Camera1 is the current (displayed) detector."""
    camera1 = FakeDetector('Camera1')
    camera2 = FakeDetector('Camera2')
    controller = makeController([camera1, camera2])
    for camera in (camera1, camera2):
        camera.calls.clear()
    return controller, camera1, camera2


@pytest.fixture
def settings_controller(cameras):
    controller, _, _ = cameras
    return controller


def savedState(detectorName='Camera1', **overrides):
    detector_state = {
        'binning': 2,
        'frame_mode': 'Custom',
        'x0': 512, 'y0': 256, 'width': 400, 'height': 300,
        'parameters': {
            'Set exposure time': 0.02,
            'Trigger source': 'External "frame-trigger"',
        },
    }
    detector_state.update(overrides)
    return {'detectors': {detectorName: detector_state}}


# --- snapshot -------------------------------------------------------------

def test_get_component_state_reads_hardware(cameras):
    """The snapshot reflects the detector, not the widget."""
    controller, camera1, _ = cameras
    camera1.binning = 4
    camera1.frameStart = (64, 32)
    camera1.shape = (256, 128)
    camera1.parameters['Set exposure time'].value = 0.05

    state = controller.getComponentState()['detectors']['Camera1']

    assert state['binning'] == 4
    assert (state['x0'], state['y0'], state['width'], state['height']) == (64, 32, 256, 128)
    assert state['parameters']['Set exposure time'] == 0.05
    assert state['frame_mode'] == 'Full chip'


def test_get_component_state_ignores_unapplied_widget_edits(cameras):
    """ROI typed into the widget but never applied must not be persisted."""
    controller, camera1, _ = cameras
    params = controller.allParams['Camera1']
    params.x0.setValue(999)
    params.width.setValue(111)

    state = controller.getComponentState()['detectors']['Camera1']

    assert (state['x0'], state['width']) == (*camera1.frameStart[:1], camera1.shape[0])


def test_get_component_state_omits_read_only_parameters(cameras):
    """Camera-reported readings are not settings and are not persisted."""
    controller, _, _ = cameras

    parameters = controller.getComponentState()['detectors']['Camera1']['parameters']

    assert 'Real exposure time' not in parameters
    assert 'Set exposure time' in parameters


# --- restore reaches hardware --------------------------------------------

@pytest.mark.parametrize('applyMode', [
    ComponentStateApplyMode.STARTUP_RESTORE,
    ComponentStateApplyMode.SETUP_MODE_APPLY,
])
def test_apply_component_state_crops_the_detector(cameras, applyMode):
    """The regression that started this: the saved ROI must reach crop()."""
    controller, camera1, _ = cameras

    warnings = controller.applyComponentState(savedState(), applyMode=applyMode)

    assert warnings == []
    assert camera1.cropCalls() == [('crop', 512, 256, 400, 300)]
    assert camera1.frameStart == (512, 256)
    assert camera1.shape == (400, 300)


def test_apply_component_state_sets_binning_on_the_saved_detector(cameras):
    """Binning must land on the restored detector, not the displayed one."""
    controller, camera1, camera2 = cameras

    controller.applyComponentState(
        savedState('Camera2', binning=4),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    assert camera2.binning == 4
    assert camera1.binning == 1
    assert ('setBinning', 4) in camera2.calls
    assert camera1.calls == []


def test_apply_component_state_applies_trigger_mode(cameras):
    """Trigger mode is a setting and must be pushed to the manager."""
    controller, camera1, _ = cameras

    controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert ('setParameter', 'Trigger source', 'External "frame-trigger"') in camera1.calls
    assert camera1.parameters['Trigger source'].value == 'External "frame-trigger"'


def test_failed_trigger_restore_warns_and_widget_keeps_hardware_value(cameras):
    """A refused trigger write must not leave the settings tree displaying the
    saved value as though the camera accepted it."""
    controller, camera1, _ = cameras
    originalSetParameter = camera1.setParameter

    def rejectingSetParameter(name, value):
        if name == 'Trigger source':
            camera1.calls.append(('setParameter', name, value))
            raise RuntimeError('camera rejected trigger write')
        return originalSetParameter(name, value)

    camera1.setParameter = rejectingSetParameter

    warnings = controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    shown = controller._widget.trees['Camera1'].p.param(
        'Acquisition mode').param('Trigger source').value()
    assert any('Trigger source' in warning and 'camera rejected' in warning
               for warning in warnings)
    assert camera1.parameters['Trigger source'].value == 'Internal trigger'
    assert shown == 'Internal trigger'


def test_apply_component_state_skips_read_only_parameters(cameras):
    """Legacy files still carry camera readings; they must be ignored, and the
    hardware value refreshed rather than overwritten with a stale number."""
    controller, camera1, _ = cameras
    state = savedState()
    state['detectors']['Camera1']['parameters']['Real exposure time'] = 9.99

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert warnings == []
    assert all(call[1] != 'Real exposure time' for call in camera1.parameterCalls())
    # Refreshed from the restored exposure (0.02 * 1.05), not the saved 9.99.
    assert camera1.parameters['Real exposure time'].value == pytest.approx(0.021)


def test_apply_component_state_does_not_disturb_other_detectors(cameras):
    """Restoring Camera2 must not rewrite Camera1's already-restored frame.

    Previously frameMode.setValue() fired updateFrame() on the *displayed*
    detector, which opened a fresh 64x64 ROI overlay and wrote its geometry over
    Camera1's fields via ROIchanged()/getCurrentParams().
    """
    controller, camera1, camera2 = cameras

    controller.applyComponentState(
        savedState('Camera1'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    controller.applyComponentState(
        savedState('Camera2', x0=100, y0=100, width=200, height=200),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert camera1.frameStart == (512, 256)
    assert camera1.shape == (400, 300)
    params = controller.allParams['Camera1']
    assert (params.x0.value(), params.y0.value(),
            params.width.value(), params.height.value()) == (512, 256, 400, 300)


def test_widget_mirrors_hardware_after_restore(cameras):
    """The widget is a view of the resulting hardware state."""
    controller, camera1, _ = cameras

    controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    params = controller.allParams['Camera1']
    assert params.binning.value() == camera1.binning
    assert (params.x0.value(), params.y0.value()) == camera1.frameStart
    assert (params.width.value(), params.height.value()) == camera1.shape
    assert params.frameMode.value() == 'Custom'


def test_restore_survives_hardware_clamping(cameras):
    """When the detector snaps the ROI, the widget shows what hardware took."""
    controller, camera1, _ = cameras

    def snappingCrop(hpos, vpos, hsize, vsize):
        camera1.calls.append(('crop', hpos, vpos, hsize, vsize))
        camera1.frameStart = (hpos, vpos)
        camera1.shape = (hsize - 4, vsize - 4)  # driver step granularity
    camera1.crop = snappingCrop

    controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    params = controller.allParams['Camera1']
    assert (params.width.value(), params.height.value()) == (396, 296)


def test_round_trip_restores_exactly(cameras):
    """snapshot -> drift -> restore must reproduce the original hardware state."""
    controller, camera1, _ = cameras
    camera1.setBinning(2)
    camera1.crop(512, 256, 400, 300)
    camera1.setParameter('Set exposure time', 0.02)
    snapshot = controller.getComponentState()

    camera1.setBinning(1)
    camera1.crop(0, 0, 2048, 2048)
    camera1.setParameter('Set exposure time', 0.5)

    warnings = controller.applyComponentState(
        snapshot, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert warnings == []
    assert camera1.binning == 2
    assert camera1.frameStart == (512, 256)
    assert camera1.shape == (400, 300)
    assert camera1.parameters['Set exposure time'].value == 0.02


def test_apply_modes_behave_identically(cameras):
    """STARTUP_RESTORE and SETUP_MODE_APPLY produce the same hardware calls."""
    controller, camera1, camera2 = cameras

    controller.applyComponentState(
        savedState('Camera1'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    startupCalls = list(camera1.calls)

    controller.applyComponentState(
        savedState('Camera2'), applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    setupModeCalls = list(camera2.calls)

    assert startupCalls == setupModeCalls


def test_apply_component_state_never_acquires(cameras):
    """Neither mode may start acquisition or live view."""
    controller, camera1, _ = cameras

    for applyMode in ComponentStateApplyMode:
        controller.applyComponentState(savedState(), applyMode=applyMode)

    forbidden = {'startAcquisition', 'stopAcquisition', 'startLive', 'flushBuffers'}
    assert not any(call[0] in forbidden for call in camera1.calls)


# --- a detector that refuses the ROI --------------------------------------

def test_adjust_frame_reports_a_refused_roi_instead_of_raising(cameras):
    """crop() raises when the camera rejects the ROI outright. adjustFrame runs
    from __init__'s execOnAll, so letting that propagate would abort controller
    construction and stop ImSwitch from starting."""
    controller, camera1, _ = cameras

    def refusingCrop(hpos, vpos, hsize, vsize):
        raise RuntimeError('camera remained at full frame')
    camera1.crop = refusingCrop

    failures = controller.adjustFrame(detector=camera1)

    assert [name for name, _ in failures] == ['Camera1']
    assert 'full frame' in failures[0][1]


def test_widget_shows_hardware_geometry_after_a_refused_roi(cameras):
    """The frame fields must fall back to what the detector really has, not
    keep displaying an ROI that was never applied."""
    controller, camera1, _ = cameras
    camera1.frameStart = (0, 0)
    camera1.shape = (2048, 2048)

    def refusingCrop(hpos, vpos, hsize, vsize):
        raise RuntimeError('camera remained at full frame')
    camera1.crop = refusingCrop

    params = controller.allParams['Camera1']
    params.x0.setValue(512)
    params.width.setValue(400)
    controller.adjustFrame(detector=camera1)

    assert (params.x0.value(), params.width.value()) == (0, 2048)


def test_restore_warnings_distinguish_skipped_from_hardware_failures(cameras):
    """A saved detector this setup doesn't have is routine; a setting the
    hardware refused is not. Only the latter should reach the operator."""
    from imswitch.imcommon.model import isCriticalRestoreWarning
    controller, camera1, _ = cameras

    warnings = controller.applyComponentState(
        savedState('NonExistentCam'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert warnings and not any(isCriticalRestoreWarning(w) for w in warnings)

    def refusingCrop(hpos, vpos, hsize, vsize):
        raise RuntimeError('camera remained at full frame')
    camera1.crop = refusingCrop

    warnings = controller.applyComponentState(
        savedState('Camera1'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert any(isCriticalRestoreWarning(w) for w in warnings)


# --- edge cases -----------------------------------------------------------

def test_non_croppable_detector_is_not_cropped(qapp):
    """APD/PMT/TimeTagger derive their shape from the scan; no ROI to restore."""
    apd = FakeDetector('APD', croppable=False)
    controller = makeController([apd])
    apd.calls.clear()

    warnings = controller.applyComponentState(
        savedState('APD'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert warnings == []
    assert apd.cropCalls() == []
    assert apd.binning == 2


def test_apply_component_state_missing_detector_warning(settings_controller):
    """A detector absent from the current setup is reported, not fatal."""
    warnings = settings_controller.applyComponentState(
        savedState('NonExistentCam'),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    assert any('NonExistentCam' in warning and 'not present' in warning
               for warning in warnings)


def test_apply_component_state_unknown_parameter_warns(cameras):
    """A parameter that no longer exists on the detector is reported."""
    controller, camera1, _ = cameras
    state = savedState()
    state['detectors']['Camera1']['parameters']['Gone'] = 1

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert any('Gone' in warning for warning in warnings)
    assert camera1.frameStart == (512, 256)  # rest of the restore still applied


def test_apply_component_state_partial_roi_is_skipped(cameras):
    """An incomplete ROI must not be half-applied."""
    controller, camera1, _ = cameras
    state = savedState()
    del state['detectors']['Camera1']['height']

    controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert camera1.cropCalls() == []


# --- payload-only surface -------------------------------------------------

def test_describe_component_state(settings_controller):
    """describeComponentState returns a human-readable summary."""
    state = {
        'detectors': {
            'Camera1': {
                'binning': 2, 'frame_mode': 'Custom',
                'x0': 100, 'y0': 100, 'width': 256, 'height': 256,
                'parameters': {'Trigger source': 'External'},
            }
        }
    }

    summary = settings_controller.describeComponentState(state)

    assert any('Camera1' in line for line in summary)
    assert any('mode: Custom' in line for line in summary)
    assert any('ROI:' in line and '100' in line and '256' in line for line in summary)
    assert any('binning: 2' in line for line in summary)
    assert any('Trigger source' in line and 'External' in line for line in summary)


def test_describe_component_state_empty(settings_controller):
    """describeComponentState handles empty state."""
    summary = settings_controller.describeComponentState({'detectors': {}})

    assert len(summary) == 1
    assert 'no detector state' in summary[0]


# --- setup-file-owned parameters -----------------------------------------

@pytest.fixture
def configuredCameras(qapp):
    """A camera whose pixel size the setup file declares (0.082 µm)."""
    camera = FakeDetector('Camera1')
    camera.parameters[CAMERA_PIXEL_SIZE_PARAM].value = 0.082
    camera.configOwnedParameters = frozenset({CAMERA_PIXEL_SIZE_PARAM})
    controller = makeController([camera])
    camera.calls.clear()
    return controller, camera


def test_config_owned_parameter_is_not_persisted(configuredCameras):
    """A configured pixel size is optical calibration, not a runtime setting."""
    controller, _ = configuredCameras

    parameters = controller.getComponentState()['detectors']['Camera1']['parameters']

    assert CAMERA_PIXEL_SIZE_PARAM not in parameters
    assert 'Set exposure time' in parameters


def test_unconfigured_pixel_size_is_still_persisted(cameras):
    """Without cameraPixelSizeUm the value is the user's, so it must survive."""
    controller, _, _ = cameras

    parameters = controller.getComponentState()['detectors']['Camera1']['parameters']

    assert parameters[CAMERA_PIXEL_SIZE_PARAM] == 0.15


def test_stale_snapshot_cannot_override_the_configured_pixel_size(configuredCameras):
    """The bug: every recording was calibrated with the previous session's
    pixel size, because snapshots written before the setup file was edited
    still carried it and restore wrote them straight back."""
    controller, camera = configuredCameras

    warnings = controller.applyComponentState(
        savedState(parameters={CAMERA_PIXEL_SIZE_PARAM: 0.15}),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    assert warnings == []
    assert camera.parameters[CAMERA_PIXEL_SIZE_PARAM].value == 0.082
    assert (CAMERA_PIXEL_SIZE_PARAM
            not in [call[1] for call in camera.parameterCalls()])


def test_widget_keeps_showing_the_configured_pixel_size(configuredCameras):
    """The display and the detector must not be allowed to disagree."""
    controller, camera = configuredCameras

    controller.applyComponentState(
        savedState(parameters={CAMERA_PIXEL_SIZE_PARAM: 0.15}),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    shown = controller._widget.trees['Camera1'].p.param('Miscellaneous').param(
        CAMERA_PIXEL_SIZE_PARAM).value()
    assert shown == pytest.approx(0.082)
    assert shown == pytest.approx(camera.parameters[CAMERA_PIXEL_SIZE_PARAM].value)


# --- the widget must show what the detector holds ------------------------

def displayedValue(param):
    """What the operator actually sees, as opposed to what the param holds.

    pyqtgraph keeps a parameter and its tree item's editor in sync only via
    ``sigValueChanged``. A readback that suppresses that signal (``setValue(v,
    blockSignal=True)``) updates the parameter and leaves the spinbox on screen
    showing the previous value -- so asserting on ``param.value()`` passes while
    the panel misreports the hardware. Assert on this instead.
    """
    item = list(param.items.keys())[0]
    widget = item.widget
    return widget.currentText() if hasattr(widget, 'currentText') else widget.value()


def test_restore_updates_what_the_operator_sees(cameras):
    """Regression: the restore reached hardware but never repainted the panel.

    The ROI stayed applied across a restart while the fields still showed the
    pre-restore geometry.
    """
    controller, camera1, _ = cameras

    controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    params = controller.allParams['Camera1']
    assert (displayedValue(params.x0), displayedValue(params.y0)) == camera1.frameStart
    assert (displayedValue(params.width), displayedValue(params.height)) == camera1.shape
    assert int(displayedValue(params.binning)) == camera1.binning
    assert displayedValue(params.frameMode) == 'Custom'


def test_restored_display_does_not_push_stale_values_at_hardware(cameras):
    """An edit after a restore must build on the restored value, not the old one.

    While the fields showed pre-restore values, the embedded spinbox held them
    too -- so the next edit sent that stale number to the detector.
    """
    controller, camera1, _ = cameras
    controller.applyComponentState(
        savedState(), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
    camera1.calls.clear()

    exposure = controller._widget.trees['Camera1'].p.param('Timings').param(
        'Set exposure time')
    assert displayedValue(exposure) == pytest.approx(0.02)

    list(exposure.items.keys())[0].widget.setValue(0.05)

    assert ('setParameter', 'Set exposure time', 0.05) in camera1.parameterCalls()
    assert camera1.parameters['Set exposure time'].value == pytest.approx(0.05)


def test_restore_readback_does_not_write_back_to_hardware(cameras):
    """Refreshing the panel from the detector must stay a pure read.

    The params emit "user intent" whoever set them, so an unguarded readback
    re-enters the write handlers -- which resolve their target through the
    *displayed* detector, not the one being restored.
    """
    controller, camera1, camera2 = cameras

    controller.applyComponentState(
        savedState('Camera2'), applyMode=ComponentStateApplyMode.STARTUP_RESTORE)

    assert camera1.calls == []
    assert camera2.cropCalls() == [('crop', 512, 256, 400, 300)]
    assert [call for call in camera2.calls if call[0] == 'setBinning'] == [('setBinning', 2)]


def test_configured_pixel_size_stays_on_screen_too(configuredCameras):
    """The config-owned value must win in the panel, not just in the manager."""
    controller, camera = configuredCameras

    controller.applyComponentState(
        savedState(parameters={CAMERA_PIXEL_SIZE_PARAM: 0.15}),
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    shown = displayedValue(controller._widget.trees['Camera1'].p.param(
        'Miscellaneous').param(CAMERA_PIXEL_SIZE_PARAM))
    assert shown == pytest.approx(0.082)
    assert shown == pytest.approx(camera.parameters[CAMERA_PIXEL_SIZE_PARAM].value)


def test_get_component_state_hazards_returns_empty(settings_controller):
    """Detector settings are passive configuration; no hazards in either mode."""
    state = savedState()

    for applyMode in ComponentStateApplyMode:
        assert settings_controller.getComponentStateHazards(
            state, applyMode=applyMode, context=None) == []


def test_component_name_and_schema_version():
    """SettingsController declares the canonical component identity."""
    assert SettingsController.componentName == 'Settings'
    assert SettingsController.stateSchemaVersion == 1
    assert SettingsController.legacyStateNames == ()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
