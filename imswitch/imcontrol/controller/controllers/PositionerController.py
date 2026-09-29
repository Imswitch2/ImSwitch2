import time
from typing import Dict, List, Any

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import getWidgetStatePersistence
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from imswitch.imcommon.model import initLogger
from qtpy.QtCore import QTimer

class PositionerController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to PositionerWidget."""

    componentName = 'Positioner'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._liveUpdateIntervalMs = 300
        self._liveUpdateTimer = QTimer()
        self._liveUpdateTimer.setInterval(self._liveUpdateIntervalMs)
        self._liveUpdateTimer.timeout.connect(self._refreshLiveUpdatedPositioners)

        self.settingAttr = False
        self._previousJoystickState = None
        self._liveUpdateAvailable = {}
        self._liveUpdateEnabled = {}
        self._liveUpdateFailureCount = {}
        self._liveUpdateRetryAfter = {}
        self._pollBackoffSeconds = (1.0, 2.0, 5.0, 10.0)
        self._coarseStepMultiplier = 5.0
        self._isCoarseMode = False
        self._joystickAutoReenable = True
        self._joystickAutoReenableDelayS = 5.0
        self._joystickAutoReenableTimers = {}
        self._joystickAutoReenablePendingAxes = {}
        self._joystickAutoReenableFailureCount = {}
        self._joystickAutoReenablePollIntervalMs = 200
        self._referenceBatchPlan = []
        self._referenceBatchIndex = 0
        self._referenceBatchRunning = False
        self._referenceBatchAbortRequested = False

        self.__logger = initLogger(self, tryInheritParent=True)

        # Set up positioners
        for pName, pManager in self._master.positionersManager:
            if not self._isPositionerShownInWidget(pManager):
                continue

            self._liveUpdateAvailable[pName] = bool(getattr(pManager, 'liveUpdate', False))
            self._liveUpdateEnabled[pName] = self._liveUpdateAvailable[pName]

            if pManager.joystick:
                self._widget.addJoystick(pName)

            speed = hasattr(pManager, 'speed')
            self._widget.addPositioner(
                pName,
                pManager.axes,
                speed,
                pManager.joystick,
                shortcutModifier=pManager.shortcutModifier,
                unit=getattr(pManager, 'positionUnit', 'µm')
            )
            for axis in pManager.axes:
                self.setSharedAttr(pName, axis, _positionAttr, pManager.position[axis])
                if speed:
                    self.setSharedAttr(pName, axis, _positionAttr, pManager.speed)

            # Do one best-effort hardware refresh per positioner. Some managers
            # (notably PIStageManager) refresh all axes in one transaction, so
            # refreshing once per axis duplicates hardware traffic at startup.
            self._pollPositionerPosition(pName, pManager, respectBackoff=False)

            if pManager.joystick:
                # Set joystick checkbox status for first start
                self.setJoystickCheckStatus(self._master.positionersManager[pName].joystickStatus)
                # Connect channels
                self._commChannel.sigRecordingStarted.connect(
                    lambda pName=pName: self.setJoystickStatusForRec(False, pName)
                )
                self._commChannel.sigRecordingEnded.connect(
                    lambda pName=pName: self.setJoystickStatusAfterRec(pName)
                )
                # self._commChannel.sigInitiateEtMonalisa.connect(lambda state, pName=pName: self.setJoystickStatus(not state, pName))

                if hasattr(pManager, "sigJoystickStatusChanged"):
                    pManager.sigJoystickStatusChanged.connect(
                        lambda enabled, pName=pName: self._onManagerJoystickStatusChanged(pName, enabled)
                    )
        
        self._widget.sigJoystickToggled.connect(self.requestJoystickStatus)
        self._widget.sigSettingsClicked.connect(self.openSettingsDialog)
        self._widget.sigSettingsChanged.connect(self.applySettings)
        self._widget.sigStepModeChanged.connect(self.setStepMode)
        self._widget.sigReferenceClicked.connect(self.openReferenceDialog)
        self._widget.sigReferenceAxisClicked.connect(self._referencePositionerFromWidget)
        self._widget.sigReferenceAllClicked.connect(self.referenceAllPositioners)
        self._widget.sigAbortReferenceClicked.connect(self.abortReferenceBatch)
        self._widget.setCoarseStepMultiplier(self._coarseStepMultiplier)
        self._widget.setStepMode(self._isCoarseMode)
        self._refreshReferenceStatus()
        self._updateLiveTimerState()
        self._refreshLiveUpdatedPositioners()

        # Connect CommunicationChannel signals
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigSetSpeed.connect(lambda speed: self.setSpeedGUI(speed))


        # Connect PositionerWidget signals
        self._widget.sigStepUpClicked.connect(self.stepUp)
        self._widget.sigStepDownClicked.connect(self.stepDown)
        self._widget.sigsetSpeedClicked.connect(self.setSpeedGUI)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Positioner', self)
    

    def _onManagerJoystickStatusChanged(self, pName, enabled):
        self.setJoystickCheckStatus(enabled)

    def _isPositionerShownInWidget(self, pManager):
        # Visibility is configuration-driven. A configured positioner stays in
        # the UI even when its hardware is currently unavailable/mock so it can
        # become usable after a runtime reconnect without rebuilding the widget.
        return bool(
            pManager.forPositioning
            and not getattr(pManager, 'hide', False)
        )

    def _isLiveUpdateEnabled(self, positionerName, pManager=None):
        if pManager is None:
            pManager = self._master.positionersManager[positionerName]
        return bool(
            getattr(pManager, 'liveUpdate', False)
            and self._liveUpdateAvailable.get(positionerName, False)
            and self._liveUpdateEnabled.get(positionerName, False)
        )

    def openSettingsDialog(self):
        positionerSettings = {}
        for pName, pManager in self._master.positionersManager:
            if not self._isPositionerShownInWidget(pManager):
                continue
            positionerSettings[pName] = {
                'liveUpdateAvailable': self._liveUpdateAvailable.get(pName, False),
                'liveUpdateEnabled': self._liveUpdateEnabled.get(pName, False),
            }
        self._widget.showSettingsDialog(
            self._liveUpdateIntervalMs,
            positionerSettings,
            {
                'coarseStepMultiplier': self._coarseStepMultiplier,
                'joystickAvailable': self._getJoystickPositionerName() is not None,
                'joystickAutoReenable': self._joystickAutoReenable,
                'joystickAutoReenableDelayS': self._joystickAutoReenableDelayS,
            },
        )

    def openReferenceDialog(self):
        self._refreshReferenceStatus()
        self._widget.showReferenceDialog()

    def hasUnreferencedReferenceAxes(self):
        return any(
            not self._isAxisReferenced(pManager, axis)
            for _, pManager, axis in self._iterReferenceAxes()
        )

    def openStartupReferenceDialogIfNeeded(self):
        """Open the normal reference dialog once startup is visible, if useful.

        This method never references or moves hardware. It only refreshes the
        rows and presents the same explicit Reference / Reference all actions as
        the manual Positioner-widget button.
        """
        referenceAxes = self._getReferenceAxesStatus()
        self._widget.setReferenceAxesStatus(referenceAxes)
        unreferenced = [
            axisInfo for axisInfo in referenceAxes
            if not axisInfo.get('referenced', False)
        ]
        if not unreferenced:
            return False

        restoredCount = sum(
            1 for axisInfo in unreferenced
            if axisInfo.get('displayedPositionRestored', False)
        )
        count = len(unreferenced)
        axisWord = 'axis is' if count == 1 else 'axes are'
        lines = [
            f'{count} open-loop {axisWord} unreferenced.'
        ]
        if restoredCount:
            restoredWord = 'position was' if restoredCount == 1 else 'positions were'
            lines.append(
                f'{restoredCount} displayed {restoredWord} restored from persisted '
                'last commands. Persisted positions have not been verified '
                'against hardware.'
            )
        lines.append('Do you want to reference them now?' if count != 1
                     else 'Do you want to reference it now?')

        self._widget.showReferenceDialog(startupHeader='\n\n'.join(lines))
        return True

    def _referencePositionerFromWidget(self, positionerName, axis, targetMode):
        if self._isReferenceBatchRunning():
            return False

        pManager = self._master.positionersManager[positionerName]
        position = None
        if targetMode == 'displayed':
            try:
                position = pManager.position[axis]
            except Exception as e:
                self._widget.showReferenceError(
                    positionerName,
                    axis,
                    f'Could not read displayed position: {e}',
                )
                return False
        elif targetMode != 'default':
            self._widget.showReferenceError(
                positionerName,
                axis,
                f'Unknown reference target: {targetMode}',
            )
            return False
        elif self._getDefaultReferencePosition(pManager) is None:
            self._widget.showReferenceError(
                positionerName,
                axis,
                'No default reference value is configured for this axis.',
            )
            return False

        targetPosition = self._formatReferenceTarget(
            pManager, position, targetMode=targetMode, axis=axis
        )
        if not self._widget.confirmReferencePositioner(positionerName, axis, targetPosition):
            return False

        return self.referencePositioner(positionerName, axis, position=position)

    def referenceAllPositioners(self):
        if self._isReferenceBatchRunning():
            return False

        try:
            plan = self._buildReferenceBatchPlan()
        except Exception as e:
            self.__logger.error(f'Could not prepare reference-all plan: {e}', exc_info=True)
            self._widget.showReferenceBatchError(f'Could not prepare reference-all plan: {e}')
            return False

        if not plan:
            return False
        if not self._widget.confirmReferenceBatch(plan):
            return False

        self._referenceBatchPlan = plan
        self._referenceBatchIndex = 0
        self._referenceBatchAbortRequested = False
        self._referenceBatchRunning = True
        self._widget.setReferenceBatchRunning(True)
        self._continueReferenceBatch()
        return True

    def abortReferenceBatch(self):
        if not self._isReferenceBatchRunning():
            return False
        self._referenceBatchAbortRequested = True
        return True

    def _continueReferenceBatch(self):
        if not self._isReferenceBatchRunning():
            return

        if self._referenceBatchAbortRequested:
            self._finishReferenceBatch()
            return

        if self._referenceBatchIndex >= len(self._referenceBatchPlan):
            self._finishReferenceBatch()
            return

        item = self._referenceBatchPlan[self._referenceBatchIndex]
        self._referenceBatchIndex += 1
        succeeded = self.referencePositioner(
            item['positionerName'],
            item['axis'],
            position=item['position'],
        )
        if not succeeded:
            self._finishReferenceBatch()
            return

        self._scheduleReferenceBatchContinue(item['waitAfterS'])

    def _scheduleReferenceBatchContinue(self, waitAfterS):
        QTimer.singleShot(max(0, int(waitAfterS * 1000)), self._continueReferenceBatch)

    def _finishReferenceBatch(self):
        self._referenceBatchPlan = []
        self._referenceBatchIndex = 0
        self._referenceBatchRunning = False
        self._referenceBatchAbortRequested = False
        self._widget.setReferenceBatchRunning(False)
        self._refreshReferenceStatus()

    def _isReferenceBatchRunning(self):
        return bool(getattr(self, '_referenceBatchRunning', False))

    def _buildReferenceBatchPlan(self):
        targetModes = self._widget.getReferenceTargetModes()
        plan = []
        for positionerName, pManager, axis in self._iterReferenceAxes():
            targetMode = targetModes.get((positionerName, axis))
            if targetMode is None:
                targetMode = self._getPreferredReferenceTarget(pManager, axis)
            position = None
            if targetMode == 'displayed':
                position = pManager.position[axis]
            elif targetMode != 'default':
                raise ValueError(
                    f'Unknown reference target "{targetMode}" for '
                    f'{positionerName} axis {axis}.'
                )
            elif self._getDefaultReferencePosition(pManager) is None:
                raise ValueError(
                    f'No default reference value is configured for '
                    f'{positionerName} axis {axis}.'
                )

            plan.append({
                'positionerName': positionerName,
                'axis': axis,
                'targetMode': targetMode,
                'position': position,
                'targetDescription': self._formatReferenceTarget(
                    pManager, position, targetMode=targetMode, axis=axis
                ),
                'waitAfterS': self._getReferenceWaitAfterS(pManager),
            })
        return plan

    def _getReferenceWaitAfterS(self, pManager):
        try:
            waitAfterS = float(getattr(pManager, 'referenceWaitAfterS', 0.3))
        except (TypeError, ValueError):
            waitAfterS = 0.3
        return max(0.0, waitAfterS)

    def referencePositioner(self, positionerName, axis, position=None):
        """Reference one positioner axis and refresh the UI state."""
        pManager = self._master.positionersManager[positionerName]
        try:
            pManager.reference(axis=axis, position=position)
        except Exception as e:
            self.__logger.error(
                f'Could not reference {positionerName} axis {axis}: {e}',
                exc_info=True,
            )
            self._widget.showReferenceError(positionerName, axis, str(e))
            return False

        refreshSucceeded = True
        try:
            self.updatePosition(positionerName, axis)
        except Exception as e:
            refreshSucceeded = False
            self.__logger.warning(
                f'Referenced {positionerName} axis {axis}, but could not refresh '
                f'the displayed position: {e}',
                exc_info=True,
            )
            self._widget.showReferenceError(
                positionerName,
                axis,
                f'Referenced, but could not refresh displayed position: {e}',
            )
        self._refreshReferenceStatus()
        return refreshSucceeded

    def _refreshReferenceStatus(self):
        self._widget.setReferenceAxesStatus(self._getReferenceAxesStatus())

    def _getReferenceAxesStatus(self):
        referenceAxes = []
        for positionerName, pManager, axis in self._iterReferenceAxes():
            displayedPositionRestored = self._isPositionRestored(pManager, axis)
            referenceAxes.append({
                'positionerName': positionerName,
                'axis': axis,
                'referenced': self._isAxisReferenced(pManager, axis),
                'defaultTargetLabel': self._formatDefaultReferenceTarget(pManager),
                'displayedTargetLabel': self._formatDisplayedReferenceTarget(
                    pManager,
                    pManager.position[axis],
                    restored=displayedPositionRestored,
                ),
                'displayedPositionRestored': displayedPositionRestored,
                'preferredTargetMode': self._getPreferredReferenceTarget(
                    pManager, axis
                ),
            })
        return referenceAxes

    def _iterReferenceAxes(self):
        for pName, pManager in self._master.positionersManager:
            if not pManager.isReferenceActionable:
                continue
            for axis in pManager.axes:
                yield pName, pManager, axis

    def _isAxisReferenced(self, pManager, axis):
        isAxisReferenced = getattr(pManager, 'isAxisReferenced', None)
        if callable(isAxisReferenced):
            return bool(isAxisReferenced(axis))
        return bool(getattr(pManager, 'isReferenced', False))

    def _isPositionRestored(self, pManager, axis):
        isPositionRestored = getattr(pManager, 'isPositionRestored', None)
        if not callable(isPositionRestored):
            return False
        try:
            return bool(isPositionRestored(axis))
        except (TypeError, ValueError):
            return False

    def _getPreferredReferenceTarget(self, pManager, axis):
        defaultPosition = self._getDefaultReferencePosition(pManager)
        if self._isPositionRestored(pManager, axis):
            currentPosition = pManager.position[axis]
            if defaultPosition is None or not self._referencePositionsEquivalent(
                currentPosition, defaultPosition
            ):
                return 'displayed'
        return 'default' if defaultPosition is not None else 'displayed'

    @staticmethod
    def _referencePositionsEquivalent(first, second):
        try:
            return abs(float(first) - float(second)) <= max(
                1e-12, 1e-9 * max(abs(float(first)), abs(float(second)))
            )
        except (TypeError, ValueError):
            return first == second

    def _formatReferenceTarget(self, pManager, position, targetMode=None, axis=None):
        if targetMode == 'displayed':
            return self._formatDisplayedReferenceTarget(
                pManager,
                position,
                restored=(
                    axis is not None
                    and self._isPositionRestored(pManager, axis)
                ),
            )
        if position is None:
            return self._formatDefaultReferenceTarget(pManager) or 'Default'
        return self._formatReferenceValues(pManager, position)

    def _formatDefaultReferenceTarget(self, pManager):
        position = self._getDefaultReferencePosition(pManager)
        if position is None:
            return None
        voltage = self._getDefaultReferenceVoltage(pManager, position)
        return f'Default ({self._formatReferenceValues(pManager, position, voltage)})'

    def _formatDisplayedReferenceTarget(self, pManager, position, *, restored=False):
        prefix = 'Persisted last command' if restored else 'Displayed position'
        return f'{prefix} ({self._formatReferenceValues(pManager, position)})'

    def _formatReferenceValues(self, pManager, position, voltage=None):
        positionText = self._formatPositionValue(pManager, position)
        if voltage is None:
            voltage = self._getReferenceVoltage(pManager, position)
        if voltage is None:
            return positionText
        return f'{positionText}, {self._formatReferenceNumber(voltage)}V'

    def _formatPositionValue(self, pManager, position):
        unit = getattr(pManager, 'positionUnit', 'µm')
        value = self._formatReferenceNumber(position)
        if unit:
            return f'{value}{unit}'
        return value

    def _formatReferenceNumber(self, value):
        try:
            return f'{float(value):.6g}'
        except (TypeError, ValueError):
            return str(value)

    def _getDefaultReferencePosition(self, pManager):
        return self._getManagerValue(pManager, 'defaultReferencePosition')

    def _getDefaultReferenceVoltage(self, pManager, defaultPosition):
        voltage = self._getManagerValue(pManager, 'defaultReferenceVoltage')
        if voltage is not None:
            return voltage
        return self._getReferenceVoltage(pManager, defaultPosition)

    def _getReferenceVoltage(self, pManager, position):
        converter = getattr(pManager, 'positionToVoltage', None)
        if callable(converter):
            try:
                voltage = converter(position)
            except (TypeError, ValueError):
                voltage = None
            if voltage is not None:
                return voltage

        conversionFactor = getattr(pManager, '_conversionFactor', None)
        if conversionFactor is None:
            managerProperties = getattr(
                getattr(pManager, '_positionerInfo', None), 'managerProperties', {}
            ) or {}
            conversionFactor = managerProperties.get('conversionFactor')
        if conversionFactor in (None, 0):
            return None
        try:
            return position / conversionFactor
        except (TypeError, ValueError):
            return None

    def _getManagerValue(self, pManager, attributeName):
        value = getattr(pManager, attributeName, None)
        if callable(value):
            value = value()
        return value

    def applySettings(self, settings):
        self._liveUpdateIntervalMs = max(100, int(settings.get(
            'liveUpdateIntervalMs', self._liveUpdateIntervalMs
        )))
        self._liveUpdateTimer.setInterval(self._liveUpdateIntervalMs)

        liveUpdateEnabled = settings.get('liveUpdateEnabled', {})
        for pName in self._liveUpdateEnabled:
            if pName in liveUpdateEnabled:
                self._liveUpdateEnabled[pName] = bool(
                    self._liveUpdateAvailable.get(pName, False)
                    and liveUpdateEnabled[pName]
                )

        try:
            multiplier = float(settings.get('coarseStepMultiplier', self._coarseStepMultiplier))
        except (TypeError, ValueError):
            multiplier = self._coarseStepMultiplier
        self._coarseStepMultiplier = max(1.0, multiplier)
        self._widget.setCoarseStepMultiplier(self._coarseStepMultiplier)

        self._joystickAutoReenable = bool(settings.get(
            'joystickAutoReenable', self._joystickAutoReenable
        ))
        try:
            delay = float(settings.get(
                'joystickAutoReenableDelayS', self._joystickAutoReenableDelayS
            ))
        except (TypeError, ValueError):
            delay = self._joystickAutoReenableDelayS
        self._joystickAutoReenableDelayS = max(0.1, delay)
        if not self._joystickAutoReenable:
            self._cancelAllJoystickAutoReenable()

        self._updateLiveTimerState()
        self._refreshLiveUpdatedPositioners()

    def setStepMode(self, coarseMode):
        self._isCoarseMode = bool(coarseMode)
        self._widget.setStepMode(self._isCoarseMode)

    def toggleStepMode(self):
        self.setStepMode(not self._isCoarseMode)

    def _getStepModeMultiplier(self):
        return self._coarseStepMultiplier if self._isCoarseMode else 1.0

    def toggleJoystick(self):
        pName = self._getJoystickPositionerName()
        if pName is None:
            return
        pManager = self._master.positionersManager[pName]
        enabled = not bool(getattr(pManager, 'joystickStatus', False))
        self.requestJoystickStatus(enabled, pName)
        self.setJoystickCheckStatus(getattr(pManager, 'joystickStatus', enabled))

    def _getJoystickPositionerName(self):
        for pName, pManager in self._master.positionersManager:
            if self._isPositionerShownInWidget(pManager) and getattr(pManager, 'joystick', False):
                return pName
        return None

    def _hasLiveUpdatePositioner(self):
        for pName, pManager in self._master.positionersManager:
            if self._isPositionerShownInWidget(pManager) and self._isLiveUpdateEnabled(pName, pManager):
                return True
        return False

    def _updateLiveTimerState(self):
        if self._hasLiveUpdatePositioner():
            if not self._liveUpdateTimer.isActive():
                self._liveUpdateTimer.start()
        elif self._liveUpdateTimer.isActive():
            self._liveUpdateTimer.stop()

    def _refreshLiveUpdatedPositioners(self):
        for pName, pManager in self._master.positionersManager:
            if not self._isPositionerShownInWidget(pManager):
                continue
            if self._isLiveUpdateEnabled(pName, pManager):
                self._pollPositionerPosition(pName, pManager)

    def _pollPositionerPosition(self, positionerName, pManager, respectBackoff=True):
        """Best-effort passive position refresh with per-positioner backoff.

        Explicit calls to :meth:`updatePosition` deliberately remain strict; only
        timer/startup-driven passive polling is allowed to absorb hardware errors.
        """
        now = time.monotonic()
        if respectBackoff and now < self._liveUpdateRetryAfter.get(positionerName, 0.0):
            return False

        try:
            self.updatePosition(positionerName, 'all')
        except Exception as e:
            failureCount = self._liveUpdateFailureCount.get(positionerName, 0) + 1
            self._liveUpdateFailureCount[positionerName] = failureCount
            delayS = self._pollBackoffSeconds[min(failureCount - 1, len(self._pollBackoffSeconds) - 1)]
            self._liveUpdateRetryAfter[positionerName] = time.monotonic() + delayS
            if failureCount <= len(self._pollBackoffSeconds) or failureCount % 10 == 0:
                self.__logger.warning(
                    f'Live position polling failed for {positionerName}; '
                    f'retrying in {delayS:g} s (failure {failureCount}): {e}'
                )
            else:
                self.__logger.debug(
                    f'Live position polling still failing for {positionerName} '
                    f'(failure {failureCount}): {e}'
                )
            return False

        failureCount = self._liveUpdateFailureCount.pop(positionerName, 0)
        self._liveUpdateRetryAfter.pop(positionerName, None)
        if failureCount:
            self.__logger.info(
                f'Live position polling recovered for {positionerName} '
                f'after {failureCount} failure(s).'
            )
        return True

    def setJoystickStatusAfterRec(self, pName):
        if self._previousJoystickState:
            # if the joystick was enabled before the scan, enable it again after rec
            self.requestJoystickStatus(True, pName)
        self._previousJoystickState = None

    def setJoystickStatusForRec(self, enabled, pName):
        if not enabled and self._previousJoystickState is None:
            pManager = self._master.positionersManager[pName]
            self._previousJoystickState = getattr(pManager, "joystickStatus", False)
        self.requestJoystickStatus(enabled, pName)


    def requestJoystickStatus(self, enabled, pName):
        pManager = self._master.positionersManager[pName]

        if not hasattr(pManager, "setJoystickEnabled"):
            return

        pManager.setJoystickEnabled(enabled)

    def setJoystickCheckStatus(self, state:bool):
        if not state and self._widget.joystickCheck.isChecked():
            self._widget.joystickCheck.setChecked(False)
        if state and not self._widget.joystickCheck.isChecked():
            self._widget.joystickCheck.setChecked(True)

    def closeEvent(self):
        self._cancelAllJoystickAutoReenable()
        if hasattr(self, '_liveUpdateTimer') and self._liveUpdateTimer.isActive():
            self._liveUpdateTimer.stop()
        self._master.positionersManager.execOnAll(
            lambda p: [p.setPosition(0, axis) for axis in p.axes],
            condition = lambda p: p.resetOnClose
        )

    def getPos(self):
        return self._master.positionersManager.execOnAll(lambda p: p.position)

    def getSpeed(self):
        return self._master.positionersManager.execOnAll(lambda p: p.speed)

    def move(self, positionerName, axis, dist):
        """Move positioner by ``dist`` in the specified axis."""
        pManager = self._master.positionersManager[positionerName]
        shouldAutoReenableJoystick = self._shouldAutoReenableJoystickAfterMove(pManager)
        result = pManager.move(dist, axis)
        if not self._isLiveUpdateEnabled(positionerName, pManager):
            if not self._applyPositionResult(positionerName, axis, result):
                self.updatePosition(positionerName, axis)
        self._scheduleJoystickAutoReenable(positionerName, axis, shouldAutoReenableJoystick)

    def setPos(self, positionerName, axis, position):
        """Move the positioner to an absolute position."""
        pManager = self._master.positionersManager[positionerName]
        shouldAutoReenableJoystick = self._shouldAutoReenableJoystickAfterMove(pManager)
        result = pManager.setPosition(position, axis)
        if not self._isLiveUpdateEnabled(positionerName, pManager):
            if not self._applyPositionResult(positionerName, axis, result):
                self.updatePosition(positionerName, axis)
        self._scheduleJoystickAutoReenable(positionerName, axis, shouldAutoReenableJoystick)

    def stepUp(self, positionerName, axis):
        stepSize = self._widget.getStepSize(positionerName, axis) * self._getStepModeMultiplier()
        self.move(positionerName, axis, stepSize)

    def stepDown(self, positionerName, axis):
        stepSize = self._widget.getStepSize(positionerName, axis) * self._getStepModeMultiplier()
        self.move(positionerName, axis, -stepSize)

    def setSpeedGUI(self):
        positionerName = self.getPositionerNames()[0]
        speed = self._widget.getSpeed()
        self.setSpeed(positionerName=positionerName, speed=speed)

    def setSpeed(self, positionerName, speed=(1000,1000,1000)):
        self._master.positionersManager[positionerName].setSpeed(speed)
        
    def updatePosition(self, positionerName, axis):
        pManager = self._master.positionersManager[positionerName]
        if hasattr(pManager, 'updatePosition'):
            pManager.updatePosition()

        axes = pManager.axes if axis == 'all' else [axis]
        for axisName in axes:
            newPos = pManager.position[axisName]
            if self._isPositionerShownInWidget(pManager):
                self._widget.updatePosition(positionerName, axisName, newPos)
            self.setSharedAttr(positionerName, axisName, _positionAttr, newPos)

    def _applyPositionResult(self, positionerName, axis, result):
        if not isinstance(result, dict) or axis not in result:
            return False
        newPos = result[axis]
        pManager = self._master.positionersManager[positionerName]
        if self._isPositionerShownInWidget(pManager):
            self._widget.updatePosition(positionerName, axis, newPos)
        self.setSharedAttr(positionerName, axis, _positionAttr, newPos)
        return True

    def _shouldAutoReenableJoystickAfterMove(self, pManager):
        return bool(
            self._joystickAutoReenable
            and getattr(pManager, 'joystick', False)
            and getattr(pManager, 'isAvailable', True)
            and hasattr(pManager, 'setJoystickEnabled')
            and getattr(pManager, 'joystickStatus', False)
        )

    def _scheduleJoystickAutoReenable(self, positionerName, axis, shouldAutoReenable):
        if not shouldAutoReenable:
            return
        pManager = self._master.positionersManager[positionerName]
        if getattr(pManager, 'joystickStatus', False):
            return
        self._joystickAutoReenablePendingAxes.setdefault(positionerName, set()).add(axis)
        timer = self._joystickAutoReenableTimers.get(positionerName)
        if timer is None:
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(lambda pName=positionerName: self._checkJoystickAutoReenable(pName))
            self._joystickAutoReenableTimers[positionerName] = timer
        timer.start(int(self._joystickAutoReenableDelayS * 1000))

    def _checkJoystickAutoReenable(self, positionerName):
        if positionerName not in self._joystickAutoReenablePendingAxes:
            return
        if not self._joystickAutoReenable:
            self._cancelJoystickAutoReenable(positionerName)
            return
        pManager = self._master.positionersManager[positionerName]
        if getattr(pManager, 'joystickStatus', False) or not getattr(pManager, 'isAvailable', True):
            self._cancelJoystickAutoReenable(positionerName)
            return

        try:
            movementFinished = self._isMovementFinished(
                pManager, self._joystickAutoReenablePendingAxes.get(positionerName, set())
            )
        except Exception as e:
            self._retryJoystickAutoReenableAfterFailure(positionerName, e)
            return

        # ``None`` means that this manager does not expose movement-finished
        # information. Keep the configured delay-only fallback in that case.
        if movementFinished is False:
            self._joystickAutoReenableFailureCount.pop(positionerName, None)
            self._joystickAutoReenableTimers[positionerName].start(self._joystickAutoReenablePollIntervalMs)
            return

        try:
            self.requestJoystickStatus(True, positionerName)
        except Exception as e:
            self._retryJoystickAutoReenableAfterFailure(positionerName, e)
            return

        self._cancelJoystickAutoReenable(positionerName)
        self.setJoystickCheckStatus(getattr(pManager, 'joystickStatus', True))

    def _retryJoystickAutoReenableAfterFailure(self, positionerName, error):
        failureCount = self._joystickAutoReenableFailureCount.get(positionerName, 0) + 1
        self._joystickAutoReenableFailureCount[positionerName] = failureCount
        delayS = self._pollBackoffSeconds[min(failureCount - 1, len(self._pollBackoffSeconds) - 1)]
        if failureCount == 1 or failureCount % 10 == 0:
            self.__logger.warning(
                f'Joystick auto-reenable communication failed for {positionerName}; '
                f'retrying in {delayS:g} s (failure {failureCount}): {error}'
            )
        else:
            self.__logger.debug(
                f'Joystick auto-reenable still failing for {positionerName} '
                f'(failure {failureCount}): {error}'
            )
        self._joystickAutoReenableTimers[positionerName].start(int(delayS * 1000))

    def _isMovementFinished(self, pManager, axes):
        query = getattr(pManager, 'isMovementFinished', None)
        if not callable(query):
            return None
        for axis in axes:
            axisFinished = query(axis)
            if axisFinished is None:
                return None
            if not axisFinished:
                return False
        return True

    def _cancelJoystickAutoReenable(self, positionerName):
        timer = self._joystickAutoReenableTimers.get(positionerName)
        if timer is not None and timer.isActive():
            timer.stop()
        self._joystickAutoReenablePendingAxes.pop(positionerName, None)
        self._joystickAutoReenableFailureCount.pop(positionerName, None)

    def _cancelAllJoystickAutoReenable(self):
        for positionerName in list(self._joystickAutoReenableTimers):
            self._cancelJoystickAutoReenable(positionerName)



    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 4 or key[0] != _attrCategory:
            return

        positionerName = key[1]
        axis = key[2]
        if key[3] == _positionAttr:
            self.setPositioner(positionerName, axis, value)

    def setSharedAttr(self, positionerName, axis, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(_attrCategory, positionerName, axis, attr)] = value
        finally:
            self.settingAttr = False

    def setXYPosition(self, x, y):
        positionerX = self.getPositionerNames()[0]
        positionerY = self.getPositionerNames()[1]
        self.__logger.debug(f"Move {positionerX}, axis X, dist {str(x)}")
        self.__logger.debug(f"Move {positionerY}, axis Y, dist {str(y)}")
        #self.move(positionerX, 'X', x)
        #self.move(positionerY, 'Y', y)

    def setZPosition(self, z):
        positionerZ = self.getPositionerNames()[2]
        self.__logger.debug(f"Move {positionerZ}, axis Z, dist {str(z)}")
        #self.move(self.getPositionerNames[2], 'Z', z)

    @APIExport()
    def getPositionerNames(self) -> List[str]:
        """ Returns the device names of all positioners. These device names can
        be passed to other positioner-related functions. """
        return self._master.positionersManager.getAllDeviceNames()

    @APIExport()
    def getPositionerPositions(self) -> Dict[str, Dict[str, float]]:
        """ Returns the positions of all positioners. """
        return self.getPos()

    @APIExport(runOnUIThread=True)
    def setPositionerStepSize(self, positionerName: str, stepSize: float) -> None:
        """ Sets the step size of the specified positioner to the specified
        number of micrometers. """
        self._widget.setStepSize(positionerName, stepSize)

    @APIExport(runOnUIThread=True)
    def movePositioner(self, positionerName: str, axis: str, dist: float) -> None:
        """ Moves the specified positioner axis by the specified number of
        micrometers. """
        self.move(positionerName, axis, dist)

    @APIExport(runOnUIThread=True)
    def setPositioner(self, positionerName: str, axis: str, position: float) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self.setPos(positionerName, axis, position)

    @APIExport(runOnUIThread=True)
    def setPositionerSpeed(self, positionerName: str, speed: float) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self.setSpeed(positionerName, speed)

    @APIExport(runOnUIThread=True)
    def setMotorsEnabled(self, positionerName: str, is_enabled: int) -> None:
        """ Moves the specified positioner axis to the specified position. """
        self._master.positionersManager[positionerName].setEnabled(is_enabled)

    @APIExport(runOnUIThread=True)
    def stepPositionerUp(self, positionerName: str, axis: str) -> None:
        """ Moves the specified positioner axis in positive direction by its
        set step size. """
        self.stepUp(positionerName, axis)

    @APIExport(runOnUIThread=True)
    def stepPositionerDown(self, positionerName: str, axis: str) -> None:
        """ Moves the specified positioner axis in negative direction by its
        set step size. """
        self.stepDown(positionerName, axis)
    
    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current positioner step sizes for both startup and setup modes.
        
        Returns step sizes per (positionerName, axis) pair.
        Does NOT include position values or speed — those are hardware state,
        not UI settings.
        
        Returns:
            {
                'step_sizes': {
                    positionerName: {
                        axis: float,
                        ...
                    },
                    ...
                }
            }
        """
        state = {
            'step_sizes': {},
            'coarse_mode': self._isCoarseMode,
            'coarse_step_multiplier': self._coarseStepMultiplier,
            'live_update_interval_ms': self._liveUpdateIntervalMs,
            'live_update_enabled': dict(self._liveUpdateEnabled),
            'joystick_auto_reenable': self._joystickAutoReenable,
            'joystick_auto_reenable_delay_s': self._joystickAutoReenableDelayS,
        }
        
        for pName, pManager in self._master.positionersManager:
            if not self._isPositionerShownInWidget(pManager):
                continue
            
            state['step_sizes'][pName] = {}
            for axis in pManager.axes:
                try:
                    state['step_sizes'][pName][axis] = self._widget.getStepSize(pName, axis)
                except Exception as e:
                    self._logger.debug(
                        f'Could not save step size for positioner {pName}, axis {axis}: {e}'
                    )
        
        return state
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode
    ) -> list[str]:
        """Restore positioner step sizes from a snapshot.
        
        IDENTICAL behavior in both STARTUP_RESTORE and SETUP_MODE_APPLY:
        - Restore step sizes per (positionerName, axis) pair
        
        NEVER (in either mode):
        - Move any stage
        - Change speed settings
        - Activate hardware
        
        Per spec Section 0 D2: settings only, never activation.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (no behavioral difference)
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []
        step_sizes = state.get('step_sizes', {})
        known_positioners = {name for name, _ in self._master.positionersManager}
        
        for pName, axes_state in step_sizes.items():
            if pName not in known_positioners:
                warnings.append(f'Positioner "{pName}" not present in current setup; skipped.')
                continue
            
            pManager = self._master.positionersManager[pName]
            if not self._isPositionerShownInWidget(pManager):
                continue
            
            for axis, step_size in axes_state.items():
                if axis not in pManager.axes:
                    warnings.append(f'Axis "{axis}" not present in positioner "{pName}"; skipped.')
                    continue
                
                try:
                    self._widget.setStepSize(pName, axis, str(step_size))
                except Exception as e:
                    warnings.append(f'Failed to restore step size for {pName}.{axis}: {e}')
        
        settings = {
            'liveUpdateIntervalMs': state.get('live_update_interval_ms', self._liveUpdateIntervalMs),
            'liveUpdateEnabled': state.get('live_update_enabled', self._liveUpdateEnabled),
            'coarseStepMultiplier': state.get('coarse_step_multiplier', self._coarseStepMultiplier),
            'joystickAutoReenable': state.get('joystick_auto_reenable', self._joystickAutoReenable),
            'joystickAutoReenableDelayS': state.get(
                'joystick_auto_reenable_delay_s', self._joystickAutoReenableDelayS
            ),
        }
        self.applySettings(settings)
        self.setStepMode(bool(state.get('coarse_mode', False)))

        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved positioner step sizes.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        step_sizes = state.get('step_sizes', {})
        if not step_sizes:
            return ['  no positioner step sizes saved']
        
        summaries = ['  step sizes:']
        for pName in sorted(step_sizes.keys()):
            axes_state = step_sizes[pName]
            for axis in sorted(axes_state.keys()):
                step_size = axes_state[axis]
                summaries.append(f'    {pName}.{axis}: {step_size:.2f} µm')
        
        return summaries
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved positioner state.
        
        Positioner step sizes have no hazards (they do not move the stage).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY
            context: Optional consumer-provided context (unused)
        
        Returns:
            Empty list (no hazards)
        """
        return []




_attrCategory = 'Positioner'
_positionAttr = 'Position'


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
