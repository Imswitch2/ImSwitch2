# Remote Server/API Follow-up Audit

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-22
**Scope:** optional `pyroServerInfo` server startup, FastAPI/Pyro exposure,
shared API generation, and remote-control safety contracts.

## Summary

The local Python scripting API is actively used, but the optional remote server
path is not production-ready. It appears dormant in bundled setups, yet it is
installed by default and wired into `ImConMainController` when
`pyroServerInfo.active` is enabled. The server mixes FastAPI and Pyro in one
worker, starts the blocking HTTP server before Pyro can run, cannot shut down
cleanly, exposes all exported microscope methods without an authentication or
admission layer, and relies on an API wrapper that does not propagate return
values or UI-thread exceptions.

## Findings

### [P1] Optional server startup/shutdown is structurally broken

**Sites:**

- `imswitch/imcontrol/model/SetupInfo.py:397-403`
- `imswitch/imcontrol/controller/ImConMainController.py:198-205`
- `imswitch/imcontrol/controller/server/ImSwitchServer.py:27-45`
- `imswitch/imcontrol/controller/server/ImSwitchServer.py:47-48`

**Evidence:**

`PyroServerInfo.active` defaults to `False`, but when it is enabled
`ImConMainController` moves `ImSwitchServer` to a thread and connects thread
finish to `self._serverWorker.stop`. `ImSwitchServer.run()` calls
`uvicorn.run(app)` before configuring or serving Pyro. That call is blocking, so
the Pyro `serve()` block below it is not reached while HTTP serving is active.
`stop()` calls `self._daemon.shutdown()`, but `_daemon` is never assigned in the
class.

**Impact:**

Enabling the optional server can hang inside the FastAPI server, fail to start
the configured Pyro endpoint, and fail during shutdown. This path is exactly the
kind of remote-control entry point that future unattended ET/tiling workflows
could be tempted to use, so it should not remain half-wired.

**Next fix:**

Either remove/disable the optional server until it is redesigned, or replace it
with a single explicit server runtime:

- Use one protocol first, not a hidden FastAPI+Pyro hybrid.
- Honor configured host/port/name for the selected protocol.
- Store the server handle and implement deterministic start/stop/finalize.
- Add a small test that starts the server on an ephemeral port and shuts it down.

### [P1] Remote API exposes microscope actions without an authorization or safety boundary

**Sites:**

- `imswitch/imcontrol/controller/server/ImSwitchServer.py:10`
- `imswitch/imcontrol/controller/server/ImSwitchServer.py:50-74`
- `imswitch/imcontrol/controller/ImConMainController.py:100-107`
- `imswitch/imcontrol/controller/controllers/PositionerController.py:250-275`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:448-519`

**Evidence:**

The module-level FastAPI app has no authentication, token check, local-only
guard, or capability filter. `createAPI()` registers every exported API function
as a GET route. `ImConMainController` builds the API from all widget
controllers, the setup-mode controller, the communication channel, and the
workflow facade. Among the exported methods are motion and motor enable calls,
recording controls, scan controls, mode changes, and script dispatch.

**Impact:**

With the default host this is local-only, but the setup schema exposes `host` as
configuration. If a setup binds the server beyond localhost, anyone who can
reach the port can call hardware-affecting operations. Even on localhost, there
is no admission concept for "read-only", "movement", "laser", "recording", or
"script execution" capabilities.

**Next fix:**

Put a remote-control policy in front of exported methods:

- Require explicit opt-in for remote control and a token or local IPC boundary.
- Classify API exports by capability/risk.
- Keep read-only status calls separate from hardware movement, laser emission,
  recording, and script execution.
- Add logging/audit metadata for remote calls that affect hardware state.

### [P1] UI-thread API calls do not return results or propagate errors

**Sites:**

- `imswitch/imcommon/model/api.py:40-45`
- `imswitch/imcommon/model/api.py:51-78`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:440-445`
- `imswitch/imcontrol/controller/controllers/RecordingController.py:529-531`
- `imswitch/imcontrol/controller/CommunicationChannel.py:420-423`

**Evidence:**

`generateAPI()` wraps every `@APIExport(runOnUIThread=True)` method in
`_UIThreadExecWrapper`. The wrapper stores args, emits a Qt signal, and returns
without returning the wrapped function's result. `_apiCall()` calls the real
function and unlocks a mutex in `finally`, but it also discards the return
value. Exported methods such as `snapImage(output=True)` and `getRecFolder()`
declare return values while being routed through this wrapper.

**Impact:**

Remote callers and scripts cannot reliably use return values from UI-thread API
methods. Exceptions raised in the UI-thread slot also cannot be reported as a
structured caller error. This explains why some API paths use side channels such
as `CommunicationChannel.output`, but that does not scale to robust workflow
orchestration.

**Next fix:**

Replace `_UIThreadExecWrapper` with a request/result primitive:

- A call id, arguments, result, exception, and completion event.
- Optional timeout and cancellation.
- Correct behavior when called from the UI thread directly.
- Tests that cover successful return values and propagated exceptions from
  non-UI threads.

### [P2] FastAPI/Pyro route generation is fragile and not a typed API contract

**Sites:**

- `imswitch/imcontrol/controller/server/ImSwitchServer.py:10`
- `imswitch/imcontrol/controller/server/ImSwitchServer.py:50-74`

**Evidence:**

The FastAPI app is global. `createAPI()` is itself decorated as `GET /` and also
called from `run()`, dynamically adding routes to that global app each time.
Every exported function is registered as a GET endpoint through a generic
`async def wrapper(*args, **kwargs)`. The Pyro wrapper is assigned to
`self.func` inside the loop, overwriting that same attribute for each exported
function.

**Impact:**

Repeated server startup can accumulate duplicate global routes. The HTTP API has
no typed request/response models, no clear body/query semantics for complex
arguments, and no structured error envelope. The Pyro side is unlikely to expose
one method per API function because the same dynamic attribute is overwritten.

**Next fix:**

Generate a stable API schema before startup and bind it once. For HTTP, use
typed request models and POST for side-effecting calls. For Pyro, expose a
single dispatcher method with method name, call id, args, kwargs, and structured
result, or attach uniquely named exposed methods deliberately.

### [P2] API export namespace is flat and extension-hostile

**Sites:**

- `imswitch/imcommon/model/api.py:25-48`
- `imswitch/imcontrol/controller/ImConMainController.py:100-107`
- `imswitch/imcontrol/controller/controllers/WorkflowFacadeController.py:37-43`

**Evidence:**

`generateAPI()` scans all objects and stores exported functions in one
dictionary by method name. A duplicate method name raises `NameError`. The
workflow facade documentation notes that the API is flattened to
`api.imcontrol.buildWorkflowFacade(...)` rather than being namespaced under the
controller.

**Impact:**

External controllers, plugins, and new workflow facades cannot safely export a
method name that another controller already uses. That turns the whole
application into one global API namespace and makes extension compatibility
depend on incidental method names.

**Next fix:**

Introduce namespaced exports while keeping the flat API as a compatibility shim:

- `api.imcontrol.positioner.move(...)`
- `api.imcontrol.recording.start(...)`
- `api.imcontrol.setupModes.apply(...)`
- `api.imcontrol.workflows.buildFacade(...)`

New plugins should register into their own namespace by default.

### [P2] Remote ndarray serialization can leak shared memory on failed clients

**Sites:**

- `imswitch/imcontrol/controller/server/_serialize.py:48-68`
- `imswitch/imcontrol/controller/server/_serialize.py:71-79`
- `imswitch/imcontrol/controller/server/_serialize.py:81-107`

**Evidence:**

`SerNDArray.to_dict()` creates a `SharedMemory` block and appends it to a
process-global deque capped at 15 entries. The receiver unlinks the block in
`from_dict()`. Cleanup also runs at process exit, and
`remove_shm_from_resource_tracker()` monkey-patches Python's resource tracker so
shared memory is not tracked normally.

**Impact:**

If a client disconnects, fails before deserializing, or never calls
`from_dict()`, shared memory ownership remains ambiguous until process exit.
When more than 15 arrays are sent, old `SharedMemory` handles can fall out of
the deque without an explicit unlink path. This is risky for remote image
streaming, especially if future workflows use the server for camera frames.

**Next fix:**

Use explicit lease/ack cleanup for image payloads, or avoid server-side shared
memory for the first supported remote-control API. If shared memory remains,
track lifetime per call id and add timeout cleanup tests.

## Suggested sequencing

1. Mark `pyroServerInfo.active` as experimental or unsupported in docs until
   server startup/shutdown is fixed.
2. Decide on one supported remote-control protocol and remove the hidden
   FastAPI/Pyro hybrid from the worker.
3. Replace UI-thread API execution with a call/result/error primitive.
4. Add authorization/capability policy before exposing hardware-affecting
   methods beyond local scripting.
5. Introduce namespaced API exports and keep the current flat API as a
   compatibility layer.
6. Revisit ndarray/image transport only after the remote call lifecycle has
   call ids and deterministic cleanup.
