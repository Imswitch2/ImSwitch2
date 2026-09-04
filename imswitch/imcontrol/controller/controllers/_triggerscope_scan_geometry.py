"""Recording geometry for autonomous TriggerScope firmware scans.

A scan-once or scan-lapse recording is armed with an exact frame expectation,
so every scan controller that a recording can be bound to has to be able to say
how many camera frames its run produces. Without that the RecordingController
falls back to whichever controller happens to implement the accessors, which on
a multi-scanner TriggerScope rig is the wrong one.

The RESOLFT-family firmware modes all step the same three counters, so the
formula lives here once rather than in each controller. ``TriggerScopeRaster``
keeps its own implementation: its frame count comes from the raster pixel grid,
not from these counters.
"""

from ._acquisition_layout_source import (
    build_triggerscope_resolft_layouts,
    scan_driven_detector_names,
)


#: Firmware counters whose product is the number of camera frames in one run.
_FRAME_COUNT_KEYS = ('roSteps', 'cycleSteps', 'timeLapsePoints')


def _positiveCount(value) -> int:
    """Coerce a widget counter to a frame count of at least one.

    Widget spin boxes return ints, but a parameter restored from a scan file or
    a saved component state can arrive as a string or a float. A counter that
    cannot be read as a number is treated as a single step, which keeps the
    product honest instead of collapsing the whole expectation to zero.
    """
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return 1
    return max(count, 1)


class TriggerScopeScanGeometryMixin:
    """Report the recording geometry of one TriggerScope firmware scan run.

    Inheriting controllers keep ownership of their parameter dicts. The default
    implementation reads the controller-level dicts; a controller that hosts
    several modes overrides :meth:`_triggerScopeGeometryParameters` to return
    the visible mode's dicts instead.
    """

    def _triggerScopeGeometryParameters(self):
        """Return ``(scanParameters, deviceParameters)`` for the visible mode."""
        self.getParameters()
        return self._scanParameterDict, self._deviceParameterDict

    def getNumScanPositions(self) -> int:
        """Number of camera frames one run of this scan produces.

        Consumed by the RecordingController in scan-once and scan-lapse mode as
        the number of frames to record.
        """
        scanParameters, _ = self._triggerScopeGeometryParameters()
        positions = 1
        for key in _FRAME_COUNT_KEYS:
            positions *= _positiveCount(scanParameters.get(key))
        return positions

    def getNumCamTTL(self) -> dict:
        """Camera TTL pulses per scan position, per detector.

        These modes drive at most one camera, named by the ``CameraTTL`` role,
        with one pulse per position. An empty mapping means "no detector has a
        non-default pulse count", which the RecordingManager reads as one pulse
        each — the right answer for a mode with no camera role configured.
        """
        _, deviceParameters = self._triggerScopeGeometryParameters()
        device = deviceParameters.get('CameraTTL')
        if not device or device not in self._setupInfo.detectors:
            return {}
        return {device: 1}

    def getAcquisitionLayouts(self, detectorNames):
        """Return the firmware's time/cycle/plane event order."""
        scanParameters, _ = self._triggerScopeGeometryParameters()
        return build_triggerscope_resolft_layouts(
            detectorNames,
            scan_parameters=scanParameters,
            scan_source=type(self).__name__,
            pulse_counts=self.getNumCamTTL(),
            scan_driven_detectors=scan_driven_detector_names(
                self, detectorNames
            ),
        )


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
