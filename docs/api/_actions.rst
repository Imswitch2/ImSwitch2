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

.. method:: runScanAndWait(timeout: Optional[float] = None, source: Optional[str] = None) -> None

   Starts one scan through ``api.imcontrol.runScan`` and waits for
   exactly that scan to end, whatever the timing of its signals. Raises
   ``RuntimeError`` if the start was refused (``ScanRequestRejectedError``)
   or the scan ended unsuccessfully, ``TimeoutError`` after ``timeout``
   seconds, and ``OperationCancelled`` immediately when the script is
   stopped (the running scan then finishes on its own). ``source`` picks
   the scan controller on rigs with several. 

.. method:: scriptInput(name: str, default: Any = None, description: str = '') -> Any

   Returns the value given for the input ``name`` when the script was
   started with inputs (e.g. from the ScriptRunner widget), otherwise
   ``default``. The ScriptRunner widget finds these calls in the script
   without running it and shows a field for each, so write the name (and
   ideally the default) as a plain value: ``exposure =
   scriptInput('exposure_ms', 10.0, 'Exposure per frame')``. A value given
   as text is converted to the type of the default. 

.. method:: sleep(seconds: float) -> None

   Sleeps for the specified number of seconds. Unlike ``time.sleep``,
   this returns immediately (raising ``OperationCancelled``) when the
   script is stopped. 

.. method:: waitUntil(predicate: Callable[[], bool], timeout: Optional[float] = None, pollIntervalSeconds: float = 0.05) -> None

   Waits until ``predicate()`` returns a true value. Raises
   ``TimeoutError`` after ``timeout`` seconds (``None`` waits
   indefinitely) and ``OperationCancelled`` if the script is stopped. 

