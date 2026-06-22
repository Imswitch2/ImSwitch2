"""Controller for live reconstruction mode in the watcher UI."""

import os

from imswitch.imcommon.model.logging import initLogger
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
        self._widget.sigLiveChanged.connect(self._onLiveToggled)

    def _onLiveToggled(self, enabled: bool) -> None:
        """Handle live reconstruction toggle."""
        if enabled:
            self._startLive()
        else:
            self._stopLive()

    def _startLive(self) -> None:
        """Start live reconstruction with the active reconstructor and selected source."""
        path = self._widget.path
        if not path or not os.path.exists(path):
            self._logger.error("No valid path selected for live reconstruction")
            self._widget.liveCheck.setChecked(False)
            return

        reconstructor = self._getActiveReconstructor()
        if reconstructor is None:
            self._logger.error("No active reconstructor available")
            self._widget.liveCheck.setChecked(False)
            return

        source = self._selectSource(path)
        if source is None:
            self._widget.liveCheck.setChecked(False)
            return

        params = self._getReconstructorParams()

        if self._liveController is None:
            self._liveController = LiveReconstructionController(self._commChannel)

        try:
            self._liveController.start(reconstructor, source, params, source_arg=path)
            self._logger.info(f"Live reconstruction started: {reconstructor.name} on {path}")
        except Exception as e:
            self._logger.error(f"Failed to start live reconstruction: {e}")
            self._widget.liveCheck.setChecked(False)

    def _stopLive(self) -> None:
        """Stop live reconstruction."""
        if self._liveController is not None:
            self._liveController.stop()
            self._logger.info("Live reconstruction stopped")

    def _getActiveReconstructor(self):
        """Get the active reconstructor from the main view controller."""
        if self._mainController is None:
            return None
        return getattr(self._mainController, '_activeReconstructor', None)

    def _getReconstructorParams(self) -> dict:
        """Get the current reconstructor parameters from the view."""
        try:
            if hasattr(self._mainController, '_widget') and hasattr(self._mainController._widget, 'parTree'):
                return self._mainController._widget.parTree.get_param_dict()
        except Exception:
            pass
        return {}

    def _selectSource(self, path: str):
        """Select the appropriate LiveSource based on file format.

        Routes through the shared ``make_live_source`` factory; unsupported
        formats (e.g. TIFF) un-toggle live mode with a clear message rather than
        raising into the UI.

        Returns:
            LiveSource instance or None if format is not supported.
        """
        try:
            return make_live_source(path, detector_name=None)
        except (NotImplementedError, ValueError) as exc:
            self._logger.warning(
                f"Live reconstruction not supported for {path}: {exc} "
                "Use batch watch mode instead (supported: .zarr, .h5, .hdf5)."
            )
            return None


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
