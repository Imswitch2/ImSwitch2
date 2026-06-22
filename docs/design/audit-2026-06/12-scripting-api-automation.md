# Scripting/API Automation Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-22
**Scope:** `imswitch/imscripting`, cross-module script dispatch, and the
`imcontrol` scripting API used by remote or automated workflows.

## Summary

The scripting module is intentionally powerful and should be treated as a
trusted local automation surface, not a sandbox. That is acceptable for many
microscope workflows. The problem is that its lifecycle and result contracts are
not robust enough for unattended smart microscopy: scripts are stopped by
terminating the Qt thread while process-wide stdout/stderr are redirected,
remote script execution has no job id or error result, and scripts are given raw
controller access in addition to the curated APIs. This makes it easy for
future ET, tiling, and mode-switch workflows to depend on private controller
details or to fail in ways that callers cannot diagnose.

## Findings

### [P1] Script cancellation can kill a worker while global stdout/stderr are redirected

**Sites:**

- `imswitch/imscripting/model/ScriptExecutor.py:28-30`
- `imswitch/imscripting/model/ScriptExecutor.py:34-49`
- `imswitch/imscripting/model/ScriptExecutor.py:70-89`
- `imswitch/imscripting/controller/EditorController.py:117-129`

**Evidence:**

`ScriptExecutor.execute()` first calls `terminate()` and then starts the worker
thread. `terminate()` calls `self._executionThread.terminate()` for an active
script. The worker redirects process-wide `sys.stdout` and `sys.stderr` to a
`SignaledStringIO` while running `exec(code, scriptScope)`, and only restores
them in `finally`.

**Impact:**

`QThread.terminate()` can stop the thread at an arbitrary instruction. If that
happens while stdout/stderr are redirected, the process can keep writing into a
script output object after the script is "stopped". The same abrupt termination
can also leave hardware state, temporary signal connections, files, or manager
operations half-completed. This is a high-risk lifecycle primitive for
unattended automation.

**Next fix:**

Replace hard termination with cooperative cancellation:

- Add a script job object with a cancellation token exposed through the action
  scope.
- Let blocking helper actions check the token and return or raise a controlled
  cancellation exception.
- Make stop/replace wait for the current script to finish cleanup or time out
  with an explicit failure state.
- Avoid process-wide stdout/stderr redirection where possible; otherwise never
  combine it with forcible thread termination.

### [P1] Remote script execution has no job/result/error contract

**Sites:**

- `imswitch/imcommon/controller/ModuleCommunicationChannel.py:10-11`
- `imswitch/imscripting/controller/EditorController.py:22-24`
- `imswitch/imcontrol/controller/CommunicationChannel.py:420-432`

**Evidence:**

The module communication channel exposes only `sigRunScript(str)` and
`sigExecutionFinished()`. `EditorController` forwards executor completion to the
module-level finish signal without status or error information. The imcontrol
API resets `self.output`, marks `_scriptExecution = True`, emits the script
text, and later only clears the flag when any script completion signal arrives.

**Impact:**

Callers cannot correlate a completion event with a specific script request, tell
success from failure, retrieve a structured traceback, or enforce a timeout.
`CommunicationChannel.output` is a mutable side channel populated by helper APIs
such as `acquireImage()`. That is too implicit for workflows that may scout,
switch modes, visit targets, and trigger event-gated recordings.

**Next fix:**

Introduce a small script-job protocol:

- `ScriptJobRequest(id, code_or_path, working_dir, metadata)`.
- `ScriptJobState(id, status, stdout, stderr, error, started_at, finished_at)`.
- Signals include the job id and state.
- The public API returns or awaits the matching job result instead of relying on
  shared mutable `output`.

### [P1] Scripts can bypass curated APIs through raw controller access

**Sites:**

- `imswitch/imscripting/controller/ImScrMainController.py:37-52`

**Evidence:**

The script scope includes both `api`, which is built from controller-exported
APIs, and `controllers`, which is a read-only wrapper around all module main
controllers. It also exposes `moduleCommChannel` directly.

**Impact:**

Scripts can depend on controller internals, private attributes, and signal-bus
details that were never intended as stable automation contracts. That makes
local scripts convenient in the short term, but it undermines the goal of a
clean extension architecture where new ET variants, tiling workflows, or setup
modes can be built against stable APIs.

**Next fix:**

Make the curated `api` facade the default scripting surface. Keep raw
controllers only behind an explicit developer escape hatch such as
`unsafeControllers`, a config flag, or a debug action scope, and document that it
is not a compatibility contract.

### [P1] External script dispatch uses a hardcoded personal Windows path

**Sites:**

- `imswitch/imscripting/controller/EditorController.py:163-164`

**Evidence:**

`EditorController.runScript()` executes externally emitted script text with:
`r'C:\Users\xavie\OneDrive\Documents\ImSwitchConfig\scripts'`.

**Impact:**

Any script sent through the module communication channel receives a script path
that is machine-specific, Windows-specific, and unrelated to the current user's
configuration. Relative imports and helpers such as `getScriptDirPath()` can
resolve against the wrong location or fail differently across machines.

**Next fix:**

Use `None`, the configured user scripts directory, or an explicit working
directory from the new script-job request. Avoid pretending that dynamically
submitted code has a real file path unless it does.

### [P2] Script helper actions can hang or leak signal connections

**Sites:**

- `imswitch/imscripting/model/actions.py:20-39`
- `imswitch/imscripting/model/actions.py:41-45`
- `imswitch/imscripting/model/actions.py:53-76`

**Evidence:**

`importScript()` executes a module before injecting the ImSwitch script scope,
so imported code cannot use the injected API during import-time execution even
though the helper presents it as a script import. `getScriptDirPath()` calls
`os.path.dirname(self._scriptPath)` even when the current script path may be
`None`. `getWaitForSignal()` waits forever by default and disconnects only after
the signal has been emitted.

**Impact:**

Automation scripts can hang indefinitely while waiting for a signal that never
arrives. If a script is interrupted before the wait completes, the temporary
signal slot can remain connected. Dynamic scripts submitted without a real file
path can also fail in helper actions that assume one exists.

**Next fix:**

Add timeout and cancellation support to wait helpers, disconnect in `finally`,
and make `getScriptDirPath()` return `None` or the configured scripts root when
there is no saved script. If `importScript()` is kept, inject the script scope
before executing imported code or document that it is a plain Python import.

### [P2] Event/script signal grouping is still drifting

**Sites:**

- `imswitch/imcontrol/controller/CommunicationChannel.py:106-112`
- `imswitch/imcontrol/controller/CommunicationChannel.py:201-205`
- `imswitch/imcontrol/controller/CommunicationChannel.py:220-222`

**Evidence:**

`sigInitiateEtSnouty` is declared next to the other ET initiation signals, but
`eventTriggeredEvents` exposes only `initiateEtMonalisa`, `initiateEt`, and
`clockWidefield`. Script events expose only `scriptExecutionFinished`, without
start/error/result signals.

**Impact:**

The grouped signal surface is meant to reduce direct dependency on the large
legacy signal bus, but it can already omit live compatibility signals. That
makes it harder for new automation code to know which surface is authoritative.

**Next fix:**

Treat grouped event surfaces as generated or validated contracts. Add tests
that assert important legacy signals are either intentionally grouped or
explicitly marked deprecated, and expand the script event group when the job
protocol exists.

## Suggested sequencing

1. Remove the hardcoded Windows script path and make dynamic script execution
   pass `None` or a configured working directory.
2. Replace `QThread.terminate()` with cooperative cancellation before adding
   more unattended workflows on top of scripts.
3. Add structured script job ids, status, errors, stdout/stderr, and result
   propagation across `ModuleCommunicationChannel` and `imcontrol` APIs.
4. Move raw controller access behind an explicit unsafe/developer scripting
   surface.
5. Harden helper actions with timeout, cancellation, and guaranteed signal
   disconnection.
6. Validate grouped event/script signal surfaces so they cannot silently drift
   from the legacy compatibility signals.
