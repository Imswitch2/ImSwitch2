import argparse
import importlib
import os
import sys
import traceback

import imswitch
from imswitch.imcommon import prepareApp, launchApp
from imswitch.imcommon.controller import ModuleCommunicationChannel, MultiModuleWindowController
from imswitch.imcommon.model import dirtools, modulesconfigtools, pythontools, initLogger
from imswitch.imcommon.model.logging import attachLogFile, setLogLevel
from imswitch.imcommon.view import MultiModuleWindow, ModuleLoadErrorView

# --setup value that asks for the setup in a dialog instead of naming it.
_CHOOSE_SETUP = 'choose'


def main():
    # No abbreviations: a restart that drops --setup looks for it spelled out.
    parser = argparse.ArgumentParser(prog='imswitch', description='ImSwitch launcher',
                                     allow_abbrev=False)
    parser.add_argument('--debug', action='store_true',
                        help='Enable DEBUG-level log output from all managers.')
    parser.add_argument('--scale', type=float, default=None, metavar='FACTOR',
                        help='Scale the entire UI by this factor (e.g. 0.8 for 80%%). '
                             'Overrides the IMSWITCH_UI_SCALE environment variable.')
    parser.add_argument('--setup', default=None, metavar='SETUP',
                        help='Start with this hardware setup (a file in the '
                             'imcontrol_setups folder) for this run only; the setup '
                             'ImSwitch normally starts with is left as it is. '
                             f'"{_CHOOSE_SETUP}" asks for it in the setup dialog.')
    args, _unused = parser.parse_known_args()
    if args.debug:
        setLogLevel('DEBUG')

    logger = initLogger('main')

    # Before the modules load, so a crash while loading one is still on disk.
    # Records emitted even earlier than this (during imports) are in the
    # in-memory buffer and get written into the file as its first lines.
    attachLogFile(os.path.join(dirtools.UserFileDirs.Root, 'logs', 'imswitch.log'))

    logger.info(f'Starting ImSwitch {imswitch.__version__}')

    app = prepareApp(scale=args.scale)

    if args.setup is not None:
        # After prepareApp: importing imcontrol pulls in the Qt bindings it picks.
        from imswitch.imcontrol.model import configfiletools
        if args.setup == _CHOOSE_SETUP:
            setupFileName = _chooseSetup(configfiletools, logger)
        else:
            try:
                setupFileName = configfiletools.resolveSetupArgument(args.setup)
            except ValueError as e:
                logger.error(f'--setup: {e}')
                parser.error(f'--setup: {e}')
        configfiletools.useSetupForThisRun(setupFileName)

    enabledModuleIds = modulesconfigtools.getEnabledModuleIds()
    if 'imscripting' in enabledModuleIds:
        # Ensure that imscripting is added last: its script scope snapshots
        # the controllers loaded before it, so a module after it has no `api`
        enabledModuleIds.append(enabledModuleIds.pop(enabledModuleIds.index('imscripting')))

    modulePkgs = [importlib.import_module(pythontools.joinModulePath('imswitch', moduleId))
                  for moduleId in enabledModuleIds]

    moduleCommChannel = ModuleCommunicationChannel()

    multiModuleWindow = MultiModuleWindow('ImSwitch')
    multiModuleWindowController = MultiModuleWindowController.create(
        multiModuleWindow, moduleCommChannel
    )
    multiModuleWindow.show(showLoadingScreen=True)
    app.processEvents()  # Draw window before continuing

    # Register modules
    for modulePkg in modulePkgs:
        moduleCommChannel.register(modulePkg)

    # Load modules
    moduleMainControllers = dict()

    for i, modulePkg in enumerate(modulePkgs):
        moduleId = modulePkg.__name__
        moduleId = moduleId[moduleId.rindex('.') + 1:]  # E.g. "imswitch.imcontrol" -> "imcontrol"

        # The displayed module name will be the module's __title__, or alternatively its ID if
        # __title__ is not set
        moduleName = modulePkg.__title__ if hasattr(modulePkg, '__title__') else moduleId

        try:
            view, controller = modulePkg.getMainViewAndController(
                moduleCommChannel=moduleCommChannel,
                multiModuleWindowController=multiModuleWindowController,
                moduleMainControllers=moduleMainControllers
            )
        except Exception as e:
            logger.error(f'Failed to initialize module {moduleId}')
            logger.error(traceback.format_exc())
            moduleCommChannel.unregister(modulePkg)
            if moduleId == 'imcontrol':
                # ImControl can fail because the selected hardware setup is not
                # usable on this machine. Keep setup selection/config editing
                # reachable even though its normal controller never finished.
                try:
                    from imswitch.imcontrol.view.ImControlLoadErrorView import (
                        ImControlLoadErrorView,
                    )
                    errorView = ImControlLoadErrorView(e)
                except Exception:
                    logger.error('Failed to initialize ImControl recovery view')
                    logger.error(traceback.format_exc())
                    errorView = ModuleLoadErrorView(e)
            else:
                errorView = ModuleLoadErrorView(e)
            multiModuleWindow.addModule(moduleId, moduleName, errorView)
        else:
            # Add module to window
            multiModuleWindow.addModule(moduleId, moduleName, view)
            moduleMainControllers[moduleId] = controller

            # Update loading progress
            multiModuleWindow.updateLoadingProgress(i / len(modulePkgs))
            app.processEvents()  # Draw window before continuing

    launchApp(app, multiModuleWindow, moduleMainControllers.values())


def _chooseSetup(configfiletools, logger):
    """ The setup picked in the setup dialog for ``--setup choose``, with the
    setup ImSwitch normally starts with preselected. The command line then
    names it, so a restart comes back with it rather than asking again. """
    from imswitch.imcontrol.view.PickSetupDialog import askForSetup

    setupFileName = askForSetup(
        configfiletools.getSetupList(),
        configfiletools.loadOptions()[0].setupFileName,
        information='Select the hardware setup for this run (the setup ImSwitch '
                    'normally starts with stays as it is):',
    )
    if not setupFileName:
        logger.critical('User did not pick a setup to use')
        sys.exit()
    sys.argv[1:] = configfiletools.restartArgumentsForSetupChoice() + [
        configfiletools.SETUP_ARGUMENT, setupFileName]
    return setupFileName


if __name__ == '__main__':
    main()


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
