*********
Scripting
*********

Imswitch2 provides a scripting module that can be used to automate
tasks in the software.  This scripting module lets you write Python
code that interacts with Imswitch2 at runtime.

See the scripting API reference for the available API modules and
methods.  In addition to the module APIs, a set of global helper
functions is documented :doc:`here <api/_actions>`.

The API modules may provide signals – events that can be bound to via
e.g. the global ``getWaitForSignal`` scripting function.

Step-by-step tutorials ship with the scripting module; see
:ref:`scripting-tutorials` below.

Threading model and waiting for events
======================================

A script runs on its own thread. Every ``api.*`` call that touches the
GUI runs on the GUI thread and **blocks the script until it has run**,
returning the function's value or raising its exception in the script.
Statements therefore execute in order, and ``x =
api.imcontrol.snapImage(output=True)`` really yields an array.

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
raises on refusal, failure or timeout. A start that is refused (a scan is
already running, the previous one is still finishing) raises
``ScanRequestRejectedError`` immediately; no ``scanEnded`` follows for it.
On rigs with several scanners, ``getScanSourceNames()`` lists the choices
for ``runScan(source=...)``.

**Stopping a script.** Pressing *Stop* (or *Run* while a script runs, or
closing ImSwitch) delivers ``OperationCancelled`` **once**, at the script's
next wait (``getWaitForSignal``, ``sleep``, ``waitUntil``,
``runScanAndWait``, ``handle.wait`` or any API call). Do not catch it. Your
``finally`` blocks then run inside a *cleanup window* of 30 seconds in
which waits and API calls work normally, so a recording can still be
stopped and a stage parked; when the window expires, cancellation re-arms.
A script that never reaches a wait (``while True: pass``, or a long
``time.sleep``) is interrupted after 2 seconds — use ``sleep()`` rather
than ``time.sleep`` to stop promptly. ``stopRecording()`` returns whether a
recording was active, so cleanup can decide whether to wait for
``recordingEnded``::

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

``tutorial/scanning/05_laser_power_series.py`` is this pattern in full,
runnable on a mock setup.

.. _scripting-tutorials:

Tutorials
=========

ImSwitch copies its scripts into ``~/ImSwitchConfig/scripts``
(``Documents\ImSwitchConfig\scripts`` on Windows) on first start. The
``tutorial`` folder there is a sequence of short scripts that build on each
other. Each is written for a *simulated setup*: a setup file, shipped with
ImSwitch, whose cameras, stages, scanners and lasers are all mock devices,
so the tutorials run without a microscope. The header of every tutorial
says what it teaches, which setup file to load (``Mock setup:``; choose it
with **Tools > Pick hardware setup…** in the Hardware Control tab), what
that setup simulates (``It simulates:``), and what your own microscope
needs for the script (``Your own microscope:``). ``tutorial/README.md``
explains how to load a setup and asks you to read each script, predict
what it will do, and only then press **Run all**.

``tutorial/basic``
    One camera (``example_mock.json``, steps 01-06): snapping into numpy
    and to files, camera settings and the live view, recording a number of
    frames and reading the file back, clean-up that survives the Stop
    button, and sharing code between scripts with ``importScript()``. Then
    two cameras and stages (``example_no_hardware.json``, steps 07-08).

``tutorial/scanning``
    Scans that trigger a camera, one frame per scan position, and scan
    timelapses (``hamamatsu_mock_scan_setup.json``, steps 01-03); a camera
    and an APD in one scan (``mixed_hamamatsu_apd_mock_scan_setup.json``);
    and a laser power series in one recording
    (``galvo_apd_mock_scan_setup.json``).
    Scan settings are loaded from files in ``scanning/scan_params``; make
    your own in the Scan widget and save them with
    ``api.imcontrol.saveScanParamsToFile()``.

``workflows``
    Complete acquisitions for real rigs -- see the cookbooks below. These
    need the hardware they name.

``tutorial/README.md`` lists every step. The test suite runs each tutorial
on its mock setup (``imswitch/imscripting/_test/test_shipped_tutorials.py``),
so they are kept working.

Only missing files are copied, so an existing ``scripts`` folder keeps its
own copies: after an update, the tutorials are added next to them, and
scripts that were removed from ImSwitch stay until you delete them.

Workflow Scripting Cookbooks
=============================

For headless, testable acquisition workflows that run independently of
the GUI, see:

* :doc:`scripting-wfs-workflows` — General pattern for scripting
  acquisition workflows, with WidefieldSTARSS as the worked example.
  Covers the facade pattern, parameter customization, device-name mapping,
  composite workflows, and mock testing.

* :doc:`scripting-time-resolved-workflows` — Time-resolved detector
  workflows for photon-counting products (TCSPC cubes, gated STED,
  tau-STED). Explains the generic time-resolved detector contract with
  Swabian TimeTagger as the worked example.
