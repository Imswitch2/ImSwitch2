"""Controller for live reconstruction mode in the watcher UI."""

import os
import re
from collections import deque

import h5py

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live import Hdf5MultiFileLapseSource, ZarrMultiFileLapseSource, make_live_source
from imswitch.improcess.live.sources import _lapse_index_template
from .basecontrollers import ImProcessWidgetController
from .LiveReconstructionController import LiveReconstructionController

# How deep below the selected folder to look for recording stores. Measurement
# folders are created one level under the watched root; depth 2 covers
# "select the parent folder" without scanning the whole tree.
_DISCOVERY_MAX_DEPTH = 2
# Output subdirectories (reconstructions/logs) that must never be ingested.
_DISCOVERY_EXCLUDE_DIRS = {"rec", "deskew", "Mini_Recon_Results", "__pycache__"}


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
        self._scanTimer = None
        self._storeQueue = deque()
        self._seenKeys = set()
        self._currentlyProcessing = False
        self._watchedFolder = None
        self._extension = "zarr"
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
        self._extension = extension or 'zarr'

        self._watchedFolder = folder_path
        self._storeQueue.clear()
        self._seenKeys.clear()
        self._currentlyProcessing = False

        # Create the live controller if needed and ensure signal is connected
        if self._liveController is None:
            self._liveController = LiveReconstructionController(self._commChannel)
        try:
            self._liveController.sigFinished.disconnect(self._onStoreFinished)
        except TypeError:
            pass  # Signal wasn't connected yet
        self._liveController.sigFinished.connect(self._onStoreFinished)

        # Poll the folder tree (the selected folder and its measurement
        # sub-folders) for new recording stores. Recursive so the user can
        # select the parent folder and have new measurement folders picked up.
        self._scanTimer = QtCore.QTimer(self)
        self._scanTimer.setInterval(1000)
        self._scanTimer.timeout.connect(self._scanForStores)
        self._scanTimer.start()

        self._logger.info(
            f"Live mode started: watching {folder_path} (recursively) for .{self._extension} stores"
        )
        self._scanForStores()

    def _stopLive(self) -> None:
        """Stop folder watching and live reconstruction."""
        if self._scanTimer is not None:
            self._scanTimer.stop()
            self._scanTimer.deleteLater()
            self._scanTimer = None

        if self._liveController is not None:
            self._liveController.stop()

        self._storeQueue.clear()
        self._seenKeys.clear()
        self._currentlyProcessing = False
        self._watchedFolder = None

        self._logger.info("Live mode stopped")

    def _scanForStores(self) -> None:
        """Discover new recording stores under the watched folder.

        Groups per-file timelapse stores (``..._scan__NN__...``) into a single
        job so the whole lapse accumulates into one multi-timepoint result;
        non-lapse stores are queued individually. Each lapse / store is enqueued
        only once (keyed by its index-independent name).
        """
        if not self._watchedFolder:
            return

        new_jobs = 0
        for store_path in self._discoverStores(self._watchedFolder):
            key, is_lapse = self._lapse_key(store_path)
            if key in self._seenKeys:
                continue
            self._seenKeys.add(key)
            self._storeQueue.append((store_path, is_lapse))
            new_jobs += 1
            self._logger.info(
                f"Discovered {'timelapse' if is_lapse else 'store'}: {store_path}"
            )

        if new_jobs:
            self._processNextStore()

    def _discoverStores(self, root: str) -> list:
        """Return sorted recording-store paths under ``root`` (depth-limited)."""
        suffix = f".{self._extension}".lower()
        found = []
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune output/cache dirs and respect the depth limit.
            dirnames[:] = [d for d in dirnames if d not in _DISCOVERY_EXCLUDE_DIRS]
            depth = dirpath[len(root):].count(os.sep)
            if depth >= _DISCOVERY_MAX_DEPTH:
                dirnames[:] = []
            # Zarr stores are directories; HDF5 are files. Check both.
            for name in list(dirnames) + filenames:
                if name.lower().endswith(suffix):
                    found.append(os.path.join(dirpath, name))
        return sorted(found)

    def _lapse_key(self, store_path: str):
        """Return ``(dedup_key, is_lapse)`` for a discovered store.

        A per-file lapse member maps to an index-independent key so all its
        timepoint files dedup to one job; other stores key on their own path.
        
        A store is a per-file lapse member if EITHER:
        - Its name has a strippable trailing integer run (lenient heuristic)
        - Its metadata confirms it's a multi-file lapse (recording:single_lapse_file=False
          with recording:num_timepoints>1)
        """
        # First try filename-based heuristic
        template = _lapse_index_template(store_path)
        if template is not None:
            folder, prefix, width, suffix, _ = template
            return (folder, prefix, suffix), True
        
        # Fallback: check for trailing integer run (more lenient than 'scan' only)
        folder = os.path.dirname(store_path)
        name = os.path.basename(store_path)
        # Match any trailing digits preceded by separator
        match = re.search(r'[_\-.](\d+)(\.[a-zA-Z0-9]+)?$', name)
        if match:
            # Strip the integer run to create the dedup key
            start_idx = match.start(1)
            prefix = name[:start_idx]
            suffix = name[match.end(1):]
            return (folder, prefix, suffix), True
        
        # Metadata-based check: try to read store attributes
        is_multifile_lapse = self._is_multifile_lapse_from_metadata(store_path)
        if is_multifile_lapse:
            # No integer pattern but metadata confirms multi-file lapse
            # Use the full name as prefix (each file will be unique but this flags it as lapse)
            return (folder, name, ''), True
        
        return store_path, False

    def _is_multifile_lapse_from_metadata(self, store_path: str) -> bool:
        """Check if metadata confirms this is a multi-file lapse member."""
        try:
            suffix = os.path.splitext(store_path)[1].lower()
            
            if suffix == '.zarr':
                import zarr
                root = zarr.open(store_path, mode='r')
                single_lapse = root.attrs.get('recording:single_lapse_file')
                num_tp = root.attrs.get('recording:num_timepoints')
                
                # Multi-file if explicitly marked as not single-file AND has multiple timepoints
                if single_lapse is False and num_tp and int(num_tp) > 1:
                    return True
            
            elif suffix in {'.h5', '.hdf5', '.hdf'}:
                with h5py.File(store_path, 'r') as f:
                    single_lapse = f.attrs.get('recording:single_lapse_file')
                    num_tp = f.attrs.get('recording:num_timepoints')
                    
                    # Multi-file if explicitly marked as not single-file AND has multiple timepoints
                    if single_lapse is False and num_tp and int(num_tp) > 1:
                        return True
        
        except (OSError, PermissionError, Exception):
            # If we can't read the file (e.g., being written), fall back to filename
            pass
        
        return False

    def _processNextStore(self) -> None:
        """Process the next store in the queue if not currently processing."""
        if self._currentlyProcessing:
            return
        
        if not self._storeQueue:
            self._logger.debug("No stores in queue to process")
            return
        
        store_path, is_lapse = self._storeQueue.popleft()
        self._currentlyProcessing = True

        self._logger.info(f"Processing {'timelapse' if is_lapse else 'store'}: {store_path}")

        reconstructor = self._getActiveReconstructor()
        if reconstructor is None:
            self._logger.error("No active reconstructor available, skipping store")
            self._currentlyProcessing = False
            self._processNextStore()
            return

        # Per-file timelapses stream all their timepoint files into one
        # accumulated multi-timepoint result; other stores use the format-based
        # source (single growing array, single-file scan{N} lapse, HDF5, ...).
        try:
            if is_lapse:
                # Select multi-file source based on extension
                suffix = os.path.splitext(store_path)[1].lower()
                if suffix == '.zarr':
                    source = ZarrMultiFileLapseSource(store_path, detector_name=None)
                elif suffix in {'.h5', '.hdf5', '.hdf'}:
                    source = Hdf5MultiFileLapseSource(store_path, detector_name=None)
                else:
                    raise ValueError(f"Unsupported lapse format: {suffix}")
            else:
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
