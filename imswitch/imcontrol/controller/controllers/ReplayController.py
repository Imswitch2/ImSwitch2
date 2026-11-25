"""Load the settings stored in a recording into the running setup ("replay").

Linked to :class:`~imswitch.imcontrol.view.ReplaySettingsDialog` (or to no
widget when used from scripts only).  The flow is:

1. read the attributes of one detector group of an HDF5 file / Zarr store;
2. resolve them against a read-only snapshot of this setup
   (:mod:`imswitch.imcontrol.model.replay`) -- nothing is applied yet;
3. interactive: show the plan in the dialog, which is also the confirmation;
   unattended (API): keep exact, in-range matches only;
4. apply the plan through the panels' own setters, in a fixed order (detector
   settings, scan parameters, laser values, then rotator and stage moves),
   every step guarded so a failure is recorded for that value and never
   escapes into a Qt slot;
5. publish the replay provenance (``replay:*``) so later recordings say where
   their settings came from.

It also publishes, at startup, the setup identity every recording carries:
``setup:name``, ``setup:hash``, ``setup:inventory`` and
``metadata:schema_version``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model import replay
from imswitch.imcontrol.model import replay_inventory
from imswitch.imcontrol.model.calibrations import laser_value_scale
from ..basecontrollers import ComponentStateApplyMode, ImConWidgetController

#: Remembered device choices per (source setup hash -> this setup hash).
REPLAY_MAPPINGS_FILENAME = 'imcontrol_replay_mappings.json'

_FAILED_SCAN_MARKERS = ('not applied', 'not changed', 'Failed', 'not a dictionary')


def readHDF5Attributes(filePath: str, dataset: Optional[str] = None):
    """(attributes, dataset name, datasets in the file) of an HDF5 recording."""
    import h5py
    from imswitch.imcommon.model import SharedAttributes
    with h5py.File(filePath, 'r') as file:
        datasets = list(file.keys())
        if dataset is None:
            if len(datasets) != 1:
                raise ValueError(
                    f'{os.path.basename(filePath)} holds {len(datasets)} datasets '
                    f'({", ".join(datasets)}); name the one to load.')
            dataset = datasets[0]
        return dict(SharedAttributes.fromHDF5File(file, dataset)), dataset, datasets


def readZarrAttributes(folderPath: str, dataset: Optional[str] = None):
    """(attributes, dataset name, datasets in the store) of a Zarr recording."""
    import zarr
    from imswitch.imcommon.model import SharedAttributes
    root = zarr.open(folderPath, mode='r')
    datasets = list(root.keys())
    if dataset is None:
        if len(datasets) != 1:
            raise ValueError(
                f'{os.path.basename(os.path.normpath(folderPath))} holds {len(datasets)} '
                f'datasets ({", ".join(datasets)}); name the one to load.')
        dataset = datasets[0]
    return dict(SharedAttributes.fromZarrStore(root, dataset)), dataset, datasets


class ReplayController(ImConWidgetController):
    """Loads a recording's settings into this setup, safely and traceably."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)
        self._controllers: Dict[str, Any] = {}
        self._setupName: Optional[str] = None
        self._setupHash: Optional[str] = self._computeSetupHash()

    # ------------------------------------------------------------------ setup

    def setControllers(self, controllers: Dict[str, Any]) -> None:
        """The panel controllers whose setters the apply layer calls."""
        self._controllers = dict(controllers or {})

    def setSetupName(self, name: Optional[str]) -> None:
        self._setupName = name

    @property
    def setupHash(self) -> Optional[str]:
        return self._setupHash

    def _computeSetupHash(self) -> Optional[str]:
        toDict = getattr(self._setupInfo, 'to_dict', None)
        if not callable(toDict):
            return None
        try:
            return replay.computeSetupHash(toDict())
        except Exception:  # noqa: BLE001 - identity is best effort
            self.__logger.warning('Could not compute the setup hash', exc_info=True)
            return None

    def _scanController(self):
        scan = self._controllers.get('Scan')
        if scan is not None and hasattr(scan, 'applyComponentState') \
                and hasattr(scan, '_analogParameterDict'):
            return scan
        return None

    def publishSetupMetadata(self) -> None:
        """Publish the setup identity recordings carry (schema 1).

        Only a hash of the setup file is recorded, never its content (it holds
        serial numbers and ports), plus the device inventory essentials."""
        attrs = self._commChannel.sharedAttrs
        try:
            inventory = replay_inventory.buildInventory(
                self._master, self._setupInfo, self._scanController())
        except Exception:  # noqa: BLE001 - recordings must still start
            self.__logger.warning('Could not build the device inventory', exc_info=True)
            inventory = None
        attrs[replay.METADATA_SCHEMA_KEY] = replay.METADATA_SCHEMA_VERSION
        attrs[replay.SETUP_NAME_KEY] = self._setupName or ''
        attrs[replay.SETUP_HASH_KEY] = self._setupHash or ''
        if inventory is not None:
            attrs[replay.SETUP_INVENTORY_KEY] = inventory
        self.publishPulseGeneratorState()

    def publishPulseGeneratorState(self) -> None:
        """Record the pulse generator's state (PulseGenerator:<name>:*).

        On this version the generator holds no user-set state of its own: the
        scan programs its sequence when it runs. What is recorded is its
        backend and whether it was connected; the channel map is in the
        inventory. Nothing listens for these keys."""
        pulse = getattr(self._master, 'pulseGeneratorManager', None)
        if pulse is None:
            return
        name = replay_inventory.pulseGeneratorName(self._setupInfo)
        attrs = self._commChannel.sharedAttrs
        try:
            attrs[(replay.PULSE_GENERATOR_CATEGORY, name, 'Backend')] = type(pulse).__name__
            attrs[(replay.PULSE_GENERATOR_CATEGORY, name, 'Connected')] = bool(
                getattr(pulse, 'connected', False))
        except Exception:  # noqa: BLE001
            self.__logger.debug('Could not publish the pulse generator state', exc_info=True)

    def snapshot(self) -> replay.SetupSnapshot:
        laserEnabled = []
        laser = self._controllers.get('Laser')
        if laser is not None:
            for name, _ in self._master.lasersManager:
                try:
                    if laser._widget.isLaserActive(name):
                        laserEnabled.append(name)
                except Exception:  # noqa: BLE001
                    pass
        return replay_inventory.buildSnapshot(
            self._master, self._setupInfo, setupName=self._setupName,
            setupHash=self._setupHash, currentAttrs=self._commChannel.sharedAttrs,
            scanController=self._scanController(), laserEnabled=laserEnabled)

    # ------------------------------------------------------------------ plan

    def parse(self, attrs, *, sourceFile: str, dataset: str,
              sourceFormat: str) -> replay.ParsedReplayFile:
        parsed = replay.parseReplayAttributes(attrs)
        parsed.sourceFile = os.path.basename(os.path.normpath(sourceFile))
        parsed.sourceDataset = dataset
        parsed.sourceFormat = sourceFormat
        return parsed

    def plan(self, parsed: replay.ParsedReplayFile, *, useMemory: bool = True) -> replay.ReplayPlan:
        snapshot = self.snapshot()
        remembered = self._rememberedTargets(parsed.setupHash) if useMemory else None
        return replay.resolveReplay(parsed, snapshot, remembered)

    # ------------------------------------------------------------ interactive

    def replayInteractive(self, parsed: replay.ParsedReplayFile) -> Optional[replay.ReplayResult]:
        """Show the dialog for ``parsed`` and apply what the user confirms.

        Returns the result, or None when cancelled.  Never raises: a failure is
        reported to the user instead of escaping into the menu's Qt slot."""
        try:
            plan = self.plan(parsed)
            canRemember = replay.mappingMemoryKey(parsed.setupHash, self._setupHash) is not None
            if not self._widget.askToApply(plan, canRemember=canRemember):
                self.__logger.info(f'Loading settings from {parsed.sourceFile} cancelled')
                return None
            if self._widget.rememberRequested():
                self._rememberTargets(plan)
            result = self.apply(plan, mode='interactive')
        except Exception as exc:  # noqa: BLE001 - never escape the menu slot
            self.__logger.error('Loading settings failed', exc_info=True)
            result = replay.ReplayResult(failed=[('load', '', str(exc))])
        try:
            self._widget.showResult(result)
        except Exception:  # noqa: BLE001
            self.__logger.error('Could not show the replay result', exc_info=True)
        return result

    # ------------------------------------------------------------ unattended

    @APIExport(runOnUIThread=True)
    def planReplayFromFile(self, filePath: str, dataset: Optional[str] = None) -> Dict[str, Any]:
        """ Describes what loading the settings of the recording ``filePath``
        (an HDF5 file, or a Zarr store folder) into this setup would do,
        without applying anything: every device the file names, whether this
        setup has it, the candidates that could replace it and each stored
        value checked against the current device's range. ``dataset`` names
        the detector group when the file holds several. """
        parsed = self._readFile(filePath, dataset)
        return replay.planToDict(self.plan(parsed, useMemory=False))

    @APIExport(runOnUIThread=True)
    def replaySettingsFromFile(self, filePath: str, dataset: Optional[str] = None,
                               allowMoves: bool = False) -> Dict[str, Any]:
        """ Loads the settings of the recording ``filePath`` (an HDF5 file or
        a Zarr store folder) into this setup without asking: only devices
        that match exactly and values inside the current device's range are
        applied; lasers are never switched on, and stages and rotators move
        only with ``allowMoves=True``. Returns ``{'plan': ..., 'result':
        {'applied', 'skipped', 'failed'}}``. ``dataset`` names the detector
        group when the file holds several. """
        parsed = self._readFile(filePath, dataset)
        plan = replay.applyUnattendedPolicy(self.plan(parsed, useMemory=False),
                                            allowMoves=bool(allowMoves))
        result = self.apply(plan, mode='unattended')
        return {'plan': replay.planToDict(plan), 'result': result.toDict()}

    def _readFile(self, filePath: str, dataset: Optional[str]) -> replay.ParsedReplayFile:
        if os.path.isdir(filePath):
            attrs, dataset, _ = readZarrAttributes(filePath, dataset)
            fmt = 'ZARR'
        else:
            attrs, dataset, _ = readHDF5Attributes(filePath, dataset)
            fmt = 'HDF5'
        return self.parse(attrs, sourceFile=filePath, dataset=dataset, sourceFormat=fmt)

    # ------------------------------------------------------------------ apply

    def apply(self, plan: replay.ReplayPlan, *, mode: str) -> replay.ReplayResult:
        """Apply ``plan`` as decided.  The only path from a recording to the
        hardware: every value goes through :func:`replay.buildActions` (the
        safety rules) and then through the owning panel's own setter."""
        actionSet = replay.buildActions(plan)
        result = replay.ReplayResult()
        for (itemId, key), reason in actionSet.skipped.items():
            result.skipped.append((itemId, key, reason))

        order = {kind: i for i, kind in enumerate(replay.APPLY_ORDER)}
        for action in sorted(actionSet.actions, key=lambda a: order.get(a.kind, 99)):
            try:
                note = self._applyOne(action, plan)
            except Exception as exc:  # noqa: BLE001 - recorded per value
                self.__logger.warning(
                    f'Replay of {action.itemId} {action.key} failed: {exc}', exc_info=True)
                result.failed.append((action.itemId, action.key, str(exc) or type(exc).__name__))
                continue
            notes = '; '.join(n for n in (action.note, note) if n)
            result.applied.append((action.itemId, action.key, action.value, notes))

        self._publishProvenance(plan, result, mode)
        self.__logger.info('Replay from %s: %s', plan.parsed.sourceFile,
                           result.summaryLines()[0])
        return result

    def _controller(self, key: str, what: str):
        controller = self._controllers.get(key)
        if controller is None:
            raise RuntimeError(f'this setup has no {what} panel')
        return controller

    def _applyOne(self, action: replay.ReplayAction, plan: replay.ReplayPlan) -> str:
        kind, target, key, value = action.kind, action.target, action.key, action.value
        if kind == replay.LASER:
            if key != 'Value':
                raise RuntimeError(f'laser attribute {key} is not replayed')
            manager = self._master.lasersManager[target]
            number = float(value)
            # The range of the value as the laser takes it now: 0-100 % under a
            # power calibration, its own range otherwise.
            lo, hi = laser_value_scale(manager)[:2]
            if lo is not None and hi is not None and not (float(lo) <= number <= float(hi)):
                raise RuntimeError(f'{number:g} is outside {lo} to {hi}; refused')
            laser = self._controller('Laser', 'Laser')
            if laser.setLaserValue(target, number) is False:
                raise RuntimeError('the laser refused the value')
            return ''
        if kind == replay.DETECTOR:
            settings = self._controller('Settings', 'Settings')
            if key == 'Binning':
                settings.setDetectorBinning(target, int(float(value)))
            elif key == 'ROI':
                x0, y0, w, h = (int(round(float(v))) for v in value)
                settings.setDetectorROI(target, (x0, y0), (w, h))
            elif key.startswith('Param:'):
                settings.setDetectorParameterExact(target, key[len('Param:'):], value)
            else:
                raise RuntimeError(f'detector attribute {key} is not replayed')
            return ''
        if kind == replay.SCAN:
            return self._applyScan(value)
        if kind == replay.ROTATOR:
            if not plan.allowMoves:  # buildActions already refuses; never move without opt-in
                raise RuntimeError('moves not enabled')
            self._controller('Rotator', 'Rotator').setRotatorPosition(target, float(value))
            return ''
        if kind == replay.POSITIONER:
            if not plan.allowMoves:
                raise RuntimeError('moves not enabled')
            positionerName, _, axis = target.partition(':')
            self._controller('Positioner', 'Positioner').setPositioner(
                positionerName, axis, float(value))
            return ''
        raise RuntimeError(f'{kind} is not replayed')

    def _applyScan(self, value: Dict[str, Dict[str, Any]]) -> str:
        """All scan parameters in one atomic component-state apply."""
        scan = self._scanController()
        if scan is None:
            raise RuntimeError('this setup has no scan panel that can take scan parameters')
        state = scan.getComponentState()
        analog = dict(state.get('analogParameterDict') or {})
        digital = dict(state.get('digitalParameterDict') or {})
        analog.update(value.get('analog') or {})
        digital.update(value.get('digital') or {})
        state['analogParameterDict'] = analog
        state['digitalParameterDict'] = digital
        if 'scan_dim_target_device' in (value.get('analog') or {}):
            state.pop('positionersScan', None)
        warnings = scan.applyComponentState(
            state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE) or []
        failures = [w for w in warnings if any(m in w for m in _FAILED_SCAN_MARKERS)]
        if failures:
            raise RuntimeError(' '.join(failures))
        return ' '.join(warnings)

    # ------------------------------------------------------------- provenance

    def _publishProvenance(self, plan: replay.ReplayPlan, result: replay.ReplayResult,
                           mode: str) -> None:
        if not result.applied:
            return  # nothing changed: the current settings did not come from the file
        try:
            entries = replay.buildProvenance(
                plan, result, appliedAt=datetime.now(timezone.utc).isoformat(), mode=mode)
            for key, value in entries.items():
                self._commChannel.sharedAttrs[key] = value
        except Exception:  # noqa: BLE001
            self.__logger.error('Could not publish the replay provenance', exc_info=True)

    # ---------------------------------------------------------- mapping memory

    @staticmethod
    def _mappingsPath() -> str:
        return os.path.join(dirtools.UserFileDirs.Root, REPLAY_MAPPINGS_FILENAME)

    def _loadMappings(self) -> Dict[str, Dict[str, Optional[str]]]:
        try:
            with open(self._mappingsPath(), encoding='utf-8') as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _rememberedTargets(self, sourceHash: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
        key = replay.mappingMemoryKey(sourceHash, self._setupHash)
        if key is None:
            return None
        remembered = self._loadMappings().get(key)
        return remembered if isinstance(remembered, dict) else None

    def _rememberTargets(self, plan: replay.ReplayPlan) -> None:
        key = replay.mappingMemoryKey(plan.parsed.setupHash, self._setupHash)
        if key is None:
            return
        data = self._loadMappings()
        data[key] = replay.rememberableTargets(plan)
        try:
            os.makedirs(os.path.dirname(self._mappingsPath()), exist_ok=True)
            with open(self._mappingsPath(), 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2, sort_keys=True)
        except OSError:
            self.__logger.warning('Could not save the replay device choices', exc_info=True)
