"""getDetectorParameter / getDetectorParameters: scripts can read what they set.

Without a getter a script that changed the exposure could not put the user's
value back; the basic tutorial had to hard-code the mock camera's start value.

These tests run the REAL SettingsController and SettingsWidget on a REAL
DetectorsManager holding the simulated camera of the mock setups (AVManager,
'exposure' in ms), so a value read back is the one the manager and the widget
hold, not one a fake invented.
"""
import threading

import pytest
from unittest.mock import MagicMock

from imswitch.imcommon.model import generateAPI, getWidgetStatePersistence
from imswitch.imcontrol.controller.controllers.SettingsController import SettingsController
from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
from imswitch.imcontrol.model.managers.MultiManager import NoSuchSubManagerError
from imswitch.imcontrol.view.widgets.SettingsWidget import SettingsWidget


@pytest.fixture
def settings(qtbot):
    """(controller, detectorsManager) for one simulated camera, "Camera"."""
    detectorsManager = DetectorsManager(
        {'Camera': DetectorInfo(
            managerName='AVManager', forAcquisition=True,
            managerProperties={'cameraListIndex': 'mock',
                               'avcam': {'exposure': 100, 'gain': 1}},
        )},
        updatePeriod=100,
    )
    master = MagicMock()
    master.detectorsManager = detectorsManager
    setupInfo = MagicMock()
    setupInfo.rois = {}
    commChannel = MagicMock()
    commChannel.getCenterViewbox.return_value = (400, 400)

    widget = SettingsWidget(options=MagicMock())
    qtbot.addWidget(widget)
    controller = SettingsController(
        setupInfo, commChannel, master,
        widget=widget, factory=MagicMock(), moduleCommChannel=MagicMock(),
    )
    yield controller, detectorsManager
    getWidgetStatePersistence().unregister('Settings')
    detectorsManager.finalize()


def test_both_getters_are_api_exports_that_run_on_the_ui_thread():
    """Same thread as the setter, so a read queued after a set sees it."""
    for getter in (SettingsController.getDetectorParameter,
                   SettingsController.getDetectorParameters):
        assert getattr(getter, '_APIExport', False) is True
        assert getattr(getter, '_APIRunOnUIThread', False) is True


def test_reads_the_value_the_camera_starts_with(settings):
    controller, _ = settings

    assert controller.getDetectorParameter('Camera', 'exposure') == 100


def test_reads_back_a_value_set_through_the_api(settings):
    controller, detectorsManager = settings

    controller.setDetectorParameter('Camera', 'exposure', 25)

    assert controller.getDetectorParameter('Camera', 'exposure') == 25
    assert detectorsManager.getDevice('Camera').parameters['exposure'].value == 25


def test_reads_back_an_edit_made_in_the_settings_widget(settings):
    """The getter answers what the user sees in the widget."""
    controller, _ = settings
    widgetParameter = controller._widget.trees['Camera'].p.param('Misc').param('exposure')

    widgetParameter.setValue(40)

    assert controller.getDetectorParameter('Camera', 'exposure') == 40


def test_reads_list_parameters_and_read_only_parameters(settings):
    controller, _ = settings

    assert controller.getDetectorParameter('Camera', 'pixel_format') == 'Mono12'
    assert controller.getDetectorParameter('Camera', 'image_width') == 800


def test_lists_every_parameter_with_its_units_editability_and_options(settings):
    controller, detectorsManager = settings

    parameters = controller.getDetectorParameters('Camera')

    assert list(parameters) == list(detectorsManager.getDevice('Camera').parameters)
    assert parameters['exposure'] == {
        'value': 100, 'units': 'ms', 'editable': True, 'options': None,
    }
    assert parameters['image_width'] == {
        'value': 800, 'units': 'arb.u.', 'editable': False, 'options': None,
    }
    assert parameters['pixel_format'] == {
        'value': 'Mono12', 'units': None, 'editable': True,
        'options': ['Mono8', 'Mono12'],
    }


def test_the_listing_is_a_copy_the_script_cannot_change_the_camera_through(settings):
    controller, detectorsManager = settings

    parameters = controller.getDetectorParameters('Camera')
    parameters['exposure']['value'] = 1
    parameters['pixel_format']['options'].append('Mono16')

    camera = detectorsManager.getDevice('Camera')
    assert camera.parameters['exposure'].value == 100
    assert camera.parameters['pixel_format'].options == ['Mono8', 'Mono12']


def test_an_unknown_parameter_name_says_which_names_exist(settings):
    """Names differ per camera ('Exposure' on a Thorlabs camera); the error
    must tell the script author what this one calls it."""
    controller, _ = settings

    with pytest.raises(AttributeError) as raised:
        controller.getDetectorParameter('Camera', 'Exposure')

    message = str(raised.value)
    assert '"Exposure"' in message
    assert "'exposure'" in message and "'pixel_format'" in message


@pytest.mark.parametrize('getter, args', [
    ('getDetectorParameter', ('exposure',)),
    ('getDetectorParameters', ()),
])
def test_an_unknown_detector_name_is_refused_like_the_setter_refuses_it(
        settings, getter, args):
    controller, _ = settings

    with pytest.raises(NoSuchSubManagerError, match='"Cam"'):
        getattr(controller, getter)('Cam', *args)
    with pytest.raises(NoSuchSubManagerError, match='"Cam"'):
        controller.setDetectorParameter('Cam', 'exposure', 5)


def test_a_script_can_change_the_exposure_and_restore_it(settings, qtbot):
    """The pattern the tutorial teaches, run from a worker thread through the
    generated API as a script runs it."""
    controller, detectorsManager = settings
    api = generateAPI([controller])
    outcome = {}

    def script():
        try:
            before = api.getDetectorParameter('Camera', 'exposure')
            try:
                api.setDetectorParameter('Camera', 'exposure', 10)
                outcome['during'] = api.getDetectorParameter('Camera', 'exposure')
            finally:
                api.setDetectorParameter('Camera', 'exposure', before)
            outcome['after'] = api.getDetectorParameters('Camera')['exposure']['value']
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            outcome['error'] = error

    thread = threading.Thread(target=script)
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=5000)

    assert 'error' not in outcome, outcome.get('error')
    assert outcome['during'] == 10
    assert outcome['after'] == 100
    assert detectorsManager.getDevice('Camera').parameters['exposure'].value == 100
