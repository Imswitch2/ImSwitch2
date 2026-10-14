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
:doc:`here <api/_actions>`.  The pages are generated from the code by
``tools/generateapidocs.py``; a test fails when they are older than the
code.

The API modules may provide signals – events that can be bound to via
e.g. the global ``getWaitForSignal`` scripting function.

Step-by-step tutorials ship with the scripting module; see
:ref:`scripting-tutorials` below.

This module drives the microscope.  To script what you do to *results* in
ImProcess (a one-off transformation, a recipe for a folder of recordings, a
console over the results list) see :ref:`improcess-python-step`.

Working interactively
=====================

The editor runs code in two ways:

*Run all*
    runs the whole script in a **fresh namespace**: nothing is left over
    from earlier runs, so a script that works with *Run all* works the same
    way every time -- for a colleague, in a workflow, tomorrow.

*Run cell* (Ctrl+Enter) and *Run selection* (F9)
    run part of the script in the **session**, a namespace that lives on
    between runs. A cell runs from a ``# %%`` line to the next (Shift+Enter
    runs it and moves to the next cell); Run selection runs the selected
    lines, or the line the cursor is on. What one run defines is there for
    the next: acquire in one cell, then analyse, change a number and
    analyse again without acquiring again. Tracebacks name the editor's own
    line numbers.

The **console** works in the same session, for looking at results --
``frames.shape``, ``frames[3].mean()``. It is disabled while a script runs,
since the script may be changing those variables, and it runs on the GUI
thread, where the blocking calls (``recordScanToMemory``,
``recordFramesToMemory``) refuse to run: acquire from the editor. The
**Variables** panel lists the session's data with its type, shape, dtype and
size in memory; it is updated when a run ends and after each console
command. **Reset session** forgets every variable (after asking, when there
are some); it is not available while a script runs.

``show(array, name)`` puts an array into ImControl's image view as a layer of
that name; showing under the same name again replaces the picture, so a loop
can update one image. A stack gets a slider, ``scale`` gives the pixel size,
and a dict of arrays shows one layer per entry. Double-clicking an array in
the Variables panel does the same::

    # %% acquire
    frames = api.imcontrol.recordFramesToMemory('Camera', 20)

    # %% analyse -- change the threshold, Ctrl+Enter, look again
    mean = frames.mean(axis=0)
    show(mean > mean.mean() + 2 * mean.std(), 'bright pixels')

``tutorial/basic/11_work_interactively.py`` is this in full.

**From the GUI to a script.** Set up an acquisition in ImControl's widgets,
then choose **File > New script from current settings** (Ctrl+Shift+N) in
the Scripting tab. A new, unsaved script opens that sets everything again
and acquires:

* the detectors the Recording widget records, with every editable setting
  each has in the Settings widget -- but not the calibrations the setup
  file owns, such as the camera pixel size;
* every laser's value and whether it is on;
* the scan the Scan widget holds, as the dict of ``getScanParameters()``;
* where the stages and rotators are, moved there only when you set
  ``MOVE_TO_POSITIONS = True`` -- putting a stage back is a decision of its
  own;
* the acquisition the Recording widget is set to: the scan once into
  memory in a scan mode, otherwise its number of frames into memory, then
  ``show()``.

The script is in cells -- what was set, apply it, acquire -- so it can be
run a step at a time, and it reads only the scripting API, so every line in
it is one you could have written. ``api.imcontrol.getRecordingSettings()``
is what it reads of the Recording widget.

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

**Frames as arrays, nothing written.** ``api.imcontrol.recordScanToMemory()``
runs the scan the Scan widget holds once and returns the frames it
triggers as a NumPy array ``(frames, rows, columns)``, without writing a
file: the recording goes to memory, is read back and dropped. Pass a
detector's name, a list of names (the result is then a dict by name),
``-1`` for the current detector or ``-2`` for all. It blocks until the
scan is over (``timeout`` seconds at most), raises ``RuntimeError`` when
the recording is refused or fails and puts the Recording widget back as
it was; ``keep=True`` leaves the recording listed in ImProcess as well.
``recordFramesToMemory(detector, numFrames)`` does the same for a number
of frames of a camera, with no scan; ``getLatestFrame(detector)`` returns
the newest frame of the live view with no recording at all::

    frames = api.imcontrol.recordScanToMemory('Camera', timeout=120)
    lattice = frames.mean(axis=0)
    dark = api.imcontrol.recordFramesToMemory('Camera', 50).mean(axis=0)
    live = api.imcontrol.getLatestFrame('Camera')      # the live view is on

``tutorial/scanning/06_scan_into_memory.py`` and
``tutorial/basic/10_frames_into_memory.py`` are these in full.

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

``tutorial/scanning/05_laser_power_series.py`` is this pattern in full,
runnable on a mock setup.

Reading and setting the microscope
==================================

Every device the setup file declares can be read as well as set, so a
script can act on what it finds. The names come first --
``getDetectorNames()``, ``getLaserNames()``, ``getPositionerNames()``,
``getRotatorNames()`` -- then, per device:

* detectors: ``getDetectorParameters(name)`` lists every setting with its
  value and units, ``getDetectorParameter`` / ``setDetectorParameter``
  read and set one, ``getDetectorPixelSize(name)`` gives the pixel size in
  the sample in µm;
* lasers: ``getLaserInfo(name)`` says what the laser takes (units, range,
  or on/off only), ``getLaserValue`` / ``setLaserValue`` and
  ``isLaserActive`` / ``setLaserActive`` read and set it;
* positioners: ``getPositionerAxes(name)``,
  ``getPositionerPosition(name, axis)`` (every axis of every stage at once:
  ``getPositionerPositions()``), ``setPositioner`` (absolute) and
  ``movePositioner`` (relative), in µm;
* rotators: ``getRotatorPosition(name)``, ``setRotatorPosition``
  (absolute) and ``moveRotator`` (relative), in degrees.

**The scan as a dict.** ``getScanParameters()`` returns the Scan widget's
settings as a dict -- the scanned axes with their lengths, step sizes and
centres in µm under ``analogParameterDict``, the trigger pulses under
``digitalParameterDict`` -- and ``setScanParameters(params)`` puts an
edited dict back, raising ``ValueError`` that names what could not be
applied. It is ``saveScanParamsToFile`` / ``loadScanParamsFromFile``
without the file::

    params = api.imcontrol.getScanParameters()
    params['analogParameterDict']['axis_step_size'][0] = 0.05     # µm
    api.imcontrol.setScanParameters(params)
    runScanAndWait(timeout=600)

**The focus lock.** ``getFocusLockState()`` is ``'unlocked'``,
``'locked'``, ``'suspended'`` (a scan owns the focus axis and holds the
lock for it), ``'reacquiring'`` (the scan is over, the lock waits for its
signal) or ``'reacquire-failed'``. ``setFocusLocked(True)`` locks where
the sample is, as the widget's **Lock** button does -- the focus camera
must be running, ``setFocusCameraActive(True)`` starts it -- and
``waitForFocusLock(timeout)`` blocks until a lock that a scan suspended is
back, for a time lapse that must not image before it is::

    api.imcontrol.setFocusLocked(True)
    for _ in range(100):
        runScanAndWait(timeout=600)
        if not api.imcontrol.waitForFocusLock(timeout=10):
            raise RuntimeError('the focus did not come back')

:doc:`api/api.imcontrol` lists every function a script can call, with
its signature and the widget that provides it.

.. _scripting-tutorials:

Tutorials
=========

ImSwitch2 copies its scripts into ``~/ImSwitchConfig/scripts``
(``Documents\ImSwitchConfig\scripts`` on Windows) on first start. The
``tutorial`` folder there is a sequence of short scripts that build on each
other. Each is written for a *simulated setup*: a setup file, shipped with
ImSwitch2, whose cameras, stages, scanners and lasers are all mock devices,
so the tutorials run without a microscope. The header of every tutorial
says what it teaches, which setup file to load (``Mock setup:``; choose it
with **Hardware > Pick hardware setup…** in the Hardware Control tab), what
that setup simulates (``It simulates:``), and what your own microscope
needs for the script (``Your own microscope:``). ``tutorial/README.md``
explains how to load a setup and asks you to read each script, predict
what it will do, and only then press **Run all**.

``tutorial/basic``
    One camera (``example_mock.json``, steps 01-06): snapping into numpy
    and to files, camera settings and the live view, recording a number of
    frames and reading the file back, clean-up that survives the Stop
    button, and sharing code between scripts with ``importScript()``. Then
    two cameras, stages, rotation mounts, frames into memory, and working
    interactively with cells, the console and ``show()``
    (``example_no_hardware.json``, steps 07-11).

``tutorial/scanning``
    Scans that trigger a camera, one frame per scan position, and scan
    timelapses (``hamamatsu_mock_scan_setup.json``, steps 01-03); a camera
    and an APD in one scan (``mixed_hamamatsu_apd_mock_scan_setup.json``);
    a laser power series in one recording
    (``galvo_apd_mock_scan_setup.json``); and a scan's frames as an array
    and the scan's settings as a dict (``hamamatsu_mock_scan_setup.json``,
    steps 06-07).
    Scan settings are loaded from files in ``scanning/scan_params``; make
    your own in the Scan widget and save them with
    ``api.imcontrol.saveScanParamsToFile()``.

``workflows``
    Complete acquisitions for real rigs -- see the cookbooks below. These
    need the hardware they name.

``tutorial/README.md`` lists every step. The test suite runs each tutorial
on its mock setup (``imswitch/imscripting/_test/test_shipped_tutorials.py``),
so they are kept working.

The copies are yours to edit. When you install a new ImSwitch2 version, a
copy you have not edited is updated to the new version at the next start,
and an untouched script ImSwitch2 no longer ships is moved to the trash; a
file you edited is never changed or removed (the log names the ones that
differ from the current version -- delete one and restart ImSwitch2 to get
it back).

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
