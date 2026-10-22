"""Controller for the profile-driven SimpleMoNaLISA scan cloak and screening."""

from __future__ import annotations

import copy
import dataclasses
import math

from imswitch.imcontrol.model import (
    LaserPowerMode,
    configfiletools,
    laserPowerModeFromSetup,
)
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.scan_cloak import PlanNotRepresentable
from imswitch.imcontrol.model.monalisa_profiles import (
    MoNaLISAProfileBinding,
    MoNaLISAProfileDefinition,
    MoNaLISAProfileError,
    MoNaLISAProfileStore,
    config_with_profile_store,
    profile_fingerprint,
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
    render_recording_suffix,
    select_mode,
    select_profile,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    DEFAULT_MOVE_TIME_S,
    DEFAULT_SETTLE_TIME_S,
)
from ._scan_cloak import ScanCloakController
from .ScanControllerAdvanced import ScanControllerAdvanced


@dataclasses.dataclass(frozen=True)
class _LoadedMoNaLISACompatibility:
    plan: object = None
    issue: str = ''
    message: str = ''

    @property
    def compatible(self) -> bool:
        return self.plan is not None and not self.issue


class ScanControllerSimpleMoNaLISA(ScanCloakController, ScanControllerAdvanced):
    """Simple MoNaLISA acquisition mapped onto the Advanced scan backend."""

    cloakClass = MoNaLISAScanCloak
    savedMoNaLISASchemaVersion = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._view.sigPlanEdited.connect(self._onPlanEdited)
        self._view.sigProfileRequested.connect(self._onProfileRequested)
        self._view.sigProfileReloadRequested.connect(self._onProfileReloadRequested)
        self._view.sigProfileDefaultsUpdateRequested.connect(
            self._onProfileDefaultsUpdateRequested
        )
        self._view.sigModeRequested.connect(self._onModeRequested)
        self._view.sigProfileNewRequested.connect(self._onProfileNewRequested)
        self._view.sigProfileDuplicateRequested.connect(self._onProfileDuplicateRequested)
        self._view.sigProfileEditRequested.connect(self._onProfileEditRequested)
        self._view.sigProfileRenameRequested.connect(self._onProfileRenameRequested)
        self._view.sigProfileDeleteRequested.connect(self._onProfileDeleteRequested)
        self._view.sigSetupEditRequested.connect(self._onSetupEditRequested)
        self._view.sigRecordingSuffixToggled.connect(
            self._onRecordingSuffixToggled
        )
        self._view.sigSaveAcquisitionToggled.connect(
            self._onSaveAcquisitionToggled
        )
        self._view.sigScanStopClicked.connect(self._onScanStopClicked)
        self._view.sigScanSeriesSettingsEdited.connect(
            self._onScanSeriesSettingsEdited
        )
        self._view.sigCameraReadoutAutoRequested.connect(
            self._onCameraReadoutAutoRequested
        )
        self._view.sigScreeningRequested.connect(self._onScreeningRequested)
        self._view.sigLiveSourceToggled.connect(self._onLiveSourceToggled)
        self._view.sigLiveSourcesEditRequested.connect(
            self._onLiveSourcesEditRequested
        )
        self._panel.sigLoadResolutionActionRequested.connect(
            self._onLoadResolutionActionRequested
        )
        self._commChannel.sigScanEnded.connect(self._onAcquisitionRunEnded)
        self._commChannel.sigAdjustFrame.connect(self._onDetectorGeometryChanged)
        self._commChannel.sigDetectorParametersChanged.connect(
            self._onDetectorParametersChanged
        )
        self._recordedSeriesActive = False
        recordingWorkflow = getattr(self._commChannel, 'recordingWorkflow', None)
        settingsChanged = getattr(
            recordingWorkflow, 'sigScanSeriesSettingsChanged', None
        )
        if settingsChanged is not None:
            settingsChanged.connect(self._onScanSeriesSettingsChanged)
        activityChanged = getattr(
            recordingWorkflow, 'sigScanSeriesActivityChanged', None
        )
        if activityChanged is not None:
            activityChanged.connect(self._onScanSeriesActivityChanged)

        self._refreshResolvedSetup(refreshView=False)
        state = self._monalisa()
        self._view.configure(state['limits'])
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._syncScanSeriesSettings()
        self._refreshSetupSummary()
        self._refreshLiveControls()
        self._refreshEstimate()
        self._mirrorToBackend(force=True)
        self._panel.showPage(self.scanPage())
        self._syncRecordingAcquisitionOwnership()

    def _monalisa(self) -> dict:
        # Advanced's constructor can ask this cloak for parameters before this
        # subclass's own __init__ body runs, so create the state lazily.
        state = self.__dict__.get('_simpleMoNaLISAState')
        if state is None:
            limits = self._cloak.limits_from_setup(self._setupInfo)
            if limits.profile_store.profiles:
                try:
                    plan = default_plan(limits)
                    profile = limits.profile_store.profile(plan.profile_id)
                    binding = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
                except PlanNotRepresentable:
                    # Keep profile editing available even when setup/profile
                    # geometry is incomplete (for example per-profile uLens
                    # mode with an older profile that has no period yet).
                    plan = None
                    binding = MoNaLISAProfileBinding.unbound()
            else:
                plan = None
                binding = MoNaLISAProfileBinding.unbound()
            state = self.__dict__['_simpleMoNaLISAState'] = {
                'limits': limits,
                'plan': plan,
                'binding': binding,
                # Live screening illumination is intentionally independent of
                # the acquisition profile/mode. The setup owns which sources
                # are shown; this runtime set owns which of those are enabled.
                'selectedLiveSources': None,
                'resolvedSetup': {},
            }
        return state

    def _activeProfileDetectors(self, limits=None, plan=None):
        limits = limits or self.cloakLimits()
        plan = plan if plan is not None else self.acquisitionPlan()
        if plan is None:
            return ()
        profile = limits.profile_store.profile(plan.profile_id)
        mode = profile.mode(plan.mode_id)
        detectorNames = set(self._master.detectorsManager.getAllDeviceNames())
        result = []
        # Prefer semantic Readout steps for the normal MoNaLISA case, then
        # accept cameras from CUSTOM/other enabled steps as well. Alignment
        # profiles can legitimately put camera TTL in a shared custom step.
        ordered_steps = tuple(
            step for step in profile.steps if step.role == 'readout'
        ) + tuple(
            step for step in profile.steps if step.role != 'readout'
        )
        for step in ordered_steps:
            for device in mode.devices_for_step(step):
                if device not in detectorNames or device in result:
                    continue
                detector = self._master.detectorsManager[device]
                if detector.forAcquisition and not detector.isScanDriven:
                    result.append(device)
        return tuple(result)

    def _resolveDetectorSetup(self, limits):
        detectors = self._activeProfileDetectors(limits=limits)
        detector_name = detectors[0] if detectors else None
        detector = self._master.detectorsManager[detector_name] if detector_name else None

        fov = limits.manual_fov_um
        fov_auto = False
        if limits.fov_source == 'detector' and detector is not None:
            try:
                shape = tuple(detector.shape)
                pixel = tuple(detector.pixelSizeUm)
                if len(shape) >= 2 and len(pixel) >= 2:
                    fov = (float(shape[0]) * float(pixel[-1]),
                           float(shape[1]) * float(pixel[-2]))
                    fov_auto = True
            except Exception:
                self._logger.warning('Could not derive MoNaLISA FOV from detector.', exc_info=True)

        detector_readout = None
        if detector is not None:
            try:
                parameter = detector.parameters.get('Readout time')
                if parameter is not None:
                    value = float(parameter.value)
                    if value > 0:
                        detector_readout = value
            except Exception:
                self._logger.warning(
                    'Could not read MoNaLISA camera readout time from detector.',
                    exc_info=True,
                )

        resolved = dataclasses.replace(limits, fov_um=tuple(fov))
        info = {
            'detector': detector_name,
            'fov_auto': fov_auto,
            'fov_um': tuple(fov),
            'detector_readout_s': detector_readout,
        }
        return resolved, info

    def _refreshResolvedSetup(self, *, refreshView=True, mirror=False):
        state = self._monalisa()
        resolved, info = self._resolveDetectorSetup(state['limits'])
        state['limits'] = resolved
        changed_timing = False
        state['resolvedSetup'] = info
        if refreshView and hasattr(self, '_view'):
            self._view.configure(resolved)
            self._view.setPlan(state['plan'])
            self._view.setBinding(state['binding'])
            self._refreshSetupSummary()
            self._refreshLiveControls()
            self._refreshEstimate()
        if mirror and changed_timing and self.cloakOwnsBackend():
            self._mirrorToBackend(force=True)
        return changed_timing

    def _refreshSetupSummary(self):
        if not hasattr(self, '_view'):
            return
        state = self._monalisa()
        limits = state['limits']
        plan = state['plan']
        info = state.get('resolvedSetup', {})
        detector = info.get('detector') or 'no profile camera'
        fx, fy = limits.fov_um
        fov_tag = 'auto' if info.get('fov_auto') else 'manual/fallback'
        try:
            if limits.ulens_period_policy == 'fixed':
                period = limits.fixed_ulens_period_um
            else:
                period = limits.ulens_period_um(plan.profile_id) if plan else None
        except Exception:
            period = None
        period_text = (
            f'{period * 1000:.0f} nm' if period is not None and period < 1
            else (f'{period:.3g} µm' if period is not None else 'profile required')
        )
        period_owner = 'setup' if limits.ulens_period_policy == 'fixed' else 'profile'

        if plan is not None:
            readout_s = limits.camera_readout_time_s(plan.profile_id)
            readout_source = limits.camera_readout_source(plan.profile_id)
        else:
            readout_s = limits.default_camera_readout_time_s
            readout_source = 'setup default'
        detector_readout_s = info.get('detector_readout_s')
        status_text = ''
        severity = ''
        recommended_s = None
        if detector_readout_s is not None:
            recommended_s = float(detector_readout_s) + limits.camera_readout_safety_margin_s
            if readout_s < recommended_s - limits.camera_readout_tolerance_s:
                status_text = '● too fast'
                severity = 'error'
            elif readout_s > (
                recommended_s + limits.camera_readout_unoptimized_threshold_s
            ):
                status_text = '● slower than needed'
                severity = 'warning'

        text = (
            f'{detector} · FOV {fx:.3g} × {fy:.3g} µm ({fov_tag}) · '
            f'readout {readout_s * 1e3:.3g} ms ({readout_source}) · '
            f'uLens {period_text} ({period_owner})'
        )
        tooltip = (
            f'Inter-pulse delay: {limits.inter_pulse_delay_s * 1e3:.3g} ms\n'
            f'FOV source: {limits.fov_source}\n'
            f'Default camera readout: {limits.default_camera_readout_time_s * 1e3:.3g} ms\n'
            f'uLens period policy: {limits.ulens_period_policy}'
        )
        self._view.setSetupSummary(text, tooltip)

        diagnostic = (
            f'Effective camera readout comes from {readout_source}.'
        )
        if detector_readout_s is not None and recommended_s is not None:
            diagnostic += (
                f' Current detector: {detector_readout_s * 1e3:.3g} ms; '
                f'recommended with safety margin: {recommended_s * 1e3:.3g} ms.'
            )
        else:
            diagnostic += ' The current detector does not report a usable readout time.'
        self._view.setCameraReadoutInfo(
            readout_s * 1e3,
            readout_source,
            status_text=status_text,
            severity=severity,
            auto_available=(plan is not None and detector_readout_s is not None),
            tooltip=diagnostic,
        )

    def _onDetectorGeometryChanged(self, *args):
        if not hasattr(self, '_view'):
            return
        self._refreshResolvedSetup(refreshView=True, mirror=True)

    def _onDetectorParametersChanged(self, detectorName):
        if not hasattr(self, '_view'):
            return
        if str(detectorName) not in self._activeProfileDetectors():
            return
        self._refreshResolvedSetup(refreshView=True, mirror=True)

    def cloakLimits(self):
        return self._monalisa()['limits']

    def acquisitionPlan(self):
        return self._monalisa()['plan']

    def profileBinding(self):
        return self._monalisa()['binding']

    def cloakOwnsBackend(self) -> bool:
        return self.acquisitionPlan() is not None and self.profileBinding().is_bound

    def _syncScanSeriesSettings(self) -> None:
        workflow = getattr(self._commChannel, 'recordingWorkflow', None)
        getter = getattr(workflow, 'get_scan_series_settings', None)
        if not callable(getter):
            return
        settings = getter()
        self._view.setScanSeriesSettings(
            settings.timepoints, settings.interval_s, settings.single_file
        )
        activeFor = getattr(workflow, 'scan_series_active_for', None)
        active = bool(activeFor(self)) if callable(activeFor) else False
        self._recordedSeriesActive = active
        self._view.setAcquisitionSeriesActive(active)

    def _setRecordingAcquisitionOwnership(self, active: bool) -> bool:
        workflow = getattr(self._commChannel, 'recordingWorkflow', None)
        setter = getattr(workflow, 'set_recording_acquisition_owner', None)
        if not callable(setter):
            return not active
        try:
            return bool(setter(self, bool(active)))
        except Exception:
            self._logger.warning(
                'Could not update Recording acquisition ownership.',
                exc_info=True,
            )
            return False

    def _syncRecordingAcquisitionOwnership(self) -> None:
        wantsOwnership = bool(
            self._onSimplePage() and self._view.saveAcquisitionRequested()
        )
        if self._setRecordingAcquisitionOwnership(wantsOwnership):
            return
        if wantsOwnership:
            self._view.setSaveAcquisitionRequested(False)
            self._view.showMessage(
                'Recording acquisition is already configured by another scan widget.',
                error=True,
            )

    def _onSaveAcquisitionToggled(self, checked: bool) -> None:
        if checked and not self._onSimplePage():
            return
        self._syncRecordingAcquisitionOwnership()

    def _onScanSeriesSettingsEdited(
        self, timepoints, interval_s, single_file
    ) -> None:
        workflow = getattr(self._commChannel, 'recordingWorkflow', None)
        setter = getattr(workflow, 'set_scan_series_settings', None)
        if not callable(setter):
            return
        try:
            setter(
                timepoints=timepoints, interval_s=interval_s,
                single_file=single_file,
            )
        except Exception as error:
            self._view.showMessage(
                f'Invalid acquisition cadence: {error}', error=True
            )
            self._syncScanSeriesSettings()

    def _onScanSeriesSettingsChanged(self, settings) -> None:
        self._view.setScanSeriesSettings(
            settings.timepoints, settings.interval_s, settings.single_file
        )

    def _onScanSeriesActivityChanged(self, source, active) -> None:
        if source is not self:
            return
        self._recordedSeriesActive = bool(active)
        self._view.setAcquisitionSeriesActive(active)

    def _notifyRecordingFilenameSuffixChanged(self) -> None:
        workflow = getattr(self._commChannel, 'recordingWorkflow', None)
        notify = getattr(workflow, 'recording_filename_suffix_changed', None)
        if callable(notify):
            try:
                notify(self)
            except Exception:
                self._logger.debug(
                    'Could not refresh Recording filename suffix preview.',
                    exc_info=True,
                )

    def getRecordingFilenameSuffix(self):
        """Return the setup-configured suffix for this bound Simple scan."""
        if self._savedMoNaLISAMetadata() is None:
            return None
        state = self._monalisa()
        naming = state['limits'].recording_naming
        if not naming.enabled:
            return None
        laser_states = {}
        if naming.include_power:
            getter = getattr(
                getattr(self._commChannel, 'scanWorkflow', None),
                'laser_acquisition_states',
                None,
            )
            if callable(getter):
                try:
                    laser_states = getter()
                except Exception:
                    self._logger.warning(
                        'Could not resolve laser powers for recording filename.',
                        exc_info=True,
                    )
        try:
            suffix = render_recording_suffix(
                state['plan'], state['limits'], laser_states=laser_states
            )
        except Exception:
            self._logger.warning(
                'Could not render SimpleMoNaLISA recording filename suffix.',
                exc_info=True,
            )
            return None
        return suffix or None

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
        self._refreshSetupSummary()
        self._refreshLiveControls()
        self._refreshEstimate()

    def _planForProfile(self, profile_id: str, *, preserve_if_same: bool = True):
        limits = self.cloakLimits()
        current = self.acquisitionPlan()
        if current is None:
            return default_plan(limits, profile_id)
        return select_profile(
            current, profile_id, limits, preserve_if_same=preserve_if_same
        )

    def _setBoundPlan(self, plan):
        # Explicit Simple-side profile selection/edit supersedes any unresolved
        # loaded-scan decision. The user is deliberately creating a new Simple
        # acquisition at this point.
        self._clearPendingLoadResolution()
        state = self._monalisa()
        plan = normalize_plan(plan, state['limits'])
        state['plan'] = plan
        profile = state['limits'].profile_store.profile(plan.profile_id)
        state['binding'] = MoNaLISAProfileBinding.bound(profile, plan.mode_id)
        self._refreshResolvedSetup(refreshView=False)
        self._view.setPlan(plan)
        self._view.setBinding(state['binding'])
        self._view.showMessage('')
        self._refreshSetupSummary()
        self._refreshLiveControls()
        self._mirrorToBackend(force=True)
        self._refreshEstimate()

    def _persistProfileStore(self, store, *, refresh_view=True):
        """Persist one atomic profile-store replacement into the active setup."""
        state = self._monalisa()
        configured = self._normalizeLiveSourcesForSave(
            getattr(self._setupInfo.scan, 'simpleMoNaLISA', None)
        )
        raw = config_with_profile_store(
            configured, store
        )
        self._persistSimpleConfig(raw)
        state['limits'] = dataclasses.replace(state['limits'], profile_store=store)
        self._refreshResolvedSetup(refreshView=refresh_view)

    def _configuredLaserNames(self):
        """Configured illumination device names in the Laser widget order."""
        try:
            return tuple(name for name, _ in self._master.lasersManager)
        except Exception:
            return tuple((getattr(self._setupInfo, 'lasers', {}) or {}).keys())

    def _legacyLiveSourcesFromConfig(self, configured=None):
        """Derive the old profile-owned Live source set in stable order.

        Older SimpleMoNaLISA setups did not have a setup-level ``liveSources``
        list: screening choices were inferred from the laser devices present in
        each profile.  Accept an explicit per-profile ``liveSources`` field as
        well, so configurations produced during intermediate development remain
        readable.  Only configured laser devices are returned; camera/TTL
        entries in profile steps are ignored.
        """
        raw = dict(
            configured
            if configured is not None
            else (getattr(self._setupInfo.scan, 'simpleMoNaLISA', None) or {})
        )
        profiles = raw.get('profiles') or {}
        if not isinstance(profiles, dict):
            return ()
        configuredLasers = set(self._configuredLaserNames())
        result = []

        def append(device):
            device = str(device).strip()
            if device in configuredLasers and device not in result:
                result.append(device)

        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            legacy = profile.get('liveSources') or ()
            if isinstance(legacy, (list, tuple, set)):
                for device in legacy:
                    append(device)
            steps = profile.get('steps') or ()
            if isinstance(steps, dict):
                steps = tuple(steps.values())
            if not isinstance(steps, (list, tuple)):
                continue
            modes = profile.get('modes') or {}
            if not isinstance(modes, dict):
                modes = {}
            for step in steps:
                if not isinstance(step, dict):
                    continue
                step_id = str(step.get('id') or '').strip()
                declared = step.get('ttlDevices') or ()
                if not isinstance(declared, (list, tuple, set)):
                    declared = ()
                # This mirrors the former candidate logic: CUSTOM steps made
                # every declared laser available, while semantic steps only
                # exposed devices enabled by a profile mode. Unioning over all
                # modes gives the old profile-owned choices without tying the
                # migrated list to whichever mode happens to be active now.
                if str(step.get('role') or '').strip().lower() == 'custom':
                    for device in declared:
                        append(device)
                    continue
                found_enabled_mode = False
                for mode in modes.values():
                    if not isinstance(mode, dict):
                        continue
                    if step_id not in (mode.get('enabledSteps') or ()):
                        continue
                    found_enabled_mode = True
                    enabled_devices = mode.get('enabledDevices') or {}
                    override = (
                        enabled_devices.get(step_id)
                        if isinstance(enabled_devices, dict)
                        else None
                    )
                    devices = declared if override is None else override
                    if isinstance(devices, (list, tuple, set)):
                        for device in devices:
                            append(device)
                if not modes or not found_enabled_mode:
                    for device in declared:
                        append(device)
        return tuple(result)

    def _normalizeLiveSourcesForSave(self, raw):
        """Migrate legacy profile-owned Live choices to setup-level storage."""
        result = copy.deepcopy(dict(raw or {}))
        if 'liveSources' not in result:
            result['liveSources'] = list(self._legacyLiveSourcesFromConfig(result))
        else:
            values = result.get('liveSources') or ()
            if not isinstance(values, (list, tuple, set)):
                values = ()
            result['liveSources'] = list(dict.fromkeys(
                str(value).strip() for value in values if str(value).strip()
            ))

        # Transitional per-profile ``liveSources`` is accepted on read, but a
        # successful save finishes the one-way migration to the setup-level
        # field. Acquisition TTL definitions are left untouched.
        profiles = result.get('profiles')
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if isinstance(profile, dict):
                    profile.pop('liveSources', None)
        return result

    def _persistSimpleConfig(self, raw):
        """Persist one complete ``simpleMoNaLISA`` block atomically."""
        raw = self._normalizeLiveSourcesForSave(raw)
        previous_scan = self._setupInfo.scan
        new_scan = dataclasses.replace(previous_scan, simpleMoNaLISA=raw)
        self._setupInfo.scan = new_scan
        try:
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)
        except Exception:
            self._setupInfo.scan = previous_scan
            raise

    def _onRecordingSuffixToggled(self, enabled: bool):
        """Persist the main-panel recording suffix switch."""
        state = self._monalisa()
        current = bool(state['limits'].recording_naming.enabled)
        if self._isBusy() or self.__dict__.get('_screeningActive', False):
            self._view.setRecordingSuffixEnabled(current)
            return

        configured = dict(
            getattr(self._setupInfo.scan, 'simpleMoNaLISA', None) or {}
        )
        naming = configured.get('recordingNaming')
        naming = dict(naming) if isinstance(naming, dict) else {}
        naming['enabled'] = bool(enabled)
        configured['recordingNaming'] = naming

        try:
            self._persistSimpleConfig(configured)
        except Exception as error:
            self._view.setRecordingSuffixEnabled(current)
            self._view.showMessage(
                f'Could not save recording suffix setting: {error}', error=True
            )
            return

        state['limits'] = dataclasses.replace(
            state['limits'],
            recording_naming=dataclasses.replace(
                state['limits'].recording_naming, enabled=bool(enabled)
            ),
        )
        self._view.setRecordingSuffixEnabled(bool(enabled))
        self._notifyRecordingFilenameSuffixChanged()

    def _onSetupEditRequested(self):
        if self._isBusy() or self.__dict__.get('_screeningActive', False):
            return
        state = self._monalisa()
        configured = dict(getattr(self._setupInfo.scan, 'simpleMoNaLISA', None) or {})
        info = dict(state.get('resolvedSetup', {}))
        fx, fy = state['limits'].fov_um
        info['fov_text'] = f'{fx:.3g} × {fy:.3g} µm'
        detector_readout = info.get('detector_readout_s')
        info['readout_text'] = (
            f'{detector_readout * 1e3:.3g} ms' if detector_readout is not None else 'Unavailable'
        )
        patch = self._view.editSetupDefinition(configured, info)
        if patch is None:
            return

        raw = dict(configured)
        raw.update(patch)
        # Removed camera-readout Auto/manual setup model. Preserve legacy files
        # on load, but clean the obsolete keys the next time Setup is saved.
        raw.pop('cameraReadoutSource', None)
        raw.pop('cameraReadoutTimeMs', None)

        # Switching from one fixed period to per-profile ownership should not
        # invalidate every existing profile at once. Seed missing profile
        # periods from the current fixed setup value; each profile can then be
        # edited independently. Existing explicit profile periods are preserved.
        old_policy = state['limits'].ulens_period_policy
        new_policy = str(raw.get('ulensPeriodPolicy') or 'fixed')
        store = state['limits'].profile_store
        if old_policy != 'per_profile' and new_policy == 'per_profile' and store.profiles:
            raw = self._normalizeLiveSourcesForSave(raw)
            seed_period = state['limits'].fixed_ulens_period_um
            profiles = {
                profile_id: (
                    profile if profile.ulens_period_um is not None
                    else dataclasses.replace(profile, ulens_period_um=seed_period)
                )
                for profile_id, profile in store.profiles.items()
            }
            store = dataclasses.replace(store, profiles=profiles)
            raw = config_with_profile_store(raw, store)

        try:
            self._persistSimpleConfig(raw)
        except Exception as error:
            self._view.showMessage(f'Could not save MoNaLISA setup: {error}', error=True)
            return

        state['limits'] = self._cloak.limits_from_setup(self._setupInfo)
        if state['plan'] is not None:
            try:
                state['plan'] = normalize_plan(state['plan'], state['limits'])
                profile = state['limits'].profile_store.profile(state['plan'].profile_id)
                state['binding'] = MoNaLISAProfileBinding.bound(profile, state['plan'].mode_id)
            except Exception as error:
                state['plan'] = None
                state['binding'] = MoNaLISAProfileBinding.unbound()
                self._view.showMessage(
                    f'Setup saved. Edit/select a profile before acquiring: {error}',
                    error=False,
                )
        elif state['limits'].profile_store.profiles:
            try:
                state['plan'] = default_plan(state['limits'])
                profile = state['limits'].profile_store.profile(state['plan'].profile_id)
                state['binding'] = MoNaLISAProfileBinding.bound(profile, state['plan'].mode_id)
            except Exception:
                pass
        self._refreshResolvedSetup(refreshView=True)
        if self.cloakOwnsBackend():
            self._mirrorToBackend(force=True)
        self._refreshSetupSummary()


    def _onCameraReadoutAutoRequested(self):
        if self._isBusy() or self.acquisitionPlan() is None:
            return
        state = self._monalisa()
        detector_readout = state.get('resolvedSetup', {}).get('detector_readout_s')
        if detector_readout is None:
            self._view.showMessage(
                'The current profile camera does not report a usable readout time.',
                error=False,
            )
            return
        plan = state['plan']
        store = state['limits'].profile_store
        try:
            profile = store.profile(plan.profile_id)
            safe_readout = (
                float(detector_readout) + state['limits'].camera_readout_safety_margin_s
            )
            updated = dataclasses.replace(
                profile, camera_readout_time_s=max(0.0, safe_readout)
            )
            new_store = store.with_profile(updated)
        except Exception as error:
            self._view.showMessage(f'Could not calculate camera readout: {error}', error=True)
            return
        if not self._saveProfileStoreOrReport(new_store):
            return
        state = self._monalisa()
        state['binding'] = MoNaLISAProfileBinding.bound(updated, plan.mode_id)
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._refreshSetupSummary()
        self._mirrorToBackend(force=True)
        self._refreshEstimate()

    def _profileWithCurrentDefaults(self, profile, plan):
        geometry_mode = str(getattr(profile, 'lateral_geometry_mode', 'coupled') or 'coupled')
        steps = []
        for step in profile.steps:
            if step.role != 'readout' and step.duration_editable and step.id in plan.step_durations_s:
                steps.append(dataclasses.replace(
                    step, default_duration_s=float(plan.step_durations_s[step.id])
                ))
            else:
                steps.append(step)
        updates = {
            'default_mode': plan.mode_id,
            'steps': tuple(steps),
            'default_z_step_um': float(plan.z_step_um),
            'default_z_scan_length_um': float(plan.z_length_um),
        }
        if geometry_mode == 'independent':
            updates.update({
                'default_lateral_step_um': None,
                'default_x_step_um': plan.step_um_for_axis('X'),
                'default_y_step_um': plan.step_um_for_axis('Y'),
            })
            if profile.scan_extent_source == 'manual':
                updates.update({
                    'manual_scan_extent_um': None,
                    'manual_scan_extent_x_um': plan.length_um_for_axis('X'),
                    'manual_scan_extent_y_um': plan.length_um_for_axis('Y'),
                })
        else:
            updates.update({
                'default_lateral_step_um': plan.step_um_for_axis('X'),
                'default_x_step_um': None,
                'default_y_step_um': None,
            })
            if profile.scan_extent_source == 'manual':
                updates.update({
                    'manual_scan_extent_um': plan.length_um_for_axis('X'),
                    'manual_scan_extent_x_um': None,
                    'manual_scan_extent_y_um': None,
                })
        return dataclasses.replace(profile, **updates)

    def _refreshViewAfterProfileStoreChange(self):
        state = self._monalisa()
        self._view.setPlan(state['plan'])
        self._view.setBinding(state['binding'])
        self._refreshSetupSummary()
        self._refreshLiveControls()
        self._refreshEstimate()

    def _saveProfileStoreOrReport(self, store, *, refresh_view=True):
        try:
            self._persistProfileStore(store, refresh_view=refresh_view)
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
        profile = self._view.editProfileDefinition(
            seed, state['limits'].ttl_devices, state['limits']
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
            duplicate, state['limits'].ttl_devices, state['limits']
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
            current, state['limits'].ttl_devices, state['limits']
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
            if (current.scan_extent_source != edited.scan_extent_source
                    and edited.scan_extent_source == 'manual'
                    and edited.manual_scan_extent_um is not None):
                plan = dataclasses.replace(
                    plan, lateral_length_um=edited.manual_scan_extent_um
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
        # Do not refresh through the old plan while persisting.  If the active
        # profile is being deleted, setPlan(old_plan) can otherwise reinsert
        # the deleted id into the combo before we select the replacement.
        if not self._saveProfileStoreOrReport(new_store, refresh_view=False):
            return
        state = self._monalisa()
        self._view.configure(state['limits'])
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
            plan = self._planForProfile(profile_id, preserve_if_same=False)
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        self._setBoundPlan(plan)

    def _onProfileReloadRequested(self, profile_id: str):
        if self._isBusy() or not profile_id:
            return
        try:
            plan = self._planForProfile(profile_id, preserve_if_same=False)
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        self._setBoundPlan(plan)

    def _onProfileDefaultsUpdateRequested(self, profile_id: str):
        if self._isBusy() or not self.cloakOwnsBackend() or not profile_id:
            return
        state = self._monalisa()
        plan = state['plan']
        if plan is None or plan.profile_id != profile_id:
            self._view.showMessage(
                'Select the profile you want to update before saving defaults.',
                error=True,
            )
            return
        store = state['limits'].profile_store
        try:
            profile = store.profile(profile_id)
            updated = self._profileWithCurrentDefaults(profile, plan)
            new_store = store.with_profile(updated)
        except Exception as error:
            self._view.showMessage(
                f'Could not update MoNaLISA profile defaults: {error}', error=True
            )
            return
        if not self._saveProfileStoreOrReport(new_store):
            return
        state = self._monalisa()
        try:
            rebound = adapt_plan_to_profile_definition(state['plan'], updated, state['limits'])
        except Exception:
            rebound = state['plan']
        state['plan'] = rebound
        state['binding'] = MoNaLISAProfileBinding.bound(updated, rebound.mode_id)
        self._refreshViewAfterProfileStoreChange()
        self._mirrorToBackend(force=True)
        self._view.showMessage('Profile defaults updated from the current Simple settings.', error=False)

    def _onModeRequested(self, mode_id: str):
        if self._isBusy() or not self.cloakOwnsBackend() or not mode_id:
            return
        try:
            plan = select_mode(self.acquisitionPlan(), mode_id, self.cloakLimits())
        except Exception as error:
            self._view.showMessage(str(error), error=True)
            return
        self._setBoundPlan(plan)

    def _onBackendWidgetEdited(self, *args):
        # Once the user manually edits Advanced, the retained loaded snapshot
        # is no longer the acquisition on screen. Do not leave actions around
        # that could later overwrite those edits. Programmatic setParameters()
        # during a resolution action is protected by ``settingParameters``.
        if (not self._onSimplePage()
                and self.__dict__.get('_pendingMoNaLISALoadResolution') is not None
                and not getattr(self, 'settingParameters', False)):
            self._clearPendingLoadResolution()
        return super()._onBackendWidgetEdited(*args)

    def _onPlanEdited(self, plan):
        if not self.cloakOwnsBackend():
            return
        self._clearPendingLoadResolution()
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
        return laserPowerModeFromSetup(self._setupInfo) is LaserPowerMode.SEPARATED

    def _availableLiveSources(self):
        """Setup-owned Live screening sources, independent of profile/mode.

        New setups persist ``simpleMoNaLISA.liveSources``.  Older setups are
        accepted without modification by deriving the same list from all
        profile laser devices until the next successful SimpleMoNaLISA config
        save performs the one-way migration.
        """
        configured = dict(getattr(self._setupInfo.scan, 'simpleMoNaLISA', None) or {})
        known = set(self._configuredLaserNames())
        if 'liveSources' in configured:
            values = configured.get('liveSources') or ()
            if not isinstance(values, (list, tuple, set)):
                return ()
            return tuple(dict.fromkeys(
                str(value).strip()
                for value in values
                if str(value).strip() in known
            ))
        return self._legacyLiveSourcesFromConfig(configured)

    def _defaultSelectedLiveSources(self):
        """Preserve the old ON + READOUT default for the first runtime use."""
        if not self.cloakOwnsBackend() or self.acquisitionPlan() is None:
            return set()
        plan = self.acquisitionPlan()
        profile = self.cloakLimits().profile_store.profile(plan.profile_id)
        mode = profile.mode(plan.mode_id)
        available = set(self._availableLiveSources())
        selected = set()
        for step in profile.steps:
            if step.role not in ('on', 'readout'):
                continue
            for device in mode.devices_for_step(step):
                if device in available:
                    selected.add(device)
        return selected

    def _selectedLiveSources(self):
        state = self._monalisa()
        available = set(self._availableLiveSources())
        selected = state.get('selectedLiveSources')
        if selected is None:
            selected = self._defaultSelectedLiveSources()
        selected = {source for source in selected if source in available}
        state['selectedLiveSources'] = selected
        return set(selected)

    def _setSelectedLiveSources(self, sources):
        available = set(self._availableLiveSources())
        self._monalisa()['selectedLiveSources'] = {
            str(source) for source in sources if str(source) in available
        }

    def _onLiveSourcesEditRequested(self):
        if self._isBusy() or self.__dict__.get('_screeningActive', False):
            return
        configured = self._configuredLaserNames()
        chosen = self._view.editLiveSources(
            configured,
            self._availableLiveSources(),
        )
        if chosen is None:
            return
        chosen = tuple(
            source for source in configured if source in set(chosen)
        )
        raw = dict(getattr(self._setupInfo.scan, 'simpleMoNaLISA', None) or {})
        raw['liveSources'] = list(chosen)
        selected = self._selectedLiveSources()
        try:
            self._persistSimpleConfig(raw)
        except Exception as error:
            self._view.showMessage(
                f'Could not save Live screening sources: {error}', error=True
            )
            return
        self._setSelectedLiveSources(selected.intersection(chosen))
        self._refreshLiveControls()
        self._view.showMessage('')

    def _profileScreeningDetectors(self):
        """Free-running acquisition detectors in active READOUT steps."""
        return self._activeProfileDetectors()

    def _refreshLiveControls(self):
        if not hasattr(self, '_view'):
            return
        self._releaseScreeningTriggerIfCameraChanged()
        sources = self._availableLiveSources()
        available = self._livePowerIsSeparated() and bool(sources)
        reason = ''
        if not self._livePowerIsSeparated():
            reason = (
                'Live screening requires separated Live/Scan laser powers. '
                'The current laser power mode shares acquisition and Live power.'
            )
        elif not sources:
            reason = (
                'No Live screening sources are configured. Use the source button '
                'next to LIVE to choose them.'
            )
        elif not self._profileScreeningDetectors():
            available = False
            reason = 'The selected profile/mode has no free-running acquisition camera.'
        self._view.setLiveSources(
            sources,
            self._selectedLiveSources() if sources else set(),
            available=available,
            reason=reason,
        )

    def _screeningLaserTargets(self, sources=None):
        selected = self._selectedLiveSources() if sources is None else set(sources)
        available = set(self._availableLiveSources())
        devices = [
            source for source in self._availableLiveSources()
            if source in selected and source in available
        ]
        return tuple(
            self._commChannel.liveScreeningWorkflow.resolve_live_lasers(devices)
        )

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

    def _ensureSimpleCameraTriggerOverride(self, detectorNames, mode):
        """Set the Simple camera trigger while retaining its original setting.

        Live requests ``internal`` and Stop requests ``external_frame``.  Both
        belong to one Simple-camera ownership interval: the original pre-Live
        value is restored only when Simple relinquishes the camera (page switch,
        profile/mode camera change, or controller close), rather than on every
        Live/Stop transition.
        """
        detectorNames = tuple(detectorNames)
        snapshot = self.__dict__.get('_screeningTriggerSnapshot')
        if snapshot is not None and set(snapshot) != set(detectorNames):
            if not self._releaseScreeningTriggerOverride():
                raise RuntimeError('Could not restore the previous Simple camera trigger.')
            snapshot = None

        immediate = self._setTriggerModeTransactional(detectorNames, mode)
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
        selected = self._selectedLiveSources()
        if not selected:
            self._view.showMessage('Select at least one Live screening illumination source.', error=True)
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
            self._ensureSimpleCameraTriggerOverride(detectors, 'internal')
            targets = self._screeningLaserTargets(selected)
            if not targets:
                raise RuntimeError('The selected screening choices do not resolve to a laser.')
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

    def _onLiveSourceToggled(self, source: str, checked: bool):
        before = self._selectedLiveSources()
        after = set(before)
        if checked:
            after.add(source)
        else:
            after.discard(source)
        self._setSelectedLiveSources(after)
        if not self.__dict__.get('_screeningActive', False):
            return
        try:
            targets = self._screeningLaserTargets(after)
            self._applyScreeningLaserSet(targets)
            if not targets:
                self.__dict__['_screeningActive'] = False
                self._view.setScreeningActive(False)
        except Exception as error:
            self._setSelectedLiveSources(before)
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
        self, start, *, requireLiveView, keepPreparedIfIdle=False,
        refreshSetup=True,
    ):
        # The first point captures ROI/readout-speed changes immediately before
        # compilation. Continuations of one retained ScanLapse deliberately do
        # not re-resolve/mirror Simple state: that acquisition was frozen at its
        # first timepoint.
        if refreshSetup:
            self._refreshResolvedSetup(refreshView=True, mirror=True)
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
        if self._view.saveAcquisitionRequested():
            detectors = self._activeProfileDetectors()
            try:
                result = self._commChannel.recordingWorkflow.record_scan_series(
                    self, detectors, use_series=self._view.scanSeriesEnabled()
                )
            except Exception as error:
                self._view.showMessage(
                    f'Could not start recorded acquisition: {error}', error=True
                )
                return None
            if not result.accepted:
                self._view.showMessage(
                    f'Could not start recorded acquisition: {result.reason}', error=True
                )
            else:
                self._view.showMessage('')
            return result
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
            refreshSetup=bool(recalculateSignals),
        )

    def _onStopClicked(self):
        # The top-row Stop belongs only to Live screening. It deliberately
        # keeps the useful Live -> Stop -> Scan camera-trigger preparation.
        if self._onSimplePage():
            if not self._stopScreening():
                return
            detectors = self._profileScreeningDetectors()
            if not detectors:
                self._view.showMessage(
                    'No acquisition camera is available to prepare.', error=True
                )
                return
            try:
                self._ensureSimpleCameraTriggerOverride(detectors, 'external_frame')
            except Exception as error:
                self._view.showMessage(
                    f'Screening stopped, but camera trigger preparation failed: {error}',
                    error=True,
                )
                return
            self._view.showMessage('')
            return
        return super()._onStopClicked()

    def _onScanStopClicked(self):
        """Gracefully stop a recorded timelapse after the current point."""
        if not self._onSimplePage():
            return
        if not self.__dict__.get('_recordedSeriesActive', False):
            return
        workflow = getattr(self._commChannel, 'recordingWorkflow', None)
        stop = getattr(workflow, 'stop_scan_series', None)
        if callable(stop) and stop(self):
            self._view.showMessage(
                'Stopping timelapse; the current scan will finish if one is running.'
            )
            return
        self._view.showMessage(
            'Could not stop the active recorded acquisition series.',
            error=True,
        )

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
        self._setRecordingAcquisitionOwnership(False)
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
            self._syncRecordingAcquisitionOwnership()
            return True

        # The Stop control disappears on Advanced, so do not carry Simple-owned
        # illumination or its persistent camera-trigger override across the
        # page boundary.
        leavingSimple = self._onSimplePage() and page != self.scanPage()
        if leavingSimple and self.__dict__.get('_screeningActive', False):
            if not self._stopScreening():
                return False
        if leavingSimple and not self._releaseScreeningTriggerOverride():
            return False
        switched = super().setScanPage(page, discardAdvanced=discardAdvanced)
        if switched and self._onSimplePage():
            self._refreshLiveControls()
        if switched:
            self._syncRecordingAcquisitionOwnership()
            self._notifyRecordingFilenameSuffixChanged()
        return switched

    def _acquisitionContext(self, profile_id: str) -> dict:
        """Setup-derived values that materially define one Simple acquisition."""
        limits = self.cloakLimits()
        ulens = limits.resolve_ulens_period(profile_id)
        return {
            'sampleRateHz': float(limits.sample_rate),
            'interPulseDelayMs': float(limits.inter_pulse_delay_s * 1e3),
            'readoutPulseTimeMs': float(limits.readout_pulse_time_s * 1e3),
            'cameraReadoutTimeMs': float(
                limits.camera_readout_time_s(profile_id) * 1e3
            ),
            'cameraReadoutSource': limits.camera_readout_source(profile_id),
            'ulens': {
                'effectivePeriodUm': float(ulens.value_um),
                'sourceKind': str(ulens.source_kind),
                'sourceId': str(ulens.source_id),
            },
        }

    def _savedMoNaLISAMetadata(self):
        """Return provenance for a genuinely bound Simple scan, else ``None``.

        A binding object is intentionally kept while the Advanced page is being
        edited, so ``binding.is_bound`` alone is not enough: provenance is only
        valid while Simple owns the backend and the binding still fingerprints
        the profile currently in the setup.
        """
        if not self._onSimplePage() or not self.cloakOwnsBackend():
            return None
        state = self._monalisa()
        plan = state['plan']
        binding = state['binding']
        if plan is None or not binding.is_bound:
            return None
        try:
            profile = state['limits'].profile_store.profile(plan.profile_id)
            profile.mode(plan.mode_id)
        except MoNaLISAProfileError:
            return None
        current_fingerprint = profile_fingerprint(profile)
        if (
            binding.profile_id != plan.profile_id
            or binding.mode_id != plan.mode_id
            or binding.profile_fingerprint != current_fingerprint
        ):
            return None
        return {
            'schemaVersion': self.savedMoNaLISASchemaVersion,
            'profileId': profile.id,
            'modeId': plan.mode_id,
            'profileFingerprint': current_fingerprint,
            'profileSnapshot': profile.to_config(),
            'acquisitionContext': self._acquisitionContext(profile.id),
        }

    def cloakStateExtras(self) -> dict:
        extras = super().cloakStateExtras()
        monalisa = self._savedMoNaLISAMetadata()
        if monalisa is not None:
            extras['monalisa'] = monalisa
        return extras

    def applyCloakStateExtras(self, extras):
        # ``liveRolesByMode`` from older scan files is intentionally ignored.
        # Live screening illumination is now setup/session UI state and loading
        # an acquisition must not change it.
        self._refreshLiveControls()

    @staticmethod
    def _savedFloatMatches(saved, current) -> bool:
        try:
            return math.isclose(
                float(saved), float(current), rel_tol=1e-9, abs_tol=1e-12
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _formatULensPeriod(value_um) -> str:
        try:
            value_um = float(value_um)
        except (TypeError, ValueError):
            return 'unknown'
        if value_um < 1.0:
            return f'{value_um * 1000:g} nm'
        return f'{value_um:g} µm'

    @staticmethod
    def _ulensSourceDescription(kind: str) -> str:
        return {
            'setup': 'setup uLens',
            'profile': 'profile uLens',
            'slm': 'SLM-derived uLens',
        }.get(str(kind), f'{kind or "unknown"} uLens')

    def _loadCompatibilityFailure(self, issue: str, message: str):
        return _LoadedMoNaLISACompatibility(
            plan=None, issue=str(issue), message=str(message)
        )

    def _loadedMoNaLISACompatibility(self, metadata, state):
        """Describe whether a saved Simple binding still matches this setup.

        The backend dictionaries in ``state`` are authoritative.  A failure is
        classified so the load-resolution bar can offer only explicit actions
        that make sense for that mismatch.
        """
        fail = self._loadCompatibilityFailure
        if not isinstance(metadata, dict):
            return fail(
                'legacy_metadata',
                'No Simple MoNaLISA binding metadata was saved.',
            )
        if metadata.get('schemaVersion') != self.savedMoNaLISASchemaVersion:
            return fail(
                'unsupported_metadata',
                'The saved Simple MoNaLISA metadata version is not supported by '
                'this installation.',
            )

        profile_id = metadata.get('profileId')
        mode_id = metadata.get('modeId')
        saved_fingerprint = metadata.get('profileFingerprint')
        snapshot = metadata.get('profileSnapshot')
        context = metadata.get('acquisitionContext')
        if not all((profile_id, mode_id, saved_fingerprint)) or not isinstance(snapshot, dict):
            return fail(
                'incomplete_metadata',
                'The saved Simple MoNaLISA binding metadata is incomplete.',
            )
        if not isinstance(context, dict):
            return fail(
                'incomplete_metadata',
                'The saved Simple MoNaLISA acquisition context is missing.',
            )

        limits = self.cloakLimits()
        try:
            profile = limits.profile_store.profile(str(profile_id))
        except MoNaLISAProfileError:
            return fail(
                'profile_missing',
                f'MoNaLISA profile {profile_id!r} is not available in this setup.',
            )
        try:
            profile.mode(str(mode_id))
        except MoNaLISAProfileError:
            return fail(
                'profile_changed',
                f'MoNaLISA mode {mode_id!r} is no longer available in profile '
                f'{profile.label!r}.',
            )

        # Check acquisition-driving setup values before the broad fingerprint so
        # setup/uLens/readout changes get a specific, resolvable diagnostic.
        try:
            current_context = self._acquisitionContext(profile.id)
        except Exception as error:
            return fail(
                'backend_incompatible',
                f'The current MoNaLISA acquisition context cannot be resolved: {error}',
            )

        saved_ulens = context.get('ulens')
        current_ulens = current_context['ulens']
        if not isinstance(saved_ulens, dict):
            return fail('incomplete_metadata', 'The saved uLens acquisition context is missing.')
        saved_kind = str(saved_ulens.get('sourceKind') or '')
        current_kind = str(current_ulens.get('sourceKind') or '')
        saved_source_id = str(saved_ulens.get('sourceId') or '')
        current_source_id = str(current_ulens.get('sourceId') or '')
        if saved_kind != current_kind or saved_source_id != current_source_id:
            return fail(
                'ulens_mismatch',
                'The uLens period source changed from '
                f'{self._ulensSourceDescription(saved_kind)} ({saved_source_id or "unknown"}) '
                f'to {self._ulensSourceDescription(current_kind)} '
                f'({current_source_id or "unknown"}).',
            )
        if not self._savedFloatMatches(
            saved_ulens.get('effectivePeriodUm'), current_ulens['effectivePeriodUm']
        ):
            return fail(
                'ulens_mismatch',
                f'The saved scan used '
                f'{self._ulensSourceDescription(saved_kind)} period '
                f'{self._formatULensPeriod(saved_ulens.get("effectivePeriodUm"))}; '
                f'the current configuration resolves '
                f'{self._formatULensPeriod(current_ulens["effectivePeriodUm"])}.',
            )

        timing_checks = (
            ('sampleRateHz', 'sample rate', 'Hz'),
            ('interPulseDelayMs', 'inter-pulse delay', 'ms'),
            ('readoutPulseTimeMs', 'readout pulse time', 'ms'),
        )
        for key, label, unit in timing_checks:
            if not self._savedFloatMatches(context.get(key), current_context[key]):
                return fail(
                    'timing_mismatch',
                    f'The saved {label} was {context.get(key)!r} {unit}; '
                    f'the current Simple configuration resolves '
                    f'{current_context[key]:g} {unit}.',
                )

        if not self._savedFloatMatches(
            context.get('cameraReadoutTimeMs'), current_context['cameraReadoutTimeMs']
        ):
            return fail(
                'readout_mismatch',
                f'The saved camera readout time was '
                f'{context.get("cameraReadoutTimeMs")!r} ms; the current Simple '
                f'configuration resolves {current_context["cameraReadoutTimeMs"]:g} ms.',
            )
        if str(context.get('cameraReadoutSource') or '') != str(
            current_context['cameraReadoutSource']
        ):
            return fail(
                'readout_mismatch',
                'The camera readout source changed from '
                f'{context.get("cameraReadoutSource")!r} to '
                f'{current_context["cameraReadoutSource"]!r}.',
            )

        if profile_fingerprint(profile) != str(saved_fingerprint):
            return fail(
                'profile_changed',
                f'MoNaLISA profile {profile.label!r} has changed since this scan was saved.',
            )

        try:
            reference = default_plan(limits, profile.id)
            reference = select_mode(reference, str(mode_id), limits)
            parsed = dicts_to_plan(
                state.get('analogParameterDict') or {},
                state.get('digitalParameterDict') or {},
                limits,
                reference_plan=reference,
            )
        except (PlanNotRepresentable, MoNaLISAProfileError) as error:
            return fail(
                'backend_incompatible',
                f'The saved backend scan no longer matches that profile/mode: {error}',
            )

        canonical = self._cloak.to_backend(parsed, limits)
        saved_backend = (
            state.get('analogParameterDict') or {},
            state.get('digitalParameterDict') or {},
        )
        if canonical != saved_backend:
            return fail(
                'backend_incompatible',
                'The saved backend scan is not an exact Simple MoNaLISA round-trip '
                'with the current code/setup.',
            )
        return _LoadedMoNaLISACompatibility(plan=parsed)

    @staticmethod
    def _loadResolutionActions(issue: str):
        actions = {
            'profile_missing': (
                ('import_saved_profile', 'Import saved profile'),
                ('keep_advanced', 'Keep as Advanced'),
            ),
            'profile_changed': (
                ('use_current_profile', 'Use current profile'),
                ('import_saved_profile', 'Import saved profile'),
                ('keep_advanced', 'Keep as Advanced'),
            ),
            'ulens_mismatch': (
                ('use_current_ulens', 'Use current uLens'),
                ('keep_advanced', 'Keep as Advanced'),
            ),
            'readout_mismatch': (
                ('use_current_readout', 'Use current readout'),
                ('keep_advanced', 'Keep as Advanced'),
            ),
            'timing_mismatch': (
                ('use_current_timing', 'Use current timing'),
                ('keep_advanced', 'Keep as Advanced'),
            ),
        }
        return actions.get(str(issue), (('keep_advanced', 'Keep as Advanced'),))

    def _showPendingLoadResolution(self, result):
        pending = self.__dict__.get('_pendingMoNaLISALoadResolution')
        if pending is None:
            self._panel.clearLoadResolution()
            return
        pending['issue'] = result.issue
        message = f'Loaded scan kept unchanged. {result.message}'
        self._panel.setLoadResolution(
            message, self._loadResolutionActions(result.issue)
        )

    def _setPendingLoadResolution(self, metadata, state, result):
        self.__dict__['_pendingMoNaLISALoadResolution'] = {
            'metadata': copy.deepcopy(metadata),
            'state': copy.deepcopy(state),
            'issue': result.issue,
        }
        self._showPendingLoadResolution(result)

    def _clearPendingLoadResolution(self):
        self.__dict__.pop('_pendingMoNaLISALoadResolution', None)
        if hasattr(self, '_panel'):
            self._panel.clearLoadResolution()

    def _savedContextLimits(self, metadata):
        """Recreate the historical Simple context from saved provenance.

        Provenance is intentionally reduced to effective values here.  This is
        only a parser/compiler context for the saved acquisition; current setup
        compatibility is checked separately by ``_loadedMoNaLISACompatibility``.
        """
        if not isinstance(metadata, dict):
            raise PlanNotRepresentable('Saved MoNaLISA metadata is missing.')
        profile_id = str(metadata.get('profileId') or '')
        snapshot = metadata.get('profileSnapshot')
        context = metadata.get('acquisitionContext')
        if not profile_id or not isinstance(snapshot, dict) or not isinstance(context, dict):
            raise PlanNotRepresentable('Saved MoNaLISA metadata is incomplete.')

        try:
            profile = MoNaLISAProfileDefinition.from_config(profile_id, snapshot)
        except MoNaLISAProfileError as error:
            raise PlanNotRepresentable(str(error)) from error

        ulens = context.get('ulens') or {}
        try:
            period_um = float(ulens.get('effectivePeriodUm'))
            sample_rate = float(context.get('sampleRateHz'))
            inter_delay_s = float(context.get('interPulseDelayMs')) * 1e-3
            readout_pulse_s = float(context.get('readoutPulseTimeMs')) * 1e-3
            camera_readout_s = float(context.get('cameraReadoutTimeMs')) * 1e-3
        except (TypeError, ValueError) as error:
            raise PlanNotRepresentable(
                f'Saved MoNaLISA acquisition context is invalid: {error}'
            ) from error

        ulens_kind = str(ulens.get('sourceKind') or '')
        if ulens_kind == 'profile':
            profile = dataclasses.replace(profile, ulens_period_um=period_um)
            period_policy = 'per_profile'
        else:
            period_policy = 'fixed'

        if str(context.get('cameraReadoutSource') or '') == 'profile':
            profile = dataclasses.replace(profile, camera_readout_time_s=camera_readout_s)
            default_readout_s = self.cloakLimits().default_camera_readout_time_s
        else:
            profile = dataclasses.replace(profile, camera_readout_time_s=None)
            default_readout_s = camera_readout_s

        store = MoNaLISAProfileStore(
            profiles={profile.id: profile}, default_profile=profile.id
        )
        return dataclasses.replace(
            self.cloakLimits(),
            sample_rate=sample_rate,
            ulens_period_policy=period_policy,
            fixed_ulens_period_um=period_um,
            profile_store=store,
            inter_pulse_delay_s=max(0.0, inter_delay_s),
            readout_pulse_time_s=max(1e-3, readout_pulse_s),
            default_camera_readout_time_s=max(0.0, default_readout_s),
        )

    def _savedPlanForPendingResolution(self, pending=None):
        pending = pending or self.__dict__.get('_pendingMoNaLISALoadResolution')
        if not pending:
            raise PlanNotRepresentable('There is no loaded MoNaLISA scan to resolve.')
        metadata = pending['metadata']
        state = pending['state']
        limits = self._savedContextLimits(metadata)
        profile_id = str(metadata['profileId'])
        mode_id = str(metadata['modeId'])
        try:
            reference = default_plan(limits, profile_id)
            reference = select_mode(reference, mode_id, limits)
            plan = dicts_to_plan(
                state.get('analogParameterDict') or {},
                state.get('digitalParameterDict') or {},
                limits,
                reference_plan=reference,
            )
        except (PlanNotRepresentable, MoNaLISAProfileError) as error:
            raise PlanNotRepresentable(
                f'The saved Simple plan cannot be reconstructed: {error}'
            ) from error
        if self._cloak.to_backend(plan, limits) != (
            state.get('analogParameterDict') or {},
            state.get('digitalParameterDict') or {},
        ):
            raise PlanNotRepresentable(
                'The saved scan is not an exact round-trip in its saved acquisition context.'
            )
        return plan

    def _writePendingBackend(self, analog, digital):
        self._analogParameterDict = copy.deepcopy(analog)
        self._digitalParameterDict = copy.deepcopy(digital)
        self._positionersScan = self._cloak.scanned_positioners(analog)
        ScanControllerAdvanced.setParameters(self)
        publish_laser_state = getattr(self, '_publishLaserScanState', None)
        if callable(publish_laser_state):
            publish_laser_state()
        self._designCache = None

    def _replacePendingAcquisition(self, plan, metadata, limits):
        pending = self.__dict__.get('_pendingMoNaLISALoadResolution')
        if pending is None:
            raise PlanNotRepresentable('There is no loaded MoNaLISA scan to resolve.')
        plan = normalize_plan(plan, limits)
        analog, digital = self._cloak.to_backend(plan, limits)
        self._writePendingBackend(analog, digital)
        pending['metadata'] = copy.deepcopy(metadata)
        pending['state']['analogParameterDict'] = copy.deepcopy(analog)
        pending['state']['digitalParameterDict'] = copy.deepcopy(digital)
        return plan

    def _rerunPendingLoadCompatibility(self):
        pending = self.__dict__.get('_pendingMoNaLISALoadResolution')
        if pending is None:
            return
        result = self._loadedMoNaLISACompatibility(
            pending['metadata'], pending['state']
        )
        if not result.compatible:
            self._clearLoadedSimpleBinding()
            self._enterPage('advanced')
            self._panel.showPage('advanced')
            self._showPendingLoadResolution(result)
            return

        self.adoptPlan(result.plan)
        self._enterPage('simple')
        self._panel.showPage('simple')
        self._panel.showNote('')
        self._clearPendingLoadResolution()
        self._mirrorToBackend(force=True)

    def _onLoadResolutionActionRequested(self, action: str):
        if self._isBusy():
            return
        pending = self.__dict__.get('_pendingMoNaLISALoadResolution')
        if pending is None:
            return
        action = str(action)
        if action == 'keep_advanced':
            self._clearPendingLoadResolution()
            self._enterPage('advanced')
            self._panel.showPage('advanced')
            self._panel.showNote('')
            return

        try:
            if action == 'import_saved_profile':
                self._resolveLoadByImportingSavedProfile(pending)
            elif action == 'use_current_profile':
                self._resolveLoadUsingCurrentProfile(pending)
            elif action == 'use_current_ulens':
                self._resolveLoadUsingCurrentContext(pending, 'ulens')
            elif action == 'use_current_readout':
                self._resolveLoadUsingCurrentContext(pending, 'readout')
            elif action == 'use_current_timing':
                self._resolveLoadUsingCurrentContext(pending, 'timing')
            else:
                return
        except Exception as error:
            self._view.showMessage(
                f'Could not resolve loaded scan: {error}', error=True
            )
            return
        self._rerunPendingLoadCompatibility()

    def _resolveLoadUsingCurrentContext(self, pending, section: str):
        metadata = copy.deepcopy(pending['metadata'])
        profile_id = str(metadata.get('profileId') or '')
        current = self._acquisitionContext(profile_id)
        context = metadata['acquisitionContext']
        if section == 'ulens':
            context['ulens'] = copy.deepcopy(current['ulens'])
        elif section == 'readout':
            context['cameraReadoutTimeMs'] = current['cameraReadoutTimeMs']
            context['cameraReadoutSource'] = current['cameraReadoutSource']
        elif section == 'timing':
            for key in ('sampleRateHz', 'interPulseDelayMs', 'readoutPulseTimeMs'):
                context[key] = current[key]
        else:
            raise ValueError(f'Unknown load-resolution context {section!r}.')

        plan = self._savedPlanForPendingResolution(pending)
        target_limits = self._savedContextLimits(metadata)
        plan = normalize_plan(plan, target_limits)
        self._replacePendingAcquisition(plan, metadata, target_limits)

    def _resolveLoadUsingCurrentProfile(self, pending):
        metadata = copy.deepcopy(pending['metadata'])
        profile_id = str(metadata.get('profileId') or '')
        current_profile = self.cloakLimits().profile_store.profile(profile_id)
        saved_plan = self._savedPlanForPendingResolution(pending)
        plan = adapt_plan_to_profile_definition(
            saved_plan, current_profile, self.cloakLimits()
        )
        metadata['modeId'] = plan.mode_id
        metadata['profileFingerprint'] = profile_fingerprint(current_profile)
        metadata['profileSnapshot'] = current_profile.to_config()
        metadata['acquisitionContext'] = self._acquisitionContext(current_profile.id)
        self._replacePendingAcquisition(plan, metadata, self.cloakLimits())

    def _resolveLoadByImportingSavedProfile(self, pending):
        metadata = copy.deepcopy(pending['metadata'])
        snapshot = metadata.get('profileSnapshot')
        original_id = str(metadata.get('profileId') or '')
        if not original_id or not isinstance(snapshot, dict):
            raise PlanNotRepresentable('The saved profile snapshot is missing.')
        saved_profile = MoNaLISAProfileDefinition.from_config(original_id, snapshot)
        store = self.cloakLimits().profile_store

        if original_id not in store.profiles:
            imported = saved_profile
        else:
            label = f'{saved_profile.label} (saved)'
            new_id = profile_id_from_label(label, store.profiles)
            imported = dataclasses.replace(saved_profile, id=new_id, label=label)

        new_store = store.with_profile(imported)
        if not self._saveProfileStoreOrReport(new_store):
            raise RuntimeError('The saved profile could not be persisted.')

        metadata['profileId'] = imported.id
        metadata['profileFingerprint'] = profile_fingerprint(imported)
        metadata['profileSnapshot'] = imported.to_config()
        ulens = metadata.get('acquisitionContext', {}).get('ulens')
        if isinstance(ulens, dict) and str(ulens.get('sourceKind') or '') == 'profile':
            ulens['sourceId'] = (
                f'simpleMoNaLISA.profiles.{imported.id}.ulensPeriodUm'
            )
        pending['metadata'] = metadata

    def _clearLoadedSimpleBinding(self):
        """Forget pre-load Simple interpretation while preserving the backend."""
        state = self._monalisa()
        state['plan'] = None
        state['binding'] = MoNaLISAProfileBinding.unbound()
        if hasattr(self, '_view'):
            self._view.setPlan(None)
            self._view.setBinding(state['binding'])
            self._refreshSetupSummary()
            self._refreshLiveControls()
            self._refreshEstimate()

    def applyComponentState(self, state, *, applyMode):
        """Load backend first; re-bind Simple only when provenance still matches."""
        original_page = self.scanPage()
        if self._isBusy():
            self.__dict__['_stateApplied'] = False
            return ['A scan acquisition is active; scan parameters were not changed.']

        # Applying while Advanced prevents ScanCloakController.setParameters()
        # from interpreting/recompiling the incoming dictionaries before they
        # have been accepted as the authoritative saved acquisition.
        self._enterPage('advanced')
        self._panel.showPage('advanced')
        warnings = super().applyComponentState(state, applyMode=applyMode)
        if not bool(self.__dict__.get('_stateApplied')):
            self._enterPage(original_page)
            self._panel.showPage(original_page)
            if original_page == 'simple':
                self._mirrorToBackend(force=True)
            return warnings

        cloak = state.get('cloak') if isinstance(state, dict) else None
        cloak = cloak if isinstance(cloak, dict) else None
        self._clearPendingLoadResolution()
        # A scan intentionally saved from Advanced is an Advanced scan. Do not
        # try to manufacture Simple provenance for it.
        if cloak and cloak.get('page') == 'advanced':
            self._clearLoadedSimpleBinding()
            self._enterPage('advanced')
            self._panel.showPage('advanced')
            self._panel.showNote('')
            return warnings

        metadata = cloak.get('monalisa') if cloak else None
        result = self._loadedMoNaLISACompatibility(metadata, state)
        if not result.compatible:
            self._clearLoadedSimpleBinding()
            self._enterPage('advanced')
            self._panel.showPage('advanced')
            self._panel.showNote('')
            self._setPendingLoadResolution(metadata, state, result)
            warning = f'Loaded scan kept unchanged in Advanced. {result.message}'
            warnings.append(warning)
            return warnings

        self.adoptPlan(result.plan)
        self._enterPage('simple')
        self._panel.showPage('simple')
        self._panel.showNote('')
        self._clearPendingLoadResolution()
        # Exact round-trip was checked above, so this does not alter the saved
        # acquisition; it only re-establishes the normal Simple/backend mirror.
        self._mirrorToBackend(force=True)
        return warnings

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
            self._notifyRecordingFilenameSuffixChanged()
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
        self._notifyRecordingFilenameSuffixChanged()


__all__ = ['ScanControllerSimpleMoNaLISA']
