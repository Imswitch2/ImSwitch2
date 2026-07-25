from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.managers import LeasePurpose
from ..basecontrollers import ImConWidgetController


class ViewController(ImConWidgetController):
    """ Linked to ViewWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acqHandle = None

        self._widget.sigLiveviewToggled.connect(self.liveview)

    def _liveViewDetectors(self):
        """ Free-running detectors only.

        Live view used to lease every ``forAcquisition`` detector, scan-driven
        ones included, so APD/PMT/TimeTagger were armed by the live-view
        toggle and a scan silently depended on live view being on to produce
        data at all. Scan-driven detectors are armed by the scan's own SCAN
        lease now (see ScanExecutionCoordinator), so live view leases only what
        it actually streams. Phase 5 narrows this further to the user's
        selection.
        """
        return self._master.detectorsManager.getAllDeviceNames(
            condition=lambda detector: (detector.forAcquisition
                                        and not detector.isScanDriven)
        )

    def liveview(self, enabled):
        """ Start liveview and activate detector acquisition. """
        if enabled and self._acqHandle is None:
            detectorNames = self._liveViewDetectors()
            if not detectorNames:
                return  # nothing free-running to stream; acquire rejects empty
            self._acqHandle = self._master.detectorsManager.acquire(
                detectorNames, LeasePurpose.LIVE_VIEW
            )
        elif not enabled and self._acqHandle is not None:
            self._master.detectorsManager.release(self._acqHandle)
            self._acqHandle = None

    def closeEvent(self):
        if self._acqHandle is not None:
            self._master.detectorsManager.release(self._acqHandle)
            self._acqHandle = None

    def get_image(self, detectorName):
        if detectorName is None:
            return self._master.detectorsManager.execOnCurrent(lambda c: c.getLatestFrame())
        else:
            return self._master.detectorsManager[detectorName].getLatestFrame()

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
