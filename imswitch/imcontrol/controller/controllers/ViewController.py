from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.managers import LeasePurpose
from ..basecontrollers import ImConWidgetController


class ViewController(ImConWidgetController):
    """ Linked to ViewWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acqHandle = None
        self._acqReleasePending = False
        self._closed = False

        self._widget.sigLiveviewToggled.connect(self.liveview)
        self._master.detectorsManager.setLiveViewLeaseProvider(
            self._reseedLiveViewLease
        )

        self._populateDetectorSelection()
        self._widget.sigDetectorSelectionChanged.connect(
            self._onDetectorSelectionToggled
        )
        # The manager applies deferred changes itself at the next scan
        # iteration, so the UI follows the model rather than assuming its own
        # request took effect.
        self._master.detectorsManager.sigDetectorSelectionChanged.connect(
            self._onDetectorSelectionApplied
        )

    def _populateDetectorSelection(self):
        detectorsManager = self._master.detectorsManager
        detectorNames = detectorsManager.getAllDeviceNames(
            condition=lambda detector: detector.forAcquisition
        )
        self._widget.setDetectorSelectionOptions(detectorNames)
        selected = detectorsManager.getSelectedDetectors()
        for detectorName in detectorNames:
            self._widget.setDetectorSelected(detectorName,
                                             detectorName in selected)

    def _onDetectorSelectionToggled(self, detectorName, selected):
        """ User ticked or unticked a detector. """
        try:
            appliedNow = self._master.detectorsManager.setDetectorSelected(
                detectorName, selected
            )
        except Exception as e:
            self._logger.error(
                f'Could not change the selection of "{detectorName}": {e}',
                exc_info=True,
            )
            # Put the box back where the model actually is.
            self._widget.setDetectorSelected(
                detectorName,
                self._master.detectorsManager.isDetectorSelected(detectorName),
            )
            return
        self._widget.setDetectorSelectionPending(detectorName, not appliedNow)

    def _onDetectorSelectionApplied(self, detectorName, selected):
        """ The model changed — sync the box and clear any pending mark. """
        self._widget.setDetectorSelected(detectorName, selected)
        self._widget.setDetectorSelectionPending(detectorName, False)

    def _liveViewDetectors(self):
        """ The selected free-running detectors.

        Live view used to lease every ``forAcquisition`` detector, scan-driven
        ones included, so APD/PMT/TimeTagger were armed by the live-view
        toggle and a scan silently depended on live view being on to produce
        data at all. Scan-driven detectors are armed by the scan's own SCAN
        lease now (see ScanExecutionCoordinator), so live view leases only what
        it actually streams — and only what the user selected.
        """
        detectorsManager = self._master.detectorsManager
        selected = detectorsManager.getSelectedDetectors()
        return detectorsManager.getAllDeviceNames(
            condition=lambda detector: (detector.forAcquisition
                                        and not detector.isScanDriven
                                        and detector.name in selected)
        )

    def _reseedLiveViewLease(self):
        """ Re-acquire the live-view lease over the current selection.

        Registered with the DetectorsManager so a selection change takes effect
        while live view is running, instead of waiting for the user to toggle
        it off and on. The manager owns the selection but not this lease, so it
        calls back here to swap it.

        A no-op when live view is not running: there is no lease to re-seed,
        and the next start will pick up the new selection anyway.
        """
        if self._acqHandle is None or self.__dict__.get('_closed', False):
            return
        detectorNames = self._liveViewDetectors()
        previous = self._acqHandle
        if not detectorNames:
            # Everything free-running was deselected; acquire() rejects an
            # empty set, so simply stop streaming.
            self._releaseLiveViewLease()
            return
        # Acquire the new set BEFORE releasing the old one so detectors common
        # to both never drop to a zero refcount and get stopped and restarted
        # mid-stream.
        self._acqHandle = self._master.detectorsManager.acquire(
            detectorNames, LeasePurpose.LIVE_VIEW
        )
        self._acqReleasePending = False
        try:
            self._master.detectorsManager.release(previous)
        except Exception as e:
            self._logger.error(
                f'Failed to release the superseded live-view lease: {e}',
                exc_info=True,
            )

    def liveview(self, enabled):
        """ Start liveview and activate detector acquisition. """
        if enabled and self.__dict__.get('_closed', False):
            return
        if enabled and self._acqHandle is not None:
            if not self.__dict__.get('_acqReleasePending', False):
                return
            if not self._releaseLiveViewLease():
                raise RuntimeError(
                    'The previous live-view detector lease is still stopping.'
                )
        if enabled:
            detectorNames = self._liveViewDetectors()
            if not detectorNames:
                return  # nothing free-running to stream; acquire rejects empty
            self._acqHandle = self._master.detectorsManager.acquire(
                detectorNames, LeasePurpose.LIVE_VIEW
            )
            self._acqReleasePending = False
        elif not enabled and self._acqHandle is not None:
            if not self._releaseLiveViewLease():
                raise RuntimeError(
                    'The live-view detector lease is still stopping.'
                )

    def closeEvent(self):
        self._closed = True
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Retry and report the exact live-view lease release."""
        if (
            self.__dict__.get('_closed', False)
            and self.__dict__.get('_acqHandle') is not None
        ):
            self._releaseLiveViewLease()
        return self.__dict__.get('_acqHandle') is None

    def _releaseLiveViewLease(self) -> bool:
        """Release once without losing the handle when poller stop times out."""
        handle = self.__dict__.get('_acqHandle')
        if handle is None:
            return True
        try:
            self._master.detectorsManager.release(handle)
        except Exception as e:
            self._acqReleasePending = True
            self._logger.error(
                f'Failed to release the live-view detector lease: {e}',
                exc_info=True,
            )
            # The manager stops the final frame poller before it consumes the
            # handle. Keep exact retry authority if that bounded join times out.
            return False
        if self.__dict__.get('_acqHandle') is handle:
            self._acqHandle = None
        self._acqReleasePending = False
        return True

    def get_image(self, detectorName):
        if detectorName is None:
            return self._master.detectorsManager.execOnCurrent(
                lambda c: c.getLatestFrameShared()
            )
        else:
            return (
                self._master.detectorsManager[detectorName]
                .getLatestFrameShared()
            )

    @APIExport(runOnUIThread=True)
    def setDetectorSelected(self, detectorName: str, selected: bool) -> bool:
        """ Select or deselect a detector for ImSwitch-managed acquisition.

        Deselected detectors are left out of live view and of scans. A
        detector an explicit consumer is holding — a recording, a workflow, an
        event modality — still runs regardless, so this cannot silently break
        an acquisition already under way.

        Returns False when the change is queued because a scan currently owns
        the detector; it is applied at the next scan iteration.
        """
        return self._master.detectorsManager.setDetectorSelected(
            detectorName, selected
        )

    @APIExport()
    def getSelectedDetectors(self) -> list:
        """ Names of the detectors currently selected for acquisition. """
        return sorted(self._master.detectorsManager.getSelectedDetectors())

    @APIExport(runOnUIThread=True)
    def setLiveViewActive(self, active: bool) -> None:
        """ Sets whether the LiveView is active and updating. """
        self._widget.setLiveViewActive(active)


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
