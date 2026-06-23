"""Controller for live reconstruction mode in the watcher UI."""

import os
from collections import deque

from imswitch.imcommon.model.logging import initLogger
from imswitch.imcommon.view.guitools.FileWatcher import FileWatcher
from imswitch.improcess.live import make_live_source
from .basecontrollers import ImProcessWidgetController
from .LiveReconstructionController import LiveReconstructionController


class LiveModeController(ImProcessWidgetController):
    """Manages live reconstruction mode: lifecycle, source selection, and result routing.
    
    Subscribes to the watcher UI's live toggle, selects the appropriate LiveSource
    based on file format, drives LiveReconstructionController, and routes results
    to the existing ReconstructionView.
    """

    def __init__(self, *args, mainController=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mainController = mainController
        self._logger = initLogger(self, tryInheritParent=False)
        
        self._liveController = None
        self._fileWatcher = None
        self._storeQueue = deque()
        self._currentlyProcessing = False
        self._watchedFolder = None
        self._widget.sigLiveChanged.connect(self._onLiveToggled)

    def _onLiveToggled(self, enabled: bool) -> None:
        """Handle live reconstruction toggle."""
        if enabled:
            self._startLive()
        else:
            self._stopLive()

    def _startLive(self) -> None:
        """Start folder watching for new recording stores."""
        folder_path = self._widget.path
        if not folder_path or not os.path.exists(folder_path):
            self._logger.error("No valid path selected for live reconstruction")
            self._widget.liveCheck.setChecked(False)
            return

        if not os.path.isdir(folder_path):
            self._logger.error(f"Path must be a directory for live mode: {folder_path}")
            self._widget.liveCheck.setChecked(False)
            return

        reconstructor = self._getActiveReconstructor()
        if reconstructor is None:
            self._logger.error("No active reconstructor available")
            self._widget.liveCheck.setChecked(False)
            return

        # Get the extension from the comm channel
        extension = self._commChannel.extension.value() if hasattr(self._commChannel, 'extension') else 'zarr'
        if not extension:
            extension = 'zarr'

        self._watchedFolder = folder_path
        self._storeQueue.clear()
        self._currentlyProcessing = False

        # Start file watcher
        self._fileWatcher = FileWatcher(folder_path, extension, pollTime=1)
        
        # Seed the queue with existing stores
        existing_stores = self._fileWatcher.filesInDirectory()
        for store_name in existing_stores:
            store_path = os.path.join(folder_path, store_name)
            self._storeQueue.append(store_path)
        
        # Connect signal for newly appearing stores
        self._fileWatcher.sigNewFiles.connect(self._onNewStoresDetected)
        
        # Start the watcher thread
        self._fileWatcher.start()
        
        # Create the live controller if needed and ensure signal is connected
        if self._liveController is None:
            self._liveController = LiveReconstructionController(self._commChannel)
        
        # Ensure the signal is connected (handles both new and existing controllers)
        try:
            self._liveController.sigFinished.disconnect(self._onStoreFinished)
        except TypeError:
            pass  # Signal wasn't connected yet
        self._liveController.sigFinished.connect(self._onStoreFinished)
        
        self._logger.info(f"Live mode started: watching {folder_path} for .{extension} stores")
        self._logger.info(f"Found {len(self._storeQueue)} existing store(s) to process")
        
        # Start processing the first store
        self._processNextStore()

    def _stopLive(self) -> None:
        """Stop folder watching and live reconstruction."""
        # Stop the file watcher
        if self._fileWatcher is not None:
            self._fileWatcher.stop()
            self._fileWatcher.quit()
            self._fileWatcher = None
        
        # Stop the live controller
        if self._liveController is not None:
            self._liveController.stop()
        
        # Clear the queue
        self._storeQueue.clear()
        self._currentlyProcessing = False
        self._watchedFolder = None
        
        self._logger.info("Live mode stopped")

    def _onNewStoresDetected(self, store_names: list) -> None:
        """Handle newly detected stores from FileWatcher."""
        for store_name in store_names:
            store_path = os.path.join(self._watchedFolder, store_name)
            self._storeQueue.append(store_path)
            self._logger.info(f"New store detected: {store_path}")
        
        # Try to process if we're not currently processing
        self._processNextStore()

    def _processNextStore(self) -> None:
        """Process the next store in the queue if not currently processing."""
        if self._currentlyProcessing:
            return
        
        if not self._storeQueue:
            self._logger.debug("No stores in queue to process")
            return
        
        store_path = self._storeQueue.popleft()
        self._currentlyProcessing = True
        
        self._logger.info(f"Processing store: {store_path}")
        
        reconstructor = self._getActiveReconstructor()
        if reconstructor is None:
            self._logger.error("No active reconstructor available, skipping store")
            self._currentlyProcessing = False
            self._processNextStore()
            return
        
        # Try to create a source for this store
        try:
            source = make_live_source(store_path, detector_name=None)
        except (NotImplementedError, ValueError) as exc:
            self._logger.warning(
                f"Skipping unsupported store {store_path}: {exc}"
            )
            self._currentlyProcessing = False
            self._processNextStore()
            return
        
        params = self._getReconstructorParams()

        try:
            started = self._liveController.start(
                reconstructor, source, params, source_arg=store_path
            )
        except Exception as e:
            self._logger.error(f"Failed to start reconstruction for {store_path}: {e}")
            started = False

        # start() returns False when the store had no readable frames yet (or
        # failed to open) — no sigFinished will arrive, so advance the queue
        # here to avoid a permanent stall.
        if not started:
            self._currentlyProcessing = False
            self._processNextStore()

    def _onStoreFinished(self) -> None:
        """Handle completion of a store reconstruction."""
        self._logger.debug("Store reconstruction finished")
        self._currentlyProcessing = False
        self._processNextStore()

    def _getActiveReconstructor(self):
        """Get the active reconstructor from the main view controller."""
        if self._mainController is None:
            return None
        return getattr(self._mainController, '_activeReconstructor', None)

    def _getReconstructorParams(self) -> dict:
        """Get the current reconstructor parameters from the view."""
        widget = getattr(self._mainController, '_widget', None)
        if widget is None:
            return {}

        getter = getattr(widget, 'getReconstructionParams', None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:
                self._logger.warning(f"Could not read reconstruction params from view: {exc}")

        par_tree = getattr(widget, 'parTree', None)
        for legacy_getter_name in ('get_values', 'get_param_dict'):
            legacy_getter = getattr(par_tree, legacy_getter_name, None)
            if callable(legacy_getter):
                try:
                    return legacy_getter()
                except Exception as exc:
                    self._logger.warning(
                        f"Could not read reconstruction params via {legacy_getter_name}: {exc}"
                    )

        return {}


# Copyright (C) 2020-2026 ImSwitch developers
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
