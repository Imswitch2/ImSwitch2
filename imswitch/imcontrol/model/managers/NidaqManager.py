import operator
import threading
import time
import traceback

try:
    import nidaqmx
    import nidaqmx._lib
    import nidaqmx.constants
    _NIDAQMX_AVAILABLE = True
except ImportError:
    _NIDAQMX_AVAILABLE = False

    class _UnavailableNidaqError(Exception):
        pass

    class _UnavailableNidaqLib:
        DaqNotFoundError = _UnavailableNidaqError
        DaqFunctionNotSupportedError = _UnavailableNidaqError

    class _UnavailableNidaqConstants:
        class AcquisitionType:
            FINITE = "finite"

        class CountDirection:
            COUNT_UP = "count_up"

        class DataTransferActiveTransferMode:
            DMA = "dma"

        class Edge:
            RISING = "rising"

        class FrequencyUnits:
            HZ = "hz"

        class TriggerType:
            DIGITAL_EDGE = "digital_edge"

        WAIT_INFINITELY = -1

    class _UnavailableNidaqmx:
        _lib = _UnavailableNidaqLib
        constants = _UnavailableNidaqConstants
        DaqError = _UnavailableNidaqError

        @staticmethod
        def Task(*_args, **_kwargs):
            raise ImportError(
                'nidaqmx is required for NI-DAQ hardware. '
                'Install it with: pip install "imswitch[hardware]"'
            )

    nidaqmx = _UnavailableNidaqmx()

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface, Thread
from imswitch.imcommon.model import initLogger
from .mockscan import ScanSimulationCoordinator


_TASK_WAITER_JOIN_TIMEOUT_MS = 2000
_TASK_DRIVER_TEARDOWN_TIMEOUT_MS = 2000
_ONE_SHOT_WAIT_TIMEOUT_S = 2.0


class NidaqManager(SignalInterface):
    """ For interaction with NI-DAQ hardware interfaces. """

    sigScanBuilt = Signal(object, object, object)  # (scanInfoDict, signalDict, deviceList)
    sigScanStarted = Signal()
    sigScanDone = Signal()

    # Simulation only: requests that a trigger-driven mock detector produce
    # frames (detectorName, nFrames), standing in for the hardware camera TTL.
    sigSimScanFrameTrigger = Signal(str, int)

    sigScanBuildFailed = Signal()

    def __init__(self, setupInfo):
        super().__init__()
        self.__logger = initLogger(self)
        self.__simulating = bool(setupInfo.nidaq.simulation)
        self.__warnedRuntimeErrors = set()

        if not _NIDAQMX_AVAILABLE:
            hasNidaqDevices = any(
                info.getAnalogChannel() is not None or info.getDigitalLine() is not None
                for info in setupInfo.getAllDevices().values()
            )
            if hasNidaqDevices and not self.__simulating:
                raise ImportError(
                    'nidaqmx is required for NI-DAQ hardware in this setup. '
                    'Install it with: pip install "imswitch[hardware]"'
                )
            if hasNidaqDevices:
                self.__logger.info(
                    'nidaqmx not installed; running NI-DAQ devices in simulation mode.'
                )
            else:
                self.__logger.debug('nidaqmx not installed; NI-DAQ operations disabled.')

        self.__setupInfo = setupInfo
        self.tasks = {}
        self.doTaskWaiter = None
        self.aoTaskWaiter = None
        self.timerTaskWaiter = None
        self._taskWaiters = {}
        self._taskGenerations = {}
        self._oneShotGeneration = 0
        # Native DAQmx stop()/close() calls can themselves wedge inside the
        # driver.  Keep one serialized teardown operation per task so callers
        # can wait with a deadline without starting concurrent close attempts
        # against the same native handle.
        self._taskTeardownOperations = {}
        self._scanGeneration = 0
        self._scanTransactionActive = False
        self._scanBuildFailure = None
        self._scanBuildFailureEmitted = False
        self._scanStateLock = threading.RLock()
        self._finalizeLock = threading.RLock()
        self._shutdownRequested = False
        self._finalized = False
        self._finalizeResult = None
        self.busy = False
        self.signalSent = False
        self.__timerCounterChannel = setupInfo.nidaq.getTimerCounterChannel()
        self.__startTrigger = setupInfo.nidaq.startTrigger
        self.__scanSimulator = None
        if self.__simulating:
            self.__scanSimulator = ScanSimulationCoordinator(setupInfo)
            self.__scanSimulator.sigFrameTrigger.connect(self.sigSimScanFrameTrigger)
            self.__scanSimulator.sigDone.connect(self.scanDone)

    def __del__(self):
        try:
            # Never perform an unbounded driver/thread wait from a destructor.
            # Production shutdown calls finalize() explicitly; this is only a
            # bounded best-effort fallback for partially constructed managers.
            self.finalize()
        except Exception:
            try:
                state = object.__getattribute__(self, '__dict__')
            except Exception:
                state = {}
            logger = state.get('_NidaqManager__logger')
            if logger is not None:
                logger.exception('Failed to finalize NI-DAQ manager')
        if hasattr(super(), '__del__'):
            super().__del__()

    def finalize(self, waiterTimeoutMs=_TASK_WAITER_JOIN_TIMEOUT_MS):
        """Stop every NI resource and join task waiters with a deadline.

        Task stop/close happens before waiter joins because a waiter may be
        blocked indefinitely inside ``wait_until_done`` until its task is
        closed.  A failed cleanup keeps the manager shut down and may be retried
        by another idempotent ``finalize``/``close`` call; a successful cleanup
        is a permanent no-op on subsequent calls.
        """
        state = object.__getattribute__(self, '__dict__')
        finalizeLock = self._getFinalizeLock()

        with finalizeLock:
            if state.get('_finalized', False):
                return bool(state.get('_finalizeResult', True))

            stateLock = state.get('_scanStateLock')
            if stateLock is None:
                stateLock = threading.RLock()
                self._scanStateLock = stateLock
            with stateLock:
                self._shutdownRequested = True
                # Refuse re-entry until all resources are known closed.  This
                # stays True after a failed finalize so a stale task can never
                # be overwritten by a new scan.
                self.busy = True
                self.signalSent = True
                self._scanTransactionActive = False

            with stateLock:
                pendingAtEntry = tuple(
                    state.get('_taskTeardownOperations', {}).items()
                )

            success = True
            for taskName in tuple(state.get('tasks', {})):
                if not self.stopTask(
                    taskName,
                    teardownTimeoutMs=waiterTimeoutMs,
                ):
                    success = False

            if pendingAtEntry and not self._waitForTaskTeardownOperations(
                pendingAtEntry,
                timeoutMs=waiterTimeoutMs,
            ):
                success = False

            simulator = state.get('_NidaqManager__scanSimulator')
            if simulator is not None:
                simulatorWorker = getattr(simulator, '_worker', None)
                try:
                    # Request stop first; join below with the same bounded
                    # contract as NI task waiters.
                    simulator.stop(wait=False)
                except Exception:
                    success = False
                    logger = state.get('_NidaqManager__logger')
                    if logger is not None:
                        logger.exception(
                            'Failed to stop simulated scan during NI-DAQ '
                            'finalization'
                        )
                if simulatorWorker is not None:
                    success = (
                        self._joinThreadBounded(
                            simulatorWorker,
                            waiterTimeoutMs,
                            'scan simulator',
                        )
                        and success
                    )

            waiters = self._taskWaiterSnapshot()
            if not self._stopAndJoinTaskWaiters(
                waiters, timeoutMs=waiterTimeoutMs
            ):
                success = False

            with stateLock:
                self._discardResolvedTaskState()
                success = (
                    success
                    and not bool(state.get('tasks', {}))
                    and not bool(
                        state.get('_taskTeardownOperations', {})
                    )
                )
                self._finalizeResult = success
                if success:
                    self._finalized = True
                    self.busy = False

            if not success:
                logger = state.get('_NidaqManager__logger')
                if logger is not None:
                    logger.error(
                        'NI-DAQ finalization left unresolved tasks or waiters; '
                        'the manager remains shut down and busy.'
                    )
            return success

    def _getFinalizeLock(self):
        """Return the resource-creation/finalization serialization lock."""
        state = object.__getattribute__(self, '__dict__')
        finalizeLock = state.get('_finalizeLock')
        if finalizeLock is None:
            finalizeLock = threading.RLock()
            self._finalizeLock = finalizeLock
        return finalizeLock

    def _assertResourceCreationAllowed(self):
        state = object.__getattribute__(self, '__dict__')
        stateLock = state.get('_scanStateLock')
        if stateLock is None:
            stateLock = threading.RLock()
            self._scanStateLock = stateLock
        with stateLock:
            if state.get('_shutdownRequested', False):
                raise NidaqManagerError(
                    'Cannot create NI-DAQ resources: the manager is '
                    'shutting down'
                )
            if state.get('_taskTeardownOperations', {}):
                raise NidaqManagerError(
                    'Cannot create NI-DAQ resources while a previous native '
                    'task teardown is unresolved'
                )

    def close(self, waiterTimeoutMs=_TASK_WAITER_JOIN_TIMEOUT_MS):
        """Alias for :meth:`finalize`, retained for manager conventions."""
        return self.finalize(waiterTimeoutMs=waiterTimeoutMs)

    def setScanSimulationDetectorStateProvider(self, detectorIsArmed):
        """Bind simulated frame generation to actual detector lease state."""
        if self.__scanSimulator is not None:
            self.__scanSimulator.setDetectorStateProvider(detectorIsArmed)

    @property
    def isSimulated(self):
        """Whether this NI-DAQ runs in simulation (no hardware) mode."""
        return self.__simulating

    def _warnRuntimeErrorOnce(self, key, message):
        if key in self.__warnedRuntimeErrors:
            self.__logger.debug(message)
            return
        self.__warnedRuntimeErrors.add(key)
        self.__logger.warning(message)

    def registerExternalScanDriver(self):
        """Legacy compatibility hook.

        Simulated scan timing and completion are now owned per scan by
        :class:`ScanSimulationCoordinator`; detector mocks must not suppress
        camera trigger generation globally.
        """
        pass

    def unregisterExternalScanDriver(self):
        pass

    def __makeSortedTargets(self, sortingKey):
        targetPairs = []
        for targetId, targetInfo in self.__setupInfo.getAllDevices().items():
            value = getattr(targetInfo, sortingKey)
            if callable(value):
                value = value()
            if value is not None:
                pair = [targetId, value]
                targetPairs.append(pair)
        targetPairs.sort(key=operator.itemgetter(1))
        return targetPairs

    def __createChanAOTask(self, name, channels, acquisitionType, source, rate,
                           min_val=-1, max_val=1, sampsInScan=1000, starttrig=False,
                           reference_trigger='ai/StartTrigger'):
        """ Simplified function to create an analog output task """
        if self.__simulating:
            return None
        aotask = nidaqmx.Task(name)
        channels = np.atleast_1d(channels)

        for channel in channels:
            aotask.ao_channels.add_ao_voltage_chan(channel,
                                                   min_val=min_val,
                                                   max_val=max_val)
        aotask.timing.cfg_samp_clk_timing(source=source,
                                          rate=rate,
                                          sample_mode=acquisitionType,
                                          samps_per_chan=sampsInScan)
        if starttrig:
            aotask.triggers.start_trigger.cfg_dig_edge_start_trig(reference_trigger)
        #self.__logger.debug(f'Created AO task: {name}')
        return aotask

    def __createLineDOTask(self, name, lines, acquisitionType, source, rate, sampsInScan=1000,
                           starttrig=False, reference_trigger='ai/StartTrigger'):
        """ Simplified function to create a digital output task """
        if self.__simulating:
            return None
        dotask = nidaqmx.Task(name)
        lines = np.atleast_1d(lines)
        for line in lines:
            dotask.do_channels.add_do_chan(line)
        dotask.timing.cfg_samp_clk_timing(source=source, rate=rate,
                                          sample_mode=acquisitionType,
                                          samps_per_chan=sampsInScan)
        if starttrig:
            dotask.triggers.start_trigger.cfg_dig_edge_start_trig(reference_trigger)
        #self.__logger.debug(f'Created DO task: {name}')
        return dotask

    def __createChanCITask(self, name, channel, acquisitionType, source, rate, sampsInScan=1000,
                           starttrig=False, reference_trigger='ai/StartTrigger', terminal='PFI0'):
        """ Simplified function to create a counter input task """
        if self.__simulating:
            return None
        citask = nidaqmx.Task(name)
        citaskchannel = citask.ci_channels.add_ci_count_edges_chan(
            channel,
            initial_count=0,
            edge=nidaqmx.constants.Edge.RISING,
            count_direction=nidaqmx.constants.CountDirection.COUNT_UP
        )
        citaskchannel.ci_count_edges_term = terminal
        # not sure if below is needed/what is standard/if I should use DMA (seems to be preferred)
        # or INTERRUPT (as in Imspector, more load on CPU)
        citaskchannel.ci_data_xfer_mech = nidaqmx.constants.DataTransferActiveTransferMode.DMA

        if acquisitionType == 'finite':
            acqType = nidaqmx.constants.AcquisitionType.FINITE
        citask.timing.cfg_samp_clk_timing(source=source,
                                          rate=rate,
                                          sample_mode=acqType,
                                          samps_per_chan=sampsInScan)
        if starttrig:
            citask.triggers.arm_start_trigger.dig_edge_src = reference_trigger
            citask.triggers.arm_start_trigger.trig_type = nidaqmx.constants.TriggerType.DIGITAL_EDGE
        #self.__logger.debug(f'Created CI task: {name}')
        return citask

    def __createChanCOTask(self, name, channel, rate, sampsInScan=1000, starttrig=False,
                           reference_trigger='ai/StartTrigger'):
        if self.__simulating:
            return None
        cotask = nidaqmx.Task(name)
        self.cotaskchannel = cotask.co_channels.add_co_pulse_chan_freq(
            channel, freq=rate, units=nidaqmx.constants.FrequencyUnits.HZ
        )
        cotask.timing.cfg_implicit_timing(sample_mode=nidaqmx.constants.AcquisitionType.FINITE,
                                          samps_per_chan=sampsInScan)
        if starttrig:
            cotask.triggers.arm_start_trigger.dig_edge_src = reference_trigger
            cotask.triggers.arm_start_trigger.trig_type = nidaqmx.constants.TriggerType.DIGITAL_EDGE
        #self.__logger.debug(f'Created CO task: {name}')
        return cotask

    def __createChanAITask(self, name, channel, acquisitionType, source, rate,
                           min_val=-0.5, max_val=10.0, sampsInScan=1000, starttrig=False,
                           reference_trigger='ai/StartTrigger'):
        """ Simplified function to create an analog input task """
        if self.__simulating:
            return None
        aitask = nidaqmx.Task(name)
        #for channel in channels:
        #    aitask.ai_channels.add_ai_voltage_chan(channel)
        aitask.ai_channels.add_ai_voltage_chan(channel)
        if acquisitionType == 'finite':
            acqType = nidaqmx.constants.AcquisitionType.FINITE
        aitask.timing.cfg_samp_clk_timing(source=source,
                                          rate=rate,
                                          sample_mode=acqType,
                                          samps_per_chan=sampsInScan)
        if starttrig:
            aitask.triggers.start_trigger.cfg_dig_edge_start_trig(reference_trigger)
        #self.__logger.debug(f'Created AI task: {name}')
        return aitask

    def startInputTask(self, taskName, taskType, *args):
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            if taskName in self.tasks:
                raise NidaqManagerError(
                    f'NI-DAQ task "{taskName}" is already registered'
                )
            if taskType == 'ai':
                task = self.__createChanAITask(taskName, *args)
            elif taskType == 'ci':
                task = self.__createChanCITask(taskName, *args)
            else:
                raise NidaqManagerError(
                    f'Unsupported NI-DAQ input task type: {taskType}'
                )
            if task is None:
                raise NidaqManagerError(
                    f'Cannot create NI-DAQ input task "{taskName}" in '
                    'simulation mode'
                )

            generation = self._scanGeneration
            # Register before start(): if the driver raises after partially
            # arming the task, the enclosing scan transaction can close it.
            self.tasks[taskName] = task
            self._taskGenerations[taskName] = generation
            task.start()
            return generation

    def readInputTask(self, taskName, samples=0, timeout=False, generation=None):
        if (generation is not None
                and self._taskGenerations.get(taskName) != generation):
            raise NidaqManagerError(
                f'Ignoring stale read for NI-DAQ task "{taskName}"'
            )
        if not timeout:
            return self.tasks[taskName].read(samples)
        else:
            return self.tasks[taskName].read(samples, timeout)

    def reportScanBuildFailure(self, source, error=None):
        """Record a detector preparation/start failure in the active scan.

        Qt does not propagate exceptions raised by signal slots back through
        ``emit`` reliably (some bindings terminate the process instead).
        Detector slots therefore catch their own exceptions and report them
        here.  ``runScan`` observes the report before advancing to the next
        transaction stage and performs the single shared rollback.
        """
        if error is None:
            error = source
            source = type(error).__name__
        with self._scanStateLock:
            if not self._scanTransactionActive:
                self.__logger.error(
                    'Scan failure reported outside an active build by %s: %s',
                    source, error,
                )
                return False
            if self._scanBuildFailure is None:
                self._scanBuildFailure = (str(source), error)
        return True

    def _raiseIfScanBuildFailed(self):
        failure = self._scanBuildFailure
        if failure is None:
            return
        source, error = failure
        failureError = NidaqManagerError(
            f'Scan preparation failed in {source}: {error}'
        )
        if isinstance(error, BaseException):
            raise failureError from error
        raise failureError

    def _registerScanTask(self, taskName, task, waiter, generation):
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            with self._scanStateLock:
                if (
                    taskName in self.tasks
                    or taskName in self.__dict__.get(
                        '_taskTeardownOperations', {}
                    )
                ):
                    raise NidaqManagerError(
                        f'NI-DAQ task "{taskName}" is already registered'
                    )
                self.tasks[taskName] = task
                self._taskGenerations[taskName] = generation
                if waiter is not None:
                    self._taskWaiters[taskName] = waiter

    def _taskWaiterSnapshot(self):
        """Return every known task waiter once, preserving a useful label."""
        state = object.__getattribute__(self, '__dict__')
        waiters = []
        seen = set()
        for attrName in ('timerTaskWaiter', 'doTaskWaiter', 'aoTaskWaiter'):
            waiter = state.get(attrName)
            if waiter is None or id(waiter) in seen:
                continue
            seen.add(id(waiter))
            waiters.append((attrName, waiter))
        for taskName, waiter in tuple(
            state.get('_taskWaiters', {}).items()
        ):
            if waiter is None or id(waiter) in seen:
                continue
            seen.add(id(waiter))
            waiters.append((f'{taskName} task waiter', waiter))
        for taskName, operation in tuple(
            state.get('_taskTeardownOperations', {}).items()
        ):
            waiter = operation.get('taskWaiter')
            if waiter is None or id(waiter) in seen:
                continue
            seen.add(id(waiter))
            waiters.append((f'{taskName} teardown waiter', waiter))
        return waiters

    def _runTaskTeardown(self, taskName, operation):
        """Execute one native stop/close sequence outside the caller thread."""
        errors = []
        closeFailed = False
        task = operation['task']
        logger = self.__dict__.get('_NidaqManager__logger')
        try:
            task.stop()
        except Exception as error:
            errors.append(error)
            if logger is not None:
                logger.exception(
                    'Failed to stop NI-DAQ task "%s"', taskName
                )
        try:
            task.close()
        except Exception as error:
            errors.append(error)
            closeFailed = True
            if logger is not None:
                logger.exception(
                    'Failed to close NI-DAQ task "%s"', taskName
                )

        with self._scanStateLock:
            operation['errors'] = tuple(errors)
            operation['closeFailed'] = closeFailed
            if closeFailed and taskName not in self.tasks:
                # Preserve the exact unresolved native handle.  A later
                # explicit retry is allowed only after this operation has
                # finished and been reaped.
                self.tasks[taskName] = task
                generation = operation.get('generation')
                if generation is not None:
                    self._taskGenerations[taskName] = generation
                taskWaiter = operation.get('taskWaiter')
                if taskWaiter is not None:
                    self._taskWaiters[taskName] = taskWaiter
            operation['event'].set()

    def _awaitTaskTeardown(
        self, taskName, operation, *, timeoutMs, strict
    ):
        """Wait for and reap one exact native teardown operation."""
        timeoutMs = max(0, int(timeoutMs))
        if not operation['event'].wait(timeoutMs / 1000):
            with self._scanStateLock:
                self.busy = True
            message = (
                f'Timed out after {timeoutMs / 1000:.3f} s tearing down '
                f'NI-DAQ task "{taskName}"; the native operation remains '
                'serialized and the manager is fail-closed'
            )
            logger = self.__dict__.get('_NidaqManager__logger')
            if logger is not None:
                logger.error(message)
            if strict:
                raise NidaqManagerError(message)
            return False

        with self._scanStateLock:
            operations = self.__dict__.setdefault(
                '_taskTeardownOperations', {}
            )
            if operations.get(taskName) is operation:
                operations.pop(taskName, None)
            errors = tuple(operation.get('errors', ()))

        if errors:
            message = (
                f'Failed to tear down NI-DAQ task "{taskName}": '
                + '; '.join(
                    str(error) or type(error).__name__
                    for error in errors
                )
            )
            if strict:
                raise NidaqManagerError(message) from errors[0]
            return False
        return True

    def _waitForTaskTeardownOperations(
        self, operations, *, timeoutMs
    ):
        """Boundedly reap operations that were already active on entry."""
        success = True
        for taskName, operation in operations:
            if not self._awaitTaskTeardown(
                taskName,
                operation,
                timeoutMs=timeoutMs,
                strict=False,
            ):
                success = False
        return success

    def _joinThreadBounded(self, thread, timeoutMs, label):
        """Join one Qt-like thread without ever falling back to wait-forever."""
        timeoutMs = max(0, int(timeoutMs))
        try:
            if not thread.isRunning():
                # The thread is already stopped, so the abstraction's
                # no-argument wait is now safe and reaps its native resources.
                try:
                    joined = thread.wait()
                except TypeError:
                    # Test doubles and raw Qt threads may expose only the
                    # timeout overload; zero preserves the bounded contract.
                    joined = thread.wait(0)
                return joined is not False
        except Exception:
            # If thread state cannot be queried, still make one bounded wait.
            pass

        try:
            joined = thread.wait(timeoutMs)
        except TypeError:
            # ImSwitch's framework.Thread exposes wait() without Qt's timeout
            # overload. Poll it to the same deadline and only call wait() once
            # isRunning() proves that doing so cannot block forever.
            deadline = time.monotonic() + timeoutMs / 1000
            while True:
                try:
                    stillRunning = thread.isRunning()
                except Exception:
                    logger = object.__getattribute__(
                        self, '__dict__'
                    ).get('_NidaqManager__logger')
                    if logger is not None:
                        logger.exception(
                            'Failed to inspect NI-DAQ %s while joining', label
                        )
                    return False
                if not stillRunning:
                    try:
                        thread.wait()
                    except Exception:
                        logger = object.__getattribute__(
                            self, '__dict__'
                        ).get('_NidaqManager__logger')
                        if logger is not None:
                            logger.exception(
                                'Failed to reap stopped NI-DAQ %s', label
                            )
                        return False
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.01, remaining))
            joined = False
            stillRunning = True
        except Exception:
            logger = object.__getattribute__(
                self, '__dict__'
            ).get('_NidaqManager__logger')
            if logger is not None:
                logger.exception(
                    'Failed to join NI-DAQ %s with a timeout', label
                )
            return False
        else:
            try:
                stillRunning = thread.isRunning()
            except Exception:
                stillRunning = joined is False

        if joined is False or stillRunning:
            logger = object.__getattribute__(
                self, '__dict__'
            ).get('_NidaqManager__logger')
            if logger is not None:
                logger.error(
                    'Timed out after %.3f s waiting for NI-DAQ %s to stop',
                    timeoutMs / 1000,
                    label,
                )
            return False
        return True

    def _stopAndJoinTaskWaiters(
        self, waiters, *, timeoutMs, skipWaiter=None
    ):
        """Request waiter shutdown, then perform bounded joins."""
        state = object.__getattribute__(self, '__dict__')
        logger = state.get('_NidaqManager__logger')
        success = True
        unresolved = set()
        for label, waiter in waiters:
            try:
                waiter.running = False
            except Exception:
                pass
            try:
                waiter.quit()
            except Exception:
                success = False
                if logger is not None:
                    logger.exception('Failed to stop NI-DAQ %s', label)
            if waiter is skipWaiter:
                continue
            if not self._joinThreadBounded(waiter, timeoutMs, label):
                success = False
                unresolved.add(id(waiter))

        for attrName in ('timerTaskWaiter', 'doTaskWaiter', 'aoTaskWaiter'):
            current = state.get(attrName)
            if (
                id(current) not in unresolved
                and any(current is waiter for _, waiter in waiters)
            ):
                setattr(self, attrName, None)
        return success

    def _discardResolvedTaskState(self):
        """Drop generation/waiter state only for tasks that are really gone."""
        state = object.__getattribute__(self, '__dict__')
        remaining = set(state.get('tasks', {}))
        taskWaiters = state.get('_taskWaiters', {})
        taskGenerations = state.get('_taskGenerations', {})
        self._taskWaiters = {
            name: waiter for name, waiter in taskWaiters.items()
            if name in remaining
        }
        self._taskGenerations = {
            name: generation
            for name, generation in taskGenerations.items()
            if name in remaining
        }
        for attrName in ('timerTask', 'doTask', 'aoTask'):
            taskName = attrName[:-4].lower()
            if taskName not in remaining and attrName in state:
                setattr(self, attrName, None)

    def _cleanupScanResources(
        self, *, skipWaiter=None,
        waiterTimeoutMs=_TASK_WAITER_JOIN_TIMEOUT_MS
    ):
        """Best-effort rollback for every resource a scan may have acquired."""
        # Completion callbacks can run while a stopped task/waiter unwinds.
        # Mark the transaction terminal before touching those resources so a
        # stale callback cannot emit sigScanDone during rollback.
        with self._scanStateLock:
            self.signalSent = True
            taskNames = tuple(self.tasks)
            pendingAtEntry = tuple(
                self.__dict__.get(
                    '_taskTeardownOperations', {}
                ).items()
            )

        success = True
        for taskName in taskNames:
            if not self.stopTask(
                taskName,
                teardownTimeoutMs=waiterTimeoutMs,
            ):
                success = False
        if pendingAtEntry and not self._waitForTaskTeardownOperations(
            pendingAtEntry,
            timeoutMs=waiterTimeoutMs,
        ):
            success = False

        scanSimulator = self.__scanSimulator
        if scanSimulator is not None:
            simulatorWorker = getattr(scanSimulator, '_worker', None)
            try:
                scanSimulator.stop(wait=False)
            except Exception:
                success = False
                self.__logger.exception(
                    'Failed to stop simulated scan during rollback'
                )
            if simulatorWorker is not None:
                success = (
                    self._joinThreadBounded(
                        simulatorWorker,
                        waiterTimeoutMs,
                        'scan simulator',
                    )
                    and success
                )

        success = (
            self._stopAndJoinTaskWaiters(
                self._taskWaiterSnapshot(),
                timeoutMs=waiterTimeoutMs,
                skipWaiter=skipWaiter,
            )
            and success
        )
        with self._scanStateLock:
            self._discardResolvedTaskState()
            return (
                success
                and not bool(self.tasks)
                and not bool(
                    self.__dict__.get(
                        '_taskTeardownOperations', {}
                    )
                )
            )

    def _reserveOneShotTask(self, taskName, createTask):
        """Create and register one short output task before native I/O.

        Resource creation is serialized with ``finalize`` only until the exact
        task is visible in the registry. Native write/wait calls happen after
        this method releases the lifecycle lock, so shutdown can close the task
        to unblock a wedged caller.
        """
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            with self._scanStateLock:
                if self.busy:
                    raise ScanBusyError(
                        f'Cannot start NI-DAQ task "{taskName}": the manager '
                        'is busy with another DAQ operation'
                    )
                if (
                    taskName in self.tasks
                    or taskName in self.__dict__.get(
                        '_taskTeardownOperations', {}
                    )
                ):
                    self.busy = True
                    raise NidaqManagerError(
                        f'NI-DAQ task "{taskName}" is already registered'
                    )
                self.busy = True
                self._oneShotGeneration = (
                    getattr(self, '_oneShotGeneration', 0) + 1
                )
                generation = ('one-shot', self._oneShotGeneration)

            try:
                task = createTask()
            except BaseException:
                with self._scanStateLock:
                    if not self._shutdownRequested:
                        self.busy = False
                raise

            if task is not None:
                with self._scanStateLock:
                    self.tasks[taskName] = task
                    self._taskGenerations[taskName] = generation
            return task, generation

    def _finishOneShotTaskState(self, taskName, generation):
        """Release coarse busy state only after this exact task is gone."""
        with self._scanStateLock:
            operation = self.__dict__.get(
                '_taskTeardownOperations', {}
            ).get(taskName)
            exactOutstanding = (
                self._taskGenerations.get(taskName) == generation
                or (
                    operation is not None
                    and operation.get('generation') == generation
                )
            )
            if exactOutstanding:
                self.busy = True
            elif (
                not self._shutdownRequested
                and not self._scanTransactionActive
                and not self.tasks
                and not self.__dict__.get(
                    '_taskTeardownOperations', {}
                )
            ):
                self.busy = False

    def _runOneShotOutput(self, taskName, createTask, signal):
        """Write one finite output and close it through bounded teardown."""
        task, generation = self._reserveOneShotTask(taskName, createTask)
        if task is None:
            # Simulation creates no native task, but follows the same busy-state
            # transition as hardware.
            self._finishOneShotTaskState(taskName, generation)
            return

        operationError = None
        teardownError = None
        try:
            task.write(signal, auto_start=True)
            task.wait_until_done(timeout=_ONE_SHOT_WAIT_TIMEOUT_S)
        except BaseException as error:
            operationError = error
        finally:
            try:
                stopped = self.stopTask(
                    taskName, generation, strict=True
                )
                if not stopped:
                    with self._scanStateLock:
                        operations = self.__dict__.get(
                            '_taskTeardownOperations', {}
                        )
                        conflictingLifecycle = (
                            taskName in self.tasks
                            or taskName in operations
                        )
                    if conflictingLifecycle:
                        raise NidaqManagerError(
                            f'NI-DAQ task "{taskName}" changed lifecycle '
                            'before its exact teardown completed'
                        )
                    # A concurrent finalize may already have reaped this exact
                    # task. Absence from both registries proves there is no
                    # remaining native identity for this caller to close.
            except BaseException as error:
                teardownError = error
            finally:
                self._finishOneShotTaskState(taskName, generation)

        if teardownError is not None:
            if operationError is not None:
                raise teardownError from operationError
            raise teardownError
        if operationError is not None:
            raise operationError

    def setDigital(self, target, enable):
        """Set one digital line through a registered finite output task."""
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            line = self.__setupInfo.getDevice(target).getDigitalLine()
        if line is None:
            raise NidaqManagerError('Target has no digital output assigned to it')

        acquisitionTypeFinite = nidaqmx.constants.AcquisitionType.FINITE
        tasklen = 100
        try:
            return self._runOneShotOutput(
                'setDigitalTask',
                lambda: self.__createLineDOTask(
                    'setDigitalTask',
                    line,
                    acquisitionTypeFinite,
                    r'100kHzTimebase',
                    100000,
                    tasklen,
                    False,
                ),
                enable * np.ones(tasklen, dtype=bool),
            )
        except (
            nidaqmx._lib.DaqNotFoundError,
            nidaqmx._lib.DaqFunctionNotSupportedError,
            nidaqmx.DaqError,
        ) as error:
            self._warnRuntimeErrorOnce(
                ('setDigital', target, type(error).__name__, str(error)),
                f'NI-DAQ digital write failed for {target}: {error}',
            )

    def setAnalog(self, target, voltage, min_val=-1, max_val=1):
        """Set one analog channel through a registered finite output task."""
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            channel = self.__setupInfo.getDevice(target).getAnalogChannel()
        if channel is None:
            raise NidaqManagerError('Target has no analog output assigned to it')

        acquisitionTypeFinite = nidaqmx.constants.AcquisitionType.FINITE
        tasklen = 10
        try:
            return self._runOneShotOutput(
                'setAnalogTask',
                lambda: self.__createChanAOTask(
                    'setAnalogTask',
                    channel,
                    acquisitionTypeFinite,
                    r'100kHzTimebase',
                    100000,
                    min_val,
                    max_val,
                    tasklen,
                    False,
                ),
                voltage * np.ones(tasklen, dtype=float),
            )
        except (
            nidaqmx._lib.DaqNotFoundError,
            nidaqmx._lib.DaqFunctionNotSupportedError,
            nidaqmx.DaqError,
        ) as error:
            self._warnRuntimeErrorOnce(
                ('setAnalog', target, type(error).__name__, str(error)),
                f'NI-DAQ analog write failed for {target}: {error}',
            )

    def runScan(self, signalDic, scanInfoDict):
        # Serialize the complete arm transaction with finalize().  Taking only
        # a task-registry snapshot in finalize is insufficient: a concurrent
        # scan could otherwise register hardware immediately after that
        # snapshot and leave a "finalized" manager owning a live task.
        with self._getFinalizeLock():
            self._assertResourceCreationAllowed()
            return self._runScanUnlocked(signalDic, scanInfoDict)

    def _runScanUnlocked(self, signalDic, scanInfoDict):
        """ Function assuming that the user wants to run a full scan with a stage
        controlled by analog voltage outputs and a cycle of TTL pulses continuously
        running.

        Raises ScanBusyError if a scan (or another DAQ operation) is already in
        flight. This used to be a silent no-op, which left the caller believing
        it had armed a scan that never started: no sigScanBuilt, no
        sigScanBuildFailed and no sigScanDone would ever follow, so the scan
        controller stayed `isRunning` with its button latched, and — once
        acquisition leases exist — its SCAN lease and lifecycle token would
        never be resolved. Refusal must be observable.
        """
        # A few lightweight integrations construct the manager without its
        # hardware initializer. Production instances always have this lock,
        # but preserving the observable busy-refusal contract costs nothing.
        if not hasattr(self, '_scanStateLock'):
            self._scanStateLock = threading.RLock()
        #self._logger.debug('nidaq runscan function called')
        #self._logger.debug(f'busy: {self.busy}')
        with self._scanStateLock:
            if getattr(self, '_shutdownRequested', False):
                raise NidaqManagerError(
                    'Cannot start scan: the NI-DAQ manager is shutting down'
                )
            if self.busy:
                raise ScanBusyError(
                    'Cannot start scan: the NI-DAQ manager is busy with another '
                    'scan or DAQ operation'
                )
            self._scanGeneration += 1
            scanGeneration = self._scanGeneration
            self.busy = True
            self.signalSent = False
            self._scanTransactionActive = True
            self._scanBuildFailure = None
            self._scanBuildFailureEmitted = False
            self._taskWaiters.clear()
            self._taskGenerations.clear()
        self.aoTaskWaiter = None
        self.doTaskWaiter = None
        self.timerTaskWaiter = None
        try:
            if self.tasks:
                raise NidaqManagerError(
                    'Cannot start scan with stale NI-DAQ tasks still registered'
                )

            stageDic = signalDic['scanSignalsDict']
            ttlDic = signalDic['TTLCycleSignalsDict']

            AOTargetChanPairs = self.__makeSortedTargets('getAnalogChannel')
            AOdevices = []
            AOsignals = []
            AOchannels = []

            for device, channel in AOTargetChanPairs:
                if device not in stageDic:
                    continue
                AOdevices.append(device)
                AOsignals.append(stageDic[device])
                AOchannels.append(channel)

            DOTargetChanPairs = self.__makeSortedTargets('getDigitalLine')
            DOdevices = []
            DOsignals = []
            DOlines = []

            for device, line in DOTargetChanPairs:
                if device not in ttlDic or 'Dev' not in line:
                    continue
                DOdevices.append(device)
                DOsignals.append(ttlDic[device])
                DOlines.append(line)
                
            # check if line and frame clock should be outputted, if so add to DO lists
            if self.__setupInfo.scan.lineClockLine:
                DOdevices.append('LineClock')
                DOsignals.append(ttlDic['line_clock'])
                DOlines.append(self.__setupInfo.scan.lineClockLine)
            if self.__setupInfo.scan.frameStartClockLine:
                DOdevices.append('FrameStartClock')
                DOsignals.append(ttlDic['frame_start_clock'])
                DOlines.append(self.__setupInfo.scan.frameStartClockLine)
            if self.__setupInfo.scan.frameEndClockLine:
                DOdevices.append('FrameEndClock')
                DOsignals.append(ttlDic['frame_end_clock'])
                DOlines.append(self.__setupInfo.scan.frameEndClockLine)

            hasOutputSignals = len(AOsignals) > 0 or len(DOsignals) > 0
            if not hasOutputSignals and not self.__simulating:
                raise NidaqManagerError('No signals to send')

            if not self.__simulating:
                if self.__timerCounterChannel is not None:
                    self.timerTaskWaiter = WaitThread()
                    # create timer counter output task, to control the acquisition timing (1 MHz)
                    detSampsInScan = int(
                        len(AOsignals[0] if len(AOsignals) > 0 else DOsignals[0]) * (1e6/100e3)
                    )
                    #self.__logger.debug(f'Total detection samples in scan: {detSampsInScan}')
                    self.timerTask = self.__createChanCOTask(
                        'TimerTask', channel=self.__timerCounterChannel, rate=1e6,
                        sampsInScan=detSampsInScan, starttrig=self.__startTrigger,
                        reference_trigger='ao/StartTrigger'
                    )
                    self.timerTaskWaiter.connect(self.timerTask)
                    self.timerTaskWaiter.sigWaitDone.connect(
                        lambda waiter=self.timerTaskWaiter,
                               generation=scanGeneration:
                            self._taskWaiterDone(
                                'timer', waiter, generation
                            )
                    )
                    self._registerScanTask(
                        'timer', self.timerTask, self.timerTaskWaiter,
                        scanGeneration,
                    )
                acquisitionTypeFinite = nidaqmx.constants.AcquisitionType.FINITE
                scanclock = r'100kHzTimebase'
                clockDO = scanclock
                if len(AOsignals) > 0:
                    self.aoTaskWaiter = WaitThread()
                    scanSampsInScan = len(AOsignals[0])
                    self.__logger.debug(f'Total scan samples in scan: {scanSampsInScan}')
                    self.__logger.debug(f'Total scan time: {scanSampsInScan / 0.1e6} s')
                    self.aoTask = self.__createChanAOTask('ScanAOTask', AOchannels,
                                                          acquisitionTypeFinite, scanclock,
                                                          100000, min_val=-10, max_val=10,
                                                          sampsInScan=scanSampsInScan,
                                                          starttrig=False)
                    self._registerScanTask(
                        'ao', self.aoTask, self.aoTaskWaiter, scanGeneration
                    )

                    # Important to squeeze the array, otherwise we might get
                    # an "invalid number of channels" error
                    self.aoTask.write(np.array(AOsignals).squeeze(), auto_start=False)
                    self.aoTaskWaiter.connect(self.aoTask)
                    self.aoTaskWaiter.sigWaitDone.connect(
                        lambda waiter=self.aoTaskWaiter,
                               generation=scanGeneration:
                            self._taskWaiterDone('ao', waiter, generation)
                    )
                    clockDO = r'ao/SampleClock'
                if len(DOsignals) > 0:
                    self.doTaskWaiter = WaitThread()
                    scanSampsInScan = len(DOsignals[0])
                    self.doTask = self.__createLineDOTask('ScanDOTask', DOlines,
                                                          acquisitionTypeFinite, clockDO,
                                                          100000, sampsInScan=scanSampsInScan,
                                                          starttrig=self.__startTrigger,
                                                          reference_trigger='ao/StartTrigger')
                    self._registerScanTask(
                        'do', self.doTask, self.doTaskWaiter, scanGeneration
                    )

                    # Important to squeeze the array, otherwise we might get
                    # an "invalid number of channels" error
                    self.doTask.write(np.array(DOsignals).squeeze(), auto_start=False)
                    self.doTaskWaiter.connect(self.doTask)
                    self.doTaskWaiter.sigWaitDone.connect(
                        lambda waiter=self.doTaskWaiter,
                               generation=scanGeneration:
                            self._taskWaiterDone('do', waiter, generation)
                    )

            self.sigScanBuilt.emit(scanInfoDict, signalDic, AOdevices + DOdevices)
            self._raiseIfScanBuildFailed()
            # A synchronously invoked slot may finalize the manager reentrantly
            # (the lifecycle lock is deliberately reentrant because detector
            # build slots create input tasks). Never continue arming after it.
            self._assertResourceCreationAllowed()

            if not self.__simulating:
                self._assertResourceCreationAllowed()
                if self.__timerCounterChannel is not None:
                    self.tasks['timer'].start()
                    self.timerTaskWaiter.start()
                if len(DOsignals) > 0:
                    self.tasks['do'].start()
                    self.doTaskWaiter.start()
                if len(AOsignals) > 0:
                    self.tasks['ao'].start()
                    self.aoTaskWaiter.start()

            self.sigScanStarted.emit()
            self._raiseIfScanBuildFailed()
            self._assertResourceCreationAllowed()

            if self.__simulating and self.__scanSimulator is not None:
                self._assertResourceCreationAllowed()
                self.__scanSimulator.start(signalDic, scanInfoDict)

            self._scanTransactionActive = False
            self.__logger.debug('Nidaq scan started!')
            if not self.__simulating and not self.tasks:
                self.scanDone()
        except Exception:
            self.__logger.error(traceback.format_exc())
            with self._scanStateLock:
                self._scanTransactionActive = False
            cleanupSucceeded = self._cleanupScanResources()
            with self._scanStateLock:
                # A driver that refused to stop/close remains registered and
                # keeps the manager fail-closed. A waiter or simulator that
                # missed its bounded deadline does the same even after its
                # underlying task closed successfully.
                self.busy = not cleanupSucceeded
                emitFailure = not self._scanBuildFailureEmitted
                self._scanBuildFailureEmitted = True
            if emitFailure:
                self.sigScanBuildFailed.emit()
            # runScan is a synchronous arm API.  Its caller must not report
            # success or retain a coordinator token after this transaction has
            # already rolled back.
            raise

    def stopTask(
        self,
        taskName,
        generation=None,
        *,
        strict=False,
        teardownTimeoutMs=_TASK_DRIVER_TEARDOWN_TIMEOUT_MS,
    ):
        """Stop and close one exact task generation.

        The task is detached before driver calls so synchronous completion
        callbacks cannot finish it twice. Native ``stop``/``close`` run in one
        daemon operation per task: the caller waits only to
        ``teardownTimeoutMs``, while the operation identity remains registered
        and prevents any concurrent retry or new arm. If close fails, the task
        is restored to the registry for an explicit later retry. A stop
        failure still propagates in strict mode, but a subsequent successful
        close proves that the native resource itself was released and must not
        be reinserted as an uncloseable zombie.

        ``strict=True`` propagates a :class:`NidaqManagerError`.  Input
        detectors use this contract so APD/PMT teardown can avoid an unbounded
        worker-thread join when closing the NI task did not unblock the read.
        """
        with self._scanStateLock:
            operations = self.__dict__.setdefault(
                '_taskTeardownOperations', {}
            )
            operation = operations.get(taskName)
            if operation is not None:
                expectedGeneration = operation.get('generation')
                if (
                    generation is not None
                    and expectedGeneration != generation
                ):
                    return False
                startOperation = False
            else:
                task = self.tasks.get(taskName)
                if task is None:
                    return False
                expectedGeneration = self._taskGenerations.get(taskName)
                if (
                    generation is not None
                    and expectedGeneration != generation
                ):
                    return False

                # Detach first: stop()/close() may synchronously trigger
                # another completion path, which must see this task as already
                # represented by the exact teardown operation.
                self.tasks.pop(taskName, None)
                self._taskGenerations.pop(taskName, None)
                taskWaiter = self._taskWaiters.pop(taskName, None)
                operation = {
                    'task': task,
                    'generation': expectedGeneration,
                    'taskWaiter': taskWaiter,
                    'event': threading.Event(),
                    'errors': (),
                    'closeFailed': False,
                }
                operations[taskName] = operation
                startOperation = True

        if startOperation:
            teardownThread = threading.Thread(
                target=self._runTaskTeardown,
                args=(taskName, operation),
                name=f'NidaqTaskTeardown-{taskName}',
                daemon=True,
            )
            operation['thread'] = teardownThread
            try:
                teardownThread.start()
            except BaseException as error:
                # Thread creation can fail under resource pressure. Restore the
                # exact detached state atomically so this attempt reports a
                # failure and a later stop/finalize can start a fresh operation.
                with self._scanStateLock:
                    operations = self.__dict__.setdefault(
                        '_taskTeardownOperations', {}
                    )
                    if operations.get(taskName) is operation:
                        operations.pop(taskName, None)
                        if taskName not in self.tasks:
                            self.tasks[taskName] = operation['task']
                            self.busy = True
                            expectedGeneration = operation.get('generation')
                            if expectedGeneration is not None:
                                self._taskGenerations[
                                    taskName
                                ] = expectedGeneration
                            taskWaiter = operation.get('taskWaiter')
                            if taskWaiter is not None:
                                self._taskWaiters[taskName] = taskWaiter
                    operation['errors'] = (error,)
                    operation['closeFailed'] = True
                    operation['event'].set()

        return self._awaitTaskTeardown(
            taskName,
            operation,
            timeoutMs=teardownTimeoutMs,
            strict=strict,
        )

    def inputTaskDone(self, taskName, generation=None):
        with self._scanStateLock:
            if taskName in self._taskGenerations and generation is None:
                # Every in-tree input worker receives the token returned by
                # startInputTask. An untagged completion cannot be
                # distinguished from a delayed pre-generation worker.
                return False
            if generation is not None and taskName in self.tasks and (
                    self._taskGenerations.get(taskName) != generation):
                return False
        try:
            if not self.stopTask(taskName, generation, strict=True):
                return False
        except Exception as error:
            # A failed input-task close can leave APD/PMT blocked in a driver
            # read.  Fail the whole scan, keep busy asserted through bounded
            # cleanup, and propagate so detector teardown knows not to wait
            # forever for its worker.
            self._terminalScanFailure(error)
            raise
        with self._scanStateLock:
            complete = (
                not self.tasks
                and not self.__dict__.get(
                    '_taskTeardownOperations', {}
                )
                and not self._scanTransactionActive
            )
        if complete:
            self.scanDone()
        return True

    def taskDone(self, taskName, taskWaiter, generation=None):
        with self._scanStateLock:
            if self.signalSent:
                return False
            if taskName in self._taskGenerations and generation is None:
                return False
            if self._taskWaiters.get(taskName) is not taskWaiter:
                return False
            if generation is not None and (
                    self._taskGenerations.get(taskName) != generation):
                return False
        try:
            if taskWaiter.running:
                return False
        except RuntimeError:
            return False
        if not self.stopTask(taskName, generation, strict=True):
            return False
        with self._scanStateLock:
            complete = (
                not self.tasks
                and not self.__dict__.get(
                    '_taskTeardownOperations', {}
                )
                and not self._scanTransactionActive
            )
        if complete:
            self.scanDone()
        return True

    def _taskWaiterDone(self, taskName, taskWaiter, generation):
        """Route one output waiter completion, including driver failures."""
        failure = getattr(taskWaiter, 'failure', None)
        validatedTaskFailure = False
        if failure is None:
            try:
                return self.taskDone(taskName, taskWaiter, generation)
            except Exception as error:
                failure = error
                # taskDone validated waiter identity and generation before
                # stopTask detached them. If stop failed but close succeeded,
                # that metadata intentionally stays detached; do not mistake
                # the resulting strict error for a stale callback.
                validatedTaskFailure = True

        with self._scanStateLock:
            if (
                self.signalSent
                or (
                    not validatedTaskFailure
                    and (
                        self._taskWaiters.get(taskName) is not taskWaiter
                        or self._taskGenerations.get(taskName) != generation
                    )
                )
            ):
                return False
            self.signalSent = True
            self._scanTransactionActive = False
            emitFailure = not self._scanBuildFailureEmitted
            self._scanBuildFailureEmitted = True

        self.__logger.error(
            'NI-DAQ task "%s" failed while waiting for completion: %s',
            taskName, failure, exc_info=(
                type(failure), failure, failure.__traceback__
            ),
        )
        cleanupSucceeded = self._cleanupScanResources(
            skipWaiter=taskWaiter
        )
        with self._scanStateLock:
            # Keep busy asserted until all stop/close and bounded waiter joins
            # have returned. Unresolved tasks, waiters, or simulator workers
            # keep it asserted permanently.
            self.busy = not cleanupSucceeded
        if emitFailure:
            self.sigScanBuildFailed.emit()
        return True

    def _terminalScanFailure(self, error, *, skipWaiter=None):
        """Fail an active scan after a runtime resource error."""
        with self._scanStateLock:
            if self.signalSent:
                return False
            self.signalSent = True
            self._scanTransactionActive = False
            emitFailure = not self._scanBuildFailureEmitted
            self._scanBuildFailureEmitted = True

        self.__logger.error(
            'NI-DAQ scan failed during resource teardown: %s',
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        cleanupSucceeded = self._cleanupScanResources(
            skipWaiter=skipWaiter
        )
        with self._scanStateLock:
            self.busy = not cleanupSucceeded
        if emitFailure:
            self.sigScanBuildFailed.emit()
        return True

    def scanDone(self):
        # Idempotent within a scan (signalSent is reset to False in runScan):
        # the simulated driver, taskDone and finishExternalMock can all reach
        # here, but the scan must only be reported done once.
        with self._scanStateLock:
            if self.signalSent:
                return
            self.signalSent = True
            self._scanTransactionActive = False
            self.busy = False
        if self.__scanSimulator is not None:
            try:
                self.__scanSimulator.stop()
            except Exception:
                self.__logger.exception('Failed to stop simulated scan')
        self.__logger.debug('Nidaq scan finished!')
        self.sigScanDone.emit()

    def finishExternalMock(self):
        if (self.__simulating and self.__scanSimulator is not None
                and self.__scanSimulator.isActive):
            self.__logger.debug(
                'Ignoring external mock completion; simulated scan '
                'coordinator owns scan completion.'
            )
            return
        with self._scanStateLock:
            taskNames = tuple(self.tasks)
        success = True
        for taskName in taskNames:
            if not self.stopTask(taskName):
                success = False
        if success:
            self.scanDone()

    def runContinuous(self, digital_targets, digital_signals):
        pass

class WaitThread(Thread):
    sigWaitDone = Signal()

    def __init__(self, *args, **lowLevelManagers):
        super().__init__(*args, **lowLevelManagers)
        self.task = None
        self.running = False
        self.failure = None

    def connect(self, task):
        self.task = task
        self.running = True
        self.failure = None

    def run(self):
        try:
            if self.running:
                self.task.wait_until_done(
                    nidaqmx.constants.WAIT_INFINITELY
                )
        except Exception as error:
            # An exception escaping a Qt worker slot can terminate the
            # process. Preserve it for the transaction-level failure path.
            self.failure = error
        finally:
            self.close()

    def close(self):
        self.running = False
        self.sigWaitDone.emit()
        self.quit()


class NidaqManagerError(Exception):
    """ Exception raised when error occurs in NidaqManager """

    def __init__(self, message):
        super().__init__(message)  # so str(exc) is the message, not ''
        self.message = message


class ScanBusyError(NidaqManagerError):
    """ Raised when runScan is called while a scan or other DAQ operation is
    already in flight. Distinct from a build failure: nothing was armed, so no
    scan-lifecycle signal will follow and the caller must unwind its own arm
    state (scan lease, lifecycle token, GUI run state) itself. """


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
