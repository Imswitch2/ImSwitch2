*********
Scripting
*********

ImSwitch2 provides a scripting module that can be used to automate
tasks in the software.  This scripting module lets you write Python
code that interacts with ImSwitch2 at runtime.

The scripting API reference lists the available modules and methods:
:doc:`api/api.imcontrol` for microscope control and
:doc:`api/mainWindow` for the main window.  In addition to the module
APIs, a set of global helper functions is documented
:doc:`here <api/_actions>`.

The API modules may provide signals – events that can be bound to via
e.g. the global ``getWaitForSignal`` scripting function.

There are example scripts under the scripting module to see how the
scripting functionality works in action.

Threading model and waiting for events
======================================

A script runs on its own thread. Every ``api.*`` call that touches the
GUI runs on the GUI thread and **blocks the script until it has run**,
returning the function's value or raising its exception in the script.
Statements therefore execute in order, and ``images =
api.imcontrol.snapImage(output=True)`` really holds the frames when it
returns: a dict that maps each captured detector's name to its array.

**Create the waiter before the trigger.** ``getWaitForSignal`` only
listens from the moment it is created; an emission that happened earlier
is never seen. Either create the waiter first::

    waitForScanToEnd = getWaitForSignal(api.imcontrol.signals().scanEnded, timeout=600)
    api.imcontrol.runScan()
    waitForScanToEnd()

or use ``callAndWaitForSignal``, which does exactly that in one line and
also catches a signal emitted synchronously inside the call::

    callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
                         api.imcontrol.stopRecording, timeout=60)

**Scans have an exact completion.** ``api.imcontrol.runScan()`` returns a
handle bound to that scan: ``handle.wait(timeout)`` returns ``True`` when
it has ended (``False`` on timeout), and ``handle.successful`` /
``handle.message`` say how. ``runScanAndWait(timeout)`` wraps both and
raises on refusal, failure or timeout. A start that is refused raises
``ScanRequestRejectedError`` immediately, with the reason as its message: a
scan is already running, the previous one is still finishing, or the scan
manager refused the design -- a scan longer than the setup's
``scan.maxScanTimeMin``, say, or one that drives a scanner outside its
voltage range. A refused start never runs, so there is no end to wait for.
On rigs with several scanners, ``getScanSourceNames()`` lists the choices
for ``runScan(source=...)``.

**Stopping a script.** Pressing *Stop* (or *Run* while a script runs)
delivers ``OperationCancelled`` **once**, at the script's
next wait (``getWaitForSignal``, ``sleep``, ``waitUntil``,
``runScanAndWait``, ``handle.wait`` or any API call). Do not catch it. Your
``finally`` blocks then run inside a *cleanup window* of 30 seconds in
which waits and API calls work normally, so a recording can still be
stopped and a stage parked; when the window expires, cancellation re-arms.
A script that never reaches a wait (``while True: pass``) is interrupted
after 2 seconds. The interruption lands between two Python statements, so
it cannot cut a blocking call short: a long ``time.sleep`` still runs to
its end. Use ``sleep()`` rather than ``time.sleep`` to stop promptly.

Closing ImSwitch2 while a script runs is stricter: the API is shut
first, so ``api.*`` calls in ``finally`` blocks raise ``RuntimeError``
("API call refused: ImSwitch is shutting down"), and the script has
10 seconds to end. If it is still running then, ImSwitch2 exits without
shutting down the hardware managers. Stop a script, and let its cleanup
finish, before closing the application.

``stopRecording()`` returns whether a recording was active, so cleanup
can decide whether to wait for the end of the recording
(``recordingEnded``, or ``recordingFailed`` if it failed)::

    api.imcontrol.setRecModeUntilStop()
    try:
        callAndWaitForSignal(api.imcontrol.signals().recordingStarted,
                             api.imcontrol.startRecording, timeout=30)
        for power in (0, 5, 10, 50):
            api.imcontrol.changeScanPower(laser, power)
            runScanAndWait(timeout=600)
    finally:
        waitForRecordingToEnd = getWaitForSignal(api.imcontrol.signals().recordingEnded, timeout=60)
        if api.imcontrol.stopRecording():
            waitForRecordingToEnd()

``example_scan_power_series.py`` under the scripting module is this
pattern in full.

Workflow Scripting Cookbooks
=============================

For acquisition workflows that run from a script without touching
widgets, and can be unit-tested against a mock facade, see:

* :doc:`scripting-wfs-workflows` — General pattern for scripting
  acquisition workflows, with WidefieldSTARSS as the worked example.
  Covers the facade pattern, parameter customization, device-name mapping,
  composite workflows, and mock testing.

* :doc:`scripting-time-resolved-workflows` — Time-resolved detector
  workflows for photon-counting products (TCSPC cubes, gated STED,
  tau-STED). Explains the generic time-resolved detector contract with
  Swabian TimeTagger as the worked example.

To reconstruct or process data from a script, import
``imswitch.improcess.workflows`` and run an ImProcess workflow; see
:doc:`improcess-workflows`.

.. toctree::
    :hidden:

    scripting-wfs-workflows
    scripting-time-resolved-workflows
