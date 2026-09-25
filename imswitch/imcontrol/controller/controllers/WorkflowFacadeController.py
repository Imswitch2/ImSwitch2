"""WorkflowFacadeController — API-only controller for building workflow facades.

This controller has no widget. It exists solely to provide a clean API method
for constructing a MicroscopeFacade without requiring user scripts to import
internal facade modules or access api._master directly.
"""

from typing import Any, List, Optional

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.scan_request import (
    ScanRequestRegistry, ScanRequestRejectedError, ScanRunHandle,
)
from ..basecontrollers import ImConWidgetController


class WorkflowFacadeController(ImConWidgetController):
    """API-only controller for building workflow facades.
    
    No widget required — this controller exists only to export
    build_facade_from_master via the API.
    """

    #: Recent scan request handles, for getScanRequestStatus (REST/Pyro polling).
    _scanRequests = ScanRequestRegistry()

    @APIExport(runOnUIThread=True)
    def runScan(self, source: Optional[str] = None) -> ScanRunHandle:
        """ Starts one scan with the parameters set in the scan widget and
        returns a handle for its completion.

        The request is pre-flighted before any lifecycle signal is published;
        a refused start (a scan is already running, the previous one is still
        finishing, ...) raises ``ScanRequestRejectedError`` (a RuntimeError)
        whose message is the reason, and nothing else happens. A scan design
        the scan manager refuses (longer than ``scan.maxScanTimeMin``, a
        scanner driven outside its voltage range) raises the same way; it is
        found only after ``scanStarting`` went out, which is then paired with
        a ``scanEnded``. On rigs with several scanners ``source``
        selects one by its widget key (see getScanSourceNames); without it the
        canonical Scan controller or a lone capable controller is used, and
        ambiguity raises. Repeat is switched off for the scan.

        The returned handle resolves for exactly this scan whatever the order
        of any waiter: ``handle.wait(timeout)`` (from a script), ``handle.done``
        / ``handle.successful`` / ``handle.message``, or
        ``getScanRequestStatus(handle.requestId)`` (remote clients receive the
        handle as ``{requestId, source, state, message}``). """
        workflow = self._commChannel.scanWorkflow
        endSignal = getattr(self._commChannel, 'sigScanEnded', None)

        # A source that does not report on requests (a third-party scan
        # controller) still ends with the global scanEnded. Observe it from
        # *before* dispatch so even that fallback cannot miss its terminal.
        from ..WorkflowServices import ScanRequestCompletion
        legacyToken = object()
        legacyCompletion = ScanRequestCompletion(None)
        legacyCompletion.bind(legacyToken)

        def onEnded(*_args, **_kwargs):
            legacyCompletion.resolve(
                legacyToken, True,
                'Resolved from the global scanEnded signal (the scan source '
                'does not report exact completions).'
            )

        observing = False
        if endSignal is not None:
            try:
                endSignal.connect(onEnded)
                observing = True
            except Exception:
                pass

        def stopObserving():
            nonlocal observing
            if observing:
                observing = False
                try:
                    endSignal.disconnect(onEnded)
                except Exception:
                    pass

        try:
            result = workflow.run_scan_prepared(
                True, False, notify_starting=True,
                preferred_source=source, exact_completion=True,
            )
        except Exception:
            stopObserving()
            raise
        if result.handled and not result.accepted:
            stopObserving()
            raise ScanRequestRejectedError(result.rejectionMessage)

        exact = [
            (owner, runToken, completion)
            for owner, runToken, completion in result.acceptedCompletions
            if completion is not None
        ]
        if exact:
            stopObserving()
            owner, _runToken, completion = exact[0]
            handle = ScanRunHandle(self._sourceKeyFor(owner), completion, exact=True)
        else:
            owner = result.acceptedTokens[0][0] if result.acceptedTokens else None
            key = self._sourceKeyFor(owner) if owner is not None else (source or 'scan')
            handle = ScanRunHandle(key, legacyCompletion, exact=False)
            legacyCompletion.add_done_callback(lambda _c: stopObserving())
        self._scanRequests.register(handle)
        return handle

    @APIExport(runOnUIThread=True)
    def getScanSourceNames(self) -> List[str]:
        """ Widget keys of every scan controller that runScan(source=...) can
        target on this setup. """
        names = getattr(self._commChannel, 'getRecordingScanSourceNames', None)
        return list(names()) if callable(names) else []

    @APIExport(runOnUIThread=True)
    def getScanRequestStatus(self, requestId: str) -> dict:
        """ Status of a scan started with runScan, as ``{requestId, source,
        state, message, exact}`` with state ``pending``, ``succeeded`` or
        ``failed``. Raises KeyError for an unknown or evicted request id. """
        handle = self._scanRequests.get(requestId)
        if handle is None:
            raise KeyError(f'Unknown scan request id {requestId!r}')
        return handle.to_dict()

    def _sourceKeyFor(self, owner):
        registry = getattr(self._commChannel, 'controllerRegistry', None)
        try:
            controllers = registry() if callable(registry) else {}
        except Exception:
            controllers = {}
        for key, controller in dict(controllers or {}).items():
            if controller is owner:
                return key
        return type(owner).__name__

    def build(self, **kwargs: Any) -> Any:
        """Build a MicroscopeFacade from the current master controller.

        Args:
            **kwargs: Forwarded to ``build_facade_from_master``.

        Returns:
            MicroscopeFacade: facade object with WFS-shaped sub-facades.
        """
        # Lazy import to avoid slowing down ImSwitch startup
        from imswitch.imcontrol.model.workflows.facade import build_facade_from_master

        kwargs.setdefault("scan_workflow", getattr(self._commChannel, "scanWorkflow", None))
        kwargs.setdefault("scan_done_signal", getattr(self._commChannel, "sigScanDone", None))
        return build_facade_from_master(self._master, **kwargs)

    @APIExport()
    def buildWorkflowFacade(self, **kwargs: Any) -> Any:
        """Build a MicroscopeFacade from the current master controller.

        ImSwitch's ``generateAPI`` flattens every ``@APIExport``'d method to
        the top level of ``api.imcontrol`` — so this method is reachable as
        ``api.imcontrol.buildWorkflowFacade(...)`` (not via a sub-namespace).

        Args:
            **kwargs: Forwarded to ``build_facade_from_master``. Common arguments:
                - ``laser_aliases`` (dict[str, str]): logical → setup laser names
                - ``detector_name`` (str): name of detector/camera to expose
                - ``xy_positioner_name`` (str): name of XY positioner
                - ``z_positioner_name`` (str): name of Z positioner
                - ``time_resolved_detector_name`` (str): detector implementing
                  the time-resolved contract
                - ``hwp_name`` / ``qwp_name`` (str): rotator names
                - ``hwp_presets`` / ``qwp_presets`` (RotatorPresets)
                - ``scan_workflow`` / ``scan_done_signal``: optional overrides;
                  by default these are taken from the communication channel

        Returns:
            MicroscopeFacade: facade object with WFS-shaped sub-facades.

        Example:
            >>> facade = api.imcontrol.buildWorkflowFacade(
            ...     laser_aliases={'488': '488 (EXC) sn27311',
            ...                    '405': '405 (ACT) sn26647'},
            ...     detector_name='Kiralux',
            ...     xy_positioner_name='XY',
            ...     z_positioner_name='Z',
            ...     hwp_name='HWP', qwp_name='QWP',
            ... )
            >>> facade.laser_con.set_constant_power(['488'], [50.0])
        """
        return self.build(**kwargs)


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
