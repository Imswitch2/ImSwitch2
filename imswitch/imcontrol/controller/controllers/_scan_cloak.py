"""The controller half of a scan cloak (docs/simple-point-scan-plan.md §10).

:class:`ScanCloakController` is a mixin placed in front of the backend scan
controller it drives::

    class ScanControllerSimplePointScan(ScanCloakController, ScanControllerAdvanced)

It is created with the cloak's panel (a ``ScanCloakPanel``) and hands the
backend its own, unchanged widget, so everything the backend controller does
talks to the widget it was written for. On top it keeps the cloak's plan:

* **On the simple page** the cloak supplies the scan dicts and runs the scan
  by driving the backend's own controls: Live is the backend's Repeat box,
  Stop unticks it. The backend's lifecycle does the rest.
* **On the Advanced page** every override here falls through: Start, Repeat,
  Stop and saved files behave exactly as in the backend's own panel.
* **The backend widget mirrors the cloak's acquisition** while the simple
  page shows, so the Advanced page is always the same scan. Whatever writes
  into that widget meanwhile -- a script export, a shared attribute, a loaded
  file -- is taken back into the plan, or, when the cloak cannot show it,
  opens the Advanced page with the reason.

A cloak implements the hooks at the bottom of the class.
"""

from __future__ import annotations

import copy
import time

from qtpy import QtCore

from imswitch.imcontrol.model.scan_cloak import PlanNotRepresentable
from imswitch.imcontrol.view.widgets.ScanCloakPanel import ADVANCED, SIMPLE


class ScanCloakController:
    """Mixin: a cloak over the backend scan controller that follows it."""

    #: The cloak's model, a ``ScanCloak`` subclass.
    cloakClass = None
    #: How long edits settle before they are written into the backend widget.
    mirrorDelayMs = 150

    def __init__(self, *args, widget, **kwargs):
        self._panel = widget
        self._view = widget.view
        self._cloak = self.cloakClass()
        self.__dict__['_cloakPage'] = SIMPLE
        super().__init__(*args, widget=widget.backend, **kwargs)

        self._mirrorTimer = QtCore.QTimer()
        self._mirrorTimer.setSingleShot(True)
        self._mirrorTimer.setInterval(self.mirrorDelayMs)
        self._mirrorTimer.timeout.connect(self._mirrorToBackend)
        # Deferred, so a write of several fields is read once it is whole.
        self._adoptTimer = QtCore.QTimer()
        self._adoptTimer.setSingleShot(True)
        self._adoptTimer.setInterval(0)
        self._adoptTimer.timeout.connect(self._adoptBackendEdits)

        self._panel.sigPageRequested.connect(self.setScanPage)
        self._view.sigStartClicked.connect(self.runScan)
        self._view.sigStopClicked.connect(self._onStopClicked)
        self._view.sigLiveToggled.connect(self._onLiveToggled)
        for name in ('sigStageParChanged', 'sigSignalParChanged', 'sigSeqTimeParChanged'):
            getattr(self._widget, name).connect(self._onBackendWidgetEdited)
        self._commChannel.sigScanRequestRejected.connect(self._onScanRejected)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def scanPage(self) -> str:
        """``'simple'`` or ``'advanced'``: which page drives the scan."""
        return self.__dict__.get('_cloakPage', SIMPLE)

    def _onSimplePage(self) -> bool:
        return self.scanPage() == SIMPLE

    def _isBusy(self) -> bool:
        return bool(getattr(self, 'isRunning', False)
                    or self.__dict__.get('_scanRunToken') is not None
                    or self.__dict__.get('_repeatPending', False))

    def setScanPage(self, page: str, discardAdvanced: bool = False) -> bool:
        """Show the simple page or the backend's panel; whether it switched.

        Back from Advanced, the backend widget's scan becomes the cloak's
        plan. A scan the cloak cannot show is refused with the reason, unless
        the user (or ``discardAdvanced``) drops it for the last simple plan.
        """
        if page not in (SIMPLE, ADVANCED):
            raise ValueError(f'Unknown scan page {page!r}')
        current = self.scanPage()
        if page == current:
            self._panel.showPage(page)
            return True
        if self._isBusy():
            self._panel.showPage(current)
            self._panel.showNote('Stop the scan before switching panels.')
            return False
        self._panel.showNote('')
        if page == ADVANCED:
            self._mirrorToBackend(force=True)
            self._enterPage(ADVANCED)
            self._panel.showPage(ADVANCED)
            return True

        analog, digital = self._readBackendWidget()
        try:
            plan = self._cloak.from_backend(analog, digital, self.cloakLimits())
        except PlanNotRepresentable as reason:
            if not (discardAdvanced or self._panel.confirmDiscardAdvanced(str(reason))):
                self._panel.showPage(ADVANCED)
                return False
            self._enterPage(SIMPLE)
            self._mirrorToBackend(force=True)
            self._panel.showPage(SIMPLE)
            self._view.showMessage('Advanced changes discarded.')
            return True
        self._enterPage(SIMPLE)
        if not self._sameAsMirrored(plan):
            self.adoptPlan(plan)
        self._mirrorToBackend(force=True)
        self._panel.showPage(SIMPLE)
        return True

    def _enterPage(self, page: str):
        self.__dict__['_cloakPage'] = page
        # The backend reuses its last scan design while the dicts stay the
        # same (Advanced's _designCache), but the two pages design the same
        # dicts differently: the simple page adds its frame geometry and
        # refuses channel power it cannot build. A design made for one page
        # must not run from the other.
        self._designCache = None

    # ------------------------------------------------------------------
    # The backend's parameter seam
    # ------------------------------------------------------------------

    def getParameters(self):
        """On the simple page: the cloak's scan -- the one the next run
        executes, or the acquisition while it is mirrored or saved."""
        if not self._onSimplePage():
            return super().getParameters()
        if getattr(self, 'settingParameters', False):
            return
        plan = (self.acquisitionPlan() if self.__dict__.get('_servingAcquisition')
                else self.runPlan())
        analog, digital = self._cloak.to_backend(plan, self.cloakLimits())
        self._analogParameterDict = analog
        self._digitalParameterDict = digital
        self._positionersScan = self._cloak.scanned_positioners(analog)

    def setParameters(self):
        """Dicts from a load, a saved state or a shared attribute. On the
        simple page they go to the backend widget first, then into the plan."""
        self.__dict__['_stateApplied'] = True
        if not self._onSimplePage():
            return super().setParameters()
        self.__dict__['_servingAcquisition'] = True
        try:
            super().setParameters()
        finally:
            self.__dict__['_servingAcquisition'] = False
        self._adoptBackendWidget()

    def attrChanged(self, key, value):
        # A shared-attribute write edits the acquisition, not the overview.
        if self._onSimplePage() and not self.settingAttr and len(key) == 2:
            analog, digital = self._cloak.to_backend(self.acquisitionPlan(), self.cloakLimits())
            self._analogParameterDict = analog
            self._digitalParameterDict = digital
        super().attrChanged(key, value)

    def _readBackendWidget(self):
        """The scan the backend widget holds, through the backend's builders."""
        saved = (self._analogParameterDict, self._digitalParameterDict, self._positionersScan)
        try:
            super().getParameters()
            return (copy.deepcopy(self._analogParameterDict),
                    copy.deepcopy(self._digitalParameterDict))
        finally:
            self._analogParameterDict, self._digitalParameterDict, self._positionersScan = saved

    # ------------------------------------------------------------------
    # The mirror
    # ------------------------------------------------------------------

    def _scheduleMirror(self):
        timer = self.__dict__.get('_mirrorTimer')
        if timer is not None:
            timer.start()

    def _mirrorToBackend(self, force: bool = False):
        """Write the acquisition into the backend widget (simple page only:
        on the Advanced page the widget is the scan)."""
        timer = self.__dict__.get('_mirrorTimer')
        if timer is not None:
            timer.stop()
        if not self._onSimplePage():
            return
        plan = self.acquisitionPlan()
        analog, digital = self._cloak.to_backend(plan, self.cloakLimits())
        if not force and (analog, digital) == self.__dict__.get('_lastMirroredDicts'):
            return
        saved = (self._analogParameterDict, self._digitalParameterDict, self._positionersScan)
        self.__dict__['_mirroring'] = True
        self.__dict__['_servingAcquisition'] = True
        try:
            self._analogParameterDict = copy.deepcopy(analog)
            self._digitalParameterDict = copy.deepcopy(digital)
            self._positionersScan = self._cloak.scanned_positioners(analog)
            super().setParameters()
        finally:
            self.__dict__['_mirroring'] = False
            self.__dict__['_servingAcquisition'] = False
            self._analogParameterDict, self._digitalParameterDict, self._positionersScan = saved
        self.__dict__['_lastMirroredDicts'] = (copy.deepcopy(analog), copy.deepcopy(digital))
        self.__dict__['_lastMirroredPlan'] = plan

    def lastMirroredPlan(self):
        """The plan the backend widget was last written from (or None)."""
        return self.__dict__.get('_lastMirroredPlan')

    def _sameAsMirrored(self, plan) -> bool:
        mirrored = self.lastMirroredPlan()
        if mirrored is None:
            return False
        limits = self.cloakLimits()
        return plan == self._cloak.from_backend(*self._cloak.to_backend(mirrored, limits), limits)

    def _onBackendWidgetEdited(self, *_):
        if (self._onSimplePage() and not self.__dict__.get('_mirroring')
                and not getattr(self, 'settingParameters', False)):
            self._adoptTimer.start()

    def _adoptBackendEdits(self):
        if self._onSimplePage():
            self._adoptBackendWidget()

    def _adoptBackendWidget(self) -> bool:
        """Take what the backend widget holds into the plan; open the
        Advanced page, with the reason, when the cloak cannot show it."""
        analog, digital = self._readBackendWidget()
        try:
            plan = self._cloak.from_backend(analog, digital, self.cloakLimits())
        except PlanNotRepresentable as refusal:
            note = f'{refusal} It is shown on the Advanced page.'
            self._enterPage(ADVANCED)
            self.__dict__['_cloakNote'] = note
            self._panel.showPage(ADVANCED)
            self._panel.showNote(note)
            return False
        if not self._sameAsMirrored(plan):
            self.adoptPlan(plan)
        self._mirrorToBackend(force=True)
        return True

    # ------------------------------------------------------------------
    # Running, by driving the backend's own controls
    # ------------------------------------------------------------------

    def runScan(self) -> None:
        """Start: from the simple page, Live repeats until Stop (the
        backend's Repeat box); otherwise one scan."""
        if not self._onSimplePage():
            return super().runScan()
        live = self.cloakWantsLive()
        if live:
            self._view.setLive(True)
        self.__dict__['_cloakLiveRun'] = live
        self._widget.setRepeatEnabled(live)
        self._resetFrameClock()
        super().runScan()

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        """A recording, script or workflow start: one scan (the backend turns
        Repeat off); the external driver owns any series."""
        if self._onSimplePage():
            self.__dict__['_cloakLiveRun'] = False
            self._resetFrameClock()
        return super().runScanExternal(recalculateSignals, isNonFinalPartOfSequence)

    def _onLiveToggled(self, live):
        # Unticking Live ends a live run after its frame; ticking it keeps a
        # live run going. It never turns a single scan into a series.
        if self._onSimplePage() and self.__dict__.get('_cloakLiveRun') and self._isBusy():
            self._widget.setRepeatEnabled(bool(live))

    def _onStopClicked(self):
        """Stop: no further frame. A running frame completes (NI-DAQ cannot
        be interrupted mid-iteration; decided 2026-09-25)."""
        self._widget.setRepeatEnabled(False)
        if self._isBusy():
            remaining = self._remainingFrameS()
            self._view.showMessage(
                'Stopping after this frame'
                + (f' (about {remaining:.0f} s)' if remaining and remaining >= 1 else '')
                + '.'
            )
            self.abortScan()

    def scanDone(self):
        if self._onSimplePage():
            self._recordFramePeriod()
            if not self._widget.repeatEnabled():
                self.__dict__['_cloakLiveRun'] = False
        return super().scanDone()

    def _onScanRejected(self, reason):
        if self._onSimplePage():
            self._view.showMessage(f'Not started: {reason}', error=True)

    def toggleBlockWidget(self, block):
        """Blocks or unblocks the whole panel, both pages."""
        self._panel.setEnabled(block)

    # ------------------------------------------------------------------
    # The measured frame period
    # ------------------------------------------------------------------

    def _resetFrameClock(self):
        self.__dict__['_frameClock'] = {
            'lastFrameEnd': None, 'periods': [], 'frameStart': time.monotonic(),
        }

    def _remainingFrameS(self):
        clock = self.__dict__.get('_frameClock') or {}
        estimate = self.cloakFrameEstimateS()
        started = clock.get('frameStart')
        if not estimate or started is None:
            return None
        return max(0.0, estimate - (time.monotonic() - started))

    def _recordFramePeriod(self):
        clock = self.__dict__.setdefault(
            '_frameClock', {'lastFrameEnd': None, 'periods': [], 'frameStart': None})
        now = time.monotonic()
        last = clock['lastFrameEnd']
        clock['lastFrameEnd'] = now
        clock['frameStart'] = now
        if last is None:
            return
        clock['periods'] = (clock['periods'] + [now - last])[-3:]
        self.framePeriodsMeasured(clock['periods'])

    def _clearFramePeriods(self):
        clock = self.__dict__.get('_frameClock')
        if clock is not None:
            clock['periods'] = []

    # ------------------------------------------------------------------
    # Saved state
    # ------------------------------------------------------------------

    def getComponentState(self) -> dict:
        # The saved dicts are the acquisition, whichever page shows (on the
        # Advanced page they are the widget's own).
        self.__dict__['_servingAcquisition'] = True
        try:
            state = super().getComponentState()
        finally:
            self.__dict__['_servingAcquisition'] = False
        state['cloak'] = {
            'type': getattr(getattr(self._setupInfo, 'scan', None), 'scanWidgetType', None),
            'page': self.scanPage(),
            **self.cloakStateExtras(),
        }
        return state

    def applyComponentState(self, state, *, applyMode):
        cloak = state.get('cloak') if isinstance(state, dict) else None
        cloak = cloak if isinstance(cloak, dict) else None
        before = self.scanPage()
        self.__dict__['_cloakNote'] = None
        self.__dict__['_stateApplied'] = False
        if cloak and cloak.get('page') == ADVANCED and not getattr(self, 'isRunning', False):
            # Saved on the Advanced page: restore it there, as it is.
            self._enterPage(ADVANCED)
        warnings = super().applyComponentState(state, applyMode=applyMode)
        applied = bool(self.__dict__.get('_stateApplied'))
        if not applied:
            self._enterPage(before)
        note = self.__dict__.pop('_cloakNote', None)
        if note:
            warnings.append(note)
        if applied:
            extras = self.cloakExtrasFromState(state)
            if extras:
                try:
                    self.applyCloakStateExtras(extras)
                except Exception as error:
                    warnings.append(f'The simple panel settings were not restored: {error}')
        self._panel.showPage(self.scanPage())
        if self._onSimplePage():
            self._mirrorToBackend(force=True)
        return warnings

    # ------------------------------------------------------------------
    # Hooks a cloak implements
    # ------------------------------------------------------------------

    def cloakLimits(self):
        """What the setup lets the cloak do (``ScanCloak.limits_from_setup``)."""
        raise NotImplementedError

    def acquisitionPlan(self):
        """The acquisition as the backend should hold it: what is saved and
        what the Advanced page shows."""
        raise NotImplementedError

    def runPlan(self):
        """The plan the next run from the simple page executes."""
        raise NotImplementedError

    def adoptPlan(self, plan):
        """Make ``plan``, read from the backend, the cloak's acquisition."""
        raise NotImplementedError

    def cloakWantsLive(self) -> bool:
        """Whether Start runs until Stop."""
        return self._view.liveEnabled()

    def cloakFrameEstimateS(self):
        """The estimated time of one scan, if known."""
        return None

    def framePeriodsMeasured(self, periods):
        """The last few measured periods between scans of a live run."""

    def cloakStateExtras(self) -> dict:
        """The cloak's own settings for a saved state (with its plan)."""
        return {}

    def cloakExtrasFromState(self, state):
        """The cloak's settings in a saved state, or None."""
        cloak = state.get('cloak') if isinstance(state, dict) else None
        return cloak if isinstance(cloak, dict) else None

    def applyCloakStateExtras(self, extras):
        """Restore what :meth:`cloakStateExtras` saved."""


__all__ = ['ADVANCED', 'SIMPLE', 'ScanCloakController']


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
