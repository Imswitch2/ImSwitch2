"""Shared lifecycle for autonomous TriggerScope firmware scans.

TriggerScope controllers differ in their parameter widgets, but not in how a
scan owns detectors, lasers or the global scan lifecycle. Keeping those rules
here prevents a new firmware scan mode from silently falling back to the old
``sigScanBuilt``-only behavior.
"""

import threading

from qtpy import QtCore

from ..basecontrollers import ScanLifecycleMixin
from imswitch.imcontrol.model.managers.NidaqManager import ScanBusyError
from imswitch.imcontrol.model.managers._scan_execution import (
    FINISH_ABORT,
    FINISH_GRACEFUL,
    ScanExecutionCoordinator,
    getSharedScanExecutionCoordinator,
)


class TriggerScopeScanLifecycleMixin(ScanLifecycleMixin):
    """Coordinate one TriggerScope scan run across firmware and detectors.

    Inheriting controllers keep ownership of parameter construction and widget
    details. They call :meth:`_startTriggerScopeScan` with the resolved laser
    devices, firmware parameters and scan type.
    """

    def _initTriggerScopeScanLifecycle(self):
        coordinator = getattr(
            self._master, 'scanExecutionCoordinator', None
        )
        if not isinstance(coordinator, ScanExecutionCoordinator):
            coordinator = getSharedScanExecutionCoordinator(
                self._master.detectorsManager,
                self._master.nidaqManager,
                logger=self._logger,
            )
        self._scanCoordinator = coordinator.configure(
            logger=self._logger,
            scheduleTimeout=lambda delayS, callback: QtCore.QTimer.singleShot(
                int(delayS * 1000), callback
            ),
        )
        self._triggerScopeRunToken = None
        self._triggerScopeStartingPublished = False
        self._triggerScopeRepeatPending = False
        self._triggerScopeCompletionPublishing = False
        self._triggerScopeTerminalLock = threading.RLock()
        # Exact request completions (plan A-05): set while an external
        # request that asked for one is being dispatched, bound after the run
        # is reserved, resolved from this run's terminal.
        self._externalTriggerScopeCompletion = None
        self._triggerScopeBoundCompletions = []
        self._triggerScopeRunOutcome = None
        self._scanStopRequested = False

        self._master.scanManager.sigScanStarted.connect(
            self._onTriggerScopeScanStarted
        )
        self._master.scanManager.sigScanDone.connect(
            self._onTriggerScopeScanDone
        )

    def _startTriggerScopeScan(
        self,
        *,
        parameters,
        scanType,
        laserDevices,
        sigScanStartingEmitted,
        isNonFinalPartOfSequence=False,
    ):
        """Reserve, announce, arm and start one firmware iteration."""
        localRun = self._triggerScopeRunToken
        activeRun = self._scanCoordinator.runForOwner(self)
        activeIteration = self._scanCoordinator.tokenForOwner(self)

        if (
            localRun is not None
            and activeRun is localRun
            and (
                self._scanStopRequested
                or activeRun.releaseRequested
            )
        ):
            reason = (
                'Ignoring a TriggerScope continuation while the current run '
                'is stopped or still releasing.'
            )
            self._logger.debug(reason)
            self.isRunning = activeIteration is not None
            self._setTriggerScopeScanButtonChecked(self.isRunning)
            ScanLifecycleMixin._recordScanStartRejection(self, reason)
            return False
        if localRun is not None and activeRun is localRun:
            if activeIteration is not None or not sigScanStartingEmitted:
                reason = (
                    'Ignoring duplicate TriggerScope scan start from the '
                    'active owner.'
                )
                self._logger.warning(reason)
                self.isRunning = activeIteration is not None
                self._setTriggerScopeScanButtonChecked(self.isRunning)
                ScanLifecycleMixin._recordScanStartRejection(self, reason)
                return False
        elif localRun is not None:
            # A released token retained by a stale UI callback is never reused.
            self._triggerScopeRunToken = None
            self._triggerScopeStartingPublished = False

        isNewRun = activeRun is None
        try:
            runToken = self._scanCoordinator.reserveRun(self)
        except ScanBusyError as error:
            # A broadcast start is intentionally won by exactly one scan
            # controller. The other candidates were refused before touching
            # hardware, so this is contention rather than a scan failure.
            reason = (
                'TriggerScope scan start was refused because another scan '
                f'run owns the coordinator: {error}'
            )
            self._logger.debug(reason)
            self.isRunning = False
            self._setTriggerScopeScanButtonChecked(False)
            ScanLifecycleMixin._recordScanStartRejection(self, reason)
            return False
        self._triggerScopeRunToken = runToken
        completion = getattr(self, '_externalTriggerScopeCompletion', None)
        if completion is not None:
            completion.bind(runToken)
            self._triggerScopeBoundCompletions.append(completion)
        if isNewRun:
            self._scanStopRequested = False

        # A recording armed for "whichever scan you start next" binds to this
        # controller here, while nothing has been announced and no hardware has
        # moved. It can refuse, and a refusal must stop the scan: running one
        # anyway would bleach the sample with nothing recording it.
        if not self._prepareRecordingForTriggerScopeScan():
            self._logger.error(
                'The armed recording could not be prepared for this scan; '
                'the scan was not started.'
            )
            self._abandonTriggerScopeRun(runToken)
            return False

        # The active source is announced only after this controller owns the
        # global run. A losing controller must not replace the real owner.
        self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
        self.isRunning = True
        if sigScanStartingEmitted:
            self._triggerScopeStartingPublished = True
        elif not self._triggerScopeStartingPublished:
            self._triggerScopeStartingPublished = True
            self.emitScanSignal(self._commChannel.sigScanStarting)

        if isNewRun:
            publishActuators = getattr(self, '_publishScanActuators', None)
            if callable(publishActuators):
                publishActuators()

        if (
            isNewRun
            and not sigScanStartingEmitted
            and self._triggerScopeWidgetFlag('autoStartRec')
        ):
            self._commChannel.sigStartRecording.emit()

        devices = list(dict.fromkeys(laserDevices or ()))
        # Membership must be published before detector acquisition and before
        # the firmware command. LaserController hands each selected gate to the
        # firmware and locks its on/off control in this slot.
        self.emitScanSignal(
            self._commChannel.sigScanDevicesResolved, devices
        )
        # Preserve the public notification for plugins that observe scan build
        # boundaries. It is no longer the laser-arming authority.
        self.emitScanSignal(self._commChannel.sigScanBuilt, devices)

        def startFirmware():
            started = self._master.scanManager.runScan(
                parameters, scan_type=scanType
            )
            if started is False:
                raise RuntimeError(
                    f'TriggerScope refused scan type "{scanType}".'
                )

        try:
            self._scanCoordinator.armWithStarter(
                startFirmware,
                owner=self,
            )
        except ScanBusyError as error:
            self._logger.debug(
                'TriggerScope scan iteration was refused because the '
                'coordinator is busy: %s',
                error,
            )
            activeIteration = self._scanCoordinator.tokenForOwner(self)
            self.isRunning = activeIteration is not None
            self._setTriggerScopeScanButtonChecked(self.isRunning)
            if (
                activeIteration is None
                and self._scanCoordinator.runForOwner(self) is runToken
                and not runToken.releaseRequested
            ):
                self._triggerScopeRunOutcome = (
                    False, f'TriggerScope refused the scan iteration: {error}'
                )
                self._releaseTriggerScopeRun(runToken)
            return False
        return True

    def _prepareRecordingForTriggerScopeScan(self) -> bool:
        """Ask an armed recording to bind to this scan. False vetoes the start.

        Deliberately synchronous and placed before ``isRunning``,
        ``sigScanStarting`` and every hardware write: a recording that learns
        about the scan from an announcement signal can no longer stop it, and
        the firmware would already be uploading parameters.
        """
        prepare = getattr(
            getattr(self._commChannel, 'scanWorkflow', None),
            'prepare_recording_for_scan',
            None,
        )
        if not callable(prepare):
            return True
        try:
            return bool(prepare(self))
        except Exception:
            self._logger.error(
                'Failed to prepare an armed recording for this TriggerScope '
                'scan; refusing the start',
                exc_info=True,
            )
            return False

    def _abandonTriggerScopeRun(self, runToken):
        """Give a reservation back before anything about it was published.

        Not a terminal: no ``sigScanStarting`` was emitted for this attempt and
        no hardware moved, so publishing an end boundary here would disarm
        lasers on behalf of a scan that never existed. A continuation that has
        already published its start is terminalized properly instead.
        """
        self._triggerScopeRepeatPending = False
        if self._triggerScopeStartingPublished:
            self._triggerScopeRunOutcome = (
                False, self._lastScanStartRejection or 'The scan was not started.'
            )
            self._releaseTriggerScopeRun(runToken)
        else:
            with self._triggerScopeTerminalLock:
                if self._triggerScopeRunToken is runToken:
                    self._triggerScopeRunToken = None
            self._scanCoordinator.releaseRun(runToken)
            self._resolveTriggerScopeCompletions(
                runToken, False,
                self._lastScanStartRejection or 'The scan was not started.',
            )
        self.isRunning = False
        self._scanStopRequested = False
        self.doingNonFinalPartOfSequence = False
        self._setTriggerScopeScanButtonChecked(False)
        self._setTriggerScopeAbortPending(False)

    def _onTriggerScopeScanStarted(self):
        """Relay the board boundary only from the controller that owns it."""
        if (
            self.isRunning
            and self._scanCoordinator.tokenForOwner(self) is not None
        ):
            self.emitScanSignal(self._commChannel.sigScanStarted)

    def _onTriggerScopeScanDone(self):
        """Resolve detector work before publishing firmware completion."""
        token = self._scanCoordinator.tokenForOwner(self)
        if token is None:
            return
        runToken = self._scanCoordinator.runForOwner(self)
        # TriggerScope reports this boundary only after its autonomous
        # iteration has physically completed. Preserve the final detector read
        # even when a stop is pending; the stop suppresses continuation.
        self._scanCoordinator.resolve(
            token,
            FINISH_GRACEFUL,
            onComplete=lambda: self._deliverOnControllerThread(
                lambda: self._completeTriggerScopeIteration(runToken)
            ),
        )

    def _completeTriggerScopeIteration(self, runToken):
        if (
            runToken is None
            or self._triggerScopeRunToken is not runToken
            or self._scanCoordinator.runForOwner(self) is not runToken
        ):
            return

        stopRequested = self._scanStopRequested
        shouldRepeat = (
            not stopRequested and self._triggerScopeRepeatRequested()
        )
        isFinalPart = not self.doingNonFinalPartOfSequence

        self._triggerScopeCompletionPublishing = True
        self.isRunning = False
        self._setTriggerScopeAbortPending(False)
        try:
            if shouldRepeat:
                self._triggerScopeRepeatPending = True
                QtCore.QTimer.singleShot(
                    0, self._fireTriggerScopeRepeat
                )
                return

            self.emitScanSignal(self._commChannel.sigScanDone)
            terminalRequested = (
                isFinalPart
                or self._scanStopRequested
            )
            if terminalRequested:
                self._scanStopRequested = False
                self.doingNonFinalPartOfSequence = False
                self._setTriggerScopeScanButtonChecked(False)
                self._triggerScopeRunOutcome = (
                    not stopRequested,
                    'Scan was aborted.' if stopRequested else '',
                )
                self._releaseTriggerScopeRun(runToken)
        finally:
            self._triggerScopeCompletionPublishing = False
            if self._triggerScopeRunToken is None:
                self._scanStopRequested = False

    def _fireTriggerScopeRepeat(self):
        if not self._triggerScopeRepeatPending:
            return
        self._triggerScopeRepeatPending = False
        runToken = self._triggerScopeRunToken
        if (
            runToken is None
            or self._scanCoordinator.runForOwner(self) is not runToken
            or self._scanStopRequested
        ):
            if runToken is not None:
                self._scanStopRequested = False
                self.doingNonFinalPartOfSequence = False
                self._setTriggerScopeScanButtonChecked(False)
                self._triggerScopeRunOutcome = (False, 'Scan was aborted.')
                self._releaseTriggerScopeRun(runToken)
            return
        if not self._triggerScopeRepeatRequested():
            # The user can untick Repeat after the firmware completion signal
            # but before this deferred continuation runs.
            self._triggerScopeCompletionPublishing = True
            try:
                self.emitScanSignal(self._commChannel.sigScanDone)
                if (
                    self._scanStopRequested
                    or not self.doingNonFinalPartOfSequence
                ):
                    aborted = bool(self._scanStopRequested)
                    self._scanStopRequested = False
                    self.doingNonFinalPartOfSequence = False
                    self._setTriggerScopeScanButtonChecked(False)
                    self._triggerScopeRunOutcome = (
                        not aborted, 'Scan was aborted.' if aborted else ''
                    )
                    self._releaseTriggerScopeRun(runToken)
            finally:
                self._triggerScopeCompletionPublishing = False
                if self._triggerScopeRunToken is None:
                    self._scanStopRequested = False
            return
        self.runScanAdvanced(sigScanStartingEmitted=True)

    def _triggerScopeRepeatRequested(self):
        repeatEnabled = getattr(self._widget, 'repeatEnabled', None)
        if not callable(repeatEnabled):
            return False
        try:
            return bool(repeatEnabled())
        except Exception:
            self._logger.error(
                'Could not read the TriggerScope repeat state',
                exc_info=True,
            )
            return False

    def _requestTriggerScopeStop(self):
        """Stop continuation; firmware already in flight runs to completion."""
        self.doingNonFinalPartOfSequence = False
        self._triggerScopeRepeatPending = False
        if self._scanStopRequested:
            self._logger.debug('A TriggerScope stop is already pending')
            return
        self._scanStopRequested = True
        if self._triggerScopeCompletionPublishing:
            # The completion publisher re-checks this flag after sigScanDone,
            # so a recording teardown can terminalize a retained sequence run.
            return
        if not self.isRunning:
            runToken = self._triggerScopeRunToken
            if runToken is not None:
                self._failTriggerScopeScan()
            else:
                self._scanStopRequested = False
                self._setTriggerScopeScanButtonChecked(False)
                self._setTriggerScopeAbortPending(False)
            return
        self._setTriggerScopeAbortPending(True)
        self._logger.debug(
            'Stop requested while TriggerScope firmware is scanning: the '
            'current iteration will finish and no continuation will follow.'
        )

    def _failTriggerScopeScan(self):
        """Resolve an arm failure or explicit local force-stop exactly once."""
        with self._triggerScopeTerminalLock:
            runToken = self._triggerScopeRunToken
            if runToken is None:
                self.isRunning = False
                self._setTriggerScopeScanButtonChecked(False)
                self._setTriggerScopeAbortPending(False)
                return False
            self._triggerScopeRepeatPending = False
            self._scanStopRequested = True
            self.doingNonFinalPartOfSequence = False
            self.isRunning = False
            self._setTriggerScopeScanButtonChecked(False)
            self._setTriggerScopeAbortPending(False)

            token = self._scanCoordinator.tokenForOwner(self)
            if token is not None:
                self._scanCoordinator.resolve(token, FINISH_ABORT)
            self._scanStopRequested = False
            self._triggerScopeRunOutcome = (False, 'Scan failed or was stopped.')
            self._releaseTriggerScopeRun(runToken)
            return True

    def _releaseTriggerScopeRun(self, runToken):
        """Release one exact run, then publish its terminal boundary."""
        with self._triggerScopeTerminalLock:
            if self._triggerScopeRunToken is not runToken:
                return False
            startingPublished = self._triggerScopeStartingPublished
            self._triggerScopeRunToken = None
            self._triggerScopeStartingPublished = False

        callbackLock = threading.Lock()
        callbackDelivered = False
        outcome = self._triggerScopeRunOutcome or (True, '')
        self._triggerScopeRunOutcome = None

        def publishEnded():
            nonlocal callbackDelivered
            with callbackLock:
                if callbackDelivered:
                    return
                callbackDelivered = True
            try:
                if startingPublished:
                    self.emitScanSignal(self._commChannel.sigScanEnded)
            except Exception:
                self._logger.error(
                    'Failed to publish the TriggerScope scan-run end signal',
                    exc_info=True,
                )
            self._resolveTriggerScopeCompletions(runToken, *outcome)
            try:
                self._triggerScopeAutoStopRecording()
            except Exception:
                self._logger.error(
                    'Failed to publish TriggerScope auto-stop recording',
                    exc_info=True,
                )
            finally:
                self._scanCoordinator.finalizeRunRelease(runToken)

        accepted = self._scanCoordinator.releaseRun(
            runToken,
            onReleased=lambda: self._deliverOnControllerThread(publishEnded),
            holdUntilFinalized=True,
        )
        if not accepted:
            # A stale callback must not publish an end boundary for a newer
            # run. If this exact token is already gone, its terminal was
            # delivered elsewhere.
            return False
        return True

    def _resolveTriggerScopeCompletions(self, runToken, successful, message=''):
        """Resolve the exact request completions bound to ``runToken``."""
        bound = getattr(self, '_triggerScopeBoundCompletions', None) or []
        matched = [c for c in bound if getattr(c, 'runToken', None) is runToken]
        for completion in matched:
            bound.remove(completion)
            try:
                completion.resolve(runToken, bool(successful), message)
            except Exception:
                self._logger.error(
                    'Failed to resolve a TriggerScope scan request completion',
                    exc_info=True,
                )

    def _runTriggerScopeScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        """Shared ``runScanExternal`` for the TriggerScope family.

        Arms exactly as before (Repeat off, run-level start already
        published by the dispatcher). When the active request asked for an
        exact completion (the script/remote API does; scan-mode Recording
        does not, and keeps its legacy global-signal path), this reports
        acceptance or the recorded refusal to the workflow service and hands
        it a completion that this run's terminal resolves."""
        workflow = getattr(self._commChannel, 'scanWorkflow', None)
        wantsExact = getattr(workflow, 'active_request_wants_exact_completion', None)
        wantsExact = bool(wantsExact()) if callable(wantsExact) else False
        self._lastScanStartRejection = None
        completion = None
        if wantsExact:
            from ..WorkflowServices import ScanRequestCompletion
            completion = ScanRequestCompletion(self)
        self._externalTriggerScopeCompletion = completion
        try:
            self._widget.setRepeatEnabled(False)
            self.runScanAdvanced(
                recalculateSignals=recalculateSignals,
                isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                sigScanStartingEmitted=True,
            )
        finally:
            self._externalTriggerScopeCompletion = None
            if completion is not None:
                accepted = completion.runToken is not None
                message = '' if accepted else (
                    self._lastScanStartRejection
                    or 'TriggerScope scan controller refused the request or '
                       'failed to arm.'
                )
                report = getattr(workflow, 'report_scan_request_result', None)
                if callable(report):
                    try:
                        report(
                            self, accepted, message,
                            completion.runToken if accepted else None,
                            completion if accepted else None,
                        )
                    except Exception:
                        self._logger.error(
                            'Failed to report the TriggerScope scan request result',
                            exc_info=True,
                        )
                if not accepted and completion in self._triggerScopeBoundCompletions:
                    self._triggerScopeBoundCompletions.remove(completion)

    def _deliverOnControllerThread(self, callback):
        try:
            self._invokeOnControllerThreadIfNeeded(callback)
            return
        except Exception:
            self._logger.error(
                'Failed to hand TriggerScope lifecycle work to the '
                'controller thread; using the fail-safe path',
                exc_info=True,
            )
        callback()

    def _triggerScopeWidgetFlag(self, name):
        value = getattr(self._widget, name, False)
        isChecked = getattr(value, 'isChecked', None)
        if callable(isChecked):
            try:
                return bool(isChecked())
            except Exception:
                return False
        return bool(value)

    def _triggerScopeAutoStopRecording(self):
        if self._triggerScopeWidgetFlag('autoStopRec'):
            self._commChannel.sigStopRecording.emit()

    def _setTriggerScopeScanButtonChecked(self, checked):
        setter = getattr(self._widget, 'setScanButtonChecked', None)
        if callable(setter):
            setter(checked)

    def _setTriggerScopeAbortPending(self, pending):
        setter = getattr(self._widget, 'setAbortPending', None)
        if callable(setter):
            setter(pending)
