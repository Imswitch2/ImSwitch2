"""Controller for the acquisition-only SimpleMoNaLISA scan cloak."""

from __future__ import annotations

from imswitch.imcontrol.model.simple_monalisa_scan import (
    MoNaLISAScanCloak,
    default_plan,
    normalize_plan,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    DEFAULT_MOVE_TIME_S,
    DEFAULT_SETTLE_TIME_S,
)
from ._scan_cloak import ScanCloakController
from .ScanControllerAdvanced import ScanControllerAdvanced


class ScanControllerSimpleMoNaLISA(ScanCloakController, ScanControllerAdvanced):
    """Simple MoNaLISA acquisition mapped onto the Advanced scan backend."""

    cloakClass = MoNaLISAScanCloak

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state = self._monalisa()
        self._view.configure(state['limits'])
        self._view.sigPlanEdited.connect(self._onPlanEdited)
        self._view.setPlan(state['plan'])
        self._refreshEstimate()
        self._mirrorToBackend(force=True)
        self._panel.showPage(self.scanPage())

    def _monalisa(self) -> dict:
        # Advanced's constructor can ask this cloak for parameters before this
        # subclass's own __init__ body runs, so create the state lazily.
        state = self.__dict__.get('_simpleMoNaLISAState')
        if state is None:
            limits = self._cloak.limits_from_setup(self._setupInfo)
            state = self.__dict__['_simpleMoNaLISAState'] = {
                'limits': limits,
                'plan': default_plan(limits),
            }
        return state

    def cloakLimits(self):
        return self._monalisa()['limits']

    def acquisitionPlan(self):
        return self._monalisa()['plan']

    def adoptPlan(self, plan):
        plan = normalize_plan(plan, self.cloakLimits())
        self._monalisa()['plan'] = plan
        if hasattr(self, '_view'):
            self._view.setPlan(plan)
            self._refreshEstimate()

    def cloakWantsLive(self) -> bool:
        # This first MoNaLISA cloak has no overview/live mode: Acquire is one
        # scan. A later widefield overview can be designed independently.
        return False

    def cloakFrameEstimateS(self):
        try:
            return self.acquisitionPlan().estimate_s(self.cloakLimits())
        except Exception:
            return None

    def _onPlanEdited(self, plan):
        plan = normalize_plan(plan, self.cloakLimits())
        self._monalisa()['plan'] = plan
        # Show snapped values (sample-rate timing / nm grid), then mirror the
        # acquisition to Advanced after the normal cloak debounce.
        self._view.setPlan(plan)
        self._scheduleMirror()
        self._refreshEstimate()

    def _setupRefusal(self) -> str:
        limits = self.cloakLimits()
        missing = limits.missing_setup_items()
        if missing:
            return 'Setup is missing: ' + ', '.join(missing) + '.'
        if self._setupInfo.scan.scanDesigner != 'BetaScanDesigner':
            return 'SimpleMoNaLISA currently expects scanDesigner "BetaScanDesigner".'
        if self._setupInfo.scan.TTLCycleDesigner != 'AdvancedScanTTLCycleDesigner':
            return 'SimpleMoNaLISA currently expects TTLCycleDesigner "AdvancedScanTTLCycleDesigner".'
        return ''

    def _refreshEstimate(self):
        if not hasattr(self, '_view'):
            return
        refusal = self._setupRefusal()
        plan = self.acquisitionPlan()
        limits = self.cloakLimits()
        if not refusal:
            params = getattr(self._setupInfo.scan, 'scanDesignerParams', None) or {}
            move = float(params.get('move_time', DEFAULT_MOVE_TIME_S))
            settle = float(params.get('settle_time', DEFAULT_SETTLE_TIME_S))
            dwell = plan.dwell_s(limits)
            if dwell <= move + settle:
                refusal = (
                    f'Pixel dwell is {dwell * 1e3:g} ms, but this stage needs more than '
                    f'{(move + settle) * 1e3:g} ms for move + settle.'
                )
        note = ''
        if not refusal:
            pixels = plan.pixels(limits)
            note = (
                f'{" × ".join(str(value) for value in pixels)} positions · '
                f'{plan.dwell_s(limits) * 1e3:g} ms/pixel · stage returns included.'
            )
        self._view.setEstimate(plan.estimate_s(limits), note=note, refusal=refusal)


__all__ = ['ScanControllerSimpleMoNaLISA']
