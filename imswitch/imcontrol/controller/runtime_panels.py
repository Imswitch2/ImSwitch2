"""Building panel controllers -- at startup, and while ImSwitch runs.

``ImConMainController`` used to build every panel's controller in one loop,
where a single failing constructor stopped ImControl, and nothing could add a
panel afterwards. Both now go through :meth:`PanelService.buildController`:

* at startup (:meth:`PanelService.buildStartupControllers`) a panel that fails
  is left out, its dock removed, and reported once the window is up -- except
  the core panels (``model/panels.py``), which still stop ImControl;
* while running (:meth:`PanelService.addPanel`, behind View > Add panel…) a
  panel is built, docked and given what startup gives it: the extra
  view objects some controllers take, the setup-mode controller, the smart
  microscopy mode service, its saved widget state. If anything fails, every
  part of it is undone -- dock, ``widgets``/``docks``/``controllers`` entries,
  state-persistence registration -- and the error is shown, not raised.

A panel that reads a setup section (Tiling, Autofocus) and does not have one
asks for it first, with the Config Studio's own form for that section, filled
in from the running hardware; the section is then put into the running setup
the way the loader would have built it. Whether to keep the panel for this
session only or save it -- to the setup file ImSwitch runs on, or as a new
setup -- is asked afterwards; saving patches the file (``model/setup_patch``).

What a panel added this way does not get until ImSwitch restarts with it in
the setup file: its functions in the scripting/REST API, which is built once
at startup. The save question says so.
"""

import dataclasses
import os
from types import ModuleType
from typing import Callable, Dict, Optional

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model import (
    configfiletools, getWidgetStatePersistence, panel_config, panels, setup_patch,
    setupfolder,
)
from imswitch.imcontrol.model.scan_cloak import resolve_scan_info
from . import controllers


def controllerClassFor(key: str, setupInfo):
    name = (f'ScanController{resolve_scan_info(setupInfo.scan).frontend_type}'
            if key == 'Scan' else f'{key}Controller')
    # The class, whatever the lazy package attribute holds (dev's
    # controllers.resolve_controller: importing a controller module binds the
    # module under the class's name).
    return controllers.resolve_controller(name)


def extraKwargsFor(key: str, viewWidgets: Dict[str, object]) -> dict:
    """View objects a controller takes besides its own widget."""
    if key == 'ViewerTools':
        # Viewer Tools' line/rectangle/intensity buttons select the Line
        # Profile panel's modes, so it is handed the panel too.
        return {
            'imageWidget': viewWidgets.get('Image'),
            'lineProfileWidget': viewWidgets.get('LineProfile'),
        }
    if key == 'ULenses':
        return {'imageWidget': viewWidgets.get('Image')}
    if key == 'TransformCalibration':
        # Its uLens-grid strategy takes its guesses from the uLenses panel.
        return {
            'imageWidget': viewWidgets.get('Image'),
            'ulensesWidget': viewWidgets.get('ULenses'),
        }
    return {}


def apiFunctionNames(controllerClass) -> list:
    """The scripting/REST functions a controller class exports."""
    return sorted(name for name in dir(controllerClass)
                  if getattr(getattr(controllerClass, name, None), '_APIExport', False))


class PanelUI:
    """The questions PanelService asks, as Qt dialogs. Tests replace it."""

    def __init__(self, view):
        self._view = view

    def editSection(self, spec, schema, current, pool, validator, notes):
        from imswitch.imcontrol.view.configeditor.section_form import SectionEditorDialog
        from qtpy import QtWidgets
        dialog = SectionEditorDialog(spec.section, schema, current, pool, self._view,
                                     title_prefix=f'Configure {spec.displayName}',
                                     validator=validator)
        if notes:
            label = QtWidgets.QLabel('Proposed from this microscope: ' + '; '.join(notes) + '.')
            label.setWordWrap(True)
            label.setStyleSheet('color: palette(mid); font-size: 8pt;')
            dialog.layout().insertWidget(1, label)
        self.lastDialog = dialog
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.result_data

    def askReviewExisting(self, spec, summary):
        from qtpy import QtWidgets
        box = QtWidgets.QMessageBox(self._view)
        box.setWindowTitle(f'Add {spec.displayName}')
        box.setText(f'This setup already has a {spec.section} configuration'
                    + (f' ({summary})' if summary else '') + '.')
        box.setInformativeText(f'Add the {spec.displayName} panel with it, or review it first?')
        add = box.addButton('Add', QtWidgets.QMessageBox.AcceptRole)
        review = box.addButton('Review configuration…', QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(add)
        box.exec_()
        clicked = box.clickedButton()
        return 'add' if clicked is add else 'review' if clicked is review else None

    def askSaveChoice(self, title, message, details):
        from imswitch.imcontrol.view.panel_docks import SaveChoiceDialog
        dialog = SaveChoiceDialog(title, message, details, self._view)
        self.lastDialog = dialog
        dialog.exec_()
        return dialog.choice

    def askNewSetupName(self, folder, suggested):
        from imswitch.imcontrol.view import guitools
        return guitools.askForTextInput(
            self._view, 'Save as new setup',
            f'Name of the new setup in {folder}\n(a subfolder may be part of it, e.g. lab/my_setup):',
            suggested)

    def askYesNo(self, title, question):
        from imswitch.imcontrol.view import guitools
        return guitools.askYesNoQuestion(self._view, title, question)

    def inform(self, title, message):
        from qtpy import QtWidgets
        QtWidgets.QMessageBox.information(self._view, title, message)

    def warn(self, title, message):
        from imswitch.imcontrol.view import guitools
        guitools.showWarning(self._view, title, message)


class PanelService:
    """Builds panel controllers and adds panels while ImSwitch runs.

    ``view`` is the ImConMainView, ``factory`` the controller factory,
    ``controllers`` the main controller's dict (filled in place),
    ``activeSetupPath`` the setup file this session runs on.
    """

    def __init__(self, *, view, factory, setupInfo, master, controllers: dict,
                 activeSetupPath: Optional[str], ui: Optional[PanelUI] = None):
        self._logger = initLogger(self, tryInheritParent=False)
        self._view = view
        self._factory = factory
        self._setupInfo = setupInfo
        self._master = master
        self.controllers = controllers
        self._activeSetupPath = activeSetupPath
        self.ui = ui if ui is not None else PanelUI(view)
        self.startupFailures: Dict[str, str] = {}
        self._setupModeController = None
        self._smartModeService = None
        self._shortcutManager = None
        self._addDialog = None

    # -- startup -------------------------------------------------------------

    def buildStartupControllers(self) -> None:
        """A controller for every widget the view built, as startup always
        did; a non-core panel whose controller fails is left out."""
        for key, widget in list(self._view.widgets.items()):
            try:
                self.buildController(key, widget, runtime=False)
            except Exception as e:
                if panels.isFatalAtStartup(key):
                    raise
                self._logger.error(f'Panel {key} could not be built and is left out: {e}',
                                   exc_info=True)
                self.startupFailures[key] = f'{type(e).__name__}: {e}'
                self._view.removePanel(key)

    def attach(self, *, setupModeController=None, smartModeService=None,
               shortcutManager=None) -> None:
        """What startup creates after the controllers, for panels added later."""
        self._setupModeController = setupModeController
        self._smartModeService = smartModeService
        self._shortcutManager = shortcutManager
        placeholders = getattr(self._view, 'panelPlaceholders', None)
        if isinstance(placeholders, dict):
            for placeholder in placeholders.values():
                placeholder.sigConfigure.connect(self.addPanel)

    def startupProblems(self) -> Dict[str, str]:
        """{panel: error} for panels left out at startup, view and controller."""
        problems = {}
        viewErrors = getattr(self._view, 'panelBuildErrors', None)
        if isinstance(viewErrors, dict):
            problems.update(viewErrors)
        problems.update(self.startupFailures)
        return problems

    # -- one panel ----------------------------------------------------------

    def buildController(self, key: str, widget, *, runtime: bool = True):
        """Build, register and wire the controller of panel ``key``.

        ``runtime``: added while ImSwitch runs, so it also gets what startup
        hands every controller once all exist (``_inject``) and its saved
        widget state -- at startup ImConMainController does both for all.
        Anything it registered for state persistence is unregistered again
        if construction or wiring fails; the exception propagates.
        """
        persistence = getWidgetStatePersistence()
        registeredBefore = set(persistence.getRegisteredControllers())
        controller = None
        try:
            controller = self._factory.createController(
                controllerClassFor(key, self._setupInfo), widget,
                **extraKwargsFor(key, self._view.widgets),
            )
            self.controllers[key] = controller
            if runtime:
                self._inject(key, controller)
        except BaseException:
            self.controllers.pop(key, None)
            for name in set(persistence.getRegisteredControllers()) - registeredBefore:
                persistence.unregister(name)
            if controller is not None:
                try:
                    controller.closeEvent()
                except Exception:  # noqa: BLE001 -- already failing; keep the first error
                    self._logger.exception(f'Closing the half-built {key} controller failed')
            raise
        if runtime:
            for name in sorted(set(persistence.getRegisteredControllers()) - registeredBefore):
                warnings = []
                try:
                    persistence.loadWidgetState(name, 'default', warnings_out=warnings)
                except Exception as e:  # noqa: BLE001 -- a stale state file is not fatal
                    self._logger.warning(f'Could not restore the saved state of {name}: {e}')
                for warning in warnings:
                    self._logger.info(f'{name}: {warning}')
        return controller

    def _inject(self, key, controller) -> None:
        """What ImConMainController hands the controllers it builds at
        startup, once they all exist."""
        if key == 'SetupModes' and self._setupModeController is not None:
            controller.setSetupModeController(self._setupModeController)
            if self._shortcutManager is not None:
                controller.setShortcutManager(self._shortcutManager,
                                              self._view.shortcutsMenu, self._view)
        if self._smartModeService is not None and hasattr(controller, 'setSmartModeService'):
            controller.setSmartModeService(self._smartModeService)

    # -- the Add panel dialog -----------------------------------------------

    def entries(self):
        docked = set(self._view.docks)
        result = []
        for key, spec in panels.PANELS.items():
            result.append((spec, self.availability(spec, docked)))
        order = {group: i for i, group in enumerate(panels.GROUP_ORDER)}
        result.sort(key=lambda entry: (order.get(entry[0].group, len(order)),
                                       not entry[1].possible, entry[0].displayName.lower()))
        return result

    def availability(self, spec, docked=None):
        if docked is None:
            docked = set(self._view.docks)
        return panels.availability(
            spec, docked=docked, setupInfo=self._setupInfo,
            prerequisiteProblem=lambda s: panel_config.prerequisiteProblem(
                s.key, self._setupInfo, self._master),
        )

    def openAddPanelDialog(self):
        from imswitch.imcontrol.view.panel_docks import AddPanelDialog
        if self._addDialog is None:
            self._addDialog = AddPanelDialog(self._view)
            self._addDialog.sigAddRequested.connect(self._addFromDialog)
        self._addDialog.setEntries(self.entries())
        self._addDialog.setStatus('')
        self._addDialog.show()
        self._addDialog.raise_()
        self._addDialog.activateWindow()
        return self._addDialog

    def _addFromDialog(self, key):
        result = self.addPanel(key)
        if self._addDialog is not None:
            self._addDialog.setEntries(self.entries())
            spec = panels.panelSpec(key)
            name = spec.displayName if spec else key
            self._addDialog.setStatus({
                'added': f'{name} added.', 'shown': f'{name} is open.',
                'cancelled': '', 'failed': f'{name} could not be added; see the message.',
                'unavailable': '',
            }.get(result, ''))

    # -- adding a panel -------------------------------------------------------

    def addPanel(self, key: str) -> str:
        """Add panel ``key`` to this session, asking for its section first if
        it needs one. Returns 'shown', 'added', 'cancelled', 'unavailable' or
        'failed'; never raises."""
        spec = panels.panelSpec(key)
        if spec is None:
            self.ui.warn('Add panel', f'There is no panel "{key}".')
            return 'unavailable'
        placeholder = key in getattr(self._view, 'panelPlaceholders', {})
        docked = set(self._view.docks) - ({key} if placeholder else set())
        availability = self.availability(spec, docked)
        if availability.action == 'show':
            self._view.showPanel(key)
            return 'shown'
        if not availability.possible:
            self.ui.warn(f'Add {spec.displayName}', availability.reason)
            return 'unavailable'

        sectionBefore = getattr(self._setupInfo, spec.section, None) if spec.section else None
        sectionToSave = None
        if spec.kind is panels.PanelKind.CONFIGURED:
            if sectionBefore is None:
                sectionToSave = self._configureSection(spec)
                if sectionToSave is None:
                    return 'cancelled'
            else:
                answer = self.ui.askReviewExisting(
                    spec, panel_config.sectionSummary(sectionBefore))
                if answer is None:
                    return 'cancelled'
                if answer == 'review':
                    sectionToSave = self._configureSection(spec)
                    if sectionToSave is None:
                        return 'cancelled'

        try:
            if placeholder:
                widget = self._view.replacePlaceholder(key)
            else:
                widget = self._view.addPanel(key)
            try:
                self.buildController(key, widget)
            except BaseException:
                self._view.removePanel(key)
                raise
        except Exception as e:
            if spec.section:
                setattr(self._setupInfo, spec.section, sectionBefore)
            self._logger.error(f'Adding panel {key} failed: {e}', exc_info=True)
            self.ui.warn(f'Could not add {spec.displayName}',
                         f'The {spec.displayName} panel could not be built, so nothing '
                         f'was added:\n\n{type(e).__name__}: {e}\n\nThe log has the details.')
            return 'failed'
        if placeholder:
            self._view.dropPlaceholder(key)
        self._logger.info(f'Panel {key} added for this session')
        self._offerSave(spec, sectionToSave)
        return 'added'

    def _configureSection(self, spec) -> Optional[dict]:
        """Ask for ``spec``'s section; on Apply put it into the running setup.
        Returns the section as it would be written to the file, or None."""
        from imswitch.imcontrol.view.configeditor.section_form import (
            _build_default_section, load_section_schema,
        )
        schema = load_section_schema(spec.section)
        if schema is None:
            self.ui.warn(f'Configure {spec.displayName}',
                         f'There is no form for the {spec.section} section.')
            return None
        current = (setup_patch.readSection(self._activeSetupPath, spec.section)
                   if self._activeSetupPath else None)
        notes = []
        before = getattr(self._setupInfo, spec.section, None)
        if current is None and before is not None:
            current = dataclasses.asdict(before)
        if current is None:
            current, notes = panel_config.proposeSection(
                spec.key, self._setupInfo, self._master, _build_default_section(schema))
        setupInfoType = type(self._setupInfo)

        def validator(values):
            return panel_config.validateSection(spec.key, values, self._setupInfo,
                                                self._master, setupInfoType)

        values = self.ui.editSection(spec, schema, current, self._devicePool(), validator, notes)
        if values is None:
            return None
        typed = panel_config.buildSection(spec.key, values, setupInfoType)
        setattr(self._setupInfo, spec.section, typed)
        return values

    def _devicePool(self) -> dict:
        """What the form's device dropdowns offer: the running setup's devices."""
        pool = {}
        for category in ('positioners', 'detectors', 'lasers', 'rotators'):
            devices = getattr(self._setupInfo, category, None) or {}
            pool[category] = {name: {} for name in devices}
        return pool

    # -- keeping it ---------------------------------------------------------

    def _offerSave(self, spec, sectionValues: Optional[dict]) -> None:
        api = apiFunctionNames(controllerClassFor(spec.key, self._setupInfo))
        message = (f'<b>{spec.displayName}</b> is added for this session. '
                   f'Keep it only until ImSwitch closes, or save it to a setup file?')
        details = []
        if sectionValues is not None:
            details.append(f'Saving also writes the <code>{spec.section}</code> section '
                           f'you just configured.')
        if api:
            shown = ', '.join(f'<code>{name}</code>' for name in api[:6])
            more = f' and {len(api) - 6} more' if len(api) > 6 else ''
            details.append(f'Its scripting/REST functions ({shown}{more}) become available '
                           f'only after ImSwitch restarts with this panel in its setup file.')
        if self._activeSetupPath:
            details.append(f'Current setup: {self._activeSetupPath}')
        choice = self.ui.askSaveChoice(f'Keep {spec.displayName}?', message, '<br>'.join(details))
        sections = {spec.section: sectionValues} if sectionValues is not None else None
        if choice == 'current':
            self.saveToCurrentSetup(spec.key, sections)
        elif choice == 'new':
            self.saveAsNewSetup(spec.key, sections)

    def saveToCurrentSetup(self, key: str, sections: Optional[dict] = None):
        """Patch the setup file this session runs on -- for a run started
        with ``--setup x``, x; the setup ImSwitch starts with is unchanged."""
        if not self._activeSetupPath:
            self.ui.warn('Save to current setup', 'This session has no setup file to save to.')
            return None
        try:
            result = setup_patch.patchSetupFile(self._activeSetupPath, addWidgets=[key],
                                                sections=sections)
        except Exception as e:  # noqa: BLE001 -- shown to the operator
            self._logger.error(f'Saving {key} to {self._activeSetupPath} failed: {e}', exc_info=True)
            self.ui.warn('Save to current setup', f'Could not write {self._activeSetupPath}:\n{e}')
            return None
        what = '; '.join(result.changes) if result.changes else 'nothing needed changing'
        self._logger.info(f'Saved panel {key} to {result.path}: {what}')
        self.ui.inform('Saved to current setup', f'{result.path}\n\n{what}.')
        return result

    def saveAsNewSetup(self, key: str, sections: Optional[dict] = None, name: str = None):
        """Write the current setup file plus the panel as a new setup in the
        setup folder; then offer to start with it next time (only that --
        this session keeps running what it runs)."""
        if not self._activeSetupPath:
            self.ui.warn('Save as new setup', 'This session has no setup file to copy.')
            return None
        folder = configfiletools.getSetupFilesDir()
        if name is None:
            base = os.path.splitext(setupfolder.setupNameFor(self._activeSetupPath, folder))[0]
            name = self.ui.askNewSetupName(folder, f'{base}_{key.lower()}')
            if not name:
                return None
        try:
            result = setup_patch.saveAsNewSetup(self._activeSetupPath, folder, name,
                                                addWidgets=[key], sections=sections)
        except (ValueError, FileExistsError) as e:
            self.ui.warn('Save as new setup', str(e))
            return None
        except Exception as e:  # noqa: BLE001
            self._logger.error(f'Saving a new setup {name} failed: {e}', exc_info=True)
            self.ui.warn('Save as new setup', f'Could not write the new setup:\n{e}')
            return None
        setupName = setupfolder.setupNameFor(result.path, folder)
        self._logger.info(f'Saved a new setup {setupName} with panel {key}')
        if self.ui.askYesNo('New setup saved',
                            f'Saved {result.path}.\n\nStart ImSwitch with "{setupName}" '
                            f'next time? (This session keeps running the setup it runs.)'):
            configfiletools.saveSetupChoice(setupName)
            self._logger.info(f'{setupName} is now the setup ImSwitch starts with')
        return result


def reportStartupProblems(view, problems: Dict[str, str], warn: Callable) -> None:
    """One message naming the panels left out at startup."""
    if not problems:
        return
    names = {key: (panels.panelSpec(key).displayName if panels.panelSpec(key) else key)
             for key in problems}
    text = '\n'.join(f'• {names[key]}: {error}' for key, error in problems.items())
    warn(view, 'Some panels could not be built',
         'ImSwitch started without these panels; the rest works as usual. '
         'The log has the details.\n\n' + text)
