"""Adding panels while ImSwitch runs, on the real ImControl stack.

The real ImConMainView and ImConMainController on the shipped mock setup
``example_mock.json`` (one XY stage, one camera), without the Image panel --
napari cannot draw headless. The setup file is a copy in a temporary setup
folder, so saving can be checked on disk; the dialogs are replaced by a
scripted stand-in (``_UI``).
"""

import json
from pathlib import Path

import pytest

from imswitch import imcontrol
from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcommon.model import ostools
from imswitch.imcontrol.controller import ImConMainController as mainControllerModule
from imswitch.imcontrol.controller import controllers as controllerTable
from imswitch.imcontrol.controller.ImConMainController import ImConMainController
from imswitch.imcontrol.model import (
    configfiletools, getWidgetStatePersistence, setupfolder,
)
from imswitch.imcontrol.model.SetupInfo import TilingInfo
from imswitch.imcontrol.view import ImConMainView, ViewSetupInfo, widgets as widgetTable


pytestmark = pytest.mark.nohardware

BASE_WIDGETS = ['Settings', 'View', 'Recording', 'Console']


class _UI:
    """Scripted answers to PanelService's questions; records what it was asked."""

    def __init__(self):
        self.asked = []
        self.warnings = []
        self.informed = []
        self.sectionAnswer = 'accept'   # 'accept', 'cancel' or a dict
        self.reviewAnswer = 'add'
        self.saveAnswer = 'session'
        self.newSetupName = 'lab/with_tiling'
        self.yes = False
        self.formCurrent = None
        self.formValidator = None
        self.formNotes = None

    def editSection(self, spec, schema, current, pool, validator, notes):
        self.asked.append(('section', spec.key))
        self.formCurrent, self.formValidator, self.formNotes = dict(current), validator, notes
        if self.sectionAnswer == 'cancel':
            return None
        values = dict(current) if self.sectionAnswer == 'accept' else dict(self.sectionAnswer)
        assert validator(values) == []
        return values

    def askReviewExisting(self, spec, summary):
        self.asked.append(('review', spec.key, summary))
        return self.reviewAnswer

    def askSaveChoice(self, title, message, details):
        self.asked.append(('save', title, message, details))
        return self.saveAnswer

    def askNewSetupName(self, folder, suggested):
        self.asked.append(('name', folder, suggested))
        return self.newSetupName

    def askYesNo(self, title, question):
        self.asked.append(('yesno', title, question))
        return self.yes

    def inform(self, title, message):
        self.informed.append((title, message))

    def warn(self, title, message):
        self.warnings.append((title, message))


@pytest.fixture
def setupFolder(tmp_path, monkeypatch):
    folder = tmp_path / 'imcontrol_setups'
    (folder / 'mock').mkdir(parents=True)
    optionsPath = tmp_path / 'imcontrol_options.json'
    optionsPath.write_text(json.dumps({'setupFileName': 'mock/scope.json'}))
    monkeypatch.setattr(setupfolder, 'defaultSetupFolder', lambda: str(folder))
    monkeypatch.setattr(configfiletools, '_optionsFilePath', str(optionsPath))
    monkeypatch.setattr(configfiletools, '_options', None)
    monkeypatch.setattr(configfiletools, '_setupForThisRun', None)
    monkeypatch.setattr(ImConMainController, '_shouldSaveWidgetStateOnClose',
                        lambda self: False)
    warnings = []
    monkeypatch.setattr(mainControllerModule.guitools, 'showWarning',
                        lambda *args, **kwargs: warnings.append(args[1:]))
    ostools.cancelRestart()
    yield folder, optionsPath, warnings
    ostools.cancelRestart()


def _classesNotModules(monkeypatch):
    """Other tests import controller submodules directly, which binds the
    module onto the lazy package under the class's name (other tests patch
    through that binding). ImConMainController reads e.g.
    ``controllers.HardwareStatusController``; point such names back at the
    class for this test."""
    import types
    from imswitch.imcontrol.controller.controllers import _CONTROLLER_MODULES
    for name in _CONTROLLER_MODULES:
        found = controllerTable.__dict__.get(name)
        if isinstance(found, types.ModuleType):
            monkeypatch.setattr(controllerTable, name, getattr(found, name))


@pytest.fixture
def imcontrolApp(setupFolder, qapp, monkeypatch):
    folder, optionsPath, warnings = setupFolder
    made = []

    def start(widgets=BASE_WIDGETS, edit=None, name='mock/scope.json'):
        data = json.loads(Path(setupfolder.shippedSetupPath('example_mock.json'))
                          .read_text(encoding='utf-8'))
        data['availableWidgets'] = list(widgets)
        data.pop('tiling', None)
        data['handEditedUnknownKey'] = {'keep': True}
        if edit is not None:
            edit(data)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        options, _ = configfiletools.loadOptions()
        setup = configfiletools.loadSetupInfo(options, ViewSetupInfo)
        _classesNotModules(monkeypatch)
        channel = ModuleCommunicationChannel()
        channel.register(imcontrol)
        view = ImConMainView(options, setup)
        controller = ImConMainController(options, setup, view, channel)
        ui = _UI()
        controller.panelService.ui = ui
        app = _App(view, controller, setup, ui, path, optionsPath, warnings)
        made.append(app)
        return app

    yield start
    for app in made:
        app.controller.closeEvent()
        app.view.deleteLater()
    qapp.processEvents()


class _App:
    def __init__(self, view, controller, setup, ui, path, optionsPath, warnings):
        self.view, self.controller, self.setup, self.ui = view, controller, setup, ui
        self.path, self.optionsPath, self.warnings = path, optionsPath, warnings

    @property
    def master(self):
        return self.controller._ImConMainController__masterController

    def file(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def dockedInArea(self, key):
        dock = self.view.docks.get(key)
        return dock is not None and dock.area is self.view.dockArea

    def defaultLayoutNames(self):
        names = []

        def walk(node):
            if node[0] == 'dock':
                names.append(node[1])
            else:
                for child in node[1]:
                    walk(child)

        walk(self.view._defaultDockState['main'])
        return names


# --------------------------------------------------------------------------
# A panel that can be added as it is
# --------------------------------------------------------------------------

def test_adding_a_panel_docks_it_and_builds_its_controller(imcontrolApp):
    app = imcontrolApp()
    fileBefore = app.path.read_text(encoding='utf-8')

    assert app.controller.addPanel('Watcher') == 'added'

    assert app.dockedInArea('Watcher')
    assert 'Watcher' in app.view.widgets
    assert type(app.controller.controllers['Watcher']).__name__ == 'WatcherController'
    assert app.controller.controllers['Watcher']._widget is app.view.widgets['Watcher']
    assert app.setup.availableWidgets == BASE_WIDGETS + ['Watcher']
    # Reset panel layout knows it, and keeps it docked.
    assert 'File Watcher' in app.defaultLayoutNames()
    app.view.resetDockLayout()
    assert app.dockedInArea('Watcher')
    # Session only unless asked: the file is as it was.
    [save] = [q for q in app.ui.asked if q[0] == 'save']
    assert 'for this session' in save[2]
    assert app.path.read_text(encoding='utf-8') == fileBefore


def test_adding_an_open_panel_shows_it_instead(imcontrolApp):
    app = imcontrolApp()
    app.controller.addPanel('FFT')
    controller = app.controller.controllers['FFT']
    asked = len(app.ui.asked)

    assert app.controller.addPanel('FFT') == 'shown'
    assert app.controller.addPanel('Console') == 'shown'

    assert app.controller.controllers['FFT'] is controller
    assert len(app.ui.asked) == asked  # no second save question
    assert app.setup.availableWidgets.count('FFT') == 1


def test_the_save_question_says_when_the_api_appears(imcontrolApp):
    app = imcontrolApp()

    app.controller.addPanel('Tiling')

    [save] = [q for q in app.ui.asked if q[0] == 'save']
    assert 'only after ImSwitch restarts' in save[3]
    assert '<code>startTiling</code>' in save[3]
    assert not hasattr(app.controller.api, 'startTiling')
    # The API object built at startup is left as it was.
    assert app.controller.api is not None


def test_a_runtime_setup_modes_panel_gets_what_startup_gives_it(imcontrolApp):
    app = imcontrolApp()

    app.controller.addPanel('SetupModes')

    modes = app.controller.controllers['SetupModes']
    assert modes._setupModeController is app.controller.setupModeController
    assert modes._shortcutManager is app.controller.shortcutManager


def test_a_panel_that_cannot_be_added_says_why(imcontrolApp):
    app = imcontrolApp()

    assert app.controller.addPanel('Laser') == 'unavailable'
    assert app.controller.addPanel('ViewerTools') == 'unavailable'

    assert 'Laser' not in app.view.docks
    assert app.ui.warnings[0][1].startswith('Hardware panel')
    assert 'Image Display' in app.ui.warnings[1][1]


def test_the_add_panel_dialog_lists_and_greys(imcontrolApp, qapp):
    app = imcontrolApp()
    dialog = app.controller.panelService.openAddPanelDialog()

    assert dialog.entryFor('FFT').flags() & 0x20  # ItemIsEnabled
    laser = dialog.entryFor('Laser')
    assert not laser.flags()
    assert laser.toolTip(0).startswith('Hardware panel')
    assert dialog.entryFor('Tiling').text(1) == 'Can be added after configuring'

    dialog._request(dialog.entryFor('Watcher'))
    assert 'Watcher' in app.controller.controllers
    assert dialog.entryFor('Watcher').text(1) == 'Open: shows it'
    dialog.close()


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------

class _BrokenController:
    """Registers for state persistence, then fails, like a constructor that
    gets half way."""

    componentName = 'BrokenFFT'

    def __init__(self, *args, **kwargs):
        getWidgetStatePersistence().register('BrokenFFT', self)
        raise RuntimeError('constructor failed half way')

    def getComponentState(self):
        return {}

    def applyComponentState(self, state, *, applyMode):
        return []

    def describeComponentState(self, state):
        return []

    def getComponentStateHazards(self, state, *, applyMode, context=None):
        return []


def test_a_failing_controller_leaves_nothing_behind(imcontrolApp, monkeypatch):
    app = imcontrolApp()
    layoutBefore = app.view._defaultDockState
    docksBefore = dict(app.view.docks)
    monkeypatch.setattr(controllerTable, 'FFTController', _BrokenController, raising=False)

    assert app.controller.addPanel('FFT') == 'failed'

    assert 'FFT' not in app.view.docks and app.view.docks == docksBefore
    assert 'FFT' not in app.view.widgets
    assert 'FFT' not in app.controller.controllers
    assert app.setup.availableWidgets == BASE_WIDGETS
    assert app.view._defaultDockState == layoutBefore
    assert not getWidgetStatePersistence().isRegistered('BrokenFFT')
    assert 'constructor failed half way' in app.ui.warnings[-1][1]
    assert not [q for q in app.ui.asked if q[0] == 'save']
    # And it can be tried again once the cause is gone.
    monkeypatch.undo()
    assert app.controller.addPanel('FFT') == 'added'


def test_a_failing_widget_leaves_nothing_behind(imcontrolApp, monkeypatch):
    app = imcontrolApp()

    class broken(widgetTable.WatcherWidget):
        def __init__(self, *args, **kwargs):
            raise RuntimeError('widget failed')

    monkeypatch.setattr(widgetTable, 'WatcherWidget', broken, raising=False)

    assert app.controller.addPanel('Watcher') == 'failed'
    assert 'Watcher' not in app.view.docks and 'Watcher' not in app.view.widgets
    assert app.setup.availableWidgets == BASE_WIDGETS


# --------------------------------------------------------------------------
# Startup: one failing optional panel does not stop ImControl
# --------------------------------------------------------------------------

def test_a_failing_optional_controller_at_startup_is_left_out(imcontrolApp, monkeypatch):
    monkeypatch.setattr(controllerTable, 'ConsoleController', _BrokenController, raising=False)

    app = imcontrolApp(BASE_WIDGETS + ['Watcher'])

    assert 'Console' not in app.controller.controllers
    assert 'Console' not in app.view.docks and 'Console' not in app.view.widgets
    assert 'Console' not in app.setup.availableWidgets
    assert 'Watcher' in app.controller.controllers  # the others are built
    assert not getWidgetStatePersistence().isRegistered('BrokenFFT')
    [(title, text)] = app.warnings
    assert title == 'Some panels could not be built'
    assert 'Console: RuntimeError: constructor failed half way' in text


def test_a_failing_optional_widget_at_startup_is_left_out(imcontrolApp, monkeypatch):
    class broken(widgetTable.ConsoleWidget):
        def __init__(self, *args, **kwargs):
            raise RuntimeError('no widget today')

    monkeypatch.setattr(widgetTable, 'ConsoleWidget', broken, raising=False)

    app = imcontrolApp()

    assert 'Console' not in app.view.docks and 'Console' not in app.controller.controllers
    assert app.view.panelBuildErrors == {'Console': 'RuntimeError: no widget today'}
    assert 'Console: RuntimeError: no widget today' in app.warnings[0][1]


def test_a_failing_core_panel_still_stops_imcontrol(imcontrolApp, monkeypatch):
    monkeypatch.setattr(controllerTable, 'SettingsController', _BrokenController, raising=False)

    with pytest.raises(RuntimeError, match='half way'):
        imcontrolApp()


# --------------------------------------------------------------------------
# Tiling: configured while running
# --------------------------------------------------------------------------

def test_a_listed_tiling_without_its_section_says_so_in_its_dock(imcontrolApp):
    app = imcontrolApp(BASE_WIDGETS + ['Tiling'])

    placeholder = app.view.panelPlaceholders['Tiling']
    assert app.dockedInArea('Tiling')
    assert 'Tiling' not in app.controller.controllers
    assert 'Tiling is not configured' in placeholder.title.text()
    assert 'no <code>tiling</code> section' in placeholder.message.text()
    assert placeholder.configureButton.text() == 'Configure…'


def test_configuring_tiling_from_its_placeholder(imcontrolApp):
    app = imcontrolApp(BASE_WIDGETS + ['Tiling'])
    placeholder = app.view.panelPlaceholders['Tiling']

    placeholder.configureButton.click()

    assert isinstance(app.setup.tiling, TilingInfo)
    assert app.setup.tiling.xyPositioner == 'Mock XY Stage'
    tiling = app.controller.controllers['Tiling']
    assert tiling._widget is app.view.widgets['Tiling']
    assert 'Tiling' not in app.view.panelPlaceholders
    assert app.view.docks['Tiling'].widgets[-1] is app.view.widgets['Tiling']
    assert tiling._widget.getTileStepUm() == pytest.approx(app.setup.tiling.defaultTileStepUm)


def test_adding_tiling_asks_for_its_section_with_proposed_values(imcontrolApp):
    app = imcontrolApp()

    assert app.controller.addPanel('Tiling') == 'added'

    assert ('section', 'Tiling') in app.ui.asked
    assert app.ui.formCurrent['xyPositioner'] == 'Mock XY Stage'
    assert app.ui.formCurrent['camera'] == 'Camera'
    assert any('field of view of Camera' in note for note in app.ui.formNotes)
    # The form's check refuses a single-axis stage, in words.
    assert app.ui.formValidator({**app.ui.formCurrent, 'xyPositioner': 'Nope'}) == [
        'XY positioner: there is no positioner "Nope" in this setup.']
    assert app.setup.tiling.camera == 'Camera'
    assert 'Tiling' in app.setup.availableWidgets
    [save] = [q for q in app.ui.asked if q[0] == 'save']
    assert 'tiling' in save[3]


def test_cancelling_the_form_adds_nothing(imcontrolApp):
    app = imcontrolApp()
    app.ui.sectionAnswer = 'cancel'

    assert app.controller.addPanel('Tiling') == 'cancelled'

    assert app.setup.tiling is None
    assert 'Tiling' not in app.view.docks


def test_a_tiling_controller_that_fails_takes_its_section_back(imcontrolApp, monkeypatch):
    app = imcontrolApp()
    monkeypatch.setattr(controllerTable, 'TilingController', _BrokenController, raising=False)

    assert app.controller.addPanel('Tiling') == 'failed'

    assert app.setup.tiling is None
    assert 'Tiling' not in app.view.docks


def test_a_configured_but_unlisted_tiling_is_added_without_the_form(imcontrolApp):
    def withTiling(data):
        data['tiling'] = {'xyPositioner': 'Mock XY Stage', 'defaultTileStepUm': 50.0}

    app = imcontrolApp(edit=withTiling)

    assert app.controller.addPanel('Tiling') == 'added'

    assert ('section', 'Tiling') not in app.ui.asked
    [review] = [q for q in app.ui.asked if q[0] == 'review']
    assert 'xyPositioner Mock XY Stage' in review[2]
    assert app.controller.controllers['Tiling']._widget.getTileStepUm() == pytest.approx(50.0)


def test_reviewing_an_existing_tiling_starts_from_the_file(imcontrolApp):
    def withTiling(data):
        data['tiling'] = {'xyPositioner': 'Mock XY Stage', 'defaultTileStepUm': 50.0,
                          'siteSpecificKey': 'kept'}

    app = imcontrolApp(edit=withTiling)
    app.ui.reviewAnswer = 'review'
    app.ui.saveAnswer = 'current'

    app.controller.addPanel('Tiling')

    assert app.ui.formCurrent['siteSpecificKey'] == 'kept'
    assert app.file()['tiling']['siteSpecificKey'] == 'kept'


def test_focus_lock_without_its_section_says_so_without_a_configure_button(imcontrolApp):
    app = imcontrolApp(BASE_WIDGETS + ['FocusLock'])

    placeholder = app.view.panelPlaceholders['FocusLock']
    assert placeholder.configureButton is None
    assert 'restart' in placeholder.message.text()
    assert 'FocusLock' not in app.controller.controllers


def test_a_mock_tiling_run_completes_on_the_added_panel(imcontrolApp, qtbot):
    app = imcontrolApp()
    app.controller.addPanel('Tiling')
    tiling = app.controller.controllers['Tiling']
    tiling._widget.setDefaultTileCounts(1, 1)
    tiling._widget.setDefaultSettleTimeMs(0)

    tiling.startTiling()
    qtbot.waitUntil(lambda: not tiling._scanning, timeout=20000)

    assert tiling._gridPositions
    assert tiling._stitcher is not None


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------

def test_save_to_current_setup_patches_only_the_panel(imcontrolApp):
    app = imcontrolApp()
    before = app.file()
    app.ui.saveAnswer = 'current'

    app.controller.addPanel('Tiling')

    after = app.file()
    assert after['availableWidgets'] == BASE_WIDGETS + ['Tiling']
    assert after['tiling']['xyPositioner'] == 'Mock XY Stage'
    assert list(after) == list(before) + ['tiling']
    for key in before:
        if key != 'availableWidgets':
            assert after[key] == before[key], key
    [(title, message)] = app.ui.informed
    assert str(app.path) in message and 'added the tiling section' in message


def test_save_to_current_setup_on_a_setup_flag_run_keeps_the_default(imcontrolApp, setupFolder):
    folder, optionsPath, _ = setupFolder
    (folder / 'mock').mkdir(exist_ok=True)
    (folder / 'mock' / 'usual.json').write_text('{}')
    optionsPath.write_text(json.dumps({'setupFileName': 'mock/usual.json'}))
    configfiletools.useSetupForThisRun('mock/scope.json')
    optionsBefore = optionsPath.read_text()

    app = imcontrolApp()
    app.ui.saveAnswer = 'current'
    app.controller.addPanel('Watcher')

    assert 'Watcher' in app.file()['availableWidgets']       # the --setup file
    assert (folder / 'mock' / 'usual.json').read_text() == '{}'
    assert optionsPath.read_text() == optionsBefore            # default unchanged


def test_save_as_new_setup_writes_a_new_file_and_offers_it_as_default(imcontrolApp, setupFolder):
    folder, optionsPath, _ = setupFolder
    app = imcontrolApp()
    fileBefore = app.path.read_text(encoding='utf-8')
    app.ui.saveAnswer = 'new'
    app.ui.yes = True

    app.controller.addPanel('Tiling')

    new = json.loads((folder / 'lab' / 'with_tiling.json').read_text(encoding='utf-8'))
    assert new['tiling']['xyPositioner'] == 'Mock XY Stage'
    assert new['handEditedUnknownKey'] == {'keep': True}
    assert app.path.read_text(encoding='utf-8') == fileBefore
    [(_, folderAsked, suggested)] = [q for q in app.ui.asked if q[0] == 'name']
    assert folderAsked == str(folder) and suggested == 'mock/scope_tiling'
    assert json.loads(optionsPath.read_text())['setupFileName'] == 'lab/with_tiling.json'
    # This session keeps running what it runs.
    assert app.controller._ImConMainController__activeSetupPath == str(app.path.resolve())


def test_save_as_new_setup_never_overwrites(imcontrolApp, setupFolder):
    folder, _, _ = setupFolder
    (folder / 'lab').mkdir()
    (folder / 'lab' / 'with_tiling.json').write_text('{"mine": true}')
    app = imcontrolApp()
    app.ui.saveAnswer = 'new'

    app.controller.addPanel('Watcher')

    assert (folder / 'lab' / 'with_tiling.json').read_text() == '{"mine": true}'
    assert 'already is a setup' in app.ui.warnings[-1][1]


def test_declining_the_new_default_leaves_the_options_alone(imcontrolApp, setupFolder):
    _, optionsPath, _ = setupFolder
    app = imcontrolApp()
    optionsBefore = optionsPath.read_text()
    app.ui.saveAnswer = 'new'
    app.ui.yes = False

    app.controller.addPanel('Watcher')

    assert optionsPath.read_text() == optionsBefore


# --------------------------------------------------------------------------
# Autofocus: adding it starts no camera
# --------------------------------------------------------------------------

def test_adding_autofocus_takes_no_camera_lease(imcontrolApp, monkeypatch):
    def withZ(data):
        data['positioners']['Mock Z'] = {
            'analogChannel': None, 'digitalLine': None,
            'managerName': 'MockPositionerManager', 'managerProperties': {},
            'axes': ['Z'], 'forPositioning': True, 'resetOnClose': False,
        }

    app = imcontrolApp(edit=withZ)
    acquired = []
    detectors = app.master.detectorsManager
    real = detectors.acquire
    monkeypatch.setattr(detectors, 'acquire',
                        lambda names, purpose: (acquired.append((names, purpose)),
                                                real(names, purpose))[1])

    assert app.controller.addPanel('Autofocus') == 'added'

    assert app.ui.formCurrent['positioner'] == 'Mock Z'
    assert app.ui.formCurrent['camera'] == 'Camera'
    assert 'Autofocus' in app.controller.controllers
    assert acquired == []
