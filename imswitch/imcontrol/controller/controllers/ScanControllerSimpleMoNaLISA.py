"""Controller for the acquisition-only SimpleMoNaLISA scan cloak."""

from __future__ import annotations

import dataclasses

from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.monalisa_profiles import (
    MoNaLISAProfileBinding,
    MoNaLISAProfileError,
    config_with_profile_store,
    profile_id_from_label,
    starter_monalisa_profile,
)
from imswitch.imcontrol.model.simple_monalisa_scan import (
    MoNaLISAScanCloak,
    default_plan,
    adapt_plan_to_profile_definition,
    dicts_to_plan,
    normalize_plan,
    plan_with_backend_geometry,
    select_mode,
    select_profile,
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
        self._view.sigProfileRequested.connect(self._onProfileRequested)
        self._view.sigModeRequested.connect(self._onModeRequested)
        self._view.sigProfileNewRequested.connect(self._onProfileNewRequested)
        self._view.sigProfileDuplicateRequested.connect(self._onProfileDuplicateRequested)
        self._view.sigProfileEditRequested.connect(self._onProfileEditRequested)
        self._view.sigProfileRenameRequested.connect(self._onProfileRenameRequested)
        self._view.sigProfileDeleteRequested.connect(self._onProfileDeleteRequested)
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._refreshEstimate()
        self._mirrorToBackend(force=True)
        self._panel.showPage(self.scanPage())

    def _monalisa(self) -> dict:
        # Advanced's constructor can ask this cloak for parameters before this
        # subclass's own __init__ body runs, so create the state lazily.
        state = self.__dict__.get('_simpleMoNaLISAState')
        if state is None:
            limits = self._cloak.limits_from_setup(self._setupInfo)
            plan = default_plan(limits)
            profile = limits.profile_store.profile(plan.profile_id)
            state = self.__dict__['_simpleMoNaLISAState'] = {
                'limits': limits,
                'plan': plan,
                'binding': MoNaLISAProfileBinding.bound(profile, plan.mode_id),
            }
        return state

    def cloakLimits(self):
        return self._monalisa()['limits']

    def acquisitionPlan(self):
        return self._monalisa()['plan']

    def profileBinding(self):
        return self._monalisa()['binding']

    def cloakOwnsBackend(self) -> bool:
        return self.profileBinding().is_bound

    def parseBackendPlan(self, analog, digital, *, referencePlan=None):
        # Parse against exactly the profile/mode that Simple already owns.
        # Advanced can preserve or break this binding, never select another.
        return dicts_to_plan(
            analog,
            digital,
            self.cloakLimits(),
            reference_plan=referencePlan or self.acquisitionPlan(),
        )

    def handleBackendIncompatibility(self, analog, digital, reason) -> bool:
        state = self._monalisa()
        # Preserve any still-representable geometry edits from Advanced so that
        # explicitly reapplying a profile repairs the sequence only.
        try:
            state['plan'] = plan_with_backend_geometry(
                state['plan'], analog, state['limits']
            )
        except Exception:
            pass
        state['binding'] = state['binding'].incompatible(str(reason))
        if hasattr(self, '_view'):
            self._view.setPlan(state['plan'])
            self._view.setBinding(state['binding'])
            self._view.showMessage(
                'Advanced settings no longer match the selected Simple profile.',
                error=False,
            )
            self._refreshEstimate()
        return True

    def adoptPlan(self, plan):
        plan = normalize_plan(plan, self.cloakLimits())
        state = self._monalisa()
        state['plan'] = plan
        profile = state['limits'].profile_store.profile(plan.profile_id)
        state['binding'] = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
        if hasattr(self, '_view'):
            self._view.setPlan(plan)
            self._view.setBinding(state['binding'])
            self._refreshEstimate()

    def cloakWantsLive(self) -> bool:
        # This MoNaLISA cloak still has no overview/live mode: Acquire is one
        # scan. A later widefield overview can be designed independently.
        return False

    def cloakFrameEstimateS(self):
        if not self.cloakOwnsBackend():
            return None
        try:
            return self.acquisitionPlan().estimate_s(self.cloakLimits())
        except Exception:
            return None

    def _setBoundPlan(self, plan):
        state = self._monalisa()
        plan = normalize_plan(plan, state['limits'])
        state['plan'] = plan
        profile = state['limits'].profile_store.profile(plan.profile_id)
        state['binding'] = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
        self._view.setPlan(plan)
        self._view.setBinding(state['binding'])
        self._view.showMessage('')
        self._mirrorToBackend(force=True)
        self._refreshEstimate()

    def _persistProfileStore(self, store):
        """Persist one atomic profile-store replacement into the active setup."""
        state = self._monalisa()
        raw = config_with_profile_store(
            getattr(self._setupInfo.scan, 'simpleMoNaLISA', None), store
        )
        previous_scan = self._setupInfo.scan
        new_scan = dataclasses.replace(previous_scan, simpleMoNaLISA=raw)
        self._setupInfo.scan = new_scan
        try:
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        except Exception:
            self._setupInfo.scan = previous_scan
            raise
        state['limits'] = dataclasses.replace(state['limits'], profile_store=store)
        self._view.configure(state['limits'])

    def _refreshViewAfterProfileStoreChange(self):
        state = self._monalisa()
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._refreshEstimate()

    def _saveProfileStoreOrReport(self, store):
        try:
            self._persistProfileStore(store)
        except Exception as error:
            self._view.showMessage(f'Could not save MoNaLISA profiles: {error}', error=True)
            return False
        return True

    def _onProfileNewRequested(self):
        if self._isBusy():
            return
        name = self._view.askProfileName(
            'New MoNaLISA profile', 'Profile name:', 'New profile'
        )
        if not name:
            return
        state = self._monalisa()
        store = state['limits'].profile_store
        profile_id = profile_id_from_label(name, store.profiles)
        try:
            seed = starter_monalisa_profile(
                profile_id, name, state['limits'].ttl_devices
            )
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        profile = self._view.editProfileDefinition(seed, state['limits'].ttl_devices)
        if profile is None:
            return
        try:
            new_store = store.with_profile(profile)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        if not self._saveProfileStoreOrReport(new_store):
            return
        plan = select_profile(
            state['plan'], profile.id, state['limits'], preserve_if_same=False
        )
        self._setBoundPlan(plan)

    def _onProfileDuplicateRequested(self, profile_id: str):
        if self._isBusy():
            return
        state = self._monalisa()
        store = state['limits'].profile_store
        try:
            source = store.profile(profile_id)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        name = self._view.askProfileName(
            'Duplicate MoNaLISA profile', 'New profile name:', f'{source.label} copy'
        )
        if not name:
            return
        new_id = profile_id_from_label(name, store.profiles)
        duplicate = dataclasses.replace(source, id=new_id, label=name)
        profile = self._view.editProfileDefinition(
            duplicate, state['limits'].ttl_devices
        )
        if profile is None:
            return
        try:
            new_store = store.with_profile(profile)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        if not self._saveProfileStoreOrReport(new_store):
            return
        plan = select_profile(
            state['plan'], profile.id, state['limits'], preserve_if_same=False
        )
        self._setBoundPlan(plan)

    def _onProfileEditRequested(self, profile_id: str):
        if self._isBusy():
            return
        state = self._monalisa()
        store = state['limits'].profile_store
        try:
            current = store.profile(profile_id)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        edited = self._view.editProfileDefinition(
            current, state['limits'].ttl_devices
        )
        if edited is None:
            return
        try:
            new_store = store.with_profile(edited)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        active = state['plan'].profile_id == profile_id and state['binding'].is_bound
        if not self._saveProfileStoreOrReport(new_store):
            return
        if active:
            plan = adapt_plan_to_profile_definition(
                state['plan'], edited, state['limits']
            )
            self._setBoundPlan(plan)
        else:
            self._refreshViewAfterProfileStoreChange()

    def _onProfileRenameRequested(self, profile_id: str):
        if self._isBusy():
            return
        state = self._monalisa()
        store = state['limits'].profile_store
        try:
            profile = store.profile(profile_id)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        name = self._view.askProfileName(
            'Rename MoNaLISA profile', 'Profile name:', profile.label
        )
        if not name or name == profile.label:
            return
        try:
            new_store = store.relabel_profile(profile_id, name)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        active = state['plan'].profile_id == profile_id and state['binding'].is_bound
        if not self._saveProfileStoreOrReport(new_store):
            return
        if active:
            # Rename preserves the stable profile id and scan structure, but it
            # is an explicit Simple-side edit so refresh the binding fingerprint.
            self._setBoundPlan(state['plan'])
        else:
            self._refreshViewAfterProfileStoreChange()

    def _onProfileDeleteRequested(self, profile_id: str):
        if self._isBusy():
            return
        state = self._monalisa()
        store = state['limits'].profile_store
        try:
            profile = store.profile(profile_id)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        if not self._view.confirmProfileDelete(profile.label):
            return
        try:
            new_store = store.without_profile(profile_id)
        except MoNaLISAProfileError as error:
            self._view.showMessage(str(error), error=True)
            return
        active = state['plan'].profile_id == profile_id
        if not self._saveProfileStoreOrReport(new_store):
            return
        if active:
            replacement_id = new_store.default_profile
            plan = select_profile(
                state['plan'], replacement_id, state['limits'], preserve_if_same=False
            )
            self._setBoundPlan(plan)
        else:
            self._refreshViewAfterProfileStoreChange()

    def _onProfileRequested(self, profile_id: str):
        if self._isBusy() or not profile_id:
            return
        try:
            plan = select_profile(
                self.acquisitionPlan(), profile_id, self.cloakLimits(),
                preserve_if_same=True,
            )
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        self._setBoundPlan(plan)

    def _onModeRequested(self, mode_id: str):
        if self._isBusy() or not self.cloakOwnsBackend() or not mode_id:
            return
        try:
            plan = select_mode(self.acquisitionPlan(), mode_id, self.cloakLimits())
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        self._setBoundPlan(plan)

    def _onPlanEdited(self, plan):
        if not self.cloakOwnsBackend():
            return
        plan = normalize_plan(plan, self.cloakLimits())
        state = self._monalisa()
        state['plan'] = plan
        profile = state['limits'].profile_store.profile(plan.profile_id)
        state['binding'] = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
        self._view.setPlan(plan)
        self._view.setBinding(state['binding'])
        self._scheduleMirror()
        self._refreshEstimate()

    def _setupRefusal(self) -> str:
        limits = self.cloakLimits()
        plan = self.acquisitionPlan()
        missing = limits.missing_setup_items(plan.profile_id)
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
        if not self.cloakOwnsBackend():
            self._view.setEstimate(
                0.0,
                refusal='Select/reapply a profile to return to a Simple acquisition.',
            )
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
