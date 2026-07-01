"""Controller for live reconstruction mode in the watcher UI."""

import os
import re
from collections import deque
from typing import Any

import h5py
import zarr

from qtpy import QtCore

from imswitch.imcommon.model.logging import initLogger
from imswitch.improcess.live import Hdf5MultiFileLapseSource, ZarrMultiFileLapseSource, make_live_source
from imswitch.improcess.live.sources import (
    _is_zarr_array,
    _is_zarr_group,
    _lapse_index_template,
)
from .basecontrollers import ImProcessWidgetController
from .LiveReconstructionController import LiveReconstructionController

# How deep below the selected folder to look for recording stores. Measurement
# folders are created one level under the watched root; depth 2 covers
# "select the parent folder" without scanning the whole tree.
_DISCOVERY_MAX_DEPTH = 2
# Output subdirectories (reconstructions/logs) that must never be ingested.
_DISCOVERY_EXCLUDE_DIRS = {"rec", "deskew", "Mini_Recon_Results", "__pycache__"}
_LIVE_EXTENSION_SUFFIXES = {
    "zarr": {".zarr"},
    "hdf5": {".hdf5", ".h5", ".hdf"},
    "h5": {".hdf5", ".h5", ".hdf"},
    "hdf": {".hdf5", ".h5", ".hdf"},
}


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
            f"Live mode started: watching {folder_path} (recursively) for "
            f"{self._format_suffixes_for_log(self._active_live_suffixes())} stores"
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

        # Always check the queue (even if no new jobs) to re-evaluate pending
        # stores that may have become complete since the last tick.
        self._processNextStore()

    def _discoverStores(self, root: str) -> list:
        """Return sorted recording-store paths under ``root`` (depth-limited)."""
        suffixes = self._active_live_suffixes()
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
                if self._matches_live_suffix(name, suffixes):
                    found.append(os.path.join(dirpath, name))
        return sorted(found)

    def _active_live_suffixes(self) -> set[str]:
        """Return file suffixes the live watcher should ingest."""
        reconstructor = self._getActiveReconstructor()
        if reconstructor is not None:
            suffixes = self._live_suffixes_for_reconstructor(reconstructor)
            if suffixes:
                return suffixes
        return self._live_suffixes_for_extension(self._extension)

    @staticmethod
    def _live_suffixes_for_reconstructor(reconstructor) -> set[str]:
        suffixes: set[str] = set()
        for extension in getattr(reconstructor, "file_extensions", []) or []:
            extension = (extension or "").lower().strip().lstrip(".")
            if extension in _LIVE_EXTENSION_SUFFIXES:
                suffixes.update(_LIVE_EXTENSION_SUFFIXES[extension])
        return suffixes

    @staticmethod
    def _live_suffixes_for_extension(extension: str | None) -> set[str]:
        extension = (extension or "").lower().strip().lstrip(".")
        if not extension:
            return {".zarr"}
        if extension in _LIVE_EXTENSION_SUFFIXES:
            return set(_LIVE_EXTENSION_SUFFIXES[extension])
        return {f".{extension}"}

    @staticmethod
    def _matches_live_suffix(name: str, suffixes: set[str]) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(suffix) for suffix in suffixes)

    @staticmethod
    def _format_suffixes_for_log(suffixes: set[str]) -> str:
        return "/".join(sorted(suffixes))

    def _lapse_key(self, store_path: str):
        """Return ``(dedup_key, is_lapse)`` for a discovered store.

        A per-file lapse member maps to an index-independent key so all its
        timepoint files dedup to one job; other stores key on their own path.
        
        A store is a per-file lapse member if EITHER:
        - Its name carries an explicit ``scan<NN>`` index (ImSwitch2 ``_scanNN_``
          or legacy ``_scan__NN__``), or
        - Its own metadata confirms a multi-file lapse
          (``recording:single_lapse_file=False`` with
          ``recording:num_timepoints>1``).

        A bare trailing integer is NOT treated as a lapse on its own: a single
        recording whose name merely ends in a number (e.g. ``..._rec_1.zarr``)
        must not be mis-grouped into a timelapse job (Finding 4 in
        docs/live_reconstruction_audit.md).
        """
        # 1. Explicit scan<NN> index template.
        template = _lapse_index_template(store_path)
        if template is not None:
            folder, prefix, width, suffix, _ = template
            return (folder, prefix, suffix), True

        # 2. Metadata-confirmed multi-file lapse. Only here do we trust a
        #    trailing-integer run to group siblings, because the store's own
        #    metadata vouches that it is a per-file lapse member.
        if self._is_multifile_lapse_from_metadata(store_path):
            folder = os.path.dirname(store_path)
            name = os.path.basename(store_path)
            match = re.search(r'[_\-.](\d+)(\.[a-zA-Z0-9]+)?$', name)
            if match:
                return (folder, name[:match.start(1)], name[match.end(1):]), True
            return (folder, name, ''), True

        # 3. Not a per-file lapse: key the single store on its own path.
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
                
                # Multi-file if explicitly marked as not single-file AND has
                # multiple timepoints. Use a falsy check (not ``is False``): h5py
                # and zarr read the attribute back as numpy.bool_/int, so the
                # identity test was always False and this path was dead.
                if (single_lapse is not None and not bool(single_lapse)
                        and num_tp and int(num_tp) > 1):
                    return True
            
            elif suffix in {'.h5', '.hdf5', '.hdf'}:
                with h5py.File(store_path, 'r') as f:
                    single_lapse = f.attrs.get('recording:single_lapse_file')
                    num_tp = f.attrs.get('recording:num_timepoints')
                    
                    # Falsy check (not ``is False``) — see the Zarr branch.
                    if (single_lapse is not None and not bool(single_lapse)
                            and num_tp and int(num_tp) > 1):
                        return True
        
        except (OSError, PermissionError, Exception):
            # If we can't read the file (e.g., being written), fall back to filename
            pass
        
        return False

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        """Coerce a value to bool, matching the logic used by LiveSources."""
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {'0', 'false', 'no', 'off'}
        return bool(value)

    def _is_store_complete(self, store_path: str, is_lapse: bool) -> bool:
        """Check if a recording store is complete (ready for reconstruction).
        
        A store is complete when its 'writing' attribute is absent or False.
        For multi-file lapses, all timepoint files must exist and the last one
        must be complete.
        
        Returns False on any error (store still being created/written).
        """
        if is_lapse:
            return self._is_lapse_complete(store_path)
        return self._is_single_file_complete(store_path)

    def _writing_attr_complete(self, writing: Any) -> bool:
        """Interpret a dataset ``writing`` attribute as a completeness flag.

        Absent ⇒ complete (legacy/external store, matching upstream
        ``DataObj.checkLock``); otherwise complete iff the flag is falsy.
        """
        if writing is None:
            return True
        return not self._coerce_bool(writing, default=True)

    def _is_single_file_complete(self, store_path: str) -> bool:
        """Check if a single-file store is complete."""
        suffix = os.path.splitext(store_path)[1].lower()
        if suffix == '.zarr':
            return self._check_zarr_complete(store_path)
        if suffix in {'.h5', '.hdf5', '.hdf'}:
            return self._check_hdf5_complete(store_path)
        return False

    def _check_zarr_complete(self, store_path: str) -> bool:
        """Check if a Zarr store is complete.

        Uses the version-tolerant ``_is_zarr_array``/``_is_zarr_group`` helpers
        (not direct ``zarr.Array``/``zarr.Group``) so a zarr build that does not
        expose those types at the top level cannot make every store look
        forever-incomplete.
        """
        try:
            root = zarr.open(store_path, mode='r')

            if _is_zarr_array(root):
                return self._writing_attr_complete(root.attrs.get('writing'))

            # Structured layout: the first detector's ``data`` array (or a bare
            # detector array) carries the ``writing`` flag.
            for key in root.keys():
                child = root[key]
                if _is_zarr_group(child):
                    if 'data' in child and _is_zarr_array(child['data']):
                        return self._writing_attr_complete(child['data'].attrs.get('writing'))
                elif _is_zarr_array(child):
                    return self._writing_attr_complete(child.attrs.get('writing'))

            return False  # no dataset created yet
        except Exception as e:
            # Lenient by design (retry next tick), but log so a real failure
            # — e.g. an unexpected zarr API change — is not silently invisible.
            self._logger.debug(f"Zarr completeness check failed for {store_path}: {e}")
            return False

    def _check_hdf5_complete(self, store_path: str) -> bool:
        """Check if an HDF5 store is complete."""
        try:
            with h5py.File(store_path, 'r') as f:
                for key in f.keys():
                    item = f[key]
                    if isinstance(item, h5py.Group):
                        if 'data' in item:
                            return self._writing_attr_complete(item['data'].attrs.get('writing'))
                    elif isinstance(item, h5py.Dataset):
                        return self._writing_attr_complete(item.attrs.get('writing'))
                return False  # no dataset created yet
        except Exception as e:
            self._logger.debug(f"HDF5 completeness check failed for {store_path}: {e}")
            return False

    def _is_lapse_complete(self, store_path: str) -> bool:
        """Check if a multi-file lapse is complete."""
        template = _lapse_index_template(store_path)
        if template is None:
            # No index template, fall back to single-file check
            return self._is_single_file_complete(store_path)
        
        folder, prefix, width, suffix, first_index = template
        
        # Try to read num_timepoints from the seed file's metadata
        num_timepoints = self._read_num_timepoints(store_path)
        
        if num_timepoints is not None and num_timepoints >= 1:
            # Known number of timepoints: all files must exist and last must be complete
            last_index = first_index + num_timepoints - 1
            for i in range(num_timepoints):
                index = first_index + i
                tp_path = os.path.join(folder, prefix + str(index).zfill(width) + suffix)
                if not os.path.exists(tp_path):
                    return False
            
            # Check if the last file is complete
            last_path = os.path.join(folder, prefix + str(last_index).zfill(width) + suffix)
            return self._is_single_file_complete(last_path)
        
        # Unknown number of timepoints: find highest contiguous index
        # and check if it's complete (conservative approach)
        index = first_index
        highest_complete_index = None
        
        while True:
            tp_path = os.path.join(folder, prefix + str(index).zfill(width) + suffix)
            if not os.path.exists(tp_path):
                break
            
            if self._is_single_file_complete(tp_path):
                highest_complete_index = index
            else:
                # This file is not complete yet, so the lapse is not complete
                return False
            
            index += 1
        
        # At least one complete file was found
        return highest_complete_index is not None

    def _read_num_timepoints(self, store_path: str) -> int | None:
        """Read recording:num_timepoints from store metadata."""
        suffix = os.path.splitext(store_path)[1].lower()
        
        try:
            if suffix == '.zarr':
                root = zarr.open(store_path, mode='r')
                num_tp = root.attrs.get('recording:num_timepoints')
                if num_tp is not None:
                    return int(num_tp)
            elif suffix in {'.h5', '.hdf5', '.hdf'}:
                with h5py.File(store_path, 'r') as f:
                    num_tp = f.attrs.get('recording:num_timepoints')
                    if num_tp is not None:
                        return int(num_tp)
        except Exception as e:
            self._logger.debug(f"Could not read num_timepoints from {store_path}: {e}")

        return None

    def _processNextStore(self) -> None:
        """Process the next complete store in the queue if not currently processing.
        
        Iterates the queue to find the FIRST complete store, enabling later
        already-complete stores to bypass earlier still-recording ones (no
        head-of-line blocking).
        """
        if self._currentlyProcessing:
            return
        
        if not self._storeQueue:
            self._logger.debug("No stores in queue to process")
            return
        
        # Find the first complete store in the queue
        store_path = None
        is_lapse = False
        queue_index = None
        
        for idx, (path, lapse) in enumerate(self._storeQueue):
            if self._is_store_complete(path, lapse):
                store_path = path
                is_lapse = lapse
                queue_index = idx
                break
        
        # No complete store found yet, will retry on next tick
        if store_path is None:
            self._logger.debug("No complete stores in queue yet, waiting...")
            return
        
        # Remove the complete store from the queue
        del self._storeQueue[queue_index]
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
