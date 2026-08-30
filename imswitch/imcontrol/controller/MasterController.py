from imswitch.imcommon.model import VFileItem, initLogger
from imswitch.imcontrol.model import (
    DetectorsManager, FlipMirrorsManager, LasersManager, MultiManager, NidaqManager, PositionersManager, RecordingManager, RS232sManager,
    ScanManagerPointScan, ScanManagerBase, ScanManagerMoNaLISA, ScanManagerTriggerScope, StandManager,
    RotatorsManager, SLMsManager, ScanManagerAdvanced
)
from imswitch.imcontrol.model.managers.TriggerScopeManager import TriggerScopeManager
from imswitch.imcontrol.model.managers._scan_execution import (
    getSharedScanExecutionCoordinator,
)


class MasterController:
    """
    This class will handle the communication between software and hardware,
    using the managers for each hardware set.
    """

    def __init__(self, setupInfo, commChannel, moduleCommChannel):
        self.__logger = initLogger(self)
        self.__setupInfo = setupInfo
        self.__commChannel = commChannel
        self.__moduleCommChannel = moduleCommChannel
        # Successful finalizers are terminal for one concrete manager object.
        # Retain the objects themselves (rather than integer ids) so a manager
        # replaced between shutdown retries is never skipped due to id reuse.
        self._shutdownFinalizedManagerObjects = []

        # Init managers
        self.nidaqManager = NidaqManager(self.__setupInfo)
        #self.pulseStreamerManager = PulseStreamerManager(self.__setupInfo)
        self.rs232sManager = RS232sManager(self.__setupInfo.rs232devices)

        # Pulse generator (Teensy / Arduino, v4 firmware).  Only built
        # when the setup actually declares one — leaving it out keeps
        # boot lean for setups without a Teensy.  The manager itself
        # falls back to an in-process mock when the port can't be opened,
        # so headless / CI runs work even with a real port configured.
        self.pulseGeneratorManager = None
        if getattr(self.__setupInfo, 'teensyPulse', None) is not None:
            from imswitch.imcontrol.model.managers.pulsegen import (
                TeensyPulseManager,
            )
            self.pulseGeneratorManager = TeensyPulseManager(
                self.__setupInfo.teensyPulse
            )

        lowLevelManagers = {
            'nidaqManager': self.nidaqManager,
            #'pulseStreamerManager' : self.pulseStreamerManager,
            'pulseGeneratorManager': self.pulseGeneratorManager,
            'rs232sManager': self.rs232sManager
        }

        if self.__setupInfo.triggerScope:
            self.triggerScopeManager = TriggerScopeManager(self.__setupInfo, self.rs232sManager)
            lowLevelManagers['triggerScopeManager'] = self.triggerScopeManager

        self.detectorsManager = DetectorsManager(self.__setupInfo.detectors, updatePeriod=300,
                                                 **lowLevelManagers)
        self.nidaqManager.setScanSimulationDetectorStateProvider(
            self.detectorsManager.isDetectorLeased
        )
        self.scanExecutionCoordinator = getSharedScanExecutionCoordinator(
            self.detectorsManager,
            self.nidaqManager,
            logger=self.__logger,
        )

        self.lasersManager = LasersManager(self.__setupInfo.lasers,
                                           **lowLevelManagers)
        self.positionersManager = PositionersManager(self.__setupInfo.positioners,
                                                     **lowLevelManagers)
        self.rotatorsManager = RotatorsManager(self.__setupInfo.rotators,
                                               **lowLevelManagers)
        self.flipMirrorsManager = FlipMirrorsManager(
            self.__setupInfo.flipMirrors,
            **lowLevelManagers
        )

        self.recordingManager = RecordingManager(self.detectorsManager)

        self.slmsManager = SLMsManager(self.__setupInfo.slms)

        if self.__setupInfo.microscopeStand:
            self.standManager = StandManager(self.__setupInfo.microscopeStand,
                                             **lowLevelManagers)

        # Generate scanManager type according to setupInfo
        if self.__setupInfo.scan:
            if self.__setupInfo.scan.scanWidgetType == "PointScan":
                self.scanManager = ScanManagerPointScan(self.__setupInfo)
            elif self.__setupInfo.scan.scanWidgetType == "Base":
                self.scanManager = ScanManagerBase(self.__setupInfo)
            elif self.__setupInfo.scan.scanWidgetType == "MoNaLISA":
                self.scanManager = ScanManagerMoNaLISA(self.__setupInfo)
            elif self.__setupInfo.scan.scanWidgetType == "Advanced":
                self.scanManager = ScanManagerAdvanced(self.__setupInfo)
            elif self.__setupInfo.scan.scanWidgetType == "TriggerScope":
                self.scanManager = ScanManagerTriggerScope(self.__setupInfo,
                                                           self.triggerScopeManager)
            else:
                self.__logger.error(
                    'ScanWidgetType in SetupInfo["scan"] not recognized, choose one of the following:'
                    ' ["Base", "PointScan", "MoNaLISA", "Advanced", "TriggerScope"].'
                )
                return

        # Read-only device inventory/status facade. It observes the managers
        # created above and deliberately performs no probing or lifecycle work.
        from imswitch.imcontrol.model.devices import DeviceSupervisor
        self.deviceSupervisor = DeviceSupervisor(self)

        # Connect signals
        cc = self.__commChannel

        self.detectorsManager.sigAcquisitionStarted.connect(cc.sigAcquisitionStarted)
        self.detectorsManager.sigAcquisitionStopped.connect(cc.sigAcquisitionStopped)
        self.detectorsManager.sigDetectorSwitched.connect(cc.sigDetectorSwitched)
        self.detectorsManager.sigImageUpdated.connect(cc.sigUpdateImage)
        self.detectorsManager.sigNewFrame.connect(cc.sigNewFrame)

        self.recordingManager.sigRecordingStarted.connect(cc.sigRecordingStarted)
        self.recordingManager.sigRecordingEnded.connect(cc.sigRecordingEnded)
        self.recordingManager.sigRecordingFailed.connect(cc.sigRecordingFailed)
        self.recordingManager.sigRecordingFrameNumUpdated.connect(cc.sigUpdateRecFrameNum)
        self.recordingManager.sigRecordingTimeUpdated.connect(cc.sigUpdateRecTime)
        self.recordingManager.sigMemorySnapAvailable.connect(cc.sigMemorySnapAvailable)
        self.recordingManager.sigMemoryRecordingAvailable.connect(self.memoryRecordingAvailable)

    def memoryRecordingAvailable(self, name, file, filePath, savedToDisk):
        self.__moduleCommChannel.memoryRecordings[name] = VFileItem(
            data=file, filePath=filePath, savedToDisk=savedToDisk
        )

    def _managerFinalizationCompleted(self, manager):
        completed = self.__dict__.setdefault(
            '_shutdownFinalizedManagerObjects', []
        )
        return any(candidate is manager for candidate in completed)

    def _markManagerFinalizationCompleted(self, manager):
        completed = self.__dict__.setdefault(
            '_shutdownFinalizedManagerObjects', []
        )
        if not any(candidate is manager for candidate in completed):
            completed.append(manager)

    def closeEvent(self):
        # Finalize all manager attributes explicitly, not only MultiManager
        # instances. This ordering is also the retry scope: a manager object
        # that already finalized successfully is skipped, while False/raising
        # finalizers remain pending on the next close attempt.
        manager_attrs = [
            'detectorsManager', 'lasersManager', 'positionersManager',
            'rotatorsManager', 'flipMirrorsManager', 'recordingManager',
            'slmsManager', 'nidaqManager', 'rs232sManager',
            'pulseGeneratorManager', 'triggerScopeManager', 'standManager',
            'scanManager',
        ]
        unsafeWhileActive = {
            'detectorsManager',
            'recordingManager',
            'nidaqManager',
            'scanManager',
        }
        pendingUnsafeManagers = {
            attrName
            for attrName in unsafeWhileActive
            if (
                (manager := getattr(self, attrName, None)) is not None
                and not self._managerFinalizationCompleted(manager)
            )
        }

        hardwareFinalizationSafe = True
        recordingManager = getattr(self, 'recordingManager', None)
        if (
            recordingManager is not None
            and 'recordingManager' in pendingUnsafeManagers
        ):
            try:
                recordingManager.endRecording(
                    emitSignal=False, wait=True
                )
            except Exception as e:
                # Recording teardown failure must not bypass cleanup of every
                # other hardware manager. Continue through the independently
                # guarded finalization loop below.
                self.__logger.error(
                    f'Error stopping active recording during shutdown: {e}',
                    exc_info=True,
                )
                hardwareFinalizationSafe = False

            recordingShutdownComplete = getattr(
                recordingManager, 'shutdownComplete', None
            )
            if callable(recordingShutdownComplete):
                try:
                    if recordingShutdownComplete() is False:
                        hardwareFinalizationSafe = False
                        self.__logger.error(
                            'Skipping acquisition-hardware finalization because '
                            'the recording producer/writer is still active.'
                        )
                except Exception:
                    hardwareFinalizationSafe = False
                    self.__logger.error(
                        'Could not verify recording-worker shutdown.',
                        exc_info=True,
                    )

        coordinator = getattr(self, 'scanExecutionCoordinator', None)
        if pendingUnsafeManagers and coordinator is not None and (
            getattr(coordinator, 'activeToken', None) is not None
            or getattr(coordinator, 'activeRunToken', None) is not None
        ):
            hardwareFinalizationSafe = False
            self.__logger.error(
                'Skipping acquisition-hardware finalization because a scan '
                'iteration/run still owns the shared coordinator.'
            )

        activeLeases = ()
        detectorsManager = getattr(self, 'detectorsManager', None)
        activeLeasesSnapshot = (
            getattr(detectorsManager, 'activeAcquisitionLeases', None)
            if 'detectorsManager' in pendingUnsafeManagers else None
        )
        if callable(activeLeasesSnapshot):
            try:
                candidate = activeLeasesSnapshot()
                if isinstance(candidate, (tuple, list)):
                    activeLeases = tuple(candidate)
            except Exception:
                self.__logger.error(
                    'Could not verify detector leases during shutdown.',
                    exc_info=True,
                )
                hardwareFinalizationSafe = False
        if activeLeases:
            hardwareFinalizationSafe = False
            self.__logger.error(
                'Skipping acquisition-hardware finalization because detector '
                f'leases remain active: {activeLeases!r}'
            )

        faultSnapshot = getattr(
            detectorsManager, 'faultedAcquisitionDetectors', None
        ) if 'detectorsManager' in pendingUnsafeManagers else None
        if hardwareFinalizationSafe and callable(faultSnapshot):
            try:
                candidate = faultSnapshot()
                faultedDetectors = (
                    tuple(candidate)
                    if isinstance(candidate, (tuple, list))
                    else ()
                )
            except Exception:
                faultedDetectors = ()
                hardwareFinalizationSafe = False
                self.__logger.error(
                    'Could not verify faulted detectors during shutdown.',
                    exc_info=True,
                )

            retryStop = getattr(
                detectorsManager, 'retryStop', None
            )
            for detectorName in faultedDetectors:
                try:
                    if not callable(retryStop):
                        raise RuntimeError(
                            'detector manager has no retryStop recovery API'
                        )
                    retryStop(detectorName)
                except Exception:
                    hardwareFinalizationSafe = False
                    self.__logger.error(
                        'Failed the bounded shutdown stop retry for detector '
                        f'{detectorName!r}.',
                        exc_info=True,
                    )

            try:
                candidate = faultSnapshot()
                unresolvedFaults = (
                    tuple(candidate)
                    if isinstance(candidate, (tuple, list))
                    else ()
                )
            except Exception:
                unresolvedFaults = ()
                hardwareFinalizationSafe = False
                self.__logger.error(
                    'Could not re-check detector faults after shutdown '
                    'recovery.',
                    exc_info=True,
                )
            if unresolvedFaults:
                hardwareFinalizationSafe = False
                self.__logger.error(
                    'Skipping acquisition-hardware finalization because '
                    'detector stop faults remain unresolved: '
                    f'{unresolvedFaults!r}'
                )

        shutdownSucceeded = hardwareFinalizationSafe
        for attrName in manager_attrs:
            if (
                not hardwareFinalizationSafe
                and attrName in unsafeWhileActive
            ):
                continue
            if not hasattr(self, attrName):
                continue
            attr = getattr(self, attrName)
            if attr is None:
                continue
            if self._managerFinalizationCompleted(attr):
                continue

            # Try finalize() first, falling back to close() only when a
            # finalize method is not available. An explicit False result is a
            # shutdown failure, not a successful no-op.
            finalizerFound = False
            for method_name in ['finalize', 'close']:
                method = getattr(attr, method_name, None)
                if not callable(method):
                    continue
                finalizerFound = True
                try:
                    result = method()
                except Exception as e:
                    shutdownSucceeded = False
                    if attrName in unsafeWhileActive:
                        hardwareFinalizationSafe = False
                    self.__logger.error(
                        f'Error finalizing {attrName}.{method_name}(): {e}',
                        exc_info=True,
                    )
                else:
                    if result is False:
                        shutdownSucceeded = False
                        if attrName in unsafeWhileActive:
                            hardwareFinalizationSafe = False
                        self.__logger.error(
                            f'{attrName}.{method_name}() reported an '
                            'incomplete shutdown.'
                        )
                    else:
                        self._markManagerFinalizationCompleted(attr)
                        self.__logger.debug(
                            f'Finalized {attrName} via {method_name}()'
                        )
                break
            if not finalizerFound:
                # This manager has no explicit finalizer. Reaching it after all
                # safety gates is a successful no-op for this concrete object,
                # and repeating pre-finalization teardown on later retries is
                # unnecessary.
                self._markManagerFinalizationCompleted(attr)
        return hardwareFinalizationSafe and shutdownSucceeded


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
