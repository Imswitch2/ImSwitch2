"""WFS-shaped facade over ImSwitch hardware managers.

The Widefield-Starss workflows under
``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/workflows`` were
written against a custom ``Microscope`` aggregator with attributes
``laser_con``, ``cam``, ``trig``, ``stage_con``, ``z_stage_con``,
``rotator_hwp``, ``rotator_qwp``. To port those workflows to ImSwitch
with minimal rewrites, we expose the same shape here and adapt to
ImSwitch's manager APIs underneath.

Concrete adapters live in this file; a mock variant for headless tests
lives in :mod:`imswitch.imcontrol.model.workflows.mock_facade`.

Construction:

    from imswitch.imcontrol.model.workflows.facade import (
        MicroscopeFacade, build_facade_from_master,
    )
    facade = build_facade_from_master(master_controller, names=...)

`build_facade_from_master` resolves managers by name from the master
controller; callers can also instantiate the sub-facades directly for
tests or partial setups.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import inspect
import threading
from typing import Iterable, List, Optional, Sequence

import numpy as np

from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.timeresolved import (
    TimeResolvedScanConfig,
    TimeResolvedScanProducts,
)

#: readChunk consumer key for workflow camera reads (see
#: DetectorManager.readChunk — plain getChunk would steal frames from
#: concurrent consumers such as the RecordingManager or BeadRec).
_WORKFLOW_CHUNK_CONSUMER = 'WorkflowFacade'


# ---------------------------------------------------------------------------
# Laser sub-facade
# ---------------------------------------------------------------------------


class LaserConFacade:
    """WFS-shaped wrapper over one or more ImSwitch ``LaserManager`` instances.

    Args:
        lasers: Mapping from logical name (e.g. ``"488"``) to the
            ImSwitch laser manager exposing ``setEnabled``, ``setValue``,
            ``setModulationEnabled`` and ``setScanModeActive``.
    """

    def __init__(self, lasers: dict) -> None:
        self._lasers = lasers

    def _resolve(self, names: Iterable[str]) -> List:
        out = []
        for n in names:
            if n not in self._lasers:
                raise KeyError(f"Laser {n!r} not in facade. Known: {list(self._lasers)}")
            out.append(self._lasers[n])
        return out

    def laser_off(self, names: Iterable[str]) -> None:
        for laser in self._resolve(names):
            laser.setEnabled(False)

    def laser_on(self, names: Iterable[str]) -> None:
        for laser in self._resolve(names):
            laser.setEnabled(True)

    def set_constant_power(self, names: Sequence[str], powers: Sequence[float]) -> None:
        """Continuous-emission mode at the given power for each laser."""
        if len(names) != len(powers):
            raise ValueError(f"names ({len(names)}) and powers ({len(powers)}) length mismatch")
        for laser, power in zip(self._resolve(names), powers):
            laser.setScanModeActive(False)
            laser.setEnabled(True)
            laser.setValue(power)

    def set_triggered_mode(self, names: Sequence[str], powers: Sequence[float]) -> None:
        """Digital-modulation mode — laser fires only on external TTL HIGH."""
        if len(names) != len(powers):
            raise ValueError(f"names ({len(names)}) and powers ({len(powers)}) length mismatch")
        for laser, power in zip(self._resolve(names), powers):
            laser.setScanModeActive(True)
            laser.setModulationEnabled(True)
            try:
                laser.setModulationPower(power)  # type: ignore[attr-defined]
            except AttributeError:
                laser.setValue(power)

    def set_modulation_mode(self, names: Optional[Iterable[str]]) -> None:
        """Exit triggered/constant-power mode for the given lasers, or all.

        Mirrors the WFS ``laser_con.set_modulation_mode`` shape; passing
        ``None`` resets every known laser.
        """
        targets = self._lasers.values() if names is None else self._resolve(names)
        for laser in targets:
            laser.setScanModeActive(False)


# ---------------------------------------------------------------------------
# Camera sub-facade
# ---------------------------------------------------------------------------


class CamFacade:
    """WFS-shaped wrapper over an ImSwitch ``DetectorManager``.

    The WFS camera object exposed ``prepare_acquisition(n)``,
    ``start_acquisition``, ``stop_acquisition``, ``get_data``,
    ``prepare_live``, ``start_live``, ``stop_live``,
    ``wait_for_frame(timeout_s)`` and a mutable ``expo`` attribute. We
    map those onto ImSwitch's ``startAcquisition`` / ``stopAcquisition``
    / ``getLatestFrame`` / ``getChunk`` and the ``Exposure`` parameter.
    """

    def __init__(self, detector, detectorsManager=None, detectorName=None) -> None:
        self._detector = detector
        self._n_planned: Optional[int] = None
        # When the DetectorsManager is supplied, the camera is held through a
        # WORKFLOW lease instead of raw start/stop on the sub-manager, so a
        # workflow can no longer disarm a detector another consumer is using
        # (and vice versa). Without it we fall back to the legacy raw calls.
        # Both are needed to lease; the detector's own name is deliberately
        # NOT read here — construction must not touch the detector object.
        self._detectorsManager = detectorsManager
        self._detectorName = detectorName
        self._acqHandle = None

    # Acquisition lifecycle ------------------------------------------------

    @property
    def _leases(self) -> bool:
        return self._detectorsManager is not None and self._detectorName is not None

    def _arm(self) -> None:
        if not self._leases:
            self._detector.startAcquisition()
            return
        if self._acqHandle is None:  # idempotent: workflows re-arm per plane
            self._acqHandle = self._detectorsManager.acquire(
                [self._detectorName], LeasePurpose.WORKFLOW
            )

    def _disarm(self) -> None:
        if not self._leases:
            self._detector.stopAcquisition()
            return
        if self._acqHandle is not None:
            self._detectorsManager.release(self._acqHandle)
            self._acqHandle = None

    def prepare_acquisition(self, n_frames: int) -> None:
        self._n_planned = int(n_frames)
        # Fresh consumer queue so no stale frames from a previous run leak in
        self._detector.releaseChunkConsumer(_WORKFLOW_CHUNK_CONSUMER)

    def start_acquisition(self) -> None:
        self._arm()

    def stop_acquisition(self) -> None:
        self._disarm()
        self._n_planned = None

    def prepare_live(self) -> None:
        self._n_planned = None
        self._detector.releaseChunkConsumer(_WORKFLOW_CHUNK_CONSUMER)

    def start_live(self) -> None:
        self._arm()

    def stop_live(self) -> None:
        self._disarm()

    # Data ----------------------------------------------------------------

    def get_data(self):
        """Return all frames acquired so far as an ndarray, or ``None``.

        Reads through DetectorManager.readChunk so concurrent consumers
        (RecordingManager, BeadRec) each still receive every frame.
        """
        frames = self._detector.readChunk(_WORKFLOW_CHUNK_CONSUMER)
        if frames is None or len(frames) == 0:
            return None
        return np.asarray(frames)

    def wait_for_frame(self, timeout_s: float = 2.0) -> bool:
        """Block until at least one frame is available, or ``timeout_s`` elapses."""
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self._detector.getLatestFrameShared()
            if frame is not None and getattr(frame, "size", 0) > 0:
                return True
            time.sleep(0.01)
        return False

    # Parameters ----------------------------------------------------------

    @property
    def expo(self) -> float:
        """Exposure in microseconds (matches WFS attribute name)."""
        params = getattr(self._detector, "parameters", {}) or {}
        if "Exposure" in params:
            return float(params["Exposure"].value)
        return 0.0

    @expo.setter
    def expo(self, value_us: float) -> None:
        self._detector.setParameter("Exposure", float(value_us))


# ---------------------------------------------------------------------------
# Time-resolved detector sub-facade
# ---------------------------------------------------------------------------


class TimeResolvedDetectorFacade:
    """Workflow-facing adapter for TCSPC/time-gated detector products."""

    _REQUIRED_METHODS = (
        "timeResolvedCapabilities",
        "configureTimeResolvedProducts",
        "waitForFinalTimeResolvedProducts",
        "getLastTimeResolvedProducts",
        "clearTimeResolvedProducts",
    )

    def __init__(self, detector, *, detectorsManager=None,
                 detectorName: str | None = None) -> None:
        missing = [
            name for name in self._REQUIRED_METHODS
            if not callable(getattr(detector, name, None))
        ]
        if missing:
            raise TypeError(
                "Detector does not implement the time-resolved contract; "
                "missing: " + ", ".join(missing)
            )
        self._detector = detector
        self._detectorsManager = detectorsManager
        self._detectorName = detectorName

    @contextmanager
    def acquisition_lease(self):
        """Keep the time-resolved detector armed through final-product drain."""
        if self._detectorsManager is None or self._detectorName is None:
            # Compatibility for standalone facade construction in tests and
            # third-party integrations that already own detector acquisition.
            yield
            return
        handle = self._detectorsManager.acquire(
            [self._detectorName], LeasePurpose.WORKFLOW
        )
        try:
            yield
        finally:
            self._detectorsManager.release(handle)

    def configure(self, config: TimeResolvedScanConfig) -> None:
        self._detector.configureTimeResolvedProducts(config)

    def wait_for_final(
        self,
        timeout_s: float | None = None,
    ) -> TimeResolvedScanProducts:
        return self._detector.waitForFinalTimeResolvedProducts(timeout_s)

    def get_last(self, copy: bool = True) -> TimeResolvedScanProducts | None:
        return self._detector.getLastTimeResolvedProducts(copy=copy)

    def clear(self) -> None:
        self._detector.clearTimeResolvedProducts()

    def capabilities(self) -> dict:
        return self._detector.timeResolvedCapabilities()


# ---------------------------------------------------------------------------
# Scan workflow sub-facade
# ---------------------------------------------------------------------------


class ScanWorkflowFacade:
    """Small adapter over ``CommunicationChannel.scanWorkflow``.

    This lets headless workflows trigger the currently configured scan surface
    without depending on a specific ScanWidget controller or scanner backend.
    """

    def __init__(self, scan_workflow, scan_done_signal=None) -> None:
        if not callable(getattr(scan_workflow, "run_scan", None)):
            raise TypeError("scan_workflow must expose run_scan(...)")
        self._scan_workflow = scan_workflow
        self._scan_done_signal = scan_done_signal

    def run_once(
        self,
        *,
        recalculate_signals: bool = True,
        is_non_final_part_of_sequence: bool = False,
        wait: bool = True,
        timeout_s: float | None = None,
        notify_starting: bool = True,
    ) -> None:
        """Trigger one existing scan and optionally wait for completion.

        Coordinator-aware controllers reject synchronously and report an exact
        request terminal bound to their accepted run token. Legacy receivers
        retain the original global-signal wait path. With no
        ``scan_done_signal`` configured, ``wait=True`` preserves the historical
        fire-and-return behavior.
        ``notify_starting=False`` is reserved for a continuation whose first
        part already published the run-level start.
        When ``wait`` is true, call this from a workflow/background thread if
        completion is delivered through queued Qt signals; blocking the Qt UI
        thread would prevent those callbacks from running.
        """
        shouldWait = wait and self._scan_done_signal is not None
        if shouldWait:
            isUiThread = getattr(
                self._scan_workflow, 'is_ui_thread', None
            )
            if callable(isUiThread) and isUiThread():
                raise RuntimeError(
                    'A blocking scan cannot run on the UI thread. Run it from '
                    'a script/background thread or pass wait=False.'
                )
            exactWaitSupported = getattr(
                self._scan_workflow, 'exact_wait_supported', None
            )
            if (
                callable(exactWaitSupported)
                and not exactWaitSupported()
            ):
                raise RuntimeError(
                    'Blocking scan completion requires one exact-capable scan '
                    'source. Select a coordinated Scan controller or pass '
                    'wait=False.'
                )

        done = threading.Event()
        scan_ended_during_request = threading.Event()
        ended_before_done = threading.Event()
        terminal = threading.Event()

        def _on_done(*_args, **_kwargs):
            done.set()
            terminal.set()

        def _on_scan_ended(*_args, **_kwargs):
            if not done.is_set():
                ended_before_done.set()
            scan_ended_during_request.set()
            terminal.set()

        connected = False
        scan_ended_signal = self._scan_ended_signal()
        scan_ended_connected = False
        if wait and self._scan_done_signal is not None:
            connected = self._connect(
                self._scan_done_signal, _on_done, direct=True
            )
        if scan_ended_signal is not None:
            scan_ended_connected = self._connect(
                scan_ended_signal, _on_scan_ended, direct=True
            )

        try:
            runPrepared = getattr(
                self._scan_workflow, "run_scan_prepared", None
            )
            notify = getattr(self._scan_workflow, "notify_scan_starting", None)
            starting_notified = False
            usePreparedRun = callable(runPrepared)
            if (
                notify_starting
                and not usePreparedRun
                and callable(notify)
            ):
                notify()
                starting_notified = True
            try:
                if usePreparedRun:
                    request_result = runPrepared(
                        bool(recalculate_signals),
                        bool(is_non_final_part_of_sequence),
                        bool(notify_starting),
                    )
                    starting_notified = bool(
                        getattr(
                            request_result,
                            'startingPublished',
                            False,
                        )
                    )
                else:
                    # This branch already published the start above, and
                    # tracks it in ``starting_notified`` so the failure paths
                    # below can pair it. Letting the dispatcher publish a
                    # second one would leave every consumer that yields
                    # hardware to a scan suspended a level deeper than the one
                    # end can unwind -- the focus lock would never come back.
                    # A dispatcher predating the argument never published one
                    # either, so the positional fallback is the same request,
                    # not a weaker version of it.
                    try:
                        request_result = self._scan_workflow.run_scan(
                            bool(recalculate_signals),
                            bool(is_non_final_part_of_sequence),
                            notify_starting=False,
                        )
                    except TypeError as signature_error:
                        if 'notify_starting' not in str(signature_error):
                            raise
                        request_result = self._scan_workflow.run_scan(
                            bool(recalculate_signals),
                            bool(is_non_final_part_of_sequence),
                        )
            except Exception as error:
                failed_request = getattr(
                    error, 'scanRequestResult', None
                )
                try:
                    starting_notified = (
                        starting_notified
                        or bool(
                            getattr(
                                failed_request,
                                'startingPublished',
                                False,
                            )
                        )
                    )
                except Exception:
                    # A malformed error envelope cannot prove a lifecycle start.
                    pass
                try:
                    ending_notified = bool(
                        getattr(
                            failed_request,
                            'endingPublished',
                            False,
                        )
                    )
                except Exception:
                    ending_notified = False
                failed_handled, failed_accepted = (
                    self._request_acceptance_state(failed_request)
                )
                if failed_handled and failed_accepted:
                    # Dispatch can raise after the controller has reserved and
                    # reported an exact run. That controller now owns the end;
                    # never forge an early global sigScanEnded while its
                    # detector barrier may still be draining.
                    try:
                        self._consume_accepted_request(
                            failed_request,
                            shouldWait=shouldWait,
                            timeout_s=timeout_s,
                            requireSuccessful=False,
                        )
                    except Exception as completionError:
                        raise completionError from error
                    raise
                if (
                    starting_notified
                    and not ending_notified
                    and not scan_ended_during_request.is_set()
                    and not (
                        failed_request is not None
                        and self._rejected_request_already_ended(
                            failed_request
                        )
                    )
                ):
                    notify_ended = getattr(
                        self._scan_workflow, "notify_scan_ended", None
                    )
                    if callable(notify_ended):
                        notify_ended()
                raise
            handled, accepted = self._request_acceptance_state(
                request_result
            )
            if handled and not accepted:
                try:
                    ending_notified = bool(
                        getattr(
                            request_result,
                            'endingPublished',
                            False,
                        )
                    )
                except Exception:
                    ending_notified = False
                if (
                    starting_notified
                    and not ending_notified
                    and not scan_ended_during_request.is_set()
                    and not self._rejected_request_already_ended(
                        request_result
                    )
                ):
                    notify_ended = getattr(
                        self._scan_workflow, "notify_scan_ended", None
                    )
                    if callable(notify_ended):
                        notify_ended()
                message = getattr(
                    request_result,
                    "rejectionMessage",
                    "No scan controller accepted the request.",
                )
                raise RuntimeError(f"Scan request rejected: {message}")

            if shouldWait and handled:
                self._consume_accepted_request(
                    request_result,
                    shouldWait=True,
                    timeout_s=timeout_s,
                    requireSuccessful=True,
                )
            elif handled:
                # Even fire-and-return mode validates that an accepted source
                # supplied one usable exact terminal. Malformed acceptance is
                # unsafe regardless of whether this caller waits.
                self._consume_accepted_request(
                    request_result,
                    shouldWait=False,
                    timeout_s=timeout_s,
                    requireSuccessful=False,
                )
            elif shouldWait and connected:
                if not terminal.wait(timeout=timeout_s):
                    raise TimeoutError(
                        "Timed out waiting for scan workflow completion"
                    )
                if ended_before_done.is_set():
                    raise RuntimeError(
                        'Scan ended before reporting successful completion.'
                    )
        finally:
            if connected:
                self._disconnect(self._scan_done_signal, _on_done)
            if scan_ended_connected:
                self._disconnect(scan_ended_signal, _on_scan_ended)

    def __call__(self) -> None:
        self.run_once()

    @staticmethod
    def _connect(signal, slot, *, direct: bool = False) -> bool:
        connect = getattr(signal, "connect", None)
        if not callable(connect):
            return False
        if direct:
            try:
                from qtpy import QtCore

                connectionType = getattr(
                    QtCore.Qt, 'ConnectionType', QtCore.Qt
                ).DirectConnection
                try:
                    connect(slot, type=connectionType)
                except TypeError:
                    connect(slot, connectionType)
                return True
            except (ImportError, AttributeError, TypeError):
                # Lightweight test/legacy signal implementations often expose
                # only connect(slot). They are already synchronous.
                pass
        connect(slot)
        return True

    @staticmethod
    def _disconnect(signal, slot) -> None:
        disconnect = getattr(signal, "disconnect", None)
        if not callable(disconnect):
            return
        try:
            disconnect(slot)
        except Exception:
            pass

    def _scan_ended_signal(self):
        """Return the service's lifecycle-end signal when it is discoverable."""
        comm_channel = getattr(self._scan_workflow, "_comm_channel", None)
        return getattr(comm_channel, "sigScanEnded", None)

    @staticmethod
    def _accepted_request_completion(request_result):
        """Require the one exact terminal supplied by an accepting owner."""
        acceptedOwners = [
            owner
            for owner, accepted, _message
            in tuple(getattr(request_result, 'reports', ()) or ())
            if accepted
        ]
        if len(acceptedOwners) != 1:
            raise RuntimeError(
                'A handled scan request must have exactly one accepting owner.'
            )
        owner = acceptedOwners[0]
        tokenEntries = tuple(
            getattr(request_result, 'acceptedTokens', ()) or ()
        )
        acceptedTokens = [
            runToken
            for tokenOwner, runToken
            in tokenEntries
            if tokenOwner is owner and runToken is not None
        ]
        if len(acceptedTokens) != 1:
            raise RuntimeError(
                'The accepting scan controller did not report one exact '
                'run token.'
            )
        runToken = acceptedTokens[0]
        for tokenOwner, extraToken in tokenEntries:
            if tokenOwner is not owner or extraToken is not runToken:
                raise RuntimeError(
                    'Multiple scan controllers reported accepted run tokens.'
                )
        entries = tuple(
            getattr(request_result, 'acceptedCompletions', ()) or ()
        )
        completions = [
            completion
            for completionOwner, completionToken, completion in entries
            if completionOwner is owner and completionToken is runToken
        ]
        if len(completions) != 1 or completions[0] is None:
            raise RuntimeError(
                'The accepting scan controller did not report one exact '
                'request terminal.'
            )
        completion = completions[0]
        if (
            getattr(completion, 'owner', None) is not owner
            or getattr(completion, 'runToken', None) is not runToken
        ):
            raise RuntimeError(
                'Scan controller reported a mismatched request terminal.'
            )
        if not callable(getattr(completion, 'wait', None)):
            raise RuntimeError(
                'The accepting scan controller reported a non-waitable exact '
                'request terminal.'
            )
        coordinator = getattr(owner, '_scanCoordinator', None)
        runForOwner = getattr(coordinator, 'runForOwner', None)
        if callable(runForOwner):
            try:
                activeRunToken = runForOwner(owner)
            except Exception as error:
                raise RuntimeError(
                    'Unable to verify the accepting scan controller run token.'
                ) from error
            if (
                activeRunToken is not runToken
                and not completion.wait(timeout=0)
            ):
                raise RuntimeError(
                    'The accepting scan controller reported a run token that '
                    'does not match its active reservation.'
                )
        for completionOwner, completionToken, extra in entries:
            if (
                extra is not None
                and (
                    completionOwner is not owner
                    or completionToken is not runToken
                    or extra is not completion
                )
            ):
                raise RuntimeError(
                    'Multiple scan controllers accepted one workflow request.'
                )
        return completion

    def _request_acceptance_state(self, request_result):
        """Read a result envelope or abort identities retained in broken fields."""
        try:
            return (
                bool(getattr(request_result, 'handled', False)),
                bool(getattr(request_result, 'accepted', False)),
            )
        except Exception:
            self._abort_accepted_request(request_result)
            raise

    def _consume_accepted_request(
        self, request_result, *, shouldWait: bool,
        timeout_s, requireSuccessful: bool,
    ):
        """Validate and optionally consume one accepted exact terminal.

        Any malformed or exceptional wait path fails closed with a targeted
        abort. A terminal that cleanly reports failure is already physically
        complete, so its source-provided message is surfaced without a
        redundant abort.
        """
        try:
            completion = self._accepted_request_completion(request_result)
        except Exception:
            # Acceptance means hardware may already be armed. A malformed
            # extension report must therefore target every identity it says
            # accepted; broadcasting could stop an unrelated scan.
            self._abort_accepted_request(request_result)
            raise
        if not shouldWait:
            return completion

        try:
            completed = completion.wait(timeout=timeout_s)
        except Exception:
            self._abort_accepted_request(request_result)
            raise
        if not completed:
            self._abort_accepted_request(request_result)
            raise TimeoutError(
                'Timed out waiting for scan workflow completion'
            )
        if not requireSuccessful:
            return completion

        try:
            successful = getattr(completion, 'successful', None)
            message = getattr(completion, 'message', '')
        except Exception:
            self._abort_accepted_request(request_result)
            raise
        if successful is not True:
            raise RuntimeError(
                message or 'Scan ended before successful completion.'
            )
        return completion

    def _abort_accepted_request(self, request_result) -> None:
        """Best-effort targeted cleanup for a malformed accepted request.

        Contract validation deliberately happens after dispatch, so a broken
        controller may already own hardware. Recover every unique owner named
        by the accepted report/token/completion fields and abort only those
        owners. Cleanup errors are suppressed to preserve the validation error
        that explains why the request could not be coordinated.
        """
        targets = []

        def retain(owner, runToken=None) -> None:
            if owner is None:
                return
            if all(
                candidate is not owner or candidateToken is not runToken
                for candidate, candidateToken in targets
            ):
                targets.append((owner, runToken))

        try:
            reports = tuple(getattr(request_result, 'reports', ()) or ())
        except Exception:
            reports = ()
        for report in reports:
            try:
                owner, accepted, _message = report
            except Exception:
                continue
            if accepted:
                retain(owner)

        try:
            tokenEntries = tuple(
                getattr(request_result, 'acceptedTokens', ()) or ()
            )
        except Exception:
            tokenEntries = ()
        for entry in tokenEntries:
            try:
                owner, runToken = entry
            except Exception:
                continue
            retain(owner, runToken)

        try:
            completionEntries = tuple(
                getattr(request_result, 'acceptedCompletions', ()) or ()
            )
        except Exception:
            completionEntries = ()
        for entry in completionEntries:
            try:
                owner, runToken, _completion = entry
            except Exception:
                continue
            retain(owner, runToken)

        # A report can name an accepted owner but omit its token. Snapshot any
        # discoverable current identity now, then carry it into the queued UI
        # abort. Never let an owner-only stale cleanup kill a later generation.
        concreteOwners = [
            owner for owner, runToken in targets if runToken is not None
        ]
        normalizedTargets = []
        for owner, runToken in targets:
            if runToken is None:
                if any(candidate is owner for candidate in concreteOwners):
                    continue
                coordinator = getattr(owner, '_scanCoordinator', None)
                runForOwner = getattr(coordinator, 'runForOwner', None)
                if callable(runForOwner):
                    try:
                        runToken = runForOwner(owner)
                    except Exception:
                        runToken = None
                if runToken is None:
                    runToken = getattr(owner, '_scanRunToken', None)
            normalizedTargets.append((owner, runToken))

        abortFrom = getattr(self._scan_workflow, 'abort_scan_from', None)
        for owner, runToken in normalizedTargets:
            if callable(abortFrom):
                try:
                    try:
                        inspect.signature(abortFrom).bind(
                            owner, runToken
                        )
                    except (TypeError, ValueError):
                        abortFrom(owner)
                    else:
                        abortFrom(owner, runToken)
                except Exception:
                    pass
                # A production service owns UI-thread marshalling. Never fall
                # through to a direct worker-thread QWidget path if that
                # service reports an abort error.
                continue
            abortScan = getattr(owner, 'abortScan', None)
            if callable(abortScan):
                try:
                    abortScan()
                except Exception:
                    pass

    @staticmethod
    def _rejected_request_already_ended(request_result) -> bool:
        """Infer a synchronously claimed-and-finished controller lifecycle.

        The normal application service exposes ``sigScanEnded``, which is the
        authoritative observation. This fallback covers lightweight service
        adapters that expose acceptance reports but not their signal channel.
        """
        for report in getattr(request_result, "reports", ()):
            if not report:
                continue
            owner = report[0]
            if (
                bool(getattr(owner, "_externalScanRequestAccepted", False))
                and getattr(owner, "_scanRunToken", None) is None
                and not bool(
                    getattr(owner, "_scanRunStartingPublished", False)
                )
            ):
                return True
        return False


# ---------------------------------------------------------------------------
# Trigger / pulse-generator sub-facade
# ---------------------------------------------------------------------------


class TrigFacade:
    """WFS-shaped wrapper around a Teensy running WFS pulse-generator firmware.

    Two construction modes are supported:

    1. **WFS pass-through (Option A).** Pass ``wfs_serial_port`` (e.g.
       ``"COM7"``) and the facade opens a direct ``pyserial`` connection.
       :meth:`snap_trigger`, :meth:`command`, and :meth:`Sendsignal` then
       speak the WFS protocol — ``Snap,...`` and ``Parameters,...`` lines
       on the wire — as documented in
       ``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/module_arduino.py``.
       No ImSwitch ``PulseGeneratorManager`` is used.

    2. **ImSwitch pulse generator.** Pass a ``pulsegen``
       (``TeensyPulseManager``/``PulseStreamerManager``). :meth:`snap_trigger`
       routes to ``pulsegen.snap``. :meth:`command` and :meth:`Sendsignal`
       remain ``NotImplementedError`` in this mode because the WFS arrays
       have not been translated to ``PulseStep`` lists yet (Option B).

    Args:
        pulsegen: An ImSwitch ``PulseGeneratorManager`` (or ``None``).
        wfs_serial_port: Serial port name (``"COM7"``) for the WFS-firmware
            Teensy. When set, takes priority over ``pulsegen``.
        wfs_baudrate: Baud rate for the WFS Teensy (default 115200).
        wfs_timeout: Per-read timeout (s) for the WFS Teensy (default 0.1).
    """

    def __init__(
        self,
        pulsegen=None,
        wfs_serial_port: Optional[str] = None,
        wfs_baudrate: int = 115200,
        wfs_timeout: float = 0.1,
    ) -> None:
        self._pulsegen = pulsegen
        self._wfs = None  # pyserial.Serial when WFS pass-through is active

        if wfs_serial_port:
            self._wfs = self._open_wfs(wfs_serial_port, wfs_baudrate, wfs_timeout)

    # --- WFS serial lifecycle ------------------------------------------

    @staticmethod
    def _open_wfs(port: str, baudrate: int, timeout: float):
        """Open the WFS-firmware Teensy with the DTR reset dance from WFS."""
        import time
        try:
            import serial
        except ImportError:
            raise RuntimeError(
                "pyserial is required for the WFS Teensy pass-through. "
                "Install with `pip install pyserial`."
            )
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        if not ser.is_open:
            raise RuntimeError(f"Could not open WFS Teensy port {port}")
        # Match the WFS Arduino class init sequence so the Teensy boots into
        # a clean state regardless of prior session.
        time.sleep(0.7)
        ser.dtr = False
        time.sleep(0.2)
        ser.dtr = True
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        return ser

    def close(self) -> None:
        if self._wfs is not None:
            try:
                self._wfs.close()
            except Exception:
                pass
            self._wfs = None

    @property
    def connected(self) -> bool:
        if self._wfs is not None:
            return bool(self._wfs.is_open)
        return self._pulsegen is not None and bool(
            getattr(self._pulsegen, "connected", True)
        )

    # --- Low-level WFS helper -----------------------------------------

    def _wfs_write_read(self, payload: str, overall_timeout: float = 2.0) -> list:
        """Send a WFS-protocol line; read response lines until DONE/ERR.

        Mirrors ``WFS.module_arduino.Arduino.write_read`` so workflow
        behaviour matches the reference implementation byte for byte.
        """
        import time
        if self._wfs is None:
            driver = getattr(self._pulsegen, "driver", None)
            send_recv = getattr(driver, "_send_recv", None)
            if callable(send_recv):
                line = send_recv(
                    payload,
                    terminal=("DONE", "ERR"),
                    timeout=overall_timeout,
                )
                return [line] if line else []
            raise RuntimeError(
                "WFS Teensy serial is not connected. Configure TrigFacade "
                "with wfs_serial_port or an ImSwitch Teensy pulse generator "
                "that exposes the legacy serial driver."
            )
        self._wfs.reset_input_buffer()
        self._wfs.reset_output_buffer()
        self._wfs.write(payload.encode())
        self._wfs.flush()

        lines = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < overall_timeout:
            raw = self._wfs.readline()
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                line = str(raw)
            if line:
                lines.append(line)
                if "DONE" in line or "ERR" in line:
                    break
        return lines

    # --- High-level helpers used by every workflow ----------------------

    def snap_trigger(self, laser_pin: int, camera_pin: int, exposure_us: int) -> None:
        """Fire one synchronised laser+camera pulse.

        WFS mode: sends ``"Snap,<laser_pin>,<camera_pin>,<exposure_us>\\n"`` and
        waits for ``DONE``/``ERR`` (or exposure-based timeout) on the Teensy.

        Pulsegen mode: emits a single :class:`PulseStep` pair via
        ``pulsegen.snap``.
        """
        if self._wfs is not None:
            payload = f"Snap,{int(laser_pin)},{int(camera_pin)},{int(exposure_us)}\n"
            timeout = int(exposure_us) * 1e-6 + 1.0
            self._wfs_write_read(payload, overall_timeout=timeout)
            return

        driver = getattr(self._pulsegen, "driver", None)
        send_recv = getattr(driver, "_send_recv", None)
        if callable(send_recv):
            payload = f"Snap,{int(laser_pin)},{int(camera_pin)},{int(exposure_us)}\n"
            timeout = int(exposure_us) * 1e-6 + 1.0
            self._wfs_write_read(payload, overall_timeout=timeout)
            return

        if self._pulsegen is None:
            raise RuntimeError(
                "TrigFacade.snap_trigger called but neither WFS Teensy nor "
                "pulse generator is configured."
            )
        width_ns = int(exposure_us) * 1000
        self._pulsegen.snap(channels=[int(laser_pin), int(camera_pin)], width_ns=width_ns)

    # --- WFS pulse-scheme: numpy translation + serial send -------------

    def command(
        self,
        start488,
        start405,
        start_camera,
        width488,
        width405,
        width_camera,
        dwelltime,
    ):
        """Build the padded ``(tWindowM, laserMod_1, laserMod_2, laserMod_3)``
        arrays that drive the WFS Teensy.

        This is a direct port of ``WFS.module_arduino.Arduino.command`` —
        pure numpy, no hardware interaction. See the source for the
        original implementation and rationale.

        Args:
            start488: Comma-separated string OR scalar; start time(s) for the
                488 nm laser pulse(s) in µs.
            start405: Start time for the 405 nm laser pulse (µs).
            start_camera: Start time for the camera exposure (µs).
            width488: Comma-separated string OR scalar; pulse width(s) for 488 in µs.
            width405: Pulse width for 405 in µs.
            width_camera: Camera exposure width in µs.
            dwelltime: Total cycle duration in µs.

        Returns:
            Tuple ``(tWindowM, laserMod_1, laserMod_2, laserMod_3)``, each
            zero-padded to length 16.
        """
        import numpy as np

        offstart = np.array([int(float(x)) for x in str(start488).split(",")])
        start = np.append(offstart, float(start405))
        start = np.append(start, float(start_camera))

        offend = np.array([int(x) for x in str(width488).split(",") if str(x).strip().isdigit()])
        width = np.append(offend, float(width405))
        width = np.append(width, float(width_camera))

        tWindowM, laserMod = self._pulse_scheme(width, start, int(dwelltime))
        laserMod = laserMod.astype(np.int32)
        tWindowM = tWindowM.astype(np.int32)

        if np.size(start) > 3:
            laserMod[1] = laserMod[0] + laserMod[1]
            laserMod[1] = np.clip(laserMod[1], 0, 1)
            laserMod = np.delete(laserMod, 0, 0)

        return (
            self._pad16(tWindowM),
            self._pad16(laserMod[0, :]),
            self._pad16(laserMod[1, :]),
            self._pad16(laserMod[2, :]),
        )

    @staticmethod
    def _pulse_scheme(pulseWidths, tStart, dwellTime):
        """Pure-numpy pulse-scheme generator — verbatim from WFS."""
        import numpy as np
        pulse = np.zeros((np.size(tStart, 0), 2))
        tEnd = np.add(tStart, pulseWidths)
        for x in range(np.size(tStart)):
            pulse[x] = [tStart[x], tEnd[x]]
        pulse = np.reshape(pulse, (1, np.size(tStart, 0) * 2))

        pulseON = np.zeros(np.size(pulse))
        pulseOFF = np.zeros(np.size(pulse))
        laserN = np.arange(1, (np.size(pulse, 1) / 2) + 1, 1)
        selON = np.arange(0, np.size(pulse), 2)
        selOFF = np.arange(1, np.size(pulse), 2)
        pulseON[selON] = laserN
        pulseOFF[selOFF] = laserN
        pulseSort = np.sort(pulse)
        indxs = np.argsort(pulse)
        pulseONsort = pulseON[indxs]
        pulseOFFsort = pulseOFF[indxs]

        laserMod = np.zeros((np.size(laserN), np.size(pulseSort) + 1))
        for x in range(np.size(pulseSort)):
            laserMod[:, x + 1] = laserMod[:, x]
            pONsBool = int(pulseONsort[0, x])
            pOFFsBool = int(pulseOFFsort[0, x])
            if pONsBool != 0:
                laserMod[pONsBool - 1, x + 1] = 1
            if pOFFsBool != 0:
                laserMod[pOFFsBool - 1, x + 1] = 0

        tLab = np.zeros(np.size(pulseSort) + 2)
        tLab[1:-1] = pulseSort
        tLab[-1] = dwellTime
        tWindowM = np.zeros(np.size(laserMod, 1))
        for x in range(np.size(tLab) - 1):
            tWindowM[x] = tLab[x + 1] - tLab[x]

        cond1 = np.where(tWindowM == 0)
        tWindowM = np.delete(tWindowM, cond1, 0)
        laserMod = np.delete(laserMod, cond1, 1)
        return tWindowM, laserMod

    @staticmethod
    def _pad16(arr):
        import numpy as np
        out = np.zeros(16)
        out[: np.size(arr)] = arr
        return out

    def Sendsignal(
        self,
        pin488,
        pin405,
        camerapin,
        delay_time,
        frame_number,
        tWindowM,
        laserMod_1,
        laserMod_2,
        laserMod_3,
    ) -> None:
        """Stream a pre-computed pulse scheme to the WFS Teensy.

        Direct port of ``WFS.module_arduino.Arduino.Sendsignal``: formats a
        ``"Parameters,...\\n"`` line and writes it to the Teensy, which
        runs the entire frame_number-long sequence on its own.

        Requires WFS pass-through mode (i.e. construction with
        ``wfs_serial_port``).
        """
        import numpy as np

        frame_arr = np.zeros(1)
        frame_arr[0] = int(frame_number)

        payload = (
            "Parameters,"
            + str(np.asarray(tWindowM).astype(np.int32)).replace("\n", "")
            + "," + str(int(pin488))
            + "," + str(np.asarray(laserMod_1).astype(np.int16))
            + "," + str(int(pin405))
            + "," + str(np.asarray(laserMod_2).astype(np.int16))
            + "," + str(int(camerapin))
            + "," + str(np.asarray(laserMod_3).astype(np.int16))
            + "," + str(int(delay_time))
            + "," + str(frame_arr.astype(np.int16))
            + "\n"
        )
        # WFS uses a generous timeout because the Teensy holds the line
        # until the entire frame_number sequence completes.
        self._wfs_write_read(payload, overall_timeout=max(5.0, float(frame_number) * 0.5))


# ---------------------------------------------------------------------------
# Stage sub-facades
# ---------------------------------------------------------------------------


class StageConFacade:
    """WFS-shaped wrapper over a 2-axis XY ``PositionerManager``.

    WFS used raw integer "stage units" via ``move_to(x, y)``. The
    underlying ``KinesisStageManager`` honours
    :attr:`KinesisStageManager._driver_units_per_position_unit` from
    config, so we forward values unchanged and let the manager apply
    the scaling.
    """

    def __init__(self, positioner, x_axis: str = "X", y_axis: str = "Y") -> None:
        self._positioner = positioner
        self._x = x_axis
        self._y = y_axis

    def move_to(self, x: float, y: float) -> None:
        self._positioner.setPosition(x, self._x)
        self._positioner.setPosition(y, self._y)

    def get_position(self) -> tuple:
        pos = self._positioner.position
        return (pos[self._x], pos[self._y])

    def jog_start(self, axis: str, sign: int) -> None:
        if hasattr(self._positioner, "jog_start"):
            self._positioner.jog_start(axis, sign)

    def jog_stop(self, axis: str) -> None:
        if hasattr(self._positioner, "jog_stop"):
            self._positioner.jog_stop(axis)


class ZStageConFacade:
    """WFS-shaped wrapper over a single-axis Z piezo (``JenaPiezoZManager``)."""

    def __init__(self, positioner, axis: str = "Z") -> None:
        self._positioner = positioner
        self._axis = axis

    def read_pos_um(self) -> float:
        return float(self._positioner.position[self._axis])

    def set_pos_um(self, value_um: float) -> None:
        self._positioner.setPosition(float(value_um), self._axis)

    def activate_ext_control(self) -> None:
        if hasattr(self._positioner, "activate_ext_control"):
            self._positioner.activate_ext_control()

    def deactivate_ext_control(self) -> None:
        if hasattr(self._positioner, "deactivate_ext_control"):
            self._positioner.deactivate_ext_control()

    @property
    def pos_range_um(self) -> tuple:
        rng = getattr(self._positioner, "_posRangeUm", (0, 100))
        return (float(rng[0]), float(rng[1]))


# ---------------------------------------------------------------------------
# Rotator sub-facade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotatorPresets:
    """H/V angles (degrees) for a polarisation-control rotator."""

    h_deg: float
    v_deg: float


class RotatorFacade:
    """WFS-shaped wrapper over an ``ElliptecRotatorManager`` (HWP or QWP)."""

    def __init__(self, rotator, presets: Optional[RotatorPresets] = None) -> None:
        self._rotator = rotator
        self._presets = presets or RotatorPresets(h_deg=0.0, v_deg=90.0)

    @property
    def presets(self) -> RotatorPresets:
        return self._presets

    def move_abs(self, deg: float) -> None:
        self._rotator.move_abs(float(deg))

    def move_rel(self, deg: float) -> None:
        self._rotator.move_rel(float(deg))

    def position(self) -> float:
        return float(self._rotator.position)

    def move_to_h(self) -> None:
        self.move_abs(self._presets.h_deg)

    def move_to_v(self) -> None:
        self.move_abs(self._presets.v_deg)

    def chained_move_to_h(self, follow_up_callable) -> None:
        """Move this rotator to H, then call ``follow_up_callable`` (the
        partner rotator's ``move_to_h``)."""
        self.move_to_h()
        if follow_up_callable is not None:
            follow_up_callable()

    def chained_move_to_v(self, follow_up_callable) -> None:
        self.move_to_v()
        if follow_up_callable is not None:
            follow_up_callable()


def _rotator_presets_from_manager(rotator) -> Optional[RotatorPresets]:
    """Read workflow H/V presets from a rotator's setup managerProperties."""
    rotator_info = getattr(rotator, "_rotatorInfo", None)
    props = getattr(rotator_info, "managerProperties", None) or {}
    raw = props.get("workflowPresets") or props.get("workflow_presets")
    if raw is None:
        return None
    return RotatorPresets(
        h_deg=float(raw.get("h_deg", raw.get("h", 0.0))),
        v_deg=float(raw.get("v_deg", raw.get("v", 90.0))),
    )


# ---------------------------------------------------------------------------
# Aggregate facade + builder
# ---------------------------------------------------------------------------


@dataclass
class MicroscopeFacade:
    """WFS-shaped aggregator. Pass this single object into a workflow.

    Any field may be ``None`` if a workflow does not need it (tests can
    construct partial facades).
    """

    laser_con: Optional[LaserConFacade] = None
    cam: Optional[CamFacade] = None
    trig: Optional[TrigFacade] = None
    stage_con: Optional[StageConFacade] = None
    z_stage_con: Optional[ZStageConFacade] = None
    rotator_hwp: Optional[RotatorFacade] = None
    rotator_qwp: Optional[RotatorFacade] = None
    time_resolved: Optional[TimeResolvedDetectorFacade] = None
    scan: Optional[ScanWorkflowFacade] = None


def build_facade_from_master(
    master,
    *,
    laser_aliases: Optional[dict] = None,
    detector_name: Optional[str] = None,
    time_resolved_detector_name: Optional[str] = None,
    pulsegen_name: str = "teensyPulse",
    wfs_teensy_port: Optional[str] = None,
    wfs_teensy_baudrate: int = 115200,
    xy_positioner_name: Optional[str] = None,
    z_positioner_name: Optional[str] = None,
    hwp_name: Optional[str] = "HWP",
    qwp_name: Optional[str] = "QWP",
    hwp_presets: Optional[RotatorPresets] = None,
    qwp_presets: Optional[RotatorPresets] = None,
    scan_workflow=None,
    scan_done_signal=None,
) -> MicroscopeFacade:
    """Build a :class:`MicroscopeFacade` from an ImSwitch ``MasterController``.

    Each name argument is optional — if omitted the corresponding
    sub-facade is left as ``None``. ``laser_aliases`` lets you map
    logical workflow names (``"488"``) to ImSwitch laser names from the
    setup JSON (e.g. ``"488 (EXC) sn27311"``):

        build_facade_from_master(master, laser_aliases={
            "488": "488 (EXC) sn27311",
            "405": "405 (ACT) sn26647",
        })
    """
    facade = MicroscopeFacade()

    if laser_aliases:
        lasers = {
            logical: master.lasersManager[real]
            for logical, real in laser_aliases.items()
        }
        facade.laser_con = LaserConFacade(lasers)

    if detector_name is not None:
        facade.cam = CamFacade(
            master.detectorsManager[detector_name],
            detectorsManager=master.detectorsManager,
            detectorName=detector_name,
        )

    if time_resolved_detector_name is not None:
        facade.time_resolved = TimeResolvedDetectorFacade(
            master.detectorsManager[time_resolved_detector_name],
            detectorsManager=master.detectorsManager,
            detectorName=time_resolved_detector_name,
        )

    if scan_workflow is not None:
        facade.scan = ScanWorkflowFacade(scan_workflow, scan_done_signal)

    # Trigger sub-facade: prefer WFS pass-through when a Teensy port was
    # given (Option A in docs/design/plans/wfs-workflows-port.md). Otherwise
    # fall back to ImSwitch's PulseGeneratorManager if present.
    if wfs_teensy_port is not None:
        facade.trig = TrigFacade(
            pulsegen=None,
            wfs_serial_port=wfs_teensy_port,
            wfs_baudrate=wfs_teensy_baudrate,
        )
    else:
        pulsegen = getattr(master, "pulseGeneratorManager", None) or getattr(
            master, "pulsegenManager", None
        )
        if pulsegen is not None:
            facade.trig = TrigFacade(pulsegen)

    if xy_positioner_name is not None:
        facade.stage_con = StageConFacade(
            master.positionersManager[xy_positioner_name]
        )

    if z_positioner_name is not None:
        facade.z_stage_con = ZStageConFacade(
            master.positionersManager[z_positioner_name]
        )

    # NB: MultiManager (parent of RotatorsManager) defines __getitem__ and
    # __iter__ (yielding `(name, manager)` tuples) but NOT __contains__, so
    # ``name in rotators_manager`` always returns False. We must look up by
    # key via __getitem__ and catch the NoSuchSubManagerError.
    rotators_manager = getattr(master, "rotatorsManager", None)
    if rotators_manager is not None:
        if hwp_name:
            try:
                hwp = rotators_manager[hwp_name]
                facade.rotator_hwp = RotatorFacade(
                    hwp,
                    hwp_presets or _rotator_presets_from_manager(hwp),
                )
            except Exception as exc:
                # Either the rotator is not in the config, or the manager
                # raised. Either way leave facade.rotator_hwp = None and
                # let the workflow fail loudly when it tries to use it.
                import logging
                logging.getLogger(__name__).warning(
                    f"Could not attach HWP rotator named {hwp_name!r}: {exc}"
                )
        if qwp_name:
            try:
                qwp = rotators_manager[qwp_name]
                facade.rotator_qwp = RotatorFacade(
                    qwp,
                    qwp_presets or _rotator_presets_from_manager(qwp),
                )
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    f"Could not attach QWP rotator named {qwp_name!r}: {exc}"
                )

    return facade


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
