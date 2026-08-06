***************************************
Scan lifecycle and detector acquisition
***************************************

How Imswitch2 tracks *which controller is running a scan*, *which detectors
take part in it*, and *when it is safe to tear the scan down*.  Consumers such
as BeadRec resolve scan geometry and running-state from the same mechanism.

Two layers are described here, and they answer different questions:

* The **active scan source** (``ScanLifecycleMixin`` +
  ``CommunicationChannel``) answers *who is scanning*.
* The **scan-execution coordinator** (``ScanExecutionCoordinator``) answers
  *may this scan start*, *which detectors belong to it*, and *when may it be
  released* — the detector-acquisition-lease model.

Design records for the lease work live in
``docs/design/plans/detector-acquisition-selection.md``.


Why this exists
===============

Several widgets need to know, at runtime, which controller is currently
running a hardware scan:

* ``BeadRecController`` polls ``isScanRunning()`` from its worker thread and
  reads scan dimensions / step sizes / frames-per-pixel on every scan start.
* Recording orchestration and display pipelines have the same question.

Historically ``CommunicationChannel`` answered by **guesswork**:

1. Try the controller registered under the hardcoded widget key ``'Scan'``
   (only the NIDAQ family registers there).
2. Otherwise iterate *all* registered controllers and return the **first** one
   implementing the ``BeadRecScanSource`` protocol.

That had two failure modes:

* **Silent death for new scanners.** A scan controller registered under its
  own widget key (every TriggerScope controller) was invisible to step 1; if
  it also lacked the protocol, BeadRec went silently inactive.
* **Wrong source with multiple implementers.** Step 2 returns whichever
  compatible controller happens to come first in the registration dict —
  regardless of which one is actually scanning.  Registration order decided
  correctness.

The root cause: the channel tried to *pull* a fact by iteration that is known
with certainty in exactly one place — the controller that started the scan.
The fix is to *push* it.


The active scan source
======================

ScanLifecycleMixin
------------------

Every scan controller already maintains a ``self.isRunning`` flag: ``True`` at
the top of ``runScanAdvanced()``, ``False`` in ``scanDone()``,
``scanFailed()`` and exception handlers.  The mixin
(``controller/basecontrollers.py``) turns that flag into a property whose
setter announces the transition to the channel:

.. code-block:: python

    class ScanLifecycleMixin:
        _isRunningFlag = False

        @property
        def isRunning(self) -> bool:
            return self._isRunningFlag

        @isRunning.setter
        def isRunning(self, value: bool) -> None:
            self._isRunningFlag = bool(value)
            if self._isRunningFlag:
                self._commChannel.setActiveScanSource(self)
            else:
                self._commChannel.clearActiveScanSource(self)

**Why a property instead of explicit announce calls:** the audit found ~30
assignment sites across 11 controllers, including inside ``except`` blocks.
The property absorbs every existing and future assignment, so no site can be
forgotten — a controller cannot start a scan "secretly" as long as it
maintains ``isRunning``, which all of them already do.

**Why this is safe at construction time:** ``ImConWidgetController.__init__``
assigns ``self._commChannel`` before any subclass ``__init__`` body runs, so
the first ``self.isRunning = False`` in a controller's constructor finds the
channel.  That initial ``False`` is a no-op clear (see the identity guard
below).

Active-source API
-----------------

.. code-block:: python

    setActiveScanSource(controller)    # on isRunning -> True; last wins
    clearActiveScanSource(controller)  # on isRunning -> False; identity-guarded
    getActiveScanSource()              # the running controller, or None
    isScanRunning()                    # _activeScanSource is not None

The **identity guard** in ``clearActiveScanSource`` only clears when the
caller *is* the active source.  This protects against two real situations:

* A controller constructed while another controller is scanning initializes
  ``isRunning = False`` — it must not evict the running scan.
* Controller A's scan is superseded by controller B (last-announce-wins); when
  A later flips its flag to ``False``, B's announcement survives.

These are plain methods, not Qt signals — no change to the channel's signal
inventory, and reads from the BeadRec worker thread are single-reference loads
(atomic enough under CPython, the same guarantee as the previous ``isRunning``
attribute read).

Metadata resolution order
-------------------------

``getDimsScan()``, ``getScanStepSizes()``, ``getNumLineSteps()``,
``getFramesPerScanPixel()`` and ``getBeadRecScanSource()`` resolve in this
order:

1. **Active scan source** — if it satisfies the ``BeadRecScanSource`` protocol
   (see ``controllers/_beadrec_scan_source.py``) and reports
   ``isBeadRecCompatible() == True``.
2. **The** ``'Scan'`` **widget key** — legacy path; covers
   ``ScanControllerAdvanced`` / ``ScanControllerMoNaLISA``, which expose the
   legacy accessors but not the protocol.
3. **First-compatible iteration** (``getBeadRecScanSource``) — idle fallback
   only, e.g. when the user presses BeadRec *Run* before starting a scan.

BeadRec reads its parameters on ``sigScanStarted``, which always fires *after*
the controller set ``isRunning = True``.  During a scan the answer therefore
always comes from the controller actually scanning; the iterate-and-guess path
only ever serves idle reads.

Unchanged and out of scope: ``getNumScanPositions()``, ``getNumCamTTL()`` and
``getNextAxial()`` stay on the ``'Scan'`` key (consumed by
``RecordingController`` and the MoNaLISA axial workflow only).


The scan-execution coordinator
==============================

``isRunning`` answers *who is scanning*.  It does not answer *may this scan
start*, *which detectors belong to it* or *when is it safe to tear down*.
Those live in a single shared, framework-free coordinator
(``model/managers/_scan_execution.py``, obtained through
``getSharedScanExecutionCoordinator`` and created by ``MasterController``).

It cannot live on ``SuperScanController``, because there are **five** direct
``nidaqManager.runScan`` callers — the four ``SuperScanController`` subclasses
and ``EtSTEDTriggeredScanRunner`` — so a base-class helper would miss one.
Today exactly one call site in the codebase reaches ``runScan``, inside the
coordinator.

Runs and iterations
-------------------

Two deliberately distinct levels:

.. list-table::
   :widths: 18 32 50
   :header-rows: 1

   * - Level
     - Token
     - Spans
   * - **Scan run**
     - ``ScanRunToken`` (``reserveRun`` / ``releaseRun``)
     - One user-initiated scan *including* every repeat iteration, MoNaLISA ``autoAxial`` follow-up and ``isNonFinalPartOfSequence`` part.
   * - **Scan iteration**
     - ``ScanIterationToken`` (``arm`` / ``resolve``)
     - One ``runScan`` → ``scanDone`` cycle, i.e. one frame.

The run reservation is what stops another entry point from arming *in the gaps
between* repeat frames, where no iteration is in flight.  Re-reserving by the
same owner is idempotent; a different owner is refused with ``ScanBusyError``
(a ``NidaqManagerError`` subclass).  A refusal starts nothing and emits no
lifecycle signal.

Arming an iteration
-------------------

``arm(...)`` (NI-DAQ) and ``armWithStarter(...)`` (autonomous firmware)
perform the same four operations:

1. **Composes the participant snapshot** —
   ``(selected scan-driven detectors) | (scan-driven detectors held by an
   override purpose)``, minus faulted ones.  A detector whose stop failed
   never rejoins a scan.  Override purposes (``RECORDING``, ``SNAP``,
   ``WORKFLOW``, ``EVENT_STREAM``, ``EVENT_DIRECT``, ``GENERIC``) win over
   deselection: deselecting is a preference, an active recording is a
   commitment.  Deferred selection changes are flushed here — the one point
   where a new selection takes effect without disturbing an iteration already
   in flight.
2. **Takes the** ``SCAN`` **lease** on those detectors
   (``DetectorsManager.acquire``).
3. **Records the snapshot on the iteration token.**  NI-DAQ additionally
   injects it into ``scanInfoDict`` as ``participants`` and
   ``excludedDetectors`` because detector managers and its simulator consume
   that dict. Autonomous firmware backends do not fabricate an NI-DAQ-shaped
   scan dictionary.
4. Invokes the backend start operation: ``nidaqManager.runScan`` through
   ``arm(...)``, or the callback supplied to ``armWithStarter(...)``.

Any failure — busy refusal, build failure, arm exception — resolves the token
and unwinds the lease before propagating, so a refused arm cannot strand
ownership.  Completion runs **exactly once** per iteration across all five
terminations: busy refusal, build failure, arm exception, normal completion
and abort.

The finish barrier
------------------

``resolve(token, mode, onComplete=...)`` asks every participant
``finishScan(mode, acknowledge)`` and then *waits asynchronously* for their
acknowledgements before releasing the ``SCAN`` lease — the call never blocks
the UI thread.  ``mode`` is ``'graceful'`` or ``'abort'``.

Point detectors integrate a whole scan and publish their frame at the very
end, so releasing the lease at ``sigScanDone`` used to tear the worker down
mid-read and lose that frame.

``finishScan`` runs for every participant regardless of refcount — a Time
Tagger that also holds a ``WORKFLOW`` lease goes 2→1, so no hardware stop
happens, but it still needs its final read.  Hardware stop happens only at
aggregate zero.  It is implemented by ``APDManager``, ``PMTManager`` and
``SwabianTimeTaggerManager``; the ``DetectorManager`` base acknowledges
immediately.

A 5 s deadline (``DEFAULT_FINISH_TIMEOUT_S``, armed through an injected
scheduler — ``QTimer.singleShot`` in the GUI) releases anyway rather than
wedging the GUI: losing one final frame beats a stuck application.

``onComplete`` fires when the barrier clears, on whichever thread acknowledged
last.  **``scanDone`` hangs off this**, which is why the sequence below
publishes completion after the barrier rather than directly from a backend's
done signal.


Scan lifecycle sequence
=======================

.. code-block:: text

    user / external trigger
      └─ runScanAdvanced()
           ├─ coordinator.reserveRun(self) ─► ScanRunToken   (ScanBusyError if
           │                                  another owner holds the run)
           ├─ isRunning = True  ──────────► channel.setActiveScanSource(self)
           ├─ sigScanStarting               (unless already emitted by trigger)
           ├─ sigScanDevicesResolved        (laser/device membership, before
           │                                 detector acquisition/backend start)
           ├─ sigScanBuilt(deviceList)      (NIDAQ: relayed from nidaqManager;
           │                                 TriggerScope: compatibility event)
           ├─ coordinator.arm(...) or
           │  coordinator.armWithStarter(...) ─► participants + SCAN lease,
           │                                      then backend start
           ├─ hardware starts
           └─ sigScanStarted                (relayed from nidaqManager /
                                             scanManager — active source is
                                             already set at this point)
      ... scan runs; consumers poll isScanRunning(), read getDimsScan() etc. ...
    execution backend reports done / build failure
      └─ coordinator.resolve(token, graceful|abort)      ← finish barrier
           ├─ finishScan(mode, ack) to every participant
           ├─ wait (async) for all acknowledgements, or 5 s deadline
           ├─ release the SCAN lease
           └─ onComplete ─► scanDone() / scanFailed()   (re-queued onto the
                ├─ isRunning = False ─────► clearActiveScanSource(self)
                ├─ sigScanDone              controller's own thread)
                └─ sigScanEnded             (skipped for non-final parts;
                                             run reservation released here)

Only the controller that *armed* the iteration reacts: ``tokenForOwner(self)``
returns ``None`` for everyone else, so a board-wide NI-DAQ or TriggerScope done
signal cannot make a bystander publish an early ``sigScanEnded``.

Repeat mode
-----------

Repeat does **not** re-enter the scan machinery synchronously.  ``scanDone``
runs inside the just-finished scan's task-completion slot; calling
``runScanAdvanced`` from there recreated NI-DAQ tasks, ``WaitThread``\ s and
per-detector scan ``QThread``\ s while the previous ones were still tearing
down — a ``QThread`` destroyed while still running, which crashed the GUI on
real hardware every time *Repeat* was enabled.

``_armRepeatScan`` therefore defers the next frame with
``QTimer.singleShot(0, self._fireRepeatScan)`` so the current signal chain
unwinds first.  ``_repeatPending`` is cleared by ``abortScan`` /
``scanFailed``, and ``_fireRepeatScan`` re-checks ``_shouldContinueRepeat()``,
so aborting or un-checking *Repeat* in that gap cancels cleanly and
terminalizes exactly as ``scanDone`` would have.

``isScanRunning()`` is briefly ``False`` between repeat frames, but the **run
reservation is retained**, so no other entry point can arm in the gap.


Contract for new scan controllers
=================================

A new controller that runs hardware scans MUST:

1. Inherit ``ScanLifecycleMixin`` — directly, via ``SuperScanController``, or
   via a backend lifecycle mixin such as ``TriggerScopeScanLifecycleMixin``:

   .. code-block:: python

       from ..basecontrollers import ImConWidgetController, ScanLifecycleMixin

       class MyNewScanController(ScanLifecycleMixin, ImConWidgetController):
           ...

2. Maintain ``self.isRunning`` around its scan lifecycle: ``True`` when
   starting, ``False`` on done / failure / error — including in exception
   handlers of ``runScanAdvanced()``.

3. Emit the channel scan signals (``sigScanStarting``,
   ``sigScanDevicesResolved``, ``sigScanBuilt``, ``sigScanStarted``,
   ``sigScanDone``, ``sigScanEnded``) following the sequence above.

4. **Never call a hardware scan manager directly.** Reserve the run with
   ``self._scanCoordinator.reserveRun(self)`` and arm each iteration through
   ``arm(...)`` for NI-DAQ or ``armWithStarter(...)`` for another backend.
   Treat ``ScanBusyError`` as a refusal rather than starting hardware anyway.
   Broadcast candidates that lose the reservation race should log that
   refusal at debug level, not report a hardware failure.
   ``SuperScanController`` and ``TriggerScopeScanLifecycleMixin`` already
   implement their respective paths.

5. **Publish completion from the finish barrier, not from a backend signal.**
   Resolve the iteration with ``coordinator.resolve(token, mode,
   onComplete=...)`` and call ``scanDone()`` / ``scanFailed()`` from
   ``onComplete``.  Guard with ``tokenForOwner(self)`` so a broadcast
   completion signal is ignored by non-owners, and re-queue ``onComplete``
   onto the controller's own thread — the last acknowledgement can arrive on a
   detector worker thread.

6. **Re-arm repeats deferred**, via ``_armRepeatScan()`` or an equivalent
   zero-delay timer — never synchronously from ``scanDone``.

7. *(Optional, for BeadRec support)* additionally inherit
   ``BeadRecScanSourceMixin`` and implement ``getBeadRecScanDims()`` /
   ``getBeadRecStepSizes()``; override ``isBeadRecCompatible()`` to return
   ``False`` if the frame stream does not map to a 2D raster.

A **scan-driven detector manager** (one whose ``isScanDriven`` is ``True``)
has a matching obligation: override ``finishScan(mode, acknowledge)`` if it
has end-of-scan work — a final read, a last frame to publish — and call
``acknowledge()`` when that work is done, from any thread.  The base
implementation acknowledges immediately, which is correct only for managers
with nothing outstanding.  Not acknowledging costs a 5 s stall per iteration
and then proceeds without you.

.. note::

   **Enforcement.** The adoption-audit test in
   ``imswitch/imcontrol/_test/unit/test_scan_lifecycle.py``
   (``test_every_scan_controller_inherits_scan_lifecycle_mixin``) AST-scans
   every controller module.  Any class that assigns ``self.isRunning`` or
   defines ``runScanAdvanced`` without inheriting the mixin (transitively)
   fails CI with a message pointing at this page.  Forgetting the contract is
   a test failure, not a silent runtime defect.


Scan controller inventory
=========================

Audited June 2026.

**NIDAQ family** — inherit ``SuperScanController`` (lifecycle-aware via its
bases), registered under widget key ``'Scan'`` as
``ScanController{scanWidgetType}`` (``ImConMainController``):

.. list-table::
   :widths: 40 20 40
   :header-rows: 1

   * - Controller
     - scanWidgetType
     - BeadRec support
   * - ``ScanControllerBase``
     - ``Base``
     - protocol (``BeadRecScanSourceMixin``)
   * - ``ScanControllerPointScan``
     - ``PointScan``
     - none
   * - ``ScanControllerAdvanced``
     - ``Advanced``
     - legacy accessors via ``'Scan'`` key
   * - ``ScanControllerMoNaLISA``
     - ``MoNaLISA``
     - legacy accessors + axial workflow

**TriggerScope family and standalone** — inherit
``TriggerScopeScanLifecycleMixin`` alongside ``ImConWidgetController``,
registered under their own widget keys.
Scan start is relayed from ``ScanManagerTriggerScope.sigScanStarted``,
completion from the **board-level** ``sigScanDone`` shared by all TriggerScope
controllers — each one's ``scanDone()`` ignores it unless it owns the active
coordinator iteration:

.. list-table::
   :widths: 60 40
   :header-rows: 1

   * - Controller
     - BeadRec support
   * - ``TriggerScopeRasterController``
     - full protocol (the Snouty BeadRec target)
   * - ``TriggerScopeScanController`` (unified RESOLFT façade)
     - none
   * - ``TriggerScopePLSRController``
     - none
   * - ``TriggerScopePLSRMulticolorController``
     - none
   * - ``TriggerScopeLSXYRController``
     - none
   * - ``TriggerScopeGalvoDetectionController``
     - none
   * - ``LightSheetMulticolorController``
     - none

TriggerScope firmware has no mid-iteration abort command.  A board
``sigScanDone`` therefore means that the autonomous iteration physically
completed, even when an ImSwitch stop is pending.  Participants finish in
``graceful`` mode so APD/PMT/TimeTagger managers can publish the final read;
the pending stop instead prevents repeat or sequence continuation.  A stop
received re-entrantly while ``sigScanDone`` is being published is retained
and terminalizes the run before a non-final ScanLapse part can re-arm.
Auto-stop recording is likewise a run-terminal action, never a per-part
sequence action.

Binding a recording to the right scanner
----------------------------------------

A rig can register several TriggerScope scan widgets, and a scan-once
recording on such a setup is *armed* and then started from whichever scan
widget the operator chooses.  The frame expectation therefore cannot be read
when REC is pressed — there is no scanner yet, and resolving one by
registration order silently binds the recording to whichever controller
happens to implement the accessors.

``_startTriggerScopeScan`` instead calls
``scanWorkflow.prepare_recording_for_scan(self)`` **before** it announces
itself as the active source, before ``sigScanStarting`` and before any
hardware write.  The RecordingController fills in ``recFrames``,
``numCamTTL``, ``scanDims`` and ``scanStepSizes`` from that exact controller
and arms its manager there.  The hook is synchronous and may refuse: a
recording that cannot be armed vetoes the scan rather than letting it bleach
the sample with nothing recording.  A refused *new* run hands the reservation
straight back without publishing any boundary; a refused *continuation*, whose
start was already published, is terminalized normally.

Every controller a recording can be bound to must therefore answer
``getNumScanPositions`` and ``getNumCamTTL``.  The RESOLFT-family modes report
``roSteps × cycleSteps × timeLapsePoints`` through
``TriggerScopeScanGeometryMixin``; ``TriggerScopeRaster`` keeps its own, since
its frame count is a pixel grid.  ``TriggerScopeScanController`` overrides the
mixin's parameter hook to read the mode its widget is showing.

A *timelapse* scan is started by the recording itself, so no operator gesture
identifies the scanner.  The Recording widget's **Scan source** chooser
supplies it, and it appears only when the setup has more than one capable
controller.  Its first entry is a valueless ``Select scan source...``
placeholder and a timelapse is refused while it is showing: repopulating a
combo box selects index zero, so a saved choice that disappeared — or a rig
seen for the first time — would otherwise arm whichever scanner happens to be
registered first, and that scanner's hardware would then actually run.

Geometry for both modes is read through ``_scanAccessor``, which binds to the
pinned ``_recordingScanSource`` rather than the CommunicationChannel
accessors.  Those resolve globally and are unambiguous only while a scan is
running, which is never true at the moment a recording needs its frame count.
When a pinned source does not implement an *optional* accessor
(``getDimsScan`` / ``getScanStepSizes``, which only the BeadRec-capable raster
controller provides) the result is ``None`` rather than a fallback: an
uncalibrated recording is correct, one labelled with another scanner's
dimensions is not.  The two required accessors have no fallback either — a
source that cannot report its frame count fails the recording outright.

Because a late-bound recording does not publish ``sigScanStarting`` itself,
``_scanStartOwnedByScanSource`` records that its scan source owns the
run-level lifecycle.  Without it, a scanner that fails after the manager was
armed would clear ownership on ``sigScanEnded`` without ever reaching the
failure terminal, and the writer would sit waiting for frames until its stall
watchdog fired.

**Not scan sources** — they orchestrate around scans but never own the
lifecycle, and are deliberately excluded: ``EtSnoutyController`` (an
event-triggered workflow that *requests* scans via
``sigRunScanTriggerScopePLSRMulticolor``) and ``RotationScanController``
(steps rotators between scans run by others).


How BeadRec consumes this
=========================

* ``BeadWorker`` polls ``commChannel.isScanRunning()`` in its acquisition loop
  — now answered by the active source for *any* scanner family, so the worker
  no longer dies on setups without a ``'Scan'`` widget (e.g. Snouty).
* **Frames come via** ``DetectorManager.readChunk('BeadRec')``, the
  multi-consumer chunk distributor.  The raw ``getChunk()`` is a destructive
  read; when the ``RecordingManager`` polled the same camera during a
  scan-once recording, BeadRec and the recording each received a random subset
  of the frames — both incomplete.  ``readChunk`` drains the hardware once and
  gives every registered consumer a full copy; consumers release their queue
  when done (``releaseChunkConsumer``).  BeadRec also only reads the
  **current** detector, so the right camera must be selected in the view.

  All destructive frame consumers go through ``readChunk`` (consumer keys
  ``'RecordingManager'``, ``'BeadRec'`` and ``'WorkflowFacade'`` for the WFS
  workflow camera facade).  Components that only *peek* — live view, focus
  lock, autofocus, EtSnouty event detection, tiling preview — use the
  non-destructive ``getLatestFrame`` and need no registration.  An enforcement
  test (``test_detector_chunk_consumers.py``) fails CI if new production code
  calls ``.getChunk()`` directly outside the detector layer.
* On ``sigScanStarted``, ``BeadRecController.updateParameters()`` reads
  ``getDimsScan()`` / ``getScanStepSizes()`` / ``getFramesPerScanPixel()`` —
  all resolved from the announcing controller. Advanced Scan currently reports
  one BeadRec frame per camera-enabled line step; it does not count multiple
  camera pulses inside one line step. Ordinary Scan-once recording separately
  counts the actual TTL rising edges and is not subject to that BeadRec-only
  assumption. See :doc:`advanced-scanning`.
* If no scan source exists at all, ``getDimsScan()`` still raises
  ``RuntimeError`` and BeadRec logs its one-shot "inactive" warning;
  ``isScanRunning()`` returns ``False`` instead of raising.

Hardware caveats for BeadRec and TriggerScope raster
----------------------------------------------------

Verified June 2026.

* **The deployed firmware pulses TTL lines 0–3 together.**  The TriggerSwitch
  0.1 sketch (``runPixelCycle()`` in the local Triggerscope repo) contains a
  "temporary fix" that drives TTL 0–3 in a single window per pixel, using the
  earliest TTL row's start/end (``p1StartUs`` / ``p1EndUs``).  The per-device
  line selection (``p1Line``) and all other TTL rows are **ignored** — laser
  emission during raster scans is controlled purely by arming
  (``sigScanDevicesResolved`` → ``setScanModeActive``), not by the TTL table.
  The
  firmware parameter parser already supports three independent pulses
  (p1/p2/p3); restoring the commented-out per-line code in ``runPixelCycle()``
  would enable real per-device pulse windows.  Note for that fix: the parser
  truncates start/end to ``uint16_t`` (max ~65.5 ms), and the panel TTL labels
  versus the firmware's 0-based ``ttl[]`` indexing must be reconciled.
* **Cameras must be armed to see triggers.**  A detector acquires only while
  something holds a lease on it; an idle externally-triggered camera ignores
  all TTL pulses.
* **Dwell time must exceed camera exposure + readout**, otherwise triggers
  arriving during readout are dropped (~half the frames, never exactly).  The
  raster controller logs a warning with the exact numbers at scan start.
  128×128 px at 5 ms dwell is confirmed working on the Snouty rig.
* **Unidirectional raster assumed.**  BeadRec fills its reconstruction buffer
  row-major.  If the firmware ``RASTER_SCAN`` is bidirectional, every other
  line appears mirrored.
* **Pixel-count convention** is ``round(axis_length / axis_step_size)``,
  matching the raster widget's steps display.
* **Trailing pixels.**  The firmware completion boundary enters the same
  detector finish barrier as NI-DAQ. Scan-driven detectors publish their final
  read and acknowledge it before the lease and run terminal are released.


Edge cases and known limits
===========================

* **Concurrent scans** are now *refused*, not merely reported.  A second entry
  point trying to reserve a run or arm an iteration while another owner holds
  one gets ``ScanBusyError``, including in the gaps between repeat frames.
  The channel-level rule still applies as a backstop for controllers outside
  the coordinator: last announcement wins, and the identity guard ensures the
  earlier controller's teardown cannot clear the newer scan.
* **Cont-laser-pulses mode** (``ScanControllerBase``): ``isRunning`` is set
  even though scan signals are suppressed, so ``isScanRunning()`` reports
  ``True`` during continuous pulsing — identical to pre-refactor behavior.
* **A controller bypassing** ``isRunning`` cannot announce at all; this is
  exactly what the adoption-audit test guards.


Test coverage
=============

Active-scan-source layer:

* ``test_scan_lifecycle.py`` — mixin behavior (announce / withdraw / identity
  guard / last-wins), the adoption audit, and channel contract assertions.
* ``test_beadrec_scan_source.py`` — BeadRec protocol/mixin defaults and the
  active-source-first resolution contract.
* ``test_communication_channel_contract.py`` — signal inventory unchanged (the
  active-source API is methods, not signals).

Coordinator and lease layer:

* ``test_scan_execution_coordinator.py`` — arm/resolve exactly-once across all
  five terminations, backend-neutral start, participant composition, finish
  barrier and its timeout.
* ``test_triggerscope_scan_lifecycle.py`` — TriggerScope family adoption,
  membership-before-start ordering, board-signal ownership, detector finish
  barrier, graceful stop, completion-publication races, non-final sequence
  retention, start refusal, terminal idempotence and repeat reservation.
* ``test_scan_busy_refusal.py`` — a refused arm starts nothing, emits no
  lifecycle signal and strands no ownership.
* ``test_scan_repeat_rearm.py`` — repeat defers instead of running
  synchronously; abort / un-check / already-running cancel the pending frame.
* ``test_detector_selection.py`` — selection, override purposes, deferred
  application at the next iteration, and back-compatibility with managers that
  have no selection support.
* ``test_acquisition_leases.py``, ``test_detectors_manager_leases.py``,
  ``test_acquisition_concurrency.py`` — lease refcounting, the detector stop
  contract, and concurrency regressions.

All paths are relative to ``imswitch/imcontrol/_test/unit/``.
