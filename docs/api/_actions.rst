**********************
Global-level functions
**********************

.. method:: callAndWaitForSignal(signal: imswitch.imcommon.framework.qt.Signal, func: Callable, *args, timeout: Optional[float] = None, **kwargs) -> Any

   Creates a waiter for ``signal``, then calls ``func(*args,
   **kwargs)``, then waits for the signal. Returns what ``func``
   returned. This is the safe way to call an API function and wait for
   the event it causes, because the waiter exists before the call: a
   signal emitted synchronously inside the call is caught too.

   Example: ``callAndWaitForSignal(api.imcontrol.signals().recordingEnded,
   api.imcontrol.stopRecording, timeout=60)``. Raises ``TimeoutError``
   after ``timeout`` seconds and ``OperationCancelled`` if the script is
   stopped.

.. method:: getLogger() -> logging.LoggerAdapter

   Returns a logger instance that can be used to print formatted
   messages to the console.

.. method:: getScriptDirPath() -> str

   Returns the path to the directory containing the running script.

.. method:: getWaitForSignal(signal: imswitch.imcommon.framework.qt.Signal, pollIntervalSeconds: float = 0.05, timeout: Optional[float] = None) -> Callable[[], NoneType]

   Returns a function that will wait for the specified signal to emit.
   The returned function will wait until the signal has been emitted
   since its creation, so **create it before triggering the action** that
   emits the signal (e.g. before ``api.imcontrol.runScan()``); an
   emission that happened before creation is not seen.

   The returned function raises ``TimeoutError`` if ``timeout`` seconds
   pass without an emission (``None`` waits indefinitely) and
   ``OperationCancelled`` if the script is stopped. It accepts an
   optional ``timeout`` argument of its own that overrides the one given
   here. The polling interval defaults to 50 ms.

.. method:: importScript(path: str) -> Any

   Imports the script at the specified path (either absolute or
   relative to the main script) and returns it as a module variable.

.. method:: returningToPositions(stages: Optional[Sequence[str]] = None, rotators: Optional[Sequence[str]] = None)

   A context manager: remembers where ``stages`` (positioner names;
   every axis of each) and ``rotators`` are, and moves them back there on
   leaving the ``with`` block, also when it raises or the script is
   stopped. Separate from the settings managers on purpose: putting a
   stage back is a move, and a script decides on it by itself::

       with returningToPositions(stages=['X', 'Y']):
           for x in range(5):
               api.imcontrol.movePositioner('X', 'X', 10)
               ...

.. method:: runScanAndWait(timeout: Optional[float] = None, source: Optional[str] = None) -> None

   Starts one scan through ``api.imcontrol.runScan`` and waits for
   exactly that scan to end, whatever the timing of its signals. Raises
   ``RuntimeError`` if the start was refused (``ScanRequestRejectedError``)
   or the scan ended unsuccessfully, ``TimeoutError`` after ``timeout``
   seconds, and ``OperationCancelled`` immediately when the script is
   stopped (the running scan then finishes on its own). ``source`` picks
   the scan controller on rigs with several.

.. method:: show(image: Any, name: Optional[str] = None, scale: Optional[Sequence[float]] = None) -> None

   Shows an array in ImControl's image view, as a layer called
   ``name`` ('script' when not given). Showing under the same name again
   replaces that layer's data, so a loop can update one picture. The
   array is copied, so changing it afterwards does not change the
   picture. A stack ``(frames, rows, columns)`` gets a slider. ``scale``
   is the pixel size per axis, last axes last (``(0.1, 0.1)`` for 0.1 µm
   pixels). A dict of arrays, as recordScanToMemory returns for several
   detectors, shows one layer per entry. Raises RuntimeError when no
   image view is loaded (the Image widget of ImControl).

.. method:: sleep(seconds: float) -> None

   Sleeps for the specified number of seconds. Unlike ``time.sleep``,
   this returns immediately (raising ``OperationCancelled``) when the
   script is stopped.

.. method:: temporaryDetectorSettings(detector: str, settings: Optional[Mapping[str, Any]] = None)

   A context manager: sets ``settings`` ({parameter name: value},
   names as in the Settings widget) on the detector, and on leaving the
   ``with`` block -- also when it raises or the script is stopped --
   puts back the values those parameters had::

       with temporaryDetectorSettings('Camera', {'Set exposure time': 0.01}):
           frames = api.imcontrol.recordFramesToMemory('Camera', 10)

   Without ``settings`` it only puts back what the block changes of the
   detector's editable parameters.

.. method:: temporaryLaserSettings(settings: Mapping[str, Any])

   A context manager: sets the lasers in ``settings`` -- {laser: its
   value} or {laser: {'value': ..., 'on': True}} -- and on leaving the
   ``with`` block, also when it raises or the script is stopped, puts
   back each one's value and whether it was on. A laser that was off is
   switched off before its value is restored, one that was on gets its
   value first::

       with temporaryLaserSettings({'488': {'value': 5.0, 'on': True}}):
           frames = api.imcontrol.recordScanToMemory('APD')

.. method:: temporaryScanParameters(parameters: Optional[Mapping[str, Any]] = None)

   A context manager: puts ``parameters`` (a dict from
   getScanParameters, edited) into the Scan widget, and the scan it held
   back on leaving the ``with`` block, also when the block raises or the
   script is stopped. Without ``parameters`` it only puts back what the
   block changes::

       params = api.imcontrol.getScanParameters()
       params['analogParameterDict']['axis_step_size'][0] = 0.05
       with temporaryScanParameters(params):
           frames = api.imcontrol.recordScanToMemory('Camera')

.. method:: waitUntil(predicate: Callable[[], bool], timeout: Optional[float] = None, pollIntervalSeconds: float = 0.05) -> None

   Waits until ``predicate()`` returns a true value. Raises
   ``TimeoutError`` after ``timeout`` seconds (``None`` waits
   indefinitely) and ``OperationCancelled`` if the script is stopped.

