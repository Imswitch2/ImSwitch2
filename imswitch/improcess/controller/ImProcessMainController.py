from imswitch.imcommon.controller import MainController
from imswitch.imcommon.model import initLogger
from .CommunicationChannel import CommunicationChannel
from .ImProcessMainViewController import ImProcessMainViewController
from .basecontrollers import ImProcessWidgetControllerFactory


class ImProcessMainController(MainController):
    def __init__(self, mainView, moduleCommChannel):
        self.__mainView = mainView
        self.__moduleCommChannel = moduleCommChannel
        self.__logger = initLogger(self, tryInheritParent=False)

        # Connect view signals
        self.__mainView.sigClosing.connect(self.closeEvent)

        # Initialize plugin registry
        self._initialize_plugins()

        # Init communication channel and master controller
        self.__commChannel = CommunicationChannel()

        # List of Controllers for the GUI Widgets
        self.__factory = ImProcessWidgetControllerFactory(
            self.__commChannel, self.__moduleCommChannel
        )

        self.mainViewController = self.__factory.createController(
            ImProcessMainViewController, self.__mainView
        )
    
    def _initialize_plugins(self):
        """
        Initialize the plugin registry based on config or standalone defaults.
        
        Order of precedence:
        1. If setup.json has a "processing" block, load specified plugins
        2. Otherwise, load standalone defaults (view-only + universal processors)
        """
        from imswitch.improcess.reconstructors.registry import get_registry
        from imswitch.improcess.reconstructors import register_default_reconstructors
        from imswitch.improcess.processors import register_default_processors
        
        registry = get_registry()
        
        # Check if we have a setup configuration
        try:
            from imswitch.imcommon.model import configfiletools
            setupInfo = configfiletools.loadSetupInfo()
            processing_config = setupInfo.get('processing', {})
        except Exception as e:
            # Standalone mode: no setup.json
            self.__logger.info(f"No setup configuration found ({e}), using standalone defaults")
            processing_config = {}
        
        # Determine which plugins to load
        if processing_config:
            # Config-driven mode
            reconstructor_ids = processing_config.get('reconstructors', ['monalisa'])
            processor_ids = processing_config.get('processors', ['drift-correct'])
            self.__logger.info(
                f"Loading plugins from config: reconstructors={reconstructor_ids}, "
                f"processors={processor_ids}"
            )
        else:
            # Standalone defaults
            reconstructor_ids = ['view-only']
            processor_ids = ['drift-correct']
            self.__logger.info("Using standalone defaults: view-only + drift-correct")
        
        # Register all available reconstructors and processors
        # (The register functions will only instantiate the ones in config)
        register_default_reconstructors(registry, reconstructor_ids)
        register_default_processors(registry)  # TODO: filter by processor_ids
        
        self.__logger.info(
            f"Plugin registry initialized: "
            f"{len(registry.reconstructors())} reconstructors, "
            f"{len(registry.processors())} processors"
        )

    def closeEvent(self):
        self.__factory.closeAllCreatedControllers()


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
