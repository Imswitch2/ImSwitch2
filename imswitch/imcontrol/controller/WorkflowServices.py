import threading

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import initLogger

_SCAN_DISPATCH_QUEUE_TIMEOUT_S = 30.0
_MISSING_SCAN_IDENTITY = object()


class _ScanDispatch:
    """One call marshalled onto the service object's UI-thread affinity."""

    def __init__(self, action) -> None:
        self.action = action
        self.result = None
        self.error = None
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._cancelled = False

    def begin(self) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._started = True
            return True

    def cancelIfQueued(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._cancelled = True
            return True


class ScanRequestCompletion:
    """Identity-scoped terminal for one accepted external scan request.

    Global scan signals are retained for compatibility, but they cannot safely
    wake a blocking workflow: a queued signal may belong to an older run.  The
    accepting controller binds this object to its exact run token and resolves
    it only from that controller's own success/failure path.
    """

    def __init__(self, owner) -> None:
        self.owner = owner
        self._runToken = None
        self._successful = None
        self._message = ''
        self._callbacks = []
        self._event = threading.Event()
        self._lock = threading.Lock()

    def bind(self, runToken) -> None:
        if runToken is None:
            raise ValueError('A scan request completion requires a run token')
        with self._lock:
            if self._runToken is None:
                self._runToken = runToken
            elif self._runToken is not runToken:
                raise RuntimeError(
                    'A scan request completion cannot change run identity'
                )

    def resolve(self, runToken, successful: bool, message: str = '') -> bool:
        callbacks = ()
        with self._lock:
            if (
                self._runToken is None
                or self._runToken is not runToken
                or self._event.is_set()
            ):
                return False
            self._successful = bool(successful)
            self._message = str(message or '')
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback(self)
            except Exception:
                # Completion publication is a hardware lifecycle terminal.
                # A subscriber failure must not make the exact request appear
                # unresolved or prevent other subscribers from being notified.
                pass
        return True

    def add_done_callback(self, callback) -> None:
        """Invoke ``callback(self)`` once this exact request is terminal."""
        if not callable(callback):
            raise TypeError('Scan request completion callback must be callable')
        callNow = False
        with self._lock:
            if self._event.is_set():
                callNow = True
            else:
                self._callbacks.append(callback)
        if callNow:
            try:
                callback(self)
            except Exception:
                pass

    def wait(self, timeout=None) -> bool:
        return self._event.wait(timeout)

    @property
    def runToken(self):
        with self._lock:
            return self._runToken

    @property
    def successful(self):
        with self._lock:
            return self._successful

    @property
    def message(self) -> str:
        with self._lock:
            return self._message


class ScanRequestResult:
    """Synchronous acceptance summary for one ``sigRunScan`` broadcast.

    The legacy Qt signal remains the transport, but scan controllers can report
    whether they actually accepted the request while that signal is being
    delivered.  Callers that need a real start handshake (notably scan-mode
    Recording) can then distinguish "armed" from "broadcast but refused".
    Older/standalone controllers simply leave ``handled`` false, preserving a
    detectable compatibility path instead of pretending acceptance.
    """

    def __init__(self) -> None:
        self._reports = []
        self._acceptedTokens = []
        self._acceptedCompletions = []
        # Set by run_scan_prepared before the global pre-arm signal is emitted.
        # It lets the caller distinguish a refusal that published no lifecycle
        # from a post-start failure that must still pair that start.
        self.startingPublished = False
        self.endingPublished = False

    def report(self, owner, accepted: bool, message: str = '',
               runToken=None, completion=None) -> None:
        self._reports.append((owner, bool(accepted), str(message or '')))
        if accepted:
            self._acceptedTokens.append((owner, runToken))
            self._acceptedCompletions.append(
                (owner, runToken, completion)
            )

    @property
    def handled(self) -> bool:
        return bool(self._reports)

    @property
    def accepted(self) -> bool:
        return any(accepted for _, accepted, _ in self._reports)

    @property
    def reports(self):
        return tuple(self._reports)

    @property
    def acceptedTokens(self):
        """Exact ``(owner, runToken)`` identities reported as accepted."""
        return tuple(self._acceptedTokens)

    @property
    def acceptedCompletions(self):
        """Exact request terminals reported by accepting controllers."""
        return tuple(self._acceptedCompletions)

    @property
    def rejectionMessage(self) -> str:
        messages = [
            message for _, accepted, message in self._reports
            if not accepted and message
        ]
        return '; '.join(messages) or 'No scan controller accepted the request.'


class ScanWorkflowService(SignalInterface):
    """Small service wrapper for scan/recording workflow signals.

    This keeps legacy CommunicationChannel signals intact while giving new code
    a narrower coordination surface than the full global signal bus.
    """

    sigDispatchScanRequest = Signal(object)

    def __init__(self, comm_channel) -> None:
        super().__init__()
        self._comm_channel = comm_channel
        self._activeScanRequest = None
        self.sigDispatchScanRequest.connect(self._execute_scan_dispatch)

    def request_scan_parameters(self) -> None:
        self._comm_channel.sigRequestScanParameters.emit()

    def prepare_recording_for_scan(self, source) -> bool:
        """Let an armed recording bind to ``source`` before it starts.

        A recording armed in scan-once mode on a standalone setup waits for the
        operator to start a scan from one of several scan widgets; this is
        where it learns which one and arms its manager with that scanner's
        frame count. Synchronous and vetoing on purpose: the scan must not run
        without the recording it was armed for, and by the time an announcement
        signal came back the firmware would already be moving.

        Returns True when there is nothing to prepare, when preparation
        succeeded, or when no Recording widget exists. False means the scan
        must not start.
        """
        try:
            controllers = self._comm_channel.controllerRegistry()
        except Exception:
            return True
        recording = controllers.get('Recording')
        prepare = getattr(recording, 'prepareForScanSource', None)
        if not callable(prepare):
            return True
        try:
            return bool(prepare(source))
        except Exception:
            # A recording that could not even be asked has not been armed for
            # this scan. Refusing is the fail-closed answer: a scan that runs
            # anyway destroys the sample state the operator meant to record.
            initLogger(self).error(
                'The recording controller failed while binding to the scan '
                'that was starting; the scan was not started',
                exc_info=True,
            )
            return False

    def run_scan_prepared(
        self,
        recalculate_signals: bool,
        is_non_final_part_of_sequence: bool,
        notify_starting: bool = True,
    ) -> ScanRequestResult:
        """Preflight, pre-arm, and dispatch one scan as one UI transaction.

        Keeping these operations inside one UI event prevents two worker
        facades from both passing preflight and publishing starts before either
        reserves the shared scan coordinator. The result records whether the
        start was actually published so rejection/error cleanup can pair only a
        real lifecycle.
        """
        def preflightNotifyAndRun():
            source = self._resolved_scan_source()
            getActiveSource = getattr(
                self._comm_channel, 'getActiveScanSource', None
            )
            activeSource = (
                getActiveSource()
                if callable(getActiveSource) else None
            )
            if activeSource is not None:
                self.report_scan_request_result(
                    source if source is not None else activeSource,
                    False,
                    'Another scan source is already active.',
                )
                return
            if source is not None:
                coordinator = getattr(source, '_scanCoordinator', None)
                runForOwner = getattr(coordinator, 'runForOwner', None)
                if callable(runForOwner):
                    retainedRun = runForOwner(source)
                    if notify_starting and retainedRun is not None:
                        self.report_scan_request_result(
                            source,
                            False,
                            'This scan source already owns a retained run; '
                            'continue it without publishing a new start.',
                        )
                        return
                    if not notify_starting and retainedRun is None:
                        self.report_scan_request_result(
                            source,
                            False,
                            'A scan continuation requires an existing retained '
                            'run.',
                        )
                        return
                runScan = getattr(source, 'runScanExternal', None)
                if not callable(runScan):
                    raise TypeError(
                        'The selected scan source does not expose '
                        'runScanExternal'
                    )
                refusalCheck = getattr(
                    source, '_externalScanStartRefusal', None
                )
                if callable(refusalCheck):
                    refusalMessage = refusalCheck()
                    if refusalMessage:
                        self.report_scan_request_result(
                            source, False, refusalMessage
                        )
                        return

            request = self._activeScanRequest
            if request is None:
                raise RuntimeError(
                    'Prepared scan dispatch has no active request envelope'
                )
            if notify_starting:
                # Mark first so a failing pre-arm listener is still paired.
                request.startingPublished = True

            scanEndedSignal = getattr(
                self._comm_channel, 'sigScanEnded', None
            )
            connectEnded = getattr(scanEndedSignal, 'connect', None)
            disconnectEnded = getattr(scanEndedSignal, 'disconnect', None)
            observingEnd = False

            def observeEnd(*_args, **_kwargs):
                request.endingPublished = True

            if callable(connectEnded):
                try:
                    connectEnded(observeEnd)
                    observingEnd = True
                except Exception:
                    # End observation is only duplicate-suppression metadata. A
                    # broken observer connection must not prevent the actual
                    # controller request from being dispatched.
                    pass
            dispatchError = None
            try:
                if notify_starting:
                    self._comm_channel.sigScanStarting.emit()
                if source is not None:
                    runScan(
                        recalculate_signals,
                        is_non_final_part_of_sequence,
                    )
                else:
                    self._comm_channel.sigRunScan.emit(
                        recalculate_signals,
                        is_non_final_part_of_sequence,
                    )
            except Exception as error:
                dispatchError = error
                raise
            finally:
                try:
                    # A synchronous rejection owns no future controller
                    # terminal. The same is true when pre-arm publication or
                    # dispatch raises before a receiver can report ownership.
                    # Pair the already-published lifecycle here on the UI
                    # thread, unless an accepted controller owns the future end
                    # or the controller already emitted it.
                    if (
                        request.startingPublished
                        and (request.handled or dispatchError is not None)
                        and not request.accepted
                        and not request.endingPublished
                        and scanEndedSignal is not None
                    ):
                        # Mark before emitting so a failing end listener cannot
                        # make the worker facade attempt a duplicate publication.
                        request.endingPublished = True
                        scanEndedSignal.emit()
                finally:
                    if observingEnd and callable(disconnectEnded):
                        try:
                            disconnectEnded(observeEnd)
                        except Exception:
                            pass

        return self._dispatch_scan_request(preflightNotifyAndRun)

    def run_scan(self, recalculate_signals: bool,
                 is_non_final_part_of_sequence: bool,
                 notify_starting: bool = True) -> ScanRequestResult:
        """Target one resolved source, or use the legacy broadcast transport.

        Dispatch is marshalled onto this service's UI-thread affinity, where
        normal scan-controller connections deliver synchronously even when a
        script called from a worker. When the application exposes source
        resolution, ambiguity or an incapable setup fails closed instead of
        falling back to a broadcast that could start several controllers.
        Adapters without source resolution retain the legacy broadcast path.
        ``handled=False`` identifies a targeted/broadcast legacy receiver that
        does not implement the acknowledgement contract.
        """
        source = self._resolved_scan_source()
        if source is not None:
            return self.run_scan_from(
                source,
                recalculate_signals,
                is_non_final_part_of_sequence,
                notify_starting=notify_starting,
            )
        return self._dispatch_scan_request(
            lambda: self._comm_channel.sigRunScan.emit(
                recalculate_signals, is_non_final_part_of_sequence
            )
        )

    def exact_wait_supported(self) -> bool:
        """Whether blocking workflow completion can be identity-scoped."""
        source = self._resolved_scan_source()
        return bool(
            getattr(source, 'supportsExactScanRequestCompletion', False)
        )

    def is_ui_thread(self) -> bool:
        """Whether the caller is on this service QObject's UI affinity."""
        try:
            from qtpy import QtCore

            return QtCore.QThread.currentThread() is self.thread()
        except Exception:
            return threading.current_thread() is threading.main_thread()

    def _resolved_scan_source(self):
        """Resolve the one production scan target without unsafe fallback."""
        resolveSource = getattr(
            self._comm_channel, 'getRecordingScanSource', None
        )
        if not callable(resolveSource):
            return None
        source = resolveSource()
        if source is None:
            raise RuntimeError(
                'Scan-source resolution returned no controller; refusing to '
                'broadcast an automated scan request.'
            )
        return source

    def run_scan_from(self, source, recalculate_signals: bool,
                      is_non_final_part_of_sequence: bool,
                      notify_starting: bool = True) -> ScanRequestResult:
        """Invoke exactly one pre-resolved scan source.

        This is the safe path for ScanLapse: standalone scan setups can have
        several receivers on the legacy broadcast, each of which would command
        hardware before ambiguity could be detected.

        Publishing ``sigScanStarting`` is part of the job, not a courtesy.
        ``runScanExternal`` arms with ``sigScanStartingEmitted=True`` -- it
        *asserts* the run-level start is already on the channel, and its
        terminal publishes ``sigScanEnded`` on the strength of that assertion.
        A dispatch that skips the start therefore produces a scan that ends
        without ever having begun, and consumers that yield hardware for the
        duration of a scan -- the focus lock above all -- go on driving an
        actuator the waveform is sweeping. Callers that own the lifecycle
        themselves pass ``notify_starting=False``.

        The emission happens inside the dispatched action so it lands on the UI
        thread ahead of the arm. Emitted from a worker it would merely *queue*
        the consumer slots, and the scan would be running before anything had
        yielded to it.
        """
        runScan = getattr(source, 'runScanExternal', None)
        if not callable(runScan):
            raise TypeError(
                'The selected scan source does not expose runScanExternal'
            )
        published = []

        def startThenRun():
            if notify_starting:
                # Mark first: a failing start listener must still be paired.
                published.append(True)
                self._comm_channel.sigScanStarting.emit()
            return runScan(
                recalculate_signals, is_non_final_part_of_sequence
            )

        try:
            result = self._dispatch_scan_request(startThenRun)
        except Exception as error:
            if published:
                self._pair_unowned_scan_start(
                    getattr(error, 'scanRequestResult', None)
                )
            raise
        if published:
            self._pair_unowned_scan_start(result)
        return result

    def _pair_unowned_scan_start(self, result) -> None:
        """Publish the end of a start no controller took responsibility for.

        Acceptance is the exact predicate: a controller marks the request
        accepted in the same call that records it owes a terminal, so an
        accepted request already has an end coming and a refused one never
        will.
        """
        if result is not None and getattr(result, 'accepted', False):
            return
        try:
            self._comm_channel.sigScanEnded.emit()
        except Exception:
            initLogger(self).error(
                'Failed to pair an unaccepted scan request with its lifecycle '
                'end; consumers may stay yielded to a scan that never ran.',
                exc_info=True,
            )

    def _dispatch_scan_request(self, action) -> ScanRequestResult:
        """Run controller-facing dispatch on this service's UI thread.

        Workflow scripts commonly run on a worker thread. A direct Qt signal
        from there queues controller slots and returns before they can report
        acceptance, which would silently downgrade a coordinated request to
        the unsafe legacy/global-terminal path. This identity-carrying
        envelope blocks only the caller while the UI thread performs the
        synchronous start handshake.
        """
        dispatch = _ScanDispatch(action)
        self.sigDispatchScanRequest.emit(dispatch)
        if not dispatch.finished.wait(_SCAN_DISPATCH_QUEUE_TIMEOUT_S):
            if dispatch.cancelIfQueued():
                raise TimeoutError(
                    'Timed out waiting for the UI thread to accept the scan '
                    'request; no scan was started.'
                )
            # The UI handler already began. Do not abandon a request that may
            # have touched hardware and let it start later without an owner.
            dispatch.finished.wait()
        if dispatch.error is not None:
            raise dispatch.error
        return dispatch.result

    def _execute_scan_dispatch(self, dispatch) -> None:
        if not dispatch.begin():
            dispatch.finished.set()
            return
        try:
            dispatch.result = self._run_scan_request(dispatch.action)
        except Exception as error:
            dispatch.error = error
        finally:
            dispatch.finished.set()

    def _run_scan_request(self, dispatch) -> ScanRequestResult:
        if self._activeScanRequest is not None:
            raise RuntimeError('A nested scan request cannot be acknowledged')
        request = ScanRequestResult()
        self._activeScanRequest = request
        try:
            try:
                dispatch()
            except Exception as error:
                # Direct targeted dispatch can propagate controller failures.
                # Preserve the acceptance/ownership reports collected in that
                # same call so a higher-level lifecycle owner can decide
                # whether it must pair the published start or the controller
                # already owns a deferred end.
                try:
                    error.scanRequestResult = request
                except Exception:
                    pass
                raise
        finally:
            self._activeScanRequest = None
        return request

    def report_scan_request_result(self, owner, accepted: bool,
                                   message: str = '',
                                   runToken=None,
                                   completion=None) -> bool:
        """Record one controller's result during the active signal delivery."""
        request = self._activeScanRequest
        if request is None:
            return False
        request.report(
            owner, accepted, message, runToken, completion
        )
        return True

    def request_scan_frequency(self) -> None:
        self._comm_channel.sigRequestScanFreq.emit()

    def set_axis_centers(self, devices, centers) -> None:
        self._comm_channel.sigSetAxisCenters.emit(devices, centers)

    def start_external_recording(self) -> None:
        self._comm_channel.sigStartRecordingExternal.emit()

    def notify_scan_starting(self) -> None:
        self._comm_channel.sigScanStarting.emit()

    def notify_scan_ended(self) -> None:
        self._comm_channel.sigScanEnded.emit()

    def abort_scan(self) -> None:
        self._comm_channel.sigAbortScan.emit()

    def abort_scan_from(self, source, runToken=None) -> None:
        """Abort a source only if it still owns the caller's exact run token."""
        abortScan = getattr(source, 'abortScan', None)
        if not callable(abortScan):
            raise TypeError(
                'The selected scan source does not expose abortScan'
            )

        def abortIfCurrent():
            if runToken is not None:
                try:
                    coordinator = getattr(source, '_scanCoordinator', None)
                    runForOwner = getattr(coordinator, 'runForOwner', None)
                except Exception:
                    # An unreadable owner identity is not authority to abort
                    # whichever generation the adapter may now be running.
                    return
                if callable(runForOwner):
                    try:
                        currentToken = runForOwner(source)
                    except Exception:
                        return
                    if currentToken is not runToken:
                        return
                else:
                    ownsToken = getattr(
                        source, 'ownsScanRunToken', None
                    )
                    if callable(ownsToken):
                        try:
                            if not ownsToken(runToken):
                                return
                        except Exception:
                            return
                    else:
                        try:
                            localToken = getattr(
                                source,
                                '_scanRunToken',
                                _MISSING_SCAN_IDENTITY,
                            )
                        except Exception:
                            return
                        if localToken is not runToken:
                            return
                    if (
                        not callable(ownsToken)
                        and localToken is _MISSING_SCAN_IDENTITY
                    ):
                        return
            abortScan()

        # Facade waits and timeout handling run on worker threads. Marshal the
        # targeted abort through the same UI-affine envelope as scan starts so
        # abortScan/scanFailed never touch QWidget state from that worker.
        self._dispatch_scan_request(abortIfCurrent)


class BeadRecWorkflowService:
    """Wrapper for bead-recognition and MoNaLISA center-query signals."""

    def __init__(self, comm_channel) -> None:
        self._comm_channel = comm_channel

    def query_center_coord(self, search_mode: str) -> None:
        self._comm_channel.sigQueryCenterCoord.emit(search_mode)

    def finish_center_coord_pipeline(self, coord) -> None:
        self._comm_channel.sigCenterCoordPipelineFinished.emit(coord)

    def update_bead_rec_center(self, y: int, x: int) -> None:
        self._comm_channel.sigUpdateBeadRecCenter.emit(y, x)

    def show_bead_rec_center_cross(self, state: bool) -> None:
        self._comm_channel.sigShowBeadRecCenterCross.emit(state)

    def set_auto_axial(self, state: bool) -> None:
        self._comm_channel.sigAutoAxialToggled.emit(state)

    def set_axial_list_buffer(self, axial_list_buffer: list) -> None:
        self._comm_channel.sigNewAxialListBuffer.emit(axial_list_buffer)

    def on_query_center_coord(self, slot) -> None:
        self._comm_channel.sigQueryCenterCoord.connect(slot)

    def on_center_coord_pipeline_finished(self, slot) -> None:
        self._comm_channel.sigCenterCoordPipelineFinished.connect(slot)

    def on_update_bead_rec_center(self, slot) -> None:
        self._comm_channel.sigUpdateBeadRecCenter.connect(slot)

    def on_show_bead_rec_center_cross(self, slot) -> None:
        self._comm_channel.sigShowBeadRecCenterCross.connect(slot)

    def on_auto_axial_toggled(self, slot) -> None:
        self._comm_channel.sigAutoAxialToggled.connect(slot)

    def on_new_axial_list_buffer(self, slot) -> None:
        self._comm_channel.sigNewAxialListBuffer.connect(slot)
