"""A scan recording must be armed with the geometry of the scan that runs.

Pins the multi-scanner binding bug: on a rig with both a TriggerScope raster
widget and a TriggerScope RESOLFT widget, a scan-once recording was armed with
the raster's pixel grid no matter which scanner the operator then started,
because the raster was the only controller implementing the recording
accessors and the frame count was read at arm time.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.CommunicationChannel import (
    RECORDING_SCAN_SOURCE_METHODS,
    CommunicationChannel,
)
from imswitch.imcontrol.controller.controllers._triggerscope_scan_geometry import (
    TriggerScopeScanGeometryMixin,
)


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_DIR = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers'

#: Every TriggerScope controller a recording can be bound to, and whether it
#: reports its geometry through the shared RESOLFT-counter mixin. The raster
#: keeps its own: its frame count is a pixel grid, not these counters.
TRIGGERSCOPE_CONTROLLERS = {
    'TriggerScopeRasterController.py': (
        'TriggerScopeRasterController', False,
    ),
    'TriggerScopeScanController.py': ('TriggerScopeScanController', True),
    'TriggerScopePLSRController.py': ('TriggerScopePLSRController', True),
    'TriggerScopePLSRMulticolorController.py': (
        'TriggerScopePLSRMulticolorController', True,
    ),
    'TriggerScopeLSXYRController.py': ('TriggerScopeLSXYRController', True),
    'TriggerScopeGalvoDetectionController.py': (
        'TriggerScopeGalvoDetectionController', True,
    ),
    'LightSheetMulticolorController.py': (
        'LightSheetMulticolorController', True,
    ),
}


class _Controller(TriggerScopeScanGeometryMixin):
    def __init__(self, scanParameters, deviceParameters, detectors=()):
        self._scanParameterDict = dict(scanParameters)
        self._deviceParameterDict = dict(deviceParameters)
        self._setupInfo = SimpleNamespace(
            detectors={name: object() for name in detectors}
        )
        self.getParametersCalls = 0

    def getParameters(self):
        self.getParametersCalls += 1


def _controller(**scanParameters):
    return _Controller(
        {'roSteps': 5, 'cycleSteps': 3, 'timeLapsePoints': 2,
         **scanParameters},
        {'CameraTTL': 'Camera'},
        detectors=('Camera',),
    )


# --------------------------------------------------------------------------- #
# Frame count                                                                  #
# --------------------------------------------------------------------------- #

def test_frame_count_is_the_product_of_the_firmware_counters():
    controller = _controller()

    assert controller.getNumScanPositions() == 30
    # Read from the widget on every call: the operator can retune the scan
    # between arming a recording and starting it.
    assert controller.getParametersCalls == 1


@pytest.mark.parametrize('value', [0, -4, None, '', 'not-a-number'])
def test_an_unusable_counter_counts_as_one_step(value):
    """A missing or nonsensical counter must not collapse the whole
    expectation to zero, which would arm a recording for no frames at all."""
    controller = _controller(cycleSteps=value)

    assert controller.getNumScanPositions() == 10


def test_counters_restored_as_text_still_multiply():
    controller = _controller(roSteps='5', cycleSteps=3.0, timeLapsePoints='2')

    assert controller.getNumScanPositions() == 30


def test_a_missing_counter_key_is_a_single_step():
    controller = _Controller({'roSteps': 7}, {})

    assert controller.getNumScanPositions() == 7


# --------------------------------------------------------------------------- #
# Camera TTL map                                                               #
# --------------------------------------------------------------------------- #

def test_camera_ttl_reports_one_pulse_for_the_configured_camera():
    assert _controller().getNumCamTTL() == {'Camera': 1}


def test_camera_ttl_is_empty_without_a_camera_role():
    """pLS-RESOLFT and galvo-detection configure no CameraTTL device. An empty
    map means "nothing overrides the default", which the RecordingManager
    reads as one pulse per position."""
    controller = _Controller(
        {'roSteps': 2, 'cycleSteps': 2, 'timeLapsePoints': 1},
        {},
        detectors=('Camera',),
    )

    assert controller.getNumCamTTL() == {}


def test_camera_ttl_ignores_a_ttl_line_that_is_not_a_detector():
    controller = _Controller(
        {'roSteps': 1, 'cycleSteps': 1, 'timeLapsePoints': 1},
        {'CameraTTL': 'SomeTTLLine'},
        detectors=('Camera',),
    )

    assert controller.getNumCamTTL() == {}


# --------------------------------------------------------------------------- #
# Adoption                                                                     #
# --------------------------------------------------------------------------- #

def _classNode(path, className):
    module = ast.parse(path.read_text(encoding='utf-8'))
    return next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == className
    )


def test_every_triggerscope_controller_can_arm_a_recording():
    """Resolving the right scanner is worthless if it cannot answer. Each of
    these controllers must expose the full recording-source protocol."""
    offenders = []
    for filename, (className, usesMixin) in TRIGGERSCOPE_CONTROLLERS.items():
        classNode = _classNode(CONTROLLER_DIR / filename, className)
        bases = {
            base.id for base in classNode.bases if isinstance(base, ast.Name)
        }
        defined = {
            node.name for node in classNode.body
            if isinstance(node, ast.FunctionDef)
        }
        inherited = (
            {'getNumScanPositions', 'getNumCamTTL'}
            if 'TriggerScopeScanGeometryMixin' in bases else set()
        )
        missing = [
            name for name in RECORDING_SCAN_SOURCE_METHODS
            if name not in defined | inherited
        ]
        if missing or ('TriggerScopeScanGeometryMixin' in bases) != usesMixin:
            offenders.append(f'{className} (missing {missing})')

    assert offenders == [], (
        'TriggerScope scan controllers must be able to report their recording '
        'geometry: ' + ', '.join(offenders)
    )


def test_multi_mode_controller_reports_the_visible_mode():
    """The unified RESOLFT controller holds one parameter dict per mode, so a
    recording armed against it must read the mode that will actually run."""
    classNode = _classNode(
        CONTROLLER_DIR / 'TriggerScopeScanController.py',
        'TriggerScopeScanController',
    )
    override = next(
        (node for node in classNode.body
         if isinstance(node, ast.FunctionDef)
         and node.name == '_triggerScopeGeometryParameters'),
        None,
    )
    assert override is not None, (
        'TriggerScopeScanController must override the geometry hook; the '
        'controller-level parameter dicts the default reads do not exist here.'
    )
    body = ast.unparse(override)
    assert '_activeAdapter()' in body
    assert 'adapter.scanParameterDict' in body
    assert 'adapter.deviceParameterDict' in body


# --------------------------------------------------------------------------- #
# Source resolution                                                            #
# --------------------------------------------------------------------------- #

def _source():
    return SimpleNamespace(
        runScanExternal=lambda *_args: None,
        abortScan=lambda: None,
        getNumScanPositions=lambda: 1,
        getNumCamTTL=lambda: {},
    )


def _channel(controllers):
    return SimpleNamespace(
        _CommunicationChannel__main=SimpleNamespace(controllers=controllers)
    )


def test_an_explicit_choice_wins_over_registration_order():
    raster, resolft = _source(), _source()
    channel = _channel(
        {'TriggerScopeRaster': raster, 'TriggerScopeScan': resolft}
    )

    assert CommunicationChannel.getRecordingScanSource(
        channel, 'TriggerScopeScan'
    ) is resolft
    assert CommunicationChannel.getRecordingScanSource(
        channel, 'TriggerScopeRaster'
    ) is raster


def test_a_stale_choice_is_refused_rather_than_silently_replaced():
    channel = _channel({'TriggerScopeRaster': _source()})

    with pytest.raises(RuntimeError, match='TriggerScopeScan'):
        CommunicationChannel.getRecordingScanSource(
            channel, 'TriggerScopeScan'
        )


def test_several_capable_scanners_without_a_choice_stay_ambiguous():
    channel = _channel(
        {'TriggerScopeRaster': _source(), 'TriggerScopeScan': _source()}
    )

    with pytest.raises(RuntimeError, match='Cannot automate scan-lapse'):
        CommunicationChannel.getRecordingScanSource(channel)


class _FakeComboBox:
    """QComboBox selection semantics, without instantiating a Qt widget.

    Reproduces the behaviour the placeholder exists to defeat: ``clear()``
    followed by ``addItem`` leaves index 0 selected.
    """

    def __init__(self):
        self._items = []
        self._index = -1

    def clear(self):
        self._items = []
        self._index = -1

    def addItem(self, label, data):
        self._items.append((label, data))
        if self._index < 0:
            self._index = 0

    def findData(self, data):
        for index, (_label, itemData) in enumerate(self._items):
            if itemData == data:
                return index
        return -1

    def setCurrentIndex(self, index):
        self._index = index

    def currentIndex(self):
        return self._index

    def currentData(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None

    def blockSignals(self, _blocked):
        return False


def test_the_chooser_never_reports_a_pick_the_operator_did_not_make():
    """Behavioural counterpart to the source-level guard: clearing a combo box
    selects index zero, so a rig seen for the first time — or one whose saved
    scanner disappeared — must report no choice rather than the first entry."""
    from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget

    widget = _FakeComboBox()
    populate = RecordingWidget.setScanSourceOptions
    getSource = RecordingWidget.getScanSource
    shell = SimpleNamespace(
        scanSourceList=widget,
        SCAN_SOURCE_PLACEHOLDER=RecordingWidget.SCAN_SOURCE_PLACEHOLDER,
    )
    shell.getScanSource = lambda: getSource(shell)

    # Never chosen before.
    populate(shell, ['TriggerScopeRaster', 'TriggerScopeScan'])
    assert widget.currentIndex() == 0
    assert getSource(shell) == ''

    # An explicit choice is honoured and survives repopulation.
    widget.setCurrentIndex(widget.findData('TriggerScopeScan'))
    assert getSource(shell) == 'TriggerScopeScan'
    populate(shell, ['TriggerScopeRaster', 'TriggerScopeScan'])
    assert getSource(shell) == 'TriggerScopeScan'

    # The chosen scanner disappears: fall back to the placeholder, never to
    # the remaining scanner.
    populate(shell, ['TriggerScopeRaster'])
    assert getSource(shell) == ''


def test_capable_source_names_are_offered_for_the_chooser():
    channel = _channel({
        'TriggerScopeRaster': _source(),
        'TriggerScopeScan': _source(),
        'Positioner': SimpleNamespace(),
    })

    assert CommunicationChannel.getRecordingScanSourceNames(channel) == [
        'TriggerScopeRaster', 'TriggerScopeScan'
    ]
