"""Controller for the profile-driven SimpleMoNaLISA scan cloak and screening."""

from __future__ import annotations

import dataclasses

from imswitch.imcontrol.model import (
    LaserPowerMode,
    configfiletools,
    normalizeLaserPowerMode,
)
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.scan_cloak import PlanNotRepresentable
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
        self._view.sigScreeningRequested.connect(self._onScreeningRequested)
        self._view.sigLiveRoleToggled.connect(self._onLiveRoleToggled)
        self._commChannel.sigScanEnded.connect(self._onAcquisitionRunEnded)
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._refreshLiveControls()
        self._refreshEstimate()
        self._mirrorToBackend(force=True)
        self._panel.showPage(self.scanPage())

    def _monalisa(self) -> dict:
        # Advanced's constructor can ask this cloak for parameters before this
        # subclass's own __init__ body runs, so create the state lazily.
        state = self.__dict__.get('_simpleMoNaLISAState')
        if state is None:
            limits = self._cloak.limits_from_setup(self._setupInfo)
            if limits.profile_store.profiles:
                plan = default_plan(limits)
                profile = limits.profile_store.profile(plan.profile_id)
                binding = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
            else:
                plan = None
                binding = MoNaLISAProfileBinding.unbound()
            state = self.__dict__['_simpleMoNaLISAState'] = {
                'limits': limits,
                'plan': plan,
                'binding': binding,
                'liveRolesByMode': {},
            }
        return state

    def cloakLimits(self):
        return self._monalisa()['limits']

    def acquisitionPlan(self):
        return self._monalisa()['plan']

    def profileBinding(self):
        return self._monalisa()['binding']

    def cloakOwnsBackend(self) -> bool:
        return self.acquisitionPlan() is not None and self.profileBinding().is_bound

    def parseBackendPlan(self, analog, digital, *, referencePlan=None):
        # Parse against exactly the profile/mode that Simple already owns.
        # Advanced can preserve or break this binding, never select another.
        reference = referencePlan or self.acquisitionPlan()
        if reference is None:
            raise PlanNotRepresentable(
                'Create a MoNaLISA profile before using the Simple panel.'
            )
        return dicts_to_plan(
            analog, digital, self.cloakLimits(), reference_plan=reference
        )

    def handleBackendIncompatibility(self, analog, digital, reason) -> bool:
        state = self._monalisa()
        if state['plan'] is None:
            # With no profile there is intentionally no Simple interpretation
            # of the backend scan. Stay in the restricted create-profile state.
            state['binding'] = MoNaLISAProfileBinding.unbound()
            if hasattr(self, '_view'):
                self._view.setPlan(None)
                self._view.setBinding(state['binding'])
                self._refreshLiveControls()
                self._refreshEstimate()
            return True
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
            self._refreshLiveControls()
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
            self._refreshLiveControls()
            self._refreshEstimate()

    def cloakWantsLive(self) -> bool:
        # Screening Live is deliberately not the generic cloak Repeat/live
        # scan. Acquire remains one Advanced-backed scan; screening is the
        # separate camera-trigger + Live-illumination workflow below.
        return False

    def cloakFrameEstimateS(self):
        if not self.cloakOwnsBackend():
            return None
        try:
            return self.acquisitionPlan().estimate_s(self.cloakLimits())
        except Exception:
            return None

    def _setUnboundProfileState(self):
        state = self._monalisa()
        state['plan'] = None
        state['binding'] = MoNaLISAProfileBinding.unbound()
        self._view.setPlan(None)
        self._view.setBinding(state['binding'])
        self._refreshLiveControls()
        self._refreshEstimate()

    def _planForProfile(self, profile_id: str, *, preserve_if_same: bool = True):
        limits = self.cloakLimits()
        current = self.acquisitionPlan()
        if current is None:
            base = default_plan(limits)
            if base.profile_id == str(profile_id):
                return base
            return select_profile(
                base, profile_id, limits, preserve_if_same=False
            )
        return select_profile(
            current, profile_id, limits, preserve_if_same=preserve_if_same
        )

    def _setBoundPlan(self, plan):
        state = self._monalisa()
        plan = normalize_plan(plan, state['limits'])
        state['plan'] = plan
        profile = state['limits'].profile_store.profile(plan.profile_id)
        state['binding'] = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
        self._view.setPlan(plan)
        self._view.setBinding(state['binding'])
        self._view.showMessage('')
        self._refreshLiveControls()
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
        self._refreshLiveControls()
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
        plan = self._planForProfile(profile.id, preserve_if_same=False)
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
        plan = self._planForProfile(profile.id, preserve_if_same=False)
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
        active = (state['plan'] is not None
                  and state['plan'].profile_id == profile_id
                  and state['binding'].is_bound)
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
        active = (state['plan'] is not None
                  and state['plan'].profile_id == profile_id
                  and state['binding'].is_bound)
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
        active = state['plan'] is not None and state['plan'].profile_id == profile_id
        if not self._saveProfileStoreOrReport(new_store):
            return
        if active and not new_store.profiles:
            self._setUnboundProfileState()
        elif active:
            replacement_id = new_store.default_profile
            plan = self._planForProfile(replacement_id, preserve_if_same=False)
            self._setBoundPlan(plan)
        else:
            self._refreshViewAfterProfileStoreChange()

    def _onProfileRequested(self, profile_id: str):
        if self._isBusy() or not profile_id:
            return
        try:
            plan = self._planForProfile(profile_id, preserve_if_same=True)
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
        self._refreshLiveControls()
        self._scheduleMirror()
        self._refreshEstimate()

    # ------------------------------------------------------------------
    # Live screening
    # ------------------------------------------------------------------

    def _livePowerIsSeparated(self) -> bool:
        return normalizeLaserPowerMode(
            getattr(self._setupInfo, 'laserPowerMode', LaserPowerMode.COUPLED.value)
        ) is LaserPowerMode.SEPARATED

    def _liveSelectionKey(self):
        plan = self.acquisitionPlan()
        return f'{plan.profile_id}:{plan.mode_id}'

    def _candidateLiveRoles(self):
        """Return ``role -> profile laser device tuple`` for this mode."""
        if not self.cloakOwnsBackend():
            return {}
        plan = self.acquisitionPlan()
        profile = self.cloakLimits().profile_store.profile(plan.profile_id)
        mode = profile.mode(plan.mode_id)
        configuredLasers = set(getattr(self._setupInfo, 'lasers', {}) or {})
        byRole = {}
        for step in profile.steps:
            devices = tuple(
                device for device in mode.devices_for_step(step)
                if device in configuredLasers
            )
            if not devices:
                continue
            existing = list(byRole.get(step.role, ()))
            for device in devices:
                if device not in existing:
                    existing.append(device)
            byRole[step.role] = tuple(existing)
        return byRole

    def _selectedLiveRoles(self):
        state = self._monalisa()
        key = self._liveSelectionKey()
        candidates = self._candidateLiveRoles()
        selections = state['liveRolesByMode']
        if key not in selections:
            selections[key] = {
                role for role in ('on', 'readout') if role in candidates
            }
        else:
            selections[key] = {
                role for role in selections[key] if role in candidates
            }
        return set(selections[key])

    def _setSelectedLiveRoles(self, roles):
        candidates = self._candidateLiveRoles()
        self._monalisa()['liveRolesByMode'][self._liveSelectionKey()] = {
            role for role in roles if role in candidates
        }

    def _profileScreeningDetectors(self):
        """Free-running acquisition detectors in active READOUT steps."""
        if not self.cloakOwnsBackend():
            return ()
        plan = self.acquisitionPlan()
        profile = self.cloakLimits().profile_store.profile(plan.profile_id)
        mode = profile.mode(plan.mode_id)
        detectorNames = set(self._master.detectorsManager.getAllDeviceNames())
        result = []
        for step in profile.steps:
            if step.role != 'readout':
                continue
            for device in mode.devices_for_step(step):
                if device not in detectorNames or device in result:
                    continue
                detector = self._master.detectorsManager[device]
                if detector.forAcquisition and not detector.isScanDriven:
                    result.append(device)
        return tuple(result)

    def _refreshLiveControls(self):
        if not hasattr(self, '_view'):
            return
        self._releaseScreeningTriggerIfCameraChanged()
        candidates = self._candidateLiveRoles()
        labels = {
            'on': 'ON',
            'off': 'OFF',
            'readout': 'Readout',
            'custom': 'Custom',
        }
        roles = [
            (role, labels.get(role, role.title()), devices)
            for role, devices in candidates.items()
        ]
        available = self._livePowerIsSeparated() and bool(roles)
        reason = ''
        if not self._livePowerIsSeparated():
            reason = (
                'Live screening requires separated Live/Scan laser powers. '
                'The current laser power mode shares acquisition and Live power.'
            )
        elif not roles:
            reason = 'The selected profile/mode has no laser roles available for screening.'
        elif not self._profileScreeningDetectors():
            available = False
            reason = 'The selected profile/mode has no free-running camera in its Readout role.'
        self._view.setLiveRoles(
            roles,
            self._selectedLiveRoles() if roles else set(),
            available=available,
            reason=reason,
        )

    def _screeningLaserTargets(self, roles=None):
        candidates = self._candidateLiveRoles()
        selected = self._selectedLiveRoles() if roles is None else set(roles)
        profileDevices = []
        for role in selected:
            for device in candidates.get(role, ()):
                if device not in profileDevices:
                    profileDevices.append(device)
        return tuple(self._commChannel.liveScreeningWorkflow.resolve_live_lasers(profileDevices))

    def _missingLiveViewDetectors(self):
        detectors = self._profileScreeningDetectors()
        leased = set(self._master.detectorsManager.leasedDetectorNames(
            (LeasePurpose.LIVE_VIEW,)
        ))
        return tuple(name for name in detectors if name not in leased)

    def _setTriggerModeTransactional(self, detectorNames, mode):
        snapshots = {
            name: self._commChannel.liveScreeningWorkflow.get_trigger_setting(name)
            for name in detectorNames
        }
        changed = []
        try:
            for name in detectorNames:
                self._commChannel.liveScreeningWorkflow.set_trigger_mode(name, mode)
                changed.append(name)
        except Exception:
            for name in reversed(changed):
                try:
                    self._commChannel.liveScreeningWorkflow.restore_trigger_setting(name, snapshots[name])
                except Exception:
                    self._logger.error(
                        f'Failed to roll back trigger setting for "{name}".',
                        exc_info=True,
                    )
            raise
        return snapshots

    def _restoreTriggerSnapshot(self, snapshot):
        failures = []
        for name, setting in (snapshot or {}).items():
            try:
                self._commChannel.liveScreeningWorkflow.restore_trigger_setting(name, setting)
            except Exception as error:
                failures.append(f'{name}: {error}')
                self._logger.error(
                    f'Failed to restore trigger setting for "{name}".',
                    exc_info=True,
                )
        return failures

    def _ensureScreeningTriggerOverride(self, detectorNames):
        """Keep the current Simple Live camera set in internal-trigger mode.

        Stop deliberately leaves this override in place.  We therefore keep
        the *original* pre-Live trigger values until Simple relinquishes the
        camera (page switch, profile/mode camera change, or controller close),
        rather than overwriting them on every Live/Stop cycle.
        """
        detectorNames = tuple(detectorNames)
        snapshot = self.__dict__.get('_screeningTriggerSnapshot')
        if snapshot is not None and set(snapshot) != set(detectorNames):
            if not self._releaseScreeningTriggerOverride():
                raise RuntimeError('Could not restore the previous screening camera trigger.')
            snapshot = None

        immediate = self._setTriggerModeTransactional(detectorNames, 'internal')
        if snapshot is None:
            self.__dict__['_screeningTriggerSnapshot'] = immediate

    def _releaseScreeningTriggerOverride(self):
        snapshot = self.__dict__.pop('_screeningTriggerSnapshot', None)
        if snapshot is None:
            return True
        failures = self._restoreTriggerSnapshot(snapshot)
        if failures:
            # Keep the snapshot so a later page switch/close can retry.
            self.__dict__['_screeningTriggerSnapshot'] = snapshot
            self._view.showMessage(
                'Could not restore screening camera trigger: ' + '; '.join(failures),
                error=True,
            )
            return False
        return True

    def _releaseScreeningTriggerIfCameraChanged(self):
        snapshot = self.__dict__.get('_screeningTriggerSnapshot')
        if snapshot is None:
            return
        if set(snapshot) == set(self._profileScreeningDetectors()):
            return
        self._releaseScreeningTriggerOverride()

    def _applyScreeningLaserSet(self, targetLasers):
        """Apply one screening set transactionally, touching ON/OFF only."""
        target = set(targetLasers)
        current = set(self.__dict__.get('_activeScreeningLasers', set()))
        affected = current | target
        previous = {}
        for name in affected:
            previous[name] = self._commChannel.liveScreeningWorkflow.get_laser_active(name)
        try:
            # Darken removed paths before enabling new ones.
            for name in sorted(current - target):
                self._commChannel.liveScreeningWorkflow.set_laser_active(name, False)
            for name in sorted(target - current):
                self._commChannel.liveScreeningWorkflow.set_laser_active(name, True)
                if not self._commChannel.liveScreeningWorkflow.get_laser_active(name):
                    raise RuntimeError(f'Laser "{name}" rejected the Live enable request.')
        except Exception:
            for name, wasActive in previous.items():
                try:
                    self._commChannel.liveScreeningWorkflow.set_laser_active(name, wasActive)
                except Exception:
                    self._logger.error(
                        f'Failed to roll back Live state for laser "{name}".',
                        exc_info=True,
                    )
            raise
        self.__dict__['_activeScreeningLasers'] = target

    def _onScreeningRequested(self):
        if self._isBusy() or not self._onSimplePage() or not self.cloakOwnsBackend():
            return
        if not self._livePowerIsSeparated():
            self._refreshLiveControls()
            return
        selected = self._selectedLiveRoles()
        if not selected:
            self._view.showMessage('Select at least one Live screening illumination role.', error=True)
            return
        detectors = self._profileScreeningDetectors()
        if not detectors:
            self._view.showMessage(
                'The selected profile/mode has no camera in its Readout role.', error=True
            )
            return
        missing = self._missingLiveViewDetectors()
        if missing:
            self._view.showMessage(
                'Start camera live view first: ' + ', '.join(missing) + '.', error=True
            )
            return
        hadTriggerOverride = '_screeningTriggerSnapshot' in self.__dict__
        try:
            self._ensureScreeningTriggerOverride(detectors)
            targets = self._screeningLaserTargets(selected)
            if not targets:
                raise RuntimeError('The selected screening roles do not resolve to a laser.')
            self._applyScreeningLaserSet(targets)
        except Exception as error:
            # A failed *first* Live must not leave the camera in internal mode.
            # An existing Stop/Live session keeps its deliberate internal mode.
            if not hadTriggerOverride:
                self._releaseScreeningTriggerOverride()
            self._view.showMessage(f'Could not start Live screening: {error}', error=True)
            return
        self.__dict__['_screeningActive'] = True
        self._view.setScreeningActive(True)
        self._view.showMessage('')

    def _stopScreening(self):
        active = set(self.__dict__.get('_activeScreeningLasers', set()))
        if not active:
            self.__dict__['_screeningActive'] = False
            self._view.setScreeningActive(False)
            return True
        failures = []
        stillActive = set()
        for name in sorted(active):
            try:
                self._commChannel.liveScreeningWorkflow.set_laser_active(name, False)
                if self._commChannel.liveScreeningWorkflow.get_laser_active(name):
                    raise RuntimeError('laser still reports ON')
            except Exception as error:
                failures.append(f'{name}: {error}')
                stillActive.add(name)
        self.__dict__['_activeScreeningLasers'] = stillActive
        self.__dict__['_screeningActive'] = bool(stillActive)
        self._view.setScreeningActive(bool(stillActive))
        if failures:
            self._view.showMessage(
                'Could not switch off all screening lasers: ' + '; '.join(failures),
                error=True,
            )
            return False
        self._view.showMessage('')
        return True

    def _onLiveRoleToggled(self, role: str, checked: bool):
        before = self._selectedLiveRoles()
        after = set(before)
        if checked:
            after.add(role)
        else:
            after.discard(role)
        self._setSelectedLiveRoles(after)
        if not self.__dict__.get('_screeningActive', False):
            return
        try:
            targets = self._screeningLaserTargets(after)
            self._applyScreeningLaserSet(targets)
            if not targets:
                self.__dict__['_screeningActive'] = False
                self._view.setScreeningActive(False)
        except Exception as error:
            self._setSelectedLiveRoles(before)
            self._refreshLiveControls()
            self._view.showMessage(
                f'Could not change Live screening illumination: {error}', error=True
            )

    def _prepareAcquisitionTrigger(self, *, requireLiveView=True):
        detectors = self._profileScreeningDetectors()
        if not detectors:
            return {}
        if requireLiveView:
            missing = self._missingLiveViewDetectors()
            if missing:
                raise RuntimeError('Start camera live view first: ' + ', '.join(missing) + '.')
        return self._setTriggerModeTransactional(detectors, 'external_frame')

    def _beginAcquisitionTriggerOverride(self, *, requireLiveView=True):
        detectors = self._profileScreeningDetectors()
        existing = self.__dict__.get('_acquisitionTriggerSnapshot')
        if existing is not None and set(existing) != set(detectors):
            # A continuation is expected to keep the same camera membership.
            # If a workflow changed profile/mode between parts, restore the old
            # camera before taking ownership of the new one.
            self._restoreAcquisitionTrigger()
            existing = None

        if existing is not None:
            # External multi-part acquisitions deliberately keep one original
            # snapshot until their final sigScanEnded. Reassert the required
            # trigger without replacing that original value.
            if requireLiveView:
                missing = self._missingLiveViewDetectors()
                if missing:
                    raise RuntimeError('Start camera live view first: ' + ', '.join(missing) + '.')
            self._setTriggerModeTransactional(detectors, 'external_frame')
            return
        self.__dict__['_acquisitionTriggerSnapshot'] = self._prepareAcquisitionTrigger(
            requireLiveView=requireLiveView
        )

    def _restoreAcquisitionTrigger(self):
        if '_acquisitionTriggerSnapshot' not in self.__dict__:
            return
        snapshot = self.__dict__.pop('_acquisitionTriggerSnapshot')
        failures = self._restoreTriggerSnapshot(snapshot)
        if failures:
            self._view.showMessage(
                'Acquisition ended, but camera trigger restore failed: '
                + '; '.join(failures),
                error=True,
            )

    def _onAcquisitionRunEnded(self):
        self._restoreAcquisitionTrigger()
        if self.__dict__.get('_closingSimpleMoNaLISA', False):
            self._releaseScreeningTriggerOverride()

    def _runSimpleAcquisition(
        self, start, *, requireLiveView, keepPreparedIfIdle=False
    ):
        if self.__dict__.get('_screeningActive', False) and not self._stopScreening():
            return None
        try:
            self._beginAcquisitionTriggerOverride(requireLiveView=requireLiveView)
        except Exception as error:
            self._view.showMessage(f'Could not prepare camera for acquisition: {error}', error=True)
            return None
        try:
            result = start()
        except Exception:
            self._restoreAcquisitionTrigger()
            raise
        # Some UI preflights (for example a user-cancelled reference warning)
        # return without creating a scan run and without emitting rejection.
        # Do not leave the camera trigger overridden in that case.
        if (not keepPreparedIfIdle and not self._isBusy()
                and '_acquisitionTriggerSnapshot' in self.__dict__):
            self._restoreAcquisitionTrigger()
        return result

    def runScan(self) -> None:
        if not self._onSimplePage():
            return super().runScan()
        if not self.cloakOwnsBackend():
            reason = 'Create and select a MoNaLISA profile before acquiring.'
            self._recordScanStartRejection(reason)
            return None
        return self._runSimpleAcquisition(super().runScan, requireLiveView=True)

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        if not self._onSimplePage():
            return super().runScanExternal(recalculateSignals, isNonFinalPartOfSequence)
        if not self.cloakOwnsBackend():
            reason = 'Create and select a MoNaLISA profile before acquiring.'
            self._recordScanStartRejection(reason)
            return None
        return self._runSimpleAcquisition(
            lambda: super(ScanControllerSimpleMoNaLISA, self).runScanExternal(
                recalculateSignals, isNonFinalPartOfSequence
            ),
            requireLiveView=False,
            keepPreparedIfIdle=bool(isNonFinalPartOfSequence),
        )

    def _onStopClicked(self):
        # In this Simple view Stop is deliberately only the screening-light
        # OFF action. It is disabled while an acquisition owns the hardware.
        if self._onSimplePage():
            self._stopScreening()
            return
        return super()._onStopClicked()

    def _onScanRejected(self, reason):
        super()._onScanRejected(reason)
        self._restoreAcquisitionTrigger()

    def closeEvent(self):
        # Screening is not a scan lifecycle, so the generic scan close path
        # would not know that these lasers/camera trigger belong to us. Abort
        # any acquisition before relinquishing its trigger override; restoring
        # the camera to internal/original mode while a frame-triggered scan is
        # still draining would be unsafe.
        self.__dict__['_closingSimpleMoNaLISA'] = True
        if self.__dict__.get('_screeningActive', False):
            self._stopScreening()
        result = super().closeEvent()
        if '_acquisitionTriggerSnapshot' not in self.__dict__:
            self._releaseScreeningTriggerOverride()
        return result

    def setScanPage(self, page: str, discardAdvanced: bool = False) -> bool:
        # With no profile there is intentionally nothing to parse/adopt from
        # Advanced. Returning to Simple just exposes the restricted creation
        # state; creating a profile is the explicit act that establishes a
        # Simple acquisition and mirrors it to Advanced.
        if page == 'simple' and not self.cloakLimits().profile_store.profiles:
            if self._isBusy():
                self._panel.showPage(self.scanPage())
                self._panel.showNote('Stop the scan before switching panels.')
                return False
            self._enterPage('simple')
            self._panel.showPage('simple')
            self._panel.showNote('')
            self._refreshLiveControls()
            self._refreshEstimate()
            return True

        # The Stop control disappears on Advanced, so do not carry Simple-owned
        # illumination or its persistent internal-trigger override across the
        # page boundary. Stop itself still remains laser-only.
        leavingSimple = self._onSimplePage() and page != self.scanPage()
        if leavingSimple and self.__dict__.get('_screeningActive', False):
            if not self._stopScreening():
                return False
        if leavingSimple and not self._releaseScreeningTriggerOverride():
            return False
        switched = super().setScanPage(page, discardAdvanced=discardAdvanced)
        if switched and self._onSimplePage():
            self._refreshLiveControls()
        return switched

    def cloakStateExtras(self) -> dict:
        extras = super().cloakStateExtras()
        extras['liveRolesByMode'] = {
            key: sorted(value)
            for key, value in self._monalisa()['liveRolesByMode'].items()
        }
        return extras

    def applyCloakStateExtras(self, extras):
        saved = extras.get('liveRolesByMode', {}) if isinstance(extras, dict) else {}
        if isinstance(saved, dict):
            self._monalisa()['liveRolesByMode'] = {
                str(key): {str(role) for role in value}
                for key, value in saved.items()
                if isinstance(value, (list, tuple, set))
            }
        self._refreshLiveControls()

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
            if not self.cloakLimits().profile_store.profiles:
                refusal = 'Create a MoNaLISA profile to enable Simple acquisition.'
            else:
                refusal = 'Select/reapply a profile to return to a Simple acquisition.'
            self._view.setEstimate(0.0, refusal=refusal)
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
