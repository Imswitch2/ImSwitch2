"""Dedicated detector settings in the Focus Lock widget."""

from types import SimpleNamespace

from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
)
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    DetectorAction,
    DetectorListParameter,
    DetectorNumberParameter,
)
from imswitch.imcontrol.view.widgets.FocusLockWidget import FocusLockWidget


class _Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):
        self.errors.append(message)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *args, **kwargs):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _TreeParam:
    def __init__(self, value=None):
        self._value = value
        self._children = {}
        self.sigValueChanged = _Signal()
        self.sigActivated = _Signal()

    def add(self, name, param):
        self._children[name] = param
        return param

    def param(self, name):
        return self._children[name]

    def setValue(self, value):
        self._value = value
        self.sigValueChanged.emit(self, value)

    def value(self):
        return self._value


class _Tree:
    def __init__(self, detector, detectorParameters=None, detectorActions=None):
        self.p = _TreeParam()
        detectorParameters = (
            detector.parameters if detectorParameters is None else detectorParameters
        )
        detectorActions = detector.actions if detectorActions is None else detectorActions
        frame = self.p.add('Image frame', _TreeParam())
        for name, value in (
            ('Binning', detector.binning),
            ('Mode', 'Custom'),
            ('X0', detector.frameStart[0]),
            ('Y0', detector.frameStart[1]),
            ('Width', detector.shape[0]),
            ('Height', detector.shape[1]),
            ('Apply', None),
            ('New ROI', None),
            ('Abort ROI', None),
        ):
            frame.add(name, _TreeParam(value))
        for parameterName, parameter in detectorParameters.items():
            try:
                group = self.p.param(parameter.group)
            except KeyError:
                group = self.p.add(parameter.group, _TreeParam())
            group.add(parameterName, _TreeParam(parameter.value))
        for actionName, action in detectorActions.items():
            try:
                group = self.p.param(action.group)
            except KeyError:
                group = self.p.add(action.group, _TreeParam())
            group.add(actionName, _TreeParam())


class _Widget:
    def __init__(self, detector):
        self._detector = detector
        self.created = None
        self.frameReadback = None

    def setFocusCameraSettings(self, detectorName, detectorModel,
                               detectorParameters, detectorActions,
                               supportedBinnings, roiInfos, croppable=True):
        self.created = {
            'name': detectorName,
            'model': detectorModel,
            'parameters': detectorParameters,
            'actions': detectorActions,
            'binnings': supportedBinnings,
            'rois': roiInfos,
            'croppable': croppable,
        }
        return _Tree(self._detector, detectorParameters, detectorActions)

    def updateFocusCameraFrameReadback(self, **kwargs):
        self.frameReadback = kwargs


class _Detector:
    def __init__(self):
        self.model = 'Focus model'
        self.supportedBinnings = [1, 2]
        self.binning = 1
        self.frameStart = (4, 5)
        self.shape = (76, 20)
        self.fullShape = (640, 480)
        self.croppable = True
        self.cropCalls = []
        self.setCalls = []
        self.actionCalls = 0
        self.parameters = {
            'Exposure': DetectorNumberParameter(
                group='Timings', value=0.01, editable=True, valueUnits='s'
            ),
            'Real exposure': DetectorNumberParameter(
                group='Timings', value=0.011, editable=False, valueUnits='s'
            ),
            'Trigger source': DetectorListParameter(
                group='Acquisition', value='Internal', editable=True,
                options=['Internal', 'External'],
            ),
            'ROI X0': DetectorNumberParameter(
                group='ROI', value=4, editable=True, valueUnits='px'
            ),
        }
        self.actions = {
            'Refresh': DetectorAction(group='Acquisition', func=self._refresh)
        }


    def crop(self, x, y, w, h):
        self.cropCalls.append((x, y, w, h))
        self.frameStart = (int(x), int(y))
        self.shape = (int(w), int(h))

    def setParameter(self, name, value):
        self.setCalls.append((name, value))
        self.parameters[name].value = value
        if name == 'Exposure':
            self.parameters['Real exposure'].value = value + 0.001
        return self.parameters

    def _refresh(self):
        self.actionCalls += 1
        self.parameters['Real exposure'].value += 0.001


class _DetectorsManager:
    def __init__(self, detector):
        self._detector = detector
        self._current = 'MainCam'

    def __getitem__(self, name):
        assert name == 'FocusCam'
        return self._detector

    def getCurrentDetectorName(self):
        return self._current


class _Master:
    def __init__(self, detector):
        self.detectorsManager = _DetectorsManager(detector)


def _makeController():
    detector = _Detector()
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._logger = _Logger()
    ctrl._widget = _Widget(detector)
    ctrl._master = _Master(detector)
    ctrl._setupInfo = SimpleNamespace(
        rois={'Small': object()},
        focusLock=SimpleNamespace(swapImageAxes=False),
    )
    ctrl.camera = 'FocusCam'
    ctrl._focusCameraSettingsTree = None
    ctrl._focusCameraSettingsUpdating = False
    ctrl._focusCameraPostStopAction = None
    ctrl._focusRoiEditing = False
    ctrl._focusRoiSession = None
    ctrl._focusRoiSelection = None
    ctrl._focusRoiDisplayShape = None
    ctrl._focusCalibrationActive = False
    ctrl._focusCameraStopPending = False
    ctrl._shutdownComplete = False
    ctrl.locked = False
    ctrl.aboutToLock = False
    ctrl._suspendedLock = False
    ctrl._FocusLockController__processDataThread = None
    ctrl.focusCameraAcquisitionActive = lambda: False
    return ctrl, detector


def test_focus_camera_tree_is_bound_without_switching_global_detector():
    ctrl, detector = _makeController()

    FocusLockController._setupFocusCameraSettings(ctrl)

    assert ctrl._widget.created['name'] == 'FocusCam'
    assert ctrl._widget.created['model'] == detector.model
    assert 'ROI X0' not in ctrl._widget.created['parameters']
    assert ctrl._master.detectorsManager.getCurrentDetectorName() == 'MainCam'
    assert ctrl._widget.frameReadback == {
        'detectorModel': 'Focus model',
        'binning': 1,
        'frameStart': (4, 5),
        'shape': (76, 20),
        'fullShape': (640, 480),
    }


def test_parameter_edit_writes_focus_camera_and_refreshes_related_readback():
    ctrl, detector = _makeController()
    FocusLockController._setupFocusCameraSettings(ctrl)
    tree = ctrl._focusCameraSettingsTree

    tree.p.param('Timings').param('Exposure').sigValueChanged.emit(None, 0.02)

    assert detector.setCalls == [('Exposure', 0.02)]
    assert tree.p.param('Timings').param('Exposure').value() == 0.02
    assert tree.p.param('Timings').param('Real exposure').value() == 0.021


def test_readonly_parameter_is_not_written_back():
    ctrl, detector = _makeController()
    FocusLockController._setupFocusCameraSettings(ctrl)
    tree = ctrl._focusCameraSettingsTree

    tree.p.param('Timings').param('Real exposure').sigValueChanged.emit(None, 99)

    assert detector.setCalls == []
    assert tree.p.param('Timings').param('Real exposure').value() == 0.011


def test_focus_camera_action_uses_current_manager_and_refreshes_values():
    ctrl, detector = _makeController()
    FocusLockController._setupFocusCameraSettings(ctrl)
    tree = ctrl._focusCameraSettingsTree

    tree.p.param('Acquisition').param('Refresh').sigActivated.emit()

    assert detector.actionCalls == 1
    assert tree.p.param('Timings').param('Real exposure').value() == 0.012


def test_focus_lock_widget_uses_two_tabs_and_exposes_safe_roi_controls(qtbot):
    widget = FocusLockWidget()
    qtbot.addWidget(widget)

    parameters = {
        'Exposure': DetectorNumberParameter(
            group='Timings', value=0.01, editable=True, valueUnits='s'
        )
    }
    tree = widget.setFocusCameraSettings(
        'FocusCam', 'Focus model', parameters, {}, [1, 2], {}
    )

    assert widget.tabs.count() == 2
    assert widget.tabs.tabText(0) == 'Focus lock'
    assert widget.tabs.tabText(1) == 'Camera'
    assert not hasattr(widget, 'camDialogButton')
    assert widget.webcamGraph.parentWidget() is widget
    assert tree.p.param('Model').value() == 'Focus model'

    frame = tree.p.param('Image frame')
    assert frame.param('Binning').opts.get('enabled') is False
    assert frame.param('Mode').opts.get('enabled') is False
    for name in ('X0', 'Y0', 'Width', 'Height', 'Apply', 'New ROI', 'Abort ROI'):
        assert frame.param(name).opts.get('enabled') is True
    for name in ('Save mode', 'Delete mode', 'Update all detectors'):
        assert frame.param(name).opts.get('visible') is False


def test_numeric_focus_roi_applies_hardware_readback_without_global_detector_switch():
    ctrl, detector = _makeController()
    FocusLockController._setupFocusCameraSettings(ctrl)
    tree = ctrl._focusCameraSettingsTree
    frame = tree.p.param('Image frame')
    for name, value in (('X0', 12), ('Y0', 16), ('Width', 100), ('Height', 80)):
        frame.param(name)._value = value

    persisted = []
    ctrl._persistFocusCameraRoi = lambda roi: persisted.append(tuple(roi))
    ctrl._refreshFocusCameraSettings = lambda: None
    ctrl._endFocusCameraRoiSession = lambda: None

    FocusLockController.applyFocusCameraROI(ctrl)

    assert detector.cropCalls == [(12, 16, 100, 80)]
    assert persisted == [(12, 16, 100, 80)]
    assert ctrl._master.detectorsManager.getCurrentDetectorName() == 'MainCam'


def test_graphical_roi_coordinate_mapping_handles_unswapped_camera_axes():
    ctrl, detector = _makeController()
    ctrl._focusRoiDisplayShape = (240, 320)
    ctrl._setupInfo.focusLock.swapImageAxes = False

    roi = FocusLockController._focusCameraDisplayRoiToSensor(
        ctrl, (24, 32), (48, 64)
    )

    # Raw camera frames are (sensor Y, sensor X), and FocusLock displays numpy
    # axis 0 as ViewBox X. Without swapImageAxes the graphical axes therefore
    # map back to sensor Y/X respectively.
    assert roi == (64, 48, 128, 96)


def test_graphical_roi_round_trip_preserves_sensor_geometry():
    ctrl, detector = _makeController()
    sensor = (40, 60, 200, 120)
    display = (320, 240)

    position, size = FocusLockController._focusCameraSensorRoiToDisplay(
        ctrl, sensor, display, detector.fullShape
    )
    ctrl._focusRoiDisplayShape = display
    round_trip = FocusLockController._focusCameraDisplayRoiToSensor(
        ctrl, position, size
    )

    assert round_trip == sensor
