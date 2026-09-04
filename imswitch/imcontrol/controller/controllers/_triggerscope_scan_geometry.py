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
    RESOLFT_COUNTERS,
    build_triggerscope_resolft_layouts,
    resolft_counter,
    resolft_position_count,
    scan_driven_detector_names,
)


#: Firmware counters whose product is the number of camera frames in one run.
#: Derived from the one table the layout builder uses, so the frame
#: expectation and the layout cannot disagree about which counters exist.
_FRAME_COUNT_KEYS = tuple(key for key, _kind, _step in RESOLFT_COUNTERS)


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
        # No counter is defaulted: a missing or unreadable one used to count
        # as a single step here AND in the layout, so the recording armed for
        # the wrong frame count with a certain layout to match.
        return resolft_position_count(scanParameters)

    def getNumCamTTL(self) -> dict:
        """Camera TTL pulses per scan position, per detector.

        These modes drive at most one camera, named by the ``CameraTTL`` role,
        with one pulse per position. The firmware gates that camera on a fixed
        line the software never chooses, so this is a *declaration* of which
        detector receives that pulse, not a decision about it.

        An empty mapping therefore means the mode has no camera declared, and
        a scan-mode recording of a camera is refused rather than assumed to
        get one pulse per position. That assumption used to be made here and,
        identically, by the recording gate, so the cross-check between the two
        compared a default with itself and a free-running camera was recorded
        as a certain, complete scan.
        """
        _, deviceParameters = self._triggerScopeGeometryParameters()
        device = deviceParameters.get('CameraTTL')
        if not device or device not in self._setupInfo.detectors:
            return {}
        return {device: 1}

    def getAcquisitionLayouts(self, detectorNames):
        """Return the firmware's time/cycle/plane event order."""
        scanParameters, deviceParameters = self._triggerScopeGeometryParameters()
        pulseCounts = self.getNumCamTTL()
        try:
            return build_triggerscope_resolft_layouts(
                detectorNames,
                scan_parameters=scanParameters,
                scan_source=type(self).__name__,
                pulse_counts=pulseCounts,
                scan_driven_detectors=scan_driven_detector_names(
                    self, detectorNames
                ),
            )
        except ValueError as error:
            if pulseCounts:
                raise
            # The generic refusal tells the user to gate the detector in the
            # scan's TTL cycle, which in these modes is not something they
            # can do: the firmware owns the camera line. What they can do is
            # say which detector is wired to it, so name that control.
            declared = deviceParameters.get('CameraTTL')
            reason = (
                f'the selected camera {declared!r} is not a detector in this '
                'setup' if declared else 'no camera is selected'
            )
            raise ValueError(
                f'{error} In this TriggerScope mode the firmware gates the '
                f'camera on a fixed line, so the scan cannot add a pulse for '
                f'it -- it can only be told which detector receives that '
                f'pulse. Set "Camera used for detection" in the scan panel '
                f'({reason}).'
            ) from error


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
